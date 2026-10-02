#!/usr/bin/env bash
# Export each consumer's imputed share of the shared Claude account's weekly
# usage as a node-exporter textfile metric, claude_quota_share_percent{consumer}.
# Runs hourly from claude-quota-share-gauge.timer as the account that holds the
# Claude credentials. The numbers come from scripts/quota-share (arg 2, default
# the deployed checkout); its residual consumer is not measured and is skipped.
# Exit 1 from quota_share.py only means "a consumer is over its share" - the
# report is still valid - while 2 means quota-axi could not be read. Whenever
# the report is not a JSON document with a consumers array (a crash also exits
# 1), or it carries warnings (an input source could not be read, so the shares
# would be wrong), the previous file stays in place and the warnings go to stderr.
set -euo pipefail
OUT_DIR="${1:-/var/lib/node_exporter/textfile}"
QUOTA_SHARE="${2:-/srv/lifekit-stack/scripts/quota-share/quota_share.py}"
for d in "$HOME"/.nvm/versions/node/*/bin; do PATH="$d:$PATH"; done  # quota-axi is an nvm-installed npm CLI
tmp="$(mktemp "$OUT_DIR/.claude_quota_share.XXXXXX")"
report="$(mktemp)"
trap 'rm -f "$tmp" "$report"' EXIT
rc=0
python3 "$QUOTA_SHARE" --json > "$report" || rc=$?
if [[ "$rc" -gt 1 ]]; then
  exit "$rc"
fi
jq -e '.consumers | arrays' "$report" > /dev/null
if ! jq -e '(.warnings // []) | length == 0' "$report" > /dev/null; then
  jq -r '.warnings[] | "quota-share: " + .' "$report" >&2
  exit 1
fi
jq -r '.consumers[] | select(.imputedUsedPct != null) |
  "claude_quota_share_percent{consumer=\"\(.name)\"} \(.imputedUsedPct)"' "$report" > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$OUT_DIR/claude_quota_share.prom"
