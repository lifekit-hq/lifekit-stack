"""learn-mcp: a claude.ai custom connector over learnclaw's learning record
(docs/runbook.md, "learn-mcp (the study coach's connector)").

Two tools and nothing else: `learning_context` reads today's problem, the open
problems, the week, the page's In-progress lines and the LearnHarness gaps;
`record_artefact` appends one row through skills/learning-record/record.py, the
record's only write path, unchanged. No tool takes a path, runs a command of
the caller's choosing, or reads anything but the learning page and the mirror.

The gate runs before the MCP app sees a byte, in this order:
  1. /health, /ready and /metrics answer only a direct caller (Prometheus and
     the platform contract on lifekit-shared). A request that came through
     the edge carries X-Forwarded-For and gets 404 there.
  2. A forwarded request from outside LEARN_MCP_ALLOW_CIDRS (Anthropic's
     published outbound ranges) gets 403. Tailscale Funnel sets
     X-Forwarded-For to the connecting address, replacing whatever the client
     sent, so its first entry is the real client (scripts/deploy-edge.sh
     checks Funnel serves nothing else).
  3. Every other request needs `Authorization: Bearer <LEARN_MCP_TOKEN>`,
     compared in constant time; anything else gets 401. With no token set,
     every request gets 401.
  4. A body over LEARN_MCP_MAX_BODY gets 413; over LEARN_MCP_RATE_PER_MIN
     requests a minute get 429.

The access log is one JSON line per request: time, source address, method,
status, the MCP method and the tool name. Tool arguments and results are never
logged.
"""

from __future__ import annotations

import datetime as dt
import hmac
import ipaddress
import json
import logging
import logging.config
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path
from typing import Any

import context
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

HERE = Path(__file__).resolve().parent
RECORD_PY = Path(
    os.environ.get("LEETCODE_RECORD_PY") or HERE / "skills/learning-record/record.py"
)
# Anthropic's published outbound IP ranges for claude.ai connectors.
DEFAULT_CIDRS = "160.79.104.0/21,153.61.208.0/20"
LOCAL_PATHS = ("/health", "/ready", "/metrics")
MCP_PATH = "/mcp"
STATUSES = ("in progress", "done", "dropped")

log = logging.getLogger("learn-mcp")


# --- logging and metrics -------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update(getattr(record, "fields", {}))
        if record.exc_info:
            out["error"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(out)


def configure_logging() -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {
                "out": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["out"], "level": "INFO"},
            "loggers": {
                # Our access line replaces uvicorn's; its errors stay, as JSON.
                "uvicorn.access": {"handlers": [], "propagate": False},
                "uvicorn": {"handlers": ["out"], "level": "INFO", "propagate": False},
                # The SDK logs each request at INFO; one access line is enough.
                "mcp": {"level": "WARNING"},
            },
        }
    )


class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: Counter[str] = Counter()
        self.tools: Counter[tuple[str, str]] = Counter()

    def request(self, outcome: str) -> None:
        with self.lock:
            self.requests[outcome] += 1

    def tool(self, name: str, result: str) -> None:
        with self.lock:
            self.tools[(name, result)] += 1

    def render(self) -> str:
        with self.lock:
            lines = [
                "# HELP learn_mcp_requests_total Requests by gate outcome.",
                "# TYPE learn_mcp_requests_total counter",
            ]
            for outcome in (
                "ok",
                "unauthorized",
                "forbidden",
                "rate_limited",
                "too_large",
            ):
                lines.append(
                    f'learn_mcp_requests_total{{outcome="{outcome}"}} '
                    f"{self.requests[outcome]}"
                )
            lines += [
                "# HELP learn_mcp_tool_calls_total Tool calls by tool and result.",
                "# TYPE learn_mcp_tool_calls_total counter",
            ]
            for tool in ("learning_context", "record_artefact"):
                for result in ("ok", "refused", "error"):
                    lines.append(
                        f'learn_mcp_tool_calls_total{{tool="{tool}",result="{result}"}} '
                        f"{self.tools[(tool, result)]}"
                    )
        return "\n".join(lines) + "\n"


METRICS = Metrics()


# --- the tools -------------------------------------------------------------------------------


def build_mcp() -> MCPServer:
    mcp = MCPServer(
        "learn-mcp",
        instructions=(
            "The owner's learning record. Call learning_context first. Call "
            "record_artefact only after the owner has said, in their own words, "
            "what to record; never invent the pattern or the complexity."
        ),
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Learning context", read_only_hint=True, open_world_hint=False
        )
    )
    def learning_context() -> dict[str, Any]:
        """Today's LeetCode problem (or why there is none today, and the next
        pick), the open problems, this week's solved count, recent record rows,
        the learning page's In-progress lines, and recent gaps (wrong, unsure,
        don't-know and skipped answers) from the LearnHarness record with the
        newest session's date. Read-only."""
        try:
            out = context.learning_context()
        except SystemExit as e:  # the skills' own `fail`: a missing page or list
            METRICS.tool("learning_context", "error")
            raise ToolError(f"the learning record cannot be read (exit {e.code})")
        except Exception:
            METRICS.tool("learning_context", "error")
            log.exception("learning_context failed")
            raise ToolError("the learning record cannot be read")
        METRICS.tool("learning_context", "ok")
        return out

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Record a learning row",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        )
    )
    def record_artefact(
        topic: str,
        artefact: str,
        status: str = "in progress",
        grilled: str = "-",
        reading: str = "-",
        applied: str = "-",
        post: str = "-",
    ) -> str:
        """Append one row to the owner's artefact record, dated today. Only on
        the owner's explicit approval, with their own words. A daily problem is
        topic `leetcode: <slug>`; `done` needs `grilled` to hold the pattern and
        the time and space complexity as the owner said them. Status is one of
        "in progress", "done", "dropped". The record is append-only: a
        correction is a new row. Refusals (a pipe or line break in a cell, a
        missing field, a lint finding) write nothing."""
        return append_row(
            topic=topic,
            artefact=artefact,
            status=status,
            grilled=grilled,
            reading=reading,
            applied=applied,
            post=post,
        )

    return mcp


def append_row(**cells: str) -> str:
    if cells["status"] not in STATUSES:
        METRICS.tool("record_artefact", "refused")
        raise ToolError(f"status must be one of {', '.join(STATUSES)}")
    date = context.load_daily().today().isoformat()
    argv = [sys.executable, str(RECORD_PY), "append", f"--date={date}"]
    argv += [f"--{k}={v}" for k, v in cells.items()]
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=120, check=False
        )
    except subprocess.TimeoutExpired:
        METRICS.tool("record_artefact", "error")
        raise ToolError("the record did not answer in time; nothing confirmed")
    if r.returncode == 0:
        METRICS.tool("record_artefact", "ok")
        return r.stdout.strip()
    METRICS.tool("record_artefact", "refused" if r.returncode in (1, 2) else "error")
    # record.py's refusal names the rule, never the vault's other content.
    raise ToolError((r.stderr.strip() or f"record.py exited {r.returncode}")[:1000])


# --- the gate ------------------------------------------------------------------------------


def parse_cidrs(text: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(c.strip()) for c in text.split(",") if c.strip()]


class Gate:
    def __init__(
        self,
        app,
        *,
        token: str,
        cidrs: list,
        rate_per_min: int,
        max_body: int,
        ready,
    ) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode() if token else None
        self.cidrs = cidrs
        self.rate = rate_per_min
        self.max_body = max_body
        self.ready = ready
        self.recent: deque[float] = deque()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        headers = {k.decode("latin-1").lower(): v for k, v in scope["headers"]}
        forwarded = headers.get("x-forwarded-for", b"").decode("latin-1")
        src = (
            forwarded.split(",")[0].strip()
            if forwarded
            else (scope.get("client") or ("", 0))[0]
        )
        entry = {
            "trace_id": uuid.uuid4().hex,
            "src": src,
            "method": scope["method"],
            "path": scope["path"],
        }

        async def reply(status: int, body: bytes, ctype: str = "text/plain") -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", ctype.encode())],
                }
            )
            await send({"type": "http.response.body", "body": body})
            self._log(entry, status, started)

        if scope["path"] in LOCAL_PATHS:
            if forwarded:
                await reply(404, b"not found\n")
            elif scope["path"] == "/health":
                await reply(200, b"ok\n")
            elif scope["path"] == "/ready":
                why = self.ready()
                await reply(503 if why else 200, (why or "ready").encode() + b"\n")
            else:
                await reply(200, METRICS.render().encode(), "text/plain; version=0.0.4")
            return

        if forwarded and self.cidrs and not self._allowed(src):
            METRICS.request("forbidden")
            await reply(403, b"forbidden\n")
            return
        given = headers.get("authorization", b"")
        if self.expected is None or not hmac.compare_digest(given, self.expected):
            METRICS.request("unauthorized")
            await reply(401, b"unauthorized\n")
            return
        if not self._take():
            METRICS.request("rate_limited")
            await reply(429, b"too many requests\n")
            return

        body = b""
        more = True
        while more:
            msg = await receive()
            if msg["type"] != "http.request":
                return  # the client went away
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > self.max_body:
                METRICS.request("too_large")
                await reply(413, b"request too large\n")
                return
        METRICS.request("ok")
        entry.update(describe(body))

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        status = {"code": 0}

        async def capture(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, replay, capture)
        finally:
            self._log(entry, status["code"] or 500, started)

    def _allowed(self, src: str) -> bool:
        try:
            ip = ipaddress.ip_address(src)
        except ValueError:
            return False
        return any(ip in net for net in self.cidrs)

    def _take(self) -> bool:
        now = time.monotonic()
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()
        if len(self.recent) >= self.rate:
            return False
        self.recent.append(now)
        return True

    @staticmethod
    def _log(entry: dict[str, Any], status: int, started: float) -> None:
        if entry["path"] in LOCAL_PATHS and status == 200:
            return  # probes every few seconds would drown the access log
        fields = {
            **entry,
            "status": status,
            "ms": round((time.monotonic() - started) * 1000),
        }
        log.info("request", extra={"fields": fields})


def describe(body: bytes) -> dict[str, str]:
    """The JSON-RPC method and, for a tool call, the tool's name. Nothing else."""
    try:
        msg = json.loads(body)
    except ValueError:
        return {}
    calls = msg if isinstance(msg, list) else [msg]
    out: dict[str, str] = {}
    methods = [c.get("method") for c in calls if isinstance(c, dict)]
    if methods and all(isinstance(m, str) for m in methods):
        out["rpc"] = ",".join(methods)[:200]
    tools = [
        c["params"].get("name")
        for c in calls
        if isinstance(c, dict)
        and c.get("method") == "tools/call"
        and isinstance(c.get("params"), dict)
        and isinstance(c["params"].get("name"), str)
    ]
    if tools:
        out["tool"] = ",".join(tools)[:200]
    return out


def ready() -> str:
    """Empty when the record can be read and written through its skill; else why not."""
    vault = Path(os.environ.get("LEARNING_VAULT") or "/vault")
    page = vault / os.environ.get("LEARNING_PAGE", "domains/learning.md")
    if not os.environ.get("LEARN_MCP_TOKEN"):
        return "no LEARN_MCP_TOKEN"
    if not page.is_file():
        return "no learning page"
    if not (vault / "bin/lint.sh").is_file():
        return "no vault lint"
    if not os.access(page, os.W_OK) or not os.access(page.parent, os.W_OK):
        return "learning page not writable"
    return ""


def build_app():
    mcp = build_mcp()
    # Stateless JSON: one POST in, one JSON reply out, no session to pin, so a
    # restart or a second claude.ai worker never loses state. Host checking is
    # off because the bearer and the edge's path rule are the gate; the Host is
    # the public tailnet name, which this service does not need to know.
    inner = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    return Gate(
        inner,
        token=os.environ.get("LEARN_MCP_TOKEN", ""),
        cidrs=parse_cidrs(os.environ.get("LEARN_MCP_ALLOW_CIDRS", DEFAULT_CIDRS)),
        rate_per_min=int(os.environ.get("LEARN_MCP_RATE_PER_MIN", "60")),
        max_body=int(os.environ.get("LEARN_MCP_MAX_BODY", "65536")),
        ready=ready,
    )


def main() -> None:
    import uvicorn

    configure_logging()
    port = int(os.environ.get("LEARN_MCP_PORT", "8099"))
    if not os.environ.get("LEARN_MCP_TOKEN"):
        log.warning("no LEARN_MCP_TOKEN: every MCP request gets 401")
    uvicorn.run(
        build_app(),
        host="0.0.0.0",  # noqa: S104 - the container's own interface; no host port
        port=port,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
