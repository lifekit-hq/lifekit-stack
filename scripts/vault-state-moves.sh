#!/usr/bin/env bash
# vault-state-moves.sh — move runtime state the memory vault no longer holds
# into the runtime-state dir (/var/lib/lifekit). deploy.sh runs it as lifekit on
# every deploy, before `up`. Idempotent: it never overwrites a destination and
# never deletes anything, so a second run is a no-op.
#
#   morning-brief  inbox.md, inbox-archive.md and topics.yaml, the morning
#                  brief's inputs, live in $STATE/morning-brief/ (the gateway
#                  sees it as ~/.life-state/morning-brief/). Each file absent
#                  there is seeded once: from the vault's copy when it has one,
#                  else from the generic template in defaults/morning-brief/.
#                  The vault copies stay where they are; retiring them is the
#                  vault's own change. memory-rotate rotates the inbox there.
#   ops-agent      the retired ops-agent's run dumps and .seen markers move
#                  verbatim from the vault's projects/ops-agent/ to
#                  $STATE/ops-agent/. plan.md and log.md stay in the vault. An
#                  entry git still tracks (a .seen marker before the vault
#                  untracks it) is left for a later run; an entry whose
#                  destination already exists is left in place and reported.
#
# Env overrides (tests): LIFEKIT_LIFE_DIR (vault, default /srv/memory),
# LIFEKIT_STATE_DIR_HOST (default /var/lib/lifekit).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES="${HERE}/../defaults/morning-brief"
VAULT="${LIFEKIT_LIFE_DIR:-/srv/memory}"
STATE="${LIFEKIT_STATE_DIR_HOST:-/var/lib/lifekit}"

status=0

# ─── morning-brief inputs ────────────────────────────────────────────────────
BRIEF="${STATE}/morning-brief"
mkdir -p "${BRIEF}"
for f in inbox.md inbox-archive.md topics.yaml; do
  [[ -e "${BRIEF}/${f}" ]] && continue
  if [[ -f "${VAULT}/${f}" ]]; then
    cp -p "${VAULT}/${f}" "${BRIEF}/${f}"
    echo "morning-brief: seeded ${BRIEF}/${f} from the vault copy"
  else
    cp "${TEMPLATES}/${f}" "${BRIEF}/${f}"
    echo "morning-brief: seeded ${BRIEF}/${f} from the template"
  fi
done

# ─── ops-agent run dumps ─────────────────────────────────────────────────────
OPS_SRC="${VAULT}/projects/ops-agent"
OPS_DST="${STATE}/ops-agent"

tracked() { [[ -n "$(git -C "${VAULT}" ls-files -- "$1" 2>/dev/null)" ]]; }

# move SRC DST -> 0 moved or skipped quietly, 1 left in place because DST exists.
move() {
  if [[ -e "$2" ]]; then
    echo "ops-agent: $2 already exists, left $1 in place" >&2
    return 1
  fi
  mv -n "$1" "$2"
}

if [[ -d "${OPS_SRC}" ]]; then
  moved=0 kept=0
  shopt -s nullglob
  for run in "${OPS_SRC}"/2*/; do
    run="${run%/}"
    if tracked "${run#"${VAULT}"/}"; then kept=$((kept + 1)); continue; fi
    mkdir -p "${OPS_DST}"
    if move "${run}" "${OPS_DST}/${run##*/}"; then moved=$((moved + 1)); else status=1; fi
  done
  for seen in "${OPS_SRC}"/.seen/*; do
    if tracked "${seen#"${VAULT}"/}"; then kept=$((kept + 1)); continue; fi
    mkdir -p "${OPS_DST}/.seen"
    if move "${seen}" "${OPS_DST}/.seen/${seen##*/}"; then moved=$((moved + 1)); else status=1; fi
  done
  shopt -u nullglob
  if [[ -d "${OPS_SRC}/.seen" ]]; then rmdir "${OPS_SRC}/.seen" 2>/dev/null || true; fi
  if ((moved || kept)); then
    echo "ops-agent: moved ${moved} entries to ${OPS_DST}, ${kept} still tracked by the vault left in place"
  fi
fi

exit "${status}"
