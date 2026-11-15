"""ensure-learning-line.sh moves the daily plan's learning objective line to learnclaw.

`docker` is stubbed like in test_today_brief_fleet.py: `cron list` prints a state
file, `cron edit` records its argv and applies --message and --tools to it. The
prompts here are synthetic, with the same anchors as the live rows (the live
prompts hold personal plans and never enter git).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ensure-learning-line.sh"

EVENING = (
    "Evening plan. Read-only everywhere else.\n\nGather:\n"
    "1. Day check: run the script.\n"
    "3. Priorities and rules: read /home/node/memory/system/goals.md, "
    "/home/node/memory/domains/learning.md and /home/node/memory/domains/career.md "
    "when it exists. Only apply them.\n"
    "4. Learning goal: learning.md holds an ordered list of goals. Pick the next "
    "unfinished goal and quote its prompt.\n"
    "5. Fleet and week: run the status script.\n\n"
    "Every day has exactly one concrete learning objective (the picked goals) and one "
    "practical output; home days: 07:30 Track A goal, problems at 17:30, Track B/C goal "
    "in the 08:30 block. Work comes first.\n"
    'Note format: "## Learning objective" (one line).'
)
BRIEF = (
    "Today brief.\n\nGather:\n"
    '7. Plan: read the note. Keep its "## Plan" block, the learning objective and the '
    'practical output. If the note is missing, say once "Plan note missing" and build a '
    "short plan from today's calendar and /home/node/memory/domains/learning.md instead "
    '(mark assumed blocks "(assumed)"). Never ask him anything.\n\n'
    "Message (Telegram, max 12 lines):\nLine 1: the date."
)
EVENING_TOOLS = ["read", "write", "exec", "memory_get"]

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ["FAKE_STATE"])
args = sys.argv[1:]
if args[0] == "ps":
    print("gw123")
    sys.exit(0)
cmd = args[args.index("openclaw") + 1:]
data = json.loads(state.read_text())
if cmd[:2] == ["cron", "list"]:
    print(json.dumps(data))
elif cmd[:2] == ["cron", "edit"]:
    with (state.parent / "edits.jsonl").open("a") as f:
        f.write(json.dumps(cmd) + "\n")
    for job in data["jobs"]:
        if job["id"] == cmd[2]:
            if "--message" in cmd:
                job["payload"]["message"] = cmd[cmd.index("--message") + 1]
            if "--tools" in cmd:
                job["payload"]["toolsAllow"] = cmd[cmd.index("--tools") + 1].split(",")
    state.write_text(json.dumps(data))
else:
    sys.exit("unexpected: " + " ".join(cmd))
"""


def run(tmp_path: Path, evening: str = EVENING, brief: str = BRIEF, env_extra=None):
    state = tmp_path / "state.json"
    if not state.exists():
        state.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "other",
                            "name": "needs-you-digest",
                            "payload": {"message": "x"},
                        },
                        {
                            "id": "ev1",
                            "name": "evening-plan-note",
                            "payload": {
                                "kind": "agentTurn",
                                "message": evening,
                                "toolsAllow": EVENING_TOOLS,
                            },
                        },
                        {
                            "id": "br1",
                            "name": "today-brief",
                            "payload": {"kind": "agentTurn", "message": brief},
                        },
                    ]
                }
            )
        )
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_STATE": str(state),
        **(env_extra or {}),
    }
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    return res, state


def edits(tmp_path: Path) -> list[list[str]]:
    f = tmp_path / "edits.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def jobs(state: Path) -> dict[str, dict]:
    return {j["id"]: j["payload"] for j in json.loads(state.read_text())["jobs"]}


def test_one_edit_per_row_and_nothing_else_touched(tmp_path):
    res, state = run(tmp_path)
    assert res.returncode == 0, res.stderr
    ev, br = edits(tmp_path)
    assert ev[:3] == ["cron", "edit", "ev1"] and br[:3] == ["cron", "edit", "br1"]
    assert ev[3] == "--message" and ev[5] == "--tools" and len(ev) == 7
    assert br[3] == "--message" and len(br) == 5  # the brief's tools are not touched
    assert jobs(state)["other"] == {"message": "x"}


def test_the_evening_row_asks_learnclaw_and_gets_the_subagent_tools(tmp_path):
    res, state = run(tmp_path)
    assert res.returncode == 0, res.stderr
    p = jobs(state)["ev1"]
    m = p["message"]
    assert "sessions_spawn (agentId learnclaw" in m
    assert "mirror newest session <YYYY-MM-DD>" in m
    assert "Write nothing anywhere and record no row" in m
    assert "Never write into LearnHarness" in m
    assert "Learning objective unavailable (learnclaw)" in m
    assert "Learning goal" not in m and "domains/learning.md" not in m
    assert "Track A" not in m and "(the picked goals)" not in m
    assert "07:30 and the 08:30 block work toward that objective" in m
    assert "goals.md and /home/node/memory/domains/career.md" in m
    assert p["toolsAllow"] == [*EVENING_TOOLS, "sessions_spawn", "sessions_yield"]
    assert "Gather:" in m and "5. Fleet and week" in m and "Work comes first." in m


def test_the_brief_copies_the_line_and_never_derives_it(tmp_path):
    res, state = run(tmp_path)
    assert res.returncode == 0, res.stderr
    m = jobs(state)["br1"]["message"]
    assert 'its "## Learning objective" line' in m
    assert "Never derive the learning objective yourself" in m
    assert "domains/learning.md" not in m
    assert "Never ask him anything." in m and "Message (Telegram" in m


def test_rerun_is_idempotent(tmp_path):
    run(tmp_path)
    res, _ = run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout.count("nothing to do") == 2
    assert len(edits(tmp_path)) == 2


def test_a_row_with_new_text_but_missing_tools_gets_only_the_tools(tmp_path):
    run(tmp_path)
    st = tmp_path / "state.json"
    data = json.loads(st.read_text())
    data["jobs"][1]["payload"]["toolsAllow"] = EVENING_TOOLS
    st.write_text(json.dumps(data))
    res, state = run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert jobs(state)["ev1"]["toolsAllow"][-2:] == ["sessions_spawn", "sessions_yield"]
    assert len(edits(tmp_path)) == 3


def test_a_prompt_without_the_anchors_is_left_untouched(tmp_path):
    res, state = run(tmp_path, evening="A rewritten prompt.", brief="Also rewritten.")
    assert res.returncode == 1
    assert res.stderr.count("left untouched") == 2
    assert edits(tmp_path) == []
    assert jobs(state)["ev1"]["message"] == "A rewritten prompt."


def test_one_changed_row_does_not_block_the_other(tmp_path):
    res, state = run(tmp_path, brief="Also rewritten.")
    assert res.returncode == 1
    assert "learnclaw" in jobs(state)["ev1"]["message"]
    assert jobs(state)["br1"]["message"] == "Also rewritten."


def test_dry_run_edits_nothing(tmp_path):
    res, _ = run(tmp_path, env_extra={"DRY_RUN": "1"})
    assert res.returncode == 0, res.stderr
    assert "sessions_spawn (agentId learnclaw" in res.stdout
    assert edits(tmp_path) == []
