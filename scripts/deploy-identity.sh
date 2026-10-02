#!/usr/bin/env bash
# scripts/deploy-identity.sh — the identity provider's deploy-time helpers,
# called by deploy.sh around `up` of compose project `identity`
# (compose/identity/, docs/runbook.md "Identity provider (Logto)").
#
#   endpoints  print IDENTITY_ENDPOINT=<url> and IDENTITY_ADMIN_ENDPOINT=<url>,
#              one per line, for deploy.sh to export. An explicit value (shell
#              env or ENV_FILE) wins for each; otherwise https://<tailnet
#              name>:3001 and :3002, the name read from `tailscale status
#              --json` (Self.DNSName). Prints nothing when a URL cannot be
#              decided: Logto must never boot with a guessed issuer. Exit 0.
#   check      report-only host facts: Tailscale Serve publishes 3001 and 3002
#              over HTTPS to the loopback ports and NOT through Funnel, and the
#              default tenant's sign-in mode is SignIn (no self-registration).
#              Exit 0 when both hold, 1 on drift, each line on stdout says
#              which.
#
# Exercised with stubbed `tailscale` and `curl` in
# scripts/tests/test_deploy_identity.py.

set -uo pipefail

ENV_FILE="${ENV_FILE:-}"
CORE_PORT=3001
ADMIN_PORT=3002

env_value() {
  local name="$1" value="${!1:-}"
  if [[ -z "${value}" && -n "${ENV_FILE}" && -f "${ENV_FILE}" ]]; then
    value="$(sed -nE "s/^[[:space:]]*${name}=[\"']?([^\"']*)[\"']?[[:space:]]*$/\1/p" "${ENV_FILE}" | head -1)"
  fi
  printf '%s' "${value}"
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

endpoints() {
  local core admin name=""
  core="$(env_value IDENTITY_ENDPOINT)"
  admin="$(env_value IDENTITY_ADMIN_ENDPOINT)"
  if [[ -z "${core}" || -z "${admin}" ]]; then
    name="$(tailnet_name)"
    if [[ -z "${name}" ]]; then
      echo "identity endpoints: tailnet name not derivable and IDENTITY_ENDPOINT / IDENTITY_ADMIN_ENDPOINT not both set" >&2
      return 0
    fi
    core="${core:-https://${name}:${CORE_PORT}}"
    admin="${admin:-https://${name}:${ADMIN_PORT}}"
  fi
  printf 'IDENTITY_ENDPOINT=%s\nIDENTITY_ADMIN_ENDPOINT=%s\n' "${core}" "${admin}"
}

check() {
  local status=0 serve mode
  serve="$(tailscale serve status --json 2>/dev/null || true)"
  if ! printf '%s' "${serve}" | python3 -c '
import json, re, sys
ports = sys.argv[1:]
try:
    cfg = json.loads(sys.stdin.read() or "{}")
except Exception:
    print("  tailscale serve: status unreadable")
    sys.exit(1)
ok = True
for port in ports:
    tcp = cfg.get("TCP", {}).get(port, {})
    proxies = [
        h.get("Proxy", "")
        for key, web in cfg.get("Web", {}).items()
        if key.rpartition(":")[2] == port
        for h in web.get("Handlers", {}).values()
    ]
    funnel = any(v for k, v in cfg.get("AllowFunnel", {}).items() if k.rpartition(":")[2] == port)
    served = tcp.get("HTTPS") and any(
        re.fullmatch(r"https?://(127\.0\.0\.1|localhost):" + port + r"/?", p) for p in proxies
    )
    if funnel:
        print(f"  tailscale serve :{port}: FUNNEL IS ON (public); the identity ports are tailnet-only")
        ok = False
    elif served:
        print(f"  tailscale serve :{port}: https -> 127.0.0.1:{port}, tailnet-only")
    else:
        print(f"  tailscale serve :{port}: not published")
        ok = False
sys.exit(0 if ok else 1)
' "${CORE_PORT}" "${ADMIN_PORT}"; then
    status=1
  fi

  mode="$(curl -sf --max-time 10 "http://127.0.0.1:${CORE_PORT}/api/.well-known/sign-in-exp" 2>/dev/null \
    | python3 -c 'import json, sys; print(json.load(sys.stdin).get("signInMode", ""))' 2>/dev/null || true)"
  case "${mode}" in
    SignIn) echo "  sign-in mode: SignIn (no self-registration)" ;;
    "") echo "  sign-in mode: unreadable (is logto up?)"; status=1 ;;
    *) echo "  sign-in mode: ${mode}, expected SignIn (anyone who reaches the sign-in page can register)"; status=1 ;;
  esac
  return "${status}"
}

case "${1:-}" in
  endpoints) endpoints ;;
  check) check; exit $? ;;
  *) echo "usage: $0 <endpoints|check>" >&2; exit 2 ;;
esac
