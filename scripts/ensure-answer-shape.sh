#!/usr/bin/env bash
# ensure-answer-shape.sh - put the "Answer shape (every reply)" block (the
# 2026-11-15 rule: answer first, 75-word budget, plain short sentences) into the
# live SOUL.md of the personas that lack it: health, learning, social and reader
# (docs/runbook.md, "Answer shape in the remaining personas").
#
# One source: the block is read from Kit's seed, the "## Answer shape (every
# reply)" section of defaults/agents/kit/workspace/SOUL.md (up to the next "## "
# heading), so the wording cannot drift from Kit's. A persona whose SOUL.md
# already has that heading is left byte-identical. Otherwise the block is
# inserted once, just before its "## Voice" section (appended at the end when
# there is none). It only adds lines: no other text is rewritten, so a second
# run adds nothing. Writes are atomic (temp file + rename, mode kept).
#
# A persona with no workspace on this box (health, learning and social were
# retired 2026-10-07) is skipped with a note and does not fail the run; the
# reader seed in git already carries the block, and ensure-reader-agent.sh
# rewrites the live reader file from it on every deploy.
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-answer-shape.sh [--dry-run] [agent-id ...]
# With no ids it covers health learning social reader. --dry-run (or DRY_RUN=1)
# prints what it would change per persona and writes nothing.
#
# Exit codes: 0 done (or nothing to do); 1 a SOUL.md could not be read or has a
# problem the script will not guess at (no usable source block).
#
# Env overrides:
#   OPENCLAW_CONFIG_DIR   host path of the gateway state dir
#                         (default: /srv/openclaw/config, as in compose)
#   ANSWER_SHAPE_SOURCE   the SOUL.md to take the block from
#                         (default: Kit's seed in this checkout)
#   DRY_RUN=1             same as --dry-run
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
config_dir="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
source_soul="${ANSWER_SHAPE_SOURCE:-${repo_root}/defaults/agents/kit/workspace/SOUL.md}"
dry="${DRY_RUN:-}"

agents=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) agents+=("$arg") ;;
  esac
done
[ "${#agents[@]}" -gt 0 ] || agents=(health learning social reader)
[ -f "$source_soul" ] || { echo "no source SOUL.md at $source_soul" >&2; exit 1; }

# stdout: "present" | "installed" | "would-install"; exit 3 when the source has
# no block.
splice() { # $1 = live SOUL.md path
  python3 - "$1" "$source_soul" "$dry" <<'PY'
import os, sys, tempfile

path, source, dry = sys.argv[1], sys.argv[2], bool(sys.argv[3])
HEADING = "## Answer shape (every reply)"

def section(text):
    """The heading line through the line before the next '## ' heading."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.rstrip("\r\n") == HEADING), None)
    if start is None:
        return None, None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines, (start, end)

live = open(path, encoding="utf-8", newline="").read()
if any(l.rstrip("\r\n") == HEADING for l in live.splitlines()):
    print("present")
    sys.exit(0)

src_lines, span = section(open(source, encoding="utf-8", newline="").read())
if span is None:
    print(f"no '{HEADING}' section in {source}", file=sys.stderr)
    sys.exit(3)
block = "".join(src_lines[span[0]:span[1]]).strip("\n") + "\n"

lines = live.splitlines(keepends=True)
at = next((i for i, l in enumerate(lines) if l.rstrip("\r\n") == "## Voice"), None)
if at is None:
    head = live if not live or live.endswith("\n") else live + "\n"
    new = head + ("\n" if head.strip() else "") + block
else:
    new = "".join(lines[:at]) + block + "\n" + "".join(lines[at:])

if dry:
    print("would-install")
    sys.exit(0)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".soul-")
with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
    f.write(new)
os.chmod(tmp, os.stat(path).st_mode & 0o7777)
os.replace(tmp, path)
print("installed")
PY
}

rc=0
for agent in "${agents[@]}"; do
  soul="${config_dir}/agents/${agent}/workspace/SOUL.md"
  if [ ! -f "$soul" ]; then
    echo "agent '$agent': no SOUL.md at $soul; skipped"
    continue
  fi
  status=0
  verb="$(splice "$soul")" || status=$?
  if [ "$status" -ne 0 ]; then
    echo "agent '$agent': $soul left untouched (python exit $status)" >&2
    rc=1
    continue
  fi
  case "$verb" in
    present) echo "agent '$agent': answer shape already present; unchanged -> $soul" ;;
    would-install) echo "agent '$agent': would insert the answer shape block (dry run, nothing written) -> $soul" ;;
    installed) echo "agent '$agent': answer shape block inserted -> $soul" ;;
  esac
done
# Record the installed commit (issue 689; the release gauge reads it) after a full
# written run (no agent subset, not --dry-run) that passed.
if [ "$rc" = 0 ] && [ -z "$dry" ] && [ "$#" -eq 0 ]; then
  bash "$(dirname "${BASH_SOURCE[0]}")/lib/install-stamp.sh" answer-shape
fi
exit "$rc"
