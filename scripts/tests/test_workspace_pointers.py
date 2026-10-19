"""Behaviour of scripts/workspace-pointers/install-workspace-pointers.sh against temp workspaces."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/workspace-pointers/install-workspace-pointers.sh"
DEFAULTS = REPO / "defaults/workspace-pointers"
FILES = ("MEMORY.md", "USER.md")


def run(agents_dir):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "LIFEKIT_AGENTS_DIR": str(agents_dir)},
        capture_output=True,
        text=True,
        check=True,
    )


def workspace(agents_dir, agent):
    ws = agents_dir / agent / "workspace"
    ws.mkdir(parents=True)
    return ws


def test_replaces_facts_with_the_pointers_and_leaves_other_files(tmp_path):
    ws = workspace(tmp_path, "kit")
    for name in (*FILES, "AGENTS.md"):
        (ws / name).write_text("a fact\n")

    out = run(tmp_path)

    for name in FILES:
        assert (ws / name).read_text() == (DEFAULTS / name).read_text()
        assert f"wrote {ws / name}" in out.stdout
    assert (ws / "AGENTS.md").read_text() == "a fact\n"
    assert sorted(p.name for p in ws.iterdir()) == ["AGENTS.md", *FILES]


def test_second_run_changes_nothing(tmp_path):
    for agent in ("kit", "career", "finance"):
        workspace(tmp_path, agent)
    run(tmp_path)

    assert run(tmp_path).stdout == ""


def test_only_the_listed_agents_and_missing_workspaces_skipped(tmp_path):
    other = workspace(tmp_path, "devclaw")
    (other / "MEMORY.md").write_text("not yet triaged\n")

    assert run(tmp_path).stdout == ""
    assert (other / "MEMORY.md").read_text() == "not yet triaged\n"
    assert not (tmp_path / "kit").exists()


def test_pointers_carry_no_personal_data():
    for name in FILES:
        text = (DEFAULTS / name).read_text()
        assert "~/memory/CORE.md" in text
        assert "{{" not in text
