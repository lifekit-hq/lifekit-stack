#!/usr/bin/env bash
# install-kit-relay.sh — render kit-relay with the second mate's home pinned
# and install it root-owned as the forced command of the Kit relay SSH key
# (docs/runbook.md, "Kit's second-mate relay"). Not run by bootstrap or deploy:
# the home is operator input, and nothing in this repo hard-codes it.
#
#   sudo FM_HOME=/path/to/second-mate/home \
#     bash /srv/lifekit-stack/scripts/kit-relay/install-kit-relay.sh
#
#   FM_HOME=/path/to/second-mate/home bash install-kit-relay.sh --print
#     render to stdout only (no root, nothing installed) - to diff against the
#     installed copy before replacing it
#
# Root owns the installed copy, so the key's holder cannot rewrite what the
# key runs. Re-running converges.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/usr/local/libexec/kit-relay   # authorized_keys pins this path

die() { echo "install-kit-relay: $*" >&2; exit 1; }

mode=install
case "${1-}" in
  "") ;;
  --print) mode=print ;;
  *) die "usage: [sudo] FM_HOME=<home> $0 [--print]" ;;
esac

FM_HOME="${FM_HOME:-}"
[[ -n "$FM_HOME" ]] || die "set FM_HOME to the second mate's home"
# The value lands inside single quotes in the script: allow only plain path
# characters, so it can neither break the quoting nor the sed below.
[[ "$FM_HOME" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "FM_HOME must be an absolute path of [A-Za-z0-9._/-]"
[[ -x "$FM_HOME/bin/fm-inbox.sh" ]] || die "$FM_HOME/bin/fm-inbox.sh is missing or not executable"

rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
sed "s|__FM_HOME__|${FM_HOME}|" "$HERE/kit-relay" >"$rendered"
grep -q "^readonly FM_HOME='${FM_HOME}'\$" "$rendered" || die "render did not pin FM_HOME"
bash -n "$rendered" || die "rendered script does not parse"

if [[ "$mode" == print ]]; then
  cat "$rendered"
  exit 0
fi

[[ $EUID -eq 0 ]] || die "must run as root (sudo); --print renders without installing"
install -d -o root -g root -m 0755 "$(dirname "$DEST")"
install -o root -g root -m 0755 "$rendered" "$DEST"
echo "kit-relay installed -> $DEST (FM_HOME=$FM_HOME)"
