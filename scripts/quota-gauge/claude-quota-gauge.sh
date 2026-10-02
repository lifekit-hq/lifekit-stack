#!/usr/bin/env bash
# Export the shared Claude account quota as node-exporter textfile metrics.
# One gauge per quota window from `quota-axi --json`, written atomically into
# the textfile collector directory (arg 1). Runs from claude-quota-gauge.timer
# as the account that holds the Claude credentials; --no-credential-refresh
# keeps every read strictly read-only so the timer never races Claude Code's
# own token refresh. A failed read leaves the previous file in place.
# Besides the per-window gauges it exports the account-wide runway (all-models
# scope): seconds of usable quota left at the current pace, the projected
# exhaustion time, quota-axi's runway status as a label, and which window
# limits it. Runway lines are simply absent when quota-axi reports none.
set -euo pipefail
OUT_DIR="${1:-/var/lib/node_exporter/textfile}"
for d in "$HOME"/.nvm/versions/node/*/bin; do PATH="$d:$PATH"; done  # quota-axi is an nvm-installed npm CLI
tmp="$(mktemp "$OUT_DIR/.claude_quota.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
quota-axi --provider claude --no-credential-refresh --json | jq -r '
  def iso: sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdate;
  .providers[] | select(.provider == "claude") |
  (.windows[] |
    "claude_quota_percent_remaining{window=\"\(.id)\"} \(.percentRemaining | numbers)",
    "claude_quota_burn_multiple{window=\"\(.id)\"} \(.pace.burnMultiple | numbers)",
    "claude_quota_reserve_percent_points{window=\"\(.id)\"} \(.pace.reservePercentPoints | numbers)",
    "claude_quota_resets_at_seconds{window=\"\(.id)\"} \(.resetsAt | iso)"),
  (.quotaSemantics.effectiveAvailability[]? | select(.scope == "all_models") | .runway // empty |
    "claude_quota_runway_seconds \(.usableRunwaySeconds | numbers)",
    (.projectedExhaustedAt | strings | "claude_quota_projected_exhausted_at_seconds \(iso)"),
    (.status | strings | "claude_quota_runway_status{status=\"\(.)\"} 1"),
    (.limitingWindowId | strings | "claude_quota_limiting_window{window=\"\(.)\"} 1"))
' > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$OUT_DIR/claude_quota.prom"
