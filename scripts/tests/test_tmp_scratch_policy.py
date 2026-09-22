"""Behaviour of scripts/tmp-scratch-policy.sh, run as a subprocess against temp paths.

The sweep reads a process table from PROC_ROOT; tests point it at a directory
of symlinks to the real /proc entries of processes they start, so "held open
by a live process" is a real process holding a real file.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/tmp-scratch-policy.sh"


def run(*args, env=None):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def procs(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    started = []

    def hold(**popen_kwargs):
        p = subprocess.Popen(["sleep", "60"], **popen_kwargs)
        started.append(p)
        (root / str(p.pid)).symlink_to(f"/proc/{p.pid}")
        return p

    hold.root = root
    yield hold
    for p in started:
        p.kill()
        p.wait()


def scratch_tree(tmp_path):
    scratch = tmp_path / "scratch"
    for name in ("abandoned", "open-file", "cwd"):
        (scratch / name).mkdir(parents=True)
        (scratch / name / "data").write_text("x")
    return scratch


def test_sweep_keeps_scratch_a_live_process_holds_and_removes_the_rest(
    tmp_path, procs
):
    scratch = scratch_tree(tmp_path)
    with open(scratch / "open-file" / "data") as held:
        holder = procs(stdin=held)
    procs(cwd=scratch / "cwd")
    env = {
        "SCRATCH_ROOT": str(scratch),
        "PROC_ROOT": str(procs.root),
        "TMP_SCRATCH_AGE_DAYS": "0",
    }

    result = run("--sweep", env=env)

    assert result.returncode == 0, result.stderr
    assert not (scratch / "abandoned").exists()
    assert (scratch / "open-file" / "data").exists()
    assert (scratch / "cwd").exists()

    holder.kill()
    holder.wait()
    assert run("--sweep", env=env).returncode == 0
    assert not (scratch / "open-file").exists()
    assert (scratch / "cwd").exists()


def test_sweep_keeps_recently_written_scratch(tmp_path, procs):
    scratch = scratch_tree(tmp_path)

    env = {"SCRATCH_ROOT": str(scratch), "PROC_ROOT": str(procs.root)}
    result = run("--sweep", env=env)

    assert result.returncode == 0, result.stderr
    assert (scratch / "abandoned").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any fd table")
def test_sweep_removes_nothing_when_a_process_table_is_unreadable(tmp_path, procs):
    scratch = scratch_tree(tmp_path)
    unreadable = procs.root / "1" / "fd"
    unreadable.mkdir(parents=True)
    unreadable.chmod(0)
    try:
        env = {
            "SCRATCH_ROOT": str(scratch),
            "PROC_ROOT": str(procs.root),
            "TMP_SCRATCH_AGE_DAYS": "0",
        }
        result = run("--sweep", env=env)
    finally:
        unreadable.chmod(0o700)

    assert result.returncode == 2
    assert (scratch / "abandoned").exists()


@pytest.fixture
def converged(tmp_path):
    """Apply the policy to temp paths with systemctl stubbed as enabled + masked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "is-enabled tmp.mount") echo masked ;;\n'
        '  "is-enabled lifekit-tmp-scratch-sweep.timer") echo enabled ;;\n'
        "esac\n"
    )
    stub.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMPFILES_DROPIN": str(tmp_path / "tmpfiles.d/lifekit-tmp-scratch.conf"),
        "SWEEP_UNIT_DIR": str(tmp_path / "systemd"),
        "SWEEP_BIN": str(tmp_path / "bin-root/lifekit-tmp-scratch-sweep.sh"),
    }
    applied = run(env={**env, "SCRATCH_ROOT": str(REPO)})
    assert applied.returncode == 0, applied.stderr
    return env


def fstype(path):
    out = subprocess.run(
        ["findmnt", "-no", "FSTYPE", "-T", str(path)], capture_output=True, text=True
    )
    return out.stdout.strip()


def test_check_passes_on_disk_backed_directory_that_is_not_a_mountpoint(converged):
    target = REPO / "scripts"
    if fstype(target) in ("", "tmpfs"):
        pytest.skip("repository checkout is not on a disk filesystem")

    result = run("--check", env={**converged, "SCRATCH_ROOT": str(target)})

    assert result.returncode == 0, result.stdout
    assert f"live filesystem: {fstype(target)}" in result.stdout


def test_check_fails_while_scratch_is_still_live_on_tmpfs(converged):
    if fstype("/dev/shm") != "tmpfs":
        pytest.skip("no tmpfs mount to point at")

    result = run("--check", env={**converged, "SCRATCH_ROOT": "/dev/shm"})

    assert result.returncode == 1, result.stdout


def test_check_is_undetermined_only_when_nothing_else_is_wrong(converged, tmp_path):
    missing = str(tmp_path / "does-not-exist")

    assert run("--check", env={**converged, "SCRATCH_ROOT": missing}).returncode == 2

    Path(converged["TMPFILES_DROPIN"]).write_text("q /tmp 1777 root root 1d\n")
    assert run("--check", env={**converged, "SCRATCH_ROOT": missing}).returncode == 1


def test_sweep_keeps_a_held_entry_too_large_to_scan_in_one_pipe_buffer(
    tmp_path, procs
):
    scratch = tmp_path / "scratch"
    big = scratch / "big"
    (big / "files").mkdir(parents=True)
    for i in range(10000):
        (big / "files" / str(i)).touch()
    procs(cwd=big)
    env = {
        "SCRATCH_ROOT": str(scratch),
        "PROC_ROOT": str(procs.root),
        "TMP_SCRATCH_AGE_DAYS": "0",
    }

    result = run("--sweep", env=env)

    assert result.returncode == 0, result.stderr
    assert big.exists()


def test_apply_installs_a_runnable_sweep_copy_that_check_tracks(converged, tmp_path):
    sweep_bin = Path(converged["SWEEP_BIN"])
    target = REPO / "scripts"
    if fstype(target) in ("", "tmpfs"):
        pytest.skip("repository checkout is not on a disk filesystem")
    env = {**converged, "SCRATCH_ROOT": str(target)}

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    swept = subprocess.run(
        [str(sweep_bin), "--sweep"],
        env={**os.environ, "SCRATCH_ROOT": str(scratch), "PROC_ROOT": str(scratch)},
        capture_output=True,
        text=True,
    )
    assert swept.returncode == 0, swept.stderr

    sweep_bin.write_text("#!/bin/sh\nexit 0\n")
    assert run("--check", env=env).returncode == 1

    sweep_bin.unlink()
    assert run("--check", env=env).returncode == 1

    assert run(env=env).returncode == 0
    assert run("--check", env=env).returncode == 0
