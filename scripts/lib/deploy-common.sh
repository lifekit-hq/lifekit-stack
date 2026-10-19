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
# The gateway's auth mode picks the compose shape. In `token` mode (today's,
# and every fresh host's) the gateway publishes 127.0.0.1:18789 and holds the
# shared token. In `trusted-proxy` mode (OpenClaw behind the sign-in gate,
# docs/runbook.md "OpenClaw behind the sign-in gate") the gateway refuses to
# start while any shared token is configured, and only the edge may reach it,
# so compose/openclaw/trusted-proxy.yml blanks the token, adds the local
# password and drops the published port. The mode is read from the live
# openclaw.json on every call, so the deploy follows whatever the operator's
# cutover or rollback left there; OPENCLAW_COMPOSE_AUTH_MODE overrides it.
OPENCLAW_TRUSTED_PROXY_FILE="${OPENCLAW_TRUSTED_PROXY_FILE:-${REPO_DIR}/compose/openclaw/trusted-proxy.yml}"
openclaw_auth_mode() {
  if [[ -n "${OPENCLAW_COMPOSE_AUTH_MODE:-}" ]]; then
    printf '%s\n' "${OPENCLAW_COMPOSE_AUTH_MODE}"
    return 0
  fi
  python3 - "${OPENCLAW_CONFIG_DIR}/openclaw.json" <<'PY'
import json, re, sys
path = sys.argv[1]
try:
    with open(path) as f:
        raw = f.read()
except FileNotFoundError:  # first deploy: onboard writes a token-mode config
    print("token")
    sys.exit()
except OSError as e:
    print(f"cannot read {path} ({e.strerror}); run as the deploy account (lifekit) or root", file=sys.stderr)
    sys.exit(1)
try:
    mode = ((json.loads(raw).get("gateway") or {}).get("auth") or {}).get("mode")
except ValueError:  # a hand edit left JSON5; the mode string is still there
    mode = "trusted-proxy" if re.search(r"""["']?mode["']?\s*:\s*["']trusted-proxy["']""", raw) else None
print("trusted-proxy" if mode == "trusted-proxy" else "token")
PY
}
openclaw_compose() {
  local mode files=(-f "${OPENCLAW_COMPOSE_FILE}")
  mode="$(openclaw_auth_mode)" || return 1
  if [[ "${mode}" == "trusted-proxy" ]]; then
    files+=(-f "${OPENCLAW_TRUSTED_PROXY_FILE}")
  fi
  docker compose -p "${OPENCLAW_PROJECT}" --env-file "${ENV_FILE}" "${files[@]}" "$@"
}

# The one network between the edge's Traefik and the gateway
# (compose/edge/docker-compose.yml, compose/openclaw/docker-compose.yml).
# Internal (no route out), and Traefik's address on it is pinned: in
# trusted-proxy mode it is the gateway's only gateway.trustedProxies entry, so
# no other container can present a signed-in identity. Created once, before
# either project's `up`, like identity-oidc.
OPENCLAW_INGRESS_NETWORK="openclaw-ingress"
OPENCLAW_INGRESS_SUBNET="10.231.78.0/28"
OPENCLAW_INGRESS_IP_RANGE="10.231.78.8/29"
# shellcheck disable=SC2034  # used by deploy-trusted-proxies.sh and openclaw-sso.sh
OPENCLAW_EDGE_IP="10.231.78.2"
openclaw_ingress_network_ensure() {
  docker network inspect "${OPENCLAW_INGRESS_NETWORK}" >/dev/null 2>&1 \
    || docker network create --internal \
      --subnet "${OPENCLAW_INGRESS_SUBNET}" --ip-range "${OPENCLAW_INGRESS_IP_RANGE}" \
      "${OPENCLAW_INGRESS_NETWORK}" >/dev/null
}
# True while the project's gateway container is running. OpenClaw >= 2026.10.2
# refuses CLI calls that read or write live gateway state from a one-shot
# `run --rm` container ("cannot verify Gateway ownership from this process
# (different PID namespace)"); those calls use `exec` into this container
# instead, and check this first so a missing gateway is a clear failure.
openclaw_gateway_running() {
  [[ -n "$(openclaw_compose ps -q --status running openclaw-gateway 2>/dev/null)" ]]
}

# The org identity provider (Logto + its Postgres) is compose project
# `identity` (compose/identity/), brought up by deploy.sh once its secret is
# in ENV_FILE. Same one-place rule as openclaw_compose.
IDENTITY_PROJECT="identity"
IDENTITY_COMPOSE_FILE="${REPO_DIR}/compose/identity/docker-compose.yml"
identity_compose() {
  docker compose -p "${IDENTITY_PROJECT}" --env-file "${ENV_FILE}" -f "${IDENTITY_COMPOSE_FILE}" "$@"
}

# The tailnet sign-in gate (Traefik + oauth2-proxy, an OIDC client of the
# identity project) is compose project `edge` (compose/edge/), brought up by
# deploy.sh once identity is up and the edge's secrets are in ENV_FILE.
EDGE_PROJECT="edge"
EDGE_COMPOSE_FILE="${REPO_DIR}/compose/edge/docker-compose.yml"
edge_compose() {
  docker compose -p "${EDGE_PROJECT}" --env-file "${ENV_FILE}" -f "${EDGE_COMPOSE_FILE}" "$@"
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
