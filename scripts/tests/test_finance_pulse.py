"""ensure-finance-pulse.sh declares the pulse checklist in the `ledger-pulse`
cron row's payload.message (an agentTurn run never sees job scratch).

`docker` is stubbed: `ps` returns a fake gateway id and `exec ... openclaw cron
list` prints a state file, `cron edit` records its argv and applies --message
to that state, so a second run sees what the first one wrote.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ensure-finance-pulse.sh"
CHECKLIST = REPO / "scripts/finance-pulse.md"

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


def run(tmp_path: Path, message: str, name: str = "ledger-pulse"):
    state = tmp_path / "state.json"
    if not state.exists():
        state.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "other",
                            "name": "heartbeat-finance",
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


def test_checklist_lands_in_payload_message(tmp_path):
    old = "There is no standing task list configured right now"
    res, state = run(tmp_path, old)
    assert res.returncode == 0, res.stderr
    (cmd,) = edits(tmp_path)
    assert cmd[:3] == [
        "cron",
        "edit",
        "job1",
    ]  # the ledger-pulse row, not heartbeat-finance
    message = json.loads(state.read_text())["jobs"][1]["payload"]["message"]
    assert message == cmd[cmd.index("--message") + 1]
    assert CHECKLIST.read_text().strip() in message
    assert "NO_REPLY" in message
    assert old not in message


def test_rerun_is_idempotent(tmp_path):
    run(tmp_path, "stale")
    res, _ = run(tmp_path, "stale")
    assert res.returncode == 0, res.stderr
    assert "nothing to do" in res.stdout
    assert len(edits(tmp_path)) == 1


def test_missing_row_fails_without_editing(tmp_path):
    res, _ = run(tmp_path, "x", name="something-else")
    assert res.returncode == 1
    assert "no 'ledger-pulse' automation row" in res.stderr
    assert edits(tmp_path) == []
