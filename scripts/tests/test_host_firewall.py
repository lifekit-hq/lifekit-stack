"""The host firewall baseline: scripts/firewall/lifekit-firewall.nft and scripts/host-firewall.sh.

Two layers. The ruleset itself is loaded with the real nft into a throwaway
user + network namespace (unprivileged, nothing on the host is touched) and
probed with real traffic by firewall-netns-harness.sh; those tests skip where
the runner has no nft or no unprivileged user namespaces. The script's modes
run as a subprocess against temp paths with nft, systemctl, systemd-run, who
and iptables stubbed on PATH, the same shape as test_docker_prune_policy.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RULESET = REPO / "scripts/firewall/lifekit-firewall.nft"
SCRIPT = REPO / "scripts/host-firewall.sh"
HARNESS = REPO / "scripts/tests/firewall-netns-harness.sh"

SBIN_PATH = f"{os.environ.get('PATH', '')}:/usr/sbin:/sbin"
NFT = shutil.which("nft", path=SBIN_PATH)


def _userns_ok() -> bool:
    if NFT is None or shutil.which("unshare") is None:
        return False
    probe = subprocess.run(
        ["unshare", "-rnpf", "--mount-proc", NFT, "list", "ruleset"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


needs_netns = pytest.mark.skipif(
    not _userns_ok(), reason="needs nft and unprivileged user/net/pid namespaces"
)


def in_netns(*cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["unshare", "-rnpf", "--mount-proc", *cmd],
        env={**os.environ, "PATH": SBIN_PATH},
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


# ─── The ruleset file ─────────────────────────────────────────────────────────


@needs_netns
def test_ruleset_passes_nft_check():
    result = in_netns(NFT, "-c", "-f", str(RULESET))

    assert result.returncode == 0, result.stderr


@needs_netns
def test_ruleset_under_real_traffic():
    result = in_netns("bash", str(HARNESS), str(RULESET))

    assert result.returncode == 0, result.stderr
    probes = dict(line.split("=", 1) for line in result.stdout.split())
    assert probes == {
        # SSH and every other host port: the tailnet only.
        "tailnet_ssh": "ok",
        "public_ssh": "blocked",
        "tailnet_other_port": "ok",
        "public_other_port": "blocked",
        # The public openings: the edge ports and Tailscale's WireGuard port.
        "public_http": "ok",
        "public_wireguard": "ok",
        "public_other_udp": "blocked",
        # Published container ports: loopback and the tailnet, never public.
        "tailnet_published_port": "ok",
        "public_published_port": "blocked",
        "public_routed_to_container": "blocked",
        # The edge bridge takes public 443 as dialled on the host, nothing else.
        "public_edge_https": "ok",
        "public_edge_other_port": "blocked",
        # Containers keep egress, host services, and the edge its backends.
        "container_egress": "ok",
        "container_to_host": "ok",
        "edge_to_container": "ok",
        "host_loopback": "ok",
        # Docker's own tables come through two loads unchanged.
        "foreign_tables": "intact",
    }


# ─── The script ───────────────────────────────────────────────────────────────


@pytest.fixture
def host(tmp_path):
    """Temp install paths plus stubbed nft, systemctl, systemd-run, who and iptables."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "calls.log"
    stubs = {
        "nft": (
            f'echo "nft $*" >> {log}\n'
            'case "$*" in\n'
            '  "list ruleset") echo "table ip filter {}" ;;\n'
            '  "-s list table inet lifekit") [ -f "$STATE_DIR/loaded" ] || exit 1; cat "$STATE_DIR/loaded" ;;\n'
            '  "-f "*) [ -z "$NFT_FAIL_LOAD" ] || exit 1 ;;\n'
            "esac\n"
        ),
        "systemctl": (
            f'echo "systemctl $*" >> {log}\n'
            'case "$1" in\n'
            '  is-enabled) cat "$STATE_DIR/enabled-$2" 2>/dev/null || echo disabled ;;\n'
            '  is-active) cat "$STATE_DIR/active-$2" 2>/dev/null || echo inactive ;;\n'
            '  enable) echo enabled > "$STATE_DIR/enabled-$2" ;;\n'
            '  reload-or-restart) echo active > "$STATE_DIR/active-$2" ;;\n'
            '  stop) rm -f "$STATE_DIR/active-$2" ;;\n'
            "esac\n"
        ),
        "systemd-run": (
            f'echo "systemd-run $*" >> {log}\n'
            'echo active > "$STATE_DIR/active-lifekit-firewall-rollback.timer"\n'
        ),
        "who": 'echo "admin    pts/0        2026-10-02 10:00 ($WHO_FROM)"\n',
        "iptables": f'echo "iptables $*" >> {log}\n',
        "ip6tables": f'echo "ip6tables $*" >> {log}\n',
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}")
        stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_DIR": str(state),
        "NFT_BIN": "nft",
        "WHO_FROM": "100.101.102.103",
        "FIREWALL_RULESET": str(tmp_path / "etc/lifekit/firewall.nft"),
        "FIREWALL_UNIT_DIR": str(tmp_path / "units"),
        "FIREWALL_STATE_DIR": str(tmp_path / "fwstate"),
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
    run.state = state
    run.calls = lambda: log.read_text().splitlines() if log.exists() else []
    return run


def test_install_writes_ruleset_and_unit_then_check_passes(host):
    assert host("--check").returncode == 1

    result = host()

    assert result.returncode == 0, result.stderr
    assert (host.tmp / "etc/lifekit/firewall.nft").read_text() == RULESET.read_text()
    unit = (host.tmp / "units/lifekit-firewall.service").read_text()
    assert f"ExecStart=nft -f {host.tmp}/etc/lifekit/firewall.nft" in unit
    assert "ExecStop=-nft delete table inet lifekit" in unit
    assert "flush" not in unit.split("[Unit]", 1)[1]
    assert "WantedBy=sysinit.target" in unit
    calls = host.calls()
    assert calls.index("systemctl daemon-reload") < calls.index(
        "systemctl enable lifekit-firewall.service"
    )
    assert calls[-1] == "systemctl reload-or-restart lifekit-firewall.service"
    check = host("--check")
    assert check.returncode == 0, check.stdout


def test_install_is_idempotent(host):
    host()
    host.log.unlink()

    result = host()

    assert result.returncode == 0, result.stderr
    assert "systemctl daemon-reload" not in host.calls()
    assert result.stdout.count("already matches") == 2


def test_check_flags_drift_and_conflicting_firewalls(host):
    host()
    (host.tmp / "etc/lifekit/firewall.nft").write_text("table inet lifekit {}\n")
    assert "does not match" in host("--check").stdout

    host()
    (host.state / "enabled-nftables.service").write_text("enabled\n")
    result = host("--check")
    assert result.returncode == 1
    assert "nftables.service: enabled" in result.stdout

    (host.state / "enabled-nftables.service").unlink()
    (host.state / "active-ufw.service").write_text("active\n")
    result = host("--check")
    assert result.returncode == 1
    assert "ufw.service: active" in result.stdout


def test_trial_records_state_and_arms_rollback_before_loading(host):
    result = host("--trial", "120")

    assert result.returncode == 0, result.stderr
    calls = host.calls()
    arm = next(i for i, c in enumerate(calls) if c.startswith("systemd-run"))
    load = calls.index(f"nft -f {RULESET}")
    assert arm < load, "the rollback must be armed before the ruleset loads"
    assert "--on-active=120s" in calls[arm]
    assert calls[arm].endswith(f"nft -f {host.tmp}/fwstate/rollback.nft")
    assert calls.index("nft list ruleset") < arm
    assert "iptables -t nat -S" in calls and "ip6tables -t filter -S" in calls
    # Nothing is persisted by a trial.
    assert not (host.tmp / "units/lifekit-firewall.service").exists()
    assert not any(c.startswith("systemctl enable") for c in calls)
    baseline = next((host.tmp / "fwstate").glob("baseline-*"))
    assert (baseline / "nft-ruleset.txt").read_text() == "table ip filter {}\n"
    assert (host.tmp / "fwstate/rollback.nft").read_text() == (
        "table inet lifekit\ndelete table inet lifekit\n"
    )


def test_trial_rollback_restores_a_previously_loaded_table(host):
    (host.state / "loaded").write_text("table inet lifekit {\n}\n")

    assert host("--trial").returncode == 0

    assert (host.tmp / "fwstate/rollback.nft").read_text() == (
        "table inet lifekit\ndelete table inet lifekit\ntable inet lifekit {\n}\n"
    )
    assert any("--on-active=300s" in c for c in host.calls())


@pytest.mark.parametrize("client", ["203.0.113.7", "2001:db8::7"])
def test_trial_refuses_a_public_ssh_session(host, client):
    result = host("--trial", WHO_FROM=client)

    assert result.returncode == 1
    assert "not the tailnet" in result.stderr
    assert host.calls() == []


@pytest.mark.parametrize(
    "client", ["100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::1", "tmux(42).%0", ""]
)
def test_trial_proceeds_from_the_tailnet_or_an_unknown_tty(host, client):
    assert host("--trial", WHO_FROM=client).returncode == 0


def test_trial_rejects_a_short_rollback_delay(host):
    result = host("--trial", "10")

    assert result.returncode == 1
    assert host.calls() == []


def test_trial_cancels_its_rollback_when_nft_rejects_the_load(host):
    result = host("--trial", NFT_FAIL_LOAD="1")

    assert result.returncode == 1
    assert host.calls()[-1] == "systemctl stop lifekit-firewall-rollback.timer"


def test_confirm_needs_a_pending_rollback(host):
    result = host("--confirm")

    assert result.returncode == 1
    assert "no rollback is pending" in result.stderr
    assert not (host.tmp / "units/lifekit-firewall.service").exists()


def test_confirm_cancels_rollback_then_persists(host):
    assert host("--trial").returncode == 0

    result = host("--confirm")

    assert result.returncode == 0, result.stderr
    calls = host.calls()
    stop = calls.index("systemctl stop lifekit-firewall-rollback.timer")
    assert stop < calls.index("systemctl enable lifekit-firewall.service")
    assert not (host.tmp / "fwstate/rollback.nft").exists()
    assert host("--check").returncode == 0


def test_rollback_applies_the_recorded_state_now(host):
    assert host("--trial").returncode == 0

    result = host("--rollback")

    assert result.returncode == 0, result.stderr
    calls = host.calls()
    assert calls[-2:] == [
        "systemctl stop lifekit-firewall-rollback.timer",
        f"nft -f {host.tmp}/fwstate/rollback.nft",
    ]
    assert host("--rollback").returncode == 1


@needs_netns
def test_trial_rollback_file_removes_only_its_own_table(tmp_path):
    """The trial's real rollback file, applied by real nft, beside a Docker-like table."""
    script = tmp_path / "run.sh"
    script.write_text(
        "set -e\n"
        "nft -f - <<'EOF'\n"
        "table ip filter {\n\tchain DOCKER-USER {\n\t\treturn\n\t}\n}\n"
        "EOF\n"
        f"nft -f {RULESET}\n"
        "printf 'table inet lifekit\\ndelete table inet lifekit\\n' > rollback.nft\n"
        "nft -c -f rollback.nft\n"
        "nft -f rollback.nft\n"
        "nft -f rollback.nft\n"
        "nft list tables\n"
    )

    result = in_netns("bash", "-c", f"cd {tmp_path} && bash {script}")

    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\n")[0] == "table ip filter"
    assert "lifekit" not in result.stdout
