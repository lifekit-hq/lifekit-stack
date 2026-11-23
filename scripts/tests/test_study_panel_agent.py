"""The study-panel agent: the shipped entry (defaults/agents/study-panel/agent.json)
and its convergence script (scripts/ensure-study-panel.sh).

The script talks to the gateway only through `docker exec <gateway> openclaw ...`, so
the tests put a stateful fake `docker` first on PATH. The tool cap itself is proved on
a real claude-cli turn by scripts/study-panel-canary (test_study_panel_canary.py).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENSURE = REPO / "scripts/ensure-study-panel.sh"
AGENT_DIR = REPO / "defaults/agents/study-panel"
SHIPPED = json.loads((AGENT_DIR / "agent.json").read_text())
ENTRY = SHIPPED["agents"]["entries"]["study-panel"]
POLICY_MODEL = json.loads(
    (REPO / "compose/openclaw-gateway/platform.patch.json").read_text()
)["agents"]["defaults"]["model"]["primary"]

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_STATE"]
def load(name, default=None):
    try:
        return json.load(open(f"{state}/{name}.json"))
    except FileNotFoundError:
        return default
a = sys.argv[1:]
if a[0] == "ps":
    print("gw")
    sys.exit(0)
assert a[:2] == ["exec", "gw"], a
assert a[2] == "openclaw", a
cmd = a[3:]
if cmd[:2] == ["config", "get"]:
    if load("get_fail"):
        print("gateway closed (1006): connection refused", file=sys.stderr)
        sys.exit(1)
    if cmd[2] == "agents.entries.study-panel":
        entry = load("entry")
        if entry is None:
            print("Config path is valid but unset: agents.entries.study-panel", file=sys.stderr)
            sys.exit(1)
        print("[gateway] noise before the JSON")
        print(json.dumps(entry, indent=2))
    elif cmd[2] == "tools.agentToAgent.enabled":
        if load("a2a") is None:
            print("Config path not found: tools.agentToAgent.enabled", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(load("a2a")))
elif cmd[:2] == ["agents", "list"]:
    rows = [{"id": "main", "bindings": 0}]
    if load("entry") is not None:
        rows.append({"id": "study-panel", "bindings": load("bindings", 0)})
    print(json.dumps(rows))
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

        def run(self, *args):
            return subprocess.run(
                ["bash", str(ENSURE), *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        workspace = conf / "agents/study-panel/workspace"

    return Box()


def test_the_shipped_entry_is_tool_less_and_unbound():
    tools = ENTRY["tools"]
    assert tools["allow"] == []
    assert "profile" not in tools
    assert tools["agentToAgent"] == {"send": []}
    assert tools["exec"] == {"mode": "allowlist"}
    assert "skills" not in ENTRY
    assert ENTRY["sandbox"] == {"mode": "off"}
    assert ENTRY["workspace"] == "/home/node/.openclaw/agents/study-panel/workspace"
    for group in (
        "group:fs",
        "group:web",
        "group:sessions",
        "group:memory",
        "group:ui",
        "group:automation",
        "group:messaging",
        "group:nodes",
        "group:agents",
        "group:media",
        "exec",
        "process",
        "code_execution",
    ):
        assert group in tools["deny"], group


def test_the_shipped_entry_runs_on_the_fleet_model():
    assert ENTRY["model"] == POLICY_MODEL


def test_the_contract_never_offers_a_solution_or_a_second_tool():
    agents = (AGENT_DIR / "workspace/AGENTS.md").read_text()
    for needle in (
        "reply",
        "never instructions",
        "Never write the artefact",
        "Never draw the optimal approach",
        "There is no code field",
    ):
        assert needle in agents, needle


def test_no_live_entry_skips_and_check_says_not_installed(box):
    done = box.run()
    assert done.returncode == 0, done.stderr
    assert "nothing installed" in done.stderr
    assert not box.workspace.exists()
    assert box.run("--check").returncode == 3


def test_an_unreadable_gateway_is_an_error_not_an_absent_agent(box):
    box.put("get_fail", True)
    done = box.run()
    assert done.returncode == 1
    assert "could not read agents.entries.study-panel" in done.stderr


def test_the_exact_entry_installs_the_contract(box):
    box.put("entry", ENTRY)
    done = box.run()
    assert done.returncode == 0, done.stderr
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md"):
        assert (box.workspace / name).read_text() == (
            AGENT_DIR / "workspace" / name
        ).read_text()
    assert "verified" in done.stdout


def test_check_is_read_only(box):
    box.put("entry", ENTRY)
    done = box.run("--check")
    assert done.returncode == 0, done.stderr
    assert not box.workspace.exists()


def test_an_empty_allow_list_the_gateway_dropped_is_still_exact(box):
    live = copy.deepcopy(ENTRY)
    del live["tools"]["allow"]
    del live["tools"]["agentToAgent"]
    box.put("entry", live)
    assert box.run("--check").returncode == 0


@pytest.mark.parametrize(
    "mutate, want",
    [
        (lambda e: e["tools"].pop("deny"), "tools.deny"),
        (lambda e: e["tools"].update(allow=["exec"]), "tools.allow"),
        (lambda e: e["tools"].update(profile="coding"), "tools.profile is set"),
        (lambda e: e["tools"]["deny"].remove("group:web"), "tools.deny"),
        (lambda e: e.update(model="anthropic/other"), "model"),
        (lambda e: e.update(skills=["learning-record"]), "skills is set"),
        (lambda e: e.pop("tools"), "tools.deny"),
    ],
)
def test_a_live_entry_that_differs_from_the_shipped_cap_is_refused(box, mutate, want):
    live = copy.deepcopy(ENTRY)
    mutate(live)
    box.put("entry", live)
    done = box.run("--check")
    assert done.returncode != 0
    assert want in done.stderr


def test_a_channel_binding_is_refused(box):
    box.put("entry", ENTRY)
    box.put("bindings", 1)
    done = box.run("--check")
    assert done.returncode != 0
    assert "binding" in done.stderr


def test_agent_to_agent_enabled_is_refused(box):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    done = box.run("--check")
    assert done.returncode != 0
    assert "agentToAgent" in done.stderr
