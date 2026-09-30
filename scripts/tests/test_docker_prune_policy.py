"""Behaviour of scripts/docker-prune-policy.sh, run as a subprocess against temp paths."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/docker-prune-policy.sh"


@pytest.fixture
def host(tmp_path):
    """Temp install paths plus stubbed systemctl and docker on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    (bin_dir / "systemctl").write_text(
        f'#!/bin/sh\necho "systemctl $*" >> {log}\n'
        'if [ "$1" = is-enabled ]; then cat "$STATE_DIR/enabled" 2>/dev/null || echo disabled; fi\n'
        'if [ "$1" = enable ]; then echo enabled > "$STATE_DIR/enabled"; fi\n'
    )
    (bin_dir / "docker").write_text(
        f'#!/bin/sh\necho "docker $*" >> {log}\n'
        '[ "$1" != "$DOCKER_FAIL" ] || { echo boom >&2; exit 1; }\n'
        'echo "Total reclaimed space: 1GB"\n'
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_DIR": str(state),
        "PRUNE_UNIT_DIR": str(tmp_path / "units"),
        "PRUNE_BIN": str(tmp_path / "usr" / "lifekit-docker-prune.sh"),
    }

    def run(*args, **extra):
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env={**env, **extra},
            capture_output=True,
            text=True,
            check=False,
        )

    run.log = log
    run.tmp = tmp_path
    return run


def test_prune_runs_image_then_builder_with_the_age_floor_and_logs_reclaimed(host):
    result = host("--prune")

    assert result.returncode == 0, result.stderr
    calls = host.log.read_text().splitlines()
    assert calls == [
        "docker image prune -af --filter until=336h",
        "docker builder prune -af --filter until=336h",
    ]
    assert "  [image] Total reclaimed space: 1GB" in result.stdout
    assert "[builder]" in result.stdout


def test_prune_attempts_both_and_fails_when_one_fails(host):
    result = host("--prune", DOCKER_FAIL="image")

    assert result.returncode == 1
    assert "docker builder prune" in host.log.read_text()


def test_apply_installs_units_then_check_passes(host):
    assert host("--check").returncode == 1

    result = host()
    assert result.returncode == 0, result.stderr

    timer = host.tmp / "units" / "lifekit-docker-prune.timer"
    assert "OnCalendar=*-*-* 03:30:00" in timer.read_text()
    installed = host.tmp / "usr" / "lifekit-docker-prune.sh"
    assert os.access(installed, os.X_OK)
    assert "systemctl enable --now lifekit-docker-prune.timer" in host.log.read_text()
    assert host("--check").returncode == 0


def test_check_flags_a_stale_installed_copy(host):
    host()
    (host.tmp / "usr" / "lifekit-docker-prune.sh").write_text("#!/bin/sh\n")

    result = host("--check")

    assert result.returncode == 1
    assert "does not match the repository content" in result.stdout
