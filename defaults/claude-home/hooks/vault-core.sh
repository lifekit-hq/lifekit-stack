#!/bin/sh
# vault-core.sh - Claude Code SessionStart hook: inject ~/memory/CORE.md into every session.
# The pointer in ~/.claude/CLAUDE.md says "read CORE first"; a pointer is a hope. This makes
# the read deterministic (the environment is the instruction), ~1.7k tokens per session.
# Silent when the vault or CORE.md is absent. Never fails the session start.
core="$HOME/memory/CORE.md"
[ -f "$core" ] || exit 0
echo '<vault-core source="~/memory/CORE.md">'
cat "$core"
echo '</vault-core>'
exit 0
