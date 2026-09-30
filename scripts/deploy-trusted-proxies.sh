#!/usr/bin/env bash
# scripts/deploy-trusted-proxies.sh — make gateway.trustedProxies list the
# gateway address of the OpenClaw compose project's default network, on
# every deploy. Called from deploy-openclaw.sh's `prepare` phase; a separate
# script so it can be exercised directly with a stubbed `docker` on PATH (see
# scripts/tests/test_deploy_trusted_proxies.py) instead of only read as text.
#
# Since OpenClaw 2026.9.x the gateway rejects proxied requests with
# proxy_attribution_required unless the proxy's source address is listed in
# gateway.trustedProxies. A proxy on the host (Tailscale Serve) reaches the
# gateway through its published loopback port, so the source the gateway sees
# is the Docker bridge gateway of its default-route network: the project's
# `default` network (gw_priority in compose/openclaw/docker-compose.yml).
# That address is Docker-assigned at network creation, so it can't be pinned
# in compose/openclaw-gateway/platform.patch.json (host-derived, not a static
# platform key) — it's derived here, from a docker-network-inspect lookup.
#
# Converges rather than sets once: the derived address is appended when the
# list lacks it, and every address already there stays (a hand-added proxy,
# or the platform project's compose_default address the gateway used before
# it moved to its own project - kept so a rollback to that layout still
# works). A list that already holds the address is left untouched, so every
# later deploy writes nothing. Not gated on first-deploy-ness: a deploy that
# dies between `onboard` writing openclaw.json and this script running is
# retried on the next attempt.
#
# The network is created on the first deploy of the project, before any of
# its services run: a one-shot `run --rm --entrypoint true` of the gateway
# image creates the project's networks as a side effect (it publishes no
# ports and starts nothing else), so the gateway's first start already has
# its proxy address in config and needs no second recreate.

set -euo pipefail

ENV_FILE="${ENV_FILE:?ENV_FILE must be set}"
COMPOSE_FILE="${COMPOSE_FILE:?COMPOSE_FILE must be set}"
OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR:?OPENCLAW_CONFIG_DIR must be set}"

COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$(dirname "${COMPOSE_FILE}")")}"
project_compose() {
  docker compose -p "${COMPOSE_PROJECT}" --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

CONFIG_FILE="${OPENCLAW_CONFIG_DIR}/openclaw.json"

# Cheapest read first: the host config file is strict JSON on a normal
# deploy (deploy-openclaw.sh reads it the same way for the platform-patch
# compare).
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
  CURRENT_TRUSTED_PROXIES="$(project_compose \
    run --rm --no-deps --entrypoint openclaw openclaw-gateway \
      config get gateway.trustedProxies 2>/dev/null | tr -d '[:space:]' || echo UNREADABLE)"
fi

if [[ "${CURRENT_TRUSTED_PROXIES}" == *UNREADABLE* ]]; then
  echo "Could not read gateway.trustedProxies from ${CONFIG_FILE} or via 'openclaw config get'." >&2
  echo "gateway.trustedProxies was not set — the gateway will reject all proxied" >&2
  echo "requests with proxy_attribution_required. Fix config access and re-run deploy.sh; it is idempotent." >&2
  exit 1
fi

TRUSTED_PROXY_NETWORK="${COMPOSE_PROJECT}_default"
network_gateway() {
  docker network inspect "${TRUSTED_PROXY_NETWORK}" \
    --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true
}
TRUSTED_PROXY_ADDR="$(network_gateway)"
if [[ -z "${TRUSTED_PROXY_ADDR}" ]]; then
  echo "openclaw trustedProxies: creating ${TRUSTED_PROXY_NETWORK} (one-shot no-op run, starts no service)"
  project_compose run --rm --no-deps --entrypoint true openclaw-gateway >/dev/null || true
  TRUSTED_PROXY_ADDR="$(network_gateway)"
fi

if [[ -z "${TRUSTED_PROXY_ADDR}" ]]; then
  echo "Could not determine the gateway address of docker network ${TRUSTED_PROXY_NETWORK}." >&2
  echo "gateway.trustedProxies was not set — the gateway will reject all proxied" >&2
  echo "requests with proxy_attribution_required. Fix docker networking and re-run deploy.sh; it is idempotent." >&2
  exit 1
fi

# Prints the new list when the address is missing, nothing when it is already
# there, INVALID when the current value is not a list.
WANTED_TRUSTED_PROXIES="$(python3 -c '
import json, sys
try:
    current = json.loads(sys.argv[1]) if sys.argv[1] else None
except ValueError:
    current = "INVALID"
current = [] if current in (None, {}) else current
if not isinstance(current, list):
    print("INVALID")
elif sys.argv[2] not in current:
    print(json.dumps(current + [sys.argv[2]], separators=(",", ":")))
' "${CURRENT_TRUSTED_PROXIES}" "${TRUSTED_PROXY_ADDR}")"

case "${WANTED_TRUSTED_PROXIES}" in
  "")
    echo "openclaw trustedProxies: already lists ${TRUSTED_PROXY_ADDR} (from ${TRUSTED_PROXY_NETWORK}); leaving it alone"
    exit 0
    ;;
  INVALID)
    echo "gateway.trustedProxies is not a list (${CURRENT_TRUSTED_PROXIES}); not changed." >&2
    echo "Fix it by hand and re-run deploy.sh; it is idempotent." >&2
    exit 1
    ;;
esac

echo "openclaw trustedProxies: adding ${TRUSTED_PROXY_ADDR} (from ${TRUSTED_PROXY_NETWORK})"
project_compose \
  run --rm --no-deps --entrypoint openclaw openclaw-gateway \
    config set gateway.trustedProxies "${WANTED_TRUSTED_PROXIES}" --strict-json
