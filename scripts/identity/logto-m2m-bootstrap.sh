#!/usr/bin/env bash
# scripts/identity/logto-m2m-bootstrap.sh — insert (or remove) firstmate's
# machine-to-machine app "firstmate-ops" straight into Logto's database, so
# Logto admin steps run through the Management API (scripts/identity/
# logto-admin.py) with no console session. docs/runbook.md "Logto admin
# through the Management API" has the procedure and the rollback.
#
#   apply [--env-out PATH]  one transaction, in tenant `default`: create the
#                           MachineToMachine app "firstmate-ops" with a
#                           generated client secret, unless it exists, and
#                           grant it the seeded role "Logto Management API
#                           access" (scope `all` on the Management API),
#                           unless granted. Re-running changes nothing.
#                           --env-out writes LOGTO_M2M_APP_ID and
#                           LOGTO_M2M_APP_SECRET as dotenv to a new 0600 file
#                           (never over an existing one); the secret goes from
#                           Postgres into that file and is never printed.
#   remove                  one transaction: delete the app (its secrets and
#                           role grant cascade). New tokens stop at once; a
#                           token already issued stays valid until it expires
#                           (one hour).
#   status                  print the app id, its secret count and whether
#                           the role is granted; never a secret value.
#
# Runs psql inside the project's postgres container (found by compose labels),
# so it needs the docker group. Env: IDENTITY_PROJECT (default `identity`, the
# production project; the rehearsal points it at its own throwaway project).
# Exit 0 done, 1 failure (nothing committed), 2 usage.
#
# Rehearsed end to end by scripts/identity/rehearse-logto-admin.sh.

set -euo pipefail

PROJECT="${IDENTITY_PROJECT:-identity}"
APP_NAME="firstmate-ops"
ROLE_NAME="Logto Management API access"
TENANT="default"

usage() {
  echo "usage: $0 <apply [--env-out PATH]|remove|status>" >&2
  exit 2
}

postgres_container() {
  local ctr
  ctr="$(docker ps -q --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=postgres" | head -1)"
  if [[ -z "${ctr}" ]]; then
    echo "logto-m2m-bootstrap: no running postgres container in compose project ${PROJECT}" >&2
    exit 1
  fi
  printf '%s' "${ctr}"
}

# psql with the names bound as variables; SQL comes on stdin. -At: bare rows.
psql_in() {
  docker exec -i "${CTR}" psql -X -q -At -v ON_ERROR_STOP=1 -U logto -d logto \
    -v tenant="${TENANT}" -v app_name="${APP_NAME}" -v role_name="${ROLE_NAME}"
}

apply() {
  local env_out="$1"
  if [[ -n "${env_out}" && -e "${env_out}" ]]; then
    echo "logto-m2m-bootstrap: ${env_out} exists; not overwriting a credentials file" >&2
    exit 1
  fi
  # psql variables do not reach a DO block, so they ride in through
  # set_config (transaction-local) and the block reads them back.
  psql_in <<'SQL'
begin;
select set_config('fm.tenant', :'tenant', true),
       set_config('fm.app_name', :'app_name', true),
       set_config('fm.role_name', :'role_name', true) \g /dev/null
do $$
declare
  v_tenant text := current_setting('fm.tenant');
  v_name text := current_setting('fm.app_name');
  v_app varchar(21);
  v_role varchar(21);
  v_count int;
begin
  select count(*) into v_count from applications
    where tenant_id = v_tenant and name = v_name;
  if v_count > 1 then
    raise exception '% applications named % in tenant %; resolve by hand', v_count, v_name, v_tenant;
  end if;
  select id into v_role from roles
    where tenant_id = v_tenant and name = current_setting('fm.role_name') and type = 'MachineToMachine';
  if v_role is null then
    raise exception 'tenant % has no MachineToMachine role %', v_tenant, current_setting('fm.role_name');
  end if;

  select id into v_app from applications where tenant_id = v_tenant and name = v_name;
  if v_app is null then
    -- Shaped like a console-created app: a 21-char id, the internal
    -- (unusable) legacy secret, and the real client secret in
    -- application_secrets under Logto's default name.
    v_app := substr(replace(gen_random_uuid()::text, '-', ''), 1, 21);
    insert into applications (tenant_id, id, name, secret, description, type, oidc_client_metadata)
    values (v_tenant, v_app, v_name,
            '#internal:' || replace(gen_random_uuid()::text, '-', ''),
            'firstmate Management API client (scripts/identity/logto-m2m-bootstrap.sh)',
            'MachineToMachine', '{"redirectUris": [], "postLogoutRedirectUris": []}');
    raise notice 'created application % (%)', v_name, v_app;
  elsif (select type from applications where id = v_app) <> 'MachineToMachine' then
    raise exception 'application % (%) exists but is not MachineToMachine', v_name, v_app;
  else
    raise notice 'application % exists (%)', v_name, v_app;
  end if;

  if not exists (select 1 from application_secrets where tenant_id = v_tenant and application_id = v_app
                   and (expires_at is null or expires_at > now())) then
    insert into application_secrets (tenant_id, application_id, name, value)
    values (v_tenant, v_app, 'Default secret',
            replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''));
    raise notice 'generated a client secret';
  end if;

  insert into applications_roles (tenant_id, id, application_id, role_id)
  values (v_tenant, substr(replace(gen_random_uuid()::text, '-', ''), 1, 21), v_app, v_role)
  on conflict on constraint applications_roles__application_id_role_id do nothing;
  if found then
    raise notice 'granted role %', current_setting('fm.role_name');
  else
    raise notice 'role % already granted', current_setting('fm.role_name');
  end if;
end $$;
commit;
SQL
  if [[ -n "${env_out}" ]]; then
    (
      umask 077
      set -o noclobber
      psql_in >"${env_out}" <<'SQL'
select 'LOGTO_M2M_APP_ID=' || a.id || E'\n' || 'LOGTO_M2M_APP_SECRET=' || s.value
  from applications a
  join application_secrets s on s.tenant_id = a.tenant_id and s.application_id = a.id
 where a.tenant_id = :'tenant' and a.name = :'app_name'
   and (s.expires_at is null or s.expires_at > now())
 order by (s.name = 'Default secret') desc, s.created_at
 limit 1;
SQL
    )
    if ! grep -q '^LOGTO_M2M_APP_SECRET=.' "${env_out}"; then
      rm -f -- "${env_out}"
      echo "logto-m2m-bootstrap: could not read the credentials back" >&2
      exit 1
    fi
    echo "logto-m2m-bootstrap: credentials written to ${env_out} (0600)"
  fi
}

remove() {
  psql_in <<'SQL'
begin;
select set_config('fm.tenant', :'tenant', true),
       set_config('fm.app_name', :'app_name', true) \g /dev/null
do $$
declare
  v_deleted int;
begin
  delete from applications
   where tenant_id = current_setting('fm.tenant') and name = current_setting('fm.app_name')
     and type = 'MachineToMachine';
  get diagnostics v_deleted = row_count;
  if v_deleted = 0 then
    raise notice 'no application % to remove', current_setting('fm.app_name');
  else
    raise notice 'removed % application(s) %', v_deleted, current_setting('fm.app_name');
  end if;
end $$;
commit;
SQL
}

status() {
  local out
  out="$(psql_in <<'SQL'
select a.id,
       (select count(*) from application_secrets s
         where s.tenant_id = a.tenant_id and s.application_id = a.id
           and (s.expires_at is null or s.expires_at > now())),
       exists (select 1 from applications_roles ar join roles r on r.id = ar.role_id
                where ar.tenant_id = a.tenant_id and ar.application_id = a.id
                  and r.name = :'role_name')
  from applications a
 where a.tenant_id = :'tenant' and a.name = :'app_name' and a.type = 'MachineToMachine';
SQL
)"
  if [[ -z "${out}" ]]; then
    echo "${APP_NAME}: absent (project ${PROJECT})"
    return 0
  fi
  local id secrets granted
  while IFS='|' read -r id secrets granted; do
    echo "${APP_NAME}: ${id}, ${secrets} live secret(s), role \"${ROLE_NAME}\": $([[ ${granted} == t ]] && echo granted || echo NOT granted) (project ${PROJECT})"
  done <<<"${out}"
}

cmd="${1:-}"
[[ -n "${cmd}" ]] || usage
shift
env_out=""
case "${cmd}" in
  apply)
    if [[ "${1:-}" == "--env-out" ]]; then
      [[ -n "${2:-}" ]] || usage
      env_out="$2"
      shift 2
    fi
    [[ $# -eq 0 ]] || usage
    ;;
  remove | status) [[ $# -eq 0 ]] || usage ;;
  *) usage ;;
esac

CTR="$(postgres_container)"
case "${cmd}" in
  apply) apply "${env_out}" ;;
  remove) remove ;;
  status) status ;;
esac
