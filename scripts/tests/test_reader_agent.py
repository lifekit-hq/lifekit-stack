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
assert a[:2] == ["exec", "gw"] and a[2] == "openclaw", a
cmd = a[3:]
if cmd[:2] == ["config", "get"]:
    if cmd[2] == "agents.entries.reader":
        entry = load("entry")
        if entry is None:
            print("Config path not found: agents.entries.reader", file=sys.stderr)
            sys.exit(1)
        print("[gateway] noise before the JSON")
        print(json.dumps(entry, indent=2))
    elif cmd[2] == "tools.agentToAgent.enabled":
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
            return subprocess.run(
                ["bash", str(ENSURE), *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

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


def test_an_extra_allowlisted_command_is_refused(box):
    box.put("entry", ENTRY)
    box.put("allow", [DOOR, "/usr/bin/python3"])
    done = box.run()
    assert done.returncode == 1 and "exec allowlist" in done.stderr


def test_the_shipped_cap_denies_every_way_out():
    tools = ENTRY["tools"]
    assert "profile" not in tools, "a profile hides MCP tools; the cap is allow + deny"
    assert tools["exec"]["mode"] == "allowlist"
    assert tools["agentToAgent"] == {"send": []}
    assert ENTRY["sandbox"]["mode"] == "off"
    for group in (
        "fs",
        "web",
        "sessions",
        "memory",
        "ui",
        "automation",
        "messaging",
        "nodes",
        "agents",
        "media",
    ):
        assert f"group:{group}" in tools["deny"]
    for allowed in tools["allow"]:
        assert not any(
            x in allowed
            for x in ("send", "draft", "create", "delete", "update", "modify")
        )
    assert set(tools["allow"]) == {
        "exec",
        "google-workspace__search_gmail_messages",
        "google-workspace__get_gmail_messages_content_batch",
    }
    # the one write tool the Workspace MCP has, and the other servers, stay refused
    for pattern in (
        "google-workspace__send_*",
        "google-workspace__draft_*",
        "google-workspace-fleet__*",
    ):
        assert pattern in tools["deny"]


def test_the_allowlisted_command_is_the_one_the_door_job_runs():
    assert f"door={DOOR.replace('reader', '${AGENT}')}" in ENSURE.read_text()
    text = (REPO / "scripts/ensure-doc-door.sh").read_text()
    assert 'AGENT="${DOC_DOOR_AGENT:-reader}"' in text
    assert "Run exactly: ${container_skill}/door.py scan" in text
    assert (
        "container_skill=/home/node/.openclaw/agents/${AGENT}/workspace/skills/${SKILL}"
        in text
    )
    assert "ensure-reader-agent.sh" in text and "--check" in text
    assert (
        "SKILL.md beside it" not in text
    ), "the reader has no file tool; scan prints the guide"


def test_the_mail_reader_job_moves_and_the_digest_stays_on_kit():
    text = (REPO / "scripts/ensure-needs-you-digest.sh").read_text()
    assert text.count('--agent "$READER_AGENT" --session isolated') == 2
    assert (
        text.count('--agent "$AGENT" --session current') == 2 and "AGENT=kit\n" in text
    )
    assert "ensure-reader-agent.sh" in text and "NEEDS_YOU_READER_AGENT" in text
    assert (
        '"$reader_id" "$READER_TOOLS" "$READER_AGENT"' in text
    ), "the live row's agent is verified"


def test_the_deploy_installs_the_reader_before_the_door():
    text = (REPO / "scripts/deploy-openclaw.sh").read_text()
    assert text.index("ensure-reader-agent.sh") < text.index("ensure-doc-door.sh")


def test_the_door_job_stays_put_until_the_reader_is_installed(box, tmp_path):
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "FAKE_STATE": str(tmp_path / "state"),
        "OPENCLAW_CONFIG_DIR": str(tmp_path / "conf"),
        "DOC_DOOR_ALERT_CHAT": "1",
    }
    done = subprocess.run(
        ["bash", str(REPO / "scripts/ensure-doc-door.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "reader agent not installed" in done.stdout
    assert not box.workspace.exists()
    # a reader entry that is not the shipped cap stops the script, never moves the job
    live = copy.deepcopy(ENTRY)
    live["tools"]["deny"] = []
    box.put("entry", live)
    done = subprocess.run(
        ["bash", str(REPO / "scripts/ensure-doc-door.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1 and "refusing" in done.stderr
