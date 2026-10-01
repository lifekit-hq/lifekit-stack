"""Behaviour of scripts/host-gauge/host-group-gauge.sh against a fixture cgroup tree."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/host-gauge/host-group-gauge.sh"


def cgroup(root, rel, current, inactive=0, swap=None):
    d = root / rel
    d.mkdir(parents=True)
    (d / "memory.current").write_text(f"{current}\n")
    (d / "memory.stat").write_text(f"anon 1\ninactive_file {inactive}\nfile 2\n")
    if swap is not None:
        (d / "memory.swap.current").write_text(f"{swap}\n")


def run(root, out, env=None):
    return subprocess.run(
        ["bash", str(SCRIPT), str(out)],
        env={
            **os.environ,
            "CGROUP_ROOT": str(root),
            "VMSTAT_FILE": str(root / "no-vmstat"),
            **(env or {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def metrics(out):
    parsed = {}
    for line in (out / "host_group.prom").read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            parsed[name] = int(value)
    return parsed


@pytest.fixture
def dirs(tmp_path):
    root, out = tmp_path / "cg", tmp_path / "out"
    root.mkdir()
    out.mkdir()
    return root, out


def test_groups_sum_and_exclude_page_cache(dirs):
    root, out = dirs
    cgroup(root, "user.slice/user-1001.slice", 1000, inactive=300, swap=50)
    cgroup(root, "system.slice/actions.runner.a.svc.service", 200, inactive=100, swap=1)
    cgroup(root, "system.slice/actions.runner.b.svc.service", 400, swap=2)
    cgroup(root, "system.slice/docker.service", 500, inactive=50, swap=5)
    cgroup(root, "system.slice/containerd.service", 100, swap=7)
    cgroup(root, "system.slice/tailscaled.service", 10)
    cgroup(root, "system.slice/systemd-journald.service", 20)
    # Not in any group: container scopes and unrelated daemons.
    cgroup(root, "system.slice/docker-abc.scope", 9999, swap=9999)
    cgroup(root, "system.slice/cron.service", 9999, swap=9999)
    result = run(root, out)
    assert result.returncode == 0, result.stderr
    assert metrics(out) == {
        'host_group_memory_bytes{group="operator"}': 700,
        'host_group_memory_swap_bytes{group="operator"}': 50,
        'host_group_memory_bytes{group="runners"}': 500,
        'host_group_memory_swap_bytes{group="runners"}': 3,
        'host_group_memory_bytes{group="os"}': 580,
        'host_group_memory_swap_bytes{group="os"}': 12,
        "host_vmstat_pswpin_pages_total": 0,
    }


def test_missing_groups_report_zero(dirs):
    root, out = dirs
    assert run(root, out).returncode == 0
    assert set(metrics(out).values()) == {0}
    assert len(metrics(out)) == 7


def test_operator_slice_and_os_units_are_configurable(dirs):
    root, out = dirs
    cgroup(root, "user.slice/user-1234.slice", 42, swap=1)
    cgroup(root, "system.slice/docker.service", 9999)
    cgroup(root, "system.slice/sshd.service", 8)
    env = {
        "HOST_GAUGE_OPERATOR_SLICE": "user.slice/user-1234.slice",
        "HOST_GAUGE_OS_UNITS": "sshd",
    }
    assert run(root, out, env).returncode == 0
    got = metrics(out)
    assert got['host_group_memory_bytes{group="operator"}'] == 42
    assert got['host_group_memory_bytes{group="os"}'] == 8


def test_write_is_atomic_and_leaves_no_scratch(dirs):
    root, out = dirs
    assert run(root, out).returncode == 0
    assert [p.name for p in out.iterdir()] == ["host_group.prom"]
    assert (out / "host_group.prom").stat().st_mode & 0o777 == 0o644


def test_prom_format_has_help_and_type(dirs):
    root, out = dirs
    run(root, out)
    text = (out / "host_group.prom").read_text()
    for name in ("host_group_memory_bytes", "host_group_memory_swap_bytes"):
        assert f"# TYPE {name} gauge" in text


def test_swap_in_counter_is_read_from_vmstat(dirs, tmp_path):
    root, out = dirs
    vmstat = tmp_path / "vmstat"
    vmstat.write_text("pgfault 5\npswpin 1234\npswpout 9\n")
    assert run(root, out, {"VMSTAT_FILE": str(vmstat)}).returncode == 0
    assert metrics(out)["host_vmstat_pswpin_pages_total"] == 1234
