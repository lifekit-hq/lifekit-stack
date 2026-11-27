#!/usr/bin/env python3
"""unrouted - the daily check that every ask and promise in the agents' chats has
a receipt (lifekit-stack#687, docs/runbook.md "Daily unrouted check").

The receipts block (defaults/agents/shared/receipts.md) asks every agent to file
an owner ask or its own promise in the turn it is said and show the receipt id.
This job is the mechanical safety net behind it. Once a day it:

  1. reads every agent's chat of one UTC day (the previous one by default)
     read-only from the gateway session store (chats.mjs, run in the gateway
     through `docker exec`; the text stays in memory on the host);
  2. keeps the conversations the owner took part in (a message the gateway
     marked senderIsOwner), leaving out cron runs and deploy smoke turns;
  3. has a model list the owner's asks and the agent's promises in each
     conversation (`claude -p`, no tools, no MCP, no settings; the cheapest
     model that passes the replay test, docs/runbook.md);
  4. looks for each one's receipt: the id the model saw cited, plus every id an
     agent reply in the same turn shows, checked where it lives - a firstmate
     task note (`<home>/state/inbox[/handled]/<id>.note`), a captain-held or
     shaping item (`held-*`, `shape-*`, fm-captain-hold.sh in each home), an
     intake spool receipt (`spool:<key>`), a GitHub issue or pull request (gh),
     or a line of a receipt file (the intake index, Kit's held-item record);
  5. files each ask or promise with no receipt through the intake door
     (`file_intake` in the gateway, project `fleet`, one fingerprint per item so
     a rerun never files twice) and writes the state file the fleet publisher
     lists on the fleet page as `unrouted` (FLEET_UNROUTED_STATE).

It never sends a chat message and never writes an agent's memory: a false
positive costs one line on the fleet page. A receipt it cannot check (gh or the
hub erroring) counts as a receipt and is reported in `errors`.

The state dir holds `unrouted.json` (schema lifekit-unrouted.v1: the last run's
time, day, model, the unrouted items of the last WINDOW_DAYS days and the run's
errors) and `filed.json` (fingerprint -> the door's receipt, so an item is
filed once).

Usage:
  unrouted.py [--day YYYY-MM-DD] [--state DIR] [--home DIR ...] [--model M]
              [--receipt-file FILE ...] [--chats FILE] [--no-file] [--now EPOCH]
  unrouted.py --check [--state DIR] [--hours H] [--now EPOCH]

--chats reads a JSONL file of chats.mjs lines instead of the gateway (the
replay test). --no-file lists and writes nothing to the door or the state dir
and prints the run's items as JSON (a dry run). --check is the dead-man: it
prints the last run's age and exits 1 when no run is recorded or the last one
is older than --hours (26).

Environment: UNROUTED_MODEL_CMD (argv that takes the prompt on stdin and prints
the model's answer; default `claude -p ...` with UNROUTED_CLAUDE, default
`claude`), UNROUTED_DOOR_CMD (argv that takes one file_intake item as JSON on
stdin and prints the door's receipt; default `docker exec` into the gateway),
UNROUTED_CHATS_CMD (argv that prints chats.mjs lines for SINCE_MS/UNTIL_MS in
its environment; default `docker exec` into the gateway), TASKS_AXI (for the
hub check, as intake_shape.py).

Exit: 0 done; 1 a part failed (the chats unreadable, a model call or a filing
failed: the state file still records what was found, with `errors`); 64 bad
usage.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent / "intake-shape"):
    if (p / "intake_shape.py").is_file():
        sys.path.insert(0, str(p))
        break
from intake_shape import Hub, one_line  # noqa: E402

SCHEMA = "lifekit-unrouted.v1"
DEFAULT_STATE = "/var/lib/lifekit-fleet/unrouted"
DEFAULT_MODEL = "haiku"
DEAD_HOURS = 26
WINDOW_DAYS = 7
MSG_CUT = 1500
CHUNK_CHARS = 60_000
QUOTE_CUT = 160
MODEL_TIMEOUT = 600
DOOR_TIMEOUT = 60
GATEWAY_LABELS = (
    "label=com.docker.compose.project=openclaw",
    "label=com.docker.compose.service=openclaw-gateway",
    "label=com.docker.compose.oneoff=False",
)
DOOR_JS = (
    'import { file } from "/home/node/.openclaw/extensions/intake-door/door.js";'
    'let s = ""; for await (const c of process.stdin) s += c;'
    'const got = await file(JSON.parse(s), "unrouted-check");'
    'process.stdout.write(JSON.stringify(got) + "\\n");'
)
# Not conversations with the owner: scheduled runs and the deploy smoke turn.
SKIP_SESSION_RE = re.compile(r":cron:|:explicit:deploy-smoke-")

NOTE_RE = re.compile(r"\b[0-9]{10}-[A-Za-z0-9]{6}\b")
SPOOL_RE = re.compile(r"\bspool:[0-9a-f]{16}\b")
HUB_RE = re.compile(r"\b(?:held|shape)-[a-z0-9][a-z0-9._-]*[a-z0-9]\b")
ISSUE_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/(?:issues|pull)/([0-9]+)"
)
ISSUE_REF_RE = re.compile(r"(?<![\w/])([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)\b")
SHORT_REF_RE = re.compile(r"(?<![\w/#])([A-Za-z][A-Za-z0-9._-]*)#([0-9]+)\b")

SYSTEM_PROMPT = """You audit one chat between the owner and an AI agent for work that could be lost.

Messages are numbered: [n] <time> <who>: <text>. <who> is `owner`, `agent` or `other`.

List every item of these two kinds:
- ask: the owner asks the agent (or the fleet: firstmate, devclaw, another agent) for work that outlives the turn - build, fix, change, file, research, remind, follow up, "do this", "every agent should ...". Leave out what the agent fully answered or did in its reply, chit-chat, and plain questions answered in place.
- promise: the agent commits to later work - "I will file ...", "saved for tomorrow", "added to tomorrow's task note", "goes out when you say go", "I'll look into it".

One item per piece of work: when an agent promise answers an owner ask, give one item of kind `promise` with `about` set to the ask's message number.

For each item give:
- kind: "ask" or "promise"
- msg: the number of the message where it was said
- about: for a promise, the number of the owner message it answers, else null
- quote: the shortest exact words from that message that say it (max 25 words)
- receipt: an id the agent showed for it as filed (an issue URL or repo#number, spool:<key>, held-<..>, shape-<..>, a task note id like 1700000000-AbCdEf), or null

Answer with one JSON object and nothing else: {"items": [...]}. No items: {"items": []}."""


def log(msg: str) -> None:
    print(f"unrouted: {msg}", file=sys.stderr)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def day_window(day: str) -> tuple[int, int]:
    start = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    since = int(start.timestamp())
    return since, since + 86400


# --- reading the chats ---------------------------------------------------------


def gateway_container() -> str:
    argv = ["docker", "ps", "-q", "--filter", "status=running"]
    for f in GATEWAY_LABELS:
        argv += ["--filter", f]
    found = subprocess.run(
        argv, capture_output=True, text=True, timeout=20, check=True
    ).stdout.split()
    if len(found) != 1:
        raise OSError(f"{len(found)} running openclaw-gateway containers, want one")
    return found[0]


def chats_from_gateway(since: int, until: int) -> list[dict[str, Any]]:
    env = {**os.environ, "SINCE_MS": str(since * 1000), "UNTIL_MS": str(until * 1000)}
    custom = shlex.split(os.environ.get("UNROUTED_CHATS_CMD") or "")
    if custom:
        argv, stdin = custom, None
    else:
        gw = gateway_container()
        argv = [
            "docker", "exec", "-i",
            "-e", f"SINCE_MS={since * 1000}", "-e", f"UNTIL_MS={until * 1000}",
            gw, "node", "--input-type=module", "-",
        ]  # fmt: skip
        stdin = (HERE / "chats.mjs").read_text(encoding="utf-8")
    res = subprocess.run(
        argv, input=stdin, env=env, capture_output=True, text=True, timeout=300
    )
    if res.returncode != 0:
        raise OSError(
            f"chat reader exit {res.returncode}: {one_line(res.stderr)[:200]}"
        )
    return [json.loads(line) for line in res.stdout.splitlines() if line.strip()]


def chats_from_file(path: Path, since: int, until: int) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        msg = json.loads(line)
        t = parse_ts(msg.get("ts"))
        if t is not None and since <= t < until:
            out.append(msg)
    return out


@dataclass
class Conversation:
    agent: str
    session_key: str
    channel: str
    messages: list[dict[str, Any]] = field(default_factory=list)

    def who(self, i: int) -> str:
        m = self.messages[i]
        if m.get("role") == "assistant":
            return "agent"
        return "owner" if m.get("owner") is True else "other"


def conversations(messages: list[dict[str, Any]]) -> list[Conversation]:
    """The day's conversations the owner took part in, one per agent session,
    messages in time order."""
    by_key: dict[tuple[str, str], Conversation] = {}
    for m in messages:
        agent = str(m.get("agent") or "")
        key = str(m.get("session_key") or "")
        if not agent or SKIP_SESSION_RE.search(key) or parse_ts(m.get("ts")) is None:
            continue
        conv = by_key.setdefault(
            (agent, key), Conversation(agent, key, str(m.get("channel") or ""))
        )
        conv.messages.append(m)
    out = []
    for conv in by_key.values():
        conv.messages.sort(key=lambda m: parse_ts(m["ts"]) or 0)
        if any(
            m.get("role") == "user" and m.get("owner") is True for m in conv.messages
        ):
            out.append(conv)
    return sorted(out, key=lambda c: (c.agent, c.session_key))


def render(conv: Conversation, lo: int, hi: int) -> str:
    rows = []
    for i in range(lo, hi):
        m = conv.messages[i]
        t = dt.datetime.fromtimestamp(parse_ts(m["ts"]) or 0, dt.timezone.utc)
        text = " ".join(str(m.get("text") or "").split())
        if len(text) > MSG_CUT:
            text = text[: MSG_CUT - 3] + "..."
        rows.append(f"[{i}] {t:%Y-%m-%d %H:%M:%S} {conv.who(i)}: {text}")
    return "\n".join(rows)


def chunks(conv: Conversation) -> list[tuple[int, int]]:
    """Consecutive message ranges of at most CHUNK_CHARS rendered characters."""
    out, lo, size = [], 0, 0
    for i, m in enumerate(conv.messages):
        n = min(len(str(m.get("text") or "")), MSG_CUT) + 40
        if size and size + n > CHUNK_CHARS:
            out.append((lo, i))
            lo, size = i, 0
        size += n
    if lo < len(conv.messages):
        out.append((lo, len(conv.messages)))
    return out


# --- the model -----------------------------------------------------------------


def model_argv(model: str) -> list[str]:
    custom = shlex.split(os.environ.get("UNROUTED_MODEL_CMD") or "")
    if custom:
        return custom
    return [
        os.environ.get("UNROUTED_CLAUDE") or "claude",
        "-p", "--model", model, "--output-format", "json",
        "--tools", "", "--strict-mcp-config", "--setting-sources", "",
        "--no-session-persistence", "--max-turns", "1",
        "--system-prompt", SYSTEM_PROMPT,
    ]  # fmt: skip


def ask_model(model: str, transcript: str) -> tuple[str, float | None]:
    """The model's answer text and its cost in USD when the CLI reports one."""
    with tempfile.TemporaryDirectory() as work:  # no project CLAUDE.md is read
        res = subprocess.run(
            model_argv(model),
            input=f"The chat:\n\n{transcript}\n",
            cwd=work,
            capture_output=True,
            text=True,
            timeout=MODEL_TIMEOUT,
        )
    if res.returncode != 0:
        raise OSError(f"model exit {res.returncode}: {one_line(res.stderr)[:200]}")
    out = res.stdout.strip()
    try:
        env = json.loads(out)
    except ValueError:
        return out, None
    if isinstance(env, dict) and env.get("type") == "result":
        if env.get("is_error"):
            raise OSError(f"model error: {one_line(str(env.get('result')))[:200]}")
        cost = env.get("total_cost_usd")
        return str(env.get("result") or ""), cost if isinstance(
            cost, (int, float)
        ) else None
    return out, None


def parse_items(answer: str, lo: int, hi: int) -> list[dict[str, Any]]:
    """The model's items, kept only when they point at a message of the chunk."""
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the model's answer")
    doc = json.loads(answer[start : end + 1])
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ValueError("the model's answer has no items list")
    out = []
    for it in items:
        if not isinstance(it, dict) or it.get("kind") not in ("ask", "promise"):
            continue
        msg, about = it.get("msg"), it.get("about")
        if not isinstance(msg, int) or isinstance(msg, bool) or not lo <= msg < hi:
            continue
        if (
            not isinstance(about, int)
            or isinstance(about, bool)
            or not lo <= about < hi
        ):
            about = None
        receipt = it.get("receipt")
        out.append(
            {
                "kind": it["kind"],
                "msg": msg,
                "about": about,
                "quote": one_line(str(it.get("quote") or "")),
                "receipt": one_line(receipt) if isinstance(receipt, str) else None,
            }
        )
    return out


# --- receipts ------------------------------------------------------------------


class Receipts:
    """Where a receipt id is looked up. `check(text)` answers for the first id
    found in the text: (verdict, id) with verdict `found`, `missing` or
    `unchecked` (the lookup failed; counted as a receipt), or (None, None)
    when the text names no receipt id at all."""

    def __init__(self, homes: list[str], receipt_files: list[str], errors: list[str]):
        self.homes = [h for h in homes if h]
        self.receipt_files = receipt_files
        self.errors = errors
        self.cache: dict[str, str] = {}

    def ids(self, text: str) -> list[str]:
        found: list[str] = []
        for m in ISSUE_URL_RE.finditer(text):
            found.append(f"{m.group(1)}#{m.group(2)}")
        for m in ISSUE_REF_RE.finditer(text):
            found.append(f"{m.group(1)}#{m.group(2)}")
        for rx in (SPOOL_RE, NOTE_RE, HUB_RE):
            found.extend(m.group(0) for m in rx.finditer(text))
        for m in SHORT_REF_RE.finditer(text):
            found.append(f"{m.group(1)}#{m.group(2)}")
        seen: set[str] = set()
        return [i for i in found if not (i in seen or seen.add(i))]

    def check(self, text: str | None) -> tuple[str | None, str | None]:
        best: tuple[str | None, str | None] = (None, None)
        for rid in self.ids(text or ""):
            verdict = self.cache.get(rid) or self.lookup(rid)
            self.cache[rid] = verdict
            if verdict in ("found", "unchecked"):
                return verdict, rid
            best = (verdict, rid)
        return best

    def in_files(self, rid: str) -> bool:
        for f in self.receipt_files:
            try:
                if rid in Path(f).read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                continue
        return False

    def lookup(self, rid: str) -> str:
        if self.in_files(rid):
            return "found"
        if rid.startswith("spool:"):
            return "found"  # the door's own receipt: the spool files it later
        if NOTE_RE.fullmatch(rid):
            for home in self.homes:
                inbox = Path(home) / "state" / "inbox"
                if (inbox / f"{rid}.note").exists() or (
                    inbox / "handled" / f"{rid}.note"
                ).exists():
                    return "found"
            return "missing"
        if HUB_RE.fullmatch(rid):
            failed = False
            for home in self.homes:
                try:
                    if Hub(home).state(rid) != "absent":
                        return "found"
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    failed = True
                    self.errors.append(f"hub check of {rid} failed: {exc}"[:200])
            return "unchecked" if failed else "missing"
        repo, _, num = rid.rpartition("#")
        if "/" not in repo:
            return "unchecked"  # repo#N with no owner: trusted as shown
        try:
            res = subprocess.run(
                ["gh", "api", f"repos/{repo}/issues/{num}", "--jq", ".number"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.errors.append(f"gh check of {rid} failed: {exc.__class__.__name__}")
            return "unchecked"
        if res.returncode == 0:
            return "found"
        if "Not Found" in res.stderr or "404" in res.stderr:
            return "missing"
        self.errors.append(f"gh check of {rid} failed: {one_line(res.stderr)[:120]}")
        return "unchecked"


def turn_replies(conv: Conversation, anchor: int) -> str:
    """The agent replies of the turn an item was said in: from the item's
    message to the next owner message after it."""
    out = []
    for i in range(anchor, len(conv.messages)):
        if i > anchor and conv.who(i) == "owner":
            break
        if conv.who(i) == "agent":
            out.append(str(conv.messages[i].get("text") or ""))
    return "\n".join(out)


# --- one run -------------------------------------------------------------------


def fingerprint(agent: str, day: str, quote: str) -> str:
    h = hashlib.sha256(f"{agent}|{day}|{quote.lower()}".encode()).hexdigest()[:10]
    return f"unrouted-{agent}-{day}-{h}"


def extract(
    conv: Conversation, model: str, errors: list[str], costs: list[float]
) -> list[dict[str, Any]]:
    items: dict[int, dict[str, Any]] = {}
    for lo, hi in chunks(conv):
        try:
            answer, cost = ask_model(model, render(conv, lo, hi))
            got = parse_items(answer, lo, hi)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"{conv.agent}: extraction failed ({exc})"[:240])
            continue
        if cost is not None:
            costs.append(cost)
        for it in got:
            # One item per owner ask: a promise about an ask replaces the ask.
            anchor = it["about"] if it["about"] is not None else it["msg"]
            if anchor not in items or it["kind"] == "promise":
                items[anchor] = it
    return [items[k] for k in sorted(items)]


def assess(
    conv: Conversation, items: list[dict[str, Any]], receipts: Receipts, day: str
) -> list[dict[str, Any]]:
    """Each item with its receipt verdict, its time and its fingerprint."""
    out = []
    for it in items:
        anchor = it["about"] if it["about"] is not None else it["msg"]
        verdict, rid = receipts.check(it["receipt"])
        if verdict not in ("found", "unchecked"):
            v2, r2 = receipts.check(turn_replies(conv, min(anchor, it["msg"])))
            if v2 in ("found", "unchecked") or verdict is None:
                verdict, rid = v2, r2
        quote = it["quote"] or one_line(str(conv.messages[it["msg"]].get("text") or ""))
        if len(quote) > QUOTE_CUT:
            quote = quote[: QUOTE_CUT - 3] + "..."
        out.append(
            {
                "agent": conv.agent,
                "channel": conv.channel,
                "day": day,
                "at": conv.messages[it["msg"]]["ts"],
                "ask_at": conv.messages[anchor]["ts"],
                "kind": it["kind"],
                "quote": quote,
                "receipt": rid if verdict in ("found", "unchecked") else None,
                "receipt_cited": rid if verdict == "missing" else None,
                "routed": verdict in ("found", "unchecked"),
                "fingerprint": fingerprint(conv.agent, day, quote),
            }
        )
    return out


def door_argv() -> tuple[list[str], bool]:
    custom = shlex.split(os.environ.get("UNROUTED_DOOR_CMD") or "")
    if custom:
        return custom, False
    gw = gateway_container()
    return [
        "docker",
        "exec",
        "-i",
        gw,
        "node",
        "--input-type=module",
        "-e",
        DOOR_JS,
    ], True


def door_item(item: dict[str, Any]) -> dict[str, Any]:
    """The file_intake arguments. The thread names the agent, channel and day,
    never the session key, which carries the owner's chat id."""
    what = "ask" if item["kind"] == "ask" else "promise"
    return {
        "project": "fleet",
        "title": f"Unrouted {what} ({item['agent']}, {item['day']}): {item['quote']}"[
            :120
        ],
        "body": "\n".join(
            [
                f"The daily unrouted check found an owner {what} with no receipt.",
                "",
                f"- agent: {item['agent']}",
                f"- said: {item['at']}",
                f"- quote: {item['quote']}",
                *(
                    [f"- cited receipt not found: {item['receipt_cited']}"]
                    if item.get("receipt_cited")
                    else []
                ),
            ]
        ),
        "fingerprint": item["fingerprint"],
        "envelope": {
            "origin": f"unrouted-check/{item['agent']}",
            "owner": "firstmate",
            "class": "unclear",
            "acceptance": "The ask or promise is done, or routed with a receipt the owner was shown",
            "thread": f"{item['agent']}/{item['channel'] or 'chat'}/{item['day']}",
        },
    }


def file_unrouted(
    items: list[dict[str, Any]], filed: dict[str, Any], errors: list[str], now: int
) -> None:
    argv: list[str] | None = None
    for item in items:
        if item["routed"]:
            continue
        done = filed.get(item["fingerprint"])
        if isinstance(done, dict) and done.get("receipt"):
            item["filed"] = done["receipt"]
            continue
        try:
            if argv is None:
                argv, _ = door_argv()
            res = subprocess.run(
                argv,
                input=json.dumps(door_item(item)),
                capture_output=True,
                text=True,
                timeout=DOOR_TIMEOUT,
            )
            got = (
                json.loads(res.stdout.strip().splitlines()[-1])
                if res.stdout.strip()
                else {}
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(
                f"filing {item['fingerprint']} failed ({exc.__class__.__name__})"
            )
            continue
        rid = got.get("id") if isinstance(got, dict) else None
        if res.returncode != 0 or not rid:
            why = (got.get("reason") if isinstance(got, dict) else None) or res.stderr
            errors.append(
                f"filing {item['fingerprint']} failed: {one_line(str(why))[:160]}"
            )
            continue
        item["filed"] = rid
        filed[item["fingerprint"]] = {
            "receipt": rid,
            "url": got.get("url") or "",
            "at": now,
        }


def run(args: argparse.Namespace) -> int:
    now = int(args.now or time.time())
    day = args.day or dt.datetime.fromtimestamp(now - 86400, dt.timezone.utc).strftime(
        "%Y-%m-%d"
    )
    since, until = day_window(day)
    errors: list[str] = []
    state = Path(args.state)
    try:
        if args.chats:
            messages = chats_from_file(Path(args.chats), since, until)
        else:
            messages = chats_from_gateway(since, until)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log(f"chats unreadable: {exc}")
        return 1  # nothing recorded: the dead-man shows the missed run
    convs = conversations(messages)
    receipts = Receipts(args.home, args.receipt_file, errors)
    costs: list[float] = []
    found: list[dict[str, Any]] = []
    for conv in convs:
        found.extend(
            assess(conv, extract(conv, args.model, errors, costs), receipts, day)
        )
    unrouted = [i for i in found if not i["routed"]]
    if args.no_file:
        print(
            json.dumps(
                {
                    "day": day,
                    "model": args.model,
                    "conversations": len(convs),
                    "cost_usd": round(sum(costs), 4),
                    "items": found,
                    "errors": errors,
                },
                indent=1,
            )
        )
        return 1 if errors else 0
    filed = read_json(state / "filed.json", {})
    file_unrouted(unrouted, filed, errors, now)
    write_json(state / "filed.json", filed)
    prev = read_json(state / "unrouted.json", {})
    cutoff = (
        dt.date.fromisoformat(day) - dt.timedelta(days=WINDOW_DAYS - 1)
    ).isoformat()
    kept = [
        i
        for i in (prev.get("items") or [])
        if isinstance(i, dict) and cutoff <= str(i.get("day")) and i.get("day") != day
    ]
    doc = {
        "schema": SCHEMA,
        "ran_at": now,
        "day": day,
        "model": args.model,
        "conversations": len(convs),
        "asks_and_promises": len(found),
        "routed": len(found) - len(unrouted),
        "items": sorted(
            kept + [{k: v for k, v in i.items() if k != "routed"} for i in unrouted],
            key=lambda i: (str(i.get("day")), str(i.get("at"))),
        ),
        "errors": errors,
    }
    write_json(state / "unrouted.json", doc)
    print(
        f"unrouted: {day}: {len(convs)} conversation(s), {len(found)} ask(s)/promise(s),"
        f" {len(unrouted)} unrouted, {len(errors)} error(s)"
    )
    for e in errors:
        log(e)
    return 1 if errors else 0


def check(args: argparse.Namespace) -> int:
    now = int(args.now or time.time())
    try:
        doc = read_json(Path(args.state) / "unrouted.json", None)
    except (OSError, ValueError):
        doc = None
    ran = doc.get("ran_at") if isinstance(doc, dict) else None
    if not isinstance(ran, int) or isinstance(ran, bool):
        print(json.dumps({"ran_at": None, "stale": True, "reason": "no run recorded"}))
        return 1
    age = (now - ran) / 3600
    stale = age > args.hours
    print(
        json.dumps(
            {
                "ran_at": ran,
                "day": doc.get("day"),
                "age_hours": round(age, 1),
                "stale": stale,
                "unrouted": len(doc.get("items") or []),
                "errors": len(doc.get("errors") or []),
            }
        )
    )
    return 1 if stale else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--check", action="store_true", help="dead-man: exit 1 when stale")
    ap.add_argument("--hours", type=float, default=DEAD_HOURS)
    ap.add_argument("--day", help="UTC day YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--home", action="append", default=[], help="a firstmate home")
    ap.add_argument("--receipt-file", action="append", default=[])
    ap.add_argument(
        "--model", default=os.environ.get("UNROUTED_MODEL") or DEFAULT_MODEL
    )
    ap.add_argument("--chats", help="read chats.mjs JSONL from this file")
    ap.add_argument(
        "--no-file", action="store_true", help="dry run: print, file nothing"
    )
    ap.add_argument("--now", type=int)
    args = ap.parse_args(argv)
    if args.day:
        try:
            dt.date.fromisoformat(args.day)
        except ValueError:
            log("--day must be YYYY-MM-DD")
            return 64
    return check(args) if args.check else run(args)


if __name__ == "__main__":
    sys.exit(main())
