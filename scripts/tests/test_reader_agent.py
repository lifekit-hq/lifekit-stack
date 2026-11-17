"""The reader agent: the shipped entry (defaults/agents/reader/agent.json), its
convergence script (scripts/ensure-reader-agent.sh) and the two crons that run as it.

The script talks to the gateway only through `docker exec <gateway> openclaw ...`, so
the tests put a stateful fake `docker` first on PATH. The tool cap itself is proved on
a real claude-cli turn by scripts/reader-canary (test_reader_canary.py).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENSURE = REPO / "scripts/ensure-reader-agent.sh"
SHIPPED = json.loads((REPO / "defaults/agents/reader/agent.json").read_text())
ENTRY = SHIPPED["agents"]["entries"]["reader"]
DOOR = "/home/node/.openclaw/agents/reader/workspace/skills/doc-door/door.py"

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_STATE"]
def load(name, default=None):
    try:
        return json.load(open(f"{state}/{name}.json"))
    except FileNotFoundError:
        return default
def save(name, value):
    json.dump(value, open(f"{state}/{name}.json", "w"))
a = sys.argv[1:]
if a[0] == "ps":
    print("gw")
    sys.exit(0)
assert a[:2] == ["exec", "gw"], a
if a[2] != "openclaw":
    sys.exit(0)  # the door skill's own installs (venv, rclone): nothing to model
cmd = a[3:]
VALUE_FLAGS = {
    "--name", "--display-name", "--description", "--cron", "--tz", "--agent", "--session",
    "--session-key", "--message", "--model", "--tools", "--timeout-seconds",
    "--failure-alert-after", "--failure-alert-mode", "--failure-alert-channel",
    "--failure-alert-account-id", "--failure-alert-to",
}
def flags(argv):
    out, i = {}, 0
    while i < len(argv):
        if argv[i] in VALUE_FLAGS:
            out[argv[i]] = argv[i + 1]
            i += 2
        else:
            out[argv[i]] = True
            i += 1
    return out
def apply(job, f):
    if "--agent" in f and not load("stuck_agent"):
        job["agentId"] = f["--agent"]
    if "--session" in f:
        job["sessionTarget"] = f["--session"]
    if "--session-key" in f:
        job["sessionKey"] = f["--session-key"]
    job["delivery"] = {"mode": "none" if "--no-deliver" in f else "announce"}
    payload = job.setdefault("payload", {})
    if "--tools" in f:
        payload["toolsAllow"] = f["--tools"].split(",")
    if "--model" in f:
        payload["model"] = f["--model"]
    if "--message" in f:
        payload["message"] = f["--message"]
def log_call(argv):
    with open(f"{state}/calls.jsonl", "a") as fh:
        fh.write(json.dumps(argv) + "\n")
if cmd[:2] == ["config", "get"]:
    if load("get_fail"):
        print("gateway closed (1006): connection refused", file=sys.stderr)
        sys.exit(1)
    if cmd[2] == "agents.entries.reader":
        entry = load("entry")
        if entry is None:
            print("Config path not found: agents.entries.reader", file=sys.stderr)
            sys.exit(1)
        print("[gateway] noise before the JSON")
        print(json.dumps(entry, indent=2))
    elif cmd[2] == "tools.agentToAgent.enabled":
        if load("a2a_unset"):
            print("Config path not found: tools.agentToAgent.enabled", file=sys.stderr)
            sys.exit(1)
        if load("a2a_fail"):
            print("gateway closed (1006): connection refused", file=sys.stderr)
            sys.exit(1)
        if load("a2a_noise"):
            print("[gateway] noise before the value")
        print(json.dumps(load("a2a", False)))
elif cmd[:2] == ["agents", "list"]:
    rows = [{"id": "main", "bindings": 0}]
    if load("entry") is not None:
        rows.append({"id": "reader", "bindings": load("bindings", 0)})
    print(json.dumps(rows))
elif cmd[:3] == ["approvals", "allowlist", "add"]:
    assert cmd[3:5] == ["--agent", "reader"], cmd
    rows = load("allow", [])
    if cmd[5] not in rows:
        rows.append(cmd[5])
    save("allow", rows)
elif cmd[:2] == ["approvals", "get"]:
    rows = [{"pattern": p} for p in load("allow", [])]
    print(json.dumps({"file": {"agents": {"reader": {"allowlist": rows}}}}))
elif cmd[:2] == ["cron", "list"]:
    print("[gateway] noise before the JSON")
    print(json.dumps({"jobs": load("jobs", [])}))
elif cmd[:2] == ["cron", "add"]:
    log_call(cmd)
    jobs = load("jobs", [])
    f = flags(cmd[2:])
    job = {"id": f"job{len(jobs) + 1}", "name": f["--name"]}
    apply(job, f)
    jobs.append(job)
    save("jobs", jobs)
elif cmd[:2] == ["cron", "edit"]:
    log_call(cmd)
    jobs = load("jobs", [])
    for job in jobs:
        if job["id"] == cmd[2]:
            apply(job, flags(cmd[3:]))
    save("jobs", jobs)
else:
    sys.exit(3)
"""


@pytest.fixture
def box(tmp_path):
    state, bin_dir, conf = tmp_path / "state", tmp_path / "bin", tmp_path / "conf"
    for d in (state, bin_dir, conf):
        d.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_STATE": str(state),
        "OPENCLAW_CONFIG_DIR": str(conf),
    }

    class Box:
        def put(self, name, value):
            (state / f"{name}.json").write_text(json.dumps(value))

        def get(self, name):
            return json.loads((state / f"{name}.json").read_text())

        def run(self, *args):
            return self.script("ensure-reader-agent.sh", *args)

        def script(self, name, *args, **extra_env):
            return subprocess.run(
                ["bash", str(REPO / "scripts" / name), *args],
                env={
                    **env,
                    "DOC_DOOR_ALERT_CHAT": "1",
                    "NEEDS_YOU_ALERT_CHAT": "1",
                    **extra_env,
                },
                capture_output=True,
                text=True,
                check=False,
            )

        def calls(self):
            path = state / "calls.jsonl"
            return (
                [json.loads(ln) for ln in path.read_text().splitlines()]
                if path.exists()
                else []
            )

        def job(self, name):
            return next(j for j in self.get("jobs") if j["name"] == name)

        workspace = conf / "agents/reader/workspace"

    return Box()


def test_without_the_entry_nothing_is_installed_and_check_says_so(box):
    done = box.run()
    assert done.returncode == 0 and "no 'reader' agent" in done.stderr
    assert not box.workspace.exists()
    assert box.run("--check").returncode == 3


def test_a_shipped_entry_converges_and_is_then_check_clean(box):
    box.put("entry", ENTRY)
    done = box.run()
    assert done.returncode == 0, done.stderr
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "skills/README.md"):
        assert (box.workspace / name).read_text() == (
            REPO / "defaults/agents/reader/workspace" / name
        ).read_text()
    assert box.get("allow") == [DOOR]
    again = box.run()
    assert again.returncode == 0 and box.get("allow") == [DOOR]
    assert box.run("--check").returncode == 0


def test_check_installs_nothing_and_needs_the_allowlist(box):
    box.put("entry", ENTRY)
    done = box.run("--check")
    assert done.returncode == 1 and "exec allowlist" in done.stderr
    assert not box.workspace.exists()


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda e: e["tools"]["deny"].remove("google-workspace__send_*"), "tools.deny"),
        (lambda e: e["tools"]["allow"].append("group:web"), "tools.allow"),
        (lambda e: e["tools"].update(profile="coding"), "tools.profile"),
        (lambda e: e["tools"]["exec"].update(mode="full"), "tools.exec.mode"),
        (lambda e: e["sandbox"].update(mode="all"), "sandbox.mode"),
    ],
)
def test_a_live_entry_that_is_not_the_shipped_cap_is_refused(box, mutate, needle):
    live = copy.deepcopy(ENTRY)
    mutate(live)
    box.put("entry", live)
    done = box.run()
    assert done.returncode == 1 and "refusing" in done.stderr and needle in done.stderr
    assert not box.workspace.exists()


def test_a_channel_binding_is_refused(box):
    box.put("entry", ENTRY)
    box.put("bindings", 1)
    done = box.run()
    assert done.returncode == 1 and "channel binding" in done.stderr


def test_agent_to_agent_messaging_is_refused(box):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    assert box.run().returncode == 1


def test_agent_to_agent_messaging_is_refused_through_log_noise(box):
    box.put("entry", ENTRY)
    box.put("a2a_noise", True)
    box.put("a2a", True)
    done = box.run()
    assert done.returncode == 1 and "agentToAgent" in done.stderr
    box.put("a2a", False)
    assert box.run().returncode == 0


def test_an_unset_agent_to_agent_key_is_disabled(box):
    box.put("entry", ENTRY)
    box.put("a2a_unset", True)
    for args in ((), ("--check",)):
        done = box.run(*args)
        assert done.returncode == 0, (args, done.stderr)
    assert box.workspace.exists()


def test_a_failed_agent_to_agent_read_is_an_error(box):
    box.put("entry", ENTRY)
    box.put("a2a_fail", True)
    for args in ((), ("--check",)):
        done = box.run(*args)
        assert done.returncode == 1, args
        assert "could not read tools.agentToAgent.enabled" in done.stderr
        assert "gateway closed" in done.stderr
    assert not box.workspace.exists()


def test_a_failed_config_read_is_an_error_not_an_absent_reader(box):
    box.put("entry", ENTRY)
    box.put("get_fail", True)
    for args in ((), ("--check",)):
        done = box.run(*args)
        assert done.returncode == 1, args
        assert "could not read agents.entries.reader" in done.stderr
        assert "gateway closed" in done.stderr
    assert not box.workspace.exists()


def test_an_extra_allowlisted_command_is_refused(box):
    box.put("entry", ENTRY)
    box.put("allow", [DOOR, "/usr/bin/python3"])
    done = box.run()
    assert done.returncode == 1 and "exec allowlist" in done.stderr


def test_the_door_job_stays_put_until_the_reader_is_installed(box):
    done = box.script("ensure-doc-door.sh")
    assert done.returncode == 0, done.stderr
    assert "reader agent not installed" in done.stdout
    assert not box.workspace.exists() and box.calls() == []
    # a reader entry that is not the shipped cap stops the script, never moves the job
    live = copy.deepcopy(ENTRY)
    live["tools"]["deny"] = []
    box.put("entry", live)
    done = box.script("ensure-doc-door.sh")
    assert done.returncode == 1 and "refusing" in done.stderr
    assert box.calls() == []


def test_a_transient_read_failure_never_moves_the_jobs_to_kit(box):
    box.put("entry", ENTRY)
    box.put("get_fail", True)
    box.put(
        "jobs",
        [
            {"id": "job1", "name": "needs-you-mail-reader", "agentId": "reader"},
            {"id": "job2", "name": "needs-you-digest", "agentId": "kit"},
        ],
    )
    for name in ("ensure-needs-you-digest.sh", "ensure-doc-door.sh"):
        done = box.script(name)
        assert done.returncode == 1, name
    assert box.calls() == []
    assert box.job("needs-you-mail-reader")["agentId"] == "reader"


def test_the_door_job_is_declared_as_the_reader_running_the_allowlisted_door(box):
    box.put("entry", ENTRY)
    assert box.run().returncode == 0
    done = box.script("ensure-doc-door.sh")
    assert done.returncode == 0, done.stderr
    add, edit = box.calls()
    for call in (add, edit):
        assert call[call.index("--agent") + 1] == "reader"
        assert call[call.index("--session") + 1] == "isolated"
        assert call[call.index("--tools") + 1] == "exec"
        assert "--no-deliver" in call
        # the one command the reader may run is the path the job tells it to run
        (allowed,) = box.get("allow")
        assert f"Run exactly: {allowed} scan" in call[call.index("--message") + 1]
    row = box.job("doc-door")
    assert row["agentId"] == "reader" and row["payload"]["toolsAllow"] == ["exec"]
    assert (box.workspace / "skills/doc-door/door.py").is_file()
    again = box.script("ensure-doc-door.sh")
    assert again.returncode == 0, again.stderr
    assert [c[:2] for c in box.calls()] == [["cron", "add"], ["cron", "edit"]] + [
        ["cron", "edit"]
    ]


def test_a_door_row_that_stays_on_another_agent_is_refused(box):
    box.put("entry", ENTRY)
    assert box.run().returncode == 0
    box.put("jobs", [{"id": "job1", "name": "doc-door", "agentId": "kit"}])
    box.put("stuck_agent", True)
    done = box.script("ensure-doc-door.sh")
    assert done.returncode == 1
    assert "agentId='kit', want 'reader'" in done.stderr


def test_the_mail_reader_moves_to_the_reader_and_the_digest_stays_on_kit(box):
    box.put("entry", ENTRY)
    assert box.run().returncode == 0
    done = box.script("ensure-needs-you-digest.sh")
    assert done.returncode == 0, done.stderr
    edits = {c[2]: c for c in box.calls() if c[:2] == ["cron", "edit"]}
    reader, digest = box.job("needs-you-mail-reader"), box.job("needs-you-digest")
    assert reader["agentId"] == "reader" and digest["agentId"] == "kit"
    assert edits[reader["id"]][edits[reader["id"]].index("--agent") + 1] == "reader"
    assert set(reader["payload"]["toolsAllow"]) == {
        "google-workspace__search_gmail_messages",
        "google-workspace__get_gmail_messages_content_batch",
    }
    assert reader["delivery"] == {"mode": "none"}


def test_a_mail_reader_row_on_another_agent_is_refused(box):
    box.put("entry", ENTRY)
    assert box.run().returncode == 0
    box.put("jobs", [{"id": "job1", "name": "needs-you-mail-reader", "agentId": "kit"}])
    box.put("stuck_agent", True)
    done = box.script("ensure-needs-you-digest.sh")
    assert done.returncode == 1
    assert "agentId='kit', want 'reader'" in done.stderr
    assert not [c for c in box.calls() if c[:2] == ["cron", "add"]]
