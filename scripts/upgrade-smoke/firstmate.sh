#!/usr/bin/env bash
# Post-upgrade smoke check for the firstmate pillar: firstmate itself (its code
# root and its live home) plus the no-mistakes toolchain it ships through.
# Read-only: it reads both checkouts and runs only their status and doctor
# commands - `no-mistakes doctor`, `no-mistakes daemon status`, and firstmate's
# own detect-only bootstrap (FM_BOOTSTRAP_DETECT_ONLY=1, local steps only),
# the read-only path firstmate's second concurrent session uses. It never
# edits either checkout and never starts, stops or restarts the daemon. No
# model call.
#
# Output: one summary line on stdout, `upgrade-smoke firstmate: PASS|FAIL ...`.
# Exit 0 on pass, 1 on fail, 2 on a usage error.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: firstmate.sh [--expect-no-mistakes-version <version>]

  --expect-no-mistakes-version <v>  Fail unless `no-mistakes --version` reports
                                    this version (e.g. v1.84.0).
  -h, --help                        Show this help.

Environment: FIRSTMATE_ROOT (code root, default ~/firstmate), FIRSTMATE_HOME
(live home, default ~/fm-vps), NO_MISTAKES_BIN (default: no-mistakes on PATH),
SMOKE_TIMEOUT (seconds per command, default 60).
USAGE
}

EXPECT=""
while (($#)); do
  case "$1" in
    --expect-no-mistakes-version) EXPECT="${2:?--expect-no-mistakes-version needs a value}"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

ROOT="${FIRSTMATE_ROOT:-${HOME}/firstmate}"
FM_HOME_DIR="${FIRSTMATE_HOME:-${HOME}/fm-vps}"
NM="${NO_MISTAKES_BIN:-no-mistakes}"
TIMEOUT="${SMOKE_TIMEOUT:-60}"
START="$(date +%s)"
RED=()
NM_VERSION="unknown" HOME_HEAD="-" NOTES=0

summary() {
  local verdict="PASS"
  ((${#RED[@]} == 0)) || verdict="FAIL"
  local line="upgrade-smoke firstmate: ${verdict} no-mistakes=${NM_VERSION} home=${HOME_HEAD} bootstrap-notes=${NOTES} elapsed=$(($(date +%s) - START))s"
  ((${#RED[@]} == 0)) || line+=" red=$(IFS=,; echo "${RED[*]}")"
  echo "${line}"
  ((${#RED[@]} == 0))
}

run() { timeout "${TIMEOUT}" "$@" </dev/null 2>&1; }

# ---------------------------------------------------------------- no-mistakes
raw="$(run "${NM}" --version || true)"
if [[ "${raw}" =~ version\ (v[0-9]+\.[0-9]+\.[0-9]+) ]]; then
  NM_VERSION="${BASH_REMATCH[1]}"
else
  RED+=("no-mistakes-version-unreadable")
fi
if [[ -n "${EXPECT}" && "${NM_VERSION}" != "${EXPECT}" ]]; then
  RED+=("no-mistakes-not-${EXPECT}")
fi

# `doctor` marks each check with a glyph: ✓ ok, – optional and absent, anything
# else (✗) a failure. git, gh, the database and the daemon must all be ✓.
rc=0
doc="$(run "${NM}" doctor)" || rc=$?
out="$(python3 -c '
import re, sys
seen, bad = {}, []
for line in sys.stdin.read().splitlines():
    m = re.match(r"^\s*(\S)\s+(\S+)", line)
    if not m or m.group(1).isascii():
        continue
    glyph, name = m.groups()
    seen[name] = glyph
    if glyph not in ("✓", "–"):
        bad.append(name)
bad += [n for n in ("git", "gh", "database", "daemon") if seen.get(n) != "✓" and n not in bad]
print(",".join(bad) or "-")
' <<<"${doc}")"
((rc == 0)) || RED+=("no-mistakes-doctor-exit-${rc}")
[[ "${out}" == "-" ]] || RED+=("no-mistakes-doctor:${out}")

status="$(run "${NM}" daemon status || true)"
grep -q 'daemon running' <<<"${status}" || RED+=("no-mistakes-daemon")

# ---------------------------------------------------------------- firstmate
# Both checkouts must be readable git trees on a branch, and every shell script
# in the home's bin/ must still parse.
for dir in "${ROOT}" "${FM_HOME_DIR}"; do
  name="root"
  [[ "${dir}" == "${FM_HOME_DIR}" ]] && name="home"
  if ! git --no-optional-locks -C "${dir}" symbolic-ref -q HEAD >/dev/null 2>&1; then
    RED+=("${name}-not-on-branch")
  fi
done
HOME_HEAD="$(git --no-optional-locks -C "${FM_HOME_DIR}" rev-parse --short HEAD 2>/dev/null || echo -)"

broken=0
for f in "${FM_HOME_DIR}"/bin/*.sh; do
  [[ -f "${f}" ]] || continue
  bash -n "${f}" 2>/dev/null || broken=$((broken + 1))
done
((broken == 0)) || RED+=("home-bin-syntax-${broken}")

# Detect-only bootstrap prints one line per actionable problem. The prefixes
# below mean a tool is missing or below its floor, or the home's own config
# no longer loads - what an upgrade breaks. The rest (BOOTSTRAP_INFO, home
# summary, backlog reconcile notes) are ordinary operations, counted only.
if [[ -x "${FM_HOME_DIR}/bin/fm-bootstrap.sh" ]]; then
  rc=0
  boot="$(FM_HOME="${FM_HOME_DIR}" FM_BOOTSTRAP_DETECT_ONLY=1 FM_BOOTSTRAP_NETWORK=skip \
    run "${FM_HOME_DIR}/bin/fm-bootstrap.sh")" || rc=$?
  ((rc == 0)) || RED+=("bootstrap-exit-${rc}")
  failing="$(grep -oE '^(MISSING|MISSING_MANUAL|BACKEND_INVALID|CREW_DISPATCH|STARTUP_MEMORY_BUDGET|TANGLE):' <<<"${boot}" |
    tr -d : | sort -u | paste -sd, - || true)"
  [[ -z "${failing}" ]] || RED+=("bootstrap:${failing}")
  NOTES="$(grep -c . <<<"${boot}" || true)"
else
  RED+=("bootstrap-missing")
fi

summary || exit 1
