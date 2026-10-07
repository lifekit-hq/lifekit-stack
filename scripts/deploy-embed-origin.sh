#!/usr/bin/env bash
# scripts/deploy-embed-origin.sh — print the origin allowed to frame Grafana
# (CSP frame-ancestors), for deploy.sh to export as GRAFANA_EMBED_ORIGIN.
#
# An explicit GRAFANA_EMBED_ORIGIN (shell env or ENV_FILE) wins and is echoed
# back unchanged. When unset, the origin is derived as https://<tailnet name>
# plus the HTTPS port the dashboard is really served on: the host's own DNS
# name (`tailscale status --json`, Self.DNSName), and the port found by
# matching the dashboard container's published loopback host port (`docker ps`
# on the dashboard compose project) against the proxy targets in
# `tailscale serve status --json`. Once the tailnet sign-in gate is cut over
# (compose project `edge`, docs/runbook.md "Tailnet sign-in gate"), Serve
# points at the gate's dashboard entrypoint instead, so its published port
# (container port 18890 of the edge traefik, also when published as a
# Docker port range such as 18890-18891->18890-18891) counts as the dashboard's too.
# The dashboard is not assumed to sit on the
# default HTTPS port: the port is omitted only when it is 443. Never guesses:
# if the name or the port can't be derived, nothing is printed and embedding
# stays denied ('none'); this script always exits 0 so it can never fail a
# deploy. Stdout is the origin (or empty); messages go to stderr. Exercised
# with stubbed `tailscale` and `docker` on PATH in
# scripts/tests/test_deploy_embed_origin.py.

set -uo pipefail

ENV_FILE="${ENV_FILE:-}"

explicit="${GRAFANA_EMBED_ORIGIN:-}"
if [[ -z "${explicit}" && -n "${ENV_FILE}" && -f "${ENV_FILE}" ]]; then
  explicit="$(sed -nE 's/^[[:space:]]*GRAFANA_EMBED_ORIGIN=["'"'"']?([^"'"'"']*)["'"'"']?[[:space:]]*$/\1/p' "${ENV_FILE}" | head -1)"
fi
if [[ -n "${explicit}" ]]; then
  echo "grafana embed origin: using explicit GRAFANA_EMBED_ORIGIN" >&2
  printf '%s\n' "${explicit}"
  exit 0
fi

DASHBOARD_PROJECT="${DASHBOARD_PROJECT:-dashboard}"
ports="$(docker ps --filter "label=com.docker.compose.project=${DASHBOARD_PROJECT}" --format '{{.Ports}}' 2>/dev/null || true)"
EDGE_PROJECT="${EDGE_PROJECT:-edge}"
edge_ports="$(docker ps --filter "label=com.docker.compose.project=${EDGE_PROJECT}" \
  --filter "label=com.docker.compose.service=traefik" --format '{{.Ports}}' 2>/dev/null || true)"

origin="$(
  {
    tailscale status --json 2>/dev/null || true
    printf '\n@@@\n'
    tailscale serve status --json 2>/dev/null || true
    printf '\n@@@\n'
    printf '%s\n' "${ports}"
    printf '\n@@@\n'
    printf '%s\n' "${edge_ports}"
  } | python3 -c '
import json, re, sys
try:
    status, serve, ports, edge_ports = sys.stdin.read().split("\n@@@\n")
    name = json.loads(status)["Self"]["DNSName"].rstrip(".").lower()
except Exception:
    sys.exit()
if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", name):
    sys.exit()
published = set(re.findall(r"(?:127\.0\.0\.1|\[::1\]):(\d+)->", ports))
# Docker prints a published range as A-B->C-D (host A+i maps to container C+i),
# a single port as A->C; take the host port mapped to container port 18890.
for lo, hi, clo, chi in re.findall(r"(?:127\.0\.0\.1|\[::1\]):(\d+)(?:-(\d+))?->(\d+)(?:-(\d+))?/", edge_ports):
    if int(clo) <= 18890 <= int(chi or clo):
        published.add(str(int(lo) + 18890 - int(clo)))
found = set()
try:
    web = json.loads(serve).get("Web", {})
    for key, cfg in web.items():
        host, _, port = key.rpartition(":")
        if host.lower() != name or not port.isdigit():
            continue
        for h in cfg.get("Handlers", {}).values():
            m = re.fullmatch(r"https?://(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)/?", h.get("Proxy", ""))
            if m and m.group(1) in published:
                found.add(port)
except Exception:
    sys.exit()
if len(found) == 1:
    port = found.pop()
    print("https://" + name + ("" if port == "443" else ":" + port))
' 2>/dev/null || true
)"

if [[ -z "${origin}" ]]; then
  echo "grafana embed origin: dashboard tailnet name/port not derivable and GRAFANA_EMBED_ORIGIN unset; embedding stays denied ('none')" >&2
  exit 0
fi

echo "grafana embed origin: derived from the tailnet name and the dashboard's served port" >&2
printf '%s\n' "${origin}"
