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


def test_undecodable_live_file_is_an_error_not_drift(source, ws):
    (ws / "AGENTS.md").write_bytes(b"\xff\xfe not utf-8\n")
    r = subprocess.run(
        [
            sys.executable,
            str(DIR / "ledger-persona.py"),
            "check",
            "--workspace",
            str(ws),
            "--pin",
            PIN,
            "--source",
            str(source),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "UnicodeDecodeError" in r.stderr
    assert "DRIFT" not in r.stdout


def test_undecodable_live_file_fails_the_alert_without_posting(
    source, ws, state, tmp_path
):
    (ws / "AGENTS.md").write_bytes(b"\xff\xfe not utf-8\n")
    r = alert(source, ws, state, tmp_path)
    assert r.returncode == 1 and "check failed" in r.stderr
    assert not (state / "calls").exists()


def run_installer(tmp_path, **env):
    fake = tmp_path / "systemctl"
    fake.write_text('#!/usr/bin/env bash\necho "$*" >> "$STATE_DIR/calls"\n')
    fake.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
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
            **env,
        },
        check=True,
        capture_output=True,
    )
    calls = (state / "calls").read_text().splitlines()
    return root, calls


def parse_unit(path: Path) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
        else:
            key, _, value = line.partition("=")
            current[key] = value
    return sections


def test_installer_renders_units_under_install_root(tmp_path):
    root, calls = run_installer(tmp_path)
    unit_dir = root / "etc/systemd/system"
    service = parse_unit(unit_dir / "lifekit-ledger-persona-check.service")["Service"]
    assert service["User"] == "svc" and service["Group"] == "svc"
    assert service["ExecStart"] == (
        "/usr/bin/python3 /opt/stack/scripts/ledger-persona/drift-alert.py"
    )
    timer = parse_unit(unit_dir / "lifekit-ledger-persona-check.timer")
    assert timer["Timer"]["OnCalendar"].startswith("*-*-*")
    assert timer["Install"]["WantedBy"] == "timers.target"
    assert "__" not in (unit_dir / "lifekit-ledger-persona-check.service").read_text()
    assert calls == ["daemon-reload"]


def test_installer_enables_the_timer_only_when_asked(tmp_path):
    _, calls = run_installer(tmp_path, ENABLE="1")
    assert calls == [
        "daemon-reload",
        "enable --now lifekit-ledger-persona-check.timer",
    ]


FAKE_UNIT_SYSTEMCTL = """#!/usr/bin/env bash
case "$1" in
  list-unit-files)
    case "${@: -1}" in
      lifekit-*.timer) echo "lifekit-ledger-persona-check.timer enabled enabled" ;;
      *) exit 1 ;;
    esac
    ;;
  show)
    unit="${@: -1}"
    case "$unit" in
      lifekit-ledger-persona-check.timer)
        printf 'LoadState=loaded\nActiveState=active\nResult=success\nUnitFileState=enabled\n' ;;
      lifekit-ledger-persona-check.service)
        printf 'LoadState=loaded\nActiveState=failed\nResult=exit-code\nUnitFileState=static\n' ;;
      *) printf 'LoadState=not-found\nActiveState=inactive\nResult=success\nUnitFileState=\n' ;;
    esac
    ;;
esac
"""


def test_unit_gauge_reports_a_failed_check_run(tmp_path):
    fake = tmp_path / "systemctl"
    fake.write_text(FAKE_UNIT_SYSTEMCTL)
    fake.chmod(0o755)
    out_dir = tmp_path / "textfile"
    out_dir.mkdir()
    subprocess.run(
        ["bash", str(REPO / "scripts/unit-gauge/unit-gauge.sh"), str(out_dir)],
        env={**os.environ, "SYSTEMCTL": str(fake)},
        check=True,
        capture_output=True,
    )
    prom = (out_dir / "host_unit.prom").read_text()
    assert 'host_unit_failed{unit="lifekit-ledger-persona-check"} 1' in prom
    assert 'host_unit_active{unit="lifekit-ledger-persona-check"} 1' in prom


def test_stale_copy_alone_alerts_with_its_own_action(source, ws, state, tmp_path):
    (ws / "AGENTS.md").write_text(IN_SYNC)
    (ws / "repos/finance-sentry").mkdir(parents=True)
    r = alert(source, ws, state, tmp_path)
    assert r.returncode == 0, r.stderr
    env = json.loads((state / "envelope.json").read_text())
    assert "retired big-picture copy" in env["headline"]
    assert "stale: repos/finance-sentry exists" in env["detail"]
    assert "primer" in env["action"] and "PIN" not in env["action"]
    assert "AGENTS.md" not in env["body"]


def test_drift_and_stale_copy_carry_both_actions(source, ws, state, tmp_path):
    (ws / "AGENTS.md").write_text("hand edited\n")
    (ws / "SOUL.md").write_text("your reply goes back to Kit\n")
    assert alert(source, ws, state, tmp_path).returncode == 0
    env = json.loads((state / "envelope.json").read_text())
    assert "DRIFT" in env["headline"] and "retired" in env["headline"]
    assert "PIN" in env["action"] and "primer" in env["action"]
    assert len(env["action"]) <= 400
