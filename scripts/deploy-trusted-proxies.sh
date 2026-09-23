#!/usr/bin/env bash
# scripts/deploy-trusted-proxies.sh — derive and set gateway.trustedProxies
# once `openclaw onboard` has created the compose project's default network,
# retrying on every deploy until the key holds a value. Extracted out of
# deploy.sh's onboard block so it can be exercised directly with a stubbed
# `docker` on PATH (see scripts/tests/test_deploy_trusted_proxies.py)
# instead of only read as text.
#
# Since OpenClaw 2026.9.x the gateway rejects proxied requests with
# proxy_attribution_required unless the proxy's source address is listed in
# gateway.trustedProxies. That address is Docker-assigned at network
# creation, so it can't be pinned in
# compose/openclaw-gateway/platform.patch.json (host-derived, not a static
# platform key) — it's derived here, from a docker-network-inspect lookup.
#
# Not gated on first-deploy-ness: a deploy that dies between `onboard`
# writing openclaw.json and this script running would otherwise never get
# retried, since openclaw.json already exists on the next attempt. Instead
# this reads the live value first and no-ops once the key already holds
# one, so an already-configured host (and every later deploy) is left
# untouched.

set -euo pipefail

ENV_FILE="${ENV_FILE:?ENV_FILE must be set}"
COMPOSE_FILE="${COMPOSE_FILE:?COMPOSE_FILE must be set}"
OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR:?OPENCLAW_CONFIG_DIR must be set}"

CONFIG_FILE="${OPENCLAW_CONFIG_DIR}/openclaw.json"

# Cheapest read first: the host config file is strict JSON on a normal
# deploy (deploy.sh reads it the same way for the platform-patch compare).
# Only fall back to a live `config get` — a second one-shot container run —
# when the file is missing or a hand edit left it non-strict JSON.
CURRENT_TRUSTED_PROXIES="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
except Exception:
    print("UNREADABLE")
    sys.exit()
print(json.dumps(data.get("gateway", {}).get("trustedProxies")))
' "${CONFIG_FILE}" 2>/dev/null || echo UNREADABLE)"

if [[ "${CURRENT_TRUSTED_PROXIES}" == "UNREADABLE" ]]; then
  CURRENT_TRUSTED_PROXIES="$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
    run --rm --no-deps --entrypoint openclaw openclaw-gateway \
      config get gateway.trustedProxies 2>/dev/null | tr -d '[:space:]' || echo UNREADABLE)"
fi

case "${CURRENT_TRUSTED_PROXIES}" in
  UNREADABLE)
    echo "Could not read gateway.trustedProxies from ${CONFIG_FILE} or via 'openclaw config get'." >&2
    echo "gateway.trustedProxies was not set — the gateway will reject all proxied" >&2
    echo "requests with proxy_attribution_required. Fix config access and re-run deploy.sh; it is idempotent." >&2
    exit 1
    ;;
  ""|null|"[]"|"{}")
    ;; # absent or empty — derive and set it below
  *)
    echo "openclaw trustedProxies: already set (${CURRENT_TRUSTED_PROXIES}); leaving it alone"
    exit 0
    ;;
esac

COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$(dirname "${COMPOSE_FILE}")")}"
TRUSTED_PROXY_NETWORK="${COMPOSE_PROJECT}_default"
TRUSTED_PROXY_ADDR="$(docker network inspect "${TRUSTED_PROXY_NETWORK}" \
  --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"

if [[ -z "${TRUSTED_PROXY_ADDR}" ]]; then
  echo "Could not determine the gateway address of docker network ${TRUSTED_PROXY_NETWORK}." >&2
  echo "gateway.trustedProxies was not set — the gateway will reject all proxied" >&2
  echo "requests with proxy_attribution_required. Fix docker networking and re-run deploy.sh; it is idempotent." >&2
  exit 1
fi

echo "openclaw trustedProxies (${TRUSTED_PROXY_ADDR}, from ${TRUSTED_PROXY_NETWORK})"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
  run --rm --no-deps --entrypoint openclaw openclaw-gateway \
    config set gateway.trustedProxies "[\"${TRUSTED_PROXY_ADDR}\"]" --strict-json
