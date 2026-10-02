#!/usr/bin/env bash
# scripts/deploy-grafana-oidc.sh — decides Grafana's sign-in through Logto for
# deploy.sh, which exports what this prints before the platform `up`
# (docs/runbook.md "Grafana sign-in through Logto").
#
# Prints KEY=value lines on stdout: GRAFANA_OIDC_ENABLED, GRAFANA_OIDC_ONLY and,
# when enabled, GRAFANA_OIDC_ISSUER (<IDENTITY_ENDPOINT>/oidc; an explicit
# IDENTITY_ENDPOINT wins, otherwise https://<tailnet name>:3001 from
# scripts/deploy-identity.sh). Values are read from the shell env, then ENV_FILE.
#
#   GRAFANA_OIDC_ENABLED unset/false  -> ENABLED=false, ONLY=false, silent.
#   enabled but the client id, the client secret or the issuer is missing
#                                     -> ENABLED=false, ONLY=false, exit 1
#                                        (the reason on stderr): Grafana never
#                                        boots half-configured.
#   GRAFANA_OIDC_ONLY=true without a working ENABLED -> ONLY=false, exit 1:
#                                        the password form is never hidden
#                                        unless the SSO button can work.
#
# Exercised in scripts/tests/test_deploy_grafana_oidc.py.

set -uo pipefail

ENV_FILE="${ENV_FILE:-}"
HERE="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"

env_value() {
  local name="$1" value="${!1:-}"
  if [[ -z "${value}" && -n "${ENV_FILE}" && -f "${ENV_FILE}" ]]; then
    value="$(sed -nE "s/^[[:space:]]*${name}=[\"']?([^\"']*)[\"']?[[:space:]]*$/\1/p" "${ENV_FILE}" | head -1)"
  fi
  printf '%s' "${value}"
}

is_true() { [[ "$(tr '[:upper:]' '[:lower:]' <<<"$1")" == "true" ]]; }

off() {
  printf 'GRAFANA_OIDC_ENABLED=false\nGRAFANA_OIDC_ONLY=false\n'
}

enabled="$(env_value GRAFANA_OIDC_ENABLED)"
only="$(env_value GRAFANA_OIDC_ONLY)"

if ! is_true "${enabled}"; then
  off
  if is_true "${only}"; then
    echo "grafana oidc: GRAFANA_OIDC_ONLY is set but GRAFANA_OIDC_ENABLED is not; the password form stays" >&2
    exit 1
  fi
  exit 0
fi

missing=()
[[ -n "$(env_value GRAFANA_OIDC_CLIENT_ID)" ]] || missing+=(GRAFANA_OIDC_CLIENT_ID)
[[ -n "$(env_value GRAFANA_OIDC_CLIENT_SECRET)" ]] || missing+=(GRAFANA_OIDC_CLIENT_SECRET)

endpoint="$(env_value IDENTITY_ENDPOINT)"
if [[ -z "${endpoint}" ]]; then
  endpoint="$(ENV_FILE="${ENV_FILE}" "${HERE}/deploy-identity.sh" endpoints 2>/dev/null | sed -n 's/^IDENTITY_ENDPOINT=//p')"
fi
[[ -n "${endpoint}" ]] || missing+=("the issuer (IDENTITY_ENDPOINT unset, tailnet name not derivable)")

if ((${#missing[@]})); then
  off
  echo "grafana oidc: enabled but missing ${missing[*]}; sign-in through Logto left off" >&2
  exit 1
fi

printf 'GRAFANA_OIDC_ENABLED=true\nGRAFANA_OIDC_ONLY=%s\nGRAFANA_OIDC_ISSUER=%s/oidc\n' \
  "$(is_true "${only}" && echo true || echo false)" "${endpoint%/}"
