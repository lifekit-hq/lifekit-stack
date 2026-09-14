#!/bin/sh
# memory-vault-audit.sh - weekly vault audit, run by the OpenClaw cron `memory_vault_audit`
# INSIDE the gateway container from the workspace mount.
#
# Since 2026-09-14 the audit itself lives in the vault (`$VAULT/bin/audit.sh`, POSIX sh + awk):
# one implementation for PC hooks, on-demand runs, and this cron. This wrapper only locates the
# vault, delegates, and keeps the delivery-required guard so a silent no-op registers as a
# cron failure. Its stdout is the one-line Telegram summary.
set -eu

VAULT="${MEMORY_VAULT:-/home/node/.openclaw/wiki/main}"
DATE=$(date -u +%F)

[ -x "$VAULT/bin/audit.sh" ] || [ -f "$VAULT/bin/audit.sh" ] || {
  echo "AUDIT FAIL: $VAULT/bin/audit.sh not found - is the vault mounted and up to date?"; exit 1; }

if ! SUMMARY=$(sh "$VAULT/bin/audit.sh" --rotate --log 2>"/tmp/memory-audit-err.txt"); then
  echo "AUDIT FAIL: bin/audit.sh errored: $(tail -1 /tmp/memory-audit-err.txt 2>/dev/null)"; exit 1
fi

# delivery-required guard: a fresh report for today must exist
REPORT="$VAULT/audits/latest.md"
if [ ! -s "$REPORT" ] || ! head -12 "$REPORT" | grep -q "Vault audit - $DATE"; then
  echo "AUDIT FAIL: no fresh report written for $DATE"; exit 1
fi

# committed + pushed by the host memory-sync.timer
echo "Vault $SUMMARY"
