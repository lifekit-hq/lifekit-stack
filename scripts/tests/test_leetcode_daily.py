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
    r = daily(
        vault,
        "done",
        "no-such-problem",
        "--pattern",
        "x",
        "--time",
        "y",
        "--space",
        "z",
    )
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
    assert daily(vault, "week", day=NEXT_MON).stdout.startswith(
        "solved: 1 (Monday 2026-11-30"
    )
    # Offered and skipped problems are not solved.
    send(vault, WED)
    daily(vault, "skip", "product-of-array-except-self", day=FRI)
    assert daily(vault, "week", day=SUN).stdout.startswith("solved: 3 ")


# --- the weekly count file the morning message quotes (#620) -------------------------


def weekfile(vault, tmp_path, day, target=20):
    out = tmp_path / "weekly-count.json"
    r = daily(
        vault, "weekfile", "--out", str(out), "--target", str(target), "--date", day
    )
    return r, (json.loads(out.read_text()) if out.exists() else None)


def solve_row(vault, slug, day, status="done", topic=None):
    page = vault / "domains/learning.md"
    row = f"| {day} | {topic or 'leetcode: ' + slug} | https://leetcode.com/problems/{slug}/ | - | - | - | - | {status} |\n"
    head, sep, tail = page.read_text().partition("\n## In-progress")
    page.write_text(head.rstrip("\n") + "\n" + row + sep + tail)  # inside the table


def test_the_count_line_rolls_over_on_monday(vault, tmp_path):
    solve_row(vault, "two-sum", SUN_BEFORE)  # last week
    solve_row(vault, "group-anagrams", SUN)  # done on Sunday of this week
    r, out = weekfile(vault, tmp_path, SUN)
    assert r.returncode == 0 and r.stdout == "NO_REPLY\n"
    assert out == {"for_date": SUN, "line": "Sun 1/20"}
    # Next Monday only a row dated that day counts: the week starts again at 0.
    r, out = weekfile(vault, tmp_path, NEXT_MON)
    assert out == {"for_date": NEXT_MON, "line": "Mon 0/20"}
    solve_row(vault, "valid-sudoku", NEXT_MON)
    assert weekfile(vault, tmp_path, NEXT_MON)[1]["line"] == "Mon 1/20"
    # A row dated after today is not counted yet.
    assert weekfile(vault, tmp_path, SUN)[1]["line"] == "Sun 1/20"


def test_the_count_line_counts_whole_slugs_by_their_latest_row(vault, tmp_path):
    solve_row(vault, "two-sum", MON, "in progress")
    solve_row(vault, "two-sum", MON)
    solve_row(vault, "two-sum", TUE)  # a repeated done row: still one slug
    solve_row(vault, "group-anagrams", WED)
    solve_row(vault, "group-anagrams", THU, "dropped")  # latest row decides
    solve_row(vault, "valid-sudoku", WED, "in progress")
    solve_row(vault, "x", WED, topic="caching")  # not a leetcode topic
    r, out = weekfile(vault, tmp_path, FRI, target=25)
    assert r.returncode == 0, r.stderr
    assert out["line"] == "Fri 1/25"
    # The line is the number `week` prints, from the same code.
    assert daily(vault, "week", day=FRI).stdout.startswith("solved: 1 ")


def test_a_row_with_an_unreadable_date_counts_nothing_and_the_rest_still_count(
    vault, tmp_path
):
    solve_row(vault, "two-sum", "someday")
    solve_row(vault, "group-anagrams", "2026-02-30")  # not a calendar date
    solve_row(vault, "valid-sudoku", TUE)
    r, out = weekfile(vault, tmp_path, WED)
    assert r.returncode == 0, r.stderr
    assert out["line"] == "Wed 1/20"


def test_an_unreadable_record_writes_unreadable_never_zero_and_fails_the_run(
    vault, tmp_path
):
    solve_row(vault, "two-sum", TUE)
    out = tmp_path / "weekly-count.json"
    out.write_text(json.dumps({"for_date": TUE, "line": "Tue 1/20"}))  # yesterday's
    (vault / "domains/learning.md").unlink()
    r, got = weekfile(vault, tmp_path, WED)
    assert r.returncode == 1 and "cannot count the week" in r.stderr
    assert r.stdout == ""
    assert got == {"for_date": WED, "line": "Wed count unreadable (target 20)"}
    assert "0/" not in got["line"]


def test_a_bad_target_is_refused_and_leaves_the_file(vault, tmp_path):
    r = daily(vault, "weekfile", "--out", str(tmp_path / "w.json"), "--target", "0")
    assert r.returncode == 2 and not (tmp_path / "w.json").exists()


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
    if flag("--command-argv") is not None:
        job["payload"] = p = {"kind": "command", "argv": json.loads(flag("--command-argv"))}
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
    # A command payload, not an agent turn: no model, no tools, so a daily.py
    # error is the run's own non-zero exit.
    assert job["payload"]["kind"] == "command"
    assert job["payload"]["argv"] == [
        "python3",
        "/home/node/.openclaw/agents/learnclaw/workspace/skills/leetcode-daily/daily.py",
        "next",
        "--record",
        "--push",
    ]
    assert "model" not in job["payload"] and "message" not in job["payload"]
    assert job["schedule"] == {"expr": "30 17 * * 1,3,5,6", "tz": "Europe/Dublin"}
    assert job["delivery"] == {
        "mode": "announce",
        "channel": "telegram",
        "accountId": "reading",
        "to": "424242",
    }
    # Re-running converges on the same row.
    assert run_script(CRON_SCRIPT, env).returncode == 0
    assert len(json.loads(state.read_text())["jobs"]) == 1


def test_an_old_agent_turn_row_is_replaced_by_the_command(tmp_path):
    old = {
        "id": "old1",
        "name": "leetcode-daily",
        "agentId": "learnclaw",
        "payload": {"kind": "agentTurn", "message": "x", "model": MODEL},
    }
    state, env = fake_box(tmp_path, [old])
    r = run_script(CRON_SCRIPT, env)
    assert r.returncode == 0, r.stderr
    (job,) = json.loads(state.read_text())["jobs"]
    assert job["id"] == "new1" and job["payload"]["kind"] == "command"


def push(vault, day=MON):
    return daily(vault, "next", "--record", "--push", day=day)


def test_push_prints_only_the_message_and_records(vault):
    r = push(vault)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert len(lines) == 2 and lines[1].startswith("https://leetcode.com/problems/")
    assert "SEND" not in r.stdout
    # The offered row was recorded, so the same day pushes nothing.
    assert push(vault).stdout == "NO_REPLY\n"


@pytest.mark.parametrize("day", [TUE, THU, SUN])
def test_push_on_a_skip_day_is_no_reply_and_records_nothing(vault, day):
    page = (vault / "domains/learning.md").read_text()
    r = push(vault, day)
    assert r.returncode == 0 and r.stdout == "NO_REPLY\n"
    assert (vault / "domains/learning.md").read_text() == page


def test_an_error_run_is_counted_as_failed_by_the_cron_command(vault):
    # The cron runs exactly this argv; the scheduler counts a non-zero exit as
    # a failed run (and fires the failure alert). Nothing reaches the chat.
    (vault / "domains/learning.md").unlink()
    r = push(vault)
    assert r.returncode != 0
    assert r.stdout == ""
    assert r.stderr.strip()


def test_a_lint_that_does_not_run_fails_the_push_and_leaves_the_page(vault):
    (vault / "bin/lint.sh").write_text("#!/bin/sh\necho boom >&2\nexit 2\n")
    page = (vault / "domains/learning.md").read_text()
    r = push(vault)
    assert r.returncode != 0 and r.stdout == ""
    assert (vault / "domains/learning.md").read_text() == page


def test_an_unreadable_problem_list_fails_the_push(vault, tmp_path):
    bad = tmp_path / "problems.json"
    bad.write_text("{not json")
    r = subprocess.run(
        [sys.executable, str(DAILY), "next", "--record", "--push"],
        env={
            **os.environ,
            "LEARNING_VAULT": str(vault),
            "LEETCODE_TODAY": MON,
            "LEETCODE_PROBLEMS": str(bad),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode != 0 and r.stdout == ""


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
    "expr",
    [
        "30 17 * * 1,2,3,5,6",
        "30 17 * * 4",
        "30 17 * * 0",
        "30 17 * * *",
        "30 17 * * 1-5",
    ],
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
    state, env = fake_box(
        tmp_path, [CHECKIN, {"id": "o", "name": "other", "payload": {}}]
    )
    r = run_script(CHECKIN_SCRIPT, env)
    assert r.returncode == 0, r.stderr
    jobs = {j["id"]: j for j in json.loads(state.read_text())["jobs"]}
    msg = jobs["gw1"]["payload"]["message"]
    assert msg.startswith(CHECKIN["payload"]["message"])  # the owner's prompt stays
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


def test_the_checkin_count_is_what_the_record_says(tmp_path, vault):
    # The weekly count the check-in reads, against a record with two solved problems.
    for slug, day in (("two-sum", MON), ("valid-sudoku", WED)):
        daily(
            vault,
            *("done", slug, "--pattern", "p", "--time", "t", "--space", "s"),
            *("--date", day),
            day=day,
        )
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
    for f in ("SKILL.md", "problems.json", "daily.py"):
        assert (ws / "skills/leetcode-daily" / f).read_bytes() == (
            SKILL / f
        ).read_bytes()
    assert os.access(ws / "skills/leetcode-daily/daily.py", os.X_OK)
    # It sits next to learning-record, where daily.py finds record.py.
    assert (ws / "skills/learning-record/record.py").is_file()


# --- ensure-learnclaw-weekcount.sh (#620) ------------------------------------------

WEEKCOUNT_SCRIPT = REPO / "scripts/ensure-learnclaw-weekcount.sh"
COUNT_FILE = "/home/node/.openclaw/agents/learnclaw/workspace/weekly-count.json"


def weekcount_env(tmp_path, extra=None):
    return fake_box(
        tmp_path, [], {"LEARNCLAW_WEEKCOUNT_CHAT": "424242", **(extra or {})}
    )


def test_the_weekcount_job_is_a_command_that_sends_nothing_itself(tmp_path):
    state, env = weekcount_env(tmp_path)
    r = run_script(WEEKCOUNT_SCRIPT, env)
    assert r.returncode == 0, r.stderr
    (job,) = json.loads(state.read_text())["jobs"]
    assert job["name"] == "learnclaw-weekcount" and job["agentId"] == "learnclaw"
    assert job["schedule"]["expr"] == "10 7 * * 1,2,3,4,5,6"  # before the 07:15 message
    argv = job["payload"]["argv"]
    assert argv[0] == "python3" and argv[1].endswith("leetcode-daily/daily.py")
    assert argv[2:] == ["weekfile", "--out", COUNT_FILE, "--target", "20"]
    # No model turn is involved in counting.
    assert job["payload"]["kind"] == "command" and "model" not in job["payload"]


def test_the_weekly_target_is_the_one_named_setting(tmp_path):
    state, env = weekcount_env(tmp_path, {"LEARNCLAW_WEEKLY_TARGET": "25"})
    assert run_script(WEEKCOUNT_SCRIPT, env).returncode == 0
    (job,) = json.loads(state.read_text())["jobs"]
    assert job["payload"]["argv"][-1] == "25"


@pytest.mark.parametrize("bad", ["0", "abc", "-5", "2.5", "05"])
def test_a_bad_weekly_target_is_refused(tmp_path, bad):
    state, env = weekcount_env(tmp_path, {"LEARNCLAW_WEEKLY_TARGET": bad})
    r = run_script(WEEKCOUNT_SCRIPT, env)
    assert r.returncode == 1 and "LEARNCLAW_WEEKLY_TARGET" in r.stderr
    assert json.loads(state.read_text())["jobs"] == []


def test_the_weekcount_schedule_cannot_fire_on_sunday(tmp_path):
    state, env = weekcount_env(tmp_path, {"LEARNCLAW_WEEKCOUNT_CRON": "10 7 * * 1,7"})
    r = run_script(WEEKCOUNT_SCRIPT, env)
    assert r.returncode == 1 and "Sunday" in r.stderr
    assert json.loads(state.read_text())["jobs"] == []
