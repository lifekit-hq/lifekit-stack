#!/usr/bin/env python3
"""rotate-state.py — the mechanism half of memory-rotate, for runtime state.

Does only what logrotate can't, and only outside the vault (the vault rotates
itself with its own bin/rotate.sh). Pure stdlib, idempotent, safe to re-run:
  - morning-brief/inbox.md : list items dated > 30d ago  -> inbox-archive.md
                             (under --brief-dir, /var/lib/lifekit/morning-brief)
  - OpenClaw agent trajectory logs: gzip after 7 days, delete the .gz after 60
  - a report-only session footprint, a heartbeat and a run-log line

It replaced rotate-extras.py, which rotated the inbox and the closed proposals
inside the vault; both now belong elsewhere (the inbox moved to the
runtime-state dir, the proposals archive is the vault's own rotation). No LLM
call belongs here.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

INBOX_HOT_DAYS = 30
TRAJECTORY_GZIP_DAYS = 7  # gzip completed-run trajectory logs older than this
TRAJECTORY_DELETE_DAYS = 60  # delete gzipped trajectory archives older than this

# A leading list item that opens with a date, e.g. "- 2026-05-28 — note"
INBOX_ITEM_RE = re.compile(r"^\s*-\s+(\d{4}-\d{2}-\d{2})\b")


def parse_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def append_archive(archive: Path, header: str, chunks: list[str]) -> None:
    """Append moved chunks under a dated rotation banner, creating file if needed."""
    if not chunks:
        return
    banner = f"\n<!-- rotated {header} -->\n"
    existing = archive.read_text() if archive.exists() else _archive_preamble(archive)
    archive.write_text(existing + banner + "\n".join(chunks).rstrip() + "\n")


def _archive_preamble(archive: Path) -> str:
    name = archive.stem.replace("-archive", "")
    return (
        f"# {name} — archive\n\n"
        f"Rotated out of `{name}.md` by scripts/rotate/rotate-state.py. Hot tier keeps recent "
        f"entries; this is the warm tier. Reorganized, never deleted.\n"
    )


# ---------------------------------------------------------------------------
# morning-brief inbox.md — line-based, items opening with a date older than the hot window
# ---------------------------------------------------------------------------
def archive_inbox(brief_dir: Path, now: datetime, dry_run: bool) -> int:
    src = brief_dir / "inbox.md"
    if not src.exists():
        return 0
    cutoff = now - timedelta(days=INBOX_HOT_DAYS)
    lines = src.read_text().splitlines()
    kept: list[str] = []
    moved: list[str] = []
    for line in lines:
        m = INBOX_ITEM_RE.match(line)
        if m and (d := parse_date(m.group(1))) and d < cutoff:
            moved.append(line)
        else:
            kept.append(line)
    if not moved:
        return 0
    if not dry_run:
        append_archive(brief_dir / "inbox-archive.md", now.strftime("%Y-%m-%d"), moved)
        src.write_text("\n".join(kept).rstrip() + "\n")
    return len(moved)


# ---------------------------------------------------------------------------
# OpenClaw agent sessions: trajectory logs grow unbounded and are NOT covered by
# cron.sessionRetention (that prunes isolated cron RUN sessions only). Trajectory
# files are completed-run audit traces — safe to gzip once old, delete once cold.
# Conversation session .jsonl are left ALONE here (OpenClaw owns resume + native
# retention); we only REPORT their size so growth is visible, never silent.
# ---------------------------------------------------------------------------
def _age_days(path: Path, now: datetime) -> float:
    return (now.timestamp() - path.stat().st_mtime) / 86400


def rotate_trajectories(agents_dir: Path, now: datetime, dry_run: bool) -> dict:
    if not agents_dir or not agents_dir.exists():
        return {"trajectories_gzipped": 0, "trajectory_archives_deleted": 0}
    gzipped = deleted = 0
    for traj in agents_dir.glob("*/sessions/*.trajectory.jsonl"):
        if _age_days(traj, now) > TRAJECTORY_GZIP_DAYS:
            if not dry_run:
                with traj.open("rb") as fin, gzip.open(f"{traj}.gz", "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                traj.unlink()
            gzipped += 1
    for arch in agents_dir.glob("*/sessions/*.trajectory.jsonl.gz"):
        if _age_days(arch, now) > TRAJECTORY_DELETE_DAYS:
            if not dry_run:
                arch.unlink()
            deleted += 1
    return {"trajectories_gzipped": gzipped, "trajectory_archives_deleted": deleted}


def session_footprint(agents_dir: Path) -> dict:
    """Report-only: size/count of session + trajectory artifacts so growth is visible."""
    if not agents_dir or not agents_dir.exists():
        return {}
    sessions = [
        f for f in agents_dir.glob("*/sessions/*.jsonl") if ".trajectory." not in f.name
    ]
    trajectories = list(agents_dir.glob("*/sessions/*.trajectory.jsonl*"))
    return {
        "session_files": len(sessions),
        "session_mb": round(sum(f.stat().st_size for f in sessions) / 1e6, 1),
        "trajectory_files": len(trajectories),
        "trajectory_mb": round(sum(f.stat().st_size for f in trajectories) / 1e6, 1),
    }


# ---------------------------------------------------------------------------
# bookkeeping
# ---------------------------------------------------------------------------
def write_heartbeat(state_dir: Path, now: datetime, dry_run: bool) -> None:
    if dry_run:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "heartbeat.txt").write_text(now.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")


def log_run(state_dir: Path, now: datetime, summary: dict, dry_run: bool) -> None:
    if dry_run:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), **summary}
    with (state_dir / "run-log.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--brief-dir",
        type=Path,
        required=True,
        help="morning-brief inputs dir (e.g. /var/lib/lifekit/morning-brief)",
    )
    ap.add_argument(
        "--state-dir",
        type=Path,
        required=True,
        help="runtime-state dir for heartbeat + run-log (e.g. /var/lib/lifekit/rotation)",
    )
    ap.add_argument(
        "--agents-dir",
        type=Path,
        default=None,
        help="OpenClaw agents dir (e.g. /srv/openclaw/config/agents). When set, gzip/prune "
        "old *.trajectory.jsonl and report session footprint. Skipped if unset.",
    )
    ap.add_argument("--now", help="override 'now' as YYYY-MM-DD (testing)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would move; touch nothing",
    )
    args = ap.parse_args()
    now = parse_date(args.now) if args.now else datetime.now()
    if now is None:
        print(f"bad --now: {args.now}", file=sys.stderr)
        return 2
    summary = {
        "inbox_moved": archive_inbox(args.brief_dir, now, args.dry_run),
        **rotate_trajectories(args.agents_dir, now, args.dry_run),
        **session_footprint(args.agents_dir),
        "dry_run": args.dry_run,
    }
    write_heartbeat(args.state_dir, now, args.dry_run)
    log_run(args.state_dir, now, summary, args.dry_run)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
