#!/usr/bin/env bash
# deploy.sh — run on the VPS, after bootstrap-vps.sh has set up the host.
#
# Pulls the latest template, rebuilds containers, runs OpenClaw's health checks.
#
# Prerequisites on the VPS:
#   /srv/lifekit-stack/                  ← cloned by bootstrap-vps.sh
#   /srv/openclaw/config/.env            ← scp'd from your laptop (see .env.example)
#   /srv/openclaw/workspace/skills/      ← rsync'd from your laptop's ~/.openclaw/workspace/skills/
#   /srv/memory/                           ← rsync'd from your laptop's ~/memory/
#   /home/lifekit/.claude/               ← either logged in on the VPS via `claude auth login`,
#                                          or rsync'd from your laptop's ~/.claude/
#
# Re-runnable. Idempotent. Restarts only the services with changed images/config.

set -euo pipefail

# Resolved before anything below can change this file on disk (see the
# re-exec block after "git pull").
SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"

REPO_DIR="${REPO_DIR:-/srv/lifekit-stack}"
ENV_FILE="${ENV_FILE:-/srv/openclaw/config/.env}"
OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
COMPOSE_FILE="${REPO_DIR}/compose/docker-compose.yml"

CURRENT_STEP="startup"
DEPLOY_COMPLETE=0
DEPLOY_FAILURES=()
say() { CURRENT_STEP="$*"; printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }
# Post-deploy assertions do not abort mid-flight (a half-deployed box is
# worse than a deployed one with a red log); they queue here and fail the
# run at the end so CI goes red and the log says exactly what is wrong.
fail_later() { DEPLOY_FAILURES+=("$*"); printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }

# A deploy that dies mid-script must announce it — `set -e` otherwise skips
# the post-up steps (cli reattach, session reset, runner verify) with nothing
# but a log tail to show for it (2026-07-11: a compose name conflict aborted
# the up step; the stack LOOKED deployed while three steps never ran). #94
on_exit() {
  local code=$?
  if [[ "${DEPLOY_COMPLETE}" != "1" ]]; then
    printf '\n\033[1;31m✗ DEPLOY FAILED (exit %s) during: %s\033[0m\n' "${code}" "${CURRENT_STEP}" >&2
    printf '\033[1;31m  Later steps (cli reattach, session reset, runner verify) did NOT run.\033[0m\n' >&2
    printf '\033[1;31m  Fix the failure and re-run deploy.sh — it is idempotent.\033[0m\n' >&2
  fi
  if [[ -n "${DEPLOY_DOCKER_CONFIG:-}" ]]; then rm -rf "${DEPLOY_DOCKER_CONFIG}"; fi
}
trap on_exit EXIT

# Everything that follows is a `main` body: bash must fully parse a function
# definition (matching the closing brace) before it runs any of it, so the
# whole thing — including the git pull below and the re-exec right after it —
# is already in memory as parsed commands before that pull can touch this
# file's bytes on disk. Without this wrapper, a top-level script keeps
# reading itself line-by-line straight off disk as it executes, and a
# `git reset --hard` partway through corrupts whatever runs after it (see the
# re-exec comment below for the incident this fixes).
main() {

cd "${REPO_DIR}"

# ─── Sanity ──────────────────────────────────────────────────────────────────

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy .env.example and fill it in:" >&2
  echo "  scp .env.example user@vps:${ENV_FILE}" >&2
  exit 1
fi

# ─── Pull latest ─────────────────────────────────────────────────────────────
#
# fetch + hard-reset is race-proof and branch-agnostic. A bare `git pull
# --ff-only` aborts when the local branch has diverged (e.g. VPS is on
# feat/goalclaw-service while origin/main moved on).
#
# DEPLOY_REF pins the reset target to the commit that triggered the deploy
# (CI passes $GITHUB_SHA) rather than "whatever origin/main is right now" —
# with several deploys queued on the same concurrency group, main can move
# again before an earlier queued run starts, which used to attribute that
# run's changes to the wrong merge (#147). Left unset, it's a self-update to
# the remote's default branch, for running this directly on the VPS.

say "git pull"
STACK_DEFAULT="$(git remote show origin | sed -n 's/.*HEAD branch: //p' | head -1)"
STACK_DEFAULT="${STACK_DEFAULT:-main}"
git fetch -q origin "${STACK_DEFAULT}"
git reset -q --hard "${DEPLOY_REF:-origin/${STACK_DEFAULT}}"

# The `main` wrapper above stops THIS run from corrupting on the hard-reset,
# but it was still parsed from whatever revision was on disk when the caller
# invoked `bash scripts/deploy.sh` — on merge run 35142022831 for #162, that
# was the pre-merge revision, so the rest of this run would silently execute
# stale deploy logic (old smoke block, old fixes) even though the repo is now
# at the merge commit. Re-exec a fresh `bash` on the just-pulled file so the
# actual deploy logic that runs is always the revision this pull just landed.
# Guarded by an env marker so this fires once even though the second pass
# repeats the pull (a no-op — it's already at the target ref).
if [[ -z "${LIFEKIT_DEPLOY_REEXEC:-}" ]]; then
  say "re-executing deploy.sh at $(git rev-parse --short HEAD) (post-pull, run the revision just pulled)"
  export LIFEKIT_DEPLOY_REEXEC=1
  exec bash "${SELF}" "$@"
fi

# ─── memory-audit: sync cron assets to the gateway workspace ─────────────────
#
# The weekly `memory_vault_audit` cron runs INSIDE the gateway container from
# the workspace mount — it cannot see this repo. Without this sync the cron
# keeps executing whatever was last hand-copied (found 2026-08-16: the box ran
# 2-month-stale scripts). Placed BEFORE the compose step on purpose so script
# drift heals even on a deploy that fails later.
# NOTE (ecosystem decoupling): when the OpenClaw entity gets its own deploy
# script, this block migrates there — the audit cron is an OpenClaw cron.

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

say "memory-audit sync (repo -> gateway workspace)"
AUDIT_DST="${OPENCLAW_WORKSPACE_DIR:-/srv/openclaw/workspace}/memory-audit"
mkdir -p "${AUDIT_DST}"
rsync -a --delete --exclude tests/ "${REPO_DIR}/scripts/memory-audit/" "${AUDIT_DST}/"

# ─── OpenClaw onboard (first deploy only) ────────────────────────────────────
#
# A fresh host has no /srv/openclaw/config/openclaw.json — without it the
# gateway can't start. `openclaw onboard` materializes it from the .env using
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
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
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

# ─── lifekit-dashboard: deployed from its own repo now (decoupling slice 2) ──
#
# The dashboard deploys from lifekit-hq/lifekit-dashboard's own deploy/
# (ghcr image + `dashboard` compose project + workflow_dispatch). This stack
# no longer clones or builds it, and the 5-minute redeploy timer is retired.

# ─── modules.yaml → /srv/memory/system/ ───────────────────────────────────────

LIFE_DIR="${LIFEKIT_LIFE_DIR:-/srv/memory}"
say "syncing config/modules.yaml → ${LIFE_DIR}/system/modules.yaml"
mkdir -p "${LIFE_DIR}/system"
cp "${REPO_DIR}/defaults/modules.yaml" "${LIFE_DIR}/system/modules.yaml"

# Runtime-state dir — split from /srv/memory per proposal
# 2026-05-27-runtime-knowledge-split. Idempotent guard so an in-place upgrade
# (without a fresh bootstrap-vps.sh run) still ends up with the dirs the compose
# bind-mounts expect.
STATE_DIR="${LIFEKIT_STATE_DIR_HOST:-/var/lib/lifekit}"
if [ ! -d "${STATE_DIR}" ]; then
  say "creating ${STATE_DIR} (runtime-state dir, first-time upgrade)"
  sudo install -d -o "$(whoami)" -g "$(whoami)" -m 0750 \
    "${STATE_DIR}" \
    "${STATE_DIR}/tasks" \
    "${STATE_DIR}/.curator-proposed"
fi

# ─── devclaw: deployed from its own repo now (devclaw spec 005) ─────────────
#
# devclaw is deployed from its own repo now (devclaw spec 005) — see
# devclaw/deploy/. This stack no longer clones/builds devclaw: the whole block
# that resolved DEVCLAW_SHA, rebuilt devclaw-mcp + devclaw-sandbox with
# --no-cache (to dodge the stale git-clone layer), tagged devclaw-sandbox:latest,
# and md5-verified runner.py against GitHub is gone. devclaw-mcp runs in
# devclaw's OWN compose project, and the sandbox image is pulled from ghcr by
# that project. (The ops-agent image this script used to build was retired
# on 2026-09-06; the observability stack in compose/ is the watcher now.)

# ─── Render Grafana's Telegram contact point ─────────────────────────────────
#
# Grafana's provisioning interpolation re-types an all-digit env value as a
# JSON number, and the telegram integration then refuses to start ("cannot
# unmarshal number into Go struct field Config.chatid of type string",
# verified on 11.2.0 — quoting it in the YAML does not help). So the chat id
# is substituted HERE instead of read from the container environment, which
# also keeps the owner's chat id out of git (docs/PRIVATE.md, #132): the
# repo carries only the .tmpl, the rendered .yml is gitignored.
#
# Fails the deploy when the id is missing. A Grafana with no contact point
# starts happily and drops every alert on the floor — the one failure mode
# this whole stack exists to prevent.

say "rendering Grafana contact point"
ALERT_DIR="${REPO_DIR}/compose/observability/grafana/provisioning/alerting"
CHAT_ID="$(sed -nE 's/^[[:space:]]*(DEVCLAW_CHAT|LIFEKIT_TELEGRAM_CHAT)=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\2/p' \
  "${ENV_FILE}" | head -1)"
if [[ -z "${CHAT_ID}" ]]; then
  echo "No DEVCLAW_CHAT or LIFEKIT_TELEGRAM_CHAT in ${ENV_FILE}." >&2
  echo "Grafana's alerts would go nowhere. Set one and re-run." >&2
  exit 1
fi
sed "s/__TELEGRAM_CHAT_ID__/${CHAT_ID}/" \
  "${ALERT_DIR}/contact-points.yml.tmpl" > "${ALERT_DIR}/contact-points.yml"

# container-exporter reads the docker socket as nobody + the docker group; the
# group id differs per box, so take it from the socket itself unless the env
# file pins DOCKER_GID.
if [[ -z "${DOCKER_GID:-}" ]] && ! grep -qE '^[[:space:]]*DOCKER_GID=' "${ENV_FILE}"; then
  DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
  export DOCKER_GID
fi

# ─── Platform contract: declarations (pre-up gate) ───────────────────────────
#
# Guardrail 1 (2026-09-16): every product meets the platform contract -
# health + readiness, metrics that reach Prometheus, JSON logs with a trace
# id, OTLP traces, behind the edge, owns its topics. No waivers. Each service
# declares how it meets it in lifekit.contract.* labels; a service with a
# missing or inconsistent declaration never reaches `up`. The running
# containers are checked after the smoke turns below. docs/platform-contract.md
say "platform contract: declarations"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" config --format json \
  | python3 "${REPO_DIR}/scripts/platform-contract.py" --static -

# ─── Build + start ───────────────────────────────────────────────────────────

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

# Private, empty Docker config for every docker call from here on. The
# dashboard and devclaw deploy jobs `docker login ghcr.io` with their
# job-scoped GITHUB_TOKEN into the shared lifekit config and never log out;
# once that token expires every ghcr pull through that config fails with
# "failed to fetch oauth token: denied" (2026-09-15/16). All images this
# stack pulls are public, so anonymous pulls are enough.
DEPLOY_DOCKER_CONFIG="$(mktemp -d)"
export DOCKER_CONFIG="${DEPLOY_DOCKER_CONFIG}"

say "docker compose build"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" build openclaw-gateway

RUNNING_VER="$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
  exec -T openclaw-gateway openclaw --version 2>/dev/null | awk '{print $2}' || true)"
BUILT_VER="$(docker run --rm --entrypoint openclaw lifekit-openclaw:local --version 2>/dev/null | awk '{print $2}' || true)"
if [[ -n "${RUNNING_VER}" && -n "${BUILT_VER}" && "${RUNNING_VER}" != "${BUILT_VER}" ]]; then
  # Keep the image that ran the OLD version reachable as :prev, and only
  # :prev - the one-rollback rule (docs/runbook.md "Rolling back OpenClaw").
  # Ad-hoc pre-<version> tags used to pile up one per bump, at ~14 GB each,
  # never cleaned up; moving :prev now also drops the image the old :prev
  # pointed to, once nothing else tags or runs it. Retag itself is gated on
  # a version change: 2026-09-13 the unconditional retag ran on three queued
  # deploys in a row and :prev ended up pointing at the new version.
  PREV_IMAGE="$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
    images -q openclaw-gateway 2>/dev/null | head -1 || true)"
  OLD_PREV_IMAGE="$(docker images -q lifekit-openclaw:prev 2>/dev/null || true)"
  if [[ -n "${PREV_IMAGE}" ]]; then
    say "tagging running image ${PREV_IMAGE:0:12} as :prev"
    docker tag "${PREV_IMAGE}" lifekit-openclaw:prev
    if [[ -n "${OLD_PREV_IMAGE}" && "${OLD_PREV_IMAGE}" != "${PREV_IMAGE}" ]]; then
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
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" --profile cli \
    stop openclaw-cli openclaw-gateway
  # openclaw backup create --output takes an archive FILE path, not a directory;
  # a fixed path here would collide with an earlier deploy's archive and refuse
  # to overwrite it. One unique path per run instead.
  UPGRADE_BACKUP="/home/node/.openclaw/openclaw-config-${RUNNING_VER}-to-${BUILT_VER}-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
    run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
      backup create --only-config --verify --output "${UPGRADE_BACKUP}"
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
    run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
      doctor --fix --non-interactive

  # Official plugins installed into the state dir (npm/) carry their own
  # version pin. One built against an older core refuses to load under the
  # new one (codex 2026.6.8 vs core 2026.9.4 on 2026-09-13: the whole codex
  # runtime gone, gateway "healthy"). The doctor pass re-pins them; verify
  # it did, try one explicit update if not, and flag the deploy otherwise.
  say "asserting official plugins match core ${BUILT_VER}"
  plugin_mismatches() {
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
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
      docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
        run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
          plugins update "@openclaw/${pid}@latest" || true
    done <<< "${MISMATCH}"
    MISMATCH="$(plugin_mismatches || true)"
    [[ -n "${MISMATCH}" ]] && fail_later "plugins still off core ${BUILT_VER} after update: ${MISMATCH//$'\n'/, }"
  fi
else
  say "OpenClaw version unchanged (${BUILT_VER:-unknown}); no state migration"
fi

say "docker compose up -d --build"
# docker's recreate path can trip on a stale temp-name reservation
# ("Conflict. The container name \"/<hash>_compose-<svc>-1\" is already in
# use..."). One force-recreate of the conflicting service picks a fresh temp
# name and clears it; anything else stays a hard failure. #94
UP_LOG="$(mktemp)"
if ! docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --build 2>&1 | tee "${UP_LOG}"; then
  CONFLICT_SVC="$(grep -oE '[0-9a-f]{12}_compose-[a-z0-9-]+-[0-9]+' "${UP_LOG}" \
    | head -1 | sed -E 's/^[0-9a-f]{12}_compose-//; s/-[0-9]+$//' || true)"
  if [[ -n "${CONFLICT_SVC}" ]]; then
    say "recreate conflict on '${CONFLICT_SVC}' — force-recreating once, retrying up"
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
      up -d --force-recreate "${CONFLICT_SVC}"
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --build
  else
    exit 1
  fi
fi
rm -f "${UP_LOG}"

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
  if ! docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
      run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
        config patch --stdin --dry-run < "${PLATFORM_PATCH}"; then
    fail_later "openclaw platform config: dry run rejected ${PLATFORM_PATCH#"${REPO_DIR}/"}; not applied"
  else
    say "openclaw platform config: applying"
    PATCH_LOG="$(mktemp)"
    if docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
        run --rm --no-deps -T --entrypoint openclaw openclaw-gateway \
          config patch --stdin < "${PLATFORM_PATCH}" 2>&1 | tee "${PATCH_LOG}"; then
      if grep -q 'Restart the gateway to apply' "${PATCH_LOG}"; then
        say "openclaw platform config: applied keys need a restart; recreating openclaw-gateway"
        docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
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

# ─── Reload Prometheus' scrape config ────────────────────────────────────────
#
# Same shape as the Grafana reload below: prometheus.yml is bind-mounted, so
# `up -d` leaves the container alone when only the file changed, and
# Prometheus reads its config at startup - the `containers` job merged in
# #139 stayed unscraped until a hand recreate (2026-09-13). SIGHUP is the
# reload path with --web.enable-lifecycle off; it works because the config is
# mounted as a DIRECTORY (a single-file bind mount keeps the old inode after
# git replaces the file, and a reload would re-read the stale copy).
say "reloading Prometheus config"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" kill -s HUP prometheus

# ─── Reload Grafana's provisioned alerting ───────────────────────────────────
#
# Grafana reads provisioning/alerting/*.yml at STARTUP only, and `up -d` does
# not recreate grafana when only a bind-mounted file changed - so a rule
# merged to main would sit on disk unloaded until the next unrelated
# recreate. The admin reload endpoint applies rules, contact points and
# policies from the files now. Fails the deploy if Grafana never answers:
# alert rules that did not load are the failure this stack exists to prevent.
say "reloading Grafana alerting provisioning"
GRAFANA_USER="$(sed -nE 's/^[[:space:]]*GRAFANA_ADMIN_USER=["'"'"']?([^"'"'"']*)["'"'"']?[[:space:]]*$/\1/p' "${ENV_FILE}" | head -1)"
GRAFANA_PASS="$(sed -nE 's/^[[:space:]]*GRAFANA_ADMIN_PASSWORD=["'"'"']?([^"'"'"']*)["'"'"']?[[:space:]]*$/\1/p' "${ENV_FILE}" | head -1)"
GRAFANA_PORT_VALUE="$(sed -nE 's/^[[:space:]]*GRAFANA_PORT=["'"'"']?([0-9]+)["'"'"']?[[:space:]]*$/\1/p' "${ENV_FILE}" | head -1)"
GRAFANA_URL="http://127.0.0.1:${GRAFANA_PORT_VALUE:-3000}"
for _ in $(seq 1 30); do
  if curl -sf -o /dev/null "${GRAFANA_URL}/api/health"; then break; fi
  sleep 2
done
# Credentials go in through a curl config on stdin: not on the command line
# (visible in `ps`), not in a -u flag (gitleaks' curl-auth-user rule).
if ! curl -sf -o /dev/null -X POST -K - "${GRAFANA_URL}/api/admin/provisioning/alerting/reload" <<CURLCFG
user = "${GRAFANA_USER:-admin}:${GRAFANA_PASS:-admin}"
CURLCFG
then
  echo "Grafana did not reload alerting provisioning at ${GRAFANA_URL}." >&2
  echo "Rules on disk are not the rules loaded. Check the grafana container." >&2
  exit 1
fi

# ─── Reload Grafana's provisioned datasources ────────────────────────────────
#
# Same trap as alerting above: datasources.yml is bind-mounted and Grafana
# reads provisioning/datasources/*.yml at STARTUP only, so the Tempo
# datasource (#144) added to disk would sit unloaded until the next
# unrelated grafana recreate. Same stdin curl-config credential pattern.
say "reloading Grafana datasource provisioning"
if ! curl -sf -o /dev/null -X POST -K - "${GRAFANA_URL}/api/admin/provisioning/datasources/reload" <<CURLCFG
user = "${GRAFANA_USER:-admin}:${GRAFANA_PASS:-admin}"
CURLCFG
then
  echo "Grafana did not reload datasource provisioning at ${GRAFANA_URL}." >&2
  echo "Datasources on disk are not the datasources loaded. Check the grafana container." >&2
  exit 1
fi

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
CLI_STATE="$(docker inspect compose-openclaw-cli-1 --format '{{.State.Status}}' 2>/dev/null || echo absent)"
if [[ "${CLI_STATE}" == "running" ]]; then
  say "reattaching openclaw-cli to new gateway network namespace"
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
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
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" exec -T openclaw-gateway \
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
# gateway container — which now bakes python3/make/g++/libvips-dev — produces
# the correct platform binaries. Source: see proposals/2026-05-19-vps-skill-wrappers.md.
#
# Skills without a package.json are skipped. life-state's CLI binary is
# installed -g separately (see SKILL_CLI_INSTALL below).

SKILLS_DIR="${OPENCLAW_WORKSPACE_DIR:-/srv/openclaw/workspace}/skills"
if [[ -d "${SKILLS_DIR}" ]]; then
  say "Installing skill native deps inside openclaw-gateway"
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" exec -T openclaw-gateway \
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
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" --profile cli run --rm -T openclaw-cli \
  doctor || echo "(doctor reported issues — review above)"

say "openclaw health"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" --profile cli run --rm -T openclaw-cli \
  health || echo "(health reported issues — review above)"

say "openclaw channels status"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" --profile cli run --rm -T openclaw-cli \
  channels status || echo "(channels status reported issues — review above)"

say "container status"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps

# ─── Smoke turns: one real agent turn per runtime ───────────────────────────
#
# doctor/health/channels all passed on 2026-09-13 while two of three runtimes
# were dead (claude-cli binary without its native part, codex auth expired).
# The only check that sees that is a real turn. Keep this list in step with
# agents.entries.*.model when routing changes:
#   fable -> claude-cli (Claude-only, no fallback: exercises that backend and
#            nothing else); kit -> claude-cli (Claude primary since the
#            2026-09-16 all-agents switch off OpenAI-primary; guard-45 report).
# Both currently exercise the same backend (OpenAI/codex is not primary for
# any agent right now) — kept as two agents because they cover different
# fallback shapes (none vs Claude->OpenAI), not different runtimes. Revisit
# this comment if OpenAI-primary routing returns for any agent.
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
SMOKE_AGENTS="${SMOKE_AGENTS:-fable kit}"
SMOKE_RUN="$(date -u +%Y%m%dT%H%M%SZ)"
say "smoke turns (${SMOKE_AGENTS})"
for agent in ${SMOKE_AGENTS}; do
  OUT="$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" exec -T openclaw-gateway \
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

# ─── Platform contract: running containers ──────────────────────────────────
#
# After the smoke turns on purpose: they are real traffic, so the gateway has
# logged traced work, and Prometheus has scraped since the reload above. This
# project's containers gate the deploy (a failure goes red like any other
# post-deploy assertion - the containers are already up). The rest of the box
# prints as a report-only census: finance-sentry, devclaw and the dashboard
# deploy from their own repos and run this same script on their own project.
say "platform contract: running containers"
STACK_PROJECT="$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" config --format json \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])')"
python3 "${REPO_DIR}/scripts/platform-contract.py" --enforce "${STACK_PROJECT}" \
  || fail_later "platform contract: ${STACK_PROJECT} containers fail it (table above)"

# ─── Docker builder cache cap: host fact the repo owns, report-only ──────────
#
# scripts/docker-builder-gc.sh carries the cap (builder.gc in daemon.json),
# but this account cannot apply it: dockerd reads builder.* at startup only
# and the deploy has no sudo, so writing the file and restarting the daemon is
# a scheduled operator step (the script's own notice). What the deploy can do
# is stop the record from lying: read the policy the running daemon enforces
# and print, in red, when it is not the repository's value. Report-only, like
# the census above - a host fact never turns the deploy red.
say "docker builder cache cap (host fact, report-only)"
CAP_STATUS=0
bash "${REPO_DIR}/scripts/docker-builder-gc.sh" --check || CAP_STATUS=$?
case "${CAP_STATUS}" in
  0) ;;
  2) warn "docker builder cache cap: could not read the live GC policy, cap undetermined (output above)" ;;
  *) printf '\033[1;31m✗ docker builder cache cap: the running daemon does not enforce the repository value (apply: sudo bash scripts/docker-builder-gc.sh, then a scheduled systemctl restart docker)\033[0m\n' >&2 ;;
esac

if (( ${#DEPLOY_FAILURES[@]} )); then
  say "post-deploy assertions failed"
  printf '  - %s\n' "${DEPLOY_FAILURES[@]}" >&2
  exit 1
fi

DEPLOY_COMPLETE=1
say "✓ deploy complete."

}

main "$@"
