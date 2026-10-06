#!/usr/bin/env bash
# Rehearse an OpenClaw bump against a copy of the live state, without touching it.
# Replaces the manual recipe in docs/runbook.md. Run by hand as the account that
# owns the state dir (the compose user); nothing calls it automatically.
#
# Privacy: the doctor logs stay on this machine. The printed summary carries
# key paths, counts and finding ids only - never a config value.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: rehearse-openclaw-bump.sh [options]

Rehearse `doctor --fix` for an OpenClaw image on a copy of the live state.

  --image <tag>       Image to rehearse (default: lifekit-openclaw:rehearse).
                      The default tag is built from compose/openclaw-gateway with
                      plain `docker build` (never compose) and removed on exit;
                      any other tag is used as-is and left alone.
  --state <dir>       Live OpenClaw config dir   (default: /srv/openclaw/config)
  --workspace <dir>   Live OpenClaw workspace    (default: /srv/openclaw/workspace)
  --dest <dir>        Where the copy and summary go
                      (default: ~/rehearsal/<timestamp>-<pid>, 0700)
  --lint-only         Fast pass: `doctor --json` against the live state mounted
                      read-only. No copy, no migration.
  --keep              Keep the copy even on a green run.
  --baseline <image>  Re-run the lint pass on a fresh copy with a second image
                      (triage mode; not implemented yet - exits 2).
  -h, --help          Show this help.

Environment: ENV_FILE (live env file, only its keys are read), REHEARSAL_ROOT
(default ~/rehearsal), DOCKER_TIMEOUT (seconds per docker run, default 1800).

A red run keeps the copy and names its path in the summary; an aborted run
removes it. The rehearsal image tag is removed on every exit path. Copies older
than 7 days under the default root are pruned at the start of a full run.
USAGE
}

REHEARSE_TAG="lifekit-openclaw:rehearse"
IMAGE="${REHEARSE_TAG}"
STATE="/srv/openclaw/config"
WORKSPACE="/srv/openclaw/workspace"
SECRET_DIR="${OPENCLAW_AUTH_PROFILE_SECRET_DIR:-/srv/openclaw/secret-key}"
ENV_FILE="${ENV_FILE:-/srv/lifekit-secrets/stack.env}"
ROOT="${REHEARSAL_ROOT:-${HOME}/rehearsal}"
DEST=""
KEEP=0
LINT_ONLY=0
BASELINE=""
HEADROOM_KB=$((25 * 1024 * 1024))   # copy + one image + slack
DOCKER_TIMEOUT="${DOCKER_TIMEOUT:-1800}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Not copied: browser/tools/logs are rebuildable, workspace and wiki are mounted
# separately, backups and *.migrated.* are large with no migration value.
# agents/ is deliberately included - the SQLite migrations happen there.
# tmp/ and the cache dirs are the running gateway's scratch (plugin-build
# captures with whole node_modules trees, created and deleted continuously):
# nothing in them is state, copying them takes minutes, and files vanish
# mid-copy. Any new scratch dir the gateway keeps under config goes here.
EXCLUDES=('/browser/' '/tools/' '/logs/' '/workspace/' '/wiki/' '/backups/'
  '/tmp/' '/.tmp/' '/cache/' '/.cache/'
  '/openclaw-config-*.tar.gz' '*.migrated.*')

while (($#)); do
  case "$1" in
    --image) IMAGE="${2:?--image needs a value}"; shift 2 ;;
    --state) STATE="${2:?--state needs a value}"; shift 2 ;;
    --workspace) WORKSPACE="${2:?--workspace needs a value}"; shift 2 ;;
    --dest) DEST="${2:?--dest needs a value}"; shift 2 ;;
    --baseline) BASELINE="${2:?--baseline needs a value}"; shift 2 ;;
    --lint-only) LINT_ONLY=1; shift ;;
    --keep) KEEP=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "${BASELINE}" ]]; then
  echo "--baseline is not implemented yet" >&2
  exit 2
fi

DEFAULT_DEST=0
if [[ -z "${DEST}" ]]; then
  DEST="${ROOT}/$(date -u +%Y%m%dT%H%M%SZ)-$$"
  DEFAULT_DEST=1
fi

say() { printf '==> %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

RED=()            # reasons the verdict is red
BUILT=0
DEST_MADE=0
START="$(date +%s)"
COPIED_KB=0
P_REPINNED=0 P_FALLBACK=0 P_OFFCORE=0
FIX_RC="-" SECOND_RC="-" SQLITE_RC="-" LINT_RC="-"
TOLERATED_COUNTS="-"
UNTOLERATED=""
CORE_VER="unknown"

cleanup() {
  local rc=$?
  trap - EXIT
  if ((BUILT)); then
    docker rmi "${REHEARSE_TAG}" >/dev/null 2>&1 || true
  fi
  if ((DEST_MADE && !KEEP)); then
    # Only a red verdict keeps the copy; green runs and aborts remove it.
    if ((${#RED[@]} == 0)) && ! rm -rf "${DEST}"; then
      say "warning: could not fully remove ${DEST}; the verdict stands"
    fi
  fi
  exit "${rc}"
}
trap cleanup EXIT

run_timeout() { timeout "${DOCKER_TIMEOUT}" "$@"; }

# Nearest existing ancestor, for df on a destination that is not created yet.
existing_ancestor() {
  local p="$1"
  while [[ ! -d "${p}" && "${p}" != "/" ]]; do p="$(dirname "${p}")"; done
  printf '%s' "${p}"
}

sources_kb() {
  local args=() e
  # du matches basenames and a trailing slash defeats the match, so strip both
  # (rsync needs them); an excluded name nested deeper is skipped here too,
  # which only under-counts a little against the 25 GiB headroom.
  for e in "${EXCLUDES[@]}"; do e="${e#/}"; args+=("--exclude=${e%/}"); done
  du -sxk "${args[@]}" "$1" | awk '{print $1}'
}

check_headroom() {
  local avail need kb ws
  kb="$(sources_kb "${STATE}")"
  ws="$(du -sxk "${WORKSPACE}" | awk '{print $1}')"
  COPIED_KB=$((kb + ws))
  avail="$(df --output=avail -k "$(existing_ancestor "${DEST}")" | tail -n1 | tr -d ' ')"
  need=$((COPIED_KB + HEADROOM_KB))
  say "copying $((COPIED_KB / 1024)) MiB; free $((avail / 1024)) MiB; need $((need / 1024)) MiB"
  if ((avail < need)); then
    die "not enough headroom: free $((avail / 1024)) MiB, need $((need / 1024)) MiB (copy + 25 GiB)"
  fi
}

# rsync from a live tree: exit 24 means files vanished between listing and
# reading (the gateway deleted them). The rest of the copy is complete, so it
# is a warning; any other non-zero exit still fails the run.
rsync_live() {
  local rc=0
  rsync "$@" || rc=$?
  if ((rc == 24)); then
    say "warning: some source files vanished during the copy (rsync 24); continuing"
    return 0
  fi
  return "${rc}"
}

copy_state() {
  local e ex=()
  for e in "${EXCLUDES[@]}"; do ex+=("--exclude=${e}"); done
  rm -rf "${DEST}/config" "${DEST}/workspace" "${DEST}/secret-key"
  rsync_live -a "${ex[@]}" "${STATE}/" "${DEST}/config/"
  rsync_live -a "${WORKSPACE}/" "${DEST}/workspace/"
  # oc() bind-mounts into the copy at these excluded paths; a mount target
  # dockerd has to create is root-owned, and a green run's cleanup then
  # cannot remove it.
  mkdir -p "${DEST}/config/workspace" "${DEST}/config/wiki/main"
  if [[ -d "${SECRET_DIR}" ]]; then
    rsync_live -a "${SECRET_DIR}/" "${DEST}/secret-key/"
  else
    mkdir -p "${DEST}/secret-key"
  fi
  local stray
  stray="$(find "${DEST}/config" "${DEST}/workspace" "${DEST}/secret-key" ! -uid "$(id -u)" -print -quit)"
  [[ -z "${stray}" ]] || die "copy holds files not owned by uid $(id -u); fix the source ownership (chown -R to the runner account) and re-run"
}

# Keys only, every value a placeholder: ${VAR} refs resolve to something
# non-empty and no live token reaches the container. Two key sources: the env
# file, and the gateway service's own compose `environment:` entries whose raw
# value is a ${...} reference - compose derives some of those from other
# variables (OPENCLAW_FINANCE_CHAT falls back to LIFEKIT_TELEGRAM_CHAT), so the
# env file alone misses them. --no-interpolate keeps compose from reading any
# value; literal entries (HOME, PATH, the dirs below) and TZ, which no
# placeholder would be a valid zone for, are left to the image defaults.
compose_env_keys() {
  # shellcheck disable=SC2016 # "${" is Python source, not a shell expansion
  docker compose -f "${REPO}/compose/openclaw/docker-compose.yml" config --no-interpolate --format json 2>/dev/null |
    python3 -c '
import json, sys
try:
    env = json.load(sys.stdin)["services"]["openclaw-gateway"].get("environment") or {}
except Exception:
    sys.exit(1)
for k, v in sorted(env.items()):
    if k != "TZ" and "${" in str(v or ""):
        print(k)
' || say "warning: could not read openclaw-gateway environment keys from compose; rehearsal env has env-file keys only"
}

write_env() {
  local out="${DEST}/env.rehearsal"
  (umask 077
    {
      echo "OPENCLAW_STATE_DIR=/home/node/.openclaw"
      echo "OPENCLAW_CONFIG_PATH=/home/node/.openclaw/openclaw.json"
      echo "OPENCLAW_CONFIG_DIR=/home/node/.openclaw"
      echo "OPENCLAW_WORKSPACE_DIR=/home/node/.openclaw/workspace"
      {
        if [[ -r "${ENV_FILE}" ]]; then
          grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "${ENV_FILE}" | cut -d= -f1
        fi
        compose_env_keys
      } | sort -u | sed 's/$/=rehearsal-placeholder/'
    } | awk -F= '!seen[$1]++' >"${out}")
}

# oc <config-dir> <workspace-dir> <secret-dir> <mount-suffix> -- openclaw args
oc() {
  local cfg="$1" ws="$2" sec="$3" sfx="$4"
  shift 5
  run_timeout docker run --rm --memory 2g --env-file "${DEST}/env.rehearsal" \
    -v "${cfg}:/home/node/.openclaw${sfx}" \
    -v "${ws}:/home/node/.openclaw/workspace${sfx}" \
    -v "${sec}:/home/node/.config/openclaw${sfx}" \
    -v "${DEST}/vault:/home/node/memory:ro" \
    -v "${DEST}/vault:/home/node/.openclaw/wiki/main:ro" \
    --entrypoint openclaw "${IMAGE}" "$@"
}
oc_copy() { oc "${DEST}/config" "${DEST}/workspace" "${DEST}/secret-key" "" -- "$@"; }

# Structured classification of `doctor --json`: matches on the finding's
# `checkId` plus a structured field (`path`, `requirement`), never on free
# text - with one anchored message prefix, below, for a finding that carries
# nothing else. An unrecognised finding is red.
classify() {
  python3 - "$1" <<'PY'
import json, re, sys
# Tolerated classes, each with its reason: (checkId, {field: anchored regex}).
TOLERATED = {
    # The rehearsal container is not on the compose network, so MCP servers
    # cannot be resolved by service name and expose no tools to validate.
    "mcp_resolution": ("core/doctor/runtime-tool-schemas",
                       {"path": r"mcp\.servers\.", "requirement": r"getaddrinfo ENOTFOUND "}),
    # The rehearsal mounts no Claude CLI credentials (the gateway gets them
    # from the lifekit account's ~/.claude bind, which is not copied).
    "claude_auth": ("core/doctor/claude-cli", {}),
    # By design: compose starts the gateway with `--bind lan` because a
    # container must listen on 0.0.0.0 for a published port, and publishes it
    # on the host's loopback only. The finding has no path or requirement,
    # hence the anchored message prefix.
    "gateway_bind": ("core/doctor/security", {"message": r'WARNING: Gateway bound to "lan" \(0\.0\.0\.0\)'}),
}
OK = {"info", "ok", "pass", "passed", "debug"}
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print("PARSE_ERROR")
    sys.exit(0)
items = data if isinstance(data, list) else next(
    (data[k] for k in ("findings", "issues", "checks", "results") if isinstance(data.get(k), list)), [])
counts = {k: 0 for k in TOLERATED}
bad = []
for f in items:
    if not isinstance(f, dict):
        continue
    sev = str(f.get("severity") or f.get("level") or "").lower()
    if sev in OK:
        continue
    fid = str(f.get("checkId") or f.get("id") or f.get("check") or f.get("code") or "unknown")
    for name, (check, fields) in TOLERATED.items():
        if fid == check and all(re.match(rx, str(f.get(k) or "")) for k, rx in fields.items()):
            counts[name] += 1
            break
    else:
        bad.append(re.sub(r"[^A-Za-z0-9_.:/-]", "?", fid)[:64])
print(" ".join(f"{k}={v}" for k, v in counts.items()), ",".join(sorted(set(bad))) or "-")
PY
}

# Changed key paths between two config snapshots. Values never leave python.
changed_paths() {
  python3 - "$1" "$2" <<'PY'
import json, re, sys
def flat(o, p=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from flat(v, f"{p}.{k}" if p else str(k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from flat(v, f"{p}[{i}]")
    else:
        yield p, json.dumps(o, sort_keys=True)
def load(path):
    try:
        return dict(flat(json.load(open(path))))
    except Exception:
        return {}
a, b = load(sys.argv[1]), load(sys.argv[2])
paths = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
def scrub(p):
    return re.sub(r"(?<=[.\[])-?\d{5,}(?=[.\]]|$)", "<id>", p)
print(len(paths))
for p in paths[:200]:
    print(scrub(p))
PY
}

plugin_mismatches() {
  oc_copy plugins list --json 2>/dev/null | python3 -c '
import json, re, sys
core = sys.argv[1]
items = json.load(sys.stdin)
items = items if isinstance(items, list) else items.get("plugins", [])
for p in items:
    if not p.get("enabled"): continue
    v = str(p.get("version") or "")
    if p.get("origin") not in ("bundled", "global") or not re.match(r"^\d{4}\.\d+\.\d+", v): continue
    if v != core: print(p["id"], v)
' "${CORE_VER}" || true
}

write_summary() {
  local verdict="GREEN" f="${DEST}/summary.md" changes="" n=0
  ((${#RED[@]})) && verdict="RED"
  if [[ -f "${DEST}/config.before.json" && -f "${DEST}/config/openclaw.json" ]]; then
    changes="$(changed_paths "${DEST}/config.before.json" "${DEST}/config/openclaw.json")"
    n="$(head -n1 <<<"${changes}")"
    changes="$(tail -n +2 <<<"${changes}")"
  fi
  {
    echo "## OpenClaw bump rehearsal: ${verdict}"
    echo
    echo "- image core version: ${CORE_VER}"
    echo "- exit codes: fix=${FIX_RC} second-fix=${SECOND_RC} session-sqlite=${SQLITE_RC} lint=${LINT_RC}"
    echo "- tolerated findings: ${TOLERATED_COUNTS}"
    echo "- untolerated finding ids: ${UNTOLERATED:--}"
    echo "- plugins: re-pinned by doctor=${P_REPINNED} needed update fallback=${P_FALLBACK} still off core=${P_OFFCORE}"
    echo "- copied: $((COPIED_KB / 1024)) MiB; elapsed: $(($(date +%s) - START)) s"
    if ((${#RED[@]})); then
      echo "- red because: ${RED[*]}"
      echo "- copy kept at: ${DEST} (full logs on this machine only)"
    fi
    echo "- changes: ${n}"
    [[ -z "${changes}" ]] || sed "s/^/  - \`/; s/\$/\`/" <<<"${changes}"
  } >"${f}"
  if (($(wc -c <"${f}") > 65536)); then
    head -c 65000 "${f}" >"${f}.cut"
    printf '\n\n(truncated; full log on the box at %s)\n' "${DEST}" >>"${f}.cut"
    mv "${f}.cut" "${f}"
  fi
  cat "${f}"
}

run_lint() {
  local sfx="$1" cfg="$2" ws="$3" sec="$4" out
  LINT_RC=0
  oc "${cfg}" "${ws}" "${sec}" "${sfx}" -- doctor --json >"${DEST}/lint.json" 2>"${DEST}/lint.err" || LINT_RC=$?
  out="$(classify "${DEST}/lint.json")"
  if [[ "${out}" == PARSE_ERROR ]]; then
    RED+=("lint-unparseable")
    return
  fi
  # "<class>=<n> ... <untolerated ids or ->": the last word is the id list.
  TOLERATED_COUNTS="${out% *}"
  UNTOLERATED="${out##* }"
  [[ "${UNTOLERATED}" == "-" ]] || RED+=("lint-findings")
}

# ---------------------------------------------------------------- main
if ((!LINT_ONLY && DEFAULT_DEST)) && [[ -d "${ROOT}" ]]; then
  find "${ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true
fi

if ((!LINT_ONLY)); then
  say "checking headroom"
  check_headroom
fi

mkdir -p "${DEST}"
chmod 700 "${DEST}"
DEST_MADE=1
mkdir -p "${DEST}/vault"
write_env

DEPLOY_DOCKER_CONFIG="$(mktemp -d)"
export DOCKER_CONFIG="${DEPLOY_DOCKER_CONFIG}"

if [[ "${IMAGE}" == "${REHEARSE_TAG}" ]]; then
  say "docker build ${REHEARSE_TAG}"
  BUILT=1
  run_timeout docker build -t "${REHEARSE_TAG}" -f "${REPO}/compose/openclaw-gateway/Dockerfile" "${REPO}/compose/openclaw-gateway"
fi

IMG_UID="$(run_timeout docker run --rm --entrypoint id "${IMAGE}" -u)"
[[ "${IMG_UID}" == "$(id -u)" ]] || die "image runs as uid ${IMG_UID}, this account is $(id -u); the copy would end up with foreign ownership"
CORE_VER="$(run_timeout docker run --rm --entrypoint openclaw "${IMAGE}" --version 2>/dev/null | awk '{print $2}' || true)"
[[ -n "${CORE_VER}" ]] || CORE_VER="unknown"

if ((LINT_ONLY)); then
  say "lint-only: doctor --json against live state (read-only)"
  run_lint ":ro" "${STATE}" "${WORKSPACE}" "${SECRET_DIR}"
  write_summary
  ((${#RED[@]} == 0)) || exit 1
  exit 0
fi

say "copying state"
copy_state
cp "${DEST}/config/openclaw.json" "${DEST}/config.before.json" 2>/dev/null || true

say "doctor --fix --non-interactive"
FIX_RC=0
oc_copy doctor --fix --non-interactive >"${DEST}/doctor-fix.log" 2>&1 || FIX_RC=$?
if ((FIX_RC != 0)) && grep -qiE 'malformed|database is locked|SQLITE_BUSY' "${DEST}/doctor-fix.log"; then
  # The copy came from a running gateway; one re-copy for a torn snapshot.
  say "torn SQLite snapshot suspected; re-copying once"
  copy_state
  FIX_RC=0
  oc_copy doctor --fix --non-interactive >"${DEST}/doctor-fix.log" 2>&1 || FIX_RC=$?
fi

if ((FIX_RC != 0)); then
  RED+=("fix-pass-exit-${FIX_RC}")
else
  say "second pass must be a no-op"
  before="$(sha256sum "${DEST}/config/openclaw.json" 2>/dev/null | awk '{print $1}')"
  SECOND_RC=0
  oc_copy doctor --fix --non-interactive >"${DEST}/doctor-fix2.log" 2>&1 || SECOND_RC=$?
  after="$(sha256sum "${DEST}/config/openclaw.json" 2>/dev/null | awk '{print $1}')"
  { ((SECOND_RC == 0)) && [[ "${before}" == "${after}" ]]; } || RED+=("second-pass-not-noop")

  say "doctor --session-sqlite dry-run"
  SQLITE_RC=0
  oc_copy doctor --session-sqlite dry-run >"${DEST}/doctor-sqlite.log" 2>&1 || SQLITE_RC=$?
  ((SQLITE_RC == 0)) || RED+=("session-sqlite-exit-${SQLITE_RC}")

  say "doctor --json lint on the migrated copy"
  run_lint "" "${DEST}/config" "${DEST}/workspace" "${DEST}/secret-key"

  say "asserting official plugins match core ${CORE_VER}"
  MISMATCH="$(plugin_mismatches)"
  if [[ -n "${MISMATCH}" ]]; then
    P_FALLBACK="$(grep -c . <<<"${MISMATCH}")"
    while read -r pid _; do
      [[ -z "${pid}" ]] && continue
      oc_copy plugins update "@openclaw/${pid}@latest" >>"${DEST}/doctor-fix.log" 2>&1 || true
    done <<<"${MISMATCH}"
    MISMATCH="$(plugin_mismatches)"
    if [[ -n "${MISMATCH}" ]]; then
      P_OFFCORE="$(grep -c . <<<"${MISMATCH}")"
      RED+=("plugins-off-core")
    fi
  fi
fi

write_summary
((${#RED[@]} == 0)) || exit 1
