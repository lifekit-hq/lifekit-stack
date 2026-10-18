"""Behaviour of scripts/vault-state-moves.sh against a temp vault and state dir."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/vault-state-moves.sh"
TEMPLATES = REPO / "defaults/morning-brief"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def box(tmp_path):
    vault, state = tmp_path / "vault", tmp_path / "state"
    vault.mkdir()
    state.mkdir()
    git(vault, "init", "-q")
    git(vault, "config", "user.email", "t@example.invalid")
    git(vault, "config", "user.name", "t")
    (vault / ".gitignore").write_text("/projects/ops-agent/2*/\n")

    def run():
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env={
                **os.environ,
                "LIFEKIT_LIFE_DIR": str(vault),
                "LIFEKIT_STATE_DIR_HOST": str(state),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    return vault, state, run


def test_morning_brief_seeds_from_vault_else_template_and_never_overwrites(box):
    vault, state, run = box
    (vault / "topics.yaml").write_text("vault topics\n")
    result = run()
    assert result.returncode == 0, result.stderr
    brief = state / "morning-brief"
    assert (brief / "topics.yaml").read_text() == "vault topics\n"
    assert (brief / "inbox.md").read_text() == (TEMPLATES / "inbox.md").read_text()
    assert (vault / "topics.yaml").exists()  # the vault copy stays

    (brief / "topics.yaml").write_text("edited live\n")
    (vault / "inbox.md").write_text("vault inbox\n")
    assert run().returncode == 0
    assert (brief / "topics.yaml").read_text() == "edited live\n"
    assert (brief / "inbox.md").read_text() == (TEMPLATES / "inbox.md").read_text()


def test_ops_agent_dumps_move_verbatim_and_tracked_entries_wait(box):
    vault, state, run = box
    ops = vault / "projects/ops-agent"
    run_dir = ops / "2026-09-01T00-00-00-O1-x"
    run_dir.mkdir(parents=True)
    (run_dir / "decision.json").write_text('{"a": 1}\n')
    (ops / ".seen").mkdir()
    (ops / ".seen/tracked.touch").write_text("")
    (ops / ".seen/loose.touch").write_text("")
    (ops / "plan.md").write_text("# plan\n")
    (ops / "log.md").write_text("# log\n")
    git(
        vault,
        "add",
        "-f",
        ".gitignore",
        "projects/ops-agent/.seen/tracked.touch",
        "projects/ops-agent/plan.md",
        "projects/ops-agent/log.md",
    )
    git(vault, "commit", "-qm", "seed")

    result = run()
    assert result.returncode == 0, result.stderr
    dst = state / "ops-agent"
    assert (dst / run_dir.name / "decision.json").read_text() == '{"a": 1}\n'
    assert not run_dir.exists()
    assert (dst / ".seen/loose.touch").exists()
    assert (ops / ".seen/tracked.touch").exists()  # still tracked: waits
    assert not (dst / ".seen/tracked.touch").exists()
    assert (ops / "plan.md").exists() and (ops / "log.md").exists()

    # The vault untracks the marker; the next run moves it, the one after is a no-op.
    git(vault, "rm", "-q", "--cached", "projects/ops-agent/.seen/tracked.touch")
    assert run().returncode == 0
    assert (dst / ".seen/tracked.touch").exists()
    assert not (ops / ".seen").exists()
    again = run()
    assert again.returncode == 0
    assert "ops-agent" not in again.stdout


def test_existing_destination_is_never_overwritten(box):
    vault, state, run = box
    run_dir = vault / "projects/ops-agent/2026-09-02-run"
    run_dir.mkdir(parents=True)
    (run_dir / "outcome.md").write_text("vault copy\n")
    (state / "ops-agent/2026-09-02-run").mkdir(parents=True)
    (state / "ops-agent/2026-09-02-run/outcome.md").write_text("state copy\n")
    result = run()
    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert (run_dir / "outcome.md").read_text() == "vault copy\n"
    assert (state / "ops-agent/2026-09-02-run/outcome.md").read_text() == "state copy\n"
