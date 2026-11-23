"""ensure-planning-reminder.sh declares Kit's Monday 10:00 planning reminder (#619).

`docker` is stubbed: `cron list` prints a state file, `cron add` and `cron edit`
apply their flags to it, so the live-row check the script runs after converging
is exercised against what it actually wrote.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ensure-planning-reminder.sh"

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
    if flag("--command-argv") is not None:
        p["argv"] = json.loads(flag("--command-argv"))
        p["kind"] = "command"
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
        d["accountId"] = "reading"
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


def fake_box(tmp_path, jobs, env_extra=None, chat="424242"):
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
        **({"PLANNING_REMINDER_CHAT": chat} if chat else {}),
        **(env_extra or {}),
    }
    env.pop("PLANNING_REMINDER_CHAT", None) if not chat else None
    return state, env


def run_script(env):
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def test_create_declares_a_fixed_monday_row_on_kits_default_account(tmp_path):
    state, env = fake_box(tmp_path, [])
    r = run_script(env)
    assert r.returncode == 0, r.stderr
    assert "created cron 'planning-reminder'" in r.stdout
    (job,) = json.loads(state.read_text())["jobs"]
    assert job["name"] == "planning-reminder" and job["agentId"] == "kit"
    assert job["schedule"] == {"expr": "0 10 * * 1", "tz": "Europe/Dublin"}
    assert job["sessionTarget"] == "isolated"
    assert job["delivery"] == {
        "mode": "announce",
        "channel": "telegram",
        "accountId": "default",
        "to": "424242",
    }
    # No model turn, nothing read: a fixed echo with no private data in it.
    p = job["payload"]
    assert p["kind"] == "command" and p["argv"][0] == "echo" and len(p["argv"]) == 2
    assert "planning session" in p["argv"][1]
    assert "424242" not in p["argv"][1]
    assert "model" not in p and "message" not in p


def test_rerun_changes_nothing(tmp_path):
    state, env = fake_box(tmp_path, [])
    assert run_script(env).returncode == 0
    first = state.read_text()
    r = run_script(env)
    assert r.returncode == 0, r.stderr
    assert "created cron" not in r.stdout
    assert state.read_text() == first
    assert len(json.loads(first)["jobs"]) == 1
    calls = [
        json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    assert sum(1 for c in calls if c[:2] == ["cron", "add"]) == 1


def test_a_paused_row_is_enabled_again(tmp_path):
    state, env = fake_box(tmp_path, [])
    assert run_script(env).returncode == 0
    data = json.loads(state.read_text())
    data["jobs"][0]["enabled"] = False
    state.write_text(json.dumps(data))
    assert run_script(env).returncode == 0
    assert json.loads(state.read_text())["jobs"][0]["enabled"] is True


def test_refuses_without_a_resolvable_owner_chat(tmp_path):
    state, env = fake_box(tmp_path, [], chat="")
    r = run_script(env)
    assert r.returncode == 1
    assert "no owner chat id" in r.stderr
    assert json.loads(state.read_text())["jobs"] == []
    assert not (tmp_path / "calls.jsonl").exists()


def test_the_chat_comes_from_the_env_file(tmp_path):
    envfile = tmp_path / "stack.env"
    envfile.write_text('LIFEKIT_TELEGRAM_CHAT="777"\n')
    state, env = fake_box(tmp_path, [], {"ENV_FILE": str(envfile)}, chat="")
    assert run_script(env).returncode == 0
    assert json.loads(state.read_text())["jobs"][0]["delivery"]["to"] == "777"


def test_refuses_a_schedule_that_is_not_monday_only(tmp_path):
    for expr in ("0 10 * * 1,2", "0 10 * * *", "0 10 * * 0", "0 10 1 * 1", "0 10 * *"):
        state, env = fake_box(tmp_path, [], {"PLANNING_REMINDER_CRON": expr})
        r = run_script(env)
        assert r.returncode == 1, expr
        assert "refusing" in r.stderr
        assert json.loads(state.read_text())["jobs"] == []


def test_refuses_when_the_live_row_is_not_the_declared_one(tmp_path):
    _state, env = fake_box(tmp_path, [], {"FAKE_WRONG_ACCOUNT": "1"})
    r = run_script(env)
    assert r.returncode == 1
    assert "delivery.accountId" in r.stderr
