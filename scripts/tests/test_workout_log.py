"""The workout-log skill's gym-day nudge and its ensure script
(docs/runbook.md, "Kit's workout log and gym-day nudge").

workout-claw is replaced by a stub that prints what the real 0.4.0 `summary`
prints, and docker by a stub that reports no running gateway, so nothing here
touches live data or the gateway.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills/workout-log"
NUDGE = SKILL / "nudge.sh"
ENSURE = REPO / "scripts/ensure-workout-log.sh"

EMPTY = "date: 2026-10-20\nsessions: 0\nmessage: no workouts logged\n"
LOGGED = """date: 2026-10-20
sessions:
  - id: 0Q0YIEcU
    time: 11:34
    muscle_group: chest
    total_sets: 3
    total_volume_kg: 1320
    exercises:
      - name: bench-press
        muscle: chest
        total_sets: 3
        total_reps: 22
        total_volume_kg: 1320
        sets:
          - reps: 8
            weight_kg: 60
"""
CORRUPT = "error: corrupt JSON in /home/node/.workout-claw/logs/2026-10-20.json (Unexpected token)\n"


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def nudge(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    out = tmp_path / "out.txt"

    def run(summary: str, rc: int = 0):
        out.write_text(summary)
        _stub(bindir, "workout-claw", f'cat "{out}"\nexit {rc}')
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)}
        return subprocess.run(
            ["sh", str(NUDGE)], env=env, capture_output=True, text=True, check=False
        )

    return run


def test_nothing_logged_sends_one_nudge_line(nudge):
    r = nudge(EMPTY)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1 and "no workout logged" in lines[0]
    assert "NO_REPLY" not in r.stdout


def test_a_logged_session_stays_silent(nudge):
    r = nudge(LOGGED)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "NO_REPLY"


def test_a_workout_claw_error_fails_the_run_instead_of_nudging(nudge):
    r = nudge(CORRUPT, rc=1)
    assert r.returncode == 1
    assert r.stdout == ""
    assert "corrupt JSON" in r.stderr


def test_a_missing_cli_fails_the_run(nudge):
    r = nudge("sh: 1: workout-claw: not found\n", rc=127)
    assert r.returncode == 1
    assert r.stdout == ""
    assert "not found" in r.stderr


def test_unrecognised_summary_output_fails_the_run(nudge):
    r = nudge('{"date":"2026-10-20","sessions":[]}\n')
    assert r.returncode == 1
    assert r.stdout == ""
    assert "unrecognised" in r.stderr


def test_ensure_installs_the_skill_and_converges(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "docker", "exit 0")  # no running gateway
    workspace = tmp_path / "config/agents/kit/workspace"
    workspace.mkdir(parents=True)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "OPENCLAW_CONFIG_DIR": str(tmp_path / "config"),
    }
    for _ in range(2):
        r = subprocess.run(
            ["bash", str(ENSURE)], env=env, capture_output=True, text=True, check=False
        )
        assert r.returncode == 0, r.stderr
        assert "not checked" in r.stdout
    dest = workspace / "skills/workout-log"
    for f in ("SKILL.md", "nudge.sh", "day-check.sh"):
        assert (dest / f).read_bytes() == (SKILL / f).read_bytes()
    assert os.access(dest / "nudge.sh", os.X_OK)
    assert os.access(dest / "day-check.sh", os.X_OK)


def test_ensure_refuses_an_agent_without_a_workspace(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "OPENCLAW_CONFIG_DIR": str(tmp_path)}
    r = subprocess.run(
        ["bash", str(ENSURE)], env=env, capture_output=True, text=True, check=False
    )
    assert r.returncode == 1
    assert "no workspace" in r.stderr
