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
GATE = REPO / "scripts/finance-pulse-gate"

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
            if "--trigger-script" in cmd:
                job["trigger"] = {
                    "script": pathlib.Path(os.environ["GATE_FILE"]).read_text()
                }
            if "--failure-alert" in cmd:
                job["failureAlert"] = {
                    "after": int(cmd[cmd.index("--failure-alert-after") + 1]),
                    "mode": cmd[cmd.index("--failure-alert-mode") + 1],
                    "channel": cmd[cmd.index("--failure-alert-channel") + 1],
                    "accountId": cmd[cmd.index("--failure-alert-account-id") + 1],
                    "to": cmd[cmd.index("--failure-alert-to") + 1],
                }
            if "--timeout-seconds" in cmd:
                job["payload"]["timeoutSeconds"] = int(
                    cmd[cmd.index("--timeout-seconds") + 1]
                )
    state.write_text(json.dumps(data))
else:
    sys.exit("unexpected: " + " ".join(cmd))
"""


def run(
    tmp_path: Path,
    message: str,
    name: str = "ledger-pulse",
    timeout: int | None = 180,
    env_file: str | None = "LIFEKIT_TELEGRAM_CHAT=4242\n",
):
    state = tmp_path / "state.json"
    if not state.exists():
        payload = {"kind": "agentTurn", "message": message}
        if timeout is not None:
            payload["timeoutSeconds"] = timeout
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
                            "payload": payload,
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
    envf = tmp_path / "stack.env"
    envf.write_text(env_file or "")
    env = {
        **os.environ,
        "ENV_FILE": str(envf),
        "FINANCE_PULSE_ALERT_CHAT": "",
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_STATE": str(state),
        "GATE_FILE": str(GATE / "gate.js"),
        "OPENCLAW_CONFIG_DIR": str(tmp_path / "config"),
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


def test_timeout_is_converged_to_declared_value(tmp_path):
    res, state = run(tmp_path, "stale")
    assert res.returncode == 0, res.stderr
    (cmd,) = edits(tmp_path)
    assert cmd[cmd.index("--timeout-seconds") + 1] == "300"
    assert json.loads(state.read_text())["jobs"][1]["payload"]["timeoutSeconds"] == 300


def test_timeout_alone_is_edited_without_touching_message(tmp_path):
    run(tmp_path, "stale")  # converge both
    state = tmp_path / "state.json"
    data = json.loads(state.read_text())
    data["jobs"][1]["payload"]["timeoutSeconds"] = 180
    state.write_text(json.dumps(data))
    res, _ = run(tmp_path, "stale")
    assert res.returncode == 0, res.stderr
    first, second = edits(tmp_path)
    assert "--message" not in second
    assert second[second.index("--timeout-seconds") + 1] == "300"


def test_unset_timeout_is_set(tmp_path):
    res, state = run(tmp_path, "stale", timeout=None)
    assert res.returncode == 0, res.stderr
    assert json.loads(state.read_text())["jobs"][1]["payload"]["timeoutSeconds"] == 300


def test_gate_is_installed_and_set_as_the_trigger_script(tmp_path):
    res, state = run(tmp_path, "stale")
    assert res.returncode == 0, res.stderr
    (cmd,) = edits(tmp_path)
    assert (
        cmd[cmd.index("--trigger-script") + 1]
        == "/home/node/.openclaw/gates/ledger-pulse/gate.js"
    )
    dest = tmp_path / "config/gates/ledger-pulse"
    for name in ("gate.js", "pulse_gate.py"):
        assert (dest / name).read_bytes() == (GATE / name).read_bytes()
    assert (
        json.loads(state.read_text())["jobs"][1]["trigger"]["script"].strip()
        == (GATE / "gate.js").read_text().strip()
    )


def test_a_changed_gate_alone_is_re_applied(tmp_path):
    run(tmp_path, "stale")  # converge everything
    state = tmp_path / "state.json"
    data = json.loads(state.read_text())
    data["jobs"][1]["trigger"] = {"script": "old gate"}
    state.write_text(json.dumps(data))
    res, _ = run(tmp_path, "stale")
    assert res.returncode == 0, res.stderr
    _, second = edits(tmp_path)
    assert "--message" not in second and "--timeout-seconds" not in second
    assert "--trigger-script" in second


def test_failure_alert_is_declared_to_the_owner_chat(tmp_path):
    res, state = run(tmp_path, "stale")
    assert res.returncode == 0, res.stderr
    (cmd,) = edits(tmp_path)
    assert "--failure-alert" in cmd
    assert cmd[cmd.index("--failure-alert-to") + 1] == "4242"
    assert cmd[cmd.index("--failure-alert-after") + 1] == "1"
    assert cmd[cmd.index("--failure-alert-mode") + 1] == "announce"
    assert cmd[cmd.index("--failure-alert-channel") + 1] == "telegram"
    assert cmd[cmd.index("--failure-alert-account-id") + 1] == "default"
    assert json.loads(state.read_text())["jobs"][1]["failureAlert"]["to"] == "4242"


def test_a_missing_failure_alert_alone_is_re_applied(tmp_path):
    run(tmp_path, "stale")  # converge everything
    state = tmp_path / "state.json"
    data = json.loads(state.read_text())
    del data["jobs"][1]["failureAlert"]
    state.write_text(json.dumps(data))
    res, _ = run(tmp_path, "stale")
    assert res.returncode == 0, res.stderr
    _, second = edits(tmp_path)
    assert "--failure-alert" in second
    assert "--message" not in second and "--trigger-script" not in second


def test_no_owner_chat_fails_without_editing(tmp_path):
    res, _ = run(tmp_path, "stale", env_file=None)
    assert res.returncode == 1
    assert "failure alert" in res.stderr
    assert edits(tmp_path) == []
