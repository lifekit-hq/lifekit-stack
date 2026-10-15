#!/usr/bin/env bash
# Export whether each lifekit-hq repo's Weekly Release run left its release PR
# unmerged, as node-exporter textfile metrics. The weekly-release.yml job merges
# the open "autorelease: pending" Release Please PR only after
# `gh pr checks --watch --fail-fast`, so a failing or missing check ends the run
# red with the PR still open, and nothing on the box saw it (2026-10-12
# finance-sentry, noticed three days later). The GitHub-hosted release runners
# cannot reach the box, so the box reads GitHub instead: the admin account's
# existing `gh` login, read-only. Atomic write into the textfile collector
# directory (arg 1), same shape as scripts/quota-gauge/claude-quota-gauge.sh.
# Run from lifekit-release-gauge.timer (installed by
# scripts/host-gauge/install-host-gauges.sh) - see docs/runbook.md
# "Weekly release gauge".
#
#   weekly_release_blocked{repo,pr_url,run_url}
#       1 when the repo's latest completed Weekly Release run did not succeed
#       and the pending release PR it should have merged is still open (the PR
#       existed when that run started); pr_url and run_url name them. Else 0
#       with both labels empty: a successful run (merged, or the "no pending
#       release PR" no-op), or a PR opened after the failed run.
#
# Inputs (env): RELEASE_REPOS, the space-separated owner/name list (default the
# four lifekit-hq repos that ship weekly-release.yml). If any read fails the
# script exits nonzero without touching weekly_release.prom, so the unit goes
# failed (host-unit-down-or-failed) and the file goes stale
# (textfile-collector-stale) instead of a blocked release reading as healthy.
set -euo pipefail
OUT_DIR="${1:-/var/lib/node_exporter/textfile}"
REPOS="${RELEASE_REPOS:-lifekit-hq/finance-sentry lifekit-hq/lifekit-stack lifekit-hq/lifekit-dashboard lifekit-hq/devclaw}"
WORKFLOW=weekly-release.yml
PENDING_LABEL="autorelease: pending"
SLUG_RE='^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
URL_RE='^https://[^"\\[:space:]]+$'

tmp="$(mktemp "$OUT_DIR/.weekly_release.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
gh_read() { timeout 60 gh "$@"; }

{
  echo "# HELP weekly_release_blocked 1 when the latest Weekly Release run failed and its release PR is still open."
  echo "# TYPE weekly_release_blocked gauge"
} > "$tmp"
for repo in $REPOS; do
  [[ "$repo" =~ $SLUG_RE ]] || {
    echo "lifekit-release-gauge: $repo is not an owner/name slug" >&2
    exit 2
  }
  run="$(gh_read api "repos/$repo/actions/workflows/$WORKFLOW/runs?status=completed&per_page=1" \
    --jq '.workflow_runs[0] // empty | [.conclusion // "unknown", .run_started_at, .html_url] | @tsv')"
  IFS=$'\t' read -r conclusion started run_url <<< "$run"
  pr_url="" blocked=0
  case "${conclusion:-success}" in
    success | skipped | neutral) ;;
    *)
      pr="$(gh_read pr list -R "$repo" --state open --label "$PENDING_LABEL" \
        --json url,createdAt --jq '.[0] // empty | [.url, .createdAt] | @tsv')"
      IFS=$'\t' read -r pr_url created <<< "$pr"
      # ISO-8601 UTC stamps from the same API compare as strings.
      if [[ -n "$pr_url" && ! "$created" > "$started" ]]; then
        blocked=1
      fi
      ;;
  esac
  if ((blocked)); then
    for url in "$pr_url" "$run_url"; do
      [[ "$url" =~ $URL_RE ]] || {
        echo "lifekit-release-gauge: $repo: unexpected URL '$url'" >&2
        exit 1
      }
    done
  else
    pr_url="" run_url=""
  fi
  echo "weekly_release_blocked{repo=\"$repo\",pr_url=\"$pr_url\",run_url=\"$run_url\"} $blocked" >> "$tmp"
done
chmod 644 "$tmp"
mv -f "$tmp" "$OUT_DIR/weekly_release.prom"
