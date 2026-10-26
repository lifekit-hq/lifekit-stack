#!/bin/sh
# memory-vault-audit.sh - weekly vault audit. Run on the host by lifekit-vault-audit.timer
# (via run-vault-audit.py, MEMORY_VAULT=/srv/memory); until the OpenClaw cron
# `memory_vault_audit` is removed it also runs INSIDE the gateway container from the
# workspace mount, where MEMORY_VAULT is unset and the default below applies.
#
# Since 2026-09-14 the audit itself lives in the vault (`$VAULT/bin/audit.sh`, POSIX sh + awk):
# one implementation for PC hooks, on-demand runs, and this job. This wrapper only locates the
# vault, delegates, and keeps the delivery-required guard so a silent no-op registers as a
# failure. Its stdout is the one-line summary; run-vault-audit.py delivers it.
set -eu

VAULT="${MEMORY_VAULT:-/home/node/memory}"
DATE=$(date -u +%F)
ERR=$(mktemp)
trap 'rm -f "$ERR"' EXIT

[ -x "$VAULT/bin/audit.sh" ] || [ -f "$VAULT/bin/audit.sh" ] || {
  echo "AUDIT FAIL: $VAULT/bin/audit.sh not found - is the vault mounted and up to date?"; exit 1; }

if ! SUMMARY=$(sh "$VAULT/bin/audit.sh" --rotate --log 2>"$ERR"); then
  echo "AUDIT FAIL: bin/audit.sh errored: $(tail -1 "$ERR" 2>/dev/null)"; exit 1
fi

# delivery-required guard: a fresh report for today must exist
REPORT="$VAULT/audits/latest.md"
if [ ! -s "$REPORT" ] || ! head -12 "$REPORT" | grep -q "Vault audit - $DATE"; then
  echo "AUDIT FAIL: no fresh report written for $DATE"; exit 1
fi

# committed + pushed by the host memory-sync.timer
echo "Vault $SUMMARY"
