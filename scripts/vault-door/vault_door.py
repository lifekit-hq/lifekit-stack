#!/usr/bin/env python3
"""vault-door - the two halves of ChatGPT's door to the fleet, both through the vault.

ChatGPT cannot talk to firstmate or OpenClaw, but it already reads the vault and
writes it through the outbox gate (the vault's adapters.md). Two jobs make that
enough, with no MCP door, no new credential and no new service:

  snapshot  once a day, overwrite one vault page (system/fleet-snapshot.md) with
            a picture of the fleet: queued work, open captain calls, what
            landed in the last 7 days, a few numbers. ChatGPT reads it to
            brainstorm in context.
  bridge    every few minutes, turn each new ChatGPT note in the vault's
            inbox.md into exactly one captain-inbox note for the main
            firstmate home, through that home's own `fm-inbox.sh note`, then
            mark the vault note as forwarded.

Hard limits, each pinned by scripts/tests/test_vault_door.py:

- Inputs are read-only: the fleet publisher's merged summary and daily metrics
  (files), `gh search prs`, and the vault. The only writes are the snapshot page,
  the forwarded markers in inbox.md, and notes through `fm-inbox.sh note`.
- A ChatGPT note is a line `- YYYY-MM-DD - chatgpt: <text>` (indented lines
  continue it) in inbox.md. Any other inbox line is not ChatGPT's and is never
  forwarded. The note reaches the captain inbox as `proposal: <text>`, the
  held-for-review kind, never as a task, and always with its own request id
  `gpt-<sha256 of the text>`: the same text is one note however often it is
  written or this job runs, because fm-inbox replays a request id.
- A forwarded note gets ` [forwarded: <note id>]` appended to its first line (a
  note that cannot be forwarded gets ` [not forwarded: <reason>]`); both end
  its processing. Marking is the last step, so a crash in between replays the
  same request id on the next run instead of making a second note.
- Both jobs skip while the vault's weekly model pass holds `.model-pass`, the
  same rule memory-sync.sh follows, so a half-done edit is never committed.
- Nothing credential-shaped and no host path is written to the snapshot page.

Environment (every one has a default except VAULT_DOOR_FM_HOME for the bridge):
  VAULT_DOOR_VAULT      vault checkout (default /srv/memory)
  VAULT_DOOR_FM_HOME    the main firstmate home that receives notes (absolute)
  VAULT_DOOR_SUMMARY    fleet publisher's merged summary
                        (default /var/lib/lifekit-fleet/home-summary.json)
  VAULT_DOOR_DAILY      fleet publisher's daily metrics
                        (default /var/lib/lifekit-fleet/metrics/daily.jsonl)
  VAULT_DOOR_GH         gh binary (default gh)
  VAULT_DOOR_OWNERS     GitHub owners whose merged PRs count (default "lifekit-hq dsdevq")
  VAULT_DOOR_PAGE       snapshot page, relative to the vault (default system/fleet-snapshot.md)
  VAULT_DOOR_NOW        pin the clock (epoch seconds), for tests

Exit: 0 done (or skipped for the model pass); 1 a source or a note failed (the
unit shows red; the page is left as it was, a note is retried next run);
78 not configured.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PAGE_NAME = "fleet-snapshot"
WINDOW_DAYS = 7
STALE_SECONDS = 15 * 60
MODEL_LOCK = ".model-pass"
MODEL_LOCK_SECONDS = 60 * 60
INBOX_FILE = "inbox.md"
BRIDGE_PER_RUN = 5
NOTE_MAX_BYTES = 16 * 1024
LINE_MAX = 400
QUEUE_GROUP_MAX = 30
QUEUE_GROUPS_MAX = 10
PAGE_LINE_BUDGET = 280
PAGE_BYTE_BUDGET = 22 * 1024
PR_MAX = 60
GH_LIMIT = 100
TIMEOUT = 60
DAILY_SCHEMA = "fleet-metrics-daily.v1"

# The fleet publisher's credential patterns (scripts/fleet-publisher/): a line
# that matches one is withheld, not trimmed.
CREDENTIAL_RE = re.compile(
    r"sk-ant-|gh[pousr]_[A-Za-z0-9]{20}|github_pat_|xox[abprs]-|tskey-|AKIA[0-9A-Z]{16}"
    r"|BEGIN [A-Z ]*PRIVATE KEY|bot[0-9]{6,}:[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.eyJ"
)
HOST_PATH_RE = re.compile(
    r"(?<![\w:/.-])/(?:home|srv|var|etc|root|tmp|usr|opt)/[^\s,;)\]\"'`]+"
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

ITEM_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) [-\u2013\u2014] (.*)$")
CHATGPT_RE = re.compile(r"^chatgpt:\s*(.*)$", re.IGNORECASE)
DONE_RE = re.compile(r"\s\[(?:forwarded: [A-Za-z0-9._-]+|not forwarded: [^\]]*)\]\s*$")


# ---------------------------------------------------------------- shared


def now_epoch() -> int:
    return int(
        os.environ.get("VAULT_DOOR_NOW") or dt.datetime.now(dt.timezone.utc).timestamp()
    )


def iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def log(message: str) -> None:
    print(f"vault-door: {message}", file=sys.stderr)


def vault_dir() -> Path:
    return Path(os.environ.get("VAULT_DOOR_VAULT", "/srv/memory"))


def model_pass_running(vault: Path, now: int) -> bool:
    try:
        return now - (vault / MODEL_LOCK).stat().st_mtime < MODEL_LOCK_SECONDS
    except OSError:
        return False


def write_in_place(path: Path, text: str) -> bool:
    """Write text over path, keeping its inode, owner and ACL; False when unchanged.

    A rename would leave the vault's other account (the sync timer runs as
    another user) with a file it cannot rewrite.
    """
    data = text.encode("utf-8")
    try:
        if path.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o664)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return True


def clean(text: Any, limit: int = LINE_MAX) -> str:
    """One printable line: no control characters, host paths or runs of space."""
    text = HOST_PATH_RE.sub("<path>", CONTROL_RE.sub(" ", str(text)))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# Regexes of the vault's bin/subjects.txt that another page owns: the vault lint
# refuses a page that restates such a subject, and queue titles are free text, so
# a matching line is withheld here instead of leaving the page red in the sync.
SUBJECTS: list[re.Pattern[str]] = []


def load_subjects(vault: Path, page: str) -> None:
    SUBJECTS.clear()
    try:
        lines = (
            (vault / "bin" / "subjects.txt").read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        return
    for line in lines:
        fields = line.split("\t")
        if (
            line.startswith("#")
            or len(fields) < 3
            or not fields[2]
            or fields[2] == page
        ):
            continue
        try:
            SUBJECTS.append(re.compile(fields[1], re.IGNORECASE))
        except re.error:
            continue


def safe_line(line: str, ident: str = "") -> str:
    if CREDENTIAL_RE.search(line):
        return "- [line withheld: it looked like a credential]"
    if any(rx.search(line.replace("`", "")) for rx in SUBJECTS):
        who = f"`{ident}`: " if ident else ""
        return (
            f"- {who}[text withheld: it restates a subject the vault keeps on one page]"
        )
    return line


# ---------------------------------------------------------------- snapshot


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def daily_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(row, dict)
            and row.get("schema") == DAILY_SCHEMA
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("date", "")))
        ):
            rows.append(row)
    return rows


def number(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    return (
        value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    )


def merged_prs(now: int) -> tuple[list[dict[str, Any]] | None, str]:
    """Merged PRs of the last WINDOW_DAYS, newest first; (None, reason) when gh fails."""
    gh = os.environ.get("VAULT_DOOR_GH", "gh")
    owners = os.environ.get("VAULT_DOOR_OWNERS", "lifekit-hq dsdevq").split()
    cutoff = now - WINDOW_DAYS * 86400
    since = dt.datetime.fromtimestamp(cutoff, dt.timezone.utc).strftime("%Y-%m-%d")
    command = [gh, "search", "prs", "--merged", "--merged-at", f">={since}"]
    for owner in owners:
        command += ["--owner", owner]
    command += [
        "--json",
        "repository,number,title,closedAt,url",
        "--limit",
        str(GH_LIMIT),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, clean(type(error).__name__)
    if result.returncode != 0:
        first = (result.stderr.strip().splitlines() or ["no output"])[0]
        return None, clean(first, 120)
    try:
        found = json.loads(result.stdout)
    except ValueError:
        return None, "gh printed no JSON"
    prs = []
    for item in found if isinstance(found, list) else []:
        try:
            closed = dt.datetime.strptime(
                item["closedAt"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=dt.timezone.utc)
            repo = item["repository"]["nameWithOwner"]
            prs.append(
                {
                    "at": closed.timestamp(),
                    "repo": repo,
                    "number": int(item["number"]),
                    "title": item["title"],
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    prs = [pr for pr in prs if pr["at"] >= cutoff]
    prs.sort(key=lambda pr: pr["at"], reverse=True)
    return prs, ""


def cap(lines: list[str], limit: int) -> list[str]:
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"- … and {len(lines) - limit} more"]


def render_numbers(
    summary: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    prs: list[dict[str, Any]] | None,
    now: int,
) -> list[str]:
    out = ["## Numbers", ""]
    if summary is not None:
        queued = [q for q in summary.get("queued") or [] if isinstance(q, dict)]
        held = [q for q in queued if q.get("hold_kind") or q.get("hold_bucket")]
        counts = summary.get("counts") or {}
        out.append(
            f"- Fleet now: {len(summary.get('active_children') or [])} running workers, "
            f"{len(summary.get('decisions_open') or [])} open captain calls, "
            f"{len(queued)} queued ({len(queued) - len(held)} ready, {len(held)} held)."
        )
        if isinstance(counts.get("endpoints"), int):
            out.append(f"- Endpoints the fleet tracks: {counts['endpoints']}.")
    else:
        out.append(
            "- Fleet counts are unavailable: the published fleet summary could not be read."
        )
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    first = today - dt.timedelta(days=WINDOW_DAYS)
    last = today - dt.timedelta(days=1)
    week = [r for r in rows if first.isoformat() <= str(r["date"]) <= last.isoformat()]
    if week:
        runs = sum(number(r, "runs") for r in week)
        passed = sum(number(r, "first_pass_runs") for r in week)
        asks = sum(
            number(r.get("asks_new") or {}, key)
            for r in week
            for key in ("t0", "t1", "t2", "untiered")
        )
        tokens = sum(number(r, "weighted_tokens") for r in week)
        allowance = [
            r["allowance_week_pct"]
            for r in week
            if isinstance(r.get("allowance_week_pct"), (int, float))
        ]
        out.append(
            f"- Days {first.isoformat()} to {last.isoformat()} (UTC, fleet metrics): "
            f"{int(runs)} task runs, {int(passed)} passed first time, {int(sum(number(r, 'fix_rounds') for r in week))} fix rounds, "
            f"{int(sum(number(r, 'merged_prs') for r in week))} merged PRs, {int(asks)} new asks to the captain, "
            f"peak {int(max(number(r, 'peak_workers') for r in week))} workers at once, "
            f"{tokens / 1e6:.1f}M weighted tokens."
        )
        if allowance:
            out.append(
                f"- Weekly allowance used at the last daily reading: {allowance[-1]:g}%."
            )
    else:
        out.append("- No daily fleet metrics for the last 7 days.")
    if prs is not None:
        out.append(
            f"- Merged PRs found by gh in the last 7 days: {len(prs)}{'+' if len(prs) >= GH_LIMIT else ''}."
        )
    return out


def render_calls(summary: dict[str, Any], limit: int = QUEUE_GROUP_MAX) -> list[str]:
    calls = [d for d in summary.get("decisions_open") or [] if isinstance(d, dict)]
    out = [f"## Open captain calls ({len(calls)})", ""]
    if not calls:
        return out + ["None."]
    lines = []
    for call in calls:
        bits = [str(call.get("verb") or "call")]
        if call.get("hold_until"):
            bits.append(f"until {call['hold_until']}")
        if isinstance(call.get("hold_age_days"), int):
            bits.append(f"{call['hold_age_days']} days old")
        text = f"- `{clean(call.get('id'), 80)}` ({', '.join(bits)}): {clean(call.get('summary') or '', 200)}"
        if call.get("reason"):
            text += f" - {clean(call['reason'], 240)}"
        lines.append(safe_line(clean(text, LINE_MAX + 100), clean(call.get("id"), 80)))
    return out + cap(lines, limit)


def render_queue(summary: dict[str, Any], limit: int = QUEUE_GROUP_MAX) -> list[str]:
    queued = [q for q in summary.get("queued") or [] if isinstance(q, dict)]
    out = [f"## Queued work ({len(queued)})", ""]
    groups: dict[str, list[str]] = {}
    for item in queued:
        kind = item.get("hold_kind") or item.get("hold_bucket")
        label = "Ready (no hold)" if not kind else f"Held: {clean(kind, 40)}"
        text = f"- `{clean(item.get('id'), 80)}`: {clean(item.get('title') or '', 200)}"
        meta = [str(item[k]) for k in ("kind", "repo") if item.get(k)]
        if item.get("hold_until"):
            meta.append(f"until {item['hold_until']}")
        if meta:
            text += f" ({clean(', '.join(meta), 120)})"
        groups.setdefault(label, []).append(safe_line(text, clean(item.get("id"), 80)))
    if not groups:
        return out + ["Nothing queued."]
    labels = sorted(groups, key=lambda name: (name != "Ready (no hold)", name))
    for label in labels[:QUEUE_GROUPS_MAX]:
        out += (
            [f"### {label} ({len(groups[label])})", ""]
            + cap(groups[label], limit)
            + [""]
        )
    rest = labels[QUEUE_GROUPS_MAX:]
    if rest:
        out += [
            f"- … and {len(rest)} more groups ({sum(len(groups[r]) for r in rest)} items)",
            "",
        ]
    return out[:-1]


def render_landed(
    summary: dict[str, Any] | None,
    prs: list[dict[str, Any]] | None,
    reason: str,
    now: int,
    limit: int = QUEUE_GROUP_MAX,
) -> list[str]:
    out = [f"## Landed in the last {WINDOW_DAYS} days", "", "### Merged PRs", ""]
    if prs is None:
        out.append(f"Unavailable this run: gh failed ({reason}).")
    elif not prs:
        out.append("None.")
    else:
        lines = [
            safe_line(
                f"- {iso(int(pr['at']))[:10]} {pr['repo']}#{pr['number']} {clean(pr['title'], 160)}"
            )
            for pr in prs
        ]
        out += cap(lines, min(PR_MAX, limit))
    out += ["", "### Finished tasks", ""]
    cutoff = (
        (
            dt.datetime.fromtimestamp(now, dt.timezone.utc)
            - dt.timedelta(days=WINDOW_DAYS)
        )
        .date()
        .isoformat()
    )
    done = []
    for item in (summary or {}).get("landed") or []:
        completion = item.get("completion") if isinstance(item, dict) else None
        when = str((completion or {}).get("date") or "")
        if when >= cutoff:
            done.append(
                safe_line(
                    f"- {when} `{clean(item.get('id'), 80)}`: {clean(item.get('title') or '', 200)}",
                    clean(item.get("id"), 80),
                )
            )
    return out + (cap(sorted(done, reverse=True), limit) if done else ["None."])


def render_snapshot(
    summary: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    prs: list[dict[str, Any]] | None,
    gh_reason: str,
    now: int,
) -> str:
    today = iso(now)[:10]
    published = (summary or {}).get("published_epoch")
    if not isinstance(published, int):
        source = "the fleet summary was unavailable"
    elif now - published > STALE_SECONDS:
        source = f"the fleet summary published {iso(published)}, which is stale"
    else:
        source = f"the fleet summary published {iso(published)}"
    head = [
        "---",
        f"name: {PAGE_NAME}",
        "summary: Generated daily by the VPS fleet-snapshot timer (lifekit-stack scripts/vault-door) and overwritten each run. "
        "Queued work, open captain calls, PRs merged in the last 7 days and a few fleet numbers, so ChatGPT can brainstorm in context. "
        "A picture, not a record: verify anything live before acting on it.",
        f"updatedAt: {today}",
        "status: active",
        "---",
        "",
        "# Fleet snapshot",
        "",
        f"Generated {iso(now)} from {source} and `gh`. Overwritten every run; never edit it by hand.",
        "",
    ]
    tail = [
        "## How to send a proposal",
        "",
        "Add one line to `inbox.md` on your outbox branch (the gate merges it): "
        "`- YYYY-MM-DD - chatgpt: <the proposal, one or a few sentences>`, with indented lines to continue it. "
        "The vault bridge turns each such line into one held `proposal:` note in the captain inbox, "
        "to be reviewed together on the Monday page, and marks the line forwarded. "
        "Lines without `chatgpt:` are not forwarded.",
    ]
    numbers = render_numbers(summary, rows, prs, now) + [""]
    text = ""
    for limit in range(QUEUE_GROUP_MAX, -1, -1):
        body = list(numbers)
        if summary is not None:
            body += render_calls(summary, limit) + [""]
            body += render_queue(summary, limit) + [""]
        body += render_landed(summary, prs, gh_reason, now, limit) + [""]
        text = "\n".join(head + body + tail) + "\n"
        if (
            len(text.splitlines()) <= PAGE_LINE_BUDGET
            and len(text.encode()) <= PAGE_BYTE_BUDGET
        ):
            break
    return text


def snapshot() -> int:
    vault = vault_dir()
    now = now_epoch()
    if model_pass_running(vault, now):
        log("model pass running, snapshot skipped")
        return 0
    summary = load_json(
        Path(
            os.environ.get(
                "VAULT_DOOR_SUMMARY", "/var/lib/lifekit-fleet/home-summary.json"
            )
        )
    )
    if not isinstance(summary, dict):
        log("the fleet summary is missing or unreadable; page left as it was")
        return 1
    rows = daily_rows(
        Path(
            os.environ.get(
                "VAULT_DOOR_DAILY", "/var/lib/lifekit-fleet/metrics/daily.jsonl"
            )
        )
    )
    prs, reason = merged_prs(now)
    if prs is None:
        log(f"gh failed ({reason}); the page says so")
    rel = os.environ.get("VAULT_DOOR_PAGE", "system/fleet-snapshot.md")
    page = vault / rel
    load_subjects(vault, rel)
    changed = write_in_place(page, render_snapshot(summary, rows, prs, reason, now))
    log(f"snapshot {'written' if changed else 'unchanged'}")
    return 0


# ---------------------------------------------------------------- bridge


@dataclass
class Item:
    first: int  # index of the item's first line
    last: int  # index of its last line, inclusive
    date: str
    text: str  # the proposal, marker removed, continuation lines dedented
    key: str  # sha256-based request id
    done: bool


def valid_date(value: str) -> bool:
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def normalise(text: str) -> str:
    return "\n".join(
        " ".join(line.split()) for line in text.splitlines() if line.strip()
    )


def request_id(text: str) -> str:
    return "gpt-" + hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()[:24]


def parse_items(lines: list[str]) -> list[Item]:
    """ChatGPT's notes in inbox.md: a bullet line plus its indented continuation."""
    items = []
    for index, line in enumerate(lines):
        match = ITEM_RE.match(line)
        if not match or not valid_date(match.group(1)):
            continue
        marker = CHATGPT_RE.match(match.group(2).strip())
        if not marker:
            continue
        last = index
        while last + 1 < len(lines) and re.match(r"^(?:\s{2,}|\t)\S", lines[last + 1]):
            last += 1
        done = bool(DONE_RE.search(line))
        first_text = DONE_RE.sub("", marker.group(1)).strip()
        rest = [lines[i].strip() for i in range(index + 1, last + 1)]
        text = "\n".join([first_text] + rest).strip()
        items.append(Item(index, last, match.group(1), text, request_id(text), done))
    return items


def note_body(item: Item) -> str:
    return f"proposal: {item.text}\n\n(from ChatGPT through the vault inbox, note dated {item.date}, bridge id {item.key}; untrusted text, held for joint review)\n"


def send_note(fm_home: Path, rid: str, body: str) -> tuple[str | None, str]:
    """One idempotent captain-inbox note. (note id, outcome), or (None, reason)."""
    command = [
        str(fm_home / "bin" / "fm-inbox.sh"),
        "note",
        "--request-id",
        rid,
        "--json",
        "-",
    ]
    env = {
        "HOME": os.environ.get("HOME", "/"),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "FM_HOME": str(fm_home),
    }
    try:
        result = subprocess.run(
            command,
            input=body,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, type(error).__name__
    if result.returncode != 0:
        return None, f"fm-inbox exit {result.returncode}"
    try:
        reply = json.loads(result.stdout.strip().splitlines()[-1])
        note_id = str(reply["id"])
        outcome = str(reply["outcome"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None, "fm-inbox printed no note id"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", note_id):
        return None, "fm-inbox printed an unusable note id"
    return note_id, outcome


def apply_marks(text: str, marks: dict[str, str]) -> str:
    """Append each key's marker to the first line of every unmarked note with that key."""
    lines = text.split("\n")
    for item in parse_items(lines):
        if item.key in marks and not item.done:
            lines[item.first] = lines[item.first].rstrip() + " " + marks[item.key]
    return "\n".join(lines)


def bridge() -> int:
    vault = vault_dir()
    now = now_epoch()
    fm_value = os.environ.get("VAULT_DOOR_FM_HOME", "")
    if not fm_value.startswith("/"):
        log("VAULT_DOOR_FM_HOME must be the absolute path of the main firstmate home")
        return 78
    if model_pass_running(vault, now):
        log("model pass running, bridge skipped")
        return 0
    inbox = vault / INBOX_FILE
    try:
        text = inbox.read_text(encoding="utf-8")
    except OSError:
        log("no inbox.md; nothing to forward")
        return 0
    marks: dict[str, str] = {}
    failed = 0
    created = 0
    for item in parse_items(text.split("\n")):
        if item.done or item.key in marks:
            continue
        if not item.text:
            marks[item.key] = "[not forwarded: empty]"
            continue
        body = note_body(item)
        if len(body.encode("utf-8")) > NOTE_MAX_BYTES:
            marks[item.key] = f"[not forwarded: over {NOTE_MAX_BYTES // 1024} KiB]"
            continue
        if created >= BRIDGE_PER_RUN:
            log(
                f"per-run cap of {BRIDGE_PER_RUN} reached; the rest wait for the next run"
            )
            break
        note_id, outcome = send_note(Path(fm_value), item.key, body)
        if note_id is None:
            log(f"{item.key} not sent ({outcome}); retried next run")
            failed += 1
            continue
        created += outcome == "created"
        marks[item.key] = f"[forwarded: {note_id}]"
        log(f"{item.key} {outcome} as {note_id}")
    if marks:
        # Re-read just before writing: marks go on whatever the file is now.
        fresh = inbox.read_text(encoding="utf-8")
        write_in_place(inbox, apply_marks(fresh, marks))
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    modes = {"snapshot": snapshot, "bridge": bridge}
    if len(argv) != 2 or argv[1] not in modes:
        print("usage: vault_door.py snapshot|bridge", file=sys.stderr)
        return 64
    return modes[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
