"""ensure-kit-cron-models.sh pins the repo's cron model (agents.defaults.model.primary)
on every Kit cron job that has none or names another model (outside
modelPolicy.allow), leaves a job already on it alone, and never touches another
agent's job.

`docker` is stubbed: `ps` returns a fake gateway id, `cron list` prints a state
file, and `cron edit --model` records its argv and applies the model to it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ensure-kit-cron-models.sh"
SONNET = json.loads(
    (REPO / "compose/openclaw-gateway/platform.patch.json").read_text()
)["agents"]["defaults"]["model"]["primary"]

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")

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
    if cmd[2] == os.environ.get("FAIL_ID"):
        sys.exit(1)
    with (state.parent / "edits.jsonl").open("a") as f:
        f.write(json.dumps(cmd) + "\n")
    for job in data["jobs"]:
        if job["id"] == cmd[2]:
            job["payload"]["model"] = cmd[cmd.index("--model") + 1]
    state.write_text(json.dumps(data))
else:
    sys.exit("unexpected: " + " ".join(cmd))
"""

JOBS = {
    "jobs": [
        {"id": "a", "name": "today-brief", "agentId": "kit", "payload": {}},
        {"id": "b", "name": "vault", "agentId": "kit", "payload": {"model": SONNET}},
        {
            "id": "e",
            "name": "weekly",
            "agentId": "kit",
            "payload": {"model": "anthropic/claude-opus-4-7"},
        },
        {"id": "c", "name": "ledger-pulse", "agentId": "finance", "payload": {}},
        {"id": "d", "name": "drain", "agentId": "kit", "payload": {"model": ""}},
    ]
}


def run(tmp_path: Path, *args: str, fail_id: str = "", jobs: dict = JOBS):
    state = tmp_path / "state.json"
    if not state.exists():
        state.write_text(json.dumps(jobs))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_STATE": str(state),
        "FAIL_ID": fail_id,
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env
    )
    edits = tmp_path / "edits.jsonl"
    done = (
        [json.loads(line) for line in edits.read_text().splitlines()]
        if edits.exists()
        else []
    )
    return proc, json.loads(state.read_text()), done


def test_repins_unpinned_and_off_policy_kit_jobs_only(tmp_path):
    proc, state, edits = run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    models = {j["id"]: j["payload"].get("model") for j in state["jobs"]}
    assert models == {"a": SONNET, "b": SONNET, "c": None, "d": SONNET, "e": SONNET}
    assert sorted(e[2] for e in edits) == ["a", "d", "e"]


def test_second_run_changes_nothing(tmp_path):
    run(tmp_path)
    proc, _, edits = run(tmp_path)
    assert proc.returncode == 0
    assert len(edits) == 3  # only the first run's edits


def test_check_reports_and_fails_without_editing(tmp_path):
    proc, state, edits = run(tmp_path, "--check")
    assert proc.returncode == 1
    assert "unpinned: 'today-brief' (a)" in proc.stdout
    assert "off-policy: 'weekly' (e) runs on anthropic/claude-opus-4-7" in proc.stdout
    assert edits == []
    assert "model" not in state["jobs"][0]["payload"]


def test_a_failed_edit_does_not_stop_the_rest_and_fails_the_run(tmp_path):
    proc, state, _ = run(tmp_path, fail_id="a")
    assert proc.returncode == 1
    assert state["jobs"][4]["payload"]["model"] == SONNET
    assert "failed: could not pin 'today-brief'" in proc.stderr


def test_a_row_naming_no_agent_is_kits(tmp_path):
    jobs = {
        "jobs": [
            {"id": "n", "name": "chat-made", "payload": {}},
            {"id": "c", "name": "ledger-pulse", "agentId": "finance", "payload": {}},
        ]
    }
    proc, state, edits = run(tmp_path, jobs=jobs)
    assert proc.returncode == 0, proc.stderr
    assert [e[2] for e in edits] == ["n"]
    assert state["jobs"][0]["payload"]["model"] == SONNET
    assert "model" not in state["jobs"][1]["payload"]


@pytest.mark.parametrize("args", [(), ("--check",)])
def test_zero_matching_jobs_warns_and_fails(tmp_path, args):
    jobs = {"jobs": [{"id": "c", "name": "x", "agentId": "finance", "payload": {}}]}
    proc, _, edits = run(tmp_path, *args, jobs=jobs)
    assert proc.returncode == 1
    assert "no kit cron jobs matched" in proc.stderr
    assert edits == []


@pytest.mark.parametrize("args", [(), ("--check",)])
def test_command_jobs_are_skipped_not_pinned_or_flagged(tmp_path, args):
    jobs = {
        "jobs": [
            {
                "id": "m",
                "name": "evening-day-plan",
                "agentId": "kit",
                "payload": {"kind": "command"},
            },
            {
                "id": "p",
                "name": "pinned",
                "agentId": "kit",
                "payload": {"model": SONNET},
            },
        ]
    }
    proc, state, edits = run(tmp_path, *args, jobs=jobs)
    assert proc.returncode == 0, proc.stderr
    assert "skipped: command job 'evening-day-plan' (m)" in proc.stdout
    assert edits == []
    assert "model" not in state["jobs"][0]["payload"]
