"""scripts/admin-group/ensure-admin-group.sh against fake id/usermod."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/admin-group/ensure-admin-group.sh"

FAKE_ID = """#!/usr/bin/env bash
[[ "$1" == -nG ]] && cat "$STATE_DIR/groups"
"""
FAKE_USERMOD = """#!/usr/bin/env bash
echo "$*" >> "$STATE_DIR/usermod"
[[ "$1" == -aG ]] && printf ' %s' "$2" >> "$STATE_DIR/groups"
"""


@pytest.fixture
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("id", FAKE_ID), ("usermod", FAKE_USERMOD)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return tmp_path


def run(host, groups, *args):
    (host / "groups").write_text(groups)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={
            **os.environ,
            "PATH": f"{host / 'bin'}:{os.environ['PATH']}",
            "STATE_DIR": str(host),
            "ADMIN_USER": "admin",
            "LIFEKIT_GROUP": "lifekit",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_fails_when_admin_not_in_group(host):
    r = run(host, "admin sudo lifekit-ops", "--check")
    assert r.returncode == 1
    assert "usermod -aG lifekit admin" in r.stderr
    assert not (host / "usermod").exists()


def test_check_passes_when_admin_in_group(host):
    r = run(host, "admin sudo lifekit", "--check")
    assert r.returncode == 0, r.stderr


def test_ensure_adds_missing_membership(host):
    r = run(host, "admin sudo")
    assert r.returncode == 0, r.stderr
    assert (host / "usermod").read_text() == "-aG lifekit admin\n"


def test_ensure_leaves_existing_membership(host):
    assert run(host, "admin lifekit").returncode == 0
    assert not (host / "usermod").exists()


def test_ensure_fails_when_membership_does_not_take(host):
    (host / "bin/usermod").write_text("#!/usr/bin/env bash\nexit 0\n")
    r = run(host, "admin")
    assert r.returncode == 1
    assert "not in the lifekit group" in r.stderr

