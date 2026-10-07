#!/usr/bin/env bash
# scripts/identity/apply-branding.sh — set Logto's sign-in page branding: the
# tenant default and each Logto application's own name, mark and primary
# color. Operator step, never run by deploy: it changes what every member sees
# at sign-in. Run it through the M2M credentials like logto-admin.py
# (docs/runbook.md "Logto admin through the Management API"):
#
#   sops exec-env <file>.sops 'bash scripts/identity/apply-branding.sh'
#
# Idempotent (logto-admin.py reads first), reversible with `logto-admin.py
# undo`. An application that does not exist yet is skipped. Marks live in
# scripts/identity/branding/; seeds and mark provenance are in its README.
#
# Env: LOGTO_ENDPOINT, LOGTO_M2M_APP_ID, LOGTO_M2M_APP_SECRET (as logto-admin.py);
# GATE_APP, FS_APP, GRAFANA_APP override the Logto application names.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADMIN=(python3 "${HERE}/logto-admin.py")
GATE_APP="${GATE_APP:-lifekit sign-in gate}"
FS_APP="${FS_APP:-finance-sentry}"
GRAFANA_APP="${GRAFANA_APP:-Grafana}"

# Seeds (light primary) and their dark-theme counterparts, from the lk-theme
# seed engine: fs Petrol, lk Indigo.
LK_LIGHT="#4f46e5" LK_DARK="#8e9aff"
FS_LIGHT="#175a6d" FS_DARK="#71afc4"

mark() { printf 'data:image/svg+xml;base64,%s' "$(base64 -w0 "${HERE}/branding/$1.svg")"; }
LK_MARK="$(mark lk)"
FS_MARK="$(mark fs)"

# Tenant default: the lifekit mark and indigo, and no Terms/Privacy links
# (lifekit publishes neither page; the links used to point at a placeholder).
"${ADMIN[@]}" set-sign-in-exp \
  --logo-url "${LK_MARK}" --dark-logo-url "${LK_MARK}" \
  --primary-color "${LK_LIGHT}" --dark-primary-color "${LK_DARK}" \
  --clear-terms-links

# The sign-in gate fronts the lifekit dashboard and the devclaw console with one
# Logto client, so both show the lifekit mark.
"${ADMIN[@]}" set-app-sign-in-exp "${GATE_APP}" --if-exists --display-name "lifekit" \
  --logo-url "${LK_MARK}" --dark-logo-url "${LK_MARK}" \
  --primary-color "${LK_LIGHT}" --dark-primary-color "${LK_DARK}"

"${ADMIN[@]}" set-app-sign-in-exp "${FS_APP}" --if-exists --display-name "Finance Sentry" \
  --logo-url "${FS_MARK}" --dark-logo-url "${FS_MARK}" \
  --primary-color "${FS_LIGHT}" --dark-primary-color "${FS_DARK}"

# Grafana has no mark of its own: it keeps the tenant default, named.
"${ADMIN[@]}" set-app-sign-in-exp "${GRAFANA_APP}" --if-exists --display-name "Grafana"
