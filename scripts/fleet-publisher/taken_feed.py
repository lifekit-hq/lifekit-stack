#!/usr/bin/env python3
"""taken-feed - the "Taken for you" feed, for fleet-publisher.sh.

Prints one fleet-taken-feed.v1 JSON object to stdout: one short line per action
the fleet took under a standing ruling instead of asking, newest first, at most
200 (contract: README.md "Taken for you feed"). Two host logs are read, read-only,
from the fleet-policy state directory:

  p16.jsonl         the permission engine's audit log (fields ts, actor, home,
                    class, cell, ruling, subject, outcome); subjects are already
                    redacted by the writer
  self-calls.jsonl  the calls the fleet answered itself (fields ts, id, home,
                    question, answer, record); only the question (as the
                    subject) and the ruling cited in `record` are published,
                    never the answer or the record text

Usage: taken_feed.py --now <epoch> [--dir <fleet-policy state dir>] [--max N]

Env: CREDENTIAL_RE (an action whose text matches it is withheld and counted as
skipped). A source that is missing is reported `read: false`; when neither
exists nothing is printed and the exit status is 3, so the publisher leaves the
previous feed as it was. A line that does not parse or lacks a field is skipped
and counted per source (`skipped`); the helper never writes a file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "fleet-taken-feed.v1"
MAX_ITEMS = 200
# Only the tail of a log is read: the feed is its newest actions, and an audit
# log only grows.
TAIL_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
SUBJECT_MAX = 160
FIELD_MAX = 64
SELF_CALL_OUTCOME = "self-answered"
SELF_CALL_RULING_RE = re.compile(r"\b(P\d{1,3}|Tier [0-9]|Q\d{1,3})\b")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
CREDENTIAL_RE = re.compile(os.environ.get("CREDENTIAL_RE") or r"(?!)")

SOURCES = {"p16": "p16.jsonl", "self_calls": "self-calls.jsonl"}


def one_line(value: Any, limit: int) -> str | None:
    """The text on one line, trimmed and cut to `limit` characters, else None."""
    if not isinstance(value, str):
        return None
    text = " ".join(CONTROL_RE.sub(" ", value).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def epoch(value: Any) -> int | None:
    """Seconds since the epoch from a number, or an ISO 8601 string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
        except OverflowError:
            return None
        if not math.isfinite(seconds):
            return None
        if seconds > 1e11:  # milliseconds
            seconds /= 1000
        return int(seconds) if seconds > 0 else None
    if isinstance(value, str):
        try:
            moment = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        try:
            return int(moment.timestamp())
        except (OverflowError, OSError):
            return None
    return None


def read_tail(path: Path) -> list[bytes] | None:
    """The complete lines of the file's last TAIL_BYTES; None when unreadable."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - TAIL_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return None
    lines = data.split(b"\n")
    if start > 0:  # the first line may be cut in half
        lines = lines[1:]
    return lines


def action(source: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """One published action from a parsed log row, None when it lacks a field."""
    at = epoch(row.get("ts"))
    if at is None:
        return None
    if source == "p16":
        subject = one_line(row.get("subject"), SUBJECT_MAX)
        outcome = one_line(row.get("outcome"), FIELD_MAX)
        ruling = one_line(row.get("ruling"), FIELD_MAX)
        if not (subject and outcome and ruling):
            return None
        extras = {
            "class": one_line(row.get("class"), FIELD_MAX),
            "actor": one_line(row.get("actor"), FIELD_MAX),
            "home_id": one_line(row.get("home"), FIELD_MAX),
        }
    else:
        subject = one_line(row.get("question"), SUBJECT_MAX)
        if not subject:
            return None
        cited = SELF_CALL_RULING_RE.search(str(row.get("record") or ""))
        outcome = SELF_CALL_OUTCOME
        ruling = cited.group(1) if cited else SELF_CALL_OUTCOME
        extras = {"class": None, "actor": None, "home_id": one_line(row.get("home"), FIELD_MAX)}
    item = {
        "source": source,
        "ts": at,
        "ruling": ruling,
        "subject": subject,
        "outcome": outcome,
        "line": f"{outcome} {subject} ({ruling})",
        **extras,
    }
    if CREDENTIAL_RE.search(json.dumps(item, ensure_ascii=False)):
        return None
    return item


def read_source(source: str, path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lines = read_tail(path)
    if lines is None:
        return [], {"read": False, "lines": 0, "skipped": 0}
    items: list[dict[str, Any]] = []
    seen: dict[bytes, int] = {}
    skipped = count = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        count += 1
        row = None
        if len(stripped) <= MAX_LINE_BYTES:
            try:
                row = json.loads(stripped)
            except (ValueError, RecursionError):
                row = None
        item = action(source, row) if isinstance(row, dict) else None
        if item is None:
            skipped += 1
            continue
        # The id is the line's own hash, so it survives reruns and rotation of
        # the tail; a line repeated verbatim gets its occurrence number too.
        seen[stripped] = seen.get(stripped, 0) + 1
        digest = hashlib.sha256(stripped + b"#%d" % seen[stripped]).hexdigest()
        items.append({"id": f"{source.replace('_', '-')}-{digest[:16]}", **item})
    return items, {"read": True, "lines": count, "skipped": skipped}


def build(now: int, directory: Path, limit: int) -> dict[str, Any] | None:
    items: list[dict[str, Any]] = []
    sources: dict[str, Any] = {}
    for source, name in SOURCES.items():
        found, stats = read_source(source, directory / name)
        items += found
        sources[source] = stats
    if not any(stats["read"] for stats in sources.values()):
        return None
    items.sort(key=lambda i: (-i["ts"], i["id"]))
    return {
        "schema": SCHEMA,
        "generated_epoch": now,
        "sources": sources,
        "items": items[:limit],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--now", type=int, required=True)
    parser.add_argument(
        "--dir",
        default=os.environ.get("FLEET_POLICY_DIR")
        or str(Path.home() / ".local/state/fleet-policy"),
    )
    parser.add_argument("--max", type=int, default=MAX_ITEMS)
    args = parser.parse_args(argv)
    feed = build(args.now, Path(args.dir), max(0, min(args.max, MAX_ITEMS)))
    if feed is None:
        print("taken-feed: no fleet-policy log is readable", file=sys.stderr)
        return 3
    json.dump(feed, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
