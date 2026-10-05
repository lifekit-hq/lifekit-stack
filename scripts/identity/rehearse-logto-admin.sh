#!/usr/bin/env bash
# scripts/identity/rehearse-logto-admin.sh — rehearse the Logto M2M bootstrap
# (logto-m2m-bootstrap.sh) and the admin script (logto-admin.py) end to end on
# a throwaway Logto, then tear it down. Never touches the live identity
# project: its own compose project (default `logto-rehearsal`) from
# rehearsal.compose.yml, its own database volume, loopback ports 13001/13002,
# the same Logto and Postgres images as compose/identity/docker-compose.yml.
#
# Run by hand from a checkout, as an account in the docker group. Prints one
# PASS/FAIL line per check and a summary; exit 0 when every check passed.
# The throwaway (containers, volume, network) is removed on every exit path,
# and the run fails if any output line carried a client secret.
#
# Env: REHEARSAL_PROJECT (default logto-rehearsal), REHEARSAL_PORT (13001),
# REHEARSAL_ADMIN_PORT (13002), KEEP=1 to leave the throwaway up for poking.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
LIVE_COMPOSE="${REPO}/compose/identity/docker-compose.yml"
REHEARSAL_COMPOSE="${HERE}/rehearsal.compose.yml"
PROJECT="${REHEARSAL_PROJECT:-logto-rehearsal}"
export REHEARSAL_PORT="${REHEARSAL_PORT:-13001}"
export REHEARSAL_ADMIN_PORT="${REHEARSAL_ADMIN_PORT:-13002}"

if [[ "${PROJECT}" == identity || "${REHEARSAL_PORT}" == 3001 || "${REHEARSAL_ADMIN_PORT}" == 3002 ]]; then
  echo "rehearse: refusing the live identity project or its ports" >&2
  exit 2
fi
if [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT}")" ]]; then
  echo "rehearse: compose project ${PROJECT} already has containers; another rehearsal running?" >&2
  exit 2
fi
for port in "${REHEARSAL_PORT}" "${REHEARSAL_ADMIN_PORT}"; do
  if ss -Hltn "sport = :${port}" | grep -q .; then
    echo "rehearse: port ${port} is in use" >&2
    exit 2
  fi
done

REHEARSAL_LOGTO_IMAGE="$(sed -nE 's/^[[:space:]]*image:[[:space:]]*(ghcr\.io\/logto-io\/logto:[^[:space:]]+).*/\1/p' "${LIVE_COMPOSE}" | head -1)"
REHEARSAL_POSTGRES_IMAGE="$(sed -nE 's/^[[:space:]]*image:[[:space:]]*(postgres:[^[:space:]]+).*/\1/p' "${LIVE_COMPOSE}" | head -1)"
REHEARSAL_DB_PASSWORD="$(openssl rand -hex 16)"
export REHEARSAL_LOGTO_IMAGE REHEARSAL_POSTGRES_IMAGE REHEARSAL_DB_PASSWORD

WORK="$(mktemp -d)"
LOG="${WORK}/transcript.log"
compose() { docker compose -p "${PROJECT}" -f "${REHEARSAL_COMPOSE}" "$@"; }

teardown() {
  local rc=$?
  if [[ "${KEEP:-0}" == 1 ]]; then
    echo "rehearse: KEEP=1, throwaway left up: docker compose -p ${PROJECT} -f ${REHEARSAL_COMPOSE} down -v"
  else
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    if [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT}")" ]] \
      || docker volume ls -q | grep -qx "${PROJECT}_postgres_data"; then
      echo "rehearse: teardown left containers or the volume of ${PROJECT} behind" >&2
      rc=1
    else
      echo "rehearse: throwaway ${PROJECT} torn down (containers, volume, network)"
    fi
  fi
  rm -rf -- "${WORK}"
  exit "${rc}"
}
trap teardown EXIT

passes=0
fails=0
pass() { echo "PASS  $*"; passes=$((passes + 1)); }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
assert_eq() {
  if [[ "$2" == "$3" ]]; then pass "$1: $3"; else fail "$1: got $2, want $3"; fi
}
# Run a tool under test: its output goes to the screen and the transcript the
# secret scan reads.
run() { "$@" 2>&1 | tee -a "${LOG}"; return "${PIPESTATUS[0]}"; }
expect_ok() {
  local what="$1"
  shift
  if run "$@"; then pass "${what}"; else fail "${what}"; fi
}
expect_fail() {
  local what="$1"
  shift
  if run "$@"; then fail "${what} (it succeeded)"; else pass "${what}"; fi
}
sql() { docker exec -i "$(compose ps -q postgres)" psql -X -q -At -U logto -d logto -c "$1"; }
admin() { run python3 "${HERE}/logto-admin.py" "$@"; }
# A read through the Management API, for assertions: prints one compact line.
api_get() {
  python3 - "$1" "$2" <<'PY'
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("la", os.path.join(os.environ["HERE"], "logto-admin.py"))
la = importlib.util.module_from_spec(spec)
spec.loader.exec_module(la)
api = la.Api(os.environ["LOGTO_ENDPOINT"], os.environ["LOGTO_M2M_APP_ID"], os.environ["LOGTO_M2M_APP_SECRET"])
path, expr = sys.argv[1], sys.argv[2]
if expr == "create-user":
    _, _, out = api.call("POST", path, {"username": "rehearsal_owner", "primaryEmail": "owner@rehearsal.invalid"})
    print(out["id"])
else:
    print(json.dumps(eval(expr, {"items": api.list_all(path)}), sort_keys=True))
PY
}
export HERE

echo "== throwaway ${PROJECT}: ${REHEARSAL_LOGTO_IMAGE} + ${REHEARSAL_POSTGRES_IMAGE}, ports ${REHEARSAL_PORT}/${REHEARSAL_ADMIN_PORT}"
compose up -d --wait --quiet-pull 2>&1 | tail -2
live_version="$(docker exec "$(compose ps -q logto)" node -p "require('./packages/core/package.json').version")"
echo "logto version in the throwaway: ${live_version}"

export IDENTITY_PROJECT="${PROJECT}"
export LOGTO_ENDPOINT="http://127.0.0.1:${REHEARSAL_PORT}"
export LOGTO_ADMIN_LEDGER="${WORK}/ledger.jsonl"
BOOT="${HERE}/logto-m2m-bootstrap.sh"
ENVF="${WORK}/m2m.env"
count_rows() {
  sql "select (select count(*) from applications where tenant_id='default' and name='firstmate-ops')
       || '/' || (select count(*) from application_secrets s join applications a on a.id = s.application_id
                   where a.tenant_id='default' and a.name='firstmate-ops')
       || '/' || (select count(*) from applications_roles ar join applications a on a.id = ar.application_id
                   where a.tenant_id='default' and a.name='firstmate-ops')"
}

echo "== bootstrap"
run bash "${BOOT}" status
expect_ok "bootstrap apply (first: inserts app, secret, role grant)" bash "${BOOT}" apply --env-out "${ENVF}"
assert_eq "rows app/secret/grant after apply" "$(count_rows)" 1/1/1
assert_eq "env-out file mode" "$(stat -c %a "${ENVF}")" 600
expect_ok "bootstrap apply (second: no change)" bash "${BOOT}" apply
assert_eq "rows app/secret/grant after re-apply (unchanged)" "$(count_rows)" 1/1/1
expect_fail "apply refuses to overwrite an existing env-out file" bash "${BOOT}" apply --env-out "${ENVF}"
expect_ok "bootstrap status" bash "${BOOT}" status

set -a
# shellcheck disable=SC1090
. "${ENVF}"
set +a
sed -n 's/^LOGTO_M2M_APP_SECRET=//p' "${ENVF}" >"${WORK}/secrets.pat"

echo "== admin script, on the live-inserted app (Logto not restarted)"
expect_ok "token-check (client_credentials, scope all, over loopback http)" admin token-check
expect_ok "ensure-role admin (creates)" admin ensure-role admin --description "lifekit admin"
expect_ok "ensure-role admin (exists)" admin ensure-role admin
owner="$(api_get /api/users create-user)"
echo "throwaway test user ${owner}"
expect_ok "assign-role by email (assigns)" admin assign-role owner@rehearsal.invalid admin
expect_ok "assign-role by username (already has it)" admin assign-role rehearsal_owner admin
assert_eq "owner's roles" "$(api_get "/api/users/${owner}/roles" '[r["name"] for r in items]')" '["admin"]'

expect_ok "ensure-app Grafana (creates, no URIs, as the console left it)" admin ensure-app Grafana
expect_ok "set-redirects Grafana (patches)" admin set-redirects Grafana \
  --redirect-uri https://box.invalid:3000/login/generic_oauth --post-logout-uri https://box.invalid:3000/login
expect_ok "set-redirects Grafana (already set)" admin set-redirects Grafana \
  --redirect-uri https://box.invalid:3000/login/generic_oauth --post-logout-uri https://box.invalid:3000/login
assert_eq "Grafana URIs as patched" \
  "$(api_get /api/applications '[a["oidcClientMetadata"] for a in items if a["name"] == "Grafana"]')" \
  '[{"postLogoutRedirectUris": ["https://box.invalid:3000/login"], "redirectUris": ["https://box.invalid:3000/login/generic_oauth"]}]'

expect_ok "ensure-app gate (creates with URIs, writes secret file)" admin ensure-app "lifekit sign-in gate" \
  --redirect-uri https://box.invalid:18790/oauth2/callback --redirect-uri https://box.invalid:18791/oauth2/callback \
  --post-logout-uri https://box.invalid:18790/ --post-logout-uri https://box.invalid:18791/ \
  --secret-file "${WORK}/gate.secret"
assert_eq "gate secret file mode" "$(stat -c %a "${WORK}/gate.secret")" 600
assert_eq "gate secret file holds a 32-char secret" "$(wc -c <"${WORK}/gate.secret")" 32
cat "${WORK}/gate.secret" >>"${WORK}/secrets.pat"
echo >>"${WORK}/secrets.pat"
expect_ok "ensure-app gate (exists, URIs already set)" admin ensure-app "lifekit sign-in gate" \
  --redirect-uri https://box.invalid:18790/oauth2/callback --redirect-uri https://box.invalid:18791/oauth2/callback \
  --post-logout-uri https://box.invalid:18790/ --post-logout-uri https://box.invalid:18791/
expect_fail "ensure-app refuses to overwrite the secret file" admin ensure-app "lifekit sign-in gate" --secret-file "${WORK}/gate.secret"
expect_fail "ensure-app refuses a name held by a non-Traditional app" admin ensure-app firstmate-ops

echo "== ledger, then undo every change in reverse"
admin ledger
while python3 "${HERE}/logto-admin.py" ledger | python3 -c '
import json, sys
es = [json.loads(l) for l in sys.stdin]
done = {e["of"] for e in es if e["action"] == "undo"}
sys.exit(0 if any("undo" in e and e["n"] not in done for e in es) else 1)'; do
  run python3 "${HERE}/logto-admin.py" undo || { fail "undo"; break; }
done
expect_fail "undo with nothing left" admin undo
assert_eq "apps after undo (Grafana and gate deleted)" "$(api_get /api/applications '[a["name"] for a in items]')" '["firstmate-ops"]'
assert_eq "User roles after undo" "$(api_get /api/roles '[r["name"] for r in items if r["type"] == "User"]')" '[]'

echo "== removal"
held_token_ok=0
token_file="${WORK}/held.token"
python3 - "${token_file}" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("la", os.path.join(os.environ["HERE"], "logto-admin.py"))
la = importlib.util.module_from_spec(spec)
spec.loader.exec_module(la)
api = la.Api(os.environ["LOGTO_ENDPOINT"], os.environ["LOGTO_M2M_APP_ID"], os.environ["LOGTO_M2M_APP_SECRET"])
api.token()
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, api._token.encode())
PY
expect_ok "bootstrap remove" bash "${BOOT}" remove
assert_eq "rows app/secret/grant after remove" "$(count_rows)" 0/0/0
expect_fail "token-check after remove (no new token)" admin token-check
expect_ok "bootstrap remove again (nothing to remove)" bash "${BOOT}" remove
expect_ok "bootstrap status (absent)" bash "${BOOT}" status
if [[ "$(curl -s -o /dev/null -w '%{http_code}' -H @<(printf 'Authorization: Bearer %s\n' "$(cat "${token_file}")") \
  "${LOGTO_ENDPOINT}/api/applications")" == 200 ]]; then
  held_token_ok=1
fi
echo "INFO  a token issued before remove still answers: $([[ ${held_token_ok} == 1 ]] && echo "yes (valid until it expires, <= 1h)" || echo no)"

echo "== secret scan"
if grep -qFf "${WORK}/secrets.pat" "${LOG}"; then
  fail "a client secret appeared in the output"
else
  pass "no client secret in any output ($(wc -l <"${LOG}") lines scanned)"
fi

echo "== ${passes} passed, ${fails} failed"
[[ "${fails}" -eq 0 ]]
