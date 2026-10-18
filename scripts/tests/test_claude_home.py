"""Behaviour of scripts/claude-home/install-claude-home.sh against a temp ~/.claude."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/claude-home/install-claude-home.sh"
DEFAULTS = REPO / "defaults/claude-home"


def run(home):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "LIFEKIT_CLAUDE_HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
    )


def session_start_commands(settings):
    return [
        h["command"]
        for entry in settings["hooks"]["SessionStart"]
        for h in entry["hooks"]
    ]


def test_merges_into_existing_settings_and_keeps_other_keys(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps(
            {
                "theme": "dark",
                "env": {"KEEP": "1"},
                "hooks": {
                    "SessionStart": [
                        {"matcher": "", "hooks": [{"type": "command", "command": "x"}]}
                    ],
                    "Stop": [],
                },
            }
        )
    )
    (home / "settings.json").chmod(0o600)

    run(home)

    settings = json.loads((home / "settings.json").read_text())
    assert settings["theme"] == "dark"
    assert settings["hooks"]["Stop"] == []
    assert settings["env"] == {"KEEP": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    commands = session_start_commands(settings)
    assert commands[0] == "x"
    assert "$HOME/.claude/hooks/vault-core.sh" in commands
    assert any("bin/session-context.sh" in c for c in commands)
    assert (home / "settings.json").stat().st_mode & 0o777 == 0o600


def test_second_run_changes_nothing(tmp_path):
    home = tmp_path / ".claude"
    run(home)
    before = {p.name: p.read_bytes() for p in home.rglob("*") if p.is_file()}

    out = run(home)

    assert out.stdout == ""
    assert {p.name: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
    settings = json.loads((home / "settings.json").read_text())
    assert len(session_start_commands(settings)) == 2


def test_previous_claude_md_kept_once(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "CLAUDE.md").write_text("old profile\n")

    run(home)
    (home / "CLAUDE.md").write_text("hand edit\n")
    run(home)

    assert (home / "CLAUDE.md.pre-vault-pointer").read_text() == "old profile\n"
    assert (home / "CLAUDE.md").read_bytes() == (DEFAULTS / "CLAUDE.md").read_bytes()


def test_auto_memory_files_left_alone(tmp_path):
    home = tmp_path / ".claude"
    mem = home / "projects/-x/memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("kept\n")

    run(home)

    assert (mem / "MEMORY.md").read_text() == "kept\n"


def test_vault_core_hook_injects_core_and_is_silent_without_it(tmp_path):
    hook = DEFAULTS / "hooks/vault-core.sh"
    env = {**os.environ, "HOME": str(tmp_path)}

    silent = subprocess.run(["sh", str(hook)], env=env, capture_output=True, text=True)
    assert (silent.returncode, silent.stdout) == (0, "")

    (tmp_path / "memory").mkdir()
    (tmp_path / "memory/CORE.md").write_text("core\n")
    out = subprocess.run(["sh", str(hook)], env=env, capture_output=True, text=True)
    assert out.returncode == 0
    assert out.stdout == '<vault-core source="~/memory/CORE.md">\ncore\n</vault-core>\n'
