#!/usr/bin/env bash
# scripts/release-gauge/installed-behind.sh [<deploy checkout>] - print every
# host component whose installed copy is behind main (issue 734). Read-only:
# it runs the release gauge's drift half (lifekit-release-gauge.sh
# --installed-only) against the checkout into a temp directory, so the deploy
# log and the lifekit_installed.prom gauge always agree on what "behind" means
# (the install stamps in /var/lib/lifekit/installed/ against the paths each
# component owns in scripts/release-gauge/components.txt at origin/main).
#
# Output, one line per component behind main:
#   ✗ <component>: behind main since <UTC time> (installed <commit>)
# then one line naming every component with no usable stamp (never installed
# since stamps exist, or a stamp the checkout cannot resolve): those cannot be
# told current, so they are listed, never counted current.
#
# Exit 0 when every stamped component is current and none is unstamped, 1 when
# any is behind or unstamped, 2 when the drift read itself failed. deploy.sh
# runs this as the deploy account and never installs anything: the fix for a
# line is the component's installer, run as the host step its docs/runbook.md
# section names.
#
# Env: LIFEKIT_INSTALLED_DIR and RELEASE_STACK_REF, passed through to the gauge.
set -euo pipefail

here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
repo="${1:-$(cd "$here/../.." && pwd)}"
stamps="${LIFEKIT_INSTALLED_DIR:-/var/lib/lifekit/installed}"

out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
RELEASE_STACK_REPO="$repo" LIFEKIT_INSTALLED_DIR="$stamps" \
  bash "$here/lifekit-release-gauge.sh" --installed-only "$out" || {
  echo "installed drift: could not read the install stamps against $repo (output above)" >&2
  exit 2
}

declare -A since=()
behind=() unknown=()
while read -r name value; do
  case "$name" in
    lifekit_component_behind_since_seconds\{component=*)
      c="${name#*\"}"
      since["${c%%\"*}"]="$value"
      ;;
    lifekit_component_behind\{component=*)
      c="${name#*\"}"
      c="${c%%\"*}"
      case "$value" in
        1) behind+=("$c") ;;
        -1) unknown+=("$c") ;;
      esac
      ;;
  esac
done < <(grep -v '^#' "$out/lifekit_installed.prom")

for c in "${behind[@]}"; do
  at="$(date -u -d "@${since[$c]:-0}" '+%Y-%m-%d %H:%M UTC')"
  commit="$(sed -nE 's/.*"commit": *"([0-9a-f]{12})[0-9a-f]*".*/\1/p' "$stamps/$c" 2> /dev/null | head -n 1)"
  echo "✗ $c: behind main since $at (installed ${commit:-?})"
done
if ((${#unknown[@]})); then
  echo "? ${#unknown[@]} component(s) without a usable install stamp, so not known to be current: ${unknown[*]}"
fi
((${#behind[@]} + ${#unknown[@]} == 0))
