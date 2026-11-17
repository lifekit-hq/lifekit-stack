"""ensure-dev-problem-block.sh: the one shared persona block for every agent.

The script is the only thing that puts the block into a workspace, so these run
it for real against a scratch gateway state dir. The oracle is the marker
contract in the script's header: the text between the two marker lines is the
block and nothing else in SOUL.md moves.
"""

from __future__ import annotations

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

# What each agent's persona holds before the block lands (host state: free text).
PERSONAS = {
    "kit": "# SOUL.md - Kit\n\nSharp, terse.\n",
    "finance": "# SOUL\n\nLedger's own notes.\n\n## Voice\n\nPlain.\n",
    "career": "# SOUL.md - Career\n",
    "learnclaw": "# SOUL.md - Learnclaw\n\nLoop owner.\n",
    "newcomer": "an agent added after this script was written\n",
}


def block_text() -> str:
    """Both shared blocks as they land in a SOUL.md: dev-problems, then a2a."""
    return SHARED.read_text().strip("\n") + "\n\n" + A2A.read_text().strip("\n")


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
        assert soul(root, agent) == before.rstrip("\n") + "\n\n" + block_text() + "\n"
        assert f"agent '{agent}': dev-problem block installed" in r.stdout
        assert f"agent '{agent}': a2a-envelope block installed" in r.stdout


def test_a_second_run_changes_nothing(tmp_path):
    root = state(tmp_path)
    run(root)
    first = {a: soul(root, a) for a in PERSONAS}
    r = run(root)
    assert r.returncode == 0, r.stderr
    assert {a: soul(root, a) for a in PERSONAS} == first
    assert r.stdout.count("block unchanged") == 2 * len(PERSONAS)


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
        f"\n\nafter the block\n\n{A2A.read_text().strip()}\n"
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
    assert r.stdout.count("block would-install") == 2 * len(PERSONAS)


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
