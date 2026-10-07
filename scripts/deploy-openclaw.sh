#!/usr/bin/env bash
# deploy-openclaw.sh — the OpenClaw phases of the deploy, split out of
# deploy.sh so an OpenClaw change and a platform change can ship on their own.
#
# Each phase is a function, run at its own point in deploy.sh's sequence:
#
#   prepare    claw state move, memory-audit sync, onboard (first deploy only),
#              gateway.trustedProxies
#   modules    defaults/modules.yaml -> the vault's system/modules.yaml
#   build      build the gateway image; on a version change, tag :prev, stop
#              the gateway and migrate its state with the new image
#   up         move the services out of the platform project (once), then
#              `up -d` the openclaw project
#   configure  apply compose/openclaw-gateway/platform.patch.json, then reload
#              and audit the gateway's SOPS-backed secrets
#   post-up    reattach openclaw-cli, reset stuck sessions, skill native deps,
#              doctor / health / channels status
#   smoke      one real agent turn per runtime
#
# Sourced, never executed: deploy.sh sources this file before its main body
# and calls the phases between its platform phases, in the original order.
# deploy.sh stays the single deploy entry point (CI runs it on every push to
# main); sourcing keeps the phases parsed in memory before its git pull can
# rewrite them on disk.
#
# Re-runnable. Idempotent, like deploy.sh.

# shellcheck source=scripts/lib/deploy-common.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")")/lib/deploy-common.sh"

# ─── The platform project's copies of the OpenClaw services ─────────────────
#
# Until 2026-09-30 openclaw-gateway, openclaw-cli and google-workspace-mcp ran
# in the platform compose project (COMPOSE_FILE's, `compose` on the box).
# They run in their own project now (OPENCLAW_PROJECT, compose/openclaw/),
# and the deploy that first brings that project up has to take the old
# containers down first: the old gateway holds 127.0.0.1:18789 and the same
# state dir, so the two must never run together. Found by compose project +
# service label, never by container name, so nothing else can match; the
# platform project is never `up --remove-orphans`ed for this, since that
# would also take any other orphan it finds. The cli (one-off `run`
# containers included) goes first, since it lives in the gateway's network
# namespace. On every later deploy the lookup finds nothing and this does
# nothing.
OPENCLAW_SERVICES=(openclaw-cli openclaw-gateway google-workspace-mcp)
# Sets OPENCLAW_PLATFORM_PROJECT once; call it outside a $(...) subshell so
# the value sticks.
openclaw_resolve_platform_project() {
  if [[ -z "${OPENCLAW_PLATFORM_PROJECT:-}" ]]; then
    OPENCLAW_PLATFORM_PROJECT="$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" config --format json \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])')"
  fi
}
# Container ids of one OpenClaw service still held by the platform project.
openclaw_legacy_ids() {
  openclaw_resolve_platform_project
  # A platform project that is itself named `openclaw` would match this
  # project's own containers; nothing is legacy then.
  [[ "${OPENCLAW_PLATFORM_PROJECT}" != "${OPENCLAW_PROJECT}" ]] || return 0
  docker ps -a -q \
    --filter "label=com.docker.compose.project=${OPENCLAW_PLATFORM_PROJECT}" \
    --filter "label=com.docker.compose.service=$1"
}
openclaw_cutover() {
  local svc ids
  openclaw_resolve_platform_project
  for svc in "${OPENCLAW_SERVICES[@]}"; do
    ids="$(openclaw_legacy_ids "${svc}")"
    [[ -n "${ids}" ]] || continue
    say "cutover: removing ${svc} from compose project ${OPENCLAW_PLATFORM_PROJECT} (it runs in project ${OPENCLAW_PROJECT} now)"
    # shellcheck disable=SC2086  # one container id per word
    docker stop ${ids} >/dev/null
    # shellcheck disable=SC2086
    docker rm ${ids} >/dev/null
  done
}

# ═══ prepare ═════════════════════════════════════════════════════════════════
openclaw_phase_prepare() {
# ─── claw state: /srv/memory/state -> /var/lib/lifekit/state (one-time move) ─────
#
# The claw data stores (workout-claw, nutrition-claw, life-state) are runtime
# state, not knowledge; the vault's README (2026-09-14) evicts them from the
# tracked vault. The compose mounts now point at $LIFEKIT_STATE_DIR_HOST/state.
# Move the existing data once, only when the new location is still empty, so a
# redeploy never overwrites live state. The vault copy is deleted from git
# separately, after this has run.
STATE_DST="${LIFEKIT_STATE_DIR_HOST:-/var/lib/lifekit}/state"
STATE_SRC="${LIFEKIT_LIFE_DIR:-/srv/memory}/state"
for claw in workout-claw nutrition-claw life-state; do
  if [ -d "${STATE_SRC}/${claw}" ] && [ ! -e "${STATE_DST}/${claw}" ]; then
    say "claw state move: ${STATE_SRC}/${claw} -> ${STATE_DST}/${claw}"
    mkdir -p "${STATE_DST}"
    cp -a "${STATE_SRC}/${claw}" "${STATE_DST}/${claw}"
    chown -R 1000:1000 "${STATE_DST}/${claw}" 2>/dev/null || true
  fi
done

# ─── memory-audit: sync cron assets to the gateway workspace ─────────────────
#
# The weekly `memory_vault_audit` cron runs INSIDE the gateway container from
# the workspace mount — it cannot see this repo. Without this sync the cron
# keeps executing whatever was last hand-copied (found 2026-08-16: the box ran
# 2-month-stale scripts). Placed BEFORE the compose step on purpose so script
# drift heals even on a deploy that fails later.

say "memory-audit sync (repo -> gateway workspace)"
AUDIT_DST="${OPENCLAW_WORKSPACE_DIR:-/srv/openclaw/workspace}/memory-audit"
mkdir -p "${AUDIT_DST}"
rsync -a --delete --exclude tests/ "${REPO_DIR}/scripts/memory-audit/" "${AUDIT_DST}/"

# ─── OpenClaw onboard (first deploy only) ────────────────────────────────────
#
# A fresh host has no /srv/openclaw/config/openclaw.json — without it the
# gateway can't start. `openclaw onboard` materializes it from the stack env file using
# the same non-interactive flags that produced a working config on cax11.
# Skipped on every subsequent deploy because the file persists in the
# host-mounted config dir (idempotent).
#
# Why openclaw-gateway and not openclaw-cli: openclaw-cli has
# `network_mode: service:openclaw-gateway`, so running it on a fresh host
# would start (and crash) the gateway as a dependency — the gateway needs the
# very openclaw.json this step generates. openclaw-gateway has the same image
# and the same config-dir bind-mount, no inter-service network dep, and with
# `--no-deps --entrypoint openclaw` we get a one-shot CLI invocation that
# only writes the config file and exits.
if [[ ! -f "${OPENCLAW_CONFIG_DIR}/openclaw.json" ]]; then
  say "openclaw onboard (generating ${OPENCLAW_CONFIG_DIR}/openclaw.json)"
  openclaw_compose \
    run --rm --no-deps --entrypoint openclaw openclaw-gateway \
      onboard \
        --non-interactive \
        --accept-risk \
        --flow quickstart \
        --mode local \
        --auth-choice skip \
        --gateway-auth token \
        --gateway-token-ref-env OPENCLAW_GATEWAY_TOKEN \
        --gateway-bind loopback \
        --gateway-port 18789
fi

# Since OpenClaw 2026.9.x the gateway rejects proxied requests with
# `proxy_attribution_required` unless the proxy's source address is listed
# in gateway.trustedProxies. That address is Docker-assigned at network
# creation, so it can't be pinned in compose/openclaw-gateway/platform.patch.json
# (host-derived, not a static platform key) — it's derived here instead, off
# the OpenClaw project's default network (the script creates it on the first
# deploy of the project, before the gateway starts there).
#
# Runs on every deploy, not just the one that just onboarded: the script
# reads the live value first and writes only when the derived address is
# missing from it, so a deploy that dies between onboard writing
# openclaw.json and this step running still retries it on the next deploy
# instead of leaving the gateway rejecting proxied requests until someone
# sets the key by hand. Logic lives in scripts/deploy-trusted-proxies.sh so
# it can be exercised directly in tests.
say "openclaw trustedProxies"
ENV_FILE="${ENV_FILE}" COMPOSE_FILE="${OPENCLAW_COMPOSE_FILE}" COMPOSE_PROJECT_NAME="${OPENCLAW_PROJECT}" \
  OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR}" "${REPO_DIR}/scripts/deploy-trusted-proxies.sh"

}

# ═══ modules ═════════════════════════════════════════════════════════════════
openclaw_phase_modules() {

# ─── modules.yaml → /srv/memory/system/ ───────────────────────────────────────

LIFE_DIR="${LIFEKIT_LIFE_DIR:-/srv/memory}"
say "syncing config/modules.yaml → ${LIFE_DIR}/system/modules.yaml"
mkdir -p "${LIFE_DIR}/system"
cp "${REPO_DIR}/defaults/modules.yaml" "${LIFE_DIR}/system/modules.yaml"

}

# ═══ build ═══════════════════════════════════════════════════════════════════
openclaw_phase_build() {

# ─── OpenClaw version bump: migrate state BEFORE the new gateway boots ───────
#
# OpenClaw's state and config migrations are one-way and the Gateway refuses
# to boot on a config it no longer recognizes (2026.6.11 -> 2026.9.4 dropped
# four keys and re-keyed agents.list). Rehearsed 2026-09-13 on a state copy:
# one `doctor --fix --non-interactive` pass with the NEW image does all of it
# (config rewrite, SQLite migrations, official-plugin re-pin to the new core)
# and a second pass is a no-op. It must run with the old Gateway stopped.
# Gated on an actual version change so ordinary deploys keep zero downtime.
# The config-only backup is seconds; a full `backup create` of the state dir
# is the manual step before a multi-month jump (docs/runbook.md).

# Pin the image the gateway runs BEFORE the build. Under the containerd image
# store (`docker info`: io.containerd.snapshotter.v1) an image id stays
# addressable only while a tag or something else references it: the build
# re-tags lifekit-openclaw:local to the new image, and from then on the old
# image - which the running gateway still uses - is gone for `docker tag`
# ("No such image: sha256:...", the 2026-10-07 deploy of #280). A pin tag
# keeps it addressable across the build; on a version change it becomes
# :prev below, and it is removed on every deploy either way. The container's
# own image reference is the source, not :local: :local can already point
# at a build the gateway is not running (an earlier deploy that built and
# then died).
OPENCLAW_PIN_TAG="lifekit-openclaw:pin"
docker rmi "${OPENCLAW_PIN_TAG}" >/dev/null 2>&1 || true
PIN_ID=""
GATEWAY_CTR="$(openclaw_compose ps -a -q openclaw-gateway 2>/dev/null | head -1 || true)"
if [[ -z "${GATEWAY_CTR}" ]]; then
  # The deploy that moves OpenClaw into its own project: the running
  # gateway is still the platform project's.
  GATEWAY_CTR="$(openclaw_legacy_ids openclaw-gateway | head -1 || true)"
fi
if [[ -n "${GATEWAY_CTR}" ]]; then
  GATEWAY_IMAGE="$(docker inspect --format '{{.Image}}' "${GATEWAY_CTR}" 2>/dev/null || true)"
  if [[ -n "${GATEWAY_IMAGE}" ]] && docker tag "${GATEWAY_IMAGE}" "${OPENCLAW_PIN_TAG}" 2>/dev/null; then
    PIN_ID="${GATEWAY_IMAGE#sha256:}"
    PIN_ID="${PIN_ID:0:12}"
    say "pinned running gateway image ${PIN_ID} as ${OPENCLAW_PIN_TAG}"
  fi
fi

say "docker compose build"
openclaw_compose build openclaw-gateway

RUNNING_VER="$(openclaw_compose \
  exec -T openclaw-gateway openclaw --version 2>/dev/null | awk '{print $2}' || true)"
# On the deploy that moves OpenClaw into its own project the running gateway
# is still the platform project's; read its version there, so a version bump
# landing on that same deploy still migrates state.
LEGACY_GATEWAY=""
if [[ -z "${RUNNING_VER}" ]]; then
  openclaw_resolve_platform_project
  LEGACY_GATEWAY="$(openclaw_legacy_ids openclaw-gateway | head -1)"
  if [[ -n "${LEGACY_GATEWAY}" ]]; then
    RUNNING_VER="$(docker exec "${LEGACY_GATEWAY}" openclaw --version 2>/dev/null | awk '{print $2}' || true)"
  fi
fi
BUILT_VER="$(docker run --rm --entrypoint openclaw lifekit-openclaw:local --version 2>/dev/null | awk '{print $2}' || true)"
if [[ -n "${RUNNING_VER}" && -n "${BUILT_VER}" && "${RUNNING_VER}" != "${BUILT_VER}" ]]; then
  # Keep the image that ran the OLD version reachable as :prev, and only
  # :prev - the one-rollback rule (docs/runbook.md "Rolling back OpenClaw").
  # Ad-hoc pre-<version> tags used to pile up one per bump, at ~14 GB each,
  # never cleaned up; moving :prev now also drops the image the old :prev
  # pointed to, once nothing else tags or runs it. Retag itself is gated on
  # a version change: 2026-09-13 the unconditional retag ran on three queued
  # deploys in a row and :prev ended up pointing at the new version.
  # The pin was taken before the build (above). With no pin the running image
  # is no longer addressable (or there is no gateway container to read it
  # from). That is acceptable only when :prev already holds the version that
  # runs - the one-time recovery after the 2026-10-07 failure, and a re-run
  # after one. Otherwise stop here, before the gateway is touched, rather
  # than swap it without a rollback image.
  if [[ -z "${PIN_ID}" ]]; then
    PREV_VER="$(docker run --rm --entrypoint openclaw lifekit-openclaw:prev --version 2>/dev/null | awk '{print $2}' || true)"
    if [[ -z "${PREV_VER}" || "${PREV_VER}" != "${RUNNING_VER}" ]]; then
      echo "OpenClaw ${RUNNING_VER} -> ${BUILT_VER}: the running gateway image cannot be pinned, and lifekit-openclaw:prev is ${PREV_VER:-missing}, not ${RUNNING_VER}." >&2
      echo "Refusing to replace the gateway without a rollback image; the gateway was not touched. Make lifekit-openclaw:prev" >&2
      echo "an image of ${RUNNING_VER} (docs/runbook.md \"Rolling back OpenClaw\"), then re-run." >&2
      exit 1
    fi
    say "running image not pinnable; keeping existing :prev (OpenClaw ${PREV_VER}) as the rollback image"
  else
    OLD_PREV_IMAGE="$(docker images -q lifekit-openclaw:prev 2>/dev/null || true)"
    say "tagging running image ${PIN_ID} as :prev"
    docker tag "${OPENCLAW_PIN_TAG}" lifekit-openclaw:prev
    docker rmi "${OPENCLAW_PIN_TAG}" >/dev/null 2>&1 || true
    if [[ -n "${OLD_PREV_IMAGE}" && "${OLD_PREV_IMAGE}" != "${PIN_ID}" ]]; then
      OLD_PREV_TAGS="$(docker inspect --format '{{json .RepoTags}}' "${OLD_PREV_IMAGE}" 2>/dev/null || echo '[]')"
      OLD_PREV_IN_USE="$(docker ps -a -q --filter "ancestor=${OLD_PREV_IMAGE}" 2>/dev/null || true)"
      if [[ "${OLD_PREV_TAGS}" == "[]" && -z "${OLD_PREV_IN_USE}" ]]; then
        say "removing image ${OLD_PREV_IMAGE:0:12} the old :prev pointed to (now untagged, unused)"
        docker rmi "${OLD_PREV_IMAGE}" 2>/dev/null || true
      fi
    fi
  fi
  # Every file under the state dir must belong to the container user (uid
  # 1000). Root-owned leftovers from hand edits (.bak-*, sqlite copies) make
  # `backup create` fail with EACCES and stall the Codex session-sidecar
  # migration (2026-09-13). Refuse to migrate over them; the fix is one line.
  say "asserting ${OPENCLAW_CONFIG_DIR} is owned by uid 1000"
  FOREIGN="$(find "${OPENCLAW_CONFIG_DIR}" -xdev ! -uid 1000 \
    -not -path "${OPENCLAW_CONFIG_DIR}/workspace*" -not -path "${OPENCLAW_CONFIG_DIR}/wiki*" \
    2>/dev/null | head -5 || true)"
  if [[ -n "${FOREIGN}" ]]; then
    echo "${FOREIGN}" >&2
    echo "Files not owned by uid 1000 under ${OPENCLAW_CONFIG_DIR}. Fix, then re-run:" >&2
    echo "  sudo find ${OPENCLAW_CONFIG_DIR} -xdev ! -uid 1000 -exec chown 1000:1000 {} +" >&2
    exit 1
  fi

  say "OpenClaw ${RUNNING_VER} -> ${BUILT_VER}: stopping gateway, migrating state"
  openclaw_cutover
  openclaw_compose --profile cli \
    stop openclaw-cli openclaw-gateway
  # openclaw backup create --output takes an archive FILE path, not a directory;
  # a fixed path here would collide with an earlier deploy's archive and refuse
  # to overwrite it. One unique path per run instead.
  UPGRADE_BACKUP="/home/node/.openclaw/openclaw-config-${RUNNING_VER}-to-${BUILT_VER}-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
  openclaw_compose \
    run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
      backup create --only-config --verify --output "${UPGRADE_BACKUP}"
  openclaw_compose \
    run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
      doctor --fix --non-interactive

  # Official plugins installed into the state dir (npm/) carry their own
  # version pin. One built against an older core refuses to load under the
  # new one (codex 2026.6.8 vs core 2026.9.4 on 2026-09-13: the whole codex
  # runtime gone, gateway "healthy"). The doctor pass re-pins them; verify
  # it did, try one explicit update if not, and flag the deploy otherwise.
  say "asserting official plugins match core ${BUILT_VER}"
  plugin_mismatches() {
    openclaw_compose \
      run --rm --no-deps -T --entrypoint openclaw openclaw-gateway plugins list --json 2>/dev/null \
    | python3 -c '
import json, re, sys
core = sys.argv[1]
items = json.load(sys.stdin)
items = items if isinstance(items, list) else items.get("plugins", [])
for p in items:
    if not p.get("enabled"): continue
    v = str(p.get("version") or "")
    if p.get("origin") not in ("bundled", "global") or not re.match(r"^\d{4}\.\d+\.\d+", v): continue
    if v != core: print(p["id"], v)
' "${BUILT_VER}"
  }
  MISMATCH="$(plugin_mismatches || true)"
  if [[ -n "${MISMATCH}" ]]; then
    while read -r pid pver; do
      [[ -z "${pid}" ]] && continue
      warn "plugin ${pid} is ${pver}, core is ${BUILT_VER}; updating"
      openclaw_compose \
        run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
          plugins update "@openclaw/${pid}@latest" || true
    done <<< "${MISMATCH}"
    MISMATCH="$(plugin_mismatches || true)"
    # An `if`, not `[[ ]] && fail_later`: this block ends the phase function,
    # and a false `[[ ]]` as its last command would return 1 and trip set -e.
    if [[ -n "${MISMATCH}" ]]; then
      fail_later "plugins still off core ${BUILT_VER} after update: ${MISMATCH//$'\n'/, }"
    fi
  fi
else
  say "OpenClaw version unchanged (${BUILT_VER:-unknown}); no state migration"
  # The pin only exists for a version change. If the gateway runs an image
  # nothing else tags (a same-version rebuild), docker refuses to untag it
  # while the container uses it; the next deploy removes it.
  docker rmi "${OPENCLAW_PIN_TAG}" >/dev/null 2>&1 || true
fi

}

# ═══ up ══════════════════════════════════════════════════════════════════════
openclaw_phase_up() {

# ─── Start the openclaw project ─────────────────────────────────────────────
#
# After the platform `up`, like the single `up` both used to share. The
# cutover (top of this file) runs first, so on the deploy that moves the
# services here the old gateway is gone before the new one starts: one
# gateway recreate, as a normal image change would cause. No --build: the
# build phase already built the image. Same one-shot retry as the platform
# `up` for docker's stale temp-name reservation (#94).
openclaw_cutover
say "docker compose -p ${OPENCLAW_PROJECT} up -d"
local up_log conflict_svc
up_log="$(mktemp)"
if ! openclaw_compose up -d 2>&1 | tee "${up_log}"; then
  conflict_svc="$(grep -oE "[0-9a-f]{12}_${OPENCLAW_PROJECT}-[a-z0-9-]+-[0-9]+" "${up_log}" \
    | head -1 | sed -E "s/^[0-9a-f]{12}_${OPENCLAW_PROJECT}-//; s/-[0-9]+\$//" || true)"
  if [[ -z "${conflict_svc}" ]]; then
    rm -f "${up_log}"
    exit 1
  fi
  say "recreate conflict on '${conflict_svc}' — force-recreating once, retrying up"
  openclaw_compose up -d --force-recreate "${conflict_svc}"
  openclaw_compose up -d
fi
rm -f "${up_log}"

}

# ═══ configure ═══════════════════════════════════════════════════════════════
openclaw_phase_configure() {

# ─── OpenClaw platform config: the repo's half of openclaw.json ──────────────
#
# openclaw.json is host state (agents.entries, channels, auth profiles, MCP
# tokens - mirrored to a private repo, never in this one). But the keys the
# PLATFORM needs from it - JSON console logs and OTLP log records for the
# contract's `logs` item, the diagnostics plugins Prometheus scrapes, the
# gateway auth rate limit, heartbeats off - used to ship as operator steps in
# PR bodies (#160, #161's runbook patch) and were applied by hand, so a green
# PR could sit un-merged until someone patched the box (2026-09-17: the
# contract gate needs logging.consoleStyle=json or every deploy goes red).
# compose/openclaw-gateway/platform.patch.json holds exactly those keys and
# this step applies it with `openclaw config patch` (objects merge, scalars
# replace), so every push to main converges the live file. Personal keys
# never go in that file; a new platform key goes there, not in a PR body.
#
# Secrets in that file are literal ${VAR} references (the hook token, hook
# path and chat id - docs/runbook.md, "Inbound hooks and the finance pulse").
# `config patch` writes the reference back verbatim, never the value, so the
# compare below sees equal strings; and the dry run refuses the patch while a
# referenced variable is missing or empty in the container environment, which
# leaves the gateway alone and turns the deploy red instead of enabling hooks
# without a token.
#
# Idempotency is decided here, not by the CLI. `config patch --dry-run`
# validates the patch against the installed schema but counts every
# assignment as an update whether or not it changes anything, and a real
# `config patch` of an already-applied patch still rewrites the file and
# rotates the .bak ring (both checked against the 2026.9.4 CLI on a scratch
# state dir, 2026-09-17). So the live file is compared with the patch first,
# and an already-converged file skips the write entirely. The gateway is
# force-recreated only when the CLI's apply hint says the changed keys need
# it ("Restart the gateway to apply." - plugins.entries and the other
# restart-only paths); hot-reloadable keys are picked up by the running
# gateway on its own (gateway.reload defaults to hybrid). The one-shot
# `run --rm --entrypoint openclaw` is the same shape as the upgrade steps
# above; the CLI prints JSON lines once consoleStyle=json is live, so the
# hint is matched as a substring, not a whole line.
PLATFORM_PATCH="${REPO_DIR}/compose/openclaw-gateway/platform.patch.json"
say "openclaw platform config: comparing ${PLATFORM_PATCH#"${REPO_DIR}/"} with the live config"
PLATFORM_PENDING="$(python3 - "${PLATFORM_PATCH}" "${OPENCLAW_CONFIG_DIR}/openclaw.json" <<'PY'
import json, sys
patch = json.load(open(sys.argv[1]))
try:
    with open(sys.argv[2]) as f:
        live = json.load(f)
except Exception as e:  # unreadable, or a hand edit left JSON5: let the CLI decide
    print(f"(cannot compare: {e.__class__.__name__} reading the live config; applying unconditionally)")
    sys.exit()
def leaves(node, path=""):
    if isinstance(node, dict) and node:
        for key, value in node.items():
            yield from leaves(value, f"{path}.{key}" if path else key)
    else:
        yield path, node
MISSING = object()
for path, want in leaves(patch):
    cur = live
    for key in path.split("."):
        cur = cur.get(key, MISSING) if isinstance(cur, dict) else MISSING
        if cur is MISSING:
            break
    if want is None:  # null in a patch deletes the path
        pending = cur is not MISSING
    else:
        pending = cur is MISSING or type(cur) is not type(want) or cur != want
    if pending:
        print(path)
PY
)"
if [[ -z "${PLATFORM_PENDING}" ]]; then
  echo "  already applied; nothing to change, gateway left alone"
else
  printf '  pending: %s\n' "${PLATFORM_PENDING//$'\n'/, }"
  say "openclaw platform config: dry run against the installed schema (writes nothing)"
  # --allow-exec: the patch's channels.telegram.accounts.*.botToken values are
  # SOPS exec SecretRefs (docs/secrets.md); the CLI otherwise refuses to
  # validate an exec-sourced reference. Dry-run only: `config patch --help`
  # rejects --allow-exec on the actual apply ("--allow-exec requires
  # --dry-run", OpenClaw 2026.9.5) - the apply below resolves the same refs
  # without the flag.
  if ! openclaw_compose \
      run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
        config patch --stdin --dry-run --allow-exec < "${PLATFORM_PATCH}"; then
    fail_later "openclaw platform config: dry run rejected ${PLATFORM_PATCH#"${REPO_DIR}/"}; not applied"
  else
    say "openclaw platform config: applying"
    PATCH_LOG="$(mktemp)"
    if openclaw_compose \
        run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
          config patch --stdin < "${PLATFORM_PATCH}" 2>&1 | tee "${PATCH_LOG}"; then
      if grep -q 'Restart the gateway to apply' "${PATCH_LOG}"; then
        say "openclaw platform config: applied keys need a restart; recreating openclaw-gateway"
        openclaw_compose \
          up -d --no-deps --force-recreate openclaw-gateway
      else
        echo "  applied; the running gateway hot-reloads these keys, no recreate"
      fi
    else
      fail_later "openclaw platform config: apply failed (output above); gateway left as deployed"
    fi
    rm -f "${PATCH_LOG}"
  fi
fi

# ─── Reload and audit the gateway's SOPS-backed secrets ─────────────────────
#
# `secrets reload` re-resolves every exec SecretRef
# (channels.telegram.accounts.*.botToken, docs/secrets.md) against
# secrets/lifekit-gateway.env.sops, so a bot-token rotation in that file takes
# effect without a gateway recreate - unconditional, like deploy.sh's
# Prometheus/Grafana reloads after this phase, since the file can change
# independently of platform.patch.json. Unlike the `run --rm` one-shot calls
# elsewhere in this script, `reload` must reach the ALREADY-RUNNING gateway
# process (it's an RPC against its live in-memory state, not a fresh CLI
# invocation) - `run` starts
# a brand-new one-off container in its own network namespace, so its loopback
# is not the running gateway's and every reload failed with "Gateway not
# reachable at ws://127.0.0.1:18789 (ECONNREFUSED)". `exec` instead runs
# inside the running gateway container itself, sharing its netns, same as the
# stuck-session reset below - no --profile cli / openclaw-cli detour needed
# since exec doesn't need a second container to share that namespace with.
# `secrets audit` is report-only: it proves every configured SecretRef still
# resolves (a trip-wire for a bad re-key or a missing id) and never prints a
# value; it stays a `run` one-shot since it's offline (doesn't need the live
# gateway) and that's already proven to pass. `secrets reload` resolves refs
# as part of its normal operation and does not take --allow-exec (OpenClaw
# 2026.9.5: "does not recognize option --allow-exec"); `secrets audit` does,
# for the same reason the config patch dry run above does.
# A compose recreate returns before the gateway listens (the PR 231 deploy hit
# ECONNREFUSED 4s after one), so probe the same /healthz its healthcheck uses,
# from inside the container, for up to 120s before reloading.
say "waiting for the gateway to accept connections (up to 120s)"
GATEWAY_READY=0
for _ in $(seq 1 60); do
  if openclaw_compose \
      exec -T openclaw-gateway node -e \
      "fetch('http://127.0.0.1:18789/healthz',{signal:AbortSignal.timeout(3000)}).then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
      >/dev/null 2>&1; then
    GATEWAY_READY=1
    break
  fi
  sleep 2
done
if [[ "${GATEWAY_READY}" -ne 1 ]]; then
  fail_later "openclaw-gateway did not answer /healthz within 120s; secrets reload skipped"
else
  say "reloading gateway secrets"
  openclaw_compose \
    exec -T openclaw-gateway openclaw \
      secrets reload || fail_later "openclaw secrets reload failed"
fi
say "auditing gateway secrets (report only, no values printed)"
openclaw_compose \
  run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
    secrets audit --allow-exec || fail_later "openclaw secrets audit reported a problem"

}

# ═══ post-up ═════════════════════════════════════════════════════════════════
openclaw_phase_post_up() {

# ─── Reattach openclaw-cli to new gateway network namespace ──────────────────
#
# openclaw-cli uses network_mode: service:openclaw-gateway. When only the
# gateway's config or image changes, compose recreates the gateway container
# but may leave the CLI container alive with a stale reference to the old
# network namespace — so ws://127.0.0.1:18789 silently fails and openclaw
# commands (cron runs, health checks) can't reach the gateway.
#
# Force-recreate whenever the CLI is running (--profile cli covers the
# profile gate; the command is a no-op if the CLI container is stopped).
# Only when the persistent CLI container is actually running. `up -d
# --force-recreate` STARTS the service regardless, and on OpenClaw >= 2026.9
# the bare `openclaw` entrypoint opens a TUI that refuses to start with
# several agents configured ("TUI startup has no explicit owner") - so every
# deploy resurrected a container that died five times and tripped the
# container-exited-abnormally alert (2026-09-13). One-shot `run --rm` calls
# below never needed the persistent container.
# Looked up by compose labels, not container name; `run --rm` one-offs carry
# oneoff=True and are not the persistent container.
CLI_ID="$(docker ps -a -q \
  --filter "label=com.docker.compose.project=${OPENCLAW_PROJECT}" \
  --filter "label=com.docker.compose.service=openclaw-cli" \
  --filter "label=com.docker.compose.oneoff=False" 2>/dev/null | head -1 || true)"
CLI_STATE="absent"
if [[ -n "${CLI_ID}" ]]; then
  CLI_STATE="$(docker inspect "${CLI_ID}" --format '{{.State.Status}}' 2>/dev/null || echo absent)"
fi
if [[ "${CLI_STATE}" == "running" ]]; then
  say "reattaching openclaw-cli to new gateway network namespace"
  openclaw_compose \
    --profile cli up -d --force-recreate openclaw-cli
else
  say "openclaw-cli persistent container is ${CLI_STATE}; not started (on-demand only)"
fi

# ─── Reset stuck agent sessions ───────────────────────────────────────────────
#
# Killing the gateway mid-conversation (every deploy) leaves agent sessions
# as status=running. The next inbound message hits that session key, finds it
# "running", and the gateway won't start a fresh conversation — so the agent
# goes silent until the session is manually cleared. Reset any stuck sessions
# immediately after the gateway starts so the first post-deploy message always
# gets a clean session.
say "resetting stuck agent sessions (running → aborted)"
openclaw_compose exec -T openclaw-gateway \
  python3 -c "
import json, glob, os, sys
stores = glob.glob('/home/node/.openclaw/agents/*/sessions/sessions.json')
total = 0
for path in stores:
    try:
        with open(path) as f:
            data = json.load(f)
        changed = 0
        for key, val in data.items():
            if isinstance(val, dict) and val.get('status') == 'running':
                val['status'] = 'done'
                val['abortedLastRun'] = True
                changed += 1
        if changed:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
            agent = os.path.basename(os.path.dirname(os.path.dirname(path)))
            print(f'  {agent}: reset {changed} stuck session(s)')
            total += changed
    except Exception as e:
        print(f'  warning: {path}: {e}', file=sys.stderr)
if total == 0:
    print('  no stuck sessions found')
else:
    print(f'  total: {total} session(s) reset')
" || echo "(session reset reported issues — review above)"

# ─── Skill native-deps install ───────────────────────────────────────────────
#
# Workspace skills are rsync'd from the laptop and may carry a package.json
# with native deps (e.g. nutrition-claw uses `sharp`, which needs a
# linux-arm64 build on the VPS). Running `npm install --omit=dev` inside the
# gateway container — which now bakes python3/make/g++ — produces
# the correct platform binaries. Source: see proposals/2026-05-19-vps-skill-wrappers.md.
#
# Skills without a package.json are skipped. life-state's CLI binary is
# installed -g separately (see SKILL_CLI_INSTALL below).

SKILLS_DIR="${OPENCLAW_WORKSPACE_DIR:-/srv/openclaw/workspace}/skills"
if [[ -d "${SKILLS_DIR}" ]]; then
  say "Installing skill native deps inside openclaw-gateway"
  # shellcheck disable=SC2016  # expanded by bash inside the container
  openclaw_compose exec -T openclaw-gateway \
    bash -c '
      set -e
      shopt -s nullglob
      for pkg in /home/node/.openclaw/workspace/skills/*/package.json; do
        dir="$(dirname "$pkg")"
        echo "→ npm install --omit=dev in $dir"
        (cd "$dir" && npm install --omit=dev --no-audit --no-fund) \
          || echo "  (npm install failed in $dir — review above)"
      done
    ' || echo "(skill native-dep install reported issues — review above)"
fi

# ─── Skill CLI install (life-state etc.) ─────────────────────────────────────
#
# Some module CLIs are NOT shipped from this repo — they live in separate
# repos (workout-claw, life-state, health-claw) on the maintainer's laptop
# and are rsync'd to /srv/openclaw/workspace/external/ on the VPS. The
# health-claw container installs them at start from those paths.
#
# Before running this deploy script after adding the health-claw service,
# rsync the external sources to the VPS:
#
#   rsync -a ~/projects/workout-claw/ lifekit@lifekit-vps:/srv/openclaw/workspace/external/workout-claw/
#   rsync -a ~/projects/life-state/   lifekit@lifekit-vps:/srv/openclaw/workspace/external/life-state/
#   rsync -a ~/projects/health-claw/  lifekit@lifekit-vps:/srv/openclaw/workspace/external/health-claw/
#
# Also create the workout-claw data dir if it doesn't exist:
#   ssh lifekit@lifekit-vps mkdir -p /srv/workout-claw
#
# And register the health-claw MCP in openclaw.json:
#   (add the entry from compose/health-claw/mcp-config.json to /srv/openclaw/config/openclaw.json)

# ─── Health checks ───────────────────────────────────────────────────────────
#
# Wait 30s instead of 10s: Telegram channels have a 120s connect-grace period
# but are typically connected within 15-20s. 30s gives them time to show as
# "connected" in the channels status output so the deploy log is useful.

say "Waiting 30s for services and Telegram channels to settle"
sleep 30

say "openclaw doctor"
openclaw_compose --profile cli run --rm -T openclaw-cli \
  doctor || echo "(doctor reported issues — review above)"

say "openclaw health"
openclaw_compose --profile cli run --rm -T openclaw-cli \
  health || echo "(health reported issues — review above)"

say "openclaw channels status"
openclaw_compose --profile cli run --rm -T openclaw-cli \
  channels status || echo "(channels status reported issues — review above)"

}

# ═══ smoke ═══════════════════════════════════════════════════════════════════
openclaw_phase_smoke() {

# ─── Smoke turns: one real agent turn per runtime ───────────────────────────
#
# doctor/health/channels all passed on 2026-09-13 while two of three runtimes
# were dead (claude-cli binary without its native part, codex auth expired).
# The only check that sees that is a real turn. Keep this list in step with
# agents.entries.*.model when routing changes. Every agent is on claude-cli
# since the 2026-09-16 all-agents switch off OpenAI-primary (guard-45
# report), so one kit turn covers the one live runtime. fable was the
# second smoke agent (the Claude-only, no-fallback shape) until the
# 2026-09 fleet reshape retired it, and kit's fallbacks were emptied in the
# 2026-09-17 host patch, so kit now carries that same shape. Add an agent
# here again only when it runs a different runtime (OpenAI-primary routing
# returning for any agent, say), not per agent.
# A per-agent, per-run session id keeps the turn out of the agents' main
# sessions (one shared id fails: a session is placed with its first agent
# and the gateway refuses another agent in it) and out of any earlier smoke
# session: a fixed id carries CLI history across a credential change, which
# the gateway refuses ("cli session history refused across auth boundary").
# No --deliver, so nothing reaches Telegram. A good turn is status "ok" with
# a non-empty reply (there is no top-level "ok" key); anything else prints
# the error, or status/summary/reply when there is none. Auth and quota
# errors are external state (re-login, weekly cap) and only warn; anything
# else fails the run.
SMOKE_AGENTS="${SMOKE_AGENTS:-kit}"
SMOKE_RUN="$(date -u +%Y%m%dT%H%M%SZ)"
say "smoke turns (${SMOKE_AGENTS})"
for agent in ${SMOKE_AGENTS}; do
  OUT="$(openclaw_compose exec -T openclaw-gateway \
    openclaw agent --agent "${agent}" --session-id "deploy-smoke-${agent}-${SMOKE_RUN}" --timeout 150 --json \
      -m "Deploy smoke test: reply with exactly the word pong and nothing else." 2>&1 || true)"
  VERDICT="$(printf '%s' "${OUT}" | python3 -c '
import json, re, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
except Exception:
    print("FAIL no JSON: " + raw[:200].replace("\n", " ")); sys.exit()
texts = [p.get("text") or "" for p in (d.get("result") or {}).get("payloads") or []]
if d.get("status") == "ok" and "".join(texts).strip():
    print("PASS"); sys.exit()
err = json.dumps(d.get("error") or {"status": d.get("status"), "summary": d.get("summary"), "reply": texts})
soft = re.search(r"rate_limit|weekly limit|usage limit|no usable profiles|expired|not logged in|auth", err, re.I)
print(("WARN " if soft else "FAIL ") + err[:300])
')"
  case "${VERDICT}" in
    PASS)   echo "  ${agent}: ok" ;;
    WARN*)  warn "${agent}: ${VERDICT#WARN }" ;;
    *)      fail_later "${agent}: ${VERDICT#FAIL }" ;;
  esac
done

}
