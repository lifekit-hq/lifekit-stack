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
import re
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
    assert set(re.findall(r"relay\.sh (\w+)", message)) == {"status"}
    assert "NO_REPLY" not in message
    assert "Line 1: the date." in message
    # the step sits inside Gather, before the Message block; the original
    # lines are all still there
    assert message.index("6. Fleet") < message.index("Message (Telegram")
    for line in PROMPT.split("\n"):
        if "reply exactly NO_REPLY" not in line:
            assert line in message


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


OLD_STEP = (
    "6. Fleet: run `sh /home/node/.openclaw/agents/kit/workspace/skills/"
    "secondmate-relay/relay.sh status` "
    "(read-only; send no relay note, never ask_user). Keep: the line "
    '"Status is stale: generated <age> ago." '
    "when generated_epoch is more than 5 minutes old; one line "
    '"Status is invalid for <home id>: <reason>." '
    "per homes[] entry whose valid is false; counts.active_children, "
    "counts.queued and the number of landed "
    "items whose completion.date is today; each decisions_open item as "
    '"<home_id> / <id> - <summary>" '
    "(and how many are not shown when counts.decisions_open exceeds the list). "
    "If the command fails, keep one "
    'line "Fleet status unavailable: <stderr line>."'
)
OLD_FLEET = (
    "After PRs waiting, add the Fleet section from step 6 when it has anything "
    "to say: warnings first, then "
    '"Fleet: N active, N queued, N landed today", then the open decisions, one '
    "per line (this section is "
    "outside the 10-line limit). If calendar, mail, deadlines-within-7-days "
    "and non-bot PRs are all empty and "
    "step 6 found no open decisions and no warning, reply exactly NO_REPLY."
)


def old_version_of(prompt: str) -> str:
    """The prompt as the previous version of this script left it."""
    return prompt.replace(
        "\n\nMessage (Telegram", "\n" + OLD_STEP + "\n\nMessage (Telegram"
    ).replace(
        "If calendar, mail, deadlines-within-7-days and non-bot PRs are all empty, reply exactly NO_REPLY.",
        OLD_FLEET,
    )


def test_a_row_with_the_previous_step_is_upgraded_in_place(tmp_path):
    res, state = run(tmp_path, message=old_version_of(PROMPT))
    assert res.returncode == 0, res.stderr
    (cmd,) = edits(tmp_path)
    message = json.loads(state.read_text())["jobs"][1]["payload"]["message"]
    assert message == cmd[4]
    # the same prompt a fresh row gets, and nothing of the old step left
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    _, fresh_state = run(fresh_dir)
    fresh = json.loads(fresh_state.read_text())["jobs"][1]["payload"]["message"]
    assert message == fresh
    assert "Status is stale" not in message
    assert "NO_REPLY" not in message
    assert message.count("6. Fleet") == 1
    # and converged: another run changes nothing
    res, _ = run(tmp_path)
    assert res.returncode == 0 and "nothing to do" in res.stdout
    assert len(edits(tmp_path)) == 1


def test_a_hand_edited_fleet_step_is_left_untouched(tmp_path):
    edited = old_version_of(PROMPT).replace("Keep: the line", "Keep: also the line")
    res, state = run(tmp_path, message=edited)
    assert res.returncode == 1
    assert "left untouched" in res.stderr
    assert edits(tmp_path) == []
    assert json.loads(state.read_text())["jobs"][1]["payload"]["message"] == edited


# The step text a live row carries before the evidence-based Google rule.
PREV_GOOGLE_TAIL = (
    "Never call start_google_auth and never show an auth URL: when "
    "Google calendar or mail fails with an expired or revoked sign-in, say once "
    '"Google sign-in expired - mail and calendar skipped" and add the to-do '
    '"Redo the Google sign-in on the PC (docs/google-mcp-setup.md, steps 6-7)".'
)


def fresh_message(tmp_path: Path) -> str:
    d = tmp_path / "fresh"
    d.mkdir()
    _, st = run(d)
    return json.loads(st.read_text())["jobs"][1]["payload"]["message"]


def test_a_fresh_insert_carries_the_evidence_based_google_rule(tmp_path):
    message = fresh_message(tmp_path)
    assert (
        "Never call start_google_auth and never show an auth URL. Make the real "
        "google-workspace calendar and mail calls on every run" in message
    )
    assert "never reuse a sign-in verdict from an earlier run or from memory" in message
    for evidence in ("invalid_grant", "ACTION REQUIRED", "Authentication Needed"):
        assert evidence in message
    assert "the tool's own error text says the sign-in is expired or revoked" in message
    assert (
        '"Google mail and calendar unavailable: <the error line, or tools not '
        'available>." and add no to-do'
    ) in message
    assert PREV_GOOGLE_TAIL not in message


def test_a_row_with_the_current_step_is_upgraded_in_place(tmp_path):
    fresh = fresh_message(tmp_path)
    start = fresh.index("6. Fleet: run")
    # the step as it is live today: the new step with the old Google tail
    prev_prompt = (
        fresh[: fresh.index("Never call start_google_auth", start)]
        + PREV_GOOGLE_TAIL
        + fresh[fresh.index("\n\nMessage (Telegram") :]
    )
    assert prev_prompt != fresh
    res, state = run(tmp_path, message=prev_prompt)
    assert res.returncode == 0, res.stderr
    (cmd,) = edits(tmp_path)
    message = json.loads(state.read_text())["jobs"][1]["payload"]["message"]
    assert message == cmd[4] == fresh
    assert PREV_GOOGLE_TAIL not in message
    assert message.count("6. Fleet") == 1


def test_a_row_with_the_new_step_is_idempotent(tmp_path):
    run(tmp_path)
    res, _ = run(tmp_path)
    assert res.returncode == 0 and "nothing to do" in res.stdout
    assert len(edits(tmp_path)) == 1


# The step text a live row carries before the Google argument names.
CURRENT_GOOGLE_ARGS = (
    " Use these argument names: search_gmail_messages takes query and page_size "
    "(never max_results); get_events takes calendar_id, time_min, time_max and "
    "max_results (never calendarId); list_calendars takes no required argument; "
    "leave user_google_email out."
)


def test_a_fresh_insert_names_the_google_tool_arguments(tmp_path):
    message = fresh_message(tmp_path)
    assert (
        "from an earlier run or from memory." + CURRENT_GOOGLE_ARGS + " Only when"
    ) in message
    assert (
        "search_gmail_messages takes query and page_size (never max_results)" in message
    )
    assert "get_events takes calendar_id" in message
    assert "(never calendarId)" in message


def test_a_row_with_the_step_before_the_argument_names_is_upgraded(tmp_path):
    fresh = fresh_message(tmp_path)
    prev_prompt = fresh.replace(CURRENT_GOOGLE_ARGS, "", 1)
    assert prev_prompt != fresh
    res, state = run(tmp_path, message=prev_prompt)
    assert res.returncode == 0, res.stderr
    (cmd,) = edits(tmp_path)
    message = json.loads(state.read_text())["jobs"][1]["payload"]["message"]
    assert message == cmd[4] == fresh
    assert message.count("6. Fleet") == 1
    res, _ = run(tmp_path)
    assert res.returncode == 0 and "nothing to do" in res.stdout
    assert len(edits(tmp_path)) == 1
