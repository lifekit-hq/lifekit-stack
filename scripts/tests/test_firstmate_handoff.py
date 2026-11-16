"""Dev problems go to firstmate, not the owner: the firstmate-handoff skill
(skills/firstmate-handoff), its installer, and the two agents' instructions
(docs/runbook.md, "Dev problems go to firstmate, not the owner").

handoff.sh is run for each agent against the real kit-relay forced command,
rendered by install-kit-relay.sh and fed through a fake ssh that sets
SSH_ORIGINAL_COMMAND the way sshd does, over a scratch firstmate home whose
fm-inbox.sh is the stub test_kit_relay.py uses. A note that lands in that
home's inbox is a note that reached firstmate.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from test_kit_relay import (
    FAKE_DOCKER,
    FAKE_SSH_FORCED,
    FAKE_SSH_RECORD,
    Relay,
    utc_days,
    write_exec,
)

REPO = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO / "skills/firstmate-handoff"
HANDOFF = SKILL_DIR / "handoff.sh"
SKILL_MD = SKILL_DIR / "SKILL.md"
ENSURE = REPO / "scripts/ensure-firstmate-handoff.sh"
CONF = "/run/lifekit/kit-relay/ssh_config"

NOTE = (
    "what: EDGAR filings fetch fails (source sec_edgar, get_filings)\n"
    "evidence: HTTP 403 on 3 consecutive pulses since 06:10 UTC\n"
    "impact: thesis reviews that need a filing cannot refresh\n"
    "repo: lifekit-hq/finance-sentry\n"
    "blocked-on: external (SEC request policy)\n"
)


@pytest.fixture
def relay(tmp_path):
    r = Relay(tmp_path)
    write_exec(r.bin / "ssh", FAKE_SSH_FORCED)
    return r


def handoff(relay, tmp_path, *args, body=NOTE, **env):
    path = tmp_path / "body.txt"
    if body is not None:
        path.write_text(body)
    argv = [a if a != "BODY" else str(path) for a in args]
    return subprocess.run(
        ["sh", str(HANDOFF), *argv],
        env={**relay.env, **env},
        capture_output=True,
        text=True,
        check=False,
    )


# ─── a note from each agent reaches firstmate's inbox ────────────────────────


@pytest.mark.parametrize("agent", ["kit", "ledger"])
def test_a_note_from_each_agent_reaches_firstmates_inbox(relay, tmp_path, agent):
    result = handoff(relay, tmp_path, "send", agent, "BODY")
    assert result.returncode == 0, result.stderr
    received, saved = [json.loads(x) for x in result.stdout.splitlines()]
    assert received["schema"] == "kit-relay-received.v1"
    assert saved["outcome"] == "created" and saved["announced"] is True
    # Landed in the main firstmate's inbox, not the second mate's.
    assert relay.notes(relay.main) and not relay.notes(relay.home)
    body = relay.notes(relay.main)[0].read_text()
    assert body == f"task: dev problem from {agent}\n{NOTE}"
    rid = relay.requests(relay.main)[0]
    day, who, digest = re.fullmatch(r"kit-(\d{8})-(\w+)-([0-9a-f]{16})", rid).groups()
    assert day in utc_days() and who == agent
    assert f"note rid={rid} " in relay.logged()


def test_each_agents_note_is_its_own_note(relay, tmp_path):
    for agent in ("kit", "ledger"):
        assert handoff(relay, tmp_path, "send", agent, "BODY").returncode == 0
    assert len(relay.notes(relay.main)) == 2


def test_the_same_note_the_same_day_is_a_replay(relay, tmp_path):
    first = handoff(relay, tmp_path, "send", "ledger", "BODY")
    second = handoff(relay, tmp_path, "send", "ledger", "BODY")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout.splitlines()[1])["outcome"] == "replay"
    assert (
        first.stdout.splitlines()[1].split('"id":')[1]
        == (second.stdout.splitlines()[1].split('"id":')[1])
    )
    assert len(relay.notes(relay.main)) == 1


def test_replies_read_the_notes_firstmate_answered(relay, tmp_path):
    handoff(relay, tmp_path, "send", "kit", "BODY")
    (relay.main / "receipts.json").write_text(
        json.dumps(
            {
                "schema": "fm-inbox-receipts.v1",
                "home": "firstmate",
                "generated": "2026-11-16T00:00:00Z",
                "pending": [
                    {"id": "note-1", "request_id": relay.requests(relay.main)[0]}
                ],
                "handled": [],
                "replies": [],
                "reply_cursor": "000000000000",
            }
        )
    )
    result = handoff(relay, tmp_path, "replies")
    assert result.returncode == 0, result.stderr
    assert "kit-relay-receipts.v1" in result.stdout
    assert "note-1" in result.stdout


# ─── what the client refuses before anything leaves ──────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bogus"],
        ["send"],
        ["send", "kit"],
        ["send", "career", "BODY"],  # only the two agents that own this route
        ["send", "kit", "BODY", "extra"],
        ["send", "../kit", "BODY"],
        ["replies", "firstmate"],
        ["replies", "12", "extra"],
    ],
)
def test_client_refuses_bad_usage(relay, tmp_path, args):
    result = handoff(relay, tmp_path, *args)
    assert result.returncode == 64
    assert not relay.notes(relay.main) and not relay.calls(relay.main)


@pytest.mark.parametrize(
    "body",
    [
        None,  # no file
        "",  # empty file
        "what: x\n",  # no evidence
        "evidence: y\n",  # no what
        "what:\nevidence: y\n",  # a key with no value
        "something broke, please look\n",
    ],
)
def test_client_refuses_a_note_without_what_and_evidence(relay, tmp_path, body):
    result = handoff(relay, tmp_path, "send", "kit", "BODY", body=body)
    assert result.returncode == 64
    assert not relay.notes(relay.main) and not relay.calls(relay.main)


def test_client_sends_only_to_the_main_firstmate_over_the_kit_relay_key(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_exec(bindir / "ssh", FAKE_SSH_RECORD)
    (tmp_path / "body.txt").write_text(NOTE)
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "SSH_TEST_DIR": str(tmp_path)}
    result = subprocess.run(
        ["sh", str(HANDOFF), "send", "ledger", str(tmp_path / "body.txt")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[:4] == ["-F", CONF, "relay", "note"]
    assert argv[4].startswith("kit-") and argv[5:] == ["firstmate"]
    assert (tmp_path / "stdin").read_text() == f"task: dev problem from ledger\n{NOTE}"


# ─── the skill's wiring ──────────────────────────────────────────────────────


def test_skill_frontmatter_names_its_folder_and_gates_on_ssh():
    yaml = pytest.importorskip("yaml")
    _, front, _ = SKILL_MD.read_text().split("---\n", 2)
    meta = yaml.safe_load(front)
    assert meta["name"] == "firstmate-handoff"
    assert meta["metadata"]["openclaw"]["requires"]["bins"] == ["ssh"]


def test_skill_sample_note_passes_the_clients_own_check(relay, tmp_path):
    text = SKILL_MD.read_text()
    sample = re.search(r"Sample, for Ledger:\n\n```\n(.*?)```", text, re.S).group(1)
    result = handoff(relay, tmp_path, "send", "ledger", "BODY", body=sample)
    assert result.returncode == 0, result.stderr
    assert len(relay.notes(relay.main)) == 1


# ─── the installer ───────────────────────────────────────────────────────────


@pytest.fixture
def workspaces(tmp_path):
    for agent in ("kit", "finance"):
        (tmp_path / f"config/agents/{agent}/workspace").mkdir(parents=True)


def ensure(tmp_path, gateway="", listed=""):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    write_exec(bindir / "docker", FAKE_DOCKER)
    return subprocess.run(
        ["bash", str(ENSURE)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "OPENCLAW_CONFIG_DIR": str(tmp_path / "config"),
            "FAKE_GATEWAY": gateway,
            "FAKE_LISTED": listed,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_ensure_installs_the_skill_for_kit_and_ledger(tmp_path, workspaces):
    for _ in range(2):  # converges on re-run
        result = ensure(tmp_path)
        assert result.returncode == 0, result.stderr
    for agent in ("kit", "finance"):
        dest = tmp_path / f"config/agents/{agent}/workspace/skills/firstmate-handoff"
        assert (dest / "SKILL.md").read_bytes() == SKILL_MD.read_bytes()
        assert (dest / "handoff.sh").read_bytes() == HANDOFF.read_bytes()
        assert (dest / "SKILL.md").stat().st_mode & 0o777 == 0o644
        assert (dest / "handoff.sh").stat().st_mode & 0o777 == 0o755
    assert "allowlists not checked" in result.stdout


def test_ensure_reports_each_missing_allowlist_entry(tmp_path, workspaces):
    result = ensure(tmp_path, gateway="abc123", listed="no")
    assert result.returncode == 0, result.stderr
    assert "NOT on agent 'kit''s skills list" in result.stdout
    assert "NOT on agent 'finance''s skills list" in result.stdout


def test_ensure_fails_for_a_missing_workspace_but_still_installs_the_other(tmp_path):
    (tmp_path / "config/agents/kit/workspace").mkdir(parents=True)
    result = ensure(tmp_path)
    assert result.returncode == 1
    assert "no workspace for agent 'finance'" in result.stderr
    assert (
        tmp_path / "config/agents/kit/workspace/skills/firstmate-handoff/handoff.sh"
    ).exists()
