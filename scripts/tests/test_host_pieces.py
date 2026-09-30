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
