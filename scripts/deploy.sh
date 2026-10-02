#!/usr/bin/env bash
# deploy.sh — run on the VPS, after bootstrap-vps.sh has set up the host.
#
# Pulls the latest template, rebuilds containers, runs OpenClaw's health checks.
#
# Prerequisites on the VPS:
#   /srv/lifekit-stack/                  ← cloned by bootstrap-vps.sh
#   /srv/lifekit-secrets/stack.env       ← rendered by scripts/secrets/render-stack-env.sh
#                                          from secrets/lifekit.env.sops (see .env.example
#                                          for what belongs in the master boundary)
#   /srv/openclaw/workspace/skills/      ← rsync'd from your laptop's ~/.openclaw/workspace/skills/
#   /srv/memory/                           ← rsync'd from your laptop's ~/memory/
#   /home/lifekit/.claude/, .claude.json ← bind-mount sources for the gateway's Claude CLI home
#                                          (session files only; Anthropic auth is the
#                                          CLAUDE_OAUTH_TOKEN SecretRef, docs/secrets-runbook.md)
#
# Re-runnable. Idempotent. Restarts only the services with changed images/config.

set -euo pipefail

# Resolved before anything below can change this file on disk (see the
# re-exec block after "git pull").
SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"

# Config defaults and the say / warn / fail_later helpers live in
# scripts/lib/deploy-common.sh, shared with scripts/deploy-openclaw.sh, whose
# OpenClaw phases are functions this script calls between its own. Both are
# sourced here, before `main` runs, so - like `main` itself - they are parsed
# into memory before the git pull below can rewrite them on disk.
SCRIPT_DIR="$(dirname "${SELF}")"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"
# shellcheck source=scripts/deploy-openclaw.sh
source "${SCRIPT_DIR}/deploy-openclaw.sh"
# shellcheck source=scripts/lib/openclaw-paths.sh
source "${SCRIPT_DIR}/lib/openclaw-paths.sh"

DEPLOY_COMPLETE=0

# A deploy that dies mid-script must announce it — `set -e` otherwise skips
# every later step with nothing but a log tail to show for it (2026-07-11: a
# compose name conflict aborted the up step; the stack LOOKED deployed while
# three steps never ran). #94
on_exit() {
  local code=$?
  if [[ "${DEPLOY_COMPLETE}" != "1" ]]; then
    printf '\n\033[1;31m✗ DEPLOY FAILED (exit %s) during: %s\033[0m\n' "${code}" "${CURRENT_STEP}" >&2
    printf '\033[1;31m  Every step after that one did NOT run.\033[0m\n' >&2
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

deploy_require_env_file

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

# ─── OpenClaw gate: run the OpenClaw phases, or leave the gateway alone ──────
#
# scripts/lib/openclaw-paths.sh. CI sets LIFEKIT_DEPLOY_OPENCLAW=auto on a push
# to main, so a platform-only merge skips every openclaw_phase_* call below
# (no doctor, no smoke turns, no gateway build, no `up` of the openclaw
# project). Decided here, after the re-exec, so the path list is
# the one in the revision being deployed.
openclaw_gate_decide
if [[ "${OPENCLAW_GATE_RUN}" == "1" ]]; then
  say "OpenClaw phases: RUN (${OPENCLAW_GATE_REASON})"
else
  say "OpenClaw phases: SKIPPED (${OPENCLAW_GATE_REASON}); the gateway is left untouched"
fi

# One call per phase at its point in the sequence; a skipped phase is a no-op.
openclaw_phase() {
  if [[ "${OPENCLAW_GATE_RUN}" == "1" ]]; then "openclaw_phase_$1"; fi
}

# ─── OpenClaw: claw state, memory-audit sync, onboard, trustedProxies ────────
#
# scripts/deploy-openclaw.sh, phase `prepare`. Placed BEFORE the compose step
# on purpose so memory-audit script drift heals even on a deploy that fails
# later.
openclaw_phase prepare

# Grafana's frame-ancestors origin: an explicit GRAFANA_EMBED_ORIGIN wins,
# otherwise derived from the host's tailnet name + the dashboard's served
# HTTPS port (none derivable -> unset,
# embedding stays denied). Exported for compose interpolation only - the
# live env file is never written. Logic in scripts/deploy-embed-origin.sh.
say "grafana embed origin"
GRAFANA_EMBED_ORIGIN="$(ENV_FILE="${ENV_FILE}" "${REPO_DIR}/scripts/deploy-embed-origin.sh" || true)"
if [[ -n "${GRAFANA_EMBED_ORIGIN}" ]]; then
  export GRAFANA_EMBED_ORIGIN
else
  unset GRAFANA_EMBED_ORIGIN
fi

# ─── lifekit-dashboard: deployed from its own repo now (decoupling slice 2) ──
#
# The dashboard deploys from lifekit-hq/lifekit-dashboard's own deploy/
# (ghcr image + `dashboard` compose project + workflow_dispatch). This stack
# no longer clones or builds it, and the 5-minute redeploy timer is retired.

# ─── OpenClaw: modules.yaml → /srv/memory/system/ ────────────────────────────
#
# scripts/deploy-openclaw.sh, phase `modules`.
openclaw_phase modules

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

# Optional external dead-man heartbeat (docs/runbook.md "External heartbeat").
# Unset = disabled: deploy continues and the watchdog rule is deleted.
HEARTBEAT_URL="$(sed -nE 's/^[[:space:]]*LIFEKIT_EXTERNAL_HEARTBEAT_URL=["'"'"']?([^"'"'"'[:space:]]+)["'"'"']?[[:space:]]*$/\1/p' \
  "${ENV_FILE}" | head -1)"
bash "${REPO_DIR}/scripts/render-heartbeat.sh" "${ALERT_DIR}" "${HEARTBEAT_URL}"
if [[ -n "${HEARTBEAT_URL}" ]]; then say "external heartbeat: enabled"; else say "external heartbeat: disabled (LIFEKIT_EXTERNAL_HEARTBEAT_URL unset)"; fi

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
openclaw_compose --profile '*' config --format json \
  | python3 "${REPO_DIR}/scripts/platform-contract.py" --static -

# ─── Build + start ───────────────────────────────────────────────────────────

deploy_private_docker_config

# ─── OpenClaw: gateway image build + version-bump state migration ────────────
#
# scripts/deploy-openclaw.sh, phase `build`. Migrates state BEFORE the new
# gateway boots, so it runs ahead of `up`.
openclaw_phase build

say "docker compose up -d --build"
# The platform project defines no OpenClaw service (they are compose project
# `openclaw`, brought up by the `up` phase below), so this `up` never builds
# the gateway image or recreates its container, whatever the gate decided.
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

# ─── OpenClaw: move out of the platform project (once), up -d ───────────────
#
# scripts/deploy-openclaw.sh, phase `up`. OpenClaw is its own compose project
# (compose/openclaw/); the platform `up` above no longer defines or starts it.
openclaw_phase up

# ─── OpenClaw: platform config patch + gateway secrets reload/audit ──────────
#
# scripts/deploy-openclaw.sh, phase `configure`.
openclaw_phase configure

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

# ─── OpenClaw: cli reattach, stuck sessions, skill deps, health checks ───────
#
# scripts/deploy-openclaw.sh, phase `post-up`.
openclaw_phase post_up

# Both projects' containers, after the OpenClaw health checks above.
say "container status"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps
openclaw_compose ps

# ─── OpenClaw: smoke turns ───────────────────────────────────────────────────
#
# scripts/deploy-openclaw.sh, phase `smoke`.
openclaw_phase smoke

# ─── Platform contract: running containers ──────────────────────────────────
#
# After the smoke turns on purpose: they are real traffic, so the gateway has
# logged traced work, and Prometheus has scraped since the reload above. The
# platform and openclaw projects' containers gate the deploy (a failure goes red like any other
# post-deploy assertion - the containers are already up). The rest of the box
# prints as a report-only census: finance-sentry, devclaw and the dashboard
# deploy from their own repos and run this same script on their own project.
say "platform contract: running containers"
STACK_PROJECT="$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" config --format json \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])')"
python3 "${REPO_DIR}/scripts/platform-contract.py" --enforce "${STACK_PROJECT}" --enforce "${OPENCLAW_PROJECT}" \
  || fail_later "platform contract: ${STACK_PROJECT} or ${OPENCLAW_PROJECT} containers fail it (table above)"

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
  *) printf '\033[1;31m✗ docker builder cache cap: the running daemon does not enforce the repository daemon.json (cap + live-restore); apply it with the operator sequence in docs/runbook.md "Applying the Docker builder cache cap"\033[0m\n' >&2 ;;
esac

# ─── /tmp scratch policy: host fact the repo owns, report-only ──────────────
#
# scripts/tmp-scratch-policy.sh carries the policy (tmp.mount masked, /tmp
# live on disk, no age-only tmpfiles cleanup, liveness-gated sweep timer),
# but this account cannot apply it: moving a live tmpfs /tmp onto disk is a
# deliberate operator step, and the deploy has no sudo. Same
# shape as the Docker builder cache cap above - a host fact never turns the
# deploy red.
say "/tmp scratch policy (host fact, report-only)"
TMP_SCRATCH_STATUS=0
bash "${REPO_DIR}/scripts/tmp-scratch-policy.sh" --check || TMP_SCRATCH_STATUS=$?
case "${TMP_SCRATCH_STATUS}" in
  0) ;;
  2) warn "/tmp scratch policy: could not read the live mount state, undetermined (output above)" ;;
  *) printf '\033[1;31m✗ /tmp scratch policy: the box has not converged on the repository policy (/tmp off tmpfs, masked tmp.mount, liveness-gated scratch sweep); apply it with the operator sequence in docs/runbook.md "Moving /tmp off RAM (agent scratch)"\033[0m\n' >&2 ;;
esac

# ─── Docker prune timer: host fact the repo owns, report-only ───────────────
#
# scripts/docker-prune-policy.sh carries the nightly image + build-cache prune
# (14-day age floor). The deploy has no sudo, so it cannot install the timer;
# it only reports whether the box has it. Same shape as the two above.
say "docker prune timer (host fact, report-only)"
DOCKER_PRUNE_STATUS=0
bash "${REPO_DIR}/scripts/docker-prune-policy.sh" --check || DOCKER_PRUNE_STATUS=$?
if [[ "${DOCKER_PRUNE_STATUS}" != 0 ]]; then
  printf '\033[1;31m✗ docker prune timer: the box has not converged on the repository policy (installed copy, units, enabled timer); apply it with the root step in docs/runbook.md "Scheduled Docker image and build-cache prune"\033[0m\n' >&2
fi

# ─── Host firewall: host fact the repo owns, report-only ────────────────────
#
# scripts/host-firewall.sh carries the nftables baseline (lifekit-firewall
# unit + ruleset). The deploy has no sudo, and a firewall change on a live box
# goes through the timed-rollback cutover, never a deploy; it only reports
# whether the installed copy matches and the unit is enabled and active. Same
# shape as the three above.
say "host firewall (host fact, report-only)"
FIREWALL_STATUS=0
bash "${REPO_DIR}/scripts/host-firewall.sh" --check || FIREWALL_STATUS=$?
if [[ "${FIREWALL_STATUS}" != 0 ]]; then
  printf '\033[1;31m✗ host firewall: the box has not converged on the repository ruleset (installed copy, unit enabled and active, no nftables.service or ufw); apply it with the timed-rollback cutover in docs/runbook.md "Host firewall (nftables)"\033[0m\n' >&2
fi

if (( ${#DEPLOY_FAILURES[@]} )); then
  say "post-deploy assertions failed"
  printf '  - %s\n' "${DEPLOY_FAILURES[@]}" >&2
  exit 1
fi

DEPLOY_COMPLETE=1
# The next auto-mode gate diffs from here. Recorded only on a complete deploy,
# so a failed or superseded run never hides its OpenClaw change.
git update-ref "${OPENCLAW_LAST_DEPLOYED_REF}" HEAD \
  || warn "could not record ${OPENCLAW_LAST_DEPLOYED_REF}; the next deploy runs the OpenClaw phases"
say "✓ deploy complete."

}

main "$@"
