#!/usr/bin/env bash
# install-workspace-pointers.sh — converge each listed OpenClaw agent's
# workspace MEMORY.md and USER.md on the vault pointers in
# defaults/workspace-pointers/. The vault is the one memory store; these two
# upstream bootstrap files stay (OpenClaw loads them) but hold no facts.
#
# No backup is kept: the openclaw-config mirror's git history holds every
# version mirrored before the files became pointers. An agent joins AGENTS
# only once the facts in its two files are confirmed in the vault.
#
# Runs as the account that owns the workspaces (lifekit): deploy.sh runs it on
# every deploy, so a hand edit or an agent-written fact is rewritten. Idempotent;
# it prints one line per file it changed.
#
# Env overrides (tests): LIFEKIT_AGENTS_DIR (default /srv/openclaw/config/agents).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${HERE}/../../defaults/workspace-pointers"
AGENTS_DIR="${LIFEKIT_AGENTS_DIR:-/srv/openclaw/config/agents}"
AGENTS=(kit career finance)

for agent in "${AGENTS[@]}"; do
  ws="${AGENTS_DIR}/${agent}/workspace"
  [[ -d "${ws}" ]] || continue
  for f in MEMORY.md USER.md; do
    cmp -s "${SRC}/${f}" "${ws}/${f}" && continue
    install -m 644 "${SRC}/${f}" "${ws}/.${f}.tmp"
    mv -f "${ws}/.${f}.tmp" "${ws}/${f}"
    echo "workspace pointers: wrote ${ws}/${f}"
  done
done
