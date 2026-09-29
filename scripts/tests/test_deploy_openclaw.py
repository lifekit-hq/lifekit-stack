"""The deploy split: scripts/deploy-openclaw.sh and the deploy.sh that calls it.

deploy.sh sources deploy-openclaw.sh and calls its OpenClaw phases between
its own platform phases; executed directly, deploy-openclaw.sh runs the named
phases on its own with the helpers in scripts/lib/deploy-common.sh. These
tests pin the call order, the standalone entry point, and the phase-function
return status (a phase whose last command is a false `[[ ]] && ...` would
return 1 and trip deploy.sh's set -e). `docker` is a stub on PATH throughout.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "scripts/deploy.sh"
OPENCLAW = REPO / "scripts/deploy-openclaw.sh"
LIB = REPO / "scripts/lib/deploy-common.sh"

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CALL_LOG"
case " $* " in
  *" exec -T openclaw-gateway openclaw --version "*) echo "OpenClaw $RUNNING_VER (abc)" ;;
  *" lifekit-openclaw:local --version "*) echo "OpenClaw $BUILT_VER (def)" ;;
  *" plugins list --json "*)
    if [ -e "$PLUGINS_FIXED" ]; then v="$BUILT_VER"; else v="2026.6.8"; fi
    echo '[{"id":"codex","enabled":true,"origin":"global","version":"'"$v"'"}]' ;;
  *" plugins update "*) [ -n "$PLUGINS_STUCK" ] || touch "$PLUGINS_FIXED" ;;
esac
exit 0
"""


def phases() -> list[str]:
    m = re.search(r"^OPENCLAW_PHASES=\(([^)]*)\)$", OPENCLAW.read_text(), re.M)
    assert m, "OPENCLAW_PHASES=(...) not found in deploy-openclaw.sh"
    return m.group(1).split()


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


def run(env, *phase_args):
    return subprocess.run(
        ["bash", str(OPENCLAW), *phase_args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_deploy_sh_calls_every_phase_once_in_order():
    called = re.findall(r"^openclaw_phase_(\w+)$", DEPLOY.read_text(), re.M)
    assert called == [p.replace("-", "_") for p in phases()]


def test_every_phase_has_a_function():
    defined = re.findall(r"^openclaw_phase_(\w+)\(\) \{$", OPENCLAW.read_text(), re.M)
    assert defined == [p.replace("-", "_") for p in phases()]


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
    assert sorted(defined) == sorted(
        f"openclaw_phase_{p.replace('-', '_')}" for p in phases()
    )
    assert failures == "failures=1"
    assert Path(env["DOCKER_CALL_LOG"]).read_text() == ""


def test_no_phase_prints_usage(env):
    r = run(env)
    assert r.returncode == 2
    assert "usage:" in r.stderr
    assert " ".join(phases()) in r.stderr


def test_unknown_phase_runs_nothing(env):
    r = run(env, "modules", "bogus")
    assert r.returncode == 2
    assert "unknown phase: bogus" in r.stderr
    assert not (Path(env["LIFEKIT_LIFE_DIR"]) / "system/modules.yaml").exists()


def test_missing_env_file_fails_before_any_phase(env, tmp_path):
    r = run({**env, "ENV_FILE": str(tmp_path / "missing.env")}, "modules")
    assert r.returncode == 1
    assert "Missing" in r.stderr
    assert not (Path(env["LIFEKIT_LIFE_DIR"]) / "system/modules.yaml").exists()


def test_modules_phase_runs_standalone(env):
    r = run(env, "modules")
    assert r.returncode == 0, r.stderr
    copied = Path(env["LIFEKIT_LIFE_DIR"]) / "system/modules.yaml"
    assert copied.read_text() == (REPO / "defaults/modules.yaml").read_text()
    assert "✓ openclaw phases complete: modules" in r.stdout
    # The private Docker config is cleaned up by the EXIT trap.
    assert list(Path(env["TMPDIR"]).iterdir()) == []


def test_build_on_version_bump_with_repinned_plugins_succeeds(env):
    r = run(env, "build")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "plugin codex is 2026.6.8, core is 2026.9.5; updating" in r.stderr
    assert "plugins still off core" not in r.stderr
    calls = Path(env["DOCKER_CALL_LOG"]).read_text()
    assert "stop openclaw-cli openclaw-gateway" in calls
    assert "doctor --fix --non-interactive" in calls


def test_build_with_plugins_still_off_core_fails_at_the_end(env):
    r = run({**env, "PLUGINS_STUCK": "1"}, "build")
    assert r.returncode == 1
    assert "plugins still off core 2026.9.5 after update: codex 2026.6.8" in r.stderr
    assert "post-deploy assertions failed" in r.stdout
    assert "OPENCLAW DEPLOY FAILED" in r.stderr
