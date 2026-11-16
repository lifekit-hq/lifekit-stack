#!/usr/bin/env python3
"""The ledger-pulse gate's check: does this pulse run need a model turn at all?

    pulse_gate.py check

The `ledger-pulse` cron row (docs/runbook.md, "The finance heartbeat, retired for
an isolated cron job") runs scripts/finance-pulse.md three times a day, and most
runs end in NO_REPLY. gate.js, the row's trigger script, runs this first; the
row's agent turn is only started when it prints "PULSE wake". Read-only: it
calls two finance-sentry MCP tools and acknowledges, records and changes nothing.

It wakes the agent when any of these holds:
  * the run starts at or before 09:22 Europe/Dublin - the morning slot, where
    the checklist's coverage-memo step (7) and, on a Sunday, the weekly
    positioning digest (6) belong to the run;
  * get_pending_companion_events (held-for-digest events included, as step 1
    pulls them) returns any event;
  * get_sync_health shows an account provider whose last good sync is more than
    24h old (step 4). A provider that never synced is not stale, and an external
    data source (kind data_source) is not a provider.

Output, first line: "PULSE idle" or "PULSE wake" (reason lines follow). A fault
(finance-sentry unreachable, token refused, an answer of an unknown shape) prints
"PULSE fault: ..." and exits 1, so the row shows the error and its failure alert
fires instead of the pulse going silently quiet.

Environment: FINANCE_SENTRY_MCP_URL (default http://finance-sentry-mcp:5100/) and
FINANCE_SENTRY_MCP_TOKEN, the bearer the gateway's finance-sentry MCP entry uses.
PULSE_GATE_NOW (an ISO-8601 instant) replaces the clock, for tests.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Dublin")
MORNING_CUTOFF = (9, 22)  # the checklist's "at or before 09:22 Europe/Dublin"
STALE_AFTER = timedelta(hours=24)
HELD_REASON = "ledger-pulse gate check"
TIMEOUT = 20


class Fault(RuntimeError):
    pass


def _url() -> str:
    return os.environ.get("FINANCE_SENTRY_MCP_URL", "http://finance-sentry-mcp:5100/")


def _post(body: dict, session: str | None = None):
    token = os.environ.get("FINANCE_SENTRY_MCP_TOKEN", "")
    if not token:
        raise Fault("FINANCE_SENTRY_MCP_TOKEN is not set")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(  # noqa: S310 - fixed internal http URL
        _url(), data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
            return resp.headers.get("Mcp-Session-Id"), resp.read().decode()
    except urllib.error.HTTPError as exc:
        raise Fault(f"finance-sentry MCP answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise Fault(f"finance-sentry MCP unreachable: {exc}") from exc


def _message(text: str, rpc_id: int) -> dict:
    """The JSON-RPC answer with this id, from a JSON body or an SSE stream."""
    candidates = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            candidates.append(line)
    if not candidates:
        candidates = [text]
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("id") == rpc_id:
            return obj
    raise Fault("finance-sentry MCP answered with no JSON-RPC result")


def call_tool(name: str, arguments: dict):
    """One tools/call; returns the tool's JSON value."""
    session, _ = _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ledger-pulse-gate", "version": "1"},
            },
        }
    )
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    _, text = _post(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        session,
    )
    msg = _message(text, 2)
    result = msg.get("result")
    if msg.get("error") or not isinstance(result, dict) or result.get("isError"):
        raise Fault(f"{name} failed")
    if result.get("structuredContent") is not None:
        value = result["structuredContent"]
        # A tool returning a list is wrapped as {"result": [...]} by the SDK.
        if isinstance(value, dict) and set(value) == {"result"}:
            return value["result"]
        return value
    for part in result.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            try:
                return json.loads(part["text"])
            except ValueError as exc:
                raise Fault(f"{name} answered with text that is not JSON") from exc
    raise Fault(f"{name} answered with no content")


def now() -> datetime:
    raw = os.environ.get("PULSE_GATE_NOW")
    if raw:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def morning_slot(at: datetime) -> bool:
    local = at.astimezone(TZ)
    return (local.hour, local.minute) <= MORNING_CUTOFF


def pending_events() -> int:
    value = call_tool(
        "get_pending_companion_events",
        {"includeHeldForDigest": True, "heldOverrideReason": HELD_REASON},
    )
    events = value.get("events") if isinstance(value, dict) else None
    if not isinstance(events, list):
        raise Fault("get_pending_companion_events answered in an unknown shape")
    return len(events)


def stale_providers(at: datetime) -> list[str]:
    value = call_tool("get_sync_health", {})
    if not isinstance(value, list):
        raise Fault("get_sync_health answered in an unknown shape")
    stale = []
    for entry in value:
        if not isinstance(entry, dict) or entry.get("kind") == "data_source":
            continue
        last = entry.get("lastSyncAt")
        if not last:
            continue
        try:
            when = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except ValueError as exc:
            raise Fault("get_sync_health answered with an unreadable time") from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if at - when > STALE_AFTER:
            stale.append(str(entry.get("provider", "?")))
    return stale


def check() -> list[str]:
    """The reasons to wake the agent; empty means this run needs no turn."""
    at = now()
    if morning_slot(at):
        return ["morning slot (coverage memos; the weekly digest on a Sunday)"]
    reasons = []
    count = pending_events()
    if count:
        reasons.append(f"{count} pending companion event(s)")
    stale = stale_providers(at)
    if stale:
        reasons.append("provider sync older than 24h: " + ", ".join(stale))
    return reasons


def main(argv: list[str]) -> int:
    if argv != ["check"]:
        print("usage: pulse_gate.py check", file=sys.stderr)
        return 2
    try:
        reasons = check()
    except Fault as exc:
        print(f"PULSE fault: {exc}")
        return 1
    print("\n".join(["PULSE wake", *reasons]) if reasons else "PULSE idle")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
