"""The deploy split: scripts/deploy-openclaw.sh and the deploy.sh that calls it.

deploy.sh sources deploy-openclaw.sh and calls its OpenClaw phases between
its own platform phases. These tests run deploy.sh end to end against a
scratch copy of the repo (phase bodies and platform helper scripts replaced by
trace-logging stubs) to pin the call order, and source deploy-openclaw.sh
under `set -euo pipefail` to pin the build phase's return status (a phase
whose last command is a false `[[ ]] && ...` would return 1 and trip
deploy.sh's set -e). `docker` is a stub on PATH throughout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
OPENCLAW = REPO / "scripts/deploy-openclaw.sh"
LIB = REPO / "scripts/lib/deploy-common.sh"

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CALL_LOG"
case " $* " in
  *" config --format json "*) echo '{"name":"proj"}' ;;
  *" exec -T openclaw-gateway openclaw --version "*) echo "OpenClaw $RUNNING_VER (abc)" ;;
  *" lifekit-openclaw:local --version "*) echo "OpenClaw $BUILT_VER (def)" ;;
  *" plugins list --json "*)
    if [ -e "$PLUGINS_FIXED" ]; then v="$BUILT_VER"; else v="2026.6.8"; fi
    echo '[{"id":"codex","enabled":true,"origin":"global","version":"'"$v"'"}]' ;;
  *" plugins update "*) [ -n "$PLUGINS_STUCK" ] || touch "$PLUGINS_FIXED" ;;
esac
exit 0
"""

LOGGING_SH = """#!/bin/sh
echo "%s" >> "$DOCKER_CALL_LOG"
"""

PHASES = ["prepare", "modules", "build", "configure", "post_up", "smoke"]

# Platform steps and OpenClaw phases in the order deploy.sh ran them before the
# split; `openclaw:<phase>` marks a phase call, the rest are platform steps.
DEPLOY_ORDER = [
    "openclaw:prepare",
    "embed-origin",
    "openclaw:modules",
    "render-heartbeat",
    "contract-static",
    "openclaw:build",
    "compose-up",
    "openclaw:configure",
    "prometheus-reload",
    "grafana-alerting-reload",
    "grafana-datasources-reload",
    "openclaw:post_up",
    "compose-ps",
    "openclaw:smoke",
    "contract-enforce",
    "builder-gc",
    "tmp-scratch",
]


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)
    env_file = tmp_path / "stack.env"
    env_file.write_text("OPENCLAW_GATEWAY_TOKEN=t\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    call_log = tmp_path / "docker-calls.log"
    call_log.write_text("")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMPDIR": str(scratch),
        "DOCKER_CALL_LOG": str(call_log),
        "REPO_DIR": str(REPO),
        "ENV_FILE": str(env_file),
        # Absent: the uid-1000 ownership check then finds nothing to refuse.
        "OPENCLAW_CONFIG_DIR": str(tmp_path / "no-config"),
        "LIFEKIT_LIFE_DIR": str(tmp_path / "memory"),
        "RUNNING_VER": "2026.9.4",
        "BUILT_VER": "2026.9.5",
        "PLUGINS_FIXED": str(tmp_path / "plugins-fixed"),
        "PLUGINS_STUCK": "",
    }


def stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def marker(line: str) -> str | None:
    if line.startswith(("openclaw:", "contract-")) or line in {
        "embed-origin",
        "render-heartbeat",
        "builder-gc",
        "tmp-scratch",
    }:
        return line
    if "up -d --build" in line:
        return "compose-up"
    if "kill -s HUP prometheus" in line:
        return "prometheus-reload"
    if line.endswith(" ps"):
        return "compose-ps"
    if "alerting/reload" in line:
        return "grafana-alerting-reload"
    if "datasources/reload" in line:
        return "grafana-datasources-reload"
    return None


def test_deploy_runs_each_openclaw_phase_at_its_point_in_the_platform_sequence(
    env, tmp_path
):
    fake = tmp_path / "repo"
    (fake / "scripts/lib").mkdir(parents=True)
    shutil.copy(REPO / "scripts/deploy.sh", fake / "scripts/deploy.sh")
    shutil.copy(LIB, fake / "scripts/lib/deploy-common.sh")
    stub(
        fake / "scripts/deploy-openclaw.sh",
        "".join(
            f'openclaw_phase_{p}() {{ echo "openclaw:{p}" >> "$DOCKER_CALL_LOG"; }}\n'
            for p in PHASES
        ),
    )
    stub(fake / "scripts/deploy-embed-origin.sh", LOGGING_SH % "embed-origin")
    stub(fake / "scripts/render-heartbeat.sh", LOGGING_SH % "render-heartbeat")
    stub(fake / "scripts/docker-builder-gc.sh", LOGGING_SH % "builder-gc")
    stub(fake / "scripts/tmp-scratch-policy.sh", LOGGING_SH % "tmp-scratch")
    stub(
        fake / "scripts/platform-contract.py",
        "import os, sys\n"
        'open(os.environ["DOCKER_CALL_LOG"], "a").write("contract-" + sys.argv[1].lstrip("-") + "\\n")\n',
    )
    alert_dir = fake / "compose/observability/grafana/provisioning/alerting"
    alert_dir.mkdir(parents=True)
    (alert_dir / "contact-points.yml.tmpl").write_text("chat: __TELEGRAM_CHAT_ID__\n")
    (fake / "compose/docker-compose.yml").write_text("")
    bin_dir = tmp_path / "bin"
    stub(bin_dir / "git", '#!/bin/sh\necho "  HEAD branch: main"\n')
    stub(bin_dir / "curl", '#!/bin/sh\necho "curl $*" >> "$DOCKER_CALL_LOG"\n')
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    Path(env["ENV_FILE"]).write_text("LIFEKIT_TELEGRAM_CHAT=123\n")

    r = subprocess.run(
        ["bash", str(fake / "scripts/deploy.sh")],
        env={
            **env,
            "REPO_DIR": str(fake),
            "LIFEKIT_DEPLOY_REEXEC": "1",
            "LIFEKIT_STATE_DIR_HOST": str(state_dir),
            "DOCKER_GID": "999",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert "✓ deploy complete." in r.stdout
    trace = Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()
    assert [m for m in map(marker, trace) if m] == DEPLOY_ORDER


def build_phase(env, extra_env=None):
    script = f"""
set -euo pipefail
source {LIB}
source {OPENCLAW}
rc=0
openclaw_phase_build || rc=$?
echo "rc=$rc"
echo "failures=${{#DEPLOY_FAILURES[@]}}"
for f in "${{DEPLOY_FAILURES[@]}}"; do echo "failure: $f"; done
"""
    return subprocess.run(
        ["bash", "-c", script],
        env={**env, **(extra_env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_on_version_bump_with_repinned_plugins_returns_zero_and_queues_nothing(
    env,
):
    r = build_phase(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0" in r.stdout.splitlines()
    assert "failures=0" in r.stdout.splitlines()
    assert "plugin codex is 2026.6.8, core is 2026.9.5; updating" in r.stderr
    calls = Path(env["DOCKER_CALL_LOG"]).read_text()
    assert "stop openclaw-cli openclaw-gateway" in calls
    assert "doctor --fix --non-interactive" in calls


def test_build_with_plugins_still_off_core_queues_a_failure_without_aborting(env):
    r = build_phase(env, {"PLUGINS_STUCK": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0" in r.stdout.splitlines()
    assert (
        "failure: plugins still off core 2026.9.5 after update: codex 2026.6.8"
        in r.stdout
    )


def test_build_with_unchanged_version_migrates_nothing(env):
    r = build_phase(env, {"RUNNING_VER": "2026.9.5"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0" in r.stdout.splitlines()
    assert "failures=0" in r.stdout.splitlines()
    calls = Path(env["DOCKER_CALL_LOG"]).read_text()
    assert "doctor --fix" not in calls
    assert " stop " not in calls


def test_sourcing_defines_the_phases_and_runs_nothing(env):
    # Sourcing the lib a second time (deploy-openclaw.sh does) must not reset
    # a failure deploy.sh already queued.
    script = f"""
set -euo pipefail
source {LIB}
fail_later "queued before" 2>/dev/null
source {OPENCLAW}
declare -F | awk '{{print $3}}' | grep '^openclaw_phase_'
echo "failures=${{#DEPLOY_FAILURES[@]}}"
"""
    r = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr
    *defined, failures = r.stdout.split()
    assert sorted(defined) == sorted(f"openclaw_phase_{p}" for p in PHASES)
    assert failures == "failures=1"
    assert Path(env["DOCKER_CALL_LOG"]).read_text() == ""
