#!/usr/bin/env bash
# scripts/identity/fleet-agent.sh — the fleet's own Logto identity: the
# machine-to-machine application `fleet-agent`, an API resource for the
# owner's apps, and one scope per action class. Apps that must accept the
# fleet validate the token's issuer, audience and scope; they never hold the
# secret. docs/runbook.md "Fleet identity (fleet-agent)" has the procedure.
#
#   issue            create what is missing, change nothing else: the API
#                    resource and its scopes, the app, a role holding exactly
#                    those scopes, the grant. The client secret goes only into
#                    the host secret store (a SOPS dotenv, 0600, encrypted to
#                    the same recipients as the firstmate-ops file), when it
#                    is absent or belongs to another app id. Never printed.
#   status           what Logto holds for the app and whether the store
#                    matches it; no secret value.
#   verify           get a token with the stored credentials and print its
#                    scope and lifetime (the token is not printed). Exits 1
#                    when the token lacks any of the fleet scopes.
#   rotate           new client secret, written to the store, older ones
#                    deleted in Logto. Callers that hold the old one stop.
#   revoke [--delete]  take the app's role away: Logto still issues it a token,
#                    but one with no scope, which every scope-checking app
#                    refuses. --delete removes the app and the stored secret:
#                    no token is issued at all.
#
# Admin calls go through scripts/identity/logto-admin.py as the firstmate-ops
# app, whose credentials come from FIRSTMATE_LOGTO_M2M_SOPS. Nothing here
# restarts Logto or any container. Env:
#   FIRSTMATE_LOGTO_M2M_SOPS  default ~/.config/firstmate/logto-m2m.env.sops
#   FLEET_AGENT_SOPS          default ~/.config/firstmate/fleet-agent.env.sops
#   FLEET_RESOURCE            default https://fleet.lifekit.internal/api
#   SOPS_AGE_KEY_FILE         default ~/.config/sops/age/firstmate.agekey
#   LOGTO_ENDPOINT            default http://127.0.0.1:3001
# Exit 0 done, 1 failure, 2 usage.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADMIN="${HERE}/logto-admin.py"
APP="fleet-agent"
M2M_SOPS="${FIRSTMATE_LOGTO_M2M_SOPS:-${HOME}/.config/firstmate/logto-m2m.env.sops}"
STORE="${FLEET_AGENT_SOPS:-${HOME}/.config/firstmate/fleet-agent.env.sops}"
RESOURCE="${FLEET_RESOURCE:-https://fleet.lifekit.internal/api}"
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-${HOME}/.config/sops/age/firstmate.agekey}"

# One scope per action class. Apps check the scope their endpoint needs.
SCOPES=(
  "fleet:read=Read fleet and health data on the owner's apps"
  "fleet:recategorise=Reversible recategorisation of transactions"
  "basket:price-ingest=Ingest prices into basket"
  "basket:catalog-read=Read basket's product catalog"
  "basket:catalog-add=Add products to basket's catalog"
)

usage() {
  echo "usage: $0 <issue|status|verify|rotate|revoke [--delete]>" >&2
  exit 2
}

die() {
  echo "fleet-agent: $*" >&2
  exit 1
}

WORK=""
cleanup() {
  if [[ -n "${WORK}" ]]; then
    [[ -f "${WORK}/secret" ]] && shred -u "${WORK}/secret" 2>/dev/null
    rm -rf -- "${WORK}"
  fi
}
trap cleanup EXIT

scratch() {
  if [[ -z "${WORK}" ]]; then
    WORK="$(umask 077 && mktemp -d)"
  fi
}

# Read one key out of a SOPS dotenv without printing anything else.
store_get() {
  local file="$1" key="$2"
  sops decrypt --input-type dotenv --output-type dotenv "${file}" 2>/dev/null |
    sed -n "s/^${key}=//p" | head -1
}

# Admin credentials: the firstmate-ops app, into this process's environment
# (never argv, never printed), for the python calls below.
load_admin() {
  command -v sops >/dev/null || die "sops not found"
  [[ -f "${M2M_SOPS}" ]] || die "${M2M_SOPS} not found (the firstmate-ops credentials)"
  LOGTO_M2M_APP_ID="$(store_get "${M2M_SOPS}" LOGTO_M2M_APP_ID)"
  LOGTO_M2M_APP_SECRET="$(store_get "${M2M_SOPS}" LOGTO_M2M_APP_SECRET)"
  [[ -n "${LOGTO_M2M_APP_ID}" && -n "${LOGTO_M2M_APP_SECRET}" ]] ||
    die "could not read the firstmate-ops credentials from ${M2M_SOPS}"
  export LOGTO_M2M_APP_ID LOGTO_M2M_APP_SECRET
}

admin() { python3 "${ADMIN}" "$@"; }

scope_names() {
  local s
  for s in "${SCOPES[@]}"; do printf '%s\n' "${s%%=*}"; done
}

app_id() {
  admin app-status "${APP}" | sed -nE "s/^app ${APP}: ([^,]+),.*/\1/p"
}

# The store's recipients are the firstmate-ops file's: firstmate + captain.
recipients() {
  sed -n 's/^sops_age__list_[0-9]*__map_recipient=//p' "${M2M_SOPS}" | paste -sd, -
}

# Encrypt "id, secret, resource" to the store, replacing it atomically.
store_put() {
  local id="$1" secret_file="$2" keys
  keys="$(recipients)"
  [[ -n "${keys}" ]] || die "no age recipients found in ${M2M_SOPS}"
  scratch
  (
    umask 077
    {
      printf 'FLEET_AGENT_CLIENT_ID=%s\n' "${id}"
      printf 'FLEET_AGENT_CLIENT_SECRET=%s\n' "$(cat "${secret_file}")"
      printf 'FLEET_AGENT_RESOURCE=%s\n' "${RESOURCE}"
    } >"${WORK}/plain.env"
    # From the scratch dir: sops would otherwise find the checkout's
    # .sops.yaml and refuse a file no creation rule matches.
    cd "${WORK}"
    sops encrypt --age "${keys}" --input-type dotenv --output-type dotenv \
      plain.env >store.sops
    shred -u plain.env
  )
  mkdir -p "$(dirname "${STORE}")"
  (umask 077 && cp "${WORK}/store.sops" "${STORE}.new") && mv -f "${STORE}.new" "${STORE}"
  chmod 600 "${STORE}"
  echo "fleet-agent: client credentials stored in ${STORE} (0600, SOPS)"
}

new_secret_path() {
  scratch
  printf '%s/secret' "${WORK}"
}

cmd_issue() {
  load_admin
  local -a scope_args=() grant_args=()
  local s
  for s in "${SCOPES[@]}"; do scope_args+=(--scope "${s}"); done
  while IFS= read -r s; do grant_args+=(--scope "${s}"); done < <(scope_names)

  admin ensure-resource "${RESOURCE}" --name "Fleet apps" "${scope_args[@]}"
  admin ensure-m2m-app "${APP}" --resource "${RESOURCE}" "${grant_args[@]}" \
    --description "Fleet service identity (scripts/identity/fleet-agent.sh)"

  local id stored
  id="$(app_id)"
  [[ -n "${id}" ]] || die "app ${APP} not found after ensure-m2m-app"
  stored=""
  [[ -f "${STORE}" ]] && stored="$(store_get "${STORE}" FLEET_AGENT_CLIENT_ID)"
  if [[ "${stored}" == "${id}" ]]; then
    echo "fleet-agent: secret store ${STORE} already holds ${id}; not touched"
    return 0
  fi
  local secret
  secret="$(new_secret_path)"
  # Idempotent on purpose: reads the app's existing default secret out of
  # Logto, so a run that died before the store write recovers on the next.
  admin ensure-m2m-app "${APP}" --resource "${RESOURCE}" "${grant_args[@]}" --secret-file "${secret}"
  store_put "${id}" "${secret}"
}

cmd_status() {
  load_admin
  admin app-status "${APP}"
  local id stored
  id="$(app_id)"
  if [[ ! -f "${STORE}" ]]; then
    echo "store: ${STORE} absent"
  else
    stored="$(store_get "${STORE}" FLEET_AGENT_CLIENT_ID)"
    echo "store: ${STORE} holds client id $([[ -n "${id}" && "${stored}" == "${id}" ]] && echo "matching the app" || echo "NOT matching the app (run rotate)")"
  fi
}

cmd_verify() {
  [[ -f "${STORE}" ]] || die "${STORE} not found; run issue"
  local id secret s
  id="$(store_get "${STORE}" FLEET_AGENT_CLIENT_ID)"
  secret="$(store_get "${STORE}" FLEET_AGENT_CLIENT_SECRET)"
  [[ -n "${id}" && -n "${secret}" ]] || die "could not read ${STORE}"
  local -a scope_args=()
  while IFS= read -r s; do scope_args+=(--scope "${s}"); done < <(scope_names)
  local out granted missing=""
  out="$(LOGTO_M2M_APP_ID="${id}" LOGTO_M2M_APP_SECRET="${secret}" \
    admin token-check --resource "${RESOURCE}" "${scope_args[@]}")" || return 1
  echo "${out}"
  granted=" $(printf '%s\n' "${out}" | sed -n 's/^token ok: scope \(.*\), expires in .*/\1/p') "
  while IFS= read -r s; do
    [[ "${granted}" == *" ${s} "* ]] || missing="${missing} ${s}"
  done < <(scope_names)
  [[ -z "${missing}" ]] || die "token lacks scope(s):${missing} (run issue)"
}

cmd_rotate() {
  load_admin
  local id secret
  id="$(app_id)"
  [[ -n "${id}" ]] || die "app ${APP} not found; run issue"
  secret="$(new_secret_path)"
  admin rotate-secret "${APP}" --secret-file "${secret}"
  store_put "${id}" "${secret}"
}

cmd_revoke() {
  load_admin
  case "${1:-}" in
    "")
      admin revoke-app "${APP}"
      ;;
    --delete)
      admin revoke-app "${APP}" --delete
      if [[ -f "${STORE}" ]]; then
        shred -u "${STORE}"
        echo "fleet-agent: ${STORE} removed"
      fi
      ;;
    *) usage ;;
  esac
  echo "fleet-agent: a token already issued is valid until it expires (at most an hour)"
}

cmd="${1:-}"
[[ -n "${cmd}" ]] || usage
shift
case "${cmd}" in
  issue | status | verify | rotate) [[ $# -eq 0 ]] || usage ;;
  revoke) [[ $# -le 1 ]] || usage ;;
  *) usage ;;
esac
"cmd_${cmd}" "$@"
