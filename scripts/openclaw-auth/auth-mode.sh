#!/usr/bin/env bash
# auth-mode.sh - the one switch between OpenClaw's two ways of reaching Claude.
#
#   api           anthropic/* models run on OpenClaw's direct runtime (agentRuntime
#                 "openclaw") with the anthropic:api-key profile, a SecretRef to
#                 ANTHROPIC_API_KEY (prepaid Console credit)
#   subscription  anthropic/* models run on the claude-cli runtime: OpenClaw
#                 launches the unmodified Claude Code binary with its native login.
#                 The setup-token profile is NEVER the fallback: OpenClaw's own
#                 client must not present a subscription OAuth token (Anthropic's
#                 terms), so the fallback is this runtime switch, not a second profile.
#
# Usage:
#   auth-mode.sh status [--json] [--quick]   mode, runtimes, auth.order, per-agent
#                                            key profile, live key probe, spend
#                                            estimate (--quick: no probe, no spend)
#   auth-mode.sh api [--force] [--dry-run]   switch to api mode
#   auth-mode.sh subscription [--reason T] [--dry-run]
#                                            switch to subscription mode
#                                            (--dry-run: print and validate the
#                                            config patch, change nothing)
#   auth-mode.sh mode                        just the mode word (one gateway call)
#   auth-mode.sh probe                       live 1-token key probe, one JSON line
#   auth-mode.sh ensure-profiles             add the SecretRef key profile to every
#                                            agent that lacks it (api does this itself)
#   auth-mode.sh offline-profiles            HOST OPERATOR ONLY, never an agent: the
#                                            same, with the gateway STOPPED (OpenClaw
#                                            2026.11.4 state ownership); it stops
#                                            the gateway and always starts it again;
#                                            run as lifekit or root (reads openclaw.json)
#
# A switch is one `openclaw config patch` inside the gateway; the gateway is
# restarted only when the apply hint says the changed keys need it. It is then
# verified with a real throwaway turn per agent in AUTH_CHECK_AGENTS (default
# "kit"; fresh session key agent:<id>:auth-check-<time>) that must be answered by the new
# mode's provider, and rolled back automatically when it is not. Idempotent. No
# key value is ever read or printed; the probe and the SecretRef resolve inside
# the gateway. Runbook: docs/runbook.md "OpenClaw auth mode".
#
# Env: DOCKER (docker), AUTH_CHECK_AGENTS (kit), AUTH_MODE_HEALTH_WAIT (300 s),
#      AUTH_MODE_SLEEP (5 s poll), SPEND_GUARD (path of spend_guard.py).
# Exit: 0 done, 1 failed or rolled back, 2 cannot run or refused.
set -euo pipefail

DOCKER="${DOCKER:-docker}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPEND_GUARD="${SPEND_GUARD:-$HERE/spend_guard.py}"
CHECK_AGENTS="${AUTH_CHECK_AGENTS:-kit}"
HEALTH_WAIT="${AUTH_MODE_HEALTH_WAIT:-300}"
POLL="${AUTH_MODE_SLEEP:-5}"
PROFILE="anthropic:api-key"
KEY_REF="anthropic-api-key"

say() { echo "auth-mode: $*" >&2; }
die() {
  say "$*"
  exit 2
}

cmd="${1:-}"
shift || true
json=0 quick=0 force=0 dry=0 reason=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) json=1 ;;
    --quick) quick=1 ;;
    --force) force=1 ;;
    --dry-run) dry=1 ;;
    --reason)
      reason="${2:?--reason needs text}"
      shift
      ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

cid="$("$DOCKER" ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)" ||
  die "docker ps failed"
[[ -n "$cid" ]] || die "no running openclaw-gateway container"

oc() { "$DOCKER" exec -i "$cid" openclaw "$@"; }
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# ── live state ────────────────────────────────────────────────────────────────

# Every openclaw CLI call costs ~20 s (CLI start-up against a busy gateway), so
# independent reads run side by side.
#
# live_config -> the agents + auth halves of the live config, as one JSON object
live_config() {
  local pa pb
  oc config get agents > "$work/agents.json" 2> /dev/null &
  pa=$!
  oc config get auth > "$work/auth.json" 2> /dev/null &
  pb=$!
  wait "$pa" || die "cannot read the live agents config through the gateway"
  wait "$pb" || die "cannot read the live auth config through the gateway"
  jq -n -c --slurpfile agents "$work/agents.json" --slurpfile auth "$work/auth.json" \
    'if ($agents[0] | type) == "object" and ($auth[0] | type) == "object"
     then {agents: $agents[0], auth: $auth[0]} else error("not objects") end' ||
    die "the live config read through the gateway is not valid"
}

# api | subscription | mixed | unknown, from the runtime of every anthropic/* model entry
mode_of() {
  jq -r '
    [ (.agents.defaults.models // {}), ((.agents.entries // {}) | to_entries[] | .value.models // {}) ]
    | map(to_entries[] | select(.key | startswith("anthropic/")) | (.value.agentRuntime.id // "default"))
    | if length == 0 then "unknown"
      elif all(. == "openclaw") then "api"
      elif all(. == "claude-cli") then "subscription"
      else "mixed" end' <<< "$1"
}

agent_ids() { jq -r '.agents.entries // {} | keys[]' <<< "$1"; }

# key_states LIVE -> JSON array, per agent {agent, profile: bool, disabled: [reason]}
key_states() {
  local a pid
  local -a pids=()
  for a in $(agent_ids "$1"); do
    oc models status --agent "$a" --json 2> /dev/null | jq -ce --arg p "$PROFILE" --arg a "$a" '
      {agent: $a,
       profile: ([.auth.oauth.profiles[]? | select(.profileId == $p)] | length > 0),
       disabled: [.auth.unusableProfiles[]? | select(.profileId == $p) | (.reason // .classification // "unusable")]}' \
      > "$work/key-$a.json" &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    wait "$pid" || {
      say "cannot read an agent's key state through the gateway"
      return 1
    }
  done
  if ((${#pids[@]} == 0)); then
    echo '[]'
    return 0
  fi
  jq -s -c '.' "$work"/key-*.json
  rm -f "$work"/key-*.json
}

probe_key() {
  "$DOCKER" exec -i "$cid" python3 - < "$HERE/key-probe.py" 2>/dev/null ||
    echo '{"result":"unknown","http":null,"detail":"probe could not run"}'
}

spend_json() {
  python3 "$SPEND_GUARD" report --json 2>/dev/null || echo 'null'
}

build_state() {
  local live agents_state
  live="$(live_config)" || exit 2
  agents_state="$(key_states "$live")" || exit 2
  jq -c -n --argjson live "$live" --arg mode "$(mode_of "$live")" --argjson agents "$agents_state" '
    {mode: $mode,
     runtimes: ([ ($live.agents.defaults.models // {}), (($live.agents.entries // {}) | to_entries[] | .value.models // {}) ]
       | map(to_entries[] | select(.key | startswith("anthropic/")) | (.value.agentRuntime.id // "default"))
       | group_by(.) | map({key: .[0], value: length}) | from_entries),
     authOrder: ($live.auth.order.anthropic // []),
     agents: $agents,
     billingDisabled: ([$agents[].disabled[]? | select(test("billing"; "i"))] | length > 0)}'
}

cmd_status() {
  local state probe="null" spend="null"
  if ((!quick)); then
    probe_key > "$work/probe.json" &
    spend_json > "$work/spend.json" &
  fi
  state="$(build_state)"
  if ((!quick)); then
    wait
    probe="$(cat "$work/probe.json")"
    spend="$(cat "$work/spend.json")"
  fi
  state="$(jq -c --argjson probe "$probe" --argjson spend "$spend" '. + {probe: $probe, spend: $spend}' <<< "$state")"
  if ((json)); then
    echo "$state"
    return
  fi
  jq -r '
    "mode: \(.mode)",
    "anthropic model runtimes: " + ([.runtimes | to_entries[] | "\(.key) x\(.value)"] | join(", ")),
    "auth.order.anthropic: " + (if (.authOrder | length) == 0 then "(empty)" else (.authOrder | join(", ")) end),
    (.agents[] | "agent \(.agent): key profile \(if .profile then "present" else "missing" end)" +
       (if (.disabled | length) > 0 then ", DISABLED (\(.disabled | join(", ")))" else "" end)),
    (if .probe then "key probe: \(.probe.result)" + (if .probe.http then " (http \(.probe.http))" else "" end) else empty end),
    (if .spend then "spend: $\(.spend.since_topup_usd) of $\(.spend.credit_usd) since \(.spend.since) (\(.spend.used_percent)%), this month $\(.spend.month_usd); guard falls back at \(.spend.fallback_percent)%" else empty end)
  ' <<< "$state"
}

# ── switching ─────────────────────────────────────────────────────────────────

# patch_for api|subscription LIVE -> the config patch that puts every anthropic/*
# model the agents use on the target runtime and sets auth.order
patch_for() {
  local runtime=claude-cli order='[]' profiles='{}'
  if [[ "$1" == api ]]; then
    runtime=openclaw
    order="[\"$PROFILE\"]"
    profiles="{\"$PROFILE\": {\"provider\": \"anthropic\", \"mode\": \"api_key\"}}"
  fi
  jq -c --arg rt "$runtime" --argjson order "$order" --argjson profiles "$profiles" '
    def refs(m): if m == null then [] elif (m | type) == "string" then [m]
                 else ([m.primary] + (m.fallbacks // [])) end | map(select(. != null));
    def anth: select(startswith("anthropic/"));
    def rt: {agentRuntime: {id: $rt}};
    (.agents.defaults // {}) as $d | (.agents.entries // {}) as $e
    | ([refs($d.model)[], ($d.models // {} | keys[]), ($d.heartbeat.model // empty),
        ($e | to_entries[] | .value | refs(.model)[], (.heartbeat.model // empty))] | map(anth) | unique) as $used
    | {agents: {
         defaults: {models: ($used | map({key: ., value: rt}) | from_entries)},
         entries: ($e | to_entries
           | map(select(.value.models != null)
                 | {key: .key, value: {models: (([(.value.models | keys[])] + refs(.value.model)) | map(anth) | unique
                                                | map({key: ., value: rt}) | from_entries)}})
           | from_entries)},
       auth: ({order: {anthropic: $order}} + (if ($profiles | length) > 0 then {profiles: $profiles} else {} end))}
    | del(.agents.entries | select(. == {}))' <<< "$2"
}

# Leaf paths of a patch: scalars and arrays (an array replaces whole), never objects.
LEAVES='def lp: path(def r: if type == "object" then .[] | r else . end; r);'

# rollback_for PATCH LIVE -> the patch that undoes PATCH. A leaf whose path exists
# live is set back to its exact live value; where the live config lacks a key (or a
# whole object, e.g. the api-key profile) the rollback deletes the shallowest missing
# ancestor with null, because leaf-by-leaf nulls would leave an empty or partial
# object that fails schema validation.
rollback_for() {
  jq -c --argjson live "$2" "$LEAVES"'
    [lp as $p
     | ([range(1; ($p | length) + 1) as $n | $p[:$n] | . as $q | select(($live | try getpath($q) catch null) == null)] | first // $p)]
    | unique as $t
    | reduce ($t[] | . as $x | select([$t[] | select(length < ($x | length) and $x[:length] == .)] | length == 0)) as $q
        ({}; setpath($q; $live | try getpath($q) catch null))' <<< "$1"
}

# pending_paths PATCH LIVE -> dotted path of every leaf whose live value differs
pending_paths() {
  jq -r --argjson live "$2" "$LEAVES"'
    . as $patch | [lp as $p | select(($live | getpath($p)) != ($patch | getpath($p))) | $p | map(tostring) | join(".")] | .[]' <<< "$1"
}

wait_healthy() {
  local waited=0 status
  while ((waited <= HEALTH_WAIT)); do
    status="$("$DOCKER" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    case "$status" in healthy | running) return 0 ;; esac
    sleep "$POLL"
    waited=$((waited + POLL + 1))
  done
  return 1
}

# apply_patch FILE -> 0 applied (restarting the gateway only when asked to)
apply_patch() {
  local log="$work/apply.log"
  oc config patch --stdin --dry-run < "$1" > "$log" 2>&1 || {
    say "config patch dry run rejected the change:"
    sed 's/^/  /' "$log" >&2
    return 1
  }
  oc config patch --stdin < "$1" > "$log" 2>&1 || {
    say "config patch failed:"
    sed 's/^/  /' "$log" >&2
    return 1
  }
  if grep -q 'Restart the gateway to apply' "$log"; then
    say "applied keys need a restart; restarting openclaw-gateway"
    "$DOCKER" restart "$cid" > /dev/null
    wait_healthy || {
      say "gateway not healthy ${HEALTH_WAIT}s after the restart"
      return 1
    }
  else
    say "applied; the running gateway hot-reloads these keys, no restart"
  fi
}

# verify_turns EXPECTED_PROVIDER -> every check agent answers a real turn on it
verify_turns() {
  local a out status provider stamp
  # A fresh session per run keeps the check out of the agents' real sessions
  # (as in the deploy smoke). It does not avoid "cli session history refused
  # across auth boundary: reason=auth-unknown": that fires on every claude-cli
  # turn in subscription mode and is expected (docs/runbook.md).
  # --thinking low, not off: claude-opus-5-5 on the direct runtime rejects "off",
  # and low is accepted by every model in both modes.
  stamp="$(date -u +%Y%m%dT%H%M%S)"
  for a in ${CHECK_AGENTS//,/ }; do
    out="$(oc agent --agent "$a" --session-key "agent:$a:auth-check-$stamp" --message "Reply OK" \
      --thinking low --json --timeout 180 2> /dev/null || true)"
    status="$(jq -r '.status // "none"' <<< "$out" 2> /dev/null || echo none)"
    provider="$(jq -r '.result.meta.executionTrace.winnerProvider // "none"' <<< "$out" 2> /dev/null || echo none)"
    if [[ "$status" != ok || "$provider" != "$1" ]]; then
      say "verification turn on $a: status=$status provider=$provider (wanted ok on $1)"
      return 1
    fi
    say "verification turn on $a: ok on $provider"
  done
}

other_mode() { [[ "$1" == api ]] && echo subscription || echo api; }
provider_of() { [[ "$1" == api ]] && echo anthropic || echo claude-cli; }

# build_plan -> writes $work/plan.json for every agent lacking the key profile;
# returns 3 when none lacks it. Names only the ref id, never a value.
build_plan() {
  local live states a missing targets="[]"
  live="$(live_config)" || exit 2
  states="$(key_states "$live")" || exit 2
  mapfile -t missing < <(jq -r '.[] | select(.profile | not) | .agent' <<< "$states")
  if ((${#missing[@]} == 0)); then
    say "every agent already has $PROFILE"
    return 3
  fi
  say "adding $PROFILE (SecretRef $KEY_REF) to: ${missing[*]}"
  for a in "${missing[@]}"; do
    targets="$(jq -c --arg a "$a" --arg p "$PROFILE" --arg id "$KEY_REF" '. + [{
      type: "auth-profiles.api_key.key", path: "profiles.\($p).key",
      pathSegments: ["profiles", $p, "key"], agentId: $a, authProfileProvider: "anthropic",
      ref: {source: "exec", provider: "sops", id: $id}}]' <<< "$targets")"
  done
  jq -n --argjson t "$targets" \
    '{version: 1, protocolVersion: 1, options: {scrubEnv: false, scrubAuthProfilesForProviderTargets: false}, targets: $t}' > "$work/plan.json"
}

# The one-line command a host operator runs for the offline step.
offline_cmd() { printf 'sudo -u lifekit bash %q offline-profiles' "$HERE/auth-mode.sh"; }

ensure_profiles() {
  local rc=0 log="$work/secrets-apply.log"
  build_plan || { rc=$?; ((rc == 3)) && return 0; exit 2; }
  "$DOCKER" exec -i "$cid" sh -c 'cat > /tmp/auth-mode-plan.json' < "$work/plan.json"
  oc secrets apply --from /tmp/auth-mode-plan.json --allow-exec --dry-run > "$log" 2>&1 &&
    oc secrets apply --from /tmp/auth-mode-plan.json --allow-exec >> "$log" 2>&1 &&
    oc secrets reload > /dev/null 2>&1 || rc=1
  "$DOCKER" exec "$cid" rm -f /tmp/auth-mode-plan.json
  if ((rc != 0)); then
    if grep -qi 'state database is busy' "$log"; then
      # OpenClaw 2026.11.4: secrets apply writes the state database, and a
      # running gateway holds it; there is no gateway route for this command.
      say "the running gateway holds OpenClaw's state database, so secrets apply cannot add the key profile."
      say "the gateway must be stopped for this one step. As a host operator (it stops the gateway; never an agent), run:"
      say "  $(offline_cmd)"
      say "then re-run: bash $HERE/auth-mode.sh api"
    else
      say "secrets apply failed for the key profile"
    fi
  fi
  return "$rc"
}

# offline_profiles: HOST OPERATOR ONLY. Same plan as ensure_profiles, applied by a
# one-shot container while the gateway is stopped, the pattern deploy-openclaw.sh
# uses for its offline steps. The gateway is started again on every exit path.
offline_profiles() {
  local rc=0 i
  stopped=0 # global: the EXIT trap reads it after this function returns
  build_plan || { rc=$?; ((rc == 3)) && return 0; exit 2; }
  REPO_DIR="${REPO_DIR:-$(cd "$HERE/../.." && pwd)}"
  # shellcheck source=../lib/deploy-common.sh disable=SC1091
  source "$HERE/../lib/deploy-common.sh"
  # the shared helpers define their own say/die; keep this script's (stderr)
  say() { echo "auth-mode: $*" >&2; }
  die() {
    say "$*"
    exit 2
  }
  restore_gateway() {
    ((stopped)) || return 0
    stopped=0
    say "starting openclaw-gateway again"
    openclaw_compose up -d openclaw-gateway > /dev/null || say "COULD NOT START THE GATEWAY: run 'docker compose -p openclaw up -d openclaw-gateway'"
  }
  trap 'restore_gateway; rm -rf "$work"' EXIT
  OPENCLAW_COMPOSE_AUTH_MODE="$(openclaw_auth_mode)" || exit 2
  export OPENCLAW_COMPOSE_AUTH_MODE
  local -a one_shot=(run --rm --no-deps -T -v "$work/plan.json:/tmp/auth-mode-plan.json:ro"
    --entrypoint openclaw openclaw-gateway secrets apply --from /tmp/auth-mode-plan.json --allow-exec)
  say "stopping openclaw-cli and openclaw-gateway"
  stopped=1
  openclaw_compose --profile cli stop openclaw-cli openclaw-gateway > /dev/null || rc=1
  ((rc != 0)) || openclaw_compose "${one_shot[@]}" --dry-run > /dev/null || rc=1
  ((rc != 0)) || openclaw_compose "${one_shot[@]}" > /dev/null || rc=1
  restore_gateway
  ((rc == 0)) || {
    say "offline secrets apply failed; the gateway was started again, the key profile is NOT added"
    return 1
  }
  cid=""
  for ((i = 0; i < 30; i++)); do
    cid="$("$DOCKER" ps -q --filter label=com.docker.compose.project=openclaw \
      --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
    [[ -z "$cid" ]] || break
    sleep "$POLL"
  done
  if [[ -z "$cid" ]] || ! wait_healthy; then
    say "gateway not healthy ${HEALTH_WAIT}s after the start"
    return 1
  fi
  # healthy is not always ready for a reload right after a start: retry once
  # after waiting for health again
  oc secrets reload > /dev/null 2>&1 || {
    say "secrets reload not accepted yet; retrying once"
    wait_healthy || true
    sleep "$POLL"
    oc secrets reload > /dev/null 2>&1 || {
      say "secrets reload failed"
      return 1
    }
  }
  say "the key profile is in place; now run: bash $HERE/auth-mode.sh api"
}

# switch_to api|subscription
switch_to() {
  local target="$1" live patch rollback pending changed=0
  live="$(live_config)" || exit 2
  patch="$(patch_for "$target" "$live")"
  rollback="$(rollback_for "$patch" "$live")"
  pending="$(pending_paths "$patch" "$live")"
  if ((dry)); then
    say "dry run for $target mode; pending keys: ${pending:-none}"
    printf '%s\n' "$patch"
    printf '%s\n' "$patch" | oc config patch --stdin --dry-run > /dev/null || return 1
    return 0
  fi
  if [[ -z "$pending" ]]; then
    say "already in $target mode; config unchanged"
  else
    say "$target mode: changing $(wc -l <<< "$pending" | tr -d ' ') config keys${reason:+ ($reason)}"
    printf '%s\n' "$patch" > "$work/patch.json"
    apply_patch "$work/patch.json" || {
      say "rolling back"
      printf '%s\n' "$rollback" > "$work/rollback.json"
      apply_patch "$work/rollback.json" || say "ROLLBACK FAILED: check 'auth-mode.sh status' and the gateway"
      return 1
    }
    changed=1
  fi
  if ! verify_turns "$(provider_of "$target")"; then
    if ((changed)); then
      say "rolling back to $(other_mode "$target") mode"
      printf '%s\n' "$rollback" > "$work/rollback.json"
      apply_patch "$work/rollback.json" || say "ROLLBACK FAILED: check 'auth-mode.sh status' and the gateway"
    fi
    return 1
  fi
  say "now in $target mode"
}

cmd_api() {
  local verdict res pct
  verdict="$(probe_key)"
  res="$(jq -r .result <<< "$verdict")"
  case "$dry:$res" in
    1:*) ;;
    *:rejected | *:exhausted | *:no-key)
      say "the API key is unusable ($res). Create a new Console key and place it with scripts/secrets/edit.sh gateway (ANTHROPIC_API_KEY), merge, then re-run."
      exit 2
      ;;
  esac
  if ((!force && !dry)); then
    pct="$(spend_json | jq -r 'if . == null then 0 else (.used_percent >= .fallback_percent) end')"
    [[ "$pct" != true ]] || die "estimated spend is past the fallback threshold; top up credit.json first (or --force)"
  fi
  if ((!dry)); then ensure_profiles || exit 1; fi
  switch_to api || exit 1
}

case "$cmd" in
  status) cmd_status ;;
  mode)
    live="$(live_config)" || exit 2
    mode_of "$live"
    ;;
  probe) probe_key ;;
  api) cmd_api ;;
  subscription) switch_to subscription || exit 1 ;;
  ensure-profiles) ensure_profiles || exit 1 ;;
  offline-profiles) offline_profiles || exit 1 ;;
  *) die "usage: auth-mode.sh status [--json] [--quick] | api [--force] | subscription [--reason TEXT] | mode | probe | ensure-profiles | offline-profiles" ;;
esac
