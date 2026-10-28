#!/usr/bin/env python3
"""learnclaw's one write path into the vault: the artefact record on the
learning page (decision 0010 in the vault: the artefact is the unit of record,
projections are computed at read).

    record.py status            the record and its projections (last artefact,
                                days since, checkpoints due or missed, what each
                                in-progress row still lacks), computed now
    record.py append --topic T --artefact A [--reading R] [--applied X]
                     [--post P] [--grilled G] [--status S] [--date D]
                                one new row at the end of the table

The table is append-only: a row is never rewritten, a correction is a new row.
`append` refuses a cell with a pipe or a line break, a status other than
"in progress", "done" or "dropped", and "done" while applied, post or grilled
is still missing (the page: a row without all three is in progress). It bumps
the page's updatedAt, then lints the page with the vault's own
`bin/lint.sh --changed --tsv`; a high or medium finding on the page puts the
page back exactly as it was and exits 1. It never commits: the vault's sync
timer does (vault README, "never commit manually").

Env: LEARNING_VAULT (default ~/memory), LEARNING_PAGE (default
domains/learning.md, relative to the vault), LEARNING_TODAY (YYYY-MM-DD,
tests only; default the local date).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

HEADING = "## Artefact record"
COLUMNS = (
    "date",
    "topic",
    "artefact",
    "reading",
    "applied",
    "post",
    "grilled",
    "status",
)
STATUSES = ("in progress", "done", "dropped")
PROOF = ("applied", "post", "grilled")
DATE = re.compile(r"^(due )?(\d{4}-\d{2}-\d{2})$")


def fail(msg: str, code: int = 2) -> None:
    print(f"record: {msg}", file=sys.stderr)
    sys.exit(code)


def today() -> dt.date:
    forced = os.environ.get("LEARNING_TODAY")
    return dt.date.fromisoformat(forced) if forced else dt.date.today()


def page_path() -> tuple[Path, Path]:
    vault = Path(os.environ.get("LEARNING_VAULT") or Path.home() / "memory")
    page = vault / os.environ.get("LEARNING_PAGE", "domains/learning.md")
    if not page.is_file():
        fail(f"no learning page at {page}")
    return vault, page


def table_span(lines: list[str]) -> tuple[int, int]:
    """[start, end) of the record table's lines: header, separator, rows."""
    try:
        head = next(i for i, ln in enumerate(lines) if ln.startswith(HEADING))
    except StopIteration:
        fail(f"no '{HEADING}' section on the page")
    start = next(
        (i for i in range(head + 1, len(lines)) if lines[i].startswith("|")), None
    )
    if start is None or any(ln.startswith("## ") for ln in lines[head + 1 : start]):
        fail("the artefact record section has no table")
    end = start
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    if end - start < 2 or len(cells(lines[start])) != len(COLUMNS):
        fail(f"the record table is not the {len(COLUMNS)}-column table this tool knows")
    return start, end


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def missing(value: str) -> bool:
    return value in ("", "-") or value.endswith("(planned)")


def rows(lines: list[str]) -> list[dict[str, str]]:
    start, end = table_span(lines)
    out = []
    for ln in lines[start + 2 : end]:
        c = cells(ln)
        if len(c) == len(COLUMNS):
            out.append(dict(zip(COLUMNS, c, strict=True)))
    return out


def status(lines: list[str], now: dt.date) -> str:
    record = rows(lines)
    out = [f"artefact record: {len(record)} row(s), today {now}"]
    dated = [
        (dt.date.fromisoformat(m.group(2)), r)
        for r in record
        if (m := DATE.match(r["date"])) and not m.group(1)
    ]
    if dated:
        last, row = max(dated, key=lambda d: d[0])
        out.append(
            f"last artefact row: {last} ({row['topic']}), {(now - last).days} days ago"
        )
    else:
        out.append("last artefact row: none yet")
    # A topic's latest row is its state: the table is append-only, so a
    # correction or a step forward is always the later row.
    latest = {r["topic"]: r for r in record}
    for r in record:
        m = DATE.match(r["date"])
        if m and m.group(1) and latest[r["topic"]]["status"] == "in progress":
            due = dt.date.fromisoformat(m.group(2))
            days = (now - due).days
            when = f"missed by {days} days" if days > 0 else f"due in {-days} days"
            out.append(f"checkpoint {due} ({r['topic']}): {when}")
    for topic, r in latest.items():
        if r["status"] == "in progress":
            lacks = [k for k in PROOF if missing(r[k])]
            out.append(
                f"in progress: {topic} - lacks {', '.join(lacks) or 'nothing (mark done)'}"
            )
    for state in ("done", "dropped"):
        topics = sorted(t for t, r in latest.items() if r["status"] == state)
        out.append(
            f"{state}: {len(topics)} topic(s){': ' + ', '.join(topics) if topics else ''}"
        )
    return "\n".join(out)


def new_row(args: argparse.Namespace, now: dt.date) -> str:
    row = {
        "date": args.date or now.isoformat(),
        "topic": args.topic,
        "artefact": args.artefact,
        "reading": args.reading,
        "applied": args.applied,
        "post": args.post,
        "grilled": args.grilled,
        "status": args.status,
    }
    for key, value in row.items():
        value = value.strip()
        if "|" in value or "\n" in value or "\r" in value:
            fail(f"{key}: a cell cannot hold a pipe or a line break")
        row[key] = value or "-"
    if not DATE.match(row["date"]):
        fail("date: YYYY-MM-DD, or 'due YYYY-MM-DD' for a planned artefact")
    for key in ("topic", "artefact"):
        if missing(row[key]):
            fail(f"{key} is required")
    if row["status"] not in STATUSES:
        fail(f"status: one of {', '.join(STATUSES)}")
    lacks = [k for k in PROOF if missing(row[k])]
    if row["status"] == "done" and lacks:
        fail(f"status done needs {', '.join(lacks)} too; record it as in progress")
    return "| " + " | ".join(row[k] for k in COLUMNS) + " |"


def lint_findings(vault: Path, page: Path) -> list[str]:
    rel = page.relative_to(vault).as_posix()
    r = subprocess.run(
        ["sh", "bin/lint.sh", "--changed", "--tsv"],
        cwd=vault,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode not in (
        0,
        1,
    ):  # 1 is "findings"; anything else is a lint that did not run
        return [f"lint exited {r.returncode}: {r.stderr.strip()[:300]}"]
    return [
        ln
        for ln in r.stdout.splitlines()
        if (f := ln.split("\t"))
        and len(f) >= 3
        and f[0] in ("high", "medium")
        and f[2] == rel
    ]


def append(args: argparse.Namespace) -> None:
    vault, page = page_path()
    if not (vault / "bin/lint.sh").is_file():
        fail(f"no vault lint at {vault / 'bin/lint.sh'}; nothing written")
    now = today()
    original = page.read_text()
    lines = original.splitlines()
    row = new_row(args, now)
    _, end = table_span(lines)
    lines.insert(end, row)
    text = "\n".join(lines) + "\n"
    text, bumped = re.subn(
        r"^updatedAt: .*$", f"updatedAt: {now.isoformat()}", text, count=1, flags=re.M
    )
    if not bumped:
        fail("the page has no updatedAt to bump; nothing written")
    tmp = page.with_name(f".{page.name}.record.tmp")
    tmp.write_text(text)
    tmp.replace(page)
    findings = lint_findings(vault, page)
    if findings:
        page.write_text(original)
        print(
            "record: the vault lint refused the row; the page is back as it was",
            file=sys.stderr,
        )
        print("\n".join(findings), file=sys.stderr)
        sys.exit(1)
    print(f"recorded: {row}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    a = sub.add_parser("append")
    a.add_argument("--date", default="")
    a.add_argument("--topic", required=True)
    a.add_argument("--artefact", required=True)
    a.add_argument("--reading", default="-")
    a.add_argument("--applied", default="-")
    a.add_argument("--post", default="-")
    a.add_argument("--grilled", default="-")
    a.add_argument("--status", default="in progress")
    args = p.parse_args()
    if args.cmd == "status":
        _, page = page_path()
        print(status(page.read_text().splitlines(), today()))
    else:
        append(args)


if __name__ == "__main__":
    main()
