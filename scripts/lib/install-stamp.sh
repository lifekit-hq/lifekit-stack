#!/usr/bin/env bash
# scripts/lib/install-stamp.sh <component> - record that <component> was just
# installed from this checkout (issue 689). Every install-*/ensure-* script that
# deploys a component runs it as its last step, only on success and never in a
# --check, --print or dry-run mode, so a failed or partial install leaves the
# previous stamp (or none) in place.
#
# Writes /var/lib/lifekit/installed/<component> atomically, as
#   {"commit": "<HEAD of this checkout>", "at": "<UTC ISO-8601, first install of that commit>"}
# The release gauge (scripts/release-gauge/lifekit-release-gauge.sh) compares
# that commit with origin/main for the paths the component owns
# (scripts/release-gauge/components.txt) and exports whether it is behind.
#
# Run it as the account the installer runs as: root writes the directory as
# lifekit:lifekit 2775 (so the lifekit deploy account and the lifekit group can
# replace stamps too); lifekit owns /var/lib/lifekit already. A checkout whose
# HEAD cannot be read writes nothing and fails: an unstamped component shows as
# unknown, never current.
#
# Env: LIFEKIT_INSTALLED_DIR (the stamp directory; default
# $INSTALL_ROOT/var/lib/lifekit/installed, so an installer test that sets
# INSTALL_ROOT never touches the host).
set -euo pipefail

component="${1:-}"
[[ "$component" =~ ^[a-z0-9][a-z0-9-]*$ ]] || {
  echo "usage: $(basename "$0") <component>  (lowercase letters, digits, -)" >&2
  exit 2
}
repo="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
dir="${LIFEKIT_INSTALLED_DIR:-${INSTALL_ROOT:-}/var/lib/lifekit/installed}"

commit="$(git -c safe.directory="$repo" -C "$repo" rev-parse --verify --quiet 'HEAD^{commit}')" || {
  echo "install stamp: cannot read HEAD of $repo; $component is not stamped" >&2
  exit 1
}

# Re-running an installer at the same commit leaves the stamp (and its "at",
# the first install of that commit) untouched and prints nothing, so an
# idempotent installer's second run stays silent.
if [[ -r "$dir/$component" ]] && grep -q "\"commit\": \"$commit\"" "$dir/$component"; then
  exit 0
fi

if [[ ! -d "$dir" ]]; then
  if [[ $EUID -eq 0 && -z "${INSTALL_ROOT:-}" ]] && getent group lifekit > /dev/null; then
    install -d -o lifekit -g lifekit -m 2775 "$dir"
  else
    install -d -m 2775 "$dir"
  fi || {
    echo "install stamp: cannot create $dir as $(id -un); the account must be in the lifekit group (or run the installer with sudo); $component is not stamped" >&2
    exit 1
  }
fi
tmp="$(mktemp "$dir/.${component}.XXXXXX")" || {
  echo "install stamp: cannot write in $dir as $(id -un); the account must be in the lifekit group (or run the installer with sudo); $component is not stamped" >&2
  exit 1
}
trap 'rm -f "$tmp"' EXIT
printf '{"commit": "%s", "at": "%s"}\n' "$commit" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$dir/$component"
trap - EXIT
echo "install stamp: $component at ${commit:0:12} -> $dir/$component"
