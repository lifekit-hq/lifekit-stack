"""scripts/learn-mcp/token.sh: one token, written once to an owner-only file
and handed to set-secret.sh on stdin, never printed and never on argv."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/learn-mcp/token.sh"

# Stands in for set-secret.sh: records its argv and what came on stdin.
STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >"$STUB_DIR/argv"
cat >"$STUB_DIR/stdin"
exit "${STUB_EXIT:-0}"
"""


def run(tmp_path, exit_code=0):
    stub = tmp_path / "set-secret.sh"
    stub.write_text(STUB)
    token_file = tmp_path / "home/.config/lifekit/learn-mcp.token"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "LEARN_MCP_SET_SECRET": str(stub),
        "STUB_DIR": str(tmp_path),
        "STUB_EXIT": str(exit_code),
    }
    r = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    return r, token_file


def test_the_token_reaches_the_owner_file_and_sops_and_nothing_else(tmp_path):
    r, token_file = run(tmp_path)
    assert r.returncode == 0, r.stderr
    token = token_file.read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    assert (tmp_path / "stdin").read_text() == token
    assert (tmp_path / "argv").read_text().strip() == "master LEARN_MCP_TOKEN"
    assert token not in r.stdout + r.stderr
    assert str(token_file) in r.stdout
    # No leftover temp file next to it.
    assert [p.name for p in token_file.parent.iterdir()] == ["learn-mcp.token"]


def test_running_it_again_rotates(tmp_path):
    _, token_file = run(tmp_path)
    first = token_file.read_text()
    _, _ = run(tmp_path)
    assert token_file.read_text() != first


def test_a_failed_sops_change_leaves_the_owner_file_as_it_was(tmp_path):
    _, token_file = run(tmp_path)
    before = token_file.read_text()
    r, _ = run(tmp_path, exit_code=1)
    assert r.returncode != 0
    assert token_file.read_text() == before
    assert [p.name for p in token_file.parent.iterdir()] == ["learn-mcp.token"]
