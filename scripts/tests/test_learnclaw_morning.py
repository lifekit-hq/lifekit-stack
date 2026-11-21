"""ensure-learnclaw-morning.sh declares learnclaw's 07:15 morning learning message (#609).

`docker` is stubbed: `cron list` prints a state file, `cron add` and `cron edit`
apply their flags to it, so the live-row check the script runs after converging
is exercised against what it actually wrote.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ensure-learnclaw-morning.sh"
MODEL = "anthropic/claude-sonnet-5-5"

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ["FAKE_STATE"])
args = sys.argv[1:]
if args[0] == "ps":
    print("gw123")
    sys.exit(0)
cmd = args[args.index("openclaw") + 1:]
data = json.loads(state.read_text())
log = state.parent / "calls.jsonl"
def flag(name):
    return cmd[cmd.index(name) + 1] if name in cmd else None
def apply(job):
    p = job.setdefault("payload", {})
    if flag("--message") is not None:
        p["message"] = flag("--message")
    if flag("--model") is not None:
        p["model"] = flag("--model")
    if flag("--tools") is not None:
        p["toolsAllow"] = flag("--tools").split(",")
    p["kind"] = "agentTurn"
    if flag("--session") is not None:
        job["sessionTarget"] = flag("--session")
    if flag("--agent") is not None:
        job["agentId"] = flag("--agent")
    if flag("--cron") is not None:
        job["schedule"] = {"expr": flag("--cron"), "tz": flag("--tz")}
    d = job.setdefault("delivery", {})
    if "--announce" in cmd:
        d["mode"] = "announce"
    for f, k in (("--channel", "channel"), ("--account", "accountId"), ("--to", "to")):
        if flag(f) is not None:
            d[k] = flag(f)
    if os.environ.get("FAKE_WRONG_ACCOUNT"):
        d["accountId"] = "default"
with log.open("a") as f:
    f.write(json.dumps(cmd) + "\n")
if cmd[:2] == ["cron", "list"]:
    print(json.dumps(data))
elif cmd[:2] == ["cron", "add"]:
    job = {"id": "new1", "name": flag("--name")}
    apply(job)
    data["jobs"].append(job)
    state.write_text(json.dumps(data))
elif cmd[:2] == ["cron", "rm"]:
    data["jobs"] = [j for j in data["jobs"] if j["id"] != cmd[2]]
    state.write_text(json.dumps(data))
elif cmd[:2] == ["cron", "edit"]:
    for job in data["jobs"]:
        if job["id"] == cmd[2]:
            apply(job)
    state.write_text(json.dumps(data))
elif cmd[:2] == ["cron", "enable"]:
    for job in data["jobs"]:
        if job["id"] == cmd[2]:
            job["enabled"] = True
    state.write_text(json.dumps(data))
else:
    sys.exit("unexpected: " + " ".join(cmd))
"""


def fake_box(tmp_path, jobs, env_extra=None):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"jobs": jobs}))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_STATE": str(state),
        "ENV_FILE": str(tmp_path / "absent.env"),
        "LEARNCLAW_MORNING_CHAT": "424242",
        **(env_extra or {}),
    }
    return state, env


def run_script(env):
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def test_the_job_is_declared_read_only_on_learnclaws_own_bot(tmp_path):
    state, env = fake_box(tmp_path, [])
    r = run_script(env)
    assert r.returncode == 0, r.stderr
    (job,) = json.loads(state.read_text())["jobs"]
    assert job["name"] == "learnclaw-morning" and job["agentId"] == "learnclaw"
    assert job["schedule"] == {"expr": "15 7 * * 1,2,3,4,5,6", "tz": "Europe/Dublin"}
    assert job["sessionTarget"] == "isolated"
    assert job["delivery"] == {
        "mode": "announce",
        "channel": "telegram",
        "accountId": "reading",
        "to": "424242",
    }
    p = job["payload"]
    assert p["kind"] == "agentTurn" and p["model"].startswith("anthropic/")
    # Read-only: no write, no subagent, no web, no send tool.
    assert p["toolsAllow"] == ["read"]
    msg = p["message"]
    assert "at most 3 lines" in msg
    assert "mirror newest session <YYYY-MM-DD>" in msg
    assert "/home/node/memory/domains/learning.md" in msg
    assert "/home/node/.life-state/learnharness/data" in msg
    assert "write nothing" in msg
    # Re-running converges on the same row, and enables it again.
    assert run_script(env).returncode == 0
    assert len(json.loads(state.read_text())["jobs"]) == 1
    calls = (tmp_path / "calls.jsonl").read_text()
    assert '"cron", "enable"' in calls
    assert json.loads(state.read_text())["jobs"][0]["enabled"] is True


def test_the_owner_chat_is_not_in_the_message(tmp_path):
    state, env = fake_box(tmp_path, [])
    assert run_script(env).returncode == 0
    (job,) = json.loads(state.read_text())["jobs"]
    assert "424242" not in job["payload"]["message"]


def test_the_chat_comes_from_the_env_file(tmp_path):
    envfile = tmp_path / "stack.env"
    envfile.write_text("LIFEKIT_TELEGRAM_CHAT='777'\n")
    state, env = fake_box(
        tmp_path, [], {"ENV_FILE": str(envfile), "LEARNCLAW_MORNING_CHAT": ""}
    )
    assert run_script(env).returncode == 0
    (job,) = json.loads(state.read_text())["jobs"]
    assert job["delivery"]["to"] == "777"


def test_no_chat_id_fails_before_touching_the_gateway(tmp_path):
    state, env = fake_box(tmp_path, [], {"LEARNCLAW_MORNING_CHAT": ""})
    r = run_script(env)
    assert r.returncode == 1 and "no owner chat id" in r.stderr
    assert json.loads(state.read_text())["jobs"] == []


@pytest.mark.parametrize(
    "expr",
    [
        "15 7 * * 0",
        "15 7 * * 1,7",
        "15 7 * * *",
        "15 7 * * 1-6",
        "15 7 1 * 1,2",
        "15 7 * *",
    ],
)
def test_a_schedule_that_can_fire_on_sunday_is_refused(tmp_path, expr):
    state, env = fake_box(tmp_path, [], {"LEARNCLAW_MORNING_CRON": expr})
    r = run_script(env)
    assert r.returncode == 1 and "refusing" in r.stderr
    assert json.loads(state.read_text())["jobs"] == []  # nothing was created


def test_a_row_that_would_deliver_through_the_wrong_bot_fails_the_run(tmp_path):
    _, env = fake_box(tmp_path, [], {"FAKE_WRONG_ACCOUNT": "1"})
    r = run_script(env)
    assert r.returncode == 1
    assert "delivery.accountId='default'" in r.stderr


def test_a_row_for_another_agent_is_repointed_or_fails_closed(tmp_path):
    # The agent flag is always sent, so a stray row converges to learnclaw.
    stray = {
        "id": "old1",
        "name": "learnclaw-morning",
        "agentId": "kit",
        "payload": {"kind": "agentTurn", "message": "x", "toolsAllow": ["read", "write"]},
    }
    state, env = fake_box(tmp_path, [stray])
    r = run_script(env)
    assert r.returncode == 0, r.stderr
    (job,) = json.loads(state.read_text())["jobs"]
    assert job["id"] == "old1" and job["agentId"] == "learnclaw"
    assert job["payload"]["toolsAllow"] == ["read"]


def test_a_live_row_that_drifts_from_the_declaration_fails_the_run(tmp_path):
    # A stub that silently ignores --tools leaves write on the row: refused.
    state, env = fake_box(tmp_path, [])
    docker = tmp_path / "bin/docker"
    docker.write_text(
        docker.read_text().replace(
            'p["toolsAllow"] = flag("--tools").split(",")',
            'p["toolsAllow"] = ["read", "exec"]',
        )
    )
    r = run_script(env)
    assert r.returncode == 1 and "payload.toolsAllow" in r.stderr


@pytest.mark.parametrize("tz", ["UTC", None])
def test_a_live_row_in_the_wrong_timezone_fails_the_run(tmp_path, tz):
    # 07:15 UTC is 08:15 in Dublin in summer: the cron expression alone is not enough.
    state, env = fake_box(tmp_path, [])
    docker = tmp_path / "bin/docker"
    docker.write_text(
        docker.read_text().replace(
            '"tz": flag("--tz")}', '"tz": ' + repr(tz) + "}"
        )
    )
    r = run_script(env)
    assert r.returncode == 1 and "schedule.tz=" + repr(tz) in r.stderr
