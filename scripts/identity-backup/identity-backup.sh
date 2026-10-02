#!/usr/bin/env bash
# identity-backup.sh — nightly dump of the identity provider's Postgres
# (compose project `identity`, compose/identity/) into the backup directory.
#
# One gzipped `pg_dumpall` per run: Logto keeps per-tenant database roles
# next to its schema, and a restore needs both, so a single-database pg_dump
# is not enough. The dump holds password hashes and every user record:
# written 0600 into a 0700 directory. Keeps the newest IDENTITY_BACKUP_KEEP
# dumps. Restore: docs/runbook.md "Identity provider (Logto)".
#
# Run by lifekit-identity-backup.timer as the deploy account (docker group).
# A box where the identity project was never deployed has nothing to dump
# and exits 0; a deployed project whose Postgres is not running, or a failed
# dump, exits 1, so the oneshot goes failed and the host unit gauge reports
# it (scripts/unit-gauge/unit-gauge.sh watches lifekit-*.timer).
#
# Env overrides (mainly for tests):
#   IDENTITY_BACKUP_DIR   default /srv/openclaw/backups/identity
#   IDENTITY_BACKUP_KEEP  default 14
#   IDENTITY_PROJECT      default identity

set -euo pipefail

BACKUP_DIR="${IDENTITY_BACKUP_DIR:-/srv/openclaw/backups/identity}"
KEEP="${IDENTITY_BACKUP_KEEP:-14}"
PROJECT="${IDENTITY_PROJECT:-identity}"

filters=(--filter "label=com.docker.compose.project=${PROJECT}" --filter "label=com.docker.compose.service=postgres")
if [[ -z "$(docker ps -aq "${filters[@]}")" ]]; then
  echo "identity-backup: compose project ${PROJECT} has no postgres container (not deployed here); nothing to dump"
  exit 0
fi
ctr="$(docker ps -q "${filters[@]}" | head -1)"
if [[ -z "${ctr}" ]]; then
  echo "identity-backup: the ${PROJECT} postgres container exists but is not running; no dump taken" >&2
  exit 1
fi

umask 077
install -d -m 700 "${BACKUP_DIR}"
out="${BACKUP_DIR}/identity-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
tmp="${out}.partial"
trap 'rm -f "${tmp}"' EXIT
docker exec "${ctr}" pg_dumpall -U logto | gzip >"${tmp}"
mv "${tmp}" "${out}"
trap - EXIT
echo "identity-backup: wrote ${out} ($(stat -c %s "${out}") bytes)"

mapfile -t old < <(find "${BACKUP_DIR}" -maxdepth 1 -name 'identity-*.sql.gz' -printf '%f\n' | sort -r | tail -n +"$((KEEP + 1))")
for f in "${old[@]}"; do
  rm -f -- "${BACKUP_DIR:?}/${f}"
  echo "identity-backup: retired ${f}"
done
