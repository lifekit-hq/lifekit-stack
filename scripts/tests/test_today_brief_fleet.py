"""ensure-today-brief-fleet.sh adds the fleet step to the `today-brief` cron
row's payload.message, and only that.

`docker` is stubbed like in test_finance_pulse.py: `ps` returns a fake gateway
id, `exec ... openclaw cron list` prints a state file, and `cron edit` records
its argv and applies --message to that state, so a second run sees what the
first one wrote. The message here is a synthetic prompt with the same anchors
as the live one.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ensure-today-brief-fleet.sh"

PROMPT = (
    "Today brief. Read-only everywhere.\n\n"
    "Gather:\n1. Calendar: today's events.\n2. Mail: needs action.\n"
    "5. PRs: count non-draft PRs.\n\n"
    "Message (Telegram, max 10 lines, no preamble):\n"
    "Line 1: the date.\n"
    "Then only sections that have content, in this order: Calendar, Needs action, PRs waiting.\n"
    "If calendar, mail, deadlines-within-7-days and non-bot PRs are all empty, reply exactly NO_REPLY."
)

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
    msg = cmd[cmd.index("--message") + 1]
    for job in data["jobs"]:
        if job["id"] == cmd[2]:
            job["payload"]["message"] = msg
    state.write_text(json.dumps(data))
else:
    sys.exit("unexpected: " + " ".join(cmd))
"""


def run(tmp_path: Path, message: str = PROMPT, name: str = "today-brief"):
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
                            "id": "job1",
                            "name": name,
                            "enabled": True,
                            "payload": {"kind": "agentTurn", "message": message},
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
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    ), state


def edits(tmp_path: Path) -> list[list[str]]:
    f = tmp_path / "edits.jsonl"
    return (
        [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []
    )


def test_the_fleet_step_is_added_and_the_rest_of_the_prompt_is_kept(tmp_path):
    res, state = run(tmp_path)
    assert res.returncode == 0, res.stderr
    (cmd,) = edits(tmp_path)
    assert cmd[:3] == ["cron", "edit", "job1"]  # today-brief, not the digest
    assert cmd[3] == "--message" and len(cmd) == 5  # nothing but the message
    message = json.loads(state.read_text())["jobs"][1]["payload"]["message"]
    assert message == cmd[4]
    assert (
        "6. Fleet: run `sh /home/node/.openclaw/agents/kit/workspace/skills/"
        "secondmate-relay/relay.sh status`" in message
    )
    for part in ("decisions_open", "Status is stale", "Status is invalid for", "landed"):
        assert part in message, part
    assert "send no relay note" in message
    # the step sits inside Gather, before the Message block; the original
    # lines are all still there
    assert message.index("6. Fleet") < message.index("Message (Telegram")
    for line in PROMPT.split("\n"):
        if "reply exactly NO_REPLY" not in line:
            assert line in message
    assert message.count("NO_REPLY") == 1


def test_rerun_is_idempotent(tmp_path):
    run(tmp_path)
    res, _ = run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert "nothing to do" in res.stdout
    assert len(edits(tmp_path)) == 1


def test_a_prompt_without_the_anchors_is_left_untouched(tmp_path):
    res, state = run(tmp_path, message="A rewritten prompt with no known anchors.")
    assert res.returncode == 1
    assert "left untouched" in res.stderr
    assert edits(tmp_path) == []
    assert (
        json.loads(state.read_text())["jobs"][1]["payload"]["message"]
        == "A rewritten prompt with no known anchors."
    )


def test_missing_row_fails_without_editing(tmp_path):
    res, _ = run(tmp_path, name="something-else")
    assert res.returncode == 1
    assert "no 'today-brief' automation row" in res.stderr
    assert edits(tmp_path) == []
