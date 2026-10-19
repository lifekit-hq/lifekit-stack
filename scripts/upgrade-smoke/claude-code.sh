#!/usr/bin/env bash
# Post-upgrade smoke check for the Claude Code pillar: does the installed CLI
# report the version it was upgraded to, and does it still authenticate and
# complete one non-interactive turn? Run as the account whose Claude Code was
# upgraded; it uses that account's own login.
#
# Cost: one minimal `claude -p` turn on the cheapest model (default haiku), no
# tools, no MCP servers, no settings sources (so no hooks), from an empty
# directory (so no CLAUDE.md), no session saved. Measured 2026-10-19: ~3.7k
# input tokens (mostly cache), 4 output tokens, reported cost 0.0006 USD,
# about 4 s. `--no-call` skips it.
#
# Output: one summary line on stdout, `upgrade-smoke claude-code: PASS|FAIL ...`.
# The reply text and auth state are never printed beyond the verdict.
# Exit 0 on pass, 1 on fail, 2 on a usage error.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: claude-code.sh [--expect-version <version>] [--no-call]

  --expect-version <v>  Fail unless `claude --version` reports this version
                        (e.g. 2.1.303): proves the upgrade is the one on PATH.
  --no-call             Version check only; no model call.
  -h, --help            Show this help.

Environment: CLAUDE_BIN (default: claude on PATH), SMOKE_MODEL (default haiku),
SMOKE_TIMEOUT (seconds for the turn, default 120).
USAGE
}

EXPECT=""
CALL=1
while (($#)); do
  case "$1" in
    --expect-version) EXPECT="${2:?--expect-version needs a value}"; shift 2 ;;
    --no-call) CALL=0; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
MODEL="${SMOKE_MODEL:-haiku}"
TIMEOUT="${SMOKE_TIMEOUT:-120}"
START="$(date +%s)"
RED=()
VERSION="unknown" TURN="skipped" COST="-"

summary() {
  local verdict="PASS"
  ((${#RED[@]} == 0)) || verdict="FAIL"
  local line="upgrade-smoke claude-code: ${verdict} version=${VERSION} turn=${TURN} cost_usd=${COST} elapsed=$(($(date +%s) - START))s"
  ((${#RED[@]} == 0)) || line+=" red=$(IFS=,; echo "${RED[*]}")"
  echo "${line}"
  ((${#RED[@]} == 0))
}

# ---------------------------------------------------------------- version
raw="$(timeout 30 "${CLAUDE_BIN}" --version </dev/null 2>/dev/null || true)"
if [[ "${raw}" =~ ^([0-9]+\.[0-9]+\.[0-9]+)\ \(Claude\ Code\) ]]; then
  VERSION="${BASH_REMATCH[1]}"
else
  RED+=("version-unreadable")
fi
if [[ -n "${EXPECT}" && "${VERSION}" != "${EXPECT}" ]]; then
  RED+=("version-not-${EXPECT}")
fi

# ---------------------------------------------------------------- one turn
if ((CALL)) && ((${#RED[@]} == 0)); then
  work="$(mktemp -d)"
  trap 'rm -rf "${work}"' EXIT
  rc=0
  out="$(cd "${work}" && timeout "${TIMEOUT}" "${CLAUDE_BIN}" -p \
    "Reply with exactly the word pong and nothing else." \
    --output-format json --model "${MODEL}" --tools "" --strict-mcp-config \
    --setting-sources "" --no-session-persistence --max-turns 1 \
    </dev/null 2>/dev/null)" || rc=$?
  verdict="$(python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    if not isinstance(d, dict):
        raise ValueError
except Exception:
    print("no-json -"); sys.exit()
cost = d.get("total_cost_usd")
cost = f"{cost:.4f}" if isinstance(cost, (int, float)) else "-"
ok = (d.get("type") == "result" and d.get("is_error") is False
      and str(d.get("result") or "").strip().strip(".").lower() == "pong")
print(("ok " if ok else "bad-reply ") + cost)
' <<<"${out}")"
  TURN="${verdict%% *}"
  COST="${verdict##* }"
  ((rc == 0)) || { TURN="exit-${rc}"; }
  [[ "${TURN}" == ok ]] || RED+=("turn-${TURN}")
fi

summary || exit 1
