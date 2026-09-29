# shellcheck shell=bash
# scripts/lib/deploy-common.sh — config defaults and helpers shared by
# scripts/deploy.sh and scripts/deploy-openclaw.sh.
#
# Sourced, never executed. Sourcing it a second time is a no-op, so both
# scripts source it and deploy.sh can also source deploy-openclaw.sh without
# resetting the step name or the queued failures.

[[ -z "${_LIFEKIT_DEPLOY_COMMON:-}" ]] || return 0
_LIFEKIT_DEPLOY_COMMON=1

REPO_DIR="${REPO_DIR:-/srv/lifekit-stack}"
ENV_FILE="${ENV_FILE:-/srv/lifekit-secrets/stack.env}"
OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
# shellcheck disable=SC2034  # used by the scripts that source this file
COMPOSE_FILE="${REPO_DIR}/compose/docker-compose.yml"
# OpenClaw (gateway, cli, google-workspace-mcp) is its own compose project,
# apart from the platform project in COMPOSE_FILE. Every OpenClaw compose call
# goes through openclaw_compose, so the project name, file and env file are
# decided in one place.
OPENCLAW_PROJECT="openclaw"
OPENCLAW_COMPOSE_FILE="${REPO_DIR}/compose/openclaw/docker-compose.yml"
openclaw_compose() {
  docker compose -p "${OPENCLAW_PROJECT}" --env-file "${ENV_FILE}" -f "${OPENCLAW_COMPOSE_FILE}" "$@"
}

# CURRENT_STEP is read by the callers' EXIT traps.
# shellcheck disable=SC2034
CURRENT_STEP="startup"
DEPLOY_FAILURES=()
# shellcheck disable=SC2034
say() { CURRENT_STEP="$*"; printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }
# Post-deploy assertions do not abort mid-flight (a half-deployed box is
# worse than a deployed one with a red log); they queue here and fail the
# run at the end so CI goes red and the log says exactly what is wrong.
fail_later() { DEPLOY_FAILURES+=("$*"); printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }

deploy_require_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    echo "Missing ${ENV_FILE}. Render it from the master secrets boundary:" >&2
    echo "  sudo bash scripts/secrets/render-stack-env.sh" >&2
    exit 1
  fi
}

# Private, empty Docker config for every docker call from here on. The
# dashboard and devclaw deploy jobs `docker login ghcr.io` with their
# job-scoped GITHUB_TOKEN into the shared lifekit config and never log out;
# once that token expires every ghcr pull through that config fails with
# "failed to fetch oauth token: denied" (2026-09-15/16). All images this
# stack pulls are public, so anonymous pulls are enough. The caller's EXIT
# trap removes DEPLOY_DOCKER_CONFIG.
#
# Buildx's default provenance attestation wraps every build's output in a
# fresh manifest list carrying a build timestamp, so even a 100%-cache-hit
# rebuild (no Dockerfile/context/base-image change) gets a new top-level
# digest and `up -d --build` recreates the container on every deploy
# (lifekit-stack fm/ls-deploy-no-rebuild investigation: CI runs
# 36536047624/36474931829/36452583710/36550718880 each showed every layer
# CACHED yet a new attestation + manifest list every time). Suppressing it
# here - rather than a compose-file `build.provenance: false` key - because
# the GitHub-hosted CI runner's older docker compose rejects that key as an
# unknown schema property (#221); this env var is honored by `docker compose
# build`/`up --build` themselves (verified with a throwaway-tag build, not
# just bare `docker buildx build`), covering both the gateway build in
# deploy-openclaw.sh and deploy.sh's `up -d --build`.
deploy_private_docker_config() {
  DEPLOY_DOCKER_CONFIG="$(mktemp -d)"
  export DOCKER_CONFIG="${DEPLOY_DOCKER_CONFIG}"
  export BUILDX_NO_DEFAULT_ATTESTATIONS=1
}
