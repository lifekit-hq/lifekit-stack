# shellcheck shell=bash
# scripts/lib/openclaw-gateway.sh - find the production OpenClaw gateway
# container (issue 716). Every host script that acts on the gateway resolves it
# here, never with its own `docker ps ... | head -n 1`: a second container
# carrying the same compose labels (a rehearsal, a throwaway measurement
# gateway) would otherwise be picked at random and acted on.
#
# Sourced, never executed. `openclaw_gateway_id` prints the id of the one
# running container that has all of:
#   com.docker.compose.project=openclaw
#   com.docker.compose.service=openclaw-gateway
#   com.docker.compose.oneoff=False            (not a `compose run` one-off)
#   com.docker.compose.project.working_dir=$OPENCLAW_GATEWAY_WORKDIR
# OPENCLAW_GATEWAY_WORKDIR defaults to the deploy checkout's compose dir,
# /srv/lifekit-stack/compose/openclaw, so a gateway started from any other dir
# is not the production one.
#
# Returns 0 and prints the id, or prints nothing when no such container runs
# (each caller decides what "not running" means for it). Returns 1 with a
# message on stderr when docker ps fails or when more than one container
# matches: the caller stops, it never picks one. Uses $DOCKER (default docker).
# openclaw_gateway_id_lenient is the same, except that a docker ps failure reads
# as "no gateway" (for the scripts that skip their gateway step when docker is
# unreachable); more than one match still fails.
#
#   GATEWAY="$(openclaw_gateway_id)" || exit 1
#   [ -n "$GATEWAY" ] || ...not running...

OPENCLAW_GATEWAY_WORKDIR="${OPENCLAW_GATEWAY_WORKDIR:-/srv/lifekit-stack/compose/openclaw}"

openclaw_gateway_filters() {
  printf '%s\n' \
    --filter label=com.docker.compose.project=openclaw \
    --filter label=com.docker.compose.service=openclaw-gateway \
    --filter label=com.docker.compose.oneoff=False \
    --filter "label=com.docker.compose.project.working_dir=${OPENCLAW_GATEWAY_WORKDIR}"
}

openclaw_gateway_id() { openclaw_gateway_resolve strict; }
openclaw_gateway_id_lenient() { openclaw_gateway_resolve lenient; }

openclaw_gateway_resolve() { # $1 = strict | lenient
  local docker="${DOCKER:-docker}" mode="$1" ids names n
  local -a filters
  mapfile -t filters < <(openclaw_gateway_filters)
  if [ "$mode" = lenient ]; then
    ids="$("$docker" ps -q "${filters[@]}" 2>/dev/null)" || return 0
  elif ! ids="$("$docker" ps -q "${filters[@]}")"; then
    echo "openclaw-gateway: docker ps failed; cannot find the gateway container" >&2
    return 1
  fi
  n="$(grep -c . <<<"$ids" || true)"
  if [ "$n" -gt 1 ]; then
    names="$("$docker" ps "${filters[@]}" --format '{{.Names}}' 2>/dev/null | paste -sd ' ' || true)"
    echo "openclaw-gateway: refusing to act: $n running containers match the production gateway" \
      "(compose project openclaw, service openclaw-gateway, working dir ${OPENCLAW_GATEWAY_WORKDIR}):" \
      "${names:-$(paste -sd ' ' <<<"$ids")}. Stop the extra one (a rehearsal or throwaway gateway) and re-run." >&2
    return 1
  fi
  [ -z "$ids" ] || printf '%s\n' "$ids"
}
