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
  compose) [ -z "${STUB_COMPOSE_JSON:-}" ] || cat "$STUB_COMPOSE_JSON"; exit 0 ;;
  rmi) exit 0 ;;
  run)
    # Like dockerd: a bind-mount target missing under the writable config
    # mount is created by root, so the runner account cannot delete entries
    # in it (modelled here as mode 0555).
    cfg=$(printf '%s' "$*" | grep -o '[^ ]*:/home/node/.openclaw ' | head -n1 | cut -d: -f1)
    if [ -n "$cfg" ]; then
      for rel in $(printf '%s' "$*" | grep -o ':/home/node/.openclaw/[^ :]*' | cut -c23-) ${STUB_EXTRA_MOUNT:-}; do
        top="$cfg/${rel%%/*}"
        if [ ! -e "$top" ]; then
          mkdir -p "$cfg/$rel" && chmod -R 0555 "$top"
        fi
      done
    fi ;;
esac
args=" $* "
case "$args" in
  *" --entrypoint id "*) echo "${STUB_IMAGE_UID:-$(id -u)}"; exit 0 ;;
  *" --version "*) echo "OpenClaw 2026.9.5 (abc)"; exit 0 ;;
  *" plugins list "*)
    if [ -f "$STUB_STATE/updated" ]; then echo "$STUB_PLUGINS_AFTER"; else echo "$STUB_PLUGINS"; fi
    exit 0 ;;
  *" plugins update "*) touch "$STUB_STATE/updated"; exit 0 ;;
  *" gateway recover-lock --confirm "*)
    [ -z "${STUB_LOCK_CONFIRM_RC:-}" ] || exit "$STUB_LOCK_CONFIRM_RC"
    case "$args" in *" --confirm fp-1234 "*) touch "$STUB_STATE/lock-cleared" ;; *) exit 3 ;; esac
    exit 0 ;;
  *" gateway recover-lock --json "*)
    [ -z "${STUB_LOCK_INSPECT_RC:-}" ] || exit "$STUB_LOCK_INSPECT_RC"
    if [ -f "$STUB_STATE/lock-cleared" ] || [ "${STUB_LOCK_PRESENT:-1}" = 0 ]; then
      echo '{"present":false,"holders":[],"confirmation":""}'
    else
      echo '{"present":true,"holders":["holder 7 at host/pid-ns 1"],"confirmation":"fp-1234","command":"x"}'
    fi
    exit 0 ;;
  *" config validate "*) exit "${STUB_VALIDATE_RC:-0}" ;;
  *" exec "*) exit "${STUB_HEALTH_RC:-0}" ;;
  *" --session-sqlite "*) exit "${STUB_SQLITE_RC:-0}" ;;
  *" doctor --json "*) cat "$STUB_LINT_JSON"; exit 0 ;;
  *" doctor --fix "*)
    # 2026.11.7+: an unverifiable owner is never reclaimed by doctor.
    if [ "${STUB_LOCK_PRESENT:-1}" != 0 ] && [ ! -f "$STUB_STATE/lock-cleared" ]; then exit 1; fi
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
# Like real rsync: a vanished-files exit (24) still leaves the copy in place.
for last; do :; done
if [ "${STUB_RSYNC_RC:-0}" = 0 ] || [ "$STUB_RSYNC_RC" = 24 ]; then
  mkdir -p "$last"
  case "$last" in */config/) cp "$STUB_BEFORE" "$last/openclaw.json" ;; esac
fi
exit "${STUB_RSYNC_RC:-0}"
"""

DU_STUB = """#!/bin/sh
[ -z "${DU_CALL_LOG:-}" ] || printf '%s\\n' "$*" >> "$DU_CALL_LOG"
for last; do :; done
printf '%s\\t%s\\n' "${STUB_DU_KB:-1024}" "$last"
"""

DF_STUB = """#!/bin/sh
printf 'Avail\\n%s\\n' "${STUB_DF_KB:-999999999}"
"""

# The shape `openclaw doctor --json` emits (2026.9.x): the id is `checkId`.
MCP_UNRESOLVED = {
    "checkId": "core/doctor/runtime-tool-schemas",
    "severity": "error",
    "message": 'Configured MCP server "devclaw" could not expose runtime tools.',
    "path": "mcp.servers.devclaw",
    "requirement": "getaddrinfo ENOTFOUND devclaw-mcp",
}
CLAUDE_LOGGED_OUT = {
    "checkId": "core/doctor/claude-cli",
    "severity": "warning",
    "message": "Claude auth: not logged in.",
}
LAN_BIND = {
    "checkId": "core/doctor/security",
    "severity": "warning",
    "message": 'WARNING: Gateway bound to "lan" (0.0.0.0) (network-accessible).',
}
MACHINE_AUTH = {
    "checkId": "core/doctor/node-hosting-preconditions",
    "severity": "warning",
    "message": "Gateway identity-header auth has no configured token/password path for machine clients; "
    "new node hosts cannot authenticate or become worker hosts.",
    "path": "gateway.auth",
    "requirement": "machine-client-auth",
}
READER_EXEC = {
    "checkId": "core/doctor/security",
    "severity": "warning",
    "message": "agents.entries.reader.tools: filesystem write tools are disabled, "
    "but exec is still available.",
    "requirement": "doctor.exec_filesystem_policy",
}
LEGACY_RELAY = {
    "checkId": "core/doctor/browser",
    "severity": "warning",
    "message": "Legacy Browser Relay Authentication is enabled "
    "(browser.extensionRelay.allowLegacyAuth=true).",
}
CLEAN_LINT = {
    "findings": [
        MCP_UNRESOLVED,
        CLAUDE_LOGGED_OUT,
        LAN_BIND,
        MACHINE_AUTH,
        READER_EXEC,
        LEGACY_RELAY,
    ]
}
# `docker compose config --no-interpolate` for the gateway: raw ${...} refs.
COMPOSE = {
    "services": {
        "openclaw-gateway": {
            "environment": {
                "OPENCLAW_FINANCE_CHAT": "${OPENCLAW_FINANCE_CHAT:-${LIFEKIT_TELEGRAM_CHAT:-}}",
                "OPENCLAW_KIT_CHAT": "${OPENCLAW_KIT_CHAT:-${LIFEKIT_TELEGRAM_CHAT:-}}",
                "OPENCLAW_GATEWAY_TOKEN": "${OPENCLAW_GATEWAY_TOKEN}",
                "TZ": "${TZ:-UTC}",
                "HOME": "/home/node",
                "OPENCLAW_STATE_DIR": "/home/node/.openclaw",
            }
        }
    }
}


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
        ("compose.json", COMPOSE),
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
        "STUB_COMPOSE_JSON": str(rig["state"] / "compose.json"),
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


def test_unremovable_leftover_warns_but_keeps_green_verdict(rig):
    out = run(rig, extra_env={"STUB_EXTRA_MOUNT": "stray/mount"})
    assert out.returncode == 0, out.stderr
    assert "GREEN" in out.stdout
    assert "could not fully remove" in out.stderr
    assert not (rig["dest"] / "summary.md").exists()
    (rig["dest"] / "config" / "stray").chmod(0o755)
    (rig["dest"] / "config" / "stray" / "mount").chmod(0o755)


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
        "/tmp/",
        "/.tmp/",
        "/cache/",
        "/.cache/",
        "/openclaw-config-*.tar.gz",
        "*.migrated.*",
    ):
        assert f"--exclude={pat}" in config_call
    assert "--exclude=/agents" not in config_call


def test_vanished_files_exit_is_a_warning_not_a_failure(rig):
    # rsync 24 = files vanished mid-copy of a live tree (the gateway deleting
    # its scratch); the rest of the copy is whole, so the run goes on.
    out = run(rig, extra_env={"STUB_RSYNC_RC": "24"})
    assert out.returncode == 0, out.stderr
    assert "vanished" in out.stderr
    assert "GREEN" in out.stdout
    calls = rig["rsync_log"].read_text().splitlines()
    assert any("/config/" in c for c in calls)
    assert any("/workspace/" in c for c in calls)


@pytest.mark.parametrize("rc", ["1", "11", "23", "30"])
def test_other_rsync_failures_still_fail_the_run(rig, rc):
    out = run(rig, extra_env={"STUB_RSYNC_RC": rc})
    assert out.returncode == int(rc)
    assert "vanished" not in out.stderr


def test_headroom_estimate_skips_excluded_dirs(rig):
    # du matches basenames: a trailing slash would make --exclude a no-op.
    log = rig["tmp"] / "du.log"
    assert run(rig, extra_env={"DU_CALL_LOG": str(log)}).returncode == 0
    est = next(c for c in log.read_text().splitlines() if "--exclude" in c)
    for name in ("tmp", ".tmp", "cache", ".cache", "browser"):
        assert f"--exclude={name} " in est + " "
    assert "--exclude=tmp/" not in est


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


def test_trusted_proxy_mode_blanks_the_placeholder_token(rig):
    # The gateway refuses to start in trusted-proxy mode while a shared token
    # is set; the live overlay (compose/openclaw/trusted-proxy.yml) blanks it.
    live = {"gateway": {"auth": {"mode": "trusted-proxy"}}}
    (rig["state"] / "before.json").write_text(json.dumps(live))
    assert run(rig, "--keep").returncode == 0
    lines = (rig["dest"] / "env.rehearsal").read_text().splitlines()
    assert "OPENCLAW_GATEWAY_TOKEN=" in lines
    assert "OTHER_KEY=rehearsal-placeholder" in lines
    assert (rig["dest"] / "env.rehearsal").stat().st_mode & 0o777 == 0o600


def test_token_mode_keeps_the_placeholder_token(rig):
    live = {"gateway": {"auth": {"mode": "token"}}}
    (rig["state"] / "before.json").write_text(json.dumps(live))
    assert run(rig, "--keep").returncode == 0
    lines = (rig["dest"] / "env.rehearsal").read_text().splitlines()
    assert "OPENCLAW_GATEWAY_TOKEN=rehearsal-placeholder" in lines


def test_env_carries_compose_derived_refs_not_literals(rig):
    assert run(rig, "--keep").returncode == 0
    lines = (rig["dest"] / "env.rehearsal").read_text().splitlines()
    assert "OPENCLAW_FINANCE_CHAT=rehearsal-placeholder" in lines
    assert "OPENCLAW_KIT_CHAT=rehearsal-placeholder" in lines
    assert sum(ln.startswith("OPENCLAW_GATEWAY_TOKEN=") for ln in lines) == 1
    assert not any(ln.startswith(("HOME=", "TZ=")) for ln in lines)
    assert "OPENCLAW_STATE_DIR=/home/node/.openclaw" in lines
    assert "LIFEKIT_TELEGRAM_CHAT" not in "\n".join(lines)


def test_unreadable_compose_warns_and_keeps_env_file_keys(rig):
    out = run(rig, "--keep", extra_env={"STUB_COMPOSE_JSON": ""})
    assert out.returncode == 0, out.stderr
    assert "environment keys" in out.stderr and "Traceback" not in out.stderr
    env_text = (rig["dest"] / "env.rehearsal").read_text()
    assert "OTHER_KEY=rehearsal-placeholder" in env_text


def test_untolerated_finding_is_red_by_id(rig):
    lint = {"findings": [{"id": "config.schema.rejected", "severity": "error"}]}
    (rig["state"] / "lint.json").write_text(json.dumps(lint))
    out = run(rig)
    assert out.returncode != 0
    assert "config.schema.rejected" in out.stdout
    assert rig["dest"].is_dir()


def test_rehearsal_only_classes_are_tolerated_by_check_id(rig):
    out = run(rig)
    assert out.returncode == 0, out.stderr
    assert (
        "mcp_resolution=1 claude_auth=1 gateway_bind=1 machine_client_auth=1 "
        "reader_exec_policy=1 browser_legacy_relay_auth=1" in out.stdout
    )
    assert "untolerated finding ids: -" in out.stdout


@pytest.mark.parametrize(
    "finding",
    [
        # Same check, other security finding: not the by-design bind.
        {
            "checkId": "core/doctor/security",
            "severity": "warning",
            "message": 'Heartbeat agent "x": directPolicy is unset.',
        },
        # Prefix is anchored: the bind text elsewhere in a message is not enough.
        {
            "checkId": "core/doctor/security",
            "severity": "warning",
            "message": 'Also: WARNING: Gateway bound to "lan" (0.0.0.0).',
        },
        # Runtime tool schemas failing for a reason other than resolution.
        {**MCP_UNRESOLVED, "requirement": "tool schema invalid"},
        {**MCP_UNRESOLVED, "path": "tools.allow"},
        # Machine-client auth tolerated only at its exact requirement and path.
        {**MACHINE_AUTH, "requirement": "machine-client-auth-extra"},
        {**MACHINE_AUTH, "path": "gateway.auth.token"},
        {**MACHINE_AUTH, "requirement": "other-precondition"},
        # The exec policy is tolerated for the reader agent only.
        {**READER_EXEC, "message": "agents.entries.finance.tools: exec is available."},
        {**READER_EXEC, "requirement": "doctor.exec_approvals_require_cwd_renewal"},
        # Any other browser finding stays red.
        {**LEGACY_RELAY, "message": "Browser readiness: no Chrome found."},
    ],
)
def test_near_misses_of_tolerated_classes_stay_red(rig, finding):
    (rig["state"] / "lint.json").write_text(json.dumps({"findings": [finding]}))
    out = run(rig)
    assert out.returncode != 0
    assert f"untolerated finding ids: {finding['checkId']}" in out.stdout


def test_plugin_off_core_after_update_is_red(rig):
    stale = json.dumps(
        [{"id": "codex", "enabled": True, "origin": "global", "version": "2026.6.8"}]
    )
    out = run(rig, extra_env={"STUB_PLUGINS": stale, "STUB_PLUGINS_AFTER": stale})
    assert out.returncode != 0
    assert "still off core=1" in out.stdout
    # The fallback targets the core's own version; npm's latest runs ahead of it.
    updates = [c for c in docker_calls(rig) if "plugins update" in c]
    assert updates and all("@openclaw/codex@2026.9.5" in c for c in updates)
    assert "plugins still off core 2026.9.5: codex 2026.6.8" in out.stderr
    assert "red because: plugins-off-core" in out.stderr


def test_untolerated_finding_names_its_requirement_in_the_job_log(rig):
    finding = {
        "checkId": "core/doctor/security",
        "severity": "warning",
        "message": 'Heartbeat agent "x": directPolicy is unset.',
        "requirement": "doctor.heartbeat_direct_policy_unset",
    }
    (rig["state"] / "lint.json").write_text(json.dumps({"findings": [finding]}))
    out = run(rig)
    assert out.returncode != 0
    fid = "core/doctor/security[doctor.heartbeat_direct_policy_unset]"
    assert f"untolerated finding ids: {fid}" in out.stdout
    # stdout is the step summary (sign-in only); stderr is the job log.
    assert "verdict: RED" in out.stderr
    assert "red because: lint-findings" in out.stderr
    assert f"untolerated finding ids: {fid}" in out.stderr


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


def calls(rig):
    return rig["docker_log"].read_text().splitlines()


def test_copied_ownership_record_is_recovered_before_doctor(rig):
    r = run(rig)
    assert r.returncode == 0, r.stderr
    log = calls(rig)
    confirm = next(
        i for i, c in enumerate(log) if "gateway recover-lock --confirm fp-1234" in c
    )
    fix = next(i for i, c in enumerate(log) if "doctor --fix" in c)
    assert confirm < fix
    assert "recover-lock=0" in r.stdout
    # the holder's pid/host from the inspection never reach the summary
    assert "holder 7" not in r.stdout + r.stderr


def test_recover_lock_runs_on_the_copy_never_live_state(rig):
    run(rig)
    for c in calls(rig):
        if "recover-lock" in c:
            assert f"{rig['dest']}/config:/home/node/.openclaw " in c
            assert f"{rig['tmp']}/config:" not in c


def test_no_lock_present_skips_the_confirm(rig):
    r = run(rig, extra_env={"STUB_LOCK_PRESENT": "0"})
    assert r.returncode == 0, r.stderr
    assert not any("--confirm" in c for c in calls(rig))


def test_failed_lock_recovery_is_red_and_keeps_the_copy(rig):
    r = run(rig, extra_env={"STUB_LOCK_CONFIRM_RC": "4"})
    assert r.returncode == 1
    assert "recover-lock-exit-4" in r.stdout
    assert rig["dest"].exists()


def test_unparseable_inspection_is_red(rig):
    r = run(rig, extra_env={"STUB_LOCK_INSPECT_RC": "2"})
    assert r.returncode == 1
    assert "recover-lock-exit-2" in r.stdout


def test_migrated_copy_is_validated_and_gateway_started_on_the_copy(rig):
    r = run(rig, extra_env={"PROBE_WAIT": "4"})
    assert r.returncode == 0, r.stderr
    log = calls(rig)
    assert any("config validate --json" in c for c in log)
    start = next(c for c in log if c.startswith("run -d "))
    assert f"{rig['dest']}/config:/home/node/.openclaw " in start
    assert "gateway run" in start and " -p " not in start and "--publish" not in start
    assert any(c.startswith("rm -f rehearsal-gateway-") for c in log)
    assert "config-validate=0 gateway-start=0" in r.stdout


def test_invalid_migrated_config_is_red(rig):
    r = run(rig, extra_env={"STUB_VALIDATE_RC": "1", "PROBE_WAIT": "4"})
    assert r.returncode == 1
    assert "config-invalid-exit-1" in r.stdout


def test_gateway_that_never_answers_is_red_and_container_removed(rig):
    r = run(rig, extra_env={"STUB_HEALTH_RC": "1", "PROBE_WAIT": "2"})
    assert r.returncode == 1
    assert "gateway-start-failed" in r.stdout
    assert any(c.startswith("rm -f rehearsal-gateway-") for c in calls(rig))
