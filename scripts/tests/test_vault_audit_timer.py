"""The weekly vault audit as a host timer (scripts/memory-audit).

What this file pins: the unit and timer carry the cron's slot (Sunday 03:30
London, Persistent) and run as the vault's owner against /srv/memory; the
installer drops both units and enables the timer; bootstrap runs the installer;
the runner turns the wrapper's result into one notify-relay envelope (an `info`
summary, or an `act` failure plus a failed unit); and the wrapper itself keeps
its delivery-required guard without a fixed /tmp file. Fixtures are synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/memory-audit"
SERVICE = DIR / "lifekit-vault-audit.service"
TIMER = DIR / "lifekit-vault-audit.timer"
INSTALLER = DIR / "install-vault-audit.sh"
RUNNER = DIR / "run-vault-audit.py"
WRAPPER = DIR / "memory-vault-audit.sh"

# Records every call; `ps` prints a container id unless NO_CONTAINER is set;
# `exec` stores the envelope from stdin and exits POST_RC.
FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$STATE_DIR/calls"
case "$1" in
  ps)
    [[ -n "${NO_CONTAINER:-}" ]] || echo abc123
    ;;
  exec)
    cat > "$STATE_DIR/envelope.json"
    echo "${POST_OUT:-200 {\\"ok\\":true}}"
    exit "${POST_RC:-0}"
    ;;
esac
"""

SUMMARY = (
    "Vault audit | 0 high / 5 medium / 28 low; new 3, resolved 1; rotation 1 item(s). "
    "Report: audits/latest.md"
)


def test_the_service_runs_the_wrapper_as_the_vault_owner_on_the_vault():
    unit = SERVICE.read_text()
    assert "Type=oneshot" in unit
    assert "User=lifekit" in unit and "Group=lifekit" in unit
    assert "Environment=MEMORY_VAULT=/srv/memory" in unit
    assert "ConditionPathExists=/srv/memory/.git" in unit
    assert "ReadWritePaths=/srv/memory" in unit
    exec_start = next(
        line for line in unit.splitlines() if line.startswith("ExecStart=")
    )
    assert exec_start.endswith("/scripts/memory-audit/run-vault-audit.py")
    assert RUNNER.is_file()
    # the runner's own wrapper timeout fits inside the unit's
    start_timeout = int(
        next(
            line.split("=")[1]
            for line in unit.splitlines()
            if line.startswith("TimeoutStartSec=")
        )
    )
    assert start_timeout > 840


def test_the_timer_keeps_the_crons_slot_and_catches_up():
    timer = TIMER.read_text()
    assert "OnCalendar=Sun *-*-* 03:30:00 Europe/London" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_the_installer_installs_both_units_and_enables_the_timer(tmp_path):
    root = tmp_path / "root"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f"#!/bin/sh\necho \"$@\" >> '{tmp_path}/calls'\n")
    systemctl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        capture_output=True,
        text=True,
        env={**os.environ, "INSTALL_ROOT": str(root), "SYSTEMCTL": str(systemctl)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    unit_dir = root / "etc/systemd/system"
    assert (unit_dir / "lifekit-vault-audit.service").read_text() == (
        SERVICE.read_text()
    )
    assert (unit_dir / "lifekit-vault-audit.timer").read_text() == TIMER.read_text()
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls == ["daemon-reload", "enable --now lifekit-vault-audit.timer"]


def test_the_installer_needs_root_outside_a_test_root():
    if os.geteuid() == 0:
        return
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "INSTALL_ROOT"},
        check=False,
    )
    assert result.returncode == 1 and "root" in result.stderr


def test_bootstrap_runs_the_installer():
    assert (
        'bash "$REPO_DIR/scripts/memory-audit/install-vault-audit.sh"'
        in (REPO / "scripts/bootstrap-vps.sh").read_text()
    )


def run_runner(tmp_path: Path, wrapper_body: str, **env: str):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(wrapper_body)
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(RUNNER)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "WRAPPER": str(wrapper),
            "DOCKER": str(docker),
            "STATE_DIR": str(state),
            **env,
        },
        check=False,
    )
    return result, state


def envelope(state: Path) -> dict:
    return json.loads((state / "envelope.json").read_text())


def test_a_clean_audit_posts_its_summary_as_one_info_envelope(tmp_path):
    result, state = run_runner(tmp_path, f"echo '{SUMMARY}'\n")
    assert result.returncode == 0, result.stderr
    assert SUMMARY in result.stdout  # the journal keeps the line too
    calls = (state / "calls").read_text().splitlines()
    assert "label=com.docker.compose.service=notify-relay" in calls[0]
    assert calls[1].startswith("exec -i abc123 node -e") and len(calls) == 2
    env = envelope(state)
    assert env == {
        "level": "info",
        "source": "host",
        "subject": "vault-audit",
        "headline": SUMMARY,
    }


def test_a_failed_audit_posts_an_act_envelope_and_fails_the_unit(tmp_path):
    body = "echo 'AUDIT FAIL: no fresh report written for 2026-11-01'\necho why >&2\nexit 1\n"
    result, state = run_runner(tmp_path, body)
    assert result.returncode == 1
    env = envelope(state)
    assert env["level"] == "act"
    assert env["headline"] == "AUDIT FAIL: no fresh report written for 2026-11-01"
    assert (env["source"], env["subject"]) == ("host", "vault-audit")
    assert "journalctl -u lifekit-vault-audit" in env["action"]
    assert env["detail"] == "why"
    assert len(env["action"]) <= 400 and len(env["headline"]) <= 800


def test_a_wrapper_that_cannot_run_is_reported_not_swallowed(tmp_path):
    result, state = run_runner(tmp_path, "", WRAPPER=str(tmp_path / "absent.sh"))
    assert result.returncode == 1
    env = envelope(state)
    assert env["level"] == "act"
    assert env["headline"] == "AUDIT FAIL: wrapper exited 2 with no output"


def test_a_wrapper_that_prints_nothing_is_a_failure_not_a_summary(tmp_path):
    result, state = run_runner(tmp_path, "true\n")
    assert result.returncode == 1
    env = envelope(state)
    assert env["level"] == "act" and "no summary" in env["headline"]


def test_an_overlong_summary_stays_under_the_headline_cap(tmp_path):
    result, state = run_runner(tmp_path, "printf 'x%.0s' $(seq 1 3000)\n")
    assert result.returncode == 0, result.stderr
    assert len(envelope(state)["headline"]) <= 800


def test_a_relay_that_refuses_the_envelope_fails_the_unit_even_after_a_clean_audit(
    tmp_path,
):
    result, _ = run_runner(
        tmp_path,
        f"echo '{SUMMARY}'\n",
        POST_RC="1",
        POST_OUT="400 bad envelope",
    )
    assert result.returncode == 1
    assert "refused the envelope" in result.stderr and "400" in result.stderr


def test_a_missing_relay_container_fails_the_unit(tmp_path):
    result, state = run_runner(tmp_path, f"echo '{SUMMARY}'\n", NO_CONTAINER="1")
    assert result.returncode == 1
    assert "no running notify-relay container" in result.stderr
    assert not (state / "envelope.json").exists()


def make_vault(tmp_path: Path, audit_body: str) -> Path:
    vault = tmp_path / "vault"
    (vault / "bin").mkdir(parents=True)
    (vault / "audits").mkdir()
    (vault / "bin/audit.sh").write_text(audit_body)
    return vault


def run_wrapper(vault: Path, tmp_path: Path, date: str = "2026-11-01"):
    scratch = tmp_path / "tmp"
    scratch.mkdir(exist_ok=True)
    # the wrapper's own `date -u +%F` is faked so the report date is known
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(exist_ok=True)
    fake_date = fake_bin / "date"
    fake_date.write_text(f"#!/bin/sh\necho {date}\n")
    fake_date.chmod(0o755)
    result = subprocess.run(
        ["sh", str(WRAPPER)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MEMORY_VAULT": str(vault),
            "TMPDIR": str(scratch),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        check=False,
    )
    return result, scratch


def test_the_wrapper_prints_the_summary_when_a_fresh_report_exists(tmp_path):
    vault = make_vault(
        tmp_path,
        'printf \'# Vault audit - 2026-11-01\\n\' > "$(dirname "$0")/../audits/latest.md"\n'
        'echo "audit | 0 high / 1 medium / 2 low"\n',
    )
    result, scratch = run_wrapper(vault, tmp_path)
    assert result.returncode == 0, result.stdout
    assert result.stdout == "Vault audit | 0 high / 1 medium / 2 low\n"
    assert list(scratch.iterdir()) == []  # its error file is removed again


def test_the_wrapper_fails_when_the_report_is_not_from_today(tmp_path):
    vault = make_vault(tmp_path, 'echo "audit | 0 high"\n')
    (vault / "audits/latest.md").write_text("# Vault audit - 2026-10-25\n")
    result, scratch = run_wrapper(vault, tmp_path)
    assert result.returncode == 1
    assert "no fresh report written for 2026-11-01" in result.stdout
    assert list(scratch.iterdir()) == []


def test_the_wrapper_reports_the_audits_last_stderr_line_on_failure(tmp_path):
    vault = make_vault(tmp_path, "echo 'lint exploded' >&2\nexit 3\n")
    result, scratch = run_wrapper(vault, tmp_path)
    assert result.returncode == 1
    assert "AUDIT FAIL: bin/audit.sh errored: lint exploded" in result.stdout
    assert list(scratch.iterdir()) == []


def test_the_wrapper_fails_without_the_vault_harness(tmp_path):
    result, _ = run_wrapper(tmp_path / "nowhere", tmp_path)
    assert result.returncode == 1
    assert "audit.sh not found" in result.stdout
