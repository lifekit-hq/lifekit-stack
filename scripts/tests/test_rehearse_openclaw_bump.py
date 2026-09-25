"""Behaviour of scripts/rehearse-openclaw-bump.sh, run as a subprocess against
stubbed `docker`, `rsync`, `df` and `du` on PATH. Nothing here builds an image
or touches real state.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/rehearse-openclaw-bump.sh"

# Fixture values that must never appear in the summary.
SECRET_TOKEN = "fixture-" + "value-" + "aaaa"
CHAT_ID = "987654321012"
BEFORE = {
    "gateway": {"auth": {"token": SECRET_TOKEN}},
    "channels": {CHAT_ID: {"name": "private-chat-name"}},
}
AFTER = {
    "gateway": {"auth": {"token": SECRET_TOKEN}, "trustedProxies": ["10.99.88.77"]},
    "channels": {CHAT_ID: {"name": "renamed-private-chat"}},
}

DOCKER_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_CALL_LOG"
case "$1" in
  build) exit "${STUB_BUILD_RC:-0}" ;;
  rmi) exit 0 ;;
esac
args=" $* "
case "$args" in
  *" --entrypoint id "*) echo "${STUB_IMAGE_UID:-$(id -u)}"; exit 0 ;;
  *" --version "*) echo "OpenClaw 2026.9.5 (abc)"; exit 0 ;;
  *" plugins list "*)
    if [ -f "$STUB_STATE/updated" ]; then echo "$STUB_PLUGINS_AFTER"; else echo "$STUB_PLUGINS"; fi
    exit 0 ;;
  *" plugins update "*) touch "$STUB_STATE/updated"; exit 0 ;;
  *" --session-sqlite "*) exit "${STUB_SQLITE_RC:-0}" ;;
  *" doctor --json "*) cat "$STUB_LINT_JSON"; exit 0 ;;
  *" doctor --fix "*)
    cfg=$(printf '%s' "$*" | grep -o '[^ ]*:/home/node/.openclaw ' | head -n1 | cut -d: -f1)
    if [ -n "$cfg" ] && [ ! -f "$STUB_STATE/fixed" ]; then
      cp "$STUB_AFTER" "$cfg/openclaw.json"
      touch "$STUB_STATE/fixed"
    fi
    exit "${STUB_FIX_RC:-0}" ;;
esac
exit 0
"""

RSYNC_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$RSYNC_CALL_LOG"
[ "${STUB_RSYNC_RC:-0}" = 0 ] || exit "$STUB_RSYNC_RC"
for last; do :; done
mkdir -p "$last"
case "$last" in */config/) cp "$STUB_BEFORE" "$last/openclaw.json" ;; esac
exit 0
"""

DU_STUB = """#!/bin/sh
for last; do :; done
printf '%s\\t%s\\n' "${STUB_DU_KB:-1024}" "$last"
"""

DF_STUB = """#!/bin/sh
printf 'Avail\\n%s\\n' "${STUB_DF_KB:-999999999}"
"""

CLEAN_LINT = {"findings": [{"id": "mcp.server.unreachable", "severity": "warn"}]}


@pytest.fixture
def rig(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("docker", DOCKER_STUB),
        ("rsync", RSYNC_STUB),
        ("du", DU_STUB),
        ("df", DF_STUB),
    ):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    state = tmp_path / "stubstate"
    state.mkdir()
    for name, data in (
        ("before.json", BEFORE),
        ("after.json", AFTER),
        ("lint.json", CLEAN_LINT),
    ):
        (state / name).write_text(json.dumps(data))
    env_file = tmp_path / "live.env"
    env_file.write_text(f"OPENCLAW_GATEWAY_TOKEN={SECRET_TOKEN}\nOTHER_KEY=hunter2\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "workspace").mkdir()
    (tmp_path / "docker.log").write_text("")
    (tmp_path / "rsync.log").write_text("")
    return {
        "tmp": tmp_path,
        "bin": bin_dir,
        "state": state,
        "dest": tmp_path / "rehearsal" / "run1",
        "docker_log": tmp_path / "docker.log",
        "rsync_log": tmp_path / "rsync.log",
        "env_file": env_file,
    }


def run(rig, *args, extra_env=None):
    plugins = json.dumps(
        [{"id": "codex", "enabled": True, "origin": "global", "version": "2026.9.5"}]
    )
    env = {
        **os.environ,
        "PATH": f"{rig['bin']}:{os.environ['PATH']}",
        "DOCKER_CALL_LOG": str(rig["docker_log"]),
        "RSYNC_CALL_LOG": str(rig["rsync_log"]),
        "STUB_STATE": str(rig["state"]),
        "STUB_BEFORE": str(rig["state"] / "before.json"),
        "STUB_AFTER": str(rig["state"] / "after.json"),
        "STUB_LINT_JSON": str(rig["state"] / "lint.json"),
        "STUB_PLUGINS": plugins,
        "STUB_PLUGINS_AFTER": plugins,
        "ENV_FILE": str(rig["env_file"]),
        "REHEARSAL_ROOT": str(rig["tmp"] / "rehearsal"),
        **(extra_env or {}),
    }
    cmd = [
        "bash",
        str(SCRIPT),
        "--state",
        str(rig["tmp"] / "config"),
        "--workspace",
        str(rig["tmp"] / "workspace"),
        "--dest",
        str(rig["dest"]),
        *args,
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)


def docker_calls(rig):
    return rig["docker_log"].read_text().splitlines()


def test_help_documents_every_flag():
    out = subprocess.run(
        ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0
    for flag in (
        "--image",
        "--state",
        "--workspace",
        "--dest",
        "--lint-only",
        "--baseline",
        "--keep",
    ):
        assert flag in out.stdout


def test_green_run_builds_rehearse_tag_and_cleans_up(rig):
    out = run(rig)
    assert out.returncode == 0, out.stderr
    calls = docker_calls(rig)
    build = [c for c in calls if c.startswith("build")]
    assert len(build) == 1 and "-t lifekit-openclaw:rehearse " in build[0] + " "
    assert "rmi lifekit-openclaw:rehearse" in calls
    assert not any(":local" in c or ":prev" in c for c in calls)
    assert not rig["dest"].exists()
    assert "GREEN" in out.stdout


def test_exclude_list_reaches_rsync(rig):
    assert run(rig).returncode == 0
    config_call = next(
        c for c in rig["rsync_log"].read_text().splitlines() if "/config/" in c
    )
    for pat in (
        "/browser/",
        "/tools/",
        "/logs/",
        "/workspace/",
        "/wiki/",
        "/backups/",
        "/openclaw-config-*.tar.gz",
        "*.migrated.*",
    ):
        assert f"--exclude={pat}" in config_call
    assert "--exclude=/agents" not in config_call


def test_aborts_before_copy_when_headroom_short(rig):
    gib25 = 25 * 1024 * 1024
    out = run(
        rig, extra_env={"STUB_DU_KB": "1000", "STUB_DF_KB": str(gib25 + 2000 - 1)}
    )
    assert out.returncode != 0
    assert "headroom" in out.stderr
    assert rig["rsync_log"].read_text() == ""
    assert not any(c.startswith("build") for c in docker_calls(rig))
    assert not rig["dest"].exists()


def test_headroom_exactly_enough_proceeds(rig):
    gib25 = 25 * 1024 * 1024
    out = run(rig, extra_env={"STUB_DU_KB": "1000", "STUB_DF_KB": str(gib25 + 2000)})
    assert out.returncode == 0, out.stderr


def test_forced_failure_removes_copy_and_tag(rig):
    out = run(rig, extra_env={"STUB_RSYNC_RC": "23"})
    assert out.returncode != 0
    assert not rig["dest"].exists()
    assert "rmi lifekit-openclaw:rehearse" in docker_calls(rig)


def test_fix_pass_failure_keeps_copy_removes_tag_names_path(rig):
    out = run(rig, extra_env={"STUB_FIX_RC": "1"})
    assert out.returncode != 0
    assert rig["dest"].is_dir()
    assert "RED" in out.stdout
    assert str(rig["dest"]) in out.stdout
    assert "rmi lifekit-openclaw:rehearse" in docker_calls(rig)
    assert not any(":local" in c or ":prev" in c for c in docker_calls(rig))


def test_summary_has_key_paths_and_no_values(rig):
    out = run(rig, "--keep")
    assert out.returncode == 0, out.stderr
    summary = (rig["dest"] / "summary.md").read_text()
    assert (
        summary == out.stdout.rstrip("\n") + "\n"
        or summary.strip() == out.stdout.strip()
    )
    assert "gateway.trustedProxies[0]" in summary
    assert "channels.<id>.name" in summary
    for value in (
        SECRET_TOKEN,
        CHAT_ID,
        "10.99.88.77",
        "private-chat-name",
        "renamed-private-chat",
        "hunter2",
    ):
        assert value not in summary
        assert value not in out.stderr


def test_env_file_carries_keys_with_placeholders_only(rig):
    assert run(rig, "--keep").returncode == 0
    env_text = (rig["dest"] / "env.rehearsal").read_text()
    assert "OPENCLAW_GATEWAY_TOKEN=rehearsal-placeholder" in env_text
    assert "OTHER_KEY=rehearsal-placeholder" in env_text
    assert SECRET_TOKEN not in env_text and "hunter2" not in env_text
    assert (rig["dest"] / "env.rehearsal").stat().st_mode & 0o777 == 0o600


def test_untolerated_finding_is_red_by_id(rig):
    lint = {"findings": [{"id": "config.schema.rejected", "severity": "error"}]}
    (rig["state"] / "lint.json").write_text(json.dumps(lint))
    out = run(rig)
    assert out.returncode != 0
    assert "config.schema.rejected" in out.stdout
    assert rig["dest"].is_dir()


def test_plugin_off_core_after_update_is_red(rig):
    stale = json.dumps(
        [{"id": "codex", "enabled": True, "origin": "global", "version": "2026.6.8"}]
    )
    out = run(rig, extra_env={"STUB_PLUGINS": stale, "STUB_PLUGINS_AFTER": stale})
    assert out.returncode != 0
    assert "still off core=1" in out.stdout


def test_lint_only_mounts_read_only_and_does_not_copy(rig):
    out = run(rig, "--image", "some/image:tag", "--lint-only")
    assert out.returncode == 0, out.stderr
    assert rig["rsync_log"].read_text() == ""
    calls = docker_calls(rig)
    assert not any(c.startswith("build") or c.startswith("rmi") for c in calls)
    doctor = next(c for c in calls if "doctor --json" in c)
    assert ":/home/node/.openclaw:ro" in doctor
    assert "mcp_resolution=1" in out.stdout


def test_baseline_not_implemented(rig):
    out = run(rig, "--baseline", "img:x")
    assert out.returncode == 2
