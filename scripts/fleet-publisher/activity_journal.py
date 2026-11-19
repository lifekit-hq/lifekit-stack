#!/usr/bin/env python3
"""activity-journal - the fleet's daily activity journal and two-weekly retro board.

Appends one mechanical row per UTC day to OUT_DIR/journal/activity.jsonl (schema
fleet-activity-journal.v1) and, once 14 journal days have gone by since the
last board, writes a retro board from those rows to
OUT_DIR/journal/retro/<from>_<to>.json and .md (contract: README.md "Activity
journal and retro board"). The store lives under the publisher's OUT_DIR, on
disk and never in the vault, so it survives a publisher restart.

Usage: activity_journal.py --now <epoch> --out-dir <dir> [--ledger FILE ...]
                           [--daily FILE ...]

Rows hold counts and identifiers only (repo slugs, ruling ids, metric names):
no task title, decision text or session content is ever copied. A source that
cannot be read writes the string "unavailable" for the field it feeds, never a
missing key; a row is appended even when every source fails. A date that
already has a row is never written again, so a rerun adds nothing.

Sources (all read-only): the fleet ledgers (merged PRs, failed tasks, resolved
decisions), OUT_DIR/taken-for-you.json (decisions taken for the owner),
OUT_DIR/metrics/daily.jsonl (stalled task hours, fleet spend in weighted
tokens), Prometheus (OpenClaw runs and errors, DevClaw sessions), gh (red CI on
main) and the clip stats feed.

Env: FLEET_PROM_URL (default http://127.0.0.1:9090; empty skips), FLEET_GH
(default gh; empty skips), FLEET_JOURNAL_REPOS (owner/name list for red CI;
default the repos whose PRs the ledger merged in the 14 days before the day),
FLEET_CLIP_STATS (path or http(s) URL of the clip stats JSON; only its numeric
top-level fields are kept; unset skips), FLEET_JOURNAL_DELAY (seconds a day
must be over before its row is written, default 7200, so the day's fleet
metrics exist), CREDENTIAL_RE (output that matches it is not written).

Exit status: 0, or 1 when a file could not be written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA = "fleet-activity-journal.v1"
RETRO_SCHEMA = "fleet-retro-board.v1"
UNAVAILABLE = "unavailable"
RETRO_DAYS = 14
CATCH_UP_DAYS = 14
RECURRING_DAYS = 3
FIELDS = (
    "merged_prs_by_repo",
    "decisions_owner",
    "decisions_taken_for_owner",
    "failed_tasks",
    "red_ci_runs",
    "stalled_task_hours",
    "devclaw_sessions",
    "openclaw_runs",
    "openclaw_errors",
    "fleet_weighted_tokens",
    "clip_stats",
)
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
PR_RE = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)/pull/\d+")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._#-]{0,63}$")
CREDENTIAL_RE = re.compile(os.environ.get("CREDENTIAL_RE") or r"(?!)")


class SourceError(Exception):
    """A source could not be read; the field it feeds becomes "unavailable"."""


def day_bounds(day: dt.date) -> tuple[int, int]:
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    return int(start.timestamp()), int(start.timestamp()) + 86400


def jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise SourceError(str(exc)) from exc
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# ---- sources: each returns the field's value or raises SourceError ----------


class Ledgers:
    """The fleet ledgers, read once; at least one must be readable."""

    def __init__(self, paths: list[str]) -> None:
        self.events: list[dict[str, Any]] = []
        self.readable = False
        for raw in paths:
            try:
                self.events += jsonl(Path(raw))
                self.readable = True
            except SourceError:
                continue

    def window(self, event: str, start: int, end: int) -> list[dict[str, Any]]:
        if not self.readable:
            raise SourceError("no ledger readable")
        return [
            e
            for e in self.events
            if e.get("event") == event
            and isinstance(e.get("ts"), (int, float))
            and not isinstance(e.get("ts"), bool)
            and start <= e["ts"] < end
        ]


def pr_repo(event: dict[str, Any]) -> str:
    match = PR_RE.match(str(event.get("pr") or ""))
    return match.group(1) if match else "unknown"


def merged_by_repo(ledgers: Ledgers, start: int, end: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in ledgers.window("task.merged", start, end):
        repo = pr_repo(e)
        counts[repo] = counts.get(repo, 0) + 1
    return dict(sorted(counts.items()))


def failed_tasks(ledgers: Ledgers, start: int, end: int) -> int:
    return len(
        {
            e.get("task")
            for e in ledgers.window("task.status", start, end)
            if e.get("state") == "failed"
        }
    )


def decisions_owner(ledgers: Ledgers, start: int, end: int) -> int:
    """Owner answers: status lines that resolve a keyed decision or blocker."""
    return sum(
        1
        for e in ledgers.window("task.status", start, end)
        if e.get("state") == "resolved" and e.get("key")
    )


def taken_for_owner(out_dir: Path, start: int, end: int) -> dict[str, Any]:
    path = out_dir / "taken-for-you.json"
    try:
        feed = json.loads(path.read_text())
        items = feed["items"]
        if not isinstance(items, list):
            raise ValueError("items")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"taken feed: {exc}") from exc
    by_ruling: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        ts = item.get("ts")
        if (
            isinstance(ts, bool)
            or not isinstance(ts, (int, float))
            or not (start <= ts < end)
        ):
            continue
        ruling = item.get("ruling")
        ruling = (
            ruling if isinstance(ruling, str) and SLUG_RE.match(ruling) else "other"
        )
        by_ruling[ruling] = by_ruling.get(ruling, 0) + 1
    return {
        "total": sum(by_ruling.values()),
        "by_ruling": dict(sorted(by_ruling.items())),
    }


def daily_metric(paths: list[str], day: dt.date, field: str) -> float:
    """The day's final fleet metric summed over homes; a partial row is not final."""
    total, seen = 0.0, False
    for raw in paths:
        try:
            rows = jsonl(Path(raw))
        except SourceError:
            continue
        for row in rows:
            value = row.get(field)
            if (
                row.get("date") == day.isoformat()
                and row.get("partial") is False
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                total += value
                seen = True
    if not seen:
        raise SourceError(f"no final daily metrics row with {field}")
    return round(total, 2)


def prometheus(query: str, at: int) -> float | None:
    """One instant-query value, None for an empty result."""
    base = os.environ.get("FLEET_PROM_URL", "http://127.0.0.1:9090")
    if not base:
        raise SourceError("FLEET_PROM_URL is empty")
    url = f"{base.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode(
        {"query": query, "time": at}
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - operator-set URL
            doc = json.load(resp)
        result = doc["data"]["result"]
        if doc["status"] != "success" or not isinstance(result, list):
            raise ValueError("status")
        return float(result[0]["value"][1]) if result else None
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise SourceError(f"prometheus: {exc}") from exc


def prom_count(query: str, at: int, *, empty_is_zero: bool) -> int:
    value = prometheus(query, at)
    if value is None:
        if empty_is_zero:
            return 0
        raise SourceError("no series")
    if value != value or value < 0:  # NaN or negative
        raise SourceError("not a count")
    return int(round(value))


def red_ci(ledgers: Ledgers, day: dt.date, start: int, end: int) -> int:
    gh = os.environ.get("FLEET_GH", "gh")
    if not gh:
        raise SourceError("FLEET_GH is empty")
    repos = os.environ.get("FLEET_JOURNAL_REPOS", "").split()
    if not repos:
        seen: set[str] = set()
        for e in ledgers.window("task.merged", start - 14 * 86400, end):
            repo = pr_repo(e)
            if repo != "unknown":
                seen.add(repo)
        repos = sorted(seen)
    if not repos or not all(REPO_RE.match(r) for r in repos):
        raise SourceError("no repos to read")
    total = 0
    for repo in repos:
        try:
            out = subprocess.run(  # noqa: S603 - fixed argv
                [
                    gh,
                    "run",
                    "list",
                    "-R",
                    repo,
                    "--branch",
                    "main",
                    "--status",
                    "failure",
                    "--limit",
                    "100",
                    "--json",
                    "createdAt",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout
            runs = json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise SourceError(f"gh {repo}: {exc}") from exc
        if not isinstance(runs, list):
            raise SourceError(f"gh {repo}: not a list")
        total += sum(
            1
            for r in runs
            if isinstance(r, dict)
            and str(r.get("createdAt", ""))[:10] == day.isoformat()
        )
    return total


def clip_stats() -> dict[str, float]:
    src = os.environ.get("FLEET_CLIP_STATS", "")
    if not src:
        raise SourceError("FLEET_CLIP_STATS is unset")
    try:
        if src.startswith(("http://", "https://")):
            with urllib.request.urlopen(src, timeout=10) as resp:  # noqa: S310 - operator-set URL
                doc = json.load(resp)
        else:
            doc = json.loads(Path(src).read_text())
    except (OSError, ValueError) as exc:
        raise SourceError(f"clip stats: {exc}") from exc
    if not isinstance(doc, dict):
        raise SourceError("clip stats: not an object")
    # Numeric top-level fields only: no free text is carried over.
    kept = {
        k: v
        for k, v in doc.items()
        if SLUG_RE.match(k) and isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    if not kept:
        raise SourceError("clip stats: no numeric fields")
    return dict(sorted(kept.items())[:20])


# ---- the journal --------------------------------------------------------------


def read_rows(path: Path) -> list[dict[str, Any]]:
    try:
        rows = jsonl(path)
    except SourceError:
        return []
    return [
        r for r in rows if r.get("schema") == SCHEMA and isinstance(r.get("date"), str)
    ]


def build_row(
    day: dt.date, now: int, args: argparse.Namespace, ledgers: Ledgers, out_dir: Path
) -> dict[str, Any]:
    start, end = day_bounds(day)
    at = end
    sources = {
        "merged_prs_by_repo": lambda: merged_by_repo(ledgers, start, end),
        "decisions_owner": lambda: decisions_owner(ledgers, start, end),
        "decisions_taken_for_owner": lambda: taken_for_owner(out_dir, start, end),
        "failed_tasks": lambda: failed_tasks(ledgers, start, end),
        "red_ci_runs": lambda: red_ci(ledgers, day, start, end),
        "stalled_task_hours": lambda: daily_metric(args.daily, day, "stall_task_hours"),
        "devclaw_sessions": lambda: prom_count(
            "sum(increase(devclaw_sessions_total[1d]))", at, empty_is_zero=False
        ),
        "openclaw_runs": lambda: prom_count(
            "sum(increase(openclaw_run_completed_total[1d]))", at, empty_is_zero=False
        ),
        "openclaw_errors": lambda: prom_count(
            'sum(increase(openclaw_run_completed_total{outcome!="completed"}[1d]))',
            at,
            empty_is_zero=True,
        ),
        "fleet_weighted_tokens": lambda: int(
            daily_metric(args.daily, day, "weighted_tokens")
        ),
        "clip_stats": clip_stats,
    }
    row: dict[str, Any] = {
        "schema": SCHEMA,
        "date": day.isoformat(),
        "recorded_epoch": now,
    }
    for name in FIELDS:
        try:
            row[name] = sources[name]()
        except SourceError as exc:
            print(
                f"activity-journal: {day}: {name} unavailable: {exc}", file=sys.stderr
            )
            row[name] = UNAVAILABLE
    return row


def write_atomic(path: Path, text: str) -> None:
    if CREDENTIAL_RE.search(text):
        raise OSError(f"{path.name} looks like it holds a credential; not written")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def journal_days(rows: list[dict[str, Any]], now: int) -> list[dt.date]:
    """The dates still owed a row: last stored date + 1 .. the last finished day."""
    delay = int(os.environ.get("FLEET_JOURNAL_DELAY", "7200"))
    last_done = dt.datetime.fromtimestamp(
        now - delay, dt.timezone.utc
    ).date() - dt.timedelta(days=1)
    have = {r["date"] for r in rows}
    stored = sorted(have)
    first = (
        last_done
        if not stored
        else dt.date.fromisoformat(stored[-1]) + dt.timedelta(days=1)
    )
    first = max(first, last_done - dt.timedelta(days=CATCH_UP_DAYS - 1))
    days, day = [], first
    while day <= last_done:
        if day.isoformat() not in have:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


# ---- the retro board ----------------------------------------------------------


def num(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def problems_of(row: dict[str, Any]) -> list[str]:
    found = []
    if (num(row.get("failed_tasks")) or 0) > 0:
        found.append("failed-tasks")
    if (num(row.get("red_ci_runs")) or 0) > 0:
        found.append("red-ci")
    if (num(row.get("stalled_task_hours")) or 0) > 0:
        found.append("stalled-work")
    if (num(row.get("openclaw_errors")) or 0) > 0:
        found.append("openclaw-errors")
    found += [f"unavailable:{f}" for f in FIELDS if row.get(f) == UNAVAILABLE]
    return found


CHANGES = {
    "failed-tasks": "Review the failed tasks on the recurring days and fix the cause they share before dispatching more of that kind.",
    "red-ci": "Make a red main CI run block new merges until it is green again, and fix the flakiest workflow first.",
    "stalled-work": "Set a stall budget: items stalled more than a day are escalated or dropped rather than left to age.",
    "openclaw-errors": "Look at the OpenClaw runs that ended in error on the recurring days and fix the failing trigger or model.",
}


def change_for(problem: str, days: int) -> str:
    if problem.startswith("unavailable:"):
        field = problem.split(":", 1)[1]
        return f"Repair the {field} source so the journal stops writing 'unavailable' for it ({days} days this period)."
    return CHANGES[problem]


def retro_board(
    rows: list[dict[str, Any]], first: dt.date, last: dt.date, now: int
) -> dict[str, Any]:
    per_day = {r["date"]: problems_of(r) for r in rows}
    tally: dict[str, list[str]] = {}
    for date, found in sorted(per_day.items()):
        for problem in found:
            tally.setdefault(problem, []).append(date)
    # Real fleet problems rank before a broken journal source of the same frequency.
    ranked = sorted(
        tally.items(),
        key=lambda kv: (kv[0].startswith("unavailable:"), -len(kv[1]), kv[0]),
    )
    recurring = [
        {"problem": p, "days": len(d), "dates": d}
        for p, d in ranked
        if len(d) >= RECURRING_DAYS
    ]
    # Always three changes: recurring problems first, then the next most frequent.
    basis = [{"problem": p, "days": len(d), "dates": d} for p, d in ranked]
    proposed = [
        {
            "change": change_for(b["problem"], b["days"]),
            "basis": b["problem"],
            "days": b["days"],
        }
        for b in basis[:3]
    ]
    while len(proposed) < 3:
        proposed.append(
            {
                "change": "No further problem showed up in these rows; keep the current practice and re-read the next board.",
                "basis": "none",
                "days": 0,
            }
        )

    merged = sum(
        sum(v.values())
        for r in rows
        if isinstance(v := r.get("merged_prs_by_repo"), dict)
    )
    runs = sum(num(r.get("openclaw_runs")) or 0 for r in rows)
    errors = sum(num(r.get("openclaw_errors")) or 0 for r in rows)
    clean = [
        r["date"]
        for r in rows
        if not any(not p.startswith("unavailable:") for p in per_day[r["date"]])
    ]
    went_well = []
    if merged:
        best = max(
            rows,
            key=lambda r: sum(r["merged_prs_by_repo"].values())
            if isinstance(r.get("merged_prs_by_repo"), dict)
            else -1,
        )
        went_well.append(
            f"{merged} PRs merged over {len(rows)} days; busiest day {best['date']}."
        )
    if clean:
        went_well.append(
            f"{len(clean)} of {len(rows)} days had no failed task, red CI, stalled work or OpenClaw error."
        )
    if runs and errors / runs < 0.05:
        went_well.append(
            f"OpenClaw error rate {errors / runs:.1%} over {int(runs)} runs."
        )
    taken = sum(
        r["decisions_taken_for_owner"]["total"]
        for r in rows
        if isinstance(r.get("decisions_taken_for_owner"), dict)
    )
    if taken:
        went_well.append(
            f"{taken} decisions were taken for the owner under standing rulings."
        )
    went_wrong = []
    for label, field in (
        ("failed tasks", "failed_tasks"),
        ("red CI runs on main", "red_ci_runs"),
        ("OpenClaw errors", "openclaw_errors"),
    ):
        total = sum(num(r.get(field)) or 0 for r in rows)
        if total:
            days = sum(1 for r in rows if (num(r.get(field)) or 0) > 0)
            went_wrong.append(f"{int(total)} {label} on {days} days.")
    stalled = sum(num(r.get("stalled_task_hours")) or 0 for r in rows)
    if stalled:
        went_wrong.append(f"{stalled:.1f} stalled task-hours.")
    gaps = sum(1 for r in rows for f in FIELDS if r.get(f) == UNAVAILABLE)
    if gaps:
        went_wrong.append(f"{gaps} journal fields were unavailable.")
    return {
        "schema": RETRO_SCHEMA,
        "from": first.isoformat(),
        "to": last.isoformat(),
        "generated_epoch": now,
        "days": len(rows),
        "went_well": went_well,
        "went_wrong": went_wrong,
        "recurring_problems": recurring,
        "proposed_changes": proposed,
    }


def retro_markdown(board: dict[str, Any]) -> str:
    lines = [
        f"# Retro {board['from']} to {board['to']}",
        "",
        f"Built from {board['days']} journal rows.",
        "",
    ]
    for title, key in (
        ("What went well", "went_well"),
        ("What went wrong", "went_wrong"),
    ):
        lines += (
            [f"## {title}", ""]
            + [f"- {x}" for x in board[key] or ["Nothing to report."]]
            + [""]
        )
    lines += ["## Recurring problems", ""]
    lines += [
        f"- {r['problem']}: {r['days']} days ({', '.join(r['dates'])})"
        for r in board["recurring_problems"]
    ] or ["- None."]
    lines += ["", "## Proposed changes", ""]
    lines += [
        f"{i}. {c['change']} (basis: {c['basis']}, {c['days']} days)"
        for i, c in enumerate(board["proposed_changes"], 1)
    ]
    return "\n".join(lines) + "\n"


def due_retro(
    rows: list[dict[str, Any]], retro_dir: Path
) -> tuple[dt.date, dt.date] | None:
    """The next 14-day window with 14 stored days after the last board, if complete."""
    if not rows:
        return None
    done = (
        sorted(p.stem.split("_")[1] for p in retro_dir.glob("*_*.json"))
        if retro_dir.is_dir()
        else []
    )
    dates = sorted(r["date"] for r in rows)
    first = (
        dt.date.fromisoformat(done[-1]) + dt.timedelta(days=1)
        if done
        else dt.date.fromisoformat(dates[0])
    )
    last = first + dt.timedelta(days=RETRO_DAYS - 1)
    return (first, last) if dates[-1] >= last.isoformat() else None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--now", type=int, required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--ledger", action="append", default=[])
    parser.add_argument("--daily", action="append", default=[])
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    store = out_dir / "journal" / "activity.jsonl"
    retro_dir = out_dir / "journal" / "retro"
    status = 0

    rows = read_rows(store)
    days = journal_days(rows, args.now)
    if days:
        ledgers = Ledgers(args.ledger)
        new = [build_row(d, args.now, args, ledgers, out_dir) for d in days]
        rows = sorted(rows + new, key=lambda r: r["date"])
        try:
            write_atomic(
                store, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
            )
        except OSError as exc:
            print(f"activity-journal: not written: {exc}", file=sys.stderr)
            return 1

    while (window := due_retro(rows, retro_dir)) is not None:
        first, last = window
        in_window = [
            r for r in rows if first.isoformat() <= r["date"] <= last.isoformat()
        ]
        board = retro_board(in_window, first, last, args.now)
        stem = retro_dir / f"{first}_{last}"
        try:
            write_atomic(stem.with_suffix(".md"), retro_markdown(board))
            write_atomic(
                stem.with_suffix(".json"),
                json.dumps(board, indent=2, sort_keys=True) + "\n",
            )
        except OSError as exc:
            print(f"activity-journal: retro not written: {exc}", file=sys.stderr)
            status = 1
            break
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
