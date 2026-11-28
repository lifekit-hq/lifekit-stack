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
# The same run exports installed-vs-repo drift (issue 689) into
# lifekit_installed.prom: each install-*/ensure-* script stamps
# /var/lib/lifekit/installed/<component> with the commit it installed
# (scripts/lib/install-stamp.sh), and this compares that commit with origin/main
# of the deploy checkout for the paths the component owns
# (scripts/release-gauge/components.txt, read at origin/main so a new row needs
# no gauge re-install):
#
#   lifekit_component_behind{component}
#       1 when a commit on origin/main that is not in the stamped commit changed
#       a path the component owns (merged, never installed); 0 when none did;
#       -1 unknown: no stamp, an unreadable stamp, or a commit the checkout
#       does not have. Unknown is never current.
#   lifekit_component_behind_since_seconds{component}
#       for a behind component only: the commit time of the oldest such commit
#       (squash merges, so the merge time), i.e. since when it has been behind.
#
# Drift inputs (env): RELEASE_STACK_REPO (the deploy checkout, default
# /srv/lifekit-stack, read-only), RELEASE_STACK_REF (default origin/main; the
# deploy fetches it on every merge), LIFEKIT_INSTALLED_DIR (default
# /var/lib/lifekit/installed). It runs first and on its own temp file: an
# unreadable checkout or manifest leaves lifekit_installed.prom untouched (it
# goes stale) and fails the unit after the release part has run.
#
# --installed-only (before the directory argument) writes lifekit_installed.prom
# and exits with the drift status, reading nothing from GitHub: deploy.sh runs
# it from the deploy checkout into a temp directory to print each component
# behind main (scripts/release-gauge/installed-behind.sh).
#
# Inputs (env): RELEASE_REPOS, the space-separated owner/name list (default the
# four lifekit-hq repos that ship weekly-release.yml). If any read fails the
# script exits nonzero without touching weekly_release.prom, so the unit goes
# failed (host-unit-down-or-failed) and the file goes stale
# (textfile-collector-stale) instead of a blocked release reading as healthy.
set -euo pipefail
INSTALLED_ONLY=0
if [[ "${1:-}" == --installed-only ]]; then
  INSTALLED_ONLY=1
  shift
fi
OUT_DIR="${1:-/var/lib/node_exporter/textfile}"
REPOS="${RELEASE_REPOS:-lifekit-hq/finance-sentry lifekit-hq/lifekit-stack lifekit-hq/lifekit-dashboard lifekit-hq/devclaw}"
WORKFLOW=weekly-release.yml
PENDING_LABEL="autorelease: pending"
SLUG_RE='^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
URL_RE='^https://[^"\\[:space:]]+$'

STACK_REPO="${RELEASE_STACK_REPO:-/srv/lifekit-stack}"
STACK_REF="${RELEASE_STACK_REF:-origin/main}"
STAMP_DIR="${LIFEKIT_INSTALLED_DIR:-/var/lib/lifekit/installed}"
MANIFEST=scripts/release-gauge/components.txt
COMPONENT_RE='^[a-z0-9][a-z0-9-]*$'

stack_git() { git -c safe.directory="$STACK_REPO" -C "$STACK_REPO" "$@"; }

# installed_drift <file>: the drift metrics into <file>; non-zero when the
# checkout, its ref or the manifest cannot be read.
installed_drift() {
  local out="$1" manifest component paths_line commit since stamp line
  local -a paths
  manifest="$(stack_git show "$STACK_REF:$MANIFEST")" || {
    echo "lifekit-release-gauge: cannot read $MANIFEST at $STACK_REF in $STACK_REPO as $(id -un); the account must be in the lifekit group (or run the drift read as lifekit)" >&2
    return 1
  }
  local behind="" sinces=""
  while IFS= read -r line; do
    line="${line%%#*}"
    read -r component paths_line <<< "$line" || true
    [[ -n "$component" ]] || continue
    [[ "$component" =~ $COMPONENT_RE ]] || {
      echo "lifekit-release-gauge: bad component name '$component' in $MANIFEST" >&2
      return 1
    }
    read -r -a paths <<< "$paths_line"
    ((${#paths[@]})) || {
      echo "lifekit-release-gauge: $component owns no paths in $MANIFEST" >&2
      return 1
    }
    stamp="$STAMP_DIR/$component"
    commit=""
    [[ -r "$stamp" ]] && commit="$(sed -nE 's/.*"commit": *"([0-9a-f]{40})".*/\1/p' "$stamp" | head -n 1)"
    if [[ -z "$commit" ]] || ! stack_git cat-file -e "$commit^{commit}" 2> /dev/null; then
      behind+="lifekit_component_behind{component=\"$component\"} -1"$'\n'
      continue
    fi
    since="$(stack_git log --format=%ct "$commit..$STACK_REF" -- "${paths[@]}" \
      | awk 'NR == 1 || $1 < m { m = $1 } END { if (NR) print m }')" || return 1
    if [[ -z "$since" ]]; then
      behind+="lifekit_component_behind{component=\"$component\"} 0"$'\n'
    else
      behind+="lifekit_component_behind{component=\"$component\"} 1"$'\n'
      sinces+="lifekit_component_behind_since_seconds{component=\"$component\"} $since"$'\n'
    fi
  done <<< "$manifest"
  {
    echo "# HELP lifekit_component_behind 1 when origin/main changed a path the component owns after its install stamp, 0 current, -1 unknown (no or unreadable stamp)."
    echo "# TYPE lifekit_component_behind gauge"
    printf '%s' "$behind"
    echo "# HELP lifekit_component_behind_since_seconds Commit time of the oldest origin/main change the behind component has not installed."
    echo "# TYPE lifekit_component_behind_since_seconds gauge"
    printf '%s' "$sinces"
  } > "$out"
}

drift_rc=0
drift_tmp="$(mktemp "$OUT_DIR/.lifekit_installed.XXXXXX")"
if installed_drift "$drift_tmp"; then
  chmod 644 "$drift_tmp"
  mv -f "$drift_tmp" "$OUT_DIR/lifekit_installed.prom"
else
  rm -f "$drift_tmp"
  drift_rc=1
fi
((INSTALLED_ONLY)) && exit "$drift_rc"

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
exit "$drift_rc"
