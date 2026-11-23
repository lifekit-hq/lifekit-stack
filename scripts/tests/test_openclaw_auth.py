"""OpenClaw auth mode: scripts/openclaw-auth/auth-mode.sh and spend_guard.py.

Both talk to the gateway only through `docker`, so the tests put a stateful
fake `docker` first on PATH. It keeps a config.json (the agents + auth halves
of openclaw.json), applies `config patch` as the real CLI documents it (objects
merge, arrays replace, null deletes), answers `models status`, runs a "turn"
on whichever runtime the config selects, and serves the key probe and the
usage extractor from canned data. Every call is logged to _calls.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/openclaw-auth"
AUTH_MODE = DIR / "auth-mode.sh"
GUARD_SH = DIR / "lifekit-openclaw-spend-guard.sh"

_SPEC = importlib.util.spec_from_file_location("spend_guard", DIR / "spend_guard.py")
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
state = os.environ["STATE_DIR"]
args = sys.argv[1:]
with open(f"{state}/_calls", "a") as log:
    log.write(" ".join(args) + "\n")

def load(name, default=None):
    try:
        with open(f"{state}/{name}") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save(name, value):
    with open(f"{state}/{name}", "w") as f:
        json.dump(value, f)

def merge(dst, patch):
    for key, value in patch.items():
        if value is None:
            dst.pop(key, None)
        elif isinstance(value, dict):
            merge(dst.setdefault(key, {}), value)
        else:
            dst[key] = value

def flag(name):
    return os.path.exists(f"{state}/{name}")

def schema_errors(config):
    # the slice of the live schema the rollback bug hit: every auth profile is a
    # complete object with a provider and a mode from the enum
    errors = []
    for pid, prof in config.get("auth", {}).get("profiles", {}).items():
        if not isinstance(prof, dict) or not isinstance(prof.get("provider"), str):
            errors.append(f"auth.profiles.{pid}.provider undefined")
        elif prof.get("mode") not in ("api_key", "aws-sdk", "oauth", "token"):
            errors.append(f"auth.profiles.{pid}.mode invalid")
    return errors

if args[0] == "compose":
    sub = args[args.index("openclaw") + 1:]
    if "stop" in sub:
        open(f"{state}/_stopped", "w").close()
    elif "up" in sub:
        if os.path.exists(f"{state}/_up_fails"):
            sys.exit(1)
        os.remove(f"{state}/_stopped")
    elif "run" in sub:
        if flag("_offline_fails"):
            print("boom", file=sys.stderr)
            sys.exit(1)
        if "--dry-run" not in sub:
            profiles = load("profiles", {})
            for agent in load("plan_agents", []):
                profiles.setdefault(agent, {})["profile"] = True
            save("profiles", profiles)
    else:
        sys.exit(3)
elif args[0] == "ps":
    print("cid123")
elif args[0] == "restart":
    pass
elif args[0] == "inspect":
    print("healthy")
elif args[0] == "exec":
    rest = args[1:]
    while rest[0] in ("-i", "-e"):
        rest = rest[2:] if rest[0] == "-e" else rest[1:]
    assert rest[0] == "cid123", rest
    rest = rest[1:]
    if rest[0] == "python3":      # key-probe.py on stdin
        sys.stdin.read()
        print(json.dumps({"result": (load("probe") or "ok"), "http": 200, "detail": ""}))
    elif rest[0] == "node":       # usage-extract.mjs on stdin
        sys.stdin.read()
        if flag("_usage_fails"):
            print("no such table: transcript_events", file=sys.stderr)
            sys.exit(1)
        for rec in load("usage", []):
            print(json.dumps(rec))
    elif rest[0] == "sh":
        sys.stdin.read()
    elif rest[0] == "rm":
        pass
    elif rest[0] == "openclaw":
        cmd = rest[1:]
        config = load("config.json")
        if (cmd[:2] == ["config", "get"] and flag("_reads_fail")) or (
            cmd[:2] == ["models", "status"] and flag("_status_fails")
        ):
            print("gateway busy", file=sys.stderr)
            sys.exit(1)
        if cmd[:2] == ["config", "get"] and flag("_reads_fail_once") and not flag("_failed_" + cmd[2]):
            open(f"{state}/_failed_{cmd[2]}", "w").close()
            print("slow SQLite transaction", file=sys.stderr)
            sys.exit(1)
        if cmd[:2] == ["config", "get"]:
            print(json.dumps(config[cmd[2]]))
        elif cmd[:2] == ["config", "patch"]:
            patch = json.load(sys.stdin)
            if flag("_patch_rejects"):
                print("schema error", file=sys.stderr)
                sys.exit(1)
            candidate = json.loads(json.dumps(config))
            merge(candidate, patch)
            errors = schema_errors(candidate)
            if errors:
                print("schema error: " + "; ".join(errors), file=sys.stderr)
                sys.exit(1)
            if "--dry-run" not in cmd:
                merge(config, patch)
                save("config.json", config)
                if flag("_restart_hint"):
                    print("Restart the gateway to apply.")
        elif cmd[:2] == ["models", "status"]:
            agent = cmd[cmd.index("--agent") + 1]
            profiles = load("profiles", {})
            info = profiles.get(agent, {})
            ids = ["openai:me"] + (["anthropic:api-key"] if info.get("profile") else [])
            print(json.dumps({
                "auth": {
                    "oauth": {"profiles": [{"profileId": i} for i in ids]},
                    "unusableProfiles": [
                        {"profileId": "anthropic:api-key", "reason": r}
                        for r in info.get("disabled", [])
                    ],
                }
            }))
        elif cmd[:2] == ["secrets", "apply"]:
            if flag("_secrets_fail"):
                sys.exit(1)
            if flag("_busy") and "--dry-run" not in cmd and not flag("_stopped"):
                print("OpenClaw state database is busy. A running Gateway can hold this ownership until it stops", file=sys.stderr)
                sys.exit(1)
            if "--dry-run" not in cmd:
                profiles = load("profiles", {})
                for agent in load("plan_agents", []):
                    profiles.setdefault(agent, {})["profile"] = True
                save("profiles", profiles)
        elif cmd[:2] == ["secrets", "reload"]:
            if flag("_reload_fails"):
                sys.exit(1)
            if flag("_reload_fails_once") and not flag("_reload_failed"):
                open(f"{state}/_reload_failed", "w").close()
                sys.exit(1)
        elif cmd[0] == "agent":
            agent = cmd[cmd.index("--agent") + 1]
            if cmd[cmd.index("--thinking") + 1] == "off":
                # claude-opus-5-5 on the direct runtime: "Thinking level off is not supported"
                print(json.dumps({"status": "error"}))
                sys.exit(1)
            model = config["agents"]["entries"][agent]["model"]
            model = model if isinstance(model, str) else model["primary"]
            models = config["agents"]["entries"][agent].get("models") or config["agents"]["defaults"]["models"]
            runtime = models[model]["agentRuntime"]["id"]
            provider = "anthropic" if runtime == "openclaw" else "claude-cli"
            if flag("_turn_fails_on_" + provider):
                print(json.dumps({"status": "error"}))
            else:
                print(json.dumps({"status": "ok", "result": {"meta": {"executionTrace": {"winnerProvider": provider}}}}))
        else:
            sys.exit(3)
    else:
        sys.exit(3)
else:
    sys.exit(3)
"""

SUBSCRIPTION_CONFIG = {
    "agents": {
        "defaults": {
            "model": {"primary": "anthropic/claude-sonnet-4-6", "fallbacks": []},
            "models": {
                "anthropic/claude-sonnet-4-6": {"agentRuntime": {"id": "claude-cli"}},
                "anthropic/claude-opus-4-8": {"agentRuntime": {"id": "claude-cli"}},
                "anthropic/claude-haiku-4-5": {"agentRuntime": {"id": "claude-cli"}},
                "openai/gpt-5.5": {"agentRuntime": {"id": "openclaw"}},
            },
        },
        "entries": {
            "kit": {
                "model": {"primary": "anthropic/claude-sonnet-4-6", "fallbacks": []},
                "heartbeat": {"model": "anthropic/claude-haiku-4-5"},
                "models": {
                    "anthropic/claude-sonnet-4-6": {
                        "agentRuntime": {"id": "claude-cli"}
                    },
                },
            },
            "finance": {"model": "anthropic/claude-sonnet-4-6"},
        },
    },
    "auth": {
        "profiles": {"anthropic:cli": {"provider": "anthropic", "mode": "token"}},
        "order": {"anthropic": [], "openai": ["openai:me"]},
    },
}

CREDIT = {"credit_usd": 100, "since": "2026-11-14T00:00:00Z", "fallback_percent": 95}
NOW_MS = int(time.mktime(time.strptime("2026-11-20", "%Y-%m-%d"))) * 1000
DAY_MS = 86_400_000


@pytest.fixture
def box(tmp_path):
    state, bin_dir, out = tmp_path / "state", tmp_path / "bin", tmp_path / "out"
    spend, conf = tmp_path / "spend", tmp_path / "conf"
    for d in (state, bin_dir, out, spend, conf):
        d.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    (conf / "credit.json").write_text(json.dumps(CREDIT))
    (conf / "prices.json").write_text((DIR / "prices.json").read_text())
    (state / "config.json").write_text(json.dumps(SUBSCRIPTION_CONFIG))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_DIR": str(state),
        "AUTH_MODE_SLEEP": "0",
        "AUTH_MODE_HEALTH_WAIT": "0",
        "AUTH_MODE_READ_RETRY_SLEEP": "0",
        "AUTH_CHECK_AGENTS": "kit,finance",
        "OPENCLAW_SPEND_STATE_DIR": str(spend),
        "OPENCLAW_COMPOSE_AUTH_MODE": "token",
    }

    def put(name, value):
        (state / name).write_text(json.dumps(value))

    def flag(name):
        (state / name).write_text("")

    def config():
        return json.loads((state / "config.json").read_text())

    def calls():
        path = state / "_calls"
        return path.read_text().splitlines() if path.exists() else []

    def auth(*args, expect=0, env_extra=None):
        proc = subprocess.run(
            ["bash", str(AUTH_MODE), *args],
            env={**env, **(env_extra or {})},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == expect, proc.stdout + proc.stderr
        return proc

    def run_guard(*extra, expect=0):
        proc = subprocess.run(
            [
                "python3",
                str(DIR / "spend_guard.py"),
                "run",
                "--config-dir",
                str(conf),
                "--state-dir",
                str(spend),
                "--textfile",
                str(out),
                "--now-ms",
                str(NOW_MS),
                *extra,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == expect, proc.stdout + proc.stderr
        return proc

    def prom():
        return [
            ln
            for ln in (out / "openclaw_api_spend.prom").read_text().splitlines()
            if not ln.startswith("#")
        ]

    def set_mode(mode):
        runtime = "openclaw" if mode == "api" else "claude-cli"
        cfg = config()
        for models in [
            cfg["agents"]["defaults"]["models"],
            cfg["agents"]["entries"]["kit"]["models"],
        ]:
            for key in models:
                if key.startswith("anthropic/"):
                    models[key] = {"agentRuntime": {"id": runtime}}
        cfg["auth"]["order"]["anthropic"] = (
            ["anthropic:api-key"] if mode == "api" else []
        )
        (state / "config.json").write_text(json.dumps(cfg))

    return SimpleNamespace(
        state=state, spend=spend, out=out, put=put, flag=flag, config=config,
        calls=calls, auth=auth, run_guard=run_guard, prom=prom, set_mode=set_mode,
        env=env,
    )  # fmt: skip


def usage_record(i, model="claude-sonnet-4-6", ts=NOW_MS - DAY_MS, **tokens):
    rec = {"id": f"e{i}", "agent": "kit", "ts": ts, "model": model}
    rec |= {"in": 0, "out": 0, "cr": 0, "cw": 0} | tokens
    return rec


# ── spend math ────────────────────────────────────────────────────────────────

PRICES = json.loads((DIR / "prices.json").read_text())


def test_price_table_matches_the_documented_list_prices():
    assert guard.price_for("claude-sonnet-4-6", PRICES) == (3.0, 15.0, True)
    assert guard.price_for("claude-opus-4-8", PRICES) == (5.0, 25.0, True)
    assert guard.price_for("claude-opus-5-5", PRICES) == (5.0, 25.0, True)
    assert guard.price_for("claude-haiku-4-5", PRICES) == (1.0, 5.0, True)
    assert guard.price_for("claude-fable-5-1", PRICES) == (5.0, 25.0, False)


def test_cache_tokens_are_priced_at_their_multipliers():
    rec = usage_record(
        1, **{"in": 1_000_000, "out": 1_000_000, "cw": 1_000_000, "cr": 1_000_000}
    )
    usd, matched = guard.record_usd(rec, PRICES)
    # sonnet: 3 in + 15 out + 3*1.25 cache write + 3*0.1 cache read
    assert matched
    assert usd == pytest.approx(3 + 15 + 3.75 + 0.3)


def test_windows_since_topup_month_and_unpriced():
    credit = {**CREDIT, "since_ms": guard.parse_time(CREDIT["since"])}
    records = [
        usage_record(1, ts=guard.parse_time("2026-11-10T00:00:00Z"), out=1_000_000),
        usage_record(2, ts=guard.parse_time("2026-11-15T00:00:00Z"), out=1_000_000),
        usage_record(3, model="claude-fable-5-1", ts=guard.parse_time("2026-11-16T00:00:00Z"), out=1_000_000),
        usage_record(4, ts=guard.parse_time("2026-10-30T00:00:00Z"), out=1_000_000),
    ]  # fmt: skip
    est = guard.summarize(records, credit, PRICES, NOW_MS)
    assert est["since_topup_usd"] == pytest.approx(15 + 25)  # records 2 and 3
    assert est["month_usd"] == pytest.approx(15 + 15 + 25)  # records 1-3, not October
    assert est["used_percent"] == pytest.approx(40)
    assert est["unpriced_tokens"] == 1_000_000


@pytest.mark.parametrize(
    ("mode", "pct", "billing", "probe", "want"),
    [
        ("api", 10, False, "ok", None),
        ("api", 94.9, False, "limited", None),
        ("api", 95, False, "ok", "spend"),
        ("api", 10, True, "ok", "billing"),
        ("api", 10, False, "rejected", "key-refused"),
        ("api", 10, False, "exhausted", "key-refused"),
        ("api", 10, False, "unknown", None),
        ("subscription", 99, True, None, None),
        ("mixed", 99, False, None, None),
    ],
)
def test_decide(mode, pct, billing, probe, want):
    est = {"used_percent": pct, "fallback_percent": 95.0}
    assert guard.decide(mode, est, billing, probe) == want


def test_ledger_dedupes_and_survives_pruned_sessions(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = guard.read_ledger(path)
    guard.merge_ledger(path, ledger, [usage_record(1), usage_record(2)])
    guard.merge_ledger(path, ledger, [usage_record(2), usage_record(3)])
    again = guard.read_ledger(path)
    assert sorted(r["id"] for r in again.values()) == ["e1", "e2", "e3"]
    assert len(path.read_text().splitlines()) == 3
    path.write_text(path.read_text() + '{"torn')  # a torn last line is skipped
    assert len(guard.read_ledger(path)) == 3


# ── the config files ──────────────────────────────────────────────────────────


def test_shipped_config_files_load():
    credit, prices = guard.load_config(DIR)
    assert credit["credit_usd"] == 100
    assert credit["fallback_percent"] == 95
    assert prices["cache_write_multiplier"] == 1.25
    assert prices["cache_read_multiplier"] == 0.1


# ── auth-mode.sh ──────────────────────────────────────────────────────────────


def test_status_reports_subscription_and_missing_profiles(box):
    out = json.loads(box.auth("status", "--json", "--quick").stdout)
    assert out["mode"] == "subscription"
    assert out["runtimes"] == {"claude-cli": 4}  # three defaults entries + kit's own
    assert out["authOrder"] == []
    assert {a["agent"]: a["profile"] for a in out["agents"]} == {
        "kit": False,
        "finance": False,
    }
    assert out["billingDisabled"] is False


def test_status_full_includes_probe_and_spend(box):
    box.put("probe", "rejected")
    proc = box.auth("status")
    assert "mode: subscription" in proc.stdout
    assert "auth.order.anthropic: (empty)" in proc.stdout
    assert "agent kit: key profile missing" in proc.stdout
    assert "key probe: rejected (http 200)" in proc.stdout
    assert "of $100" in proc.stdout


def test_api_refuses_a_rejected_key_and_changes_nothing(box):
    box.put("probe", "rejected")
    before = box.config()
    proc = box.auth("api", expect=2)
    assert "scripts/secrets/edit.sh gateway" in proc.stderr
    assert box.config() == before
    assert not any(
        "config patch --stdin" in c and "--dry-run" not in c for c in box.calls()
    )


def test_api_switches_every_anthropic_entry_and_verifies_each_agent(box):
    proc = box.auth("api")
    cfg = box.config()
    assert cfg["auth"]["order"]["anthropic"] == ["anthropic:api-key"]
    assert cfg["auth"]["profiles"]["anthropic:api-key"] == {
        "provider": "anthropic",
        "mode": "api_key",
    }
    assert cfg["auth"]["profiles"]["anthropic:cli"]["mode"] == "token"  # untouched
    assert cfg["auth"]["order"]["openai"] == ["openai:me"]
    defaults = cfg["agents"]["defaults"]["models"]
    assert {m: v["agentRuntime"]["id"] for m, v in defaults.items()} == {
        "anthropic/claude-sonnet-4-6": "openclaw",
        "anthropic/claude-opus-4-8": "openclaw",
        "anthropic/claude-haiku-4-5": "openclaw",
        "openai/gpt-5.5": "openclaw",
    }
    kit = cfg["agents"]["entries"]["kit"]["models"]
    assert set(kit) == {"anthropic/claude-sonnet-4-6"}
    assert all(v["agentRuntime"]["id"] == "openclaw" for v in kit.values())
    assert "models" not in cfg["agents"]["entries"]["finance"]  # follows the defaults
    assert "verification turn on kit: ok on anthropic" in proc.stderr
    assert "verification turn on finance: ok on anthropic" in proc.stderr
    assert "no restart" in proc.stderr
    assert not any(c.startswith("restart") for c in box.calls())
    assert json.loads(box.auth("status", "--json", "--quick").stdout)["mode"] == "api"


def test_api_adds_the_secretref_profile_to_agents_lacking_it(box):
    box.put("plan_agents", ["kit", "finance"])
    box.auth("api")
    calls = box.calls()
    assert (
        sum(
            "secrets apply --from /tmp/auth-mode-plan.json --allow-exec" in c
            for c in calls
        )
        == 2
    )  # dry run + real
    assert any("secrets reload" in c for c in calls)
    status = json.loads(box.auth("status", "--json", "--quick").stdout)
    assert all(a["profile"] for a in status["agents"])
    # no key value can be in any call: only the ref id travels
    assert not any("sk-ant" in c for c in calls)


def test_api_names_the_offline_step_when_the_gateway_holds_the_database(box):
    box.put("plan_agents", ["kit", "finance"])
    box.flag("_busy")
    proc = box.auth("api", expect=1)
    assert "holds OpenClaw's state database" in proc.stderr
    assert f"bash {AUTH_MODE} offline-profiles" in proc.stderr
    assert "docker compose" not in "\n".join(box.calls())  # api never stops anything
    assert box.config()["auth"]["order"]["anthropic"] == []  # no half switch


def test_a_plain_apply_failure_is_not_called_busy(box):
    box.flag("_secrets_fail")
    proc = box.auth("api", expect=1)
    assert "offline-profiles" not in proc.stderr
    assert "secrets apply failed" in proc.stderr


def test_offline_profiles_stops_applies_in_a_one_shot_and_restarts(box):
    box.put("plan_agents", ["kit", "finance"])
    box.flag("_busy")
    box.auth("offline-profiles")
    calls = [c for c in box.calls() if c.startswith("compose")]
    verbs = ["stop" if " stop " in c else "up" if " up " in c else "run" for c in calls]
    assert verbs == ["stop", "run", "run", "up"]
    assert "--profile cli stop openclaw-cli openclaw-gateway" in calls[0]
    for c in calls[1:3]:
        assert (
            "run --rm --no-deps -T" in c
            and "--entrypoint openclaw openclaw-gateway secrets apply" in c
        )
    assert "--dry-run" in calls[1] and "--dry-run" not in calls[2]
    assert any("secrets reload" in c for c in box.calls())
    assert not (box.state / "_stopped").exists()
    assert not any("sk-ant" in c for c in box.calls())
    status = json.loads(box.auth("status", "--json", "--quick").stdout)
    assert all(a["profile"] for a in status["agents"])
    box.auth("api")  # the profiles are in place: api now completes


def test_offline_profiles_waits_for_health_before_the_reload(box):
    box.put("plan_agents", ["kit", "finance"])
    box.flag("_busy")
    box.auth("offline-profiles")
    calls = box.calls()
    up = next(i for i, c in enumerate(calls) if c.startswith("compose") and " up " in c)
    reload_ = next(i for i, c in enumerate(calls) if "secrets reload" in c)
    assert any(c.startswith("inspect") for c in calls[up:reload_])


def test_offline_profiles_retries_the_reload_once(box):
    box.put("plan_agents", ["kit", "finance"])
    box.flag("_busy")
    box.flag("_reload_fails_once")
    proc = box.auth("offline-profiles")
    assert sum("secrets reload" in c for c in box.calls()) == 2
    assert "secrets reload failed" not in proc.stderr
    assert "the key profile is in place" in proc.stderr


def test_offline_profiles_reports_a_reload_that_keeps_failing(box):
    box.put("plan_agents", ["kit", "finance"])
    box.flag("_busy")
    box.flag("_reload_fails")
    proc = box.auth("offline-profiles", expect=1)
    assert sum("secrets reload" in c for c in box.calls()) == 2
    assert "secrets reload failed" in proc.stderr


def test_offline_profiles_starts_the_gateway_again_when_the_apply_fails(box):
    box.flag("_offline_fails")
    proc = box.auth("offline-profiles", expect=1)
    assert "started again" in proc.stderr
    assert any(" up " in c for c in box.calls() if c.startswith("compose"))
    assert not (box.state / "_stopped").exists()


def test_offline_profiles_refuses_before_stopping_when_the_config_is_unreadable(
    box, tmp_path
):
    conf = tmp_path / "unreadable"
    (conf / "openclaw.json").mkdir(parents=True)  # reading it fails for any account
    box.put("plan_agents", ["kit", "finance"])
    proc = box.auth(
        "offline-profiles",
        expect=2,
        env_extra={
            "OPENCLAW_COMPOSE_AUTH_MODE": "",
            "OPENCLAW_CONFIG_DIR": str(conf),
        },
    )
    assert "run as the deploy account (lifekit) or root" in proc.stderr
    assert "started again" not in proc.stderr
    assert "COULD NOT START" not in proc.stderr
    assert not [c for c in box.calls() if c.startswith("compose")]


def test_offline_profiles_does_nothing_when_every_agent_has_the_profile(box):
    box.put("profiles", {"kit": {"profile": True}, "finance": {"profile": True}})
    box.auth("offline-profiles")
    assert not [c for c in box.calls() if c.startswith("compose")]


def test_api_skips_profiles_that_already_exist(box):
    box.put("profiles", {"kit": {"profile": True}, "finance": {"profile": True}})
    box.auth("api")
    assert not any("secrets apply" in c for c in box.calls())


def test_gateway_restarts_only_when_the_apply_hint_says_so(box):
    box.flag("_restart_hint")
    proc = box.auth("api")
    assert "restarting openclaw-gateway" in proc.stderr
    assert sum(c == "restart cid123" for c in box.calls()) == 1


def test_failed_verification_rolls_the_config_back(box):
    before = box.config()
    box.flag("_turn_fails_on_anthropic")
    proc = box.auth("api", expect=1)
    assert "rolling back to subscription mode" in proc.stderr
    assert box.config() == before
    assert "ROLLBACK FAILED" not in proc.stderr
    assert box.calls()[-1] != ""  # the rollback patch was applied, not skipped


def test_check_turns_use_a_thinking_level_every_model_accepts(box):
    proc = box.auth("api")
    turns = [c for c in box.calls() if " agent --agent " in c]
    assert len(turns) == 2 and all("--thinking low" in c for c in turns)
    assert "--thinking off" not in "\n".join(box.calls())
    assert "verification turn on kit: ok on anthropic" in proc.stderr


def test_rollback_to_subscription_deletes_what_the_switch_created(box):
    box.flag("_turn_fails_on_anthropic")
    proc = box.auth("api", expect=1)
    assert "rolling back to subscription mode" in proc.stderr
    patches = [c for c in box.calls() if "config patch --stdin" in c]
    assert len(patches) == 4  # dry run + apply, then dry run + rollback
    cfg = box.config()
    assert "anthropic:api-key" not in cfg["auth"]["profiles"]
    assert cfg["auth"]["order"]["anthropic"] == []


def test_rollback_to_api_restores_the_exact_old_values(box):
    box.set_mode("api")
    before = box.config()
    box.flag("_turn_fails_on_claude-cli")
    proc = box.auth("subscription", expect=1)
    assert "rolling back to api mode" in proc.stderr
    assert "ROLLBACK FAILED" not in proc.stderr
    assert box.config() == before
    assert before["auth"]["order"]["anthropic"] == ["anthropic:api-key"]


def test_a_failed_apply_rolls_back_clean_against_the_schema(box):
    box.flag("_restart_hint")
    before = box.config()
    box.flag("_turn_fails_on_anthropic")
    box.auth("api", expect=1)
    assert box.config() == before


def test_rejected_patch_leaves_the_config_alone(box):
    box.flag("_patch_rejects")
    before = box.config()
    box.auth("api", expect=1)
    assert box.config() == before


def test_subscription_is_idempotent_and_still_verifies(box):
    proc = box.auth("subscription")
    assert "already in subscription mode" in proc.stderr
    assert "verification turn on kit: ok on claude-cli" in proc.stderr
    assert not any(
        "config patch --stdin" in c and "--dry-run" not in c for c in box.calls()
    )


def test_subscription_flip_back_from_api(box):
    box.auth("api")
    box.auth("subscription", "--reason", "test")
    cfg = box.config()
    assert cfg["auth"]["order"]["anthropic"] == []
    assert (
        json.loads(box.auth("status", "--json", "--quick").stdout)["mode"]
        == "subscription"
    )
    # the setup-token profile is never part of any patch
    assert not any("setup-token" in c for c in box.calls())


def test_dry_run_changes_nothing(box):
    before = box.config()
    proc = box.auth("api", "--dry-run")
    assert "dry run for api mode" in proc.stderr
    assert box.config() == before
    assert not any("restart" in c or "agent --agent" in c for c in box.calls())


def test_api_refuses_when_spend_is_past_the_threshold(box, tmp_path):
    box.put(
        "usage",
        [usage_record(1, ts=NOW_MS - DAY_MS, model="claude-opus-4-8", out=3_900_000)],
    )  # $97.50 of $100
    wrapper = tmp_path / "spend_guard_fixed.py"
    wrapper.write_text(
        "import runpy, sys\n"
        f'sys.argv += ["--config-dir", "{tmp_path / "conf"}", "--now-ms", "{NOW_MS}"]\n'
        f'runpy.run_path("{DIR / "spend_guard.py"}", run_name="__main__")\n'
    )
    box.env["SPEND_GUARD"] = str(wrapper)
    proc = box.auth("api", expect=2)
    assert "past the fallback threshold" in proc.stderr
    assert box.config()["auth"]["order"]["anthropic"] == []
    box.auth("api", "--force")  # the operator can still override
    assert box.config()["auth"]["order"]["anthropic"] == ["anthropic:api-key"]


def test_a_failed_config_read_aborts_instead_of_patching_from_nothing(box):
    box.auth("api")
    before, seen = box.config(), len(box.calls())
    box.flag("_reads_fail")
    proc = box.auth("subscription", expect=2)
    assert "cannot read the live" in proc.stderr
    assert "(after 3 tries): gateway busy" in proc.stderr  # the underlying error, kept
    box.auth("mode", expect=2)
    assert box.config() == before
    assert not any("config patch" in c for c in box.calls()[seen:])


def test_a_transient_config_read_failure_is_retried(box):
    box.flag("_reads_fail_once")  # each read fails once, then succeeds
    proc = box.auth("mode")
    assert proc.stdout.strip() == "subscription"
    gets = [c for c in box.calls() if "config get" in c]
    assert len(gets) == 4  # agents and auth, two attempts each


def test_a_hung_config_read_times_out_and_names_it(box, tmp_path):
    hang = tmp_path / "hang"
    hang.mkdir()
    (hang / "docker").write_text(
        '#!/bin/sh\n[ "$1" = ps ] && { echo cid123; exit 0; }\nsleep 30\n'
    )
    (hang / "docker").chmod(0o755)
    proc = box.auth(
        "mode",
        expect=2,
        env_extra={
            "PATH": f"{hang}:{box.env['PATH']}",
            "AUTH_MODE_READ_TIMEOUT": "1",
            "AUTH_MODE_READ_TRIES": "2",
        },
    )
    assert "(after 2 tries): timed out after 1s" in proc.stderr


def test_a_failed_key_status_read_does_not_pass_for_a_present_profile(box):
    box.flag("_status_fails")
    proc = box.auth("ensure-profiles", expect=2)
    assert "cannot read" in proc.stderr
    assert not any("secrets apply" in c for c in box.calls())


def test_unknown_command_is_a_usage_error(box):
    proc = box.auth("nonsense", expect=2)
    assert "usage: auth-mode.sh" in proc.stderr


# ── the guard pass ────────────────────────────────────────────────────────────


def test_guard_in_subscription_mode_exports_and_never_flips(box):
    box.put(
        "usage",
        [usage_record(1, ts=NOW_MS - DAY_MS, model="claude-opus-4-8", out=3_000_000)],
    )
    box.run_guard()
    prom = box.prom()
    assert 'openclaw_api_spend_usd{window="since_topup"} 75.0' in prom
    assert "openclaw_api_credit_used_percent 75.0" in prom
    assert 'openclaw_api_auth_mode{mode="subscription"} 1' in prom
    assert not any(ln.startswith("openclaw_api_fallback_active") for ln in prom)
    assert not any(
        ln.startswith("openclaw_api_key_probe_ok") for ln in prom
    )  # no probe off api mode
    assert not any("config patch" in c and "--dry-run" not in c for c in box.calls())
    assert (box.out / "openclaw_api_spend.prom").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("spend", "spend"),
        ("billing", "billing"),
        ("probe", "key-refused"),
    ],
)
def test_guard_flips_api_to_subscription_and_says_why(box, setup, reason):
    box.auth("api")
    box.put("profiles", {"kit": {"profile": True}, "finance": {"profile": True}})
    if setup == "spend":
        box.put(
            "usage",
            [
                usage_record(
                    1, ts=NOW_MS - DAY_MS, model="claude-opus-4-8", out=3_900_000
                )
            ],
        )  # $97.5
    if setup == "billing":
        box.put(
            "profiles",
            {
                "kit": {"profile": True, "disabled": ["billing"]},
                "finance": {"profile": True},
            },
        )
    if setup == "probe":
        box.put("probe", "rejected")
    proc = box.run_guard()
    assert f"spend guard: {reason}" in proc.stderr
    assert box.config()["auth"]["order"]["anthropic"] == []
    prom = box.prom()
    assert f'openclaw_api_fallback_active{{reason="{reason}"}} 1' in prom
    assert 'openclaw_api_auth_mode{mode="subscription"} 1' in prom
    assert json.loads((box.spend / "fallback.json").read_text())["reason"] == reason


def test_guard_leaves_a_healthy_api_box_alone_and_clears_a_stale_marker(box):
    box.auth("api")
    box.put("profiles", {"kit": {"profile": True}, "finance": {"profile": True}})
    (box.spend / "fallback.json").write_text(json.dumps({"reason": "spend"}))
    box.run_guard()
    prom = box.prom()
    assert box.config()["auth"]["order"]["anthropic"] == ["anthropic:api-key"]
    assert "openclaw_api_key_probe_ok 1" in prom
    assert 'openclaw_api_auth_mode{mode="api"} 1' in prom
    assert not (box.spend / "fallback.json").exists()
    assert not any(ln.startswith("openclaw_api_fallback_active") for ln in prom)


def test_guard_fails_loudly_when_the_flip_does_not_stick(box):
    box.auth("api")
    box.put("profiles", {"kit": {"profile": True}, "finance": {"profile": True}})
    box.put("probe", "exhausted")
    box.flag("_turn_fails_on_claude-cli")
    proc = box.run_guard(expect=1)
    assert "flip to subscription failed" in proc.stderr
    assert not (box.spend / "fallback.json").exists()


def test_guard_still_flips_on_billing_when_usage_extraction_fails(box):
    box.auth("api")
    box.put(
        "profiles",
        {
            "kit": {"profile": True, "disabled": ["billing"]},
            "finance": {"profile": True},
        },
    )
    box.flag("_usage_fails")
    proc = box.run_guard(expect=2)
    assert "usage extraction failed" in proc.stderr
    assert box.config()["auth"]["order"]["anthropic"] == []
    prom = box.prom()
    assert 'openclaw_api_fallback_active{reason="billing"} 1' in prom
    assert 'openclaw_api_auth_mode{mode="subscription"} 1' in prom
    assert not any(ln.startswith("openclaw_api_credit_used_percent") for ln in prom)


def test_guard_without_an_estimate_leaves_a_healthy_api_box_alone_but_fails(box):
    box.auth("api")
    box.put("profiles", {"kit": {"profile": True}, "finance": {"profile": True}})
    box.flag("_usage_fails")
    box.run_guard(expect=2)
    assert box.config()["auth"]["order"]["anthropic"] == ["anthropic:api-key"]
    prom = box.prom()
    assert "openclaw_api_key_probe_ok 1" in prom
    assert 'openclaw_api_auth_mode{mode="api"} 1' in prom
    assert not any(ln.startswith("openclaw_api_spend_usd") for ln in prom)


def test_guard_exports_a_mode_unreadable_sample_when_the_gateway_is_down(box):
    (box.out / "openclaw_api_spend.prom").write_text("old 1\n")
    env = {**box.env, "DOCKER": "false"}
    proc = subprocess.run(
        ["python3", str(DIR / "spend_guard.py"), "run", "--textfile", str(box.out), "--state-dir", str(box.spend)],
        env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert proc.returncode == 2
    assert "mode unreadable" in proc.stderr
    prom = box.prom()  # fresh, not the old file
    assert 'openclaw_api_auth_mode{mode="unreadable"} 1' in prom
    assert 'openclaw_api_auth_mode{mode="api"} 0' in prom
    assert not any(ln.startswith("openclaw_api_key_billing_disabled") for ln in prom)


def test_guard_keeps_exporting_when_only_the_mode_read_keeps_failing(box):
    box.put("usage", [usage_record(1, ts=NOW_MS - DAY_MS, out=1_000_000)])
    box.flag("_reads_fail")
    proc = box.run_guard(expect=2)
    assert "cannot read the live agents config" in proc.stderr
    assert "gateway busy" in proc.stderr
    prom = box.prom()
    assert 'openclaw_api_auth_mode{mode="unreadable"} 1' in prom
    assert 'openclaw_api_spend_usd{window="since_topup"} 15.0' in prom
    assert not any("config patch" in c and "--dry-run" not in c for c in box.calls())


def test_guard_survives_a_transient_mode_read_failure(box):
    box.flag("_reads_fail_once")
    box.run_guard()
    assert 'openclaw_api_auth_mode{mode="subscription"} 1' in box.prom()


def test_usage_extract_treats_a_missing_agents_directory_as_no_usage(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = DIR / "usage-extract.mjs"
    run = lambda root: subprocess.run(
        [node, "--input-type=module", "-"],
        input=script.read_text(),
        env={**os.environ, "OPENCLAW_AGENTS_DIR": str(root), "SINCE_MS": "0"},
        capture_output=True, text=True, check=False, cwd=tmp_path,
    )  # fmt: skip
    proc = run(tmp_path / "no-such-agents-dir")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    (tmp_path / "agents").write_text("a file, not a directory")  # ENOTDIR still fails
    assert run(tmp_path / "agents").returncode != 0


def test_guard_ledger_keeps_spend_after_openclaw_prunes_the_sessions(box):
    box.put(
        "usage",
        [usage_record(1, ts=NOW_MS - DAY_MS, model="claude-opus-4-8", out=1_000_000)],
    )
    box.run_guard()
    box.put("usage", [])  # the session is gone from the gateway
    box.run_guard()
    assert 'openclaw_api_spend_usd{window="since_topup"} 25.0' in box.prom()


def test_report_prints_the_estimate(box):
    box.put("usage", [usage_record(1, ts=NOW_MS - DAY_MS, out=1_000_000)])
    conf = box.spend.parent / "conf"
    proc = subprocess.run(
        ["python3", str(DIR / "spend_guard.py"), "report", "--json", "--config-dir", str(conf),
         "--state-dir", str(box.spend), "--now-ms", str(NOW_MS)],
        env=box.env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    est = json.loads(proc.stdout)
    assert est["since_topup_usd"] == 15
    assert est["fallback_percent"] == 95


# ── installation ──────────────────────────────────────────────────────────────


def test_timer_units_are_valid_and_the_wrapper_runs_the_guard(box, tmp_path):
    import configparser

    def unit(name):
        parser = configparser.ConfigParser(strict=False)
        parser.optionxform = str
        parser.read_string((DIR / name).read_text())
        return parser

    service = unit("lifekit-openclaw-spend-guard.service")
    assert service["Service"]["User"] == "__ADMIN_USER__"
    assert service["Service"]["ExecStart"].startswith(
        "/usr/local/bin/lifekit-openclaw-spend-guard.sh "
    )
    timer = unit("lifekit-openclaw-spend-guard.timer")
    assert (
        timer["Timer"]["OnUnitActiveSec"] == "10min"
    )  # under the 900 s textfile-stale rule
    box.put("usage", [usage_record(1, ts=int(time.time() * 1000), out=1_000_000)])
    textfile = tmp_path / "wrapper-out"
    textfile.mkdir()
    proc = subprocess.run(
        ["bash", str(GUARD_SH), str(textfile)],
        env={**box.env, "SPEND_GUARD": str(DIR / "spend_guard.py")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = (textfile / "openclaw_api_spend.prom").read_text().splitlines()
    assert 'openclaw_api_spend_usd{window="since_topup"} 15.0' in lines
    assert 'openclaw_api_auth_mode{mode="subscription"} 1' in lines
    assert (box.spend / "ledger.jsonl").exists()  # the wrapper's state dir
