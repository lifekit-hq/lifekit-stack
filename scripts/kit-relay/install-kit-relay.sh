#!/usr/bin/env bash
# install-kit-relay.sh — render kit-relay with the homes it may act on pinned
# and install it root-owned as the forced command of the Kit relay SSH key
# (docs/runbook.md, "Kit's second-mate relay"). Not run by bootstrap or deploy:
# the home is operator input, and nothing in this repo hard-codes it.
#
#   sudo FM_HOME=/path/to/second-mate/home \
#     bash /srv/lifekit-stack/scripts/kit-relay/install-kit-relay.sh
#
#   sudo FM_HOME=/path/to/second-mate/home FM_HOME_ALT=/path/to/main/home \
#     bash /srv/lifekit-stack/scripts/kit-relay/install-kit-relay.sh
#     FM_HOME_ALT is optional: a second home a client may name (by its
#     basename) on the SSH command; FM_HOME stays the default when none is named
#
#   sudo FM_HOME=... TASKS_AXI=/abs/path/to/tasks-axi bash .../install-kit-relay.sh
#     TASKS_AXI is optional: pinning it turns on Kit's hold verb (a held item
#     filed as a shaping item, lifekit-stack#684) and installs its helper,
#     scripts/kit-held/kit_held.py with the intake_shape.py it imports, root-owned
#     under /usr/local/libexec/kit-held/. Without it hold is refused (exit 78).
#
#   FM_HOME=/path/to/second-mate/home bash install-kit-relay.sh --print
#     render to stdout only (no root, nothing installed) - to diff against the
#     installed copy before replacing it
#
# Root owns the installed copy, so the key's holder cannot rewrite what the
# key runs. Re-running converges.
#
# One installed script serves four authorized keys, told apart by the argument
# pinned in each authorized_keys command=: none (Kit's key, kit- request ids),
# "dashboard" (the dashboard's key, dash- ids), "finance-sentry"
# (finance-sentry's key, fs- ids) or "openclaw" (the gateway fleet-gate
# plugin's key, oc- ids). Installing does not touch authorized_keys;
# the operator adds each line (runbook).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/usr/local/libexec/kit-relay   # authorized_keys pins this path
HELD_DIR=/usr/local/libexec/kit-held

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

FM_HOME_ALT="${FM_HOME_ALT:-}"
if [[ -n "$FM_HOME_ALT" ]]; then
  [[ "$FM_HOME_ALT" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "FM_HOME_ALT must be an absolute path of [A-Za-z0-9._/-]"
  # Clients name a home by basename, so it must be a plain id, distinct from the default's.
  [[ "${FM_HOME_ALT##*/}" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] \
    || die "FM_HOME_ALT's last path part must match [a-z0-9][a-z0-9-]{0,63}"
  [[ "${FM_HOME_ALT##*/}" != "${FM_HOME##*/}" ]] || die "FM_HOME and FM_HOME_ALT must differ in their last path part"
  [[ -x "$FM_HOME_ALT/bin/fm-inbox.sh" ]] || die "$FM_HOME_ALT/bin/fm-inbox.sh is missing or not executable"
fi

TASKS_AXI="${TASKS_AXI:-}"
HELD_BIN=
if [[ -n "$TASKS_AXI" ]]; then
  [[ "$TASKS_AXI" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "TASKS_AXI must be an absolute path of [A-Za-z0-9._/-]"
  [[ -x "$TASKS_AXI" ]] || die "$TASKS_AXI is missing or not executable"
  [[ -x "$FM_HOME/bin/fm-captain-hold.sh" ]] || die "$FM_HOME/bin/fm-captain-hold.sh is missing or not executable"
  HELD_BIN="$HELD_DIR/kit_held.py"
fi

rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
sed -e "s|__FM_HOME__|${FM_HOME}|" -e "s|__FM_HOME_ALT__|${FM_HOME_ALT}|" \
  -e "s|__KIT_HELD__|${HELD_BIN}|" -e "s|__TASKS_AXI__|${TASKS_AXI}|" "$HERE/kit-relay" >"$rendered"
grep -q "^readonly DEFAULT_HOME='${FM_HOME}'\$" "$rendered" || die "render did not pin FM_HOME"
grep -q "^readonly ALT_HOME='${FM_HOME_ALT}'" "$rendered" || die "render did not pin FM_HOME_ALT"
grep -q "^readonly HELD_BIN='${HELD_BIN}'\$" "$rendered" || die "render did not pin the hold helper"
grep -q "^readonly TASKS_AXI_BIN='${TASKS_AXI}'\$" "$rendered" || die "render did not pin TASKS_AXI"
bash -n "$rendered" || die "rendered script does not parse"
grep -q '^  dashboard) ID_PREFIX=dash- ;;$' "$rendered" || die "rendered script lacks the dashboard sender"
grep -q '^  finance-sentry) ID_PREFIX=fs- ;;$' "$rendered" || die "rendered script lacks the finance-sentry sender"
grep -q '^  openclaw) ID_PREFIX=oc- ;;$' "$rendered" || die "rendered script lacks the openclaw sender"

if [[ "$mode" == print ]]; then
  cat "$rendered"
  exit 0
fi

[[ $EUID -eq 0 ]] || die "must run as root (sudo); --print renders without installing"
install -d -o root -g root -m 0755 "$(dirname "$DEST")"
if [[ -n "$HELD_BIN" ]]; then
  install -d -o root -g root -m 0755 "$HELD_DIR"
  install -o root -g root -m 0644 "$HERE/../intake-shape/intake_shape.py" "$HELD_DIR/intake_shape.py"
  install -o root -g root -m 0755 "$HERE/../kit-held/kit_held.py" "$HELD_BIN"
fi
install -o root -g root -m 0755 "$rendered" "$DEST"
echo "kit-relay installed -> $DEST (FM_HOME=$FM_HOME${FM_HOME_ALT:+ FM_HOME_ALT=$FM_HOME_ALT}${TASKS_AXI:+ TASKS_AXI=$TASKS_AXI, hold helper $HELD_BIN})"

# Record the installed commit (issue 689; the release gauge reads it).
bash "$(dirname "${BASH_SOURCE[0]}")/../lib/install-stamp.sh" kit-relay
