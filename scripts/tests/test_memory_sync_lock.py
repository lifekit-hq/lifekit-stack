"""memory-sync.sh holds the vault's flock and records git's error in .sync-failed."""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/sync/memory-sync.sh"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_vault(tmp_path):
    origin = tmp_path / "origin.git"
    vault = tmp_path / "vault"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-b", "main", str(vault))
    git(vault, "remote", "add", "origin", str(origin))
    (vault / "a.md").write_text("a\n")
    git(vault, "add", "-A")
    git(vault, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    git(vault, "push", "-q", "origin", "main")
    return vault


def tick(vault, tmp_path, **env):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "MEMORY_SYNC_DIR": str(vault),
            "MEMORY_SYNC_HOME": str(tmp_path),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_tick_succeeds_and_leaves_no_marker(tmp_path):
    vault = make_vault(tmp_path)
    (vault / "b.md").write_text("b\n")
    r = tick(vault, tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (vault / ".sync-failed").exists()


def test_tick_waits_for_held_lock_then_proceeds(tmp_path):
    vault = make_vault(tmp_path)
    (vault / "b.md").write_text("b\n")
    lock = vault / ".git" / "vault.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    start = time.monotonic()
    proc = subprocess.Popen(
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "MEMORY_SYNC_DIR": str(vault),
            "MEMORY_SYNC_HOME": str(tmp_path),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.5)
    assert proc.poll() is None, "tick must wait while the lock is held"
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert time.monotonic() - start >= 1.5
    assert not (vault / ".sync-failed").exists()


def test_lock_timeout_fails_with_step_lock(tmp_path):
    vault = make_vault(tmp_path)
    fd = os.open(vault / ".git" / "vault.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = tick(vault, tmp_path, MEMORY_SYNC_LOCK_WAIT="1")
    finally:
        os.close(fd)
    assert r.returncode == 1
    first, *rest = (vault / ".sync-failed").read_text().splitlines()
    assert " step=lock exit=1" in first
    assert any("busy" in line for line in rest)


def test_failure_marker_carries_redacted_git_error(tmp_path):
    vault = make_vault(tmp_path)
    git(vault, "remote", "set-url", "origin", "https://user:s3cret@127.0.0.1:1/x.git")
    r = tick(vault, tmp_path)
    assert r.returncode != 0
    lines = (vault / ".sync-failed").read_text().splitlines()
    assert " step=fetch exit=" in lines[0]
    assert 1 < len(lines) <= 6
    assert "s3cret" not in "\n".join(lines)
