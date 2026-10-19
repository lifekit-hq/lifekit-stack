"""Behaviour of the post-upgrade smoke checks in scripts/upgrade-smoke/, run as
subprocesses against stubbed `docker`, `curl`, `claude` and `no-mistakes` on
PATH and a throwaway firstmate root and home. Nothing here touches a real
gateway, calls a model or reads a real firstmate checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / "scripts/upgrade-smoke"

# ---------------------------------------------------------------- stubs

DOCKER_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_DIR/docker.log"
case "$1" in
  ps) [ -z "${STUB_NO_GATEWAY:-}" ] && echo gw123; exit 0 ;;
  inspect) echo "${STUB_CONTAINER_STATE:-true healthy}"; exit 0 ;;
  exec) ;;
  *) echo "unexpected docker $1" >&2; exit 99 ;;
esac
args=" $* "
case "$args" in
  *" --version "*) echo "OpenClaw ${STUB_OC_VERSION:-2026.9.8} (fc23bc8)" ;;
  *" node -e "*) [ -z "${STUB_HEALTHZ_RC:-}" ] || exit "$STUB_HEALTHZ_RC"; echo "${STUB_HEALTHZ:-{\"ok\":true,\"status\":\"live\"\}}" ;;
  *" health --json "*) cat "$STUB_DIR/health.json" ;;
  *" doctor --post-upgrade --json "*) cat "$STUB_DIR/post-upgrade.json" ;;
  *" mcp probe --json "*) cat "$STUB_DIR/mcp.json"; exit "${STUB_MCP_RC:-0}" ;;
  *" doctor --json "*) cat "$STUB_DIR/doctor.json" ;;
  *) echo "unexpected exec: $*" >&2; exit 99 ;;
esac
"""

CURL_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_DIR/curl.log"
[ -n "${STUB_CURL_HEALTHZ:-}" ] || exit 7
printf '%s\n' "$STUB_CURL_HEALTHZ"
"""

CLAUDE_STUB = r"""#!/bin/sh
printf '%s\n' "$PWD $*" >> "$STUB_DIR/claude.log"
case " $* " in
  *" --version "*) echo "${STUB_CLAUDE_VERSION:-2.1.303 (Claude Code)}"; exit 0 ;;
esac
[ -z "${STUB_CLAUDE_RC:-}" ] || exit "$STUB_CLAUDE_RC"
cat "$STUB_DIR/claude.json"
"""

NM_STUB = r"""#!/bin/sh
case "$1" in
  --version) echo "no-mistakes version ${STUB_NM_VERSION:-v1.84.0} (4822244) 2026-09-26T08:30:47Z" ;;
  doctor) cat "$STUB_DIR/nm-doctor.txt"; exit "${STUB_NM_DOCTOR_RC:-0}" ;;
  daemon) echo "${STUB_NM_DAEMON:-  ● daemon running (pid 1)}" ;;
  *) echo "unexpected no-mistakes $*" >&2; exit 99 ;;
esac
"""

BOOTSTRAP_STUB = r"""#!/bin/sh
printf '%s %s %s\n' "$FM_HOME" "$FM_BOOTSTRAP_DETECT_ONLY" "$FM_BOOTSTRAP_NETWORK" >> "$STUB_DIR/bootstrap.log"
[ -z "${STUB_BOOTSTRAP_OUT:-}" ] || printf '%s\n' "$STUB_BOOTSTRAP_OUT"
exit "${STUB_BOOTSTRAP_RC:-0}"
"""

# ---------------------------------------------------------------- fixtures

HEALTHY_HEALTH = {
    "ok": True,
    "plugins": {"loaded": ["anthropic", "telegram"], "errors": [], "unavailable": []},
    "modelRuntime": {"degraded": False, "pendingAgents": []},
    "childRuntime": {"available": True},
    "channels": {
        "telegram": {
            "accounts": {
                "default": {"enabled": True, "running": True, "connected": True},
                "second": {"enabled": True, "running": True, "connected": True},
                "off": {"enabled": False, "running": False, "connected": None},
            }
        }
    },
}
HEALTHY_MCP = {"servers": {"a": {"tools": 3}, "b": {"tools": 1}}, "tools": []}
# The live install's standing warnings: never red on their own.
HEALTHY_DOCTOR = {
    "ok": False,
    "findings": [
        {
            "checkId": "core/doctor/security",
            "severity": "warning",
            "message": "lan bind",
        },
        {"checkId": "core/doctor/heartbeat-scratch-migration", "severity": "warning"},
    ],
}
HEALTHY_CLAUDE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "pong",
    "total_cost_usd": 0.00061969,
}
HEALTHY_NM_DOCTOR = """  System
  ✓ git             git version 2.47.3
  ✓ gh              ok
  – az              not found (optional, needed for Azure DevOps PR/CI)
  ✓ data directory  /home/x/.no-mistakes
  ✓ database        ok
  ✓ daemon          running

  Agents
  ✓ claude          /home/x/.local/bin/claude
  – codex           not found
  ✓ gate validation  claude is runnable
"""


@pytest.fixture
def rig(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("docker", DOCKER_STUB),
        ("curl", CURL_STUB),
        ("claude", CLAUDE_STUB),
        ("no-mistakes", NM_STUB),
    ):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    stub = tmp_path / "stub"
    stub.mkdir()
    for name, data in (
        ("health.json", HEALTHY_HEALTH),
        ("post-upgrade.json", {"probesRun": ["plugin.version_drift"], "findings": []}),
        ("mcp.json", HEALTHY_MCP),
        ("doctor.json", HEALTHY_DOCTOR),
        ("claude.json", HEALTHY_CLAUDE),
    ):
        (stub / name).write_text(json.dumps(data))
    (stub / "nm-doctor.txt").write_text(HEALTHY_NM_DOCTOR)
    # A firstmate code root and live home: git trees on a branch, the home
    # with a bin/ that parses and a detect-only bootstrap stub.
    root, home = tmp_path / "firstmate", tmp_path / "fm-home"
    for d in (root, home):
        (d / "bin").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(d),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "init",
            ],
            check=True,
        )
    (home / "bin/fm-ok.sh").write_text("#!/usr/bin/env bash\necho ok\n")
    (home / "bin/fm-bootstrap.sh").write_text(BOOTSTRAP_STUB)
    (home / "bin/fm-bootstrap.sh").chmod(0o755)
    return {"tmp": tmp_path, "bin": bin_dir, "stub": stub, "root": root, "home": home}


def write(rig, name, data):
    (rig["stub"] / name).write_text(data if isinstance(data, str) else json.dumps(data))


def smoke(rig, pillar, *args, **env):
    full_env = {
        **os.environ,
        "PATH": f"{rig['bin']}:{os.environ['PATH']}",
        "STUB_DIR": str(rig["stub"]),
        "FIRSTMATE_ROOT": str(rig["root"]),
        "FIRSTMATE_HOME": str(rig["home"]),
        **{k: str(v) for k, v in env.items()},
    }
    full_env.pop("OPENCLAW_GATEWAY", None)
    proc = subprocess.run(
        ["bash", str(SMOKE / f"{pillar}.sh"), *args],
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = proc.stdout.splitlines()
    assert len(lines) == 1, f"one summary line expected, got {proc.stdout!r}"
    assert lines[0].startswith(f"upgrade-smoke {pillar}: ")
    return proc.returncode, lines[0]


# ---------------------------------------------------------------- openclaw


def test_openclaw_passes_on_a_healthy_gateway(rig):
    rc, line = smoke(rig, "openclaw", "--expect-version", "2026.9.8")
    assert rc == 0, line
    assert "PASS version=2026.9.8 channels=2/2 mcp=2/2 doctor-errors=0" in line


def test_openclaw_probes_healthz_inside_the_container_not_the_host_port(rig):
    rc, line = smoke(rig, "openclaw")
    assert rc == 0, line
    assert not (rig["stub"] / "curl.log").exists()
    calls = (rig["stub"] / "docker.log").read_text()
    assert "exec gw123 node -e" in calls
    assert "127.0.0.1:18789/healthz" in calls


def test_openclaw_healthz_override_curls_the_given_url(rig):
    url = "http://127.0.0.1:19999/healthz"
    rc, line = smoke(
        rig,
        "openclaw",
        OPENCLAW_HEALTHZ=url,
        STUB_CURL_HEALTHZ='{"ok":true}',
        STUB_HEALTHZ_RC="1",
    )
    assert rc == 0, line
    assert url in (rig["stub"] / "curl.log").read_text()
    rc, line = smoke(rig, "openclaw", OPENCLAW_HEALTHZ=url)
    assert rc == 1, line
    assert "healthz" in line.split(" red=", 1)[1]


def test_openclaw_never_fixes_restarts_or_recreates(rig):
    smoke(rig, "openclaw")
    calls = (rig["stub"] / "docker.log").read_text().splitlines()
    assert {c.split()[0] for c in calls} <= {"ps", "inspect", "exec"}
    joined = "\n".join(calls)
    for forbidden in ("--fix", "--repair", "restart", "config patch", " agent "):
        assert forbidden not in joined


@pytest.mark.parametrize(
    ("setup", "env", "reason"),
    [
        (None, {"STUB_NO_GATEWAY": "1"}, "gateway-not-found"),
        (None, {"STUB_CONTAINER_STATE": "true unhealthy"}, "container-true-unhealthy"),
        (None, {"STUB_OC_VERSION": "2026.9.5"}, "version-not-2026.9.8"),
        (None, {"STUB_HEALTHZ": '{"ok":false}'}, "healthz"),
        (None, {"STUB_HEALTHZ_RC": "1"}, "healthz"),
        (
            ("health.json", {**HEALTHY_HEALTH, "plugins": {"errors": [{"id": "x"}]}}),
            {},
            "plugin-errors",
        ),
        (
            ("health.json", {**HEALTHY_HEALTH, "modelRuntime": {"degraded": True}}),
            {},
            "model-runtime-degraded",
        ),
        (
            (
                "health.json",
                {
                    **HEALTHY_HEALTH,
                    "channels": {
                        "telegram": {
                            "accounts": {
                                "default": {
                                    "enabled": True,
                                    "running": True,
                                    "connected": False,
                                }
                            }
                        }
                    },
                },
            ),
            {},
            "channels-down",
        ),
        (("health.json", "not json"), {}, "health-unreadable"),
        (
            ("post-upgrade.json", {"findings": [{"checkId": "plugin.version_drift"}]}),
            {},
            "post-upgrade-findings",
        ),
        (None, {"STUB_MCP_RC": "1"}, "mcp-probe"),
        (("mcp.json", {"servers": {"a": {"tools": 3}, "b": {}}}), {}, "mcp-probe"),
        (
            (
                "doctor.json",
                {
                    "findings": [
                        *HEALTHY_DOCTOR["findings"],
                        {"checkId": "core/doctor/sessions", "severity": "error"},
                    ]
                },
            ),
            {},
            "doctor:core/doctor/sessions",
        ),
    ],
)
def test_openclaw_fails_on_a_broken_gateway(rig, setup, env, reason):
    if setup:
        write(rig, *setup)
    rc, line = smoke(rig, "openclaw", "--expect-version", "2026.9.8", **env)
    assert rc == 1, line
    assert "FAIL" in line
    assert reason in line.split(" red=", 1)[1]


def test_openclaw_summary_carries_no_config_values(rig):
    health = json.loads(json.dumps(HEALTHY_HEALTH))
    health["channels"]["telegram"]["accounts"]["private-chat-name"] = {
        "enabled": True,
        "running": False,
        "lastError": "token fixture-value-aaaa rejected",
    }
    write(rig, "health.json", health)
    rc, line = smoke(rig, "openclaw")
    assert rc == 1
    assert "private-chat-name" not in line and "fixture-value" not in line


# ---------------------------------------------------------------- claude code


def test_claude_code_passes_on_a_healthy_cli(rig):
    rc, line = smoke(rig, "claude-code", "--expect-version", "2.1.303")
    assert rc == 0, line
    assert "PASS version=2.1.303 turn=ok cost_usd=0.0006" in line


def test_claude_code_makes_exactly_one_minimal_call(rig):
    smoke(rig, "claude-code")
    calls = [
        c for c in (rig["stub"] / "claude.log").read_text().splitlines() if " -p " in c
    ]
    assert len(calls) == 1
    cwd, _, argv = calls[0].partition(" ")
    # From a throwaway directory (no CLAUDE.md), removed afterwards.
    assert not Path(cwd).exists()
    for flag in (
        "--model haiku",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--max-turns 1",
    ):
        assert flag in argv


def test_claude_code_no_call_skips_the_turn(rig):
    rc, line = smoke(rig, "claude-code", "--no-call")
    assert rc == 0, line
    assert "turn=skipped" in line
    assert " -p " not in (rig["stub"] / "claude.log").read_text()


@pytest.mark.parametrize(
    ("reply", "env", "reason"),
    [
        (None, {"STUB_CLAUDE_VERSION": "command not found"}, "version-unreadable"),
        (None, {"STUB_CLAUDE_VERSION": "2.1.290 (Claude Code)"}, "version-not-2.1.303"),
        (None, {"STUB_CLAUDE_RC": "1"}, "turn-exit-1"),
        (
            {**HEALTHY_CLAUDE, "is_error": True, "result": "Not logged in"},
            {},
            "turn-bad-reply",
        ),
        ({**HEALTHY_CLAUDE, "result": "I cannot help with that"}, {}, "turn-bad-reply"),
        ("Invalid API key", {}, "turn-no-json"),
        ([HEALTHY_CLAUDE], {}, "turn-no-json"),
    ],
)
def test_claude_code_fails_on_a_broken_cli(rig, reply, env, reason):
    if reply is not None:
        write(rig, "claude.json", reply)
    rc, line = smoke(rig, "claude-code", "--expect-version", "2.1.303", **env)
    assert rc == 1, line
    assert reason in line.split(" red=", 1)[1]
    assert "Not logged in" not in line and "Invalid API key" not in line


# ---------------------------------------------------------------- firstmate


def test_firstmate_passes_on_a_healthy_toolchain(rig):
    rc, line = smoke(rig, "firstmate", "--expect-no-mistakes-version", "v1.84.0")
    assert rc == 0, line
    assert "PASS no-mistakes=v1.84.0" in line


def test_firstmate_runs_bootstrap_detect_only_and_local(rig):
    smoke(rig, "firstmate")
    log = (rig["stub"] / "bootstrap.log").read_text().split()
    assert log == [str(rig["home"]), "1", "skip"]


def test_firstmate_tolerates_ordinary_bootstrap_notes(rig):
    rc, line = smoke(
        rig, "firstmate", STUB_BOOTSTRAP_OUT="BOOTSTRAP_INFO: lavish-axi is older"
    )
    assert rc == 0, line
    assert "bootstrap-notes=1" in line


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"STUB_NM_VERSION": "v1.80.0"}, "no-mistakes-not-v1.84.0"),
        ({"STUB_NM_DOCTOR_RC": "1"}, "no-mistakes-doctor-exit-1"),
        ({"STUB_NM_DAEMON": "  ○ daemon not running"}, "no-mistakes-daemon"),
        ({"STUB_BOOTSTRAP_RC": "1"}, "bootstrap-exit-1"),
        (
            {"STUB_BOOTSTRAP_OUT": "MISSING: no-mistakes (install: curl ...)"},
            "bootstrap:MISSING",
        ),
        (
            {"STUB_BOOTSTRAP_OUT": "TANGLE: primary on a feature branch"},
            "bootstrap:TANGLE",
        ),
    ],
)
def test_firstmate_fails_on_a_broken_toolchain(rig, env, reason):
    rc, line = smoke(rig, "firstmate", "--expect-no-mistakes-version", "v1.84.0", **env)
    assert rc == 1, line
    assert reason in line


def test_firstmate_fails_on_a_doctor_cross(rig):
    write(rig, "nm-doctor.txt", HEALTHY_NM_DOCTOR.replace("✓ daemon ", "✗ daemon "))
    rc, line = smoke(rig, "firstmate")
    assert rc == 1
    assert "no-mistakes-doctor:daemon" in line


def test_firstmate_fails_on_a_home_script_that_no_longer_parses(rig):
    (rig["home"] / "bin/fm-broken.sh").write_text("if then fi (\n")
    rc, line = smoke(rig, "firstmate")
    assert rc == 1
    assert "home-bin-syntax-1" in line


def test_firstmate_fails_on_a_detached_home(rig):
    subprocess.run(
        ["git", "-C", str(rig["home"]), "checkout", "-q", "--detach"], check=True
    )
    rc, line = smoke(rig, "firstmate")
    assert rc == 1
    assert "home-not-on-branch" in line


def test_firstmate_never_edits_either_checkout(rig):
    def snapshot():
        return {
            str(p.relative_to(rig["tmp"])): p.stat().st_mtime_ns
            for d in (rig["root"], rig["home"])
            for p in d.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    smoke(rig, "firstmate")
    assert snapshot() == before


@pytest.mark.parametrize("pillar", ["openclaw", "claude-code", "firstmate"])
def test_unknown_option_is_a_usage_error(rig, pillar):
    proc = subprocess.run(
        ["bash", str(SMOKE / f"{pillar}.sh"), "--bogus"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
