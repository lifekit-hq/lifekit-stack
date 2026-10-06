"""media-prune.sh behaviour and the host-piece unit/installer wiring."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PRUNE = REPO / "scripts/media-prune/media-prune.sh"
PIECES = {
    "media-prune": REPO / "scripts/media-prune",
    "alert-inbox": REPO / "scripts/alert-inbox",
    "openclaw-config-sync": REPO / "scripts/sync",
    "lifekit-fleet-publisher": REPO / "scripts/fleet-publisher",
    "lifekit-identity-backup": REPO / "scripts/identity-backup",
}


def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def prune(media_dir):
    return subprocess.run(
        ["bash", str(PRUNE)],
        env={**os.environ, "MEDIA_DIR": str(media_dir)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_prune_removes_old_files_and_empty_dirs_only(tmp_path):
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old = old_dir / "voice.ogg"
    old.write_text("x")
    age(old, 8)
    keep_dir = tmp_path / "new"
    keep_dir.mkdir()
    fresh = keep_dir / "photo.jpg"
    fresh.write_text("x")
    age(fresh, 6)
    result = prune(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not old.exists()
    assert not old_dir.exists()
    assert fresh.exists()
    assert tmp_path.exists()


def test_prune_missing_dir_is_a_noop(tmp_path):
    assert prune(tmp_path / "absent").returncode == 0


@pytest.mark.parametrize("name", sorted(PIECES))
def test_unit_files_and_installer_exist(name):
    d = PIECES[name]
    for ext in ("service", "timer"):
        assert (d / f"{name}.{ext}").is_file()
    installer = next(d.glob("install-*.sh"))
    assert os.access(installer, os.X_OK)


@pytest.mark.parametrize("name", sorted(PIECES))
def test_bootstrap_calls_installer(name):
    installer = next(PIECES[name].glob("install-*.sh")).name
    assert installer in (REPO / "scripts/bootstrap-vps.sh").read_text()


def test_alert_inbox_unit_hardcodes_no_personal_paths():
    unit = (REPO / "scripts/alert-inbox/alert-inbox.service").read_text()
    assert "__ALERT_INBOX_USER__" in unit and "__REPO_DIR__" in unit
    assert not re.search(r"/home/\w", unit)


def test_fleet_publisher_unit_hardcodes_no_personal_paths():
    unit = (
        REPO / "scripts/fleet-publisher/lifekit-fleet-publisher.service"
    ).read_text()
    assert "__ADMIN_USER__" in unit and '"FM_HOMES=__FM_HOMES__"' in unit
    assert not re.search(r"/home/\w", unit)


def test_bootstrap_hands_the_fleet_home_list_to_the_installer():
    line = next(
        ln
        for ln in (REPO / "scripts/bootstrap-vps.sh").read_text().splitlines()
        if "install-fleet-publisher.sh" in ln and not ln.startswith("#")
    )
    assert 'FM_HOMES="${FM_HOMES:-}"' in line and 'FM_HOME="${FM_HOME:-}"' in line


RUNNER_RESTART = REPO / "scripts/runner-restart/install-runner-restart.sh"
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "$*" >> "$STATE_DIR/calls"
if [[ "$1" == list-unit-files ]]; then
  cat "$STATE_DIR/files"
fi
"""


def _run_runner_restart(tmp_path, listed):
    state, units = tmp_path / "state", tmp_path / "units"
    state.mkdir(exist_ok=True)
    units.mkdir(exist_ok=True)
    fake = tmp_path / "systemctl"
    fake.write_text(FAKE_SYSTEMCTL)
    fake.chmod(0o755)
    (state / "files").write_text("".join(f"{u} enabled\n" for u in listed))
    subprocess.run(
        ["bash", str(RUNNER_RESTART)],
        env={
            **os.environ,
            "SYSTEMCTL": str(fake),
            "STATE_DIR": str(state),
            "UNIT_DIR": str(units),
        },
        check=True,
        capture_output=True,
    )
    return units, state


def test_runner_restart_writes_one_dropin_per_runner_unit(tmp_path):
    runners = ["actions.runner.o-r.host-a.service", "actions.runner.o-r.host-b.service"]
    units, state = _run_runner_restart(tmp_path, [*runners, "sshd.service"])
    assert sorted(p.name for p in units.iterdir()) == [f"{r}.d" for r in runners]
    for r in runners:
        assert (units / f"{r}.d/restart.conf").read_text() == (
            "[Service]\nRestart=on-failure\nRestartSec=10\n"
        )
    assert "daemon-reload" in (state / "calls").read_text().splitlines()


def test_runner_restart_is_idempotent_and_handles_no_runners(tmp_path):
    runner = "actions.runner.o-r.host.service"
    units, _ = _run_runner_restart(tmp_path, [runner])
    first = (units / f"{runner}.d/restart.conf").read_text()
    units, _ = _run_runner_restart(tmp_path, [runner])
    assert (units / f"{runner}.d/restart.conf").read_text() == first
    empty = tmp_path / "empty"
    empty.mkdir()
    units, state = _run_runner_restart(empty, [])
    assert list(units.iterdir()) == []
    assert "daemon-reload" in (state / "calls").read_text()
