"""learnclaw's daily LeetCode problem (#594): the pick order, the home-day rule,
the no-repeat rule, the weekly count, the cron declaration and the check-in wiring.

The vault is a temporary git repo with a learning page shaped like the real one
and a stub bin/lint.sh (as in test_learning_record.py). `docker` is stubbed for the
two ensure scripts: `cron list` prints a state file, `cron add` and `cron edit`
apply their flags to it, so the live-row check the scripts run after converging
is exercised against what they actually wrote.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills/leetcode-daily"
DAILY = SKILL / "daily.py"
RECORD = REPO / "skills/learning-record/record.py"
CRON_SCRIPT = REPO / "scripts/ensure-leetcode-daily.sh"
CHECKIN_SCRIPT = REPO / "scripts/ensure-goal-checkin-solved.sh"

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
| due 2026-09-22 | caching | cache from scratch | a chapter | a boundary (planned) | - | - | in progress |

## In-progress
_(to fill)_
"""
LINT = "#!/bin/sh\nexit 0\n"

# 2026-11-23 is a Monday.
MON, TUE, WED, THU, FRI, SAT, SUN = (f"2026-11-{d}" for d in range(23, 30))
NEXT_MON = "2026-11-30"
SUN_BEFORE = "2026-11-22"
MODEL = "anthropic/claude-sonnet-5-5@anthropic:api-key"


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "domains").mkdir(parents=True)
    (v / "bin").mkdir()
    (v / "domains/learning.md").write_text(PAGE)
    (v / "bin/lint.sh").write_text(LINT)
    return v


def daily(vault, *args, day=MON):
    return subprocess.run(
        [sys.executable, str(DAILY), *args],
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


def rows(vault):
    lines = (vault / "domains/learning.md").read_text().splitlines()
    return [ln for ln in lines if ln.startswith("| ") and not ln.startswith("| date")]


def send(vault, day):
    r = daily(vault, "next", "--record", day=day)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


# --- the list and the pick order ------------------------------------------------


def test_the_list_is_valid_and_in_track_a_order():
    r = subprocess.run(
        [sys.executable, str(DAILY), "check"], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    data = json.loads((SKILL / "problems.json").read_text())
    assert [p["name"] for p in data["patterns"]] == [
        "Big-O",
        "Hash maps",
        "Two pointers and sliding window",
        "Stacks and monotonic stack",
        "Binary search",
        "Linked lists",
        "Trees",
        "Heaps",
        "Graphs",
        "Intervals and greedy",
        "Backtracking",
        "Dynamic programming, 1-D",
        "Dynamic programming, 2-D",
        "Tries and union-find",
    ]
    assert data["patterns"][0]["problems"] == []  # pure basics: nothing to send


def test_a_sample_week_sends_one_problem_a_day_in_pattern_order(vault):
    week = {MON: "Two Sum", WED: "Group Anagrams", FRI: "Top K Frequent Elements"}
    sent = {day: send(vault, day) for day in (MON, WED, FRI, SAT)}
    for day, title in week.items():
        assert sent[day][:2] == ["SEND", title]
    assert sent[SAT][:2] == ["SEND", "Product of Array Except Self"]
    # Name and link only: three lines, the link is the whole second line, and
    # nothing names the pattern.
    assert sent[MON] == ["SEND", "Two Sum", "https://leetcode.com/problems/two-sum/"]
    assert "hash" not in "\n".join(sent[MON]).lower()
    assert len(rows(vault)) == 1 + 4


def test_the_pick_moves_through_patterns_one_at_a_time(vault):
    data = json.loads((SKILL / "problems.json").read_text())
    order = [p["title"] for pat in data["patterns"] for p in pat["problems"]]
    first_pattern = len(data["patterns"][1]["problems"])
    picked = []
    day = dt.date.fromisoformat(MON)
    while len(picked) < first_pattern + 1:
        out = daily(vault, "next", "--record", day=day.isoformat()).stdout.splitlines()
        if out[0] == "SEND":  # office days and Sunday print SKIP and move on
            picked.append(out[1])
        day += dt.timedelta(days=1)
    assert picked == order[: first_pattern + 1]
    # The last pick is the first problem of the next pattern: Valid Palindrome.
    assert picked[-1] == "Valid Palindrome"


# --- the home-day-only schedule ------------------------------------------------


@pytest.mark.parametrize(
    "day,reason",
    [
        (TUE, "SKIP office day (Tuesday)"),
        (THU, "SKIP office day (Thursday)"),
        (SUN, "SKIP rest day (Sunday)"),
    ],
)
def test_office_days_and_sunday_produce_nothing(vault, day, reason):
    r = daily(vault, "next", "--record", day=day)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == reason
    assert len(rows(vault)) == 1  # nothing recorded


@pytest.mark.parametrize("day", [MON, WED, FRI, SAT])
def test_home_days_send(vault, day):
    assert send(vault, day)[0] == "SEND"


def test_a_second_run_on_the_same_day_sends_nothing(vault):
    assert send(vault, MON)[0] == "SEND"
    r = daily(vault, "next", "--record", day=MON)
    assert r.stdout.strip() == "SKIP offered today: two-sum"
    assert len(rows(vault)) == 2


# --- the no-repeat rule ----------------------------------------------------------


def test_a_done_and_a_skipped_problem_never_come_back(vault):
    assert send(vault, MON)[1] == "Two Sum"
    assert send(vault, WED)[1] == "Group Anagrams"
    r = daily(
        vault,
        *("done", "two-sum", "--pattern", "hash map", "--time", "O(n)"),
        *("--space", "O(n)"),
        day=WED,
    )
    assert r.returncode == 0, r.stderr
    r = daily(vault, "skip", "group-anagrams", "--reason", "no time", day=FRI)
    assert r.returncode == 0, r.stderr
    assert daily(vault, "status", day=FRI).stdout.splitlines() == [
        "leetcode: 1 done, 1 skipped, 0 open, 126 in the list",
        "next: Top K Frequent Elements (A2 Hash maps)",
    ]
    assert send(vault, FRI)[1] == "Top K Frequent Elements"
    # An offered problem the owner never answered does not repeat either.
    assert send(vault, SAT)[1] == "Product of Array Except Self"
    seen = " ".join(rows(vault))
    assert seen.count("leetcode: two-sum") == 2  # offered, then done
    assert seen.count("leetcode: group-anagrams") == 2  # offered, then skipped


def test_done_records_the_owners_words_and_needs_all_three(vault):
    r = daily(vault, "done", "two-sum", "--pattern", "hash map", "--time", "O(n)")
    assert r.returncode == 2 and "--space is required" in r.stderr
    r = daily(vault, "done", "no-such-problem", "--pattern", "x", "--time", "y", "--space", "z")
    assert r.returncode == 2 and "not in the problem list" in r.stderr
    assert len(rows(vault)) == 1
    r = daily(
        vault,
        *("done", "two-sum", "--pattern", "hash map", "--time", "O(n)"),
        *("--space", "O(n)"),
    )
    assert r.returncode == 0, r.stderr
    assert rows(vault)[-1] == (
        "| 2026-11-23 | leetcode: two-sum | https://leetcode.com/problems/two-sum/ "
        "| - | - | - | pattern: hash map; time O(n); space O(n) | done |"
    )


def test_the_artefact_record_projections_ignore_the_problem_rows(vault):
    send(vault, MON)
    daily(
        vault,
        *("done", "two-sum", "--pattern", "hash map", "--time", "O(n)"),
        *("--space", "O(n)"),
    )
    r = subprocess.run(
        [sys.executable, str(RECORD), "status"],
        env={**os.environ, "LEARNING_VAULT": str(vault), "LEARNING_TODAY": MON},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "artefact record: 1 row(s)" in r.stdout
    assert "daily problems: 1 done, 0 skipped, 0 open" in r.stdout
    assert "leetcode" not in r.stdout.replace("leetcode-daily", "")
    # An ordinary artefact still cannot be done without its proof.
    r = subprocess.run(
        [sys.executable, str(RECORD), "append", "--topic", "x", "--artefact", "y"]
        + ["--status", "done", "--grilled", "g"],
        env={**os.environ, "LEARNING_VAULT": str(vault), "LEARNING_TODAY": MON},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2 and "needs applied, post" in r.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("done", "two-sum", "--pattern", "p", "--time", "t", "--space", "s"),
        ("skip", "two-sum"),
    ],
)
def test_done_and_skip_are_dated_in_dublin_not_by_the_container_clock(vault, args):
    # Just after midnight Dublin on Monday the container (UTC) is still on Sunday.
    r = subprocess.run(
        [sys.executable, str(DAILY), *args],
        env={
            **os.environ,
            "LEARNING_VAULT": str(vault),
            "LEARNING_TODAY": SUN_BEFORE,
            "LEETCODE_TODAY": MON,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert rows(vault)[-1].startswith(f"| {MON} |")
    if args[0] == "done":
        week = daily(vault, "week", day=MON).stdout.splitlines()[0]
        assert week.startswith("solved: 1 (Monday 2026-11-23")


# --- the weekly count (read by the check-in) ----------------------------------


def test_the_weekly_count_is_monday_to_sunday(vault):
    def solve(slug, day):
        r = daily(
            vault,
            *("done", slug, "--pattern", "p", "--time", "t", "--space", "s"),
            *("--date", day),
            day=day,
        )
        assert r.returncode == 0, r.stderr

    solve("two-sum", "2026-11-22")  # the Sunday before: last week
    solve("group-anagrams", MON)
    solve("top-k-frequent-elements", SAT)
    solve("valid-sudoku", SUN)
    solve("longest-consecutive-sequence", NEXT_MON)  # next week
    r = daily(vault, "week", day=SUN)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "solved: 3 (Monday 2026-11-23 to Sunday 2026-11-29, by hand)"
    assert lines[1:] == [
        "- 2026-11-23 Group Anagrams",
        "- 2026-11-28 Top K Frequent Elements",
        "- 2026-11-29 Valid Sudoku",
    ]
    assert daily(vault, "week", day=NEXT_MON).stdout.startswith("solved: 1 (Monday 2026-11-30")
    # Offered and skipped problems are not solved.
    send(vault, WED)
    daily(vault, "skip", "product-of-array-except-self", day=FRI)
    assert daily(vault, "week", day=SUN).stdout.startswith("solved: 3 ")


# --- the cron declaration ----------------------------------------------------------

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
elif cmd[:2] == ["cron", "edit"]:
    for job in data["jobs"]:
        if job["id"] == cmd[2]:
            apply(job)
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
        "LEETCODE_DAILY_CHAT": "424242",
        **(env_extra or {}),
    }
    return state, env


def run_script(script, env):
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, check=False
    )


def test_the_cron_is_declared_with_the_account_model_and_learnclaws_own_bot(tmp_path):
    state, env = fake_box(tmp_path, [])
    r = run_script(CRON_SCRIPT, env)
    assert r.returncode == 0, r.stderr
    (job,) = json.loads(state.read_text())["jobs"]
    assert job["name"] == "leetcode-daily" and job["agentId"] == "learnclaw"
    assert job["payload"]["model"] == MODEL
    assert job["payload"]["toolsAllow"] == ["exec", "read"]
    assert job["schedule"] == {"expr": "30 17 * * 1,3,5,6", "tz": "Europe/Dublin"}
    assert job["delivery"] == {
        "mode": "announce",
        "channel": "telegram",
        "accountId": "reading",
        "to": "424242",
    }
    # The prompt runs the script and carries no problem, pattern or hint.
    msg = job["payload"]["message"]
    assert "daily.py next --record" in msg and "NO_REPLY" in msg
    (no_reply,) = [x for x in msg.split(". ") if "NO_REPLY" in x.replace("never reply NO_REPLY", "")]
    assert "SKIP" in no_reply and "error" not in no_reply.lower()
    assert "failed" in msg and "never reply NO_REPLY" in msg
    # Re-running converges on the same row.
    assert run_script(CRON_SCRIPT, env).returncode == 0
    assert len(json.loads(state.read_text())["jobs"]) == 1


def test_an_error_run_exits_nonzero_with_no_send_line(vault):
    (vault / "domains/learning.md").unlink()
    r = daily(vault, "next", "--record")
    assert r.returncode != 0
    assert "SEND" not in r.stdout and "NO_REPLY" not in r.stdout
    assert r.stderr.strip()


def test_the_cron_model_value_is_the_policy_model_with_the_inference_account():
    r = subprocess.run(
        [
            "bash",
            "-c",
            f"source {REPO}/scripts/lib/cron-model.sh && cron_model_account",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout.strip() == MODEL


@pytest.mark.parametrize(
    "expr", ["30 17 * * 1,2,3,5,6", "30 17 * * 4", "30 17 * * 0", "30 17 * * *", "30 17 * * 1-5"]
)
def test_a_schedule_that_can_fire_on_an_office_day_or_sunday_is_refused(tmp_path, expr):
    state, env = fake_box(tmp_path, [], {"LEETCODE_DAILY_CRON": expr})
    r = run_script(CRON_SCRIPT, env)
    assert r.returncode == 1 and "refusing" in r.stderr
    assert json.loads(state.read_text())["jobs"] == []  # nothing was created


def test_a_row_that_would_deliver_through_the_wrong_bot_fails_the_run(tmp_path):
    _, env = fake_box(tmp_path, [], {"FAKE_WRONG_ACCOUNT": "1"})
    r = run_script(CRON_SCRIPT, env)
    assert r.returncode == 1
    assert "delivery.accountId='default'" in r.stderr


# --- the check-in wiring ----------------------------------------------------------

CHECKIN = {
    "id": "gw1",
    "name": "goal-weekly-checkin",
    "payload": {
        "kind": "agentTurn",
        "message": "Sunday goal check-in. Ask the owner how many LeetCode problems he solved.",
        "toolsAllow": ["read", "write"],
    },
}


def test_the_checkin_reads_the_count_from_learnclaw_and_converges(tmp_path, vault):
    state, env = fake_box(tmp_path, [CHECKIN, {"id": "o", "name": "other", "payload": {}}])
    r = run_script(CHECKIN_SCRIPT, env)
    assert r.returncode == 0, r.stderr
    jobs = {j["id"]: j for j in json.loads(state.read_text())["jobs"]}
    msg = jobs["gw1"]["payload"]["message"]
    assert msg.startswith(CHECKIN["payload"]["message"])  # the owner's prompt stays
    assert "daily.py week" in msg and "do not ask the owner" in msg
    assert jobs["gw1"]["payload"]["toolsAllow"] == ["read", "write", "exec"]
    assert jobs["o"]["payload"] == {}
    # Idempotent: a second run changes nothing and makes no edit.
    r = run_script(CHECKIN_SCRIPT, env)
    assert r.returncode == 0 and "unchanged" in r.stdout
    edits = [
        ln
        for ln in (tmp_path / "calls.jsonl").read_text().splitlines()
        if '"edit"' in ln
    ]
    assert len(edits) == 1
    assert msg.count("<<LEETCODE-COUNT") == 1


def test_the_checkin_count_is_what_the_record_says(tmp_path, vault):
    # The command the check-in runs, against a record with two solved problems.
    for slug, day in (("two-sum", MON), ("valid-sudoku", WED)):
        daily(
            vault,
            *("done", slug, "--pattern", "p", "--time", "t", "--space", "s"),
            *("--date", day),
            day=day,
        )
    state, env = fake_box(tmp_path, [CHECKIN])
    run_script(CHECKIN_SCRIPT, env)
    msg = json.loads(state.read_text())["jobs"][0]["payload"]["message"]
    command = msg.split("run '")[1].split("'")[0]
    assert command.startswith("python3 /home/node/.openclaw/agents/learnclaw/")
    assert command.endswith("/skills/leetcode-daily/daily.py week")
    out = daily(vault, "week", day=SUN).stdout.splitlines()[0]
    assert out.startswith("solved: 2 (")


def test_the_checkin_dry_run_edits_nothing_and_a_missing_row_is_an_error(tmp_path):
    state, env = fake_box(tmp_path, [CHECKIN], {"DRY_RUN": "1"})
    r = run_script(CHECKIN_SCRIPT, env)
    assert r.returncode == 0 and "DRY_RUN" in r.stdout
    assert json.loads(state.read_text())["jobs"][0] == CHECKIN
    state, env = fake_box(tmp_path, [])
    r = run_script(CHECKIN_SCRIPT, env)
    assert r.returncode == 1 and "no 'goal-weekly-checkin' automation row" in r.stderr


def test_the_install_puts_the_skill_in_the_workspace(tmp_path):
    ws = tmp_path / "state/agents/learnclaw/workspace"
    ws.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", "#!/bin/sh\nexit 0\n"), ("curl", "#!/bin/sh\nexit 22\n")):
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
    for f in ("SKILL.md", "problems.json", "daily.py"):
        assert (ws / "skills/leetcode-daily" / f).read_bytes() == (SKILL / f).read_bytes()
    assert os.access(ws / "skills/leetcode-daily/daily.py", os.X_OK)
    # It sits next to learning-record, where daily.py finds record.py.
    assert (ws / "skills/learning-record/record.py").is_file()
