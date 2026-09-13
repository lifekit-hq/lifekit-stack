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

REPO_DIR="${REPO_DIR:-/srv/lifekit-stack}"
ENV_FILE="${ENV_FILE:-/srv/openclaw/config/.env}"
OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
COMPOSE_FILE="${REPO_DIR}/compose/docker-compose.yml"

CURRENT_STEP="startup"
DEPLOY_COMPLETE=0
say() { CURRENT_STEP="$*"; printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }

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
}
trap on_exit EXIT

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
# feat/goalclaw-service while origin/main moved on). The CI workflow does the
# same thing before invoking this script, so this is a no-op in CI and a
# self-update when run directly on the VPS.

say "git pull"
STACK_DEFAULT="$(git remote show origin | sed -n 's/.*HEAD branch: //p' | head -1)"
STACK_DEFAULT="${STACK_DEFAULT:-main}"
git fetch -q origin "${STACK_DEFAULT}"
git reset -q --hard "origin/${STACK_DEFAULT}"

# ─── memory-audit: sync cron assets to the gateway workspace ─────────────────
#
# The weekly `memory_vault_audit` cron runs INSIDE the gateway container from
# the workspace mount — it cannot see this repo. Without this sync the cron
# keeps executing whatever was last hand-copied (found 2026-08-16: the box ran
# 2-month-stale scripts). Placed BEFORE the compose step on purpose so script
# drift heals even on a deploy that fails later.
# NOTE (ecosystem decoupling): when the OpenClaw entity gets its own deploy
# script, this block migrates there — the audit cron is an OpenClaw cron.

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

# ─── Build + start ───────────────────────────────────────────────────────────

# Keep the image that is running right now reachable as lifekit-openclaw:prev
# so an OpenClaw bump that passes doctor/health but misbehaves in real traffic
# has a one-command rollback (docs/runbook.md "Rolling back OpenClaw"). The
# `local` tag is rebuilt in place by the build below, so without this the
# previous image is unreachable the moment the build finishes.
if docker image inspect lifekit-openclaw:local >/dev/null 2>&1; then
  say "tagging current lifekit-openclaw:local as :prev (rollback target)"
  docker tag lifekit-openclaw:local lifekit-openclaw:prev
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
say "reattaching openclaw-cli to new gateway network namespace"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
  --profile cli up -d --force-recreate openclaw-cli \
  || echo "(openclaw-cli force-recreate skipped — not running)"

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

DEPLOY_COMPLETE=1
say "✓ deploy complete."
