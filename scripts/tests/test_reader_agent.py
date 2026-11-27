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
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENSURE = REPO / "scripts/ensure-reader-agent.sh"
SHIPPED = json.loads((REPO / "defaults/agents/reader/agent.json").read_text())
ENTRY = SHIPPED["agents"]["entries"]["reader"]
POLICY_MODEL = json.loads(
    (REPO / "compose/openclaw-gateway/platform.patch.json").read_text()
)["agents"]["defaults"]["model"]["primary"]
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
if a[:2] == ["exec", "-i"]:
    a = ["exec"] + a[2:]
assert a[:2] == ["exec", "gw"], a
if a[2] != "openclaw":
    sys.exit(0)  # the door skill's own installs (venv, rclone): nothing to model
cmd = a[3:]
VALUE_FLAGS = {
    "--name", "--display-name", "--description", "--cron", "--tz", "--agent", "--session",
    "--session-key", "--message", "--model", "--tools", "--timeout-seconds",
    "--failure-alert-after", "--failure-alert-mode", "--failure-alert-channel",
    "--failure-alert-account-id", "--failure-alert-to", "--trigger-script",
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
    if "--cron" in f:
        job["schedule"] = {"kind": "cron", "expr": f["--cron"], "tz": f.get("--tz")}
    job["delivery"] = {"mode": "none" if "--no-deliver" in f else "announce"}
    payload = job.setdefault("payload", {})
    if "--tools" in f:
        payload["toolsAllow"] = f["--tools"].split(",")
    if "--model" in f:
        payload["model"] = f["--model"]
    if "--message" in f:
        payload["message"] = f["--message"]
    if "--trigger-script" in f:
        # The gateway stores the gate's content, read from the path the script
        # installed it to (/home/node/.openclaw is OPENCLAW_CONFIG_DIR here).
        path = f["--trigger-script"].replace("/home/node/.openclaw", os.environ["OPENCLAW_CONFIG_DIR"], 1)
        job["trigger"] = {"script": open(path).read()}
    if "--clear-trigger" in f:
        job.pop("trigger", None)
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
            if load("unset_phrase"):
                print("Config path is valid but unset: agents.entries.reader", file=sys.stderr)
            else:
                print("Config path not found: agents.entries.reader", file=sys.stderr)
            sys.exit(1)
        print("[gateway] noise before the JSON")
        print(json.dumps(entry, indent=2))
    elif cmd[2] == "tools.agentToAgent.enabled":
        if load("a2a_unset"):
            if load("unset_phrase"):
                print("Config path is valid but unset: tools.agentToAgent.enabled", file=sys.stderr)
            else:
                print("Config path not found: tools.agentToAgent.enabled", file=sys.stderr)
            sys.exit(1)
        if load("a2a_fail"):
            print("gateway closed (1006): connection refused", file=sys.stderr)
            sys.exit(1)
        if load("a2a_noise"):
            print("[gateway] noise before the value")
        print(json.dumps(load("a2a", False)))
    elif cmd[2] == "agents.entries":
        entries = load("entries", {})
        if load("entry") is not None:
            entries["reader"] = load("entry")
        print("[gateway] noise before the JSON")
        print(json.dumps(entries))
    elif cmd[2] == "tools.agentToAgent.allow":
        allow = load("a2a_allow")
        if allow is None:
            print("Config path not found: tools.agentToAgent.allow", file=sys.stderr)
            sys.exit(1)
        if load("a2a_allow_noise"):
            print("[gateway] noise before the value")
        print(json.dumps(allow))
elif cmd[:2] == ["config", "patch"]:
    patch = json.load(sys.stdin)
    log_call(cmd + [patch])
    if "tools" in patch:
        save("a2a_allow", patch["tools"]["agentToAgent"]["allow"])
    entries = load("entries", {})
    for i, e in patch.get("agents", {}).get("entries", {}).items():
        live = entries.setdefault(i, {})
        live.setdefault("tools", {}).setdefault("agentToAgent", {})
        live["tools"]["agentToAgent"]["send"] = e["tools"]["agentToAgent"]["send"]
    save("entries", entries)
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
    refusal = load("edit_refusal")
    if refusal:
        print(refusal, file=sys.stderr)
        sys.exit(1)
    jobs = load("jobs", [])
    for job in jobs:
        if job["id"] == cmd[2]:
            apply(job, flags(cmd[3:]))
    save("jobs", jobs)
elif cmd[:2] == ["cron", "enable"]:
    jobs = load("jobs", [])
    for job in jobs:
        if job["id"] == cmd[2]:
            job["enabled"] = True
    save("jobs", jobs)
else:
    sys.exit(3)
"""


@pytest.fixture
def box(tmp_path):
    return make_box(tmp_path)


def make_box(tmp_path, repo=REPO):
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
                ["bash", str(repo / "scripts" / name), *args],
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


def test_a_valid_but_unset_entry_is_an_absent_reader(box):
    box.put("unset_phrase", True)
    done = box.run()
    assert done.returncode == 0 and "no 'reader' agent" in done.stderr
    assert not box.workspace.exists()
    assert box.run("--check").returncode == 3


def test_a_valid_but_unset_agent_to_agent_key_is_disabled(box):
    box.put("entry", ENTRY)
    box.put("a2a_unset", True)
    box.put("unset_phrase", True)
    for args in ((), ("--check",)):
        done = box.run(*args)
        assert done.returncode == 0, (args, done.stderr)


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


# The owner's rule (2026-11-25, #683): every agent can message Kit and no other agent,
# Kit keeps its exchange with each of them, and the reader and the study panel stay
# unreachable. The agents that run today, by name: the oracle the derived list and the
# converged sends are held to.
ALLOW = ["career", "finance", "kit", "learnclaw"]
SEND = {"kit": ["*"], "finance": ["kit"], "learnclaw": ["kit"], "career": ["kit"]}
LIVE = {
    i: {"workspace": f"/w/{i}", "tools": {"agentToAgent": {"send": s}}}
    for i, s in SEND.items()
}


def a2a_repo(tmp_path):
    """A copy of the pieces the script derives the allow list from, to add agents to."""
    repo = tmp_path / "repo"
    (repo / "scripts/ledger-persona").mkdir(parents=True)
    shutil.copy(ENSURE, repo / "scripts/ensure-reader-agent.sh")
    shutil.copy(
        REPO / "scripts/ledger-persona/ledger-persona.py",
        repo / "scripts/ledger-persona/ledger-persona.py",
    )
    shutil.copytree(REPO / "defaults/agents", repo / "defaults/agents")
    # Not a git checkout, so the install stamp (test_release_gauge.py covers it)
    # is a no-op here.
    (repo / "scripts/lib").mkdir()
    (repo / "scripts/lib/install-stamp.sh").write_text("exit 0\n")
    shutil.copy(
        REPO / "scripts/lib/openclaw-gateway.sh",
        repo / "scripts/lib/openclaw-gateway.sh",
    )
    return repo


def add_agent(repo, agent, send=None):
    (repo / f"defaults/agents/{agent}/workspace").mkdir(parents=True)
    if send is not None:
        entry = {"tools": {"agentToAgent": {"send": send}}}
        (repo / f"defaults/agents/{agent}/agent.json").write_text(
            json.dumps({"agents": {"entries": {agent: entry}}})
        )


def patches(box):
    return [c for c in box.calls() if c[:2] == ["config", "patch"]]


def sends(box):
    return {
        i: e["tools"]["agentToAgent"].get("send")
        for i, e in box.get("entries").items()
        if "agentToAgent" in e.get("tools", {})
    }


def test_agent_to_agent_on_with_no_allow_list_converges_to_the_users_only(box):
    # enabled with no allow list reaches every agent: the script narrows it, then passes
    box.put("entry", ENTRY)
    box.put("a2a", True)
    done = box.run()
    assert done.returncode == 0, done.stderr
    assert box.get("a2a_allow") == ALLOW


def test_every_running_agent_reaches_kit_and_only_kit(box):
    # the live entries as they were before #683: no per-agent send, the study panel's []
    box.put("entry", ENTRY)
    box.put("a2a", True)
    box.put("a2a_allow", ["kit", "finance"])
    live = {i: {"workspace": f"/w/{i}"} for i in SEND}
    live["study-panel"] = {"tools": {"agentToAgent": {"send": []}}}
    box.put("entries", live)
    done = box.run()
    assert done.returncode == 0, done.stderr
    allow = box.get("a2a_allow")
    assert allow == ALLOW and not {"reader", "study-panel"} & set(allow)
    got = sends(box)
    assert {i: got[i] for i in SEND} == SEND and got["study-panel"] == []
    assert box.get("entries")["kit"]["workspace"] == "/w/kit"
    for agent in ("learnclaw", "career"):
        assert not {"finance", "study-panel", "reader", "*"} & set(got[agent])
    # Ledger is reached by Kit alone
    assert [i for i, s in got.items() if {"finance", "*"} & set(s)] == ["kit"]
    assert box.run("--check").returncode == 0


def test_a_new_agent_definition_joins_the_allow_list(tmp_path):
    repo = a2a_repo(tmp_path)
    add_agent(repo, "workout", ["kit"])
    box = make_box(tmp_path, repo)
    box.put("entry", ENTRY)
    box.put("allow", [DOOR])
    box.put("a2a", True)
    box.put("a2a_allow", ALLOW)
    box.put("entries", {**LIVE, "workout": {"workspace": "/w/workout"}})
    done = box.run("--check")
    assert done.returncode == 1
    assert "differs from the agents the repo defines" in done.stderr
    done = box.run()
    assert done.returncode == 0, done.stderr
    assert box.get("a2a_allow") == sorted([*ALLOW, "workout"])
    assert sends(box)["workout"] == ["kit"]
    assert box.run("--check").returncode == 0


def test_a_new_agent_definition_without_a_send_list_is_refused(tmp_path):
    repo = a2a_repo(tmp_path)
    add_agent(repo, "workout")
    box = make_box(tmp_path, repo)
    box.put("entry", ENTRY)
    box.put("a2a", True)
    done = box.run()
    assert done.returncode == 1
    assert (
        "defaults/agents/workout/agent.json ships no tools.agentToAgent.send"
        in done.stderr
    )
    assert patches(box) == []


def test_an_isolated_agent_definition_stays_out_of_the_allow_list(tmp_path):
    repo = a2a_repo(tmp_path)
    add_agent(repo, "vault", [])
    box = make_box(tmp_path, repo)
    box.put("entry", ENTRY)
    box.put("a2a", True)
    assert box.run().returncode == 0
    assert box.get("a2a_allow") == ALLOW


@pytest.mark.parametrize("live", [["*"], ["kit", "finance"], None])
def test_check_refuses_a_send_list_wider_than_the_shipped_one(box, live):
    box.put("entry", ENTRY)
    box.put("allow", [DOOR])
    box.put("a2a", True)
    box.put("a2a_allow", ALLOW)
    career = {"workspace": "/w/career"}
    if live is not None:
        career["tools"] = {"agentToAgent": {"send": live}}
    box.put("entries", {**LIVE, "career": career})
    done = box.run("--check")
    assert done.returncode == 1
    assert "agents.entries.career.tools.agentToAgent.send" in done.stderr
    assert patches(box) == []
    assert box.run().returncode == 0
    (patch,) = patches(box)
    assert patch[-1] == {
        "agents": {
            "entries": {"career": {"tools": {"agentToAgent": {"send": ["kit"]}}}}
        }
    }
    assert sends(box)["career"] == ["kit"]
    assert box.get("entries")["career"]["workspace"] == "/w/career"


def test_an_agent_not_installed_here_gets_no_entry(box):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    box.put("entries", {"kit": {"workspace": "/w/kit"}})
    assert box.run().returncode == 0
    assert sorted(box.get("entries")) == ["kit"] and sends(box) == {"kit": ["*"]}


def test_the_reader_never_joins_the_allow_list(tmp_path):
    # even with its definition present and Ledger's installer pointed at it
    repo = a2a_repo(tmp_path)
    ledger = repo / "scripts/ledger-persona/ledger-persona.py"
    ledger.write_text(
        ledger.read_text().replace(
            "/agents/finance/workspace", "/agents/reader/workspace"
        )
    )
    box = make_box(tmp_path, repo)
    box.put("entry", ENTRY)
    box.put("a2a", True)
    done = box.run()
    assert done.returncode == 0 or "exec allowlist" in done.stderr, done.stderr
    assert "reader" not in box.get("a2a_allow") and box.get("a2a_allow")


def test_a_missing_ledger_id_fails_closed(tmp_path):
    repo = a2a_repo(tmp_path)
    ledger = repo / "scripts/ledger-persona/ledger-persona.py"
    ledger.write_text(ledger.read_text().replace("DEFAULT_WORKSPACE = ", "OTHER = "))
    box = make_box(tmp_path, repo)
    box.put("entry", ENTRY)
    box.put("a2a", True)
    done = box.run()
    assert done.returncode == 1 and "Ledger's agent id" in done.stderr
    assert patches(box) == []


@pytest.mark.parametrize("allow", [["kit", "finance"], [*ALLOW, "ghost"]])
def test_check_refuses_an_allow_list_that_differs_from_the_defined_agents(box, allow):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    box.put("a2a_allow", allow)
    done = box.run("--check")
    assert (
        done.returncode == 1
        and "differs from the agents the repo defines" in done.stderr
    )
    assert patches(box) == []
    done = box.run()
    assert done.returncode == 0 or "exec allowlist" in done.stderr, done.stderr
    assert box.get("a2a_allow") == ALLOW


def test_agent_to_agent_on_with_no_allow_list_is_refused_by_check(box):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    done = box.run("--check")
    assert done.returncode == 1 and "no allow list" in done.stderr
    assert "reachable by other agents" in done.stderr and patches(box) == []


def test_agent_to_agent_on_with_an_empty_allow_list_is_refused_by_check(box):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    box.put("a2a_allow", [])
    done = box.run("--check")
    assert done.returncode == 1 and "no allow list" in done.stderr


@pytest.mark.parametrize("allow", [["kit", "reader"], ["*"], ["kit", "rea*"]])
def test_an_allow_list_that_reaches_the_reader_is_refused_by_check(box, allow):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    box.put("a2a_allow", allow)
    done = box.run("--check")
    assert done.returncode == 1 and "reaches the reader" in done.stderr
    assert patches(box) == []


def test_converge_removes_the_reader_from_the_allow_list(box):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    box.put("a2a_allow", ["kit", "finance", "reader"])
    assert box.run().returncode == 0
    assert box.get("a2a_allow") == ALLOW


def test_an_allow_list_that_excludes_the_reader_passes_and_is_left_alone(box):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    box.put("a2a_allow", list(reversed(ALLOW)))
    for args in ((), ("--check",)):
        done = box.run(*args)
        assert done.returncode == 0 or "exec allowlist" in done.stderr, done.stderr
        assert "allow list" not in done.stderr
    assert patches(box) == []


def test_disabled_agent_to_agent_passes_and_is_not_turned_on(box):
    box.put("entry", ENTRY)
    box.put("a2a", False)
    done = box.run()
    assert done.returncode == 0, done.stderr
    assert patches(box) == [] and box.get("a2a") is not True


def test_the_allow_list_converge_is_idempotent(box):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    assert box.run().returncode == 0
    assert len(patches(box)) == 1
    again = box.run()
    assert again.returncode == 0, again.stderr
    assert len(patches(box)) == 1 and box.get("a2a_allow") == ALLOW


def test_agent_to_agent_enabled_is_read_through_log_noise(box):
    box.put("entry", ENTRY)
    box.put("a2a_noise", True)
    box.put("a2a", True)
    done = box.run("--check")
    assert done.returncode == 1 and "agentToAgent" in done.stderr
    box.put("a2a", False)
    assert box.run().returncode == 0


def test_the_allow_list_is_read_through_log_noise(box):
    box.put("entry", ENTRY)
    box.put("a2a", True)
    box.put("a2a_allow_noise", True)
    box.put("a2a_allow", list(reversed(ALLOW)))
    for args in ((), ("--check",)):
        done = box.run(*args)
        assert done.returncode == 0 or "exec allowlist" in done.stderr, done.stderr
        assert "allow list" not in done.stderr
    assert patches(box) == []
    box.put("a2a_allow", ["kit", "reader"])
    done = box.run("--check")
    assert done.returncode == 1 and "reaches the reader" in done.stderr


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


REFUSED = "invalid cron.update params: MCP authorization was disconnected or replaced"


def test_a_refused_cron_update_skips_the_edit_with_one_line_and_is_not_converged(box):
    box.put("entry", ENTRY)
    assert box.run().returncode == 0
    box.put("jobs", [{"id": "job1", "name": "doc-door", "agentId": "reader"}])
    box.put("edit_refusal", REFUSED)
    # the live row is a good one, so the fail-closed check passes on it unchanged
    row = box.get("jobs")[0]
    row |= {
        "sessionTarget": "isolated",
        "delivery": {"mode": "none"},
        "payload": {"toolsAllow": ["exec"], "model": POLICY_MODEL},
    }
    box.put("jobs", [row])
    done = box.script("ensure-doc-door.sh")
    assert done.returncode == 0, done.stderr
    lines = [ln for ln in done.stderr.splitlines() if "NOT updated" in ln]
    assert len(lines) == 1 and "disconnected or replaced" in lines[0]
    assert "converged" not in done.stdout
    assert box.job("doc-door") == row


def test_a_refused_update_on_a_row_that_is_not_the_declared_one_still_fails_closed(box):
    box.put("entry", ENTRY)
    assert box.run().returncode == 0
    box.put("jobs", [{"id": "job1", "name": "doc-door", "agentId": "kit"}])
    box.put("edit_refusal", REFUSED)
    done = box.script("ensure-doc-door.sh")
    assert done.returncode == 1
    assert "agentId='kit', want 'reader'" in done.stderr


def test_any_other_cron_update_failure_is_still_red(box):
    box.put("entry", ENTRY)
    assert box.run().returncode == 0
    box.put("jobs", [{"id": "job1", "name": "doc-door", "agentId": "reader"}])
    box.put("edit_refusal", "gateway closed (1006): connection refused")
    done = box.script("ensure-doc-door.sh")
    assert done.returncode == 1
    assert "connection refused" in done.stderr


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


def test_every_declared_job_runs_on_the_policy_model(box):
    box.put("entry", ENTRY)
    assert box.run().returncode == 0
    for name in ("ensure-doc-door.sh", "ensure-needs-you-digest.sh"):
        done = box.script(name)
        assert done.returncode == 0, done.stderr
    declared = [c for c in box.calls() if c[:2] in (["cron", "add"], ["cron", "edit"])]
    assert len(declared) >= 6  # an add and an edit each for door, reader, digest
    for call in declared:
        assert call[call.index("--model") + 1] == POLICY_MODEL, call
    for name in ("doc-door", "needs-you-mail-reader", "needs-you-digest"):
        assert box.job(name)["payload"]["model"] == POLICY_MODEL


def test_the_doc_door_gate_is_set_for_the_reader_and_cleared_on_the_kit_rollback(box):
    box.put("entry", ENTRY)
    assert box.run().returncode == 0
    done = box.script("ensure-doc-door.sh")
    assert done.returncode == 0, done.stderr
    scan = (REPO / "scripts/doc-door-gate/scan.js").read_text()
    assert box.job("doc-door")["trigger"]["script"] == scan
    box.workspace.parent.parent.joinpath("kit/workspace").mkdir(parents=True)
    done = box.script("ensure-doc-door.sh", DOC_DOOR_AGENT="kit")
    assert done.returncode == 0, done.stderr
    edit = [c for c in box.calls() if c[:2] == ["cron", "edit"]][-1]
    assert "--clear-trigger" in edit and "--trigger-script" not in edit
    row = box.job("doc-door")
    assert row["agentId"] == "kit" and "trigger" not in row
