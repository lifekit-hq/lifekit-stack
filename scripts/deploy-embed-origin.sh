#!/usr/bin/env bash
# scripts/deploy-embed-origin.sh — print the origin allowed to frame Grafana
# (CSP frame-ancestors), for deploy.sh to export as GRAFANA_EMBED_ORIGIN.
#
# An explicit GRAFANA_EMBED_ORIGIN (shell env or ENV_FILE) wins and is echoed
# back unchanged. When unset, the origin is derived from the host's own
# tailnet DNS name (`tailscale status --json`, Self.DNSName), because the
# dashboard is published through Tailscale Serve at https://<that name>/ on
# the default HTTPS port (see README "lifekit-dashboard"), so the origin is
# scheme + name with no port. Never guesses: if the name can't be derived,
# nothing is printed and embedding stays denied ('none'); this script always
# exits 0 so it can never fail a deploy. Stdout is the origin (or empty);
# messages go to stderr. Exercised with a stubbed `tailscale` on PATH in
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

name="$(tailscale status --json 2>/dev/null | python3 -c '
import json, re, sys
try:
    n = json.load(sys.stdin)["Self"]["DNSName"].rstrip(".").lower()
except Exception:
    sys.exit()
if re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", n):
    print(n)
' 2>/dev/null || true)"

if [[ -z "${name}" ]]; then
  echo "grafana embed origin: tailnet name not derivable and GRAFANA_EMBED_ORIGIN unset; embedding stays denied ('none')" >&2
  exit 0
fi

echo "grafana embed origin: derived from the host's tailnet name" >&2
printf 'https://%s\n' "${name}"
