"""drift-alert.py: silent when the live persona matches the pin, one notify-relay
envelope on drift, a nonzero exit (journal + unit gauge) on its own failure.
Runs against a scratch workspace, a local --source dir and a fake docker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/ledger-persona"
ALERT = DIR / "drift-alert.py"
PIN = "a" * 40
IN_SYNC = "# Core\n\nlaw one\n\n---\n\n# OpenClaw\n\ntelegram\n"

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


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "adapters").mkdir(parents=True)
    (src / "persona.core.md").write_text("# Core\n\nlaw one\n")
    (src / "adapters/openclaw.md").write_text("# OpenClaw\n\ntelegram\n")
    return src


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = tmp_path / "ws"
    w.mkdir()
    return w


@pytest.fixture
def state(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


def alert(source: Path, ws: Path, state: Path, tmp_path: Path, **env: str):
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    return subprocess.run(
        [
            sys.executable,
            str(ALERT),
            "--pin",
            PIN,
            "--source",
            str(source),
            "--workspace",
            str(ws),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "DOCKER": str(docker), "STATE_DIR": str(state), **env},
    )


def test_in_sync_is_silent(source, ws, state, tmp_path):
    (ws / "AGENTS.md").write_text(IN_SYNC)
    r = alert(source, ws, state, tmp_path)
    assert r.returncode == 0 and "in sync" in r.stdout
    assert not (state / "calls").exists()


def test_drift_posts_one_act_envelope(source, ws, state, tmp_path):
    (ws / "AGENTS.md").write_text("hand edited\n")
    r = alert(source, ws, state, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "alert sent" in r.stdout
    calls = (state / "calls").read_text().splitlines()
    assert calls[0].startswith(
        "ps -q --filter label=com.docker.compose.project=compose"
    )
    assert "label=com.docker.compose.service=notify-relay" in calls[0]
    assert calls[1].startswith("exec -i abc123 node -e")
    assert len(calls) == 2
    env = json.loads((state / "envelope.json").read_text())
    assert env["level"] == "act"
    assert (env["source"], env["subject"]) == ("host", "ledger-persona")
    assert "DRIFT" in env["headline"] and "finance-sentry@aaaaaaa" in env["headline"]
    assert "+law one" in env["detail"]
    assert "PIN" in env["action"] and len(env["action"]) <= 400
    assert len(env["headline"]) <= 800


def test_missing_live_file_is_drift(source, ws, state, tmp_path):
    r = alert(source, ws, state, tmp_path)
    assert r.returncode == 0, r.stderr
    env = json.loads((state / "envelope.json").read_text())
    assert env["level"] == "act" and "is missing" in env["detail"]


def test_detail_is_bounded(source, ws, state, tmp_path):
    (ws / "AGENTS.md").write_text("\n".join(f"line {i}" for i in range(500)) + "\n")
    assert alert(source, ws, state, tmp_path).returncode == 0
    env = json.loads((state / "envelope.json").read_text())
    assert len(env["detail"].splitlines()) <= 40


def test_check_failure_exits_nonzero_without_alerting(source, ws, state, tmp_path):
    (source / "persona.core.md").unlink()
    (ws / "AGENTS.md").write_text("hand edited\n")
    r = alert(source, ws, state, tmp_path)
    assert r.returncode == 1
    assert "check failed" in r.stderr
    assert not (state / "calls").exists()


def test_unreadable_workspace_file_is_a_failure_not_drift(source, ws, state, tmp_path):
    (ws / "AGENTS.md").write_text("x\n")
    (ws / "AGENTS.md").chmod(0)
    if os.access(ws / "AGENTS.md", os.R_OK):
        pytest.skip("running as a user that ignores file modes")
    r = alert(source, ws, state, tmp_path)
    assert r.returncode == 1 and "check failed" in r.stderr
    assert not (state / "calls").exists()


def test_relay_down_is_a_failure(source, ws, state, tmp_path):
    (ws / "AGENTS.md").write_text("hand edited\n")
    r = alert(source, ws, state, tmp_path, NO_CONTAINER="1")
    assert r.returncode == 1
    assert "not delivered" in r.stderr and "notify-relay" in r.stderr


def test_relay_refusal_is_a_failure(source, ws, state, tmp_path):
    (ws / "AGENTS.md").write_text("hand edited\n")
    r = alert(source, ws, state, tmp_path, POST_RC="1", POST_OUT="400 bad envelope")
    assert r.returncode == 1
    assert "not delivered" in r.stderr and "400 bad envelope" in r.stderr


def test_check_exit_codes_separate_drift_from_error(source, ws):
    script = DIR / "ledger-persona.py"

    def run(*extra: str):
        return subprocess.run(
            [sys.executable, str(script), "check", "--workspace", str(ws), *extra],
            capture_output=True,
            text=True,
        )

    (ws / "AGENTS.md").write_text("hand edited\n")
    assert run("--pin", PIN, "--source", str(source)).returncode == 1
    bad_pin = run("--pin", "main", "--source", str(source))
    assert bad_pin.returncode == 2 and "40-char" in bad_pin.stderr
    missing_source = run("--pin", PIN, "--source", str(source / "nope"))
    assert missing_source.returncode == 2


def test_unit_runs_the_checkout_as_the_deploy_account():
    unit = (DIR / "lifekit-ledger-persona-check.service").read_text()
    assert "User=__LIFEKIT_USER__" in unit
    assert "__REPO_DIR__/scripts/ledger-persona/drift-alert.py" in unit
    assert "EnvironmentFile" not in unit
    assert "/home/" not in unit


def test_timer_is_daily_and_named_for_the_unit_gauge():
    # unit-gauge.sh reports every installed `lifekit-*.timer` as host_unit_*,
    # which the "host systemd unit is down or failed" rule alerts on.
    timer = (DIR / "lifekit-ledger-persona-check.timer").read_text()
    assert "OnCalendar=*-*-*" in timer
    assert "Persistent=true" in timer
    gauge = (REPO / "scripts/unit-gauge/unit-gauge.sh").read_text()
    assert "TIMER_GLOB='lifekit-*.timer'" in gauge


def test_installer_renders_units_under_install_root(tmp_path):
    fake = tmp_path / "systemctl"
    fake.write_text('#!/usr/bin/env bash\necho "$*" >> "$STATE_DIR/calls"\n')
    fake.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "root"
    subprocess.run(
        ["bash", str(DIR / "install-ledger-persona-check.sh")],
        env={
            **os.environ,
            "INSTALL_ROOT": str(root),
            "SYSTEMCTL": str(fake),
            "STATE_DIR": str(state),
            "LIFEKIT_USER": "svc",
            "REPO_DIR": "/opt/stack",
        },
        check=True,
        capture_output=True,
    )
    unit = (
        root / "etc/systemd/system/lifekit-ledger-persona-check.service"
    ).read_text()
    assert "User=svc" in unit and "Group=svc" in unit
    assert (
        "ExecStart=/usr/bin/python3 /opt/stack/scripts/ledger-persona/drift-alert.py"
        in unit
    )
    assert "__" not in unit
    assert (root / "etc/systemd/system/lifekit-ledger-persona-check.timer").is_file()
    calls = (state / "calls").read_text().splitlines()
    assert calls == [
        "daemon-reload",
        "enable --now lifekit-ledger-persona-check.timer",
    ]
