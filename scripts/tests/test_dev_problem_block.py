"""ensure-dev-problem-block.sh: the one shared persona block for every agent.

The script is the only thing that puts the block into a workspace, so these run
it for real against a scratch gateway state dir. The oracle is the marker
contract in the script's header: the text between the two marker lines is the
block and nothing else in SOUL.md moves.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENSURE = REPO / "scripts/ensure-dev-problem-block.sh"
SHARED = REPO / "defaults/agents/shared/dev-problems.md"
A2A = REPO / "defaults/agents/shared/a2a-envelope.md"
BEGIN = "<!-- lifekit:dev-problems:begin -->"
END = "<!-- lifekit:dev-problems:end -->"
A2A_BEGIN = "<!-- lifekit:a2a-envelope:begin -->"
RECEIPTS = REPO / "defaults/agents/shared/receipts.md"
RECEIPTS_BEGIN = "<!-- lifekit:receipts:begin -->"
RECEIPTS_END = "<!-- lifekit:receipts:end -->"
INTENTS = REPO / "defaults/agents/shared/standing-intents.md"
INTENTS_BEGIN = "<!-- lifekit:standing-intents:begin -->"
INTENTS_END = "<!-- lifekit:standing-intents:end -->"
# The one-line rule the standing-intents block grew from (live Kit and Ledger).
LEGACY_INTENT_LINE = (
    'A "when X comes up, remind me" ask is a standing intent, not a note: create it'
    " with the `intent` tool (trigger, what to say, expiry). A clock-time reminder"
    " is a scheduled task instead."
)
BLOCKS = 3
# Kit and Ledger also get the standing-intents block.
INTENT_AGENTS = ("kit", "finance")

# What each agent's persona holds before the block lands (host state: free text).
PERSONAS = {
    "kit": "# SOUL.md - Kit\n\nSharp, terse.\n",
    "finance": "# SOUL\n\nLedger's own notes.\n\n## Voice\n\nPlain.\n",
    "career": "# SOUL.md - Career\n",
    "learnclaw": "# SOUL.md - Learnclaw\n\nLoop owner.\n",
    "newcomer": "an agent added after this script was written\n",
}


def block_text(agent: str = "career") -> str:
    """The shared blocks as they land in a SOUL.md: dev-problems, a2a, receipts,
    and standing-intents for Kit and Ledger."""
    parts = (SHARED, A2A, RECEIPTS) + ((INTENTS,) if agent in INTENT_AGENTS else ())
    return "\n\n".join(p.read_text().strip("\n") for p in parts)


def block_count(agents) -> int:
    return sum(BLOCKS + (a in INTENT_AGENTS) for a in agents)


def state(tmp_path, personas=None):
    root = tmp_path / "state"
    for agent, text in (PERSONAS if personas is None else personas).items():
        ws = root / "agents" / agent / "workspace"
        ws.mkdir(parents=True)
        if text is not None:
            (ws / "SOUL.md").write_text(text)
    return root


def run(root, *args, **env):
    return subprocess.run(
        ["bash", str(ENSURE), *args],
        env={**os.environ, "OPENCLAW_CONFIG_DIR": str(root), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def soul(root, agent):
    return (root / "agents" / agent / "workspace" / "SOUL.md").read_text()


def test_every_workspace_gets_the_block_and_keeps_its_own_persona(tmp_path):
    root = state(tmp_path)
    r = run(root)
    assert r.returncode == 0, r.stderr
    for agent, before in PERSONAS.items():
        # The persona stays exactly as it was, with the block after a blank line.
        assert (
            soul(root, agent) == before.rstrip("\n") + "\n\n" + block_text(agent) + "\n"
        )
        assert f"agent '{agent}': dev-problem block installed" in r.stdout
        assert f"agent '{agent}': a2a-envelope block installed" in r.stdout
        assert f"agent '{agent}': receipts block installed" in r.stdout


def test_a_second_run_changes_nothing(tmp_path):
    root = state(tmp_path)
    run(root)
    first = {a: soul(root, a) for a in PERSONAS}
    r = run(root)
    assert r.returncode == 0, r.stderr
    assert {a: soul(root, a) for a in PERSONAS} == first
    assert r.stdout.count("block unchanged") == block_count(PERSONAS)


def test_a_changed_block_replaces_in_place_without_a_second_copy(tmp_path):
    # The owner wrote text around an older block; the shared text then changed.
    root = state(
        tmp_path,
        {"kit": f"# Kit\n\nintro\n\n{BEGIN}\nold text\n{END}\n\nafter the block\n"},
    )
    new_block = tmp_path / "new.md"
    new_block.write_text(f"{BEGIN}\n## Development-class problems\n\nnew text\n{END}\n")
    r = run(root, DEV_PROBLEM_BLOCK_FILE=str(new_block))
    assert r.returncode == 0, r.stderr
    assert soul(root, "kit") == (
        f"# Kit\n\nintro\n\n{BEGIN}\n## Development-class problems\n\nnew text\n{END}"
        f"\n\nafter the block\n\n{A2A.read_text().strip()}\n\n{RECEIPTS.read_text().strip()}"
        f"\n\n{INTENTS.read_text().strip()}\n"
    )
    assert "dev-problem block updated" in r.stdout
    assert "a2a-envelope block installed" in r.stdout


def test_named_agents_only(tmp_path):
    root = state(tmp_path)
    r = run(root, "kit", "career")
    assert r.returncode == 0, r.stderr
    assert BEGIN in soul(root, "kit") and BEGIN in soul(root, "career")
    assert soul(root, "finance") == PERSONAS["finance"]


def test_a_workspace_without_a_soul_is_reported_and_the_others_still_land(tmp_path):
    root = state(tmp_path, {"kit": PERSONAS["kit"], "ghost": None})
    r = run(root)
    assert r.returncode == 1
    assert "agent 'ghost': no SOUL.md" in r.stderr
    assert BEGIN in soul(root, "kit")
    assert not (root / "agents/ghost/workspace/SOUL.md").exists()


def test_a_lone_marker_leaves_that_file_untouched(tmp_path):
    broken = "# Kit\n\n" + BEGIN + "\nhalf a block, edited by hand\n"
    root = state(tmp_path, {"kit": broken, "career": PERSONAS["career"]})
    r = run(root)
    assert r.returncode == 1
    assert soul(root, "kit") == broken
    assert "kit" in r.stderr and "left untouched" in r.stderr
    assert BEGIN in soul(root, "career")


def test_dry_run_writes_nothing(tmp_path):
    root = state(tmp_path)
    r = run(root, DRY_RUN="1")
    assert r.returncode == 0, r.stderr
    assert {a: soul(root, a) for a in PERSONAS} == PERSONAS
    assert r.stdout.count("block would-install") == block_count(PERSONAS)


def test_a_state_dir_with_no_workspace_fails_loudly(tmp_path):
    r = run(tmp_path / "empty")
    assert r.returncode == 1
    assert "no agent workspace" in r.stderr


def test_the_block_carries_the_envelope_and_the_ack(tmp_path):
    # Every agent's SOUL.md gets the envelope fields and the ack rule, and the
    # rule cites the vault build standard rather than restating it.
    root = state(tmp_path)
    assert run(root).returncode == 0
    for agent in PERSONAS:
        text = soul(root, agent)
        assert A2A_BEGIN in text
        for field in (
            '"a2a":1',
            '"thread"',
            '"from"',
            '"to"',
            '"type"',
            '"turn"',
            '"reply_expected"',
            '"deadline"',
            '"origin"',
            '"owner"',
            '"class"',
            '"fingerprint"',
            '"acceptance"',
            '"body"',
        ):
            assert field in text, field
        # The receipt: one ack first, before any work, with its state and home.
        assert "`type: ack`" in text
        assert "before any work or reply" in text
        assert "`submitted` or `rejected`" in text
        assert "where the item now lives" in text
        # An already-open thread gets no second item.
        assert "no second item" in text and "`state: working`" in text
        # The sender stores the receipt and never re-sends.
        assert "never re-send" in text
        assert "Envelope and receipt at every door" in text


def test_a_lone_a2a_marker_leaves_the_whole_file_untouched(tmp_path):
    broken = "# Kit\n\n" + A2A_BEGIN + "\nhalf a block\n"
    root = state(tmp_path, {"kit": broken, "career": PERSONAS["career"]})
    r = run(root)
    assert r.returncode == 1
    assert "a2a-envelope marker" in r.stderr and "left untouched" in r.stderr
    assert soul(root, "kit") == broken
    assert A2A_BEGIN in soul(root, "career")


def test_the_receipts_block_states_the_rule_and_its_doors(tmp_path):
    root = state(tmp_path)
    assert run(root).returncode == 0
    for agent in PERSONAS:
        text = soul(root, agent)
        assert text.count(RECEIPTS_BEGIN) == 1 and text.count(RECEIPTS_END) == 1
        # A receipt in the intake store, in the same turn, with the 0016 envelope.
        assert "receipt in the intake store in the turn it is said" in text
        for field in (
            "origin",
            "owner",
            "class",
            "fingerprint",
            "acceptance",
            "thread",
        ):
            assert f"`{field}`" in text, field
        assert "decision 0016 envelope" in text
        # The owner sees the receipt id; nothing is held for later.
        assert "shows the owner the receipt id" in text
        assert 'Never "save it for later"' in text
        assert "Reversible work needs no second go from the owner" in text
        # The doors and what to do when one fails.
        assert "`file_intake`" in text and "intake projects" in text
        assert "agent-to-agent envelope to Kit" in text
        assert "write the failure itself to the intake store" in text
        assert "Never hand the item back to the owner" in text


def test_check_passes_after_install_and_writes_nothing(tmp_path):
    root = state(tmp_path)
    assert run(root).returncode == 0
    before = {a: soul(root, a) for a in PERSONAS}
    r = run(root, "--check")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("block unchanged") == block_count(PERSONAS)
    assert {a: soul(root, a) for a in PERSONAS} == before


def test_check_fails_when_an_agent_lacks_the_block(tmp_path):
    root = state(tmp_path)
    r = run(root, "--check")
    assert r.returncode == 1
    assert "agent 'kit': receipts block missing" in r.stdout
    assert "check failed" in r.stderr
    # --check never writes.
    assert {a: soul(root, a) for a in PERSONAS} == PERSONAS


def test_check_fails_when_the_text_between_the_markers_differs(tmp_path):
    root = state(tmp_path)
    assert run(root).returncode == 0
    path = root / "agents/career/workspace/SOUL.md"
    drifted = soul(root, "career").replace(
        "Reversible work needs no second go", "Reversible work needs a second go"
    )
    assert drifted != soul(root, "career")
    path.write_text(drifted)
    r = run(root, "--check")
    assert r.returncode == 1
    assert "agent 'career': receipts block differs" in r.stdout
    assert "agent 'kit': receipts block unchanged" in r.stdout
    assert soul(root, "career") == drifted
    # Re-installing restores the shared text, and the check is clean again.
    assert run(root).returncode == 0
    assert run(root, "--check").returncode == 0


def test_check_fails_for_a_workspace_without_a_soul(tmp_path):
    root = state(tmp_path, {"kit": PERSONAS["kit"], "ghost": None})
    assert run(root, "kit").returncode == 0
    r = run(root, "--check")
    assert r.returncode == 1
    assert "agent 'ghost': no SOUL.md" in r.stderr


def test_an_agent_defined_later_fails_the_check_until_the_next_install(tmp_path):
    root = state(tmp_path, {"kit": PERSONAS["kit"]})
    assert run(root).returncode == 0
    ws = root / "agents/latecomer/workspace"
    ws.mkdir(parents=True)
    (ws / "SOUL.md").write_text("# SOUL.md - Latecomer\n")
    r = run(root, "--check")
    assert r.returncode == 1
    assert "agent 'latecomer': receipts block missing" in r.stdout
    r = run(root)
    assert r.returncode == 0, r.stderr
    assert "agent 'latecomer': receipts block installed" in r.stdout
    assert "agent 'kit': receipts block unchanged" in r.stdout
    assert soul(root, "latecomer") == "# SOUL.md - Latecomer\n\n" + block_text() + "\n"
    assert run(root, "--check").returncode == 0


def test_an_unknown_option_is_refused(tmp_path):
    r = run(state(tmp_path), "--bogus")
    assert r.returncode == 2
    assert "unknown option" in r.stderr


def definition(defs, agent, deny, send):
    """An agent.json shaped like defaults/agents/<id>/agent.json."""
    tools = {"deny": deny, "agentToAgent": {"send": send}}
    path = defs / agent / "agent.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"agents": {"entries": {agent: {"tools": tools}}}}))


def test_the_shipped_agents_with_no_door_get_no_block_and_pass_the_check(tmp_path):
    # The reader and the study panel ship devclaw denied and send [], so neither
    # door is reachable; their SOUL.md stays the git copy their own script installs.
    capped = {
        "reader": (REPO / "defaults/agents/reader/workspace/SOUL.md").read_text(),
        "study-panel": (
            REPO / "defaults/agents/study-panel/workspace/SOUL.md"
        ).read_text(),
    }
    root = state(tmp_path, {"kit": PERSONAS["kit"], **capped})
    r = run(root)
    assert r.returncode == 0, r.stderr
    for agent, before in capped.items():
        assert soul(root, agent) == before
        assert f"agent '{agent}': no door reachable" in r.stdout
        assert f"agent '{agent}': receipts block" not in r.stdout
    assert "agent 'kit': receipts block installed" in r.stdout
    r = run(root, "--check")
    assert r.returncode == 0, r.stderr
    # Named explicitly, a capped agent is still skipped.
    r = run(root, "reader")
    assert r.returncode == 0, r.stderr
    assert soul(root, "reader") == capped["reader"]


def test_a_later_definition_decides_by_its_doors_not_its_name(tmp_path):
    defs = tmp_path / "defs"
    definition(defs, "mailbox", ["group:web", "devclaw__*"], [])
    definition(defs, "sealed", ["*"], [])
    definition(defs, "helper", ["group:web"], ["kit"])
    # One door still open: devclaw intake is allowed even though it sends to no one.
    definition(defs, "filer", ["group:web"], [])
    personas = {
        a: f"# SOUL.md - {a}\n" for a in ("mailbox", "sealed", "helper", "filer")
    }
    root = state(tmp_path, personas)
    r = run(root, AGENT_DEFS_DIR=str(defs))
    assert r.returncode == 0, r.stderr
    for agent in ("mailbox", "sealed"):
        assert soul(root, agent) == personas[agent]
        assert f"agent '{agent}': no door reachable" in r.stdout
    for agent in ("helper", "filer"):
        assert soul(root, agent) == personas[agent] + "\n" + block_text() + "\n"
    assert run(root, "--check", AGENT_DEFS_DIR=str(defs)).returncode == 0


def test_only_kit_and_ledger_get_the_standing_intents_block(tmp_path):
    root = state(tmp_path)
    r = run(root)
    assert r.returncode == 0, r.stderr
    for agent in PERSONAS:
        assert soul(root, agent).count(INTENTS_BEGIN) == (agent in INTENT_AGENTS)
        assert (f"agent '{agent}': standing-intents block" in r.stdout) == (
            agent in INTENT_AGENTS
        )


def test_the_standing_intents_block_replaces_the_one_line_rule_in_place(tmp_path):
    before = f"# SOUL\n\n## Memory\n\nnotes\n\n{LEGACY_INTENT_LINE}\n\n## Last\n\nend\n"
    root = state(tmp_path, {"kit": before, "finance": before})
    assert run(root).returncode == 0
    for agent in INTENT_AGENTS:
        text = soul(root, agent)
        assert LEGACY_INTENT_LINE not in text.replace(INTENTS.read_text(), "")
        intents = INTENTS.read_text().strip("\n")
        assert text.startswith(
            f"# SOUL\n\n## Memory\n\nnotes\n\n{intents}\n\n## Last\n\nend\n"
        )
    second = run(root)
    assert second.returncode == 0
    assert "standing-intents block unchanged" in second.stdout
    assert run(root, "--check").returncode == 0


def test_the_standing_intents_block_sets_a_date_expiry_and_a_one_line_renew():
    text = INTENTS.read_text()
    assert text.startswith(INTENTS_BEGIN + "\n") and text.rstrip().endswith(INTENTS_END)
    # The block keeps the one-line rule it replaces, so nothing is lost.
    assert LEGACY_INTENT_LINE in text
    for phrase in (
        "`expiresAt` on every create",
        "90 days",
        "#698",
        "`expired`",
        '"renew',
    ):
        assert phrase in text, phrase
