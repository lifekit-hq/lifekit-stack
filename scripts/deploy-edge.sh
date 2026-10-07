#!/usr/bin/env bash
# scripts/deploy-edge.sh - the tailnet sign-in gate's deploy-time helpers,
# called by deploy.sh around `up` of compose project `edge` (compose/edge/,
# docs/runbook.md "Tailnet sign-in gate").
#
#   domains  print EDGE_REDIRECT_DOMAINS=<list> for deploy.sh to export: where
#            oauth2-proxy may redirect after sign-in or sign-out. Every port of
#            this host's tailnet name (`<name>:*`, the gated surfaces), plus
#            the host:port of IDENTITY_ENDPOINT (Logto's end-session URL) when
#            the issuer lives elsewhere. Prints nothing when the tailnet name
#            cannot be read: the gate must never boot unable to send a user
#            back. Exit 0.
#   check    report-only host facts: Tailscale Serve sends each gated surface's
#            tailnet port to its edge entrypoint (18790 -> 127.0.0.1:18890,
#            18791 -> 127.0.0.1:18891), tailnet-only, NOT through Funnel; and
#            DEVCLAW_MCP_TOKEN is set, or the console gets an empty bearer; and
#            RELAY_EDGE_PROOF is set, or the dashboard's relay endpoint answers
#            503 (it has no proof to check).
#            Exit 0 when all hold, 1 otherwise; each line on stdout says which.
#
# Exercised with a stubbed `tailscale` in scripts/tests/test_deploy_edge.py.

set -uo pipefail

ENV_FILE="${ENV_FILE:-}"
# surface tailnet port -> edge entrypoint loopback port
GATED_PORTS="18790:18890 18791:18891"

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

domains() {
  local name identity
  name="$(tailnet_name)"
  if [[ -z "${name}" ]]; then
    echo "edge redirect domains: tailnet name not derivable" >&2
    return 0
  fi
  identity="$(env_value IDENTITY_ENDPOINT)"
  python3 -c '
import sys
from urllib.parse import urlsplit
name, identity = sys.argv[1], sys.argv[2]
out = [f"{name}:*"]
try:
    url = urlsplit(identity)
    host, port = (url.hostname or "").lower(), url.port
except ValueError:
    host, port = "", None
if host and host != name:
    out.append(f"{host}:{port}" if port else host)
print("EDGE_REDIRECT_DOMAINS=" + ",".join(out))
' "${name}" "${identity}"
}

check() {
  local status=0 serve
  serve="$(tailscale serve status --json 2>/dev/null || true)"
  if ! printf '%s' "${serve}" | python3 -c '
import json, re, sys
pairs = [p.split(":") for p in sys.argv[1].split()]
try:
    cfg = json.loads(sys.stdin.read() or "{}")
except Exception:
    print("  tailscale serve: status unreadable")
    sys.exit(1)
ok = True
for port, entry in pairs:
    tcp = cfg.get("TCP", {}).get(port, {})
    proxies = [
        h.get("Proxy", "")
        for key, web in cfg.get("Web", {}).items()
        if key.rpartition(":")[2] == port
        for h in web.get("Handlers", {}).values()
    ]
    funnel = any(v for k, v in cfg.get("AllowFunnel", {}).items() if k.rpartition(":")[2] == port)
    def to(p):
        return any(re.fullmatch(r"https?://(127\.0\.0\.1|localhost):" + p + r"/?", x) for x in proxies)
    if funnel:
        print(f"  tailscale serve :{port}: FUNNEL IS ON (public); the gated surfaces are tailnet-only")
        ok = False
    elif tcp.get("HTTPS") and to(entry):
        print(f"  tailscale serve :{port}: https -> 127.0.0.1:{entry} (sign-in gate), tailnet-only")
    elif tcp.get("HTTPS") and proxies:
        print(f"  tailscale serve :{port}: https -> {proxies[0]}, not the sign-in gate (127.0.0.1:{entry})")
        ok = False
    else:
        print(f"  tailscale serve :{port}: not published")
        ok = False
sys.exit(0 if ok else 1)
' "${GATED_PORTS}"; then
    status=1
  fi

  if [[ -n "$(env_value DEVCLAW_MCP_TOKEN)" ]]; then
    echo "  devclaw console bearer: DEVCLAW_MCP_TOKEN set"
  else
    echo "  devclaw console bearer: DEVCLAW_MCP_TOKEN unset; the console answers 401 after sign-in"
    status=1
  fi

  if [[ -n "$(env_value RELAY_EDGE_PROOF)" ]]; then
    echo "  dashboard relay proof: RELAY_EDGE_PROOF set"
  else
    echo "  dashboard relay proof: RELAY_EDGE_PROOF unset; the dashboard's relay endpoint answers 503"
    status=1
  fi
  return "${status}"
}

case "${1:-}" in
  domains) domains ;;
  check) check; exit $? ;;
  *) echo "usage: $0 <domains|check>" >&2; exit 2 ;;
esac
