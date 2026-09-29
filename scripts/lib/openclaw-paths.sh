# shellcheck shell=bash
# scripts/lib/openclaw-paths.sh - which repo paths belong to the OpenClaw side
# of the deploy, and the gate that decides whether deploy.sh runs the OpenClaw
# phases (scripts/deploy-openclaw.sh) on this run.
#
# Sourced by scripts/deploy.sh, never executed. The one place the path list
# lives: openclaw_path_matches below.
#
# LIFEKIT_DEPLOY_OPENCLAW picks the mode:
#   always (default)  run every OpenClaw phase - a manual `bash scripts/deploy.sh`
#                     and CI's workflow_dispatch deploy the whole stack
#   auto              CI on a push to main: run them only when the range since
#                     the last successful deploy touched an OpenClaw path

# Refs under refs/lifekit/ live in the VPS clone's .git: no new host path.
OPENCLAW_LAST_DEPLOYED_REF="refs/lifekit/last-deployed"

# Compose services that are OpenClaw's: on a platform-only deploy `compose up`
# leaves them out, so the gateway image is neither built nor recreated.
# shellcheck disable=SC2034  # used by deploy.sh
OPENCLAW_SERVICES=(openclaw-gateway openclaw-cli lifekit-orchestrator)

# Exit 0 when the repo-relative path is OpenClaw's (or, for compose/
# docker-compose.yml, might be: the gateway's service definition lives in it
# and a change cannot be attributed to one service precisely, so any change to
# the file counts).
openclaw_path_matches() {
  case "$1" in
    compose/openclaw-gateway/* | compose/openclaw/* | defaults/* | skills/*) return 0 ;;
    platform.patch.json | */platform.patch.json) return 0 ;;
    scripts/deploy-openclaw.sh | scripts/memory-audit/*) return 0 ;;
    compose/docker-compose.yml) return 0 ;;
  esac
  return 1
}

# Sets OPENCLAW_GATE_RUN (1 run the phases, 0 skip them) and OPENCLAW_GATE_REASON.
# Runs in the repo (cwd), after deploy.sh has reset it to the ref being
# deployed. The range is <last successfully deployed commit>..HEAD, not the
# push's before..after: a deploy that was superseded, skipped or failed never
# advances the ref, so its OpenClaw change stays inside the next range. Any
# range that cannot be established runs the phases.
openclaw_gate_decide() {
  local mode="${LIFEKIT_DEPLOY_OPENCLAW:-always}" base head f matched=""
  OPENCLAW_GATE_RUN=1
  case "${mode}" in
    always) OPENCLAW_GATE_REASON="mode 'always' (manual run or workflow_dispatch)"; return 0 ;;
    auto) ;;
    *) OPENCLAW_GATE_REASON="unknown LIFEKIT_DEPLOY_OPENCLAW='${mode}', running everything"; return 0 ;;
  esac
  head="$(git rev-parse --verify -q 'HEAD^{commit}')" || { OPENCLAW_GATE_REASON="cannot resolve HEAD"; return 0; }
  base="$(git rev-parse --verify -q "${OPENCLAW_LAST_DEPLOYED_REF}^{commit}")" \
    || { OPENCLAW_GATE_REASON="no record of a previous successful deploy"; return 0; }
  if ! git merge-base --is-ancestor "${base}" "${head}" 2>/dev/null; then
    OPENCLAW_GATE_REASON="last deployed ${base:0:7} is not an ancestor of ${head:0:7} (force push?)"
    return 0
  fi
  local changed
  changed="$(git diff --name-only "${base}" "${head}")" \
    || { OPENCLAW_GATE_REASON="cannot diff ${base:0:7}..${head:0:7}"; return 0; }
  while IFS= read -r f; do
    [[ -n "${f}" ]] || continue
    if openclaw_path_matches "${f}"; then matched="${f}"; break; fi
  done <<<"${changed}"
  if [[ -n "${matched}" ]]; then
    OPENCLAW_GATE_REASON="${matched} changed since last deploy ${base:0:7}"
    return 0
  fi
  OPENCLAW_GATE_RUN=0
  OPENCLAW_GATE_REASON="no OpenClaw path changed since last deploy ${base:0:7}"
}
