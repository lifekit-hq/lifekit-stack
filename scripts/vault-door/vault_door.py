#!/usr/bin/env python3
"""vault-door - ChatGPT's door to the fleet through the vault: the bridge.

ChatGPT cannot talk to firstmate or OpenClaw, but it already writes the vault
through the outbox gate (the vault's adapters.md). One job makes that enough,
with no MCP door, no new credential and no new service:

  bridge    every few minutes, turn each new ChatGPT note in the vault's
            inbox.md into exactly one captain-inbox note for the main
            firstmate home, through that home's own `fm-inbox.sh note`, then
            mark the vault note as forwarded.

Hard limits, each pinned by scripts/tests/test_vault_door.py:

- The only input is the vault. The only writes are the forwarded markers in
  inbox.md and notes through `fm-inbox.sh note`.
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
- The job skips while the vault's weekly model pass holds `.model-pass`, the
  same rule memory-sync.sh follows, so a half-done edit is never committed.

Environment:
  VAULT_DOOR_VAULT      vault checkout (default /srv/memory)
  VAULT_DOOR_FM_HOME    the main firstmate home that receives notes (absolute, required)
  VAULT_DOOR_NOW        pin the clock (epoch seconds), for tests

Exit: 0 done (or skipped for the model pass); 1 a note failed (the unit shows
red; the note is retried next run); 78 not configured.
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

MODEL_LOCK = ".model-pass"
MODEL_LOCK_SECONDS = 60 * 60
INBOX_FILE = "inbox.md"
BRIDGE_PER_RUN = 5
NOTE_MAX_BYTES = 16 * 1024
TIMEOUT = 60

ITEM_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) [-\u2013\u2014] (.*)$")
CHATGPT_RE = re.compile(r"^chatgpt:\s*(.*)$", re.IGNORECASE)
DONE_RE = re.compile(r"\s\[(?:forwarded: [A-Za-z0-9._-]+|not forwarded: [^\]]*)\]\s*$")


# ---------------------------------------------------------------- shared


def now_epoch() -> int:
    return int(
        os.environ.get("VAULT_DOOR_NOW") or dt.datetime.now(dt.timezone.utc).timestamp()
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
    if len(argv) != 2 or argv[1] != "bridge":
        print("usage: vault_door.py bridge", file=sys.stderr)
        return 64
    return bridge()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
