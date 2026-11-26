#!/usr/bin/env bash
# ensure-dev-problem-block.sh - put the shared persona blocks into the SOUL.md
# of every OpenClaw agent workspace:
#   dev-problems  (defaults/agents/shared/dev-problems.md): file a development-class
#                 problem through devclaw intake and carry on, never telling the owner
#                 (docs/runbook.md, "Development-class problems: file and carry on").
#   a2a-envelope  (defaults/agents/shared/a2a-envelope.md): the one agent-to-agent
#                 envelope and the mandatory ack (docs/runbook.md, "Agent-to-agent
#                 envelope and ack").
#   receipts      (defaults/agents/shared/receipts.md): every owner ask or commitment
#                 gets an intake-store receipt in the same turn, never "saved for later"
#                 (docs/runbook.md, "Owner asks get a receipt in the same turn").
#
# One source per block: the text lives only in its shared file. The script
# finds every workspace under <config>/agents/*/workspace and replaces each
# block between its two marker lines in that workspace's SOUL.md, or appends it
# when the markers are absent. Nothing else in the file is touched, so the
# per-agent persona and Ledger's own notes stay as they are. (Ledger's AGENTS.md
# is composed from finance-sentry and byte-compared by ledger-persona.py; its
# SOUL.md is host state, so the block goes there.)
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-dev-problem-block.sh [--check|--dry-run] [agent-id ...]
# With no ids it covers every workspace found, so an agent defined later gets
# the blocks on the next run. --check writes nothing and exits 1 when any
# workspace lacks a block, holds a different text between its markers, has no
# SOUL.md or has a marker problem; each such block is named on stdout.
# ensure-learnclaw.sh runs it for learnclaw after the deploy rewrites that
# SOUL.md from git.
#
# Re-running converges. A workspace with no SOUL.md, or whose SOUL.md has a
# lone or reversed marker, is left untouched and reported, and the exit code
# is 1 once the other workspaces are done.
#
# Env overrides:
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
#   DEV_PROBLEM_BLOCK_FILE  the dev-problems block to install (default: the shared file)
#   A2A_BLOCK_FILE          the a2a-envelope block to install (default: the shared file)
#   RECEIPTS_BLOCK_FILE     the receipts block to install (default: the shared file)
#   DRY_RUN=1               same as --dry-run: report what would change, write nothing
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
config_dir="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
mode=""
[ -z "${DRY_RUN:-}" ] || mode=dry

agents=()
for arg in "$@"; do
  case "$arg" in
    --check) mode=check ;;
    --dry-run) [ "$mode" = check ] || mode=dry ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) agents+=("$arg") ;;
  esac
done

# label|marker name|block file; the markers are <!-- lifekit:NAME:begin|end -->.
blocks=(
  "dev-problem|dev-problems|${DEV_PROBLEM_BLOCK_FILE:-${repo_root}/defaults/agents/shared/dev-problems.md}"
  "a2a-envelope|a2a-envelope|${A2A_BLOCK_FILE:-${repo_root}/defaults/agents/shared/a2a-envelope.md}"
  "receipts|receipts|${RECEIPTS_BLOCK_FILE:-${repo_root}/defaults/agents/shared/receipts.md}"
)
for spec in "${blocks[@]}"; do
  [ -f "${spec##*|}" ] || { echo "no block file at ${spec##*|}" >&2; exit 1; }
done

if [ "${#agents[@]}" -eq 0 ]; then
  for ws in "$config_dir"/agents/*/workspace; do
    [ -d "$ws" ] && agents+=("$(basename "$(dirname "$ws")")")
  done
fi
if [ "${#agents[@]}" -eq 0 ]; then
  echo "no agent workspace under ${config_dir}/agents" >&2
  exit 1
fi

# stdout: one "<label> <verb>" line per block, verb = installed | updated |
# unchanged | would-install | would-update (dry run) | missing | differs
# (check). All blocks are checked before anything is written, so a lone or
# reversed marker for any of them (exit 3) leaves the whole file untouched.
splice() { # $1 = SOUL.md path
  python3 - "$1" "$mode" "${blocks[@]}" <<'PY'
import os, sys, tempfile

path, mode = sys.argv[1], sys.argv[2]
dry = bool(mode)
text = open(path, encoding="utf-8").read()
WOULD = {
    "dry": {"installed": "would-install", "updated": "would-update"},
    "check": {"installed": "missing", "updated": "differs"},
}.get(mode)
new, out = text, []
for spec in sys.argv[3:]:
    label, name, block_path = spec.split("|", 2)
    BEGIN, END = f"<!-- lifekit:{name}:begin -->", f"<!-- lifekit:{name}:end -->"
    block = open(block_path, encoding="utf-8").read().strip("\n")
    b, e = new.find(BEGIN), new.find(END)
    if (b < 0) != (e < 0) or new.count(BEGIN) > 1 or new.count(END) > 1 or (b >= 0 and e < b):
        print(f"lone, repeated or reversed {name} marker", file=sys.stderr)
        sys.exit(3)
    if b >= 0:
        after = new[:b] + block + new[e + len(END):]
        verb = "updated"
    else:
        after = new.rstrip("\n") + ("\n\n" if new.strip() else "") + block + "\n"
        verb = "installed"
    if after == new:
        out.append(f"{label} unchanged")
    else:
        out.append(f"{label} " + (WOULD[verb] if dry else verb))
    new = after
if new != text and not dry:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".soul-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    os.replace(tmp, path)
print("\n".join(out))
PY
}

rc=0
drift=0
for agent in "${agents[@]}"; do
  soul="${config_dir}/agents/${agent}/workspace/SOUL.md"
  if [ ! -f "$soul" ]; then
    echo "agent '$agent': no SOUL.md at $soul; block not installed" >&2
    rc=1
    continue
  fi
  if verbs="$(splice "$soul")"; then
    while IFS= read -r line; do
      echo "agent '$agent': ${line% *} block ${line##* } -> $soul"
      case "${line##* }" in missing | differs) drift=1 ;; esac
    done <<<"$verbs"
  else
    echo "agent '$agent': $soul left untouched (block marker problem above)" >&2
    rc=1
  fi
done
if [ "$drift" -eq 1 ]; then
  echo "check failed: a shared persona block is missing or differs; run $(basename "$0") without --check" >&2
  rc=1
fi
exit "$rc"
