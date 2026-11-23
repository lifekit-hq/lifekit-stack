"""ensure-learnclaw-morning.sh declares learnclaw's 07:15 morning learning message (#609).

`docker` is stubbed: `cron list` prints a state file, `cron add` and `cron edit`
apply their flags to it, so the live-row check the script runs after converging
is exercised against what it actually wrote.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ensure-learnclaw-morning.sh"
MODEL = "anthropic/claude-sonnet-5-5"
MIRROR = "/home/node/.openclaw/agents/learnclaw/workspace/learnharness-mirror"

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
    assert "at most 4 lines" in msg
    assert "mirror newest session <YYYY-MM-DD>" in msg
    assert "/home/node/memory/domains/learning.md" in msg
    assert MIRROR in msg
    assert ".life-state" not in msg  # outside what the read tool reaches
    assert "write nothing" in msg
    # Re-running converges on the same row, and enables it again.
    assert run_script(env).returncode == 0
    assert len(json.loads(state.read_text())["jobs"]) == 1
    calls = (tmp_path / "calls.jsonl").read_text()
    assert '"cron", "enable"' in calls
    assert json.loads(state.read_text())["jobs"][0]["enabled"] is True


def declared_message(tmp_path, extra=None):
    state, env = fake_box(tmp_path, [], extra)
    r = run_script(env)
    assert r.returncode == 0, r.stderr
    (job,) = json.loads(state.read_text())["jobs"]
    return job["payload"]["message"]


def line4(msg):
    return msg.split("\nLine 4: ", 1)[1].split("\nNever teach", 1)[0]


# The independent oracle for the weekly count is skills/leetcode-daily/daily.py `week`,
# the tested Monday-to-Sunday count of done problems over the learning record. The
# morning job's model is told the same rule in line 4 and shown worked examples; the
# examples it is shown must equal what the oracle prints for a matching record.
DAILY = REPO / "skills/leetcode-daily/daily.py"
PAGE = """\
---
name: learning
updatedAt: 2026-10-01
status: active
---

# Learning

## Artefact record (v0 - one row per artefact)

| date | topic | artefact (repo/path) | reading | applied | post | grilled | status |
|---|---|---|---|---|---|---|---|
"""


def oracle(tmp_path, done, day):
    # `done` is a list of (slug, date) rows; returns the oracle's count, or None
    # when the record cannot be read.
    vault = tmp_path / "vault"
    (vault / "domains").mkdir(parents=True, exist_ok=True)
    (vault / "bin").mkdir(exist_ok=True)
    (vault / "bin/lint.sh").write_text("#!/bin/sh\nexit 0\n")
    if done is not None:
        body = "".join(
            f"| {d} | leetcode: {slug} | https://leetcode.com/problems/{slug}/ | - | - | - | - | done |\n"
            for slug, d in done
        )
        (vault / "domains/learning.md").write_text(PAGE + body)
    r = subprocess.run(
        [sys.executable, str(DAILY), "week"],
        env={
            **os.environ,
            "LEARNING_VAULT": str(vault),
            "LEARNING_TODAY": day,
            "LEETCODE_TODAY": day,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    return int(re.match(r"solved: (\d+) ", r.stdout).group(1))


def shown(msg):
    # The two worked examples line 4 shows the model: (weekday, count, target).
    ex = re.search(r"for example (\w+) (\d+)/(\d+) or (\w+) (\d+)/(\d+)\.", line4(msg))
    assert ex, line4(msg)
    a, b = ex.groups()[:3], ex.groups()[3:]
    return [(w, int(n), int(t)) for w, n, t in (a, b)]


def weekday(day):
    return dt.date.fromisoformat(day).strftime("%a")


def test_the_example_for_a_normal_week_matches_the_weekly_count(tmp_path):
    # Wednesday 2026-11-25 after three problems solved Monday to Wednesday, with a
    # done row from the Sunday before that must not be counted.
    wed = "2026-11-25"
    done = [
        ("two-sum", "2026-11-22"),
        ("group-anagrams", "2026-11-23"),
        ("valid-sudoku", "2026-11-24"),
        ("top-k-frequent-elements", wed),
    ]
    count = oracle(tmp_path, done, wed)
    assert count == 3
    assert (weekday(wed), count, 20) in shown(declared_message(tmp_path))


def test_the_example_for_a_week_start_is_zero_despite_last_weeks_rows(tmp_path):
    # Monday 2026-11-23: last week's done rows stay out, so the week starts at 0.
    mon = "2026-11-23"
    last_week = [("two-sum", "2026-11-17"), ("group-anagrams", "2026-11-22")]
    count = oracle(tmp_path, last_week, mon)
    assert count == 0
    assert (weekday(mon), count, 20) in shown(declared_message(tmp_path))


def test_an_unreadable_record_gets_a_plain_statement_not_a_count(tmp_path):
    # No learning page: the oracle itself cannot produce a count.
    assert oracle(tmp_path, None, "2026-11-25") is None
    four = line4(declared_message(tmp_path))
    unreadable = re.search(
        r"write instead: (<weekday> count unreadable \(target 20\))", four
    )
    assert unreadable
    # The unreadable form is not of the <weekday> <n>/<target> shape.
    assert not re.search(r"\d+/\d+", unreadable.group(1))


def test_the_weekly_target_is_one_named_setting(tmp_path):
    msg = declared_message(tmp_path, {"LEARNCLAW_WEEKLY_TARGET": "25"})
    assert [t for _, _, t in shown(msg)] == [25, 25]
    assert "target 25" in line4(msg)
    assert "20" not in line4(msg)  # no second copy of the default


@pytest.mark.parametrize("bad", ["0", "abc", "-5", "2.5", "05"])
def test_a_bad_weekly_target_is_refused(tmp_path, bad):
    state, env = fake_box(tmp_path, [], {"LEARNCLAW_WEEKLY_TARGET": bad})
    r = run_script(env)
    assert r.returncode == 1 and "LEARNCLAW_WEEKLY_TARGET" in r.stderr
    assert json.loads(state.read_text())["jobs"] == []


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
        "payload": {
            "kind": "agentTurn",
            "message": "x",
            "toolsAllow": ["read", "write"],
        },
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
        docker.read_text().replace('"tz": flag("--tz")}', '"tz": ' + repr(tz) + "}")
    )
    r = run_script(env)
    assert r.returncode == 1 and "schedule.tz=" + repr(tz) in r.stderr


# --- #611: the read tool reaches the workspace, so the mirror is copied there ---

SYNC = REPO / "scripts/learnharness-sync"


def render_sync(mirror):
    r = subprocess.run(
        ["bash", str(SYNC / "install-learnharness-sync.sh"), "--print"],
        env={
            **os.environ,
            "LH_SYNC_HOST": "pc",
            "LH_SYNC_USER": "u",
            "LH_MIRROR_DIR": str(mirror),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout


def test_the_unit_refreshes_a_workspace_copy_the_read_tool_can_reach(tmp_path):
    unit = render_sync("/srv/x/agents/learnclaw/workspace/learnharness-mirror")
    assert "@MIRROR@" not in unit
    # Tolerates a host without the workspace; the sandbox may write only there.
    assert "ExecStartPost=-/usr/bin/rsync" in unit
    assert (
        "ReadWritePaths=/var/lib/lifekit/learnharness -/srv/x/agents/learnclaw/workspace/learnharness-mirror"
        in unit
    )
    default = subprocess.run(
        ["bash", str(SYNC / "install-learnharness-sync.sh"), "--print"],
        env={**os.environ, "LH_SYNC_HOST": "pc", "LH_SYNC_USER": "u"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert (
        "/srv/openclaw/config/agents/learnclaw/workspace/learnharness-mirror" in default
    )


def test_the_copy_step_mirrors_the_record_and_removes_what_the_pc_dropped(tmp_path):
    data = tmp_path / "data"
    (data / "chats").mkdir(parents=True)
    (data / "chats/a.json").write_text("{}")
    mirror = tmp_path / "ws/learnharness-mirror"
    mirror.mkdir(parents=True)
    (mirror / "stale.json").write_text("{}")
    unit = render_sync(mirror)
    post = next(ln for ln in unit.splitlines() if ln.startswith("ExecStartPost="))
    body = unit[unit.index(post) :].split("\n", 2)
    cmd = (post + " " + body[1]).removeprefix("ExecStartPost=-").replace("\\", " ")
    cmd = cmd.replace("/var/lib/lifekit/learnharness/data/", f"{data}/")
    subprocess.run(cmd, shell=True, check=True)
    assert (mirror / "chats/a.json").read_text() == "{}"
    assert not (mirror / "stale.json").exists()
    assert oct((mirror / "chats/a.json").stat().st_mode & 0o777) == "0o640"


def test_ensure_learnclaw_creates_the_workspace_copy_dir(tmp_path):
    ws = tmp_path / "state/agents/learnclaw/workspace"
    ws.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (
        ("docker", "#!/bin/sh\nexit 0\n"),
        ("curl", "#!/bin/sh\nexit 22\n"),
    ):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    r = subprocess.run(
        ["bash", str(REPO / "scripts/ensure-learnclaw.sh")],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "OPENCLAW_CONFIG_DIR": str(tmp_path / "state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    mirror = ws / "learnharness-mirror"
    assert mirror.is_dir()
    assert oct(mirror.stat().st_mode & 0o777) == "0o750"
