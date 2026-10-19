#!/usr/bin/env bash
# scripts/openclaw-sso.sh - the OpenClaw gateway behind the tailnet sign-in
# gate (docs/runbook.md "OpenClaw behind the sign-in gate"): the operator's
# cutover from token auth to trusted-proxy auth, its one-command rollback, and
# a report-only check. Run as root from the deployed checkout:
#
#   sudo bash scripts/openclaw-sso.sh check
#   sudo bash scripts/openclaw-sso.sh cutover --operator-email <email>
#   sudo bash scripts/openclaw-sso.sh rollback
#
#   check     report-only: the gateway's auth mode and where Tailscale Serve's
#             :18789 points. Exit 0 when the two agree (token mode and Serve
#             at the gateway, or trusted-proxy mode and Serve at the edge), 1
#             otherwise.
#   cutover   checks everything it depends on before it changes anything
#             (the edge's routes answer, Prometheus already scrapes through
#             the edge, finance-sentry already calls through it, the local
#             password is rendered), saves what it is about to change to
#             ROLLBACK_FILE, then: stops the gateway, patches gateway.auth to
#             trusted-proxy (the given email is the only operator, with
#             operator.admin from identityScopes), trustedProxies to exactly
#             the edge, controlUi.allowedOrigins plus the tailnet URL, starts
#             the gateway without its published port, and points Serve's
#             :18789 at the edge. It then checks the result the same way and
#             rolls back by itself on any failure. The email is the Logto
#             user the gate admits (runbook step), never a value in this repo.
#   rollback  restores exactly what cutover saved (gateway.auth,
#             trustedProxies, controlUi.allowedOrigins, Serve's :18789
#             target) and starts the gateway in token mode, port published.
#             Without a saved file it restores token mode with the gateway
#             token's env reference and Serve at 127.0.0.1:18789, the
#             reference deployment's shape.
#
# The gateway restarts once in each direction (gateway.auth.mode is not
# hot-reloadable), like any deploy that recreates it. Exercised with stubbed
# docker, tailscale and curl in scripts/tests/test_openclaw_sso.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(dirname "${SCRIPT_DIR}")}"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"

SERVE_PORT=18789
EDGE_URL="http://127.0.0.1:18892"
GATEWAY_URL="http://127.0.0.1:${OPENCLAW_GATEWAY_PORT:-18789}"
ROLLBACK_FILE="${OPENCLAW_SSO_ROLLBACK_FILE:-$(dirname "${ENV_FILE}")/openclaw-sso-rollback.json}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://127.0.0.1:${PROMETHEUS_PORT:-9090}}"
POLL_TRIES="${OPENCLAW_SSO_POLL_TRIES:-30}"
POLL_INTERVAL="${OPENCLAW_SSO_POLL_INTERVAL:-5}"
CONFIG_FILE="${OPENCLAW_CONFIG_DIR}/openclaw.json"
# The route each kind of client takes (compose/edge/traefik/openclaw.yml): a
# desktop browser signs in; the phone app's dashboard tab (an iPhone
# WKWebView, no `Safari/`) goes straight to the gateway.
BROWSER_UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.7 Safari/605.1.15"
WEBVIEW_UA="Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
# The service identities compose/edge/traefik/openclaw.yml sends.
SERVICE_USERS='["service:prometheus","service:finance-sentry"]'
FINANCE_SENTRY_EDGE="openclaw-edge:18893"

require_root() {
  if [[ "$(id -u)" != 0 ]]; then
    echo "run as root (sudo): it changes Tailscale Serve and reads ${ENV_FILE}" >&2
    exit 2
  fi
}

# True when NAME has a non-empty value in ENV_FILE. Never prints it.
env_set() {
  grep -Eq "^[[:space:]]*$1=[\"']?[^\"'[:space:]]" "${ENV_FILE}" 2>/dev/null
}

tailnet_name() {
  tailscale status --json 2>/dev/null | python3 -c '
import json, re, sys
try:
    name = json.load(sys.stdin)["Self"]["DNSName"].rstrip(".").lower()
except Exception:
    sys.exit()
if re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", name):
    print(name)
' 2>/dev/null || true
}

# Serve's proxy target for SERVE_PORT, or "funnel" when the port is public,
# or nothing when it is not published.
serve_target() {
  tailscale serve status --json 2>/dev/null | python3 -c '
import json, sys
port = sys.argv[1]
try:
    cfg = json.loads(sys.stdin.read() or "{}")
except Exception:
    sys.exit()
if any(v for k, v in cfg.get("AllowFunnel", {}).items() if k.rpartition(":")[2] == port):
    print("funnel")
    sys.exit()
if not cfg.get("TCP", {}).get(port, {}).get("HTTPS"):
    sys.exit()
for key, web in cfg.get("Web", {}).items():
    if key.rpartition(":")[2] == port:
        for h in web.get("Handlers", {}).values():
            if h.get("Proxy"):
                print(h["Proxy"].rstrip("/"))
                sys.exit()
' "${SERVE_PORT}" 2>/dev/null || true
}

serve_to() {
  tailscale serve --bg --https="${SERVE_PORT}" "$1" >/dev/null
}

container_ip_on() {
  local id="$1" network="$2"
  [[ -n "${id}" ]] || return 0
  docker inspect -f "{{with index .NetworkSettings.Networks \"${network}\"}}{{.IPAddress}}{{end}}" "${id}" 2>/dev/null || true
}

http_code() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 10 -A "$1" "$2" 2>/dev/null || true
}

prometheus_up() {
  curl -s --max-time 10 --data-urlencode 'query=up{job="openclaw"}' "${PROMETHEUS_URL}/api/v1/query" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin)["data"]["result"][0]["value"][1])
except Exception:
    pass
' 2>/dev/null || true
}

# A fresh scrape needs up to one interval (30s) after a restart.
wait_prometheus_up() {
  local i
  for ((i = 0; i < POLL_TRIES; i++)); do
    [[ "$(prometheus_up)" == 1 ]] && return 0
    sleep "${POLL_INTERVAL}"
  done
  return 1
}

# Only this one variable leaves the container's environment: the template
# filters it inside docker. Prints "absent" when finance-sentry is not here.
finance_sentry_base_url() {
  docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "Agent__OpenClaw__BaseUrl"}}{{.}}{{end}}{{end}}' \
    finance-sentry-api 2>/dev/null | sed 's/^Agent__OpenClaw__BaseUrl=//' || echo absent
}

cli_health() {
  openclaw_compose exec -T openclaw-gateway node dist/index.js health --json >/dev/null 2>&1
}

# For the config patch with the gateway stopped: a clean stop releases the
# state, so the one-shot container can write it (replica, 2026-10-19).
openclaw_cli() {
  openclaw_compose run --rm --no-deps -T --entrypoint openclaw openclaw-gateway "$@"
}

# With the gateway running, OpenClaw >= 2026.10.2 refuses a one-shot's state
# read ("cannot verify Gateway ownership"), so the dry run goes through `exec`
# into it (scripts/lib/deploy-common.sh openclaw_gateway_running).
openclaw_exec() {
  openclaw_compose exec -T openclaw-gateway openclaw "$@"
}

check() {
  local mode target status=0
  mode="$(openclaw_auth_mode)"
  target="$(serve_target)"
  echo "  gateway auth mode: ${mode}"
  echo "  tailscale serve :${SERVE_PORT}: ${target:-not published}"
  case "${mode}:${target}" in
    token:"${GATEWAY_URL}" | token:http://localhost:"${OPENCLAW_GATEWAY_PORT:-18789}")
      echo "  token mode, Serve at the gateway: today's routing (the cutover has not run)" ;;
    trusted-proxy:"${EDGE_URL}" | trusted-proxy:http://localhost:18892)
      echo "  trusted-proxy mode, Serve at the edge: behind the sign-in gate" ;;
    *:funnel)
      echo "  FUNNEL IS ON for :${SERVE_PORT} (public); the gateway is tailnet-only"
      status=1 ;;
    trusted-proxy:*)
      echo "  trusted-proxy mode but Serve does not point at the edge (${EDGE_URL}): the tailnet URL is down"
      status=1 ;;
    *)
      echo "  token mode but Serve does not point at the gateway (${GATEWAY_URL})"
      status=1 ;;
  esac
  return "${status}"
}

preflight() {
  local email="$1" failures=() id ip fs code
  env_set OPENCLAW_GATEWAY_PASSWORD \
    || failures+=("OPENCLAW_GATEWAY_PASSWORD is not in ${ENV_FILE} (docs/secrets.md; render after the merge)")
  env_set OPENCLAW_GATEWAY_TOKEN \
    || failures+=("OPENCLAW_GATEWAY_TOKEN is not in ${ENV_FILE}: the edge's service routes match on it")
  [[ ! -e "${ROLLBACK_FILE}" ]] \
    || failures+=("${ROLLBACK_FILE} exists from an earlier cutover; run rollback first, or remove it if that state is gone")
  python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "${CONFIG_FILE}" 2>/dev/null \
    || failures+=("${CONFIG_FILE} is not readable strict JSON")
  [[ -n "$(tailnet_name)" ]] || failures+=("the tailnet name is not readable (tailscale status)")
  case "${SERVE_TARGET}" in
    "") failures+=("Serve does not publish :${SERVE_PORT}") ;;
    funnel) failures+=("Funnel is on for :${SERVE_PORT}; the gateway is tailnet-only") ;;
  esac
  id="$(edge_compose ps -q traefik 2>/dev/null || true)"
  ip="$(container_ip_on "${id}" "${OPENCLAW_INGRESS_NETWORK}")"
  [[ "${ip}" == "${OPENCLAW_EDGE_IP}" ]] \
    || failures+=("the edge's traefik is not running at ${OPENCLAW_EDGE_IP} on ${OPENCLAW_INGRESS_NETWORK} (got '${ip}'); deploy first")
  [[ -n "$(edge_compose ps -q --status running oauth2-proxy 2>/dev/null || true)" ]] \
    || failures+=("the edge's oauth2-proxy is not running: browsers could not sign in (runbook \"Tailnet sign-in gate\")")
  openclaw_gateway_running \
    || failures+=("the gateway is not running; the dry run of the patch runs inside it (deploy first)")
  id="$(openclaw_compose ps -q openclaw-gateway 2>/dev/null || true)"
  [[ -n "$(container_ip_on "${id}" "${OPENCLAW_INGRESS_NETWORK}")" ]] \
    || failures+=("the gateway is not on ${OPENCLAW_INGRESS_NETWORK}; deploy first")
  code="$(http_code "${BROWSER_UA}" "${EDGE_URL}/")"
  [[ "${code}" == 302 ]] || failures+=("a browser at ${EDGE_URL}/ got ${code:-no answer}, not the 302 to sign-in")
  code="$(http_code "${WEBVIEW_UA}" "${EDGE_URL}/")"
  [[ "${code}" == 200 ]] || failures+=("the phone app's dashboard tab at ${EDGE_URL}/ got ${code:-no answer}, not 200")
  [[ "$(prometheus_up)" == 1 ]] \
    || failures+=("Prometheus does not see up{job=\"openclaw\"} == 1 through the edge")
  fs="$(finance_sentry_base_url)"
  if [[ "${fs}" != absent && "${fs}" != *"${FINANCE_SENTRY_EDGE}"* ]]; then
    failures+=("finance-sentry still calls the gateway at '${fs:-unset}', not http://${FINANCE_SENTRY_EDGE} (runbook step)")
  fi
  if ((${#failures[@]})); then
    echo "cutover: not started, nothing changed:" >&2
    printf '  - %s\n' "${failures[@]}" >&2
    return 1
  fi
  echo "  preflight ok: edge routes answer, Prometheus and finance-sentry go through the edge, operator ${email}"
}

save_rollback() {
  ( umask 077 && python3 - "${CONFIG_FILE}" "${SERVE_TARGET}" >"${ROLLBACK_FILE}" <<'PY'
import json, sys
gw = json.load(open(sys.argv[1])).get("gateway") or {}
print(json.dumps({
    "auth": gw.get("auth"),
    "trustedProxies": gw.get("trustedProxies"),
    "allowedOrigins": (gw.get("controlUi") or {}).get("allowedOrigins"),
    "serveTarget": sys.argv[2],
}, indent=2))
PY
  )
}

cutover_patch() {
  python3 - "${CONFIG_FILE}" "$1" "$(tailnet_name)" "${OPENCLAW_EDGE_IP}" "${SERVICE_USERS}" <<'PY'
import json, sys
config, email, name, edge_ip, services = sys.argv[1:6]
gw = json.load(open(config)).get("gateway") or {}
origins = list((gw.get("controlUi") or {}).get("allowedOrigins") or [])
url = f"https://{name}:18789"
if url not in origins:
    origins.append(url)
print(json.dumps({"gateway": {
    "auth": {
        "mode": "trusted-proxy",
        "token": None,
        "password": "${OPENCLAW_GATEWAY_PASSWORD}",
        "trustedProxy": {
            "userHeader": "x-auth-request-email",
            "allowUsers": [email, *json.loads(services)],
            "deviceAutoApprove": {"enabled": True},
        },
        "identityScopes": {email: ["operator.admin"]},
    },
    "trustedProxies": [edge_ip],
    "controlUi": {"allowedOrigins": origins},
}}))
PY
}

verify_cutover() {
  local failures=() code target
  cli_health || failures+=("the in-container CLI cannot reach the gateway with the local password")
  code="$(http_code "${BROWSER_UA}" "${EDGE_URL}/")"
  [[ "${code}" == 302 ]] || failures+=("a browser got ${code:-no answer}, not the 302 to sign-in")
  code="$(http_code "${WEBVIEW_UA}" "${EDGE_URL}/")"
  [[ "${code}" == 200 ]] || failures+=("the phone app's dashboard tab got ${code:-no answer}, not 200")
  target="$(serve_target)"
  [[ "${target}" == "${EDGE_URL}" ]] || failures+=("Serve :${SERVE_PORT} points at '${target}', not ${EDGE_URL}")
  wait_prometheus_up || failures+=("Prometheus does not see up{job=\"openclaw\"} == 1 after the restart")
  if ((${#failures[@]})); then
    printf '  - %s\n' "${failures[@]}" >&2
    return 1
  fi
}

cutover() {
  local email=""
  while (($#)); do
    case "$1" in
      --operator-email) email="${2:-}"; shift; shift || true ;;
      *) echo "usage: $0 cutover --operator-email <email>" >&2; exit 2 ;;
    esac
  done
  email="${email,,}"
  if [[ ! "${email}" =~ ^[^@[:space:]\"]+@[^@[:space:]\"]+\.[^@[:space:]\"]+$ ]]; then
    echo "usage: $0 cutover --operator-email <email>: the Logto user the gate admits (runbook)" >&2
    exit 2
  fi
  require_root
  deploy_require_env_file
  if [[ "$(openclaw_auth_mode)" == trusted-proxy ]]; then
    echo "cutover: the gateway is already in trusted-proxy mode; nothing to do"
    check
    return
  fi
  SERVE_TARGET="$(serve_target)"

  say "openclaw sso cutover: preflight (changes nothing)"
  preflight "${email}"

  say "openclaw sso cutover: saving the current state to ${ROLLBACK_FILE}"
  save_rollback
  local patch
  patch="$(cutover_patch "${email}")"

  say "openclaw sso cutover: dry run of the config patch (writes nothing)"
  if ! printf '%s\n' "${patch}" | openclaw_exec config patch --stdin --dry-run --allow-exec; then
    rm -f "${ROLLBACK_FILE}"
    echo "cutover: the patch was rejected; nothing changed" >&2
    return 1
  fi

  say "openclaw sso cutover: switching the gateway to trusted-proxy auth"
  if ! {
    openclaw_compose stop openclaw-gateway \
      && printf '%s\n' "${patch}" | OPENCLAW_COMPOSE_AUTH_MODE=trusted-proxy openclaw_cli config patch --stdin \
      && openclaw_compose up -d --no-deps --wait --wait-timeout 180 openclaw-gateway \
      && serve_to "${EDGE_URL}"
  }; then
    warn "cutover: a step failed; rolling back"
    rollback
    return 1
  fi

  say "openclaw sso cutover: checking the result"
  if ! verify_cutover; then
    warn "cutover: the result is not right (above); rolling back"
    rollback
    return 1
  fi
  cat <<EOF

✓ The gateway is behind the sign-in gate. Live check on the phone now:
  open the OpenClaw app and confirm it shows the gateway connected and the
  dashboard tab loads. Then open https://$(tailnet_name):${SERVE_PORT}/ in a
  browser: Logto's sign-in, then the dashboard, with no token asked for.
  If the phone does not connect: sudo bash scripts/openclaw-sso.sh rollback
EOF
}

# Keeps ROLLBACK_FILE: re-running rollback once the cause is fixed restores
# the same state.
rollback_failed() {
  echo "rollback: $1 failed; the saved state stays in ${ROLLBACK_FILE}: fix the cause and re-run: sudo bash scripts/openclaw-sso.sh rollback" >&2
  exit 1
}

rollback() {
  require_root
  deploy_require_env_file
  local saved target patch
  if [[ -f "${ROLLBACK_FILE}" ]]; then
    saved="$(cat "${ROLLBACK_FILE}")"
  else
    warn "rollback: no ${ROLLBACK_FILE}; restoring token mode with the gateway token's env reference and Serve at ${GATEWAY_URL}"
    saved='{"auth":{"mode":"token","token":{"source":"env","provider":"default","id":"OPENCLAW_GATEWAY_TOKEN"}},"trustedProxies":null,"serveTarget":""}'
  fi
  target="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1]).get("serveTarget") or "")' "${saved}")"
  target="${target:-${GATEWAY_URL}}"
  patch="$(python3 -c '
import json, sys
s = json.loads(sys.argv[1])
gw = {"auth": s.get("auth") or {"mode": "token"}}
if "allowedOrigins" in s:  # null: there was none, so the added list goes
    gw["controlUi"] = {"allowedOrigins": s["allowedOrigins"]}
if s.get("trustedProxies") is not None:
    gw["trustedProxies"] = s["trustedProxies"]
print(json.dumps({"gateway": gw}))
' "${saved}")"

  say "openclaw sso rollback: restoring token auth"
  openclaw_compose stop openclaw-gateway || true
  printf '%s\n' "${patch}" | OPENCLAW_COMPOSE_AUTH_MODE=token openclaw_cli config patch --stdin --replace-path gateway.auth \
    || rollback_failed "the config patch back to token auth"
  # Without a saved list, the deploy's own derivation puts the host's proxy
  # address back (scripts/deploy-trusted-proxies.sh, token mode).
  if [[ "$(python3 -c 'import json, sys; print(json.loads(sys.argv[1]).get("trustedProxies") is None)' "${saved}")" == True ]]; then
    ENV_FILE="${ENV_FILE}" COMPOSE_FILE="${OPENCLAW_COMPOSE_FILE}" COMPOSE_PROJECT_NAME="${OPENCLAW_PROJECT}" \
      OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR}" OPENCLAW_AUTH_MODE=token \
      bash "${REPO_DIR}/scripts/deploy-trusted-proxies.sh" \
      || rollback_failed "restoring gateway.trustedProxies"
  fi
  openclaw_compose up -d --no-deps --wait --wait-timeout 180 openclaw-gateway \
    || rollback_failed "starting the gateway in token mode (openclaw_compose ps / logs openclaw-gateway)"
  serve_to "${target}" || rollback_failed "pointing Serve :${SERVE_PORT} back at ${target}"
  rm -f "${ROLLBACK_FILE}"
  echo "✓ rolled back: token mode, Serve :${SERVE_PORT} -> ${target}"
  check || true
}

case "${1:-}" in
  check) check; exit $? ;;
  cutover) shift; cutover "$@" ;;
  rollback) rollback ;;
  *) echo "usage: $0 <check|cutover --operator-email <email>|rollback>" >&2; exit 2 ;;
esac
