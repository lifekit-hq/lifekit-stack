"""Behaviour of scripts/rotate/rotate-state.py against temp state dirs."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/rotate/rotate-state.py"


def run(tmp_path, *extra):
    return subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--brief-dir",
            str(tmp_path / "brief"),
            "--state-dir",
            str(tmp_path / "rotation"),
            "--agents-dir",
            str(tmp_path / "agents"),
            "--now",
            "2026-11-01",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_inbox_items_past_30_days_move_to_the_archive(tmp_path):
    brief = tmp_path / "brief"
    brief.mkdir()
    (brief / "inbox.md").write_text(
        "# Inbox\n\n- 2026-09-01 — old note\n- 2026-10-20 — fresh note\nloose line\n"
    )
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["inbox_moved"] == 1
    inbox = (brief / "inbox.md").read_text()
    assert "old note" not in inbox and "fresh note" in inbox and "loose line" in inbox
    archive = (brief / "inbox-archive.md").read_text()
    assert "<!-- rotated 2026-11-01 -->" in archive and "old note" in archive
    assert "rotate-state.py" in archive
    assert json.loads(run(tmp_path).stdout)["inbox_moved"] == 0


def test_dry_run_touches_nothing(tmp_path):
    brief = tmp_path / "brief"
    brief.mkdir()
    (brief / "inbox.md").write_text("- 2026-01-01 — old\n")
    result = run(tmp_path, "--dry-run")
    assert json.loads(result.stdout)["inbox_moved"] == 1
    assert (brief / "inbox.md").read_text() == "- 2026-01-01 — old\n"
    assert not (tmp_path / "rotation").exists()


def test_trajectories_gzip_then_prune_and_bookkeeping(tmp_path):
    sessions = tmp_path / "agents/kit/sessions"
    sessions.mkdir(parents=True)
    old = sessions / "a.trajectory.jsonl"
    old.write_text('{"x": 1}\n')
    cold = sessions / "b.trajectory.jsonl.gz"
    cold.write_bytes(gzip.compress(b"{}\n"))
    now = time.mktime(time.strptime("2026-11-01", "%Y-%m-%d"))
    os.utime(old, (now - 10 * 86400, now - 10 * 86400))
    os.utime(cold, (now - 90 * 86400, now - 90 * 86400))
    result = run(tmp_path)
    summary = json.loads(result.stdout)
    assert summary["trajectories_gzipped"] == 1
    assert summary["trajectory_archives_deleted"] == 1
    assert (
        gzip.decompress((sessions / "a.trajectory.jsonl.gz").read_bytes())
        == b'{"x": 1}\n'
    )
    assert not old.exists() and not cold.exists()
    assert (tmp_path / "rotation/heartbeat.txt").read_text() == "2026-11-01T00:00:00Z\n"
    assert "proposals_moved" not in summary
