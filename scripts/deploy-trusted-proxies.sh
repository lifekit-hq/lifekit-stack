#!/usr/bin/env bash
# scripts/deploy-trusted-proxies.sh — derive and set gateway.trustedProxies
# on first deploy, once `openclaw onboard` has created the compose project's
# default network. Extracted out of deploy.sh's onboard block so it can be
# exercised directly with a stubbed `docker` on PATH (see
# scripts/tests/test_deploy_trusted_proxies.py) instead of only read as text.
#
# Since OpenClaw 2026.9.x the gateway rejects proxied requests with
# proxy_attribution_required unless the proxy's source address is listed in
# gateway.trustedProxies. That address is Docker-assigned at network
# creation, so it can't be pinned in
# compose/openclaw-gateway/platform.patch.json (host-derived, not a static
# platform key) — it's derived here, from a docker-network-inspect lookup.

set -euo pipefail

ENV_FILE="${ENV_FILE:?ENV_FILE must be set}"
COMPOSE_FILE="${COMPOSE_FILE:?COMPOSE_FILE must be set}"

COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$(dirname "${COMPOSE_FILE}")")}"
TRUSTED_PROXY_NETWORK="${COMPOSE_PROJECT}_default"
TRUSTED_PROXY_ADDR="$(docker network inspect "${TRUSTED_PROXY_NETWORK}" \
  --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"

if [[ -z "${TRUSTED_PROXY_ADDR}" ]]; then
  echo "Could not determine the gateway address of docker network ${TRUSTED_PROXY_NETWORK}." >&2
  echo "gateway.trustedProxies was not set — the gateway will reject all proxied" >&2
  echo "requests with proxy_attribution_required. Fix docker networking and re-run." >&2
  exit 1
fi

echo "openclaw trustedProxies (${TRUSTED_PROXY_ADDR}, from ${TRUSTED_PROXY_NETWORK})"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
  run --rm --no-deps --entrypoint openclaw openclaw-gateway \
    config set gateway.trustedProxies "[\"${TRUSTED_PROXY_ADDR}\"]" --strict-json
