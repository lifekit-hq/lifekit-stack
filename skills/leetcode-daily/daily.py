#!/usr/bin/env python3
"""learnclaw's daily LeetCode problem: the pick, the home-day rule, the record
and the weekly solved count (docs/runbook.md, "Daily LeetCode problem").

    daily.py next [--date D] [--record]   today's problem, or why there is none
    daily.py done SLUG --pattern P --time T --space S [--date D]
    daily.py skip SLUG [--reason R] [--date D]
    daily.py week [--date D]              problems solved Monday to Sunday
    daily.py weekfile --out F --target N [--date D]
                                          the count line as a file the morning job quotes
    daily.py status                       where the list stands
    daily.py check                        validate problems.json

The list is problems.json next to this file: patterns in Track A order, problems
in order inside a pattern. The pick is the first problem with no row in the
record, so a done, a skipped or an already offered problem never comes back.

There is no store of its own. State is the artefact record that
skills/learning-record writes, one `leetcode: <slug>` row per step, the latest
row being the state: `in progress` = offered, `done` = solved by hand (the
grilled cell holds the pattern and the complexity in the owner's words),
`dropped` = skipped. Rows are appended through record.py's own append, so its
checks and the vault lint apply.

`next` prints either `SEND` then the problem name and its link (nothing else:
no pattern, no hint), or one `SKIP <reason>` line. Tuesday and Thursday are
office days and Sunday is rest: no problem on those days, whatever the cron
fired. `next` without --record only looks; with --record it appends the
offered row, and a second call the same day prints SKIP.

`next --record --push` is what the cron runs, as a command payload (no model
turn): it prints only the two message lines on a send and exactly NO_REPLY on
a skip, and any error exits non-zero with nothing on stdout, so the run is
recorded as failed and the failure alert fires.

Env: LEARNING_VAULT, LEARNING_PAGE (as record.py), LEETCODE_TODAY (YYYY-MM-DD,
tests only; default the date in Europe/Dublin), LEETCODE_PROBLEMS (the list;
default problems.json next to this file), LEETCODE_RECORD_PY (default
../learning-record/record.py, the sibling skill).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREFIX = "leetcode: "
URL = "https://leetcode.com/problems/{}/"
# Monday=0 ... Sunday=6. Office days and the rest day get no problem.
BLOCKED = {
    1: "office day (Tuesday)",
    3: "office day (Thursday)",
    6: "rest day (Sunday)",
}


def fail(msg: str, code: int = 2) -> None:
    print(f"daily: {msg}", file=sys.stderr)
    sys.exit(code)


def load_record():
    path = Path(
        os.environ.get("LEETCODE_RECORD_PY")
        or HERE.parent / "learning-record/record.py"
    )
    if not path.is_file():
        fail(f"no record.py at {path}")
    spec = importlib.util.spec_from_file_location("learning_record", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def today() -> dt.date:
    forced = os.environ.get("LEETCODE_TODAY")
    if forced:
        return dt.date.fromisoformat(forced)
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo("Europe/Dublin")).date()
    except (
        Exception
    ):  # no tz database in the image: the host date is the same day at 17:30
        return dt.date.today()


def load_list() -> list[dict]:
    path = Path(os.environ.get("LEETCODE_PROBLEMS") or HERE / "problems.json")
    out = []
    for pat in json.loads(path.read_text())["patterns"]:
        for p in pat["problems"]:
            out.append({**p, "pattern": pat["id"], "pattern_name": pat["name"]})
    return out


def states(record, now_rows: list[dict[str, str]]) -> dict[str, dict]:
    """slug -> {status, date} from the latest row of each `leetcode:` topic."""
    latest: dict[str, dict] = {}
    for r in now_rows:
        if r["topic"].startswith(PREFIX):
            parsed = record.parse_date(r["date"])
            latest[r["topic"][len(PREFIX) :]] = {
                "status": r["status"],
                "date": parsed[1] if parsed and not parsed[0] else None,
                "row": r,
            }
    return latest


def read_state(record) -> dict[str, dict]:
    _, page = record.page_path()
    return states(record, record.rows(page.read_text().splitlines()))


def pick(problems: list[dict], state: dict[str, dict]) -> dict | None:
    return next((p for p in problems if p["slug"] not in state), None)


def offered_on(state: dict[str, dict], day: dt.date) -> str | None:
    for slug, s in state.items():
        if s["status"] == "in progress" and s["date"] == day:
            return slug
    return None


def week_of(day: dt.date) -> tuple[dt.date, dt.date]:
    monday = day - dt.timedelta(days=day.weekday())
    return monday, monday + dt.timedelta(days=6)


def row_args(**kw) -> argparse.Namespace:
    base = dict(
        date="", reading="-", applied="-", post="-", grilled="-", status="in progress"
    )
    return argparse.Namespace(**{**base, **kw})


def skip(args: argparse.Namespace, reason: str) -> None:
    # --push is the cron's command payload: its stdout is the message, and the
    # scheduler suppresses a reply that is exactly NO_REPLY.
    print("NO_REPLY" if args.push else f"SKIP {reason}")


def cmd_next(args: argparse.Namespace) -> None:
    day = dt.date.fromisoformat(args.date) if args.date else today()
    if day.weekday() in BLOCKED:
        skip(args, BLOCKED[day.weekday()])
        return
    record, problems = load_record(), load_list()
    state = read_state(record)
    already = offered_on(state, day)
    if already:
        skip(args, f"offered today: {already}")
        return
    nxt = pick(problems, state)
    if nxt is None:
        skip(args, "list finished: every problem is done, skipped or offered")
        return
    if args.record:
        # record.append prints "recorded: ..."; the cron sends this script's
        # output, so nothing may precede SEND.
        with contextlib.redirect_stdout(io.StringIO()):
            record.append(
                row_args(
                    date=day.isoformat(),
                    topic=PREFIX + nxt["slug"],
                    artefact=URL.format(nxt["slug"]),
                )
            )
    if not args.push:
        print("SEND")
    print(nxt["title"])
    print(URL.format(nxt["slug"]))


def known(problems: list[dict], slug: str) -> dict:
    for p in problems:
        if p["slug"] == slug:
            return p
    fail(f"{slug!r} is not in the problem list")
    raise AssertionError  # unreachable


def cmd_done(args: argparse.Namespace) -> None:
    for key in ("pattern", "time", "space"):
        if not getattr(args, key).strip():
            fail(f"--{key} is required: the owner's own words")
    record = load_record()
    p = known(load_list(), args.slug)
    record.append(
        row_args(
            date=args.date or today().isoformat(),
            topic=PREFIX + p["slug"],
            artefact=URL.format(p["slug"]),
            grilled=f"pattern: {args.pattern}; time {args.time}; space {args.space}",
            status="done",
        )
    )


def cmd_skip(args: argparse.Namespace) -> None:
    record = load_record()
    p = known(load_list(), args.slug)
    why = f" (skipped: {args.reason.strip()})" if args.reason.strip() else ""
    record.append(
        row_args(
            date=args.date or today().isoformat(),
            topic=PREFIX + p["slug"],
            artefact=URL.format(p["slug"]) + why,
            status="dropped",
        )
    )


def cmd_week(args: argparse.Namespace) -> None:
    day = dt.date.fromisoformat(args.date) if args.date else today()
    first, last = week_of(day)
    record = load_record()
    titles = {p["slug"]: p["title"] for p in load_list()}
    solved = sorted(
        (s["date"], slug)
        for slug, s in read_state(record).items()
        if s["status"] == "done" and s["date"] and first <= s["date"] <= last
    )
    print(f"solved: {len(solved)} (Monday {first} to Sunday {last}, by hand)")
    for d, slug in solved:
        print(f"- {d} {titles.get(slug, slug)}")


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def week_line(day: dt.date, target: int) -> str:
    """`<Mon..Sun> <solved>/<target>`: the count `week` prints, in the morning line's form."""
    first, last = week_of(day)
    solved = sum(
        1
        for s in read_state(load_record()).values()
        if s["status"] == "done" and s["date"] and first <= s["date"] <= last
    )
    return f"{WEEKDAYS[day.weekday()]} {solved}/{target}"


def cmd_weekfile(args: argparse.Namespace) -> None:
    """Write {"for_date", "line"} for learnclaw-morning, which only quotes it.

    The file is replaced atomically. When the record cannot be read the line
    says so (never 0), and the run exits 1 so the failure alert fires.
    """
    day = dt.date.fromisoformat(args.date) if args.date else today()
    if args.target < 1:
        fail("--target must be a positive whole number")
    err = ""
    try:
        line = week_line(day, args.target)
    except (SystemExit, Exception) as e:  # an unreadable page or record
        err = str(e) or type(e).__name__
        line = f"{WEEKDAYS[day.weekday()]} count unreadable (target {args.target})"
    out = Path(args.out)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps({"for_date": day.isoformat(), "line": line}) + "\n")
    tmp.replace(out)
    if err:
        fail(f"cannot count the week: {err}", 1)
    print("NO_REPLY")


def cmd_status(_: argparse.Namespace) -> None:
    record, problems = load_record(), load_list()
    state = read_state(record)
    count = {"done": 0, "dropped": 0, "in progress": 0}
    for s in state.values():
        count[s["status"]] = count.get(s["status"], 0) + 1
    nxt = pick(problems, state)
    print(
        f"leetcode: {count['done']} done, {count['dropped']} skipped, "
        f"{count['in progress']} open, {len(problems)} in the list"
    )
    print(
        f"next: {nxt['title']} ({nxt['pattern']} {nxt['pattern_name']})"
        if nxt
        else "next: none, the list is finished"
    )


def cmd_check(_: argparse.Namespace) -> None:
    path = Path(os.environ.get("LEETCODE_PROBLEMS") or HERE / "problems.json")
    data = json.loads(path.read_text())
    seen, ids, bad = set(), set(), []
    for pat in data["patterns"]:
        if pat["id"] in ids:
            bad.append(f"duplicate pattern id {pat['id']}")
        ids.add(pat["id"])
        if not pat["problems"] and not pat.get("note"):
            bad.append(f"pattern {pat['id']} has no problems and no note")
        for p in pat["problems"]:
            if p["slug"] in seen:
                bad.append(f"duplicate slug {p['slug']}")
            seen.add(p["slug"])
            if not p["title"].strip() or "|" in p["title"]:
                bad.append(f"bad title for {p['slug']}")
    if bad:
        fail("; ".join(bad), 1)
    print(f"ok: {len(data['patterns'])} patterns, {len(seen)} problems")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("next")
    n.add_argument("--date", default="")
    n.add_argument("--record", action="store_true")
    n.add_argument("--push", action="store_true")
    d = sub.add_parser("done")
    d.add_argument("slug")
    d.add_argument("--pattern", default="")
    d.add_argument("--time", default="")
    d.add_argument("--space", default="")
    d.add_argument("--date", default="")
    s = sub.add_parser("skip")
    s.add_argument("slug")
    s.add_argument("--reason", default="")
    s.add_argument("--date", default="")
    w = sub.add_parser("week")
    w.add_argument("--date", default="")
    f = sub.add_parser("weekfile")
    f.add_argument("--out", required=True)
    f.add_argument("--target", type=int, required=True)
    f.add_argument("--date", default="")
    sub.add_parser("status")
    sub.add_parser("check")
    args = p.parse_args()
    {
        "next": cmd_next,
        "done": cmd_done,
        "skip": cmd_skip,
        "week": cmd_week,
        "weekfile": cmd_weekfile,
        "status": cmd_status,
        "check": cmd_check,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
