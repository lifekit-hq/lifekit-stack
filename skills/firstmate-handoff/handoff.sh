#!/bin/sh
# handoff.sh - an OpenClaw agent hands a dev-class problem to firstmate's inbox.
#   handoff.sh send <agent> <body-file>   file one note to the main firstmate
#   handoff.sh replies [<cursor>]         the notes sent from here, with replies
# <agent> is kit or ledger. This is the kit-relay path secondmate-relay uses
# (same key, same forced command, same kit- request ids); nothing here is a new
# channel. The note is the file's text under one header line naming the agent,
# so firstmate reads "task: dev problem from <agent>" and files the issue in the
# owning repo. The request id is derived from the note and the UTC day, so a
# retry of the same note is a replay, never a second one.
set -eu
CONF=/run/lifekit/kit-relay/ssh_config
HOME_ID=firstmate
USAGE="usage: handoff.sh send <kit|ledger> <body-file> | replies [<12-digit cursor>]"

case "${1-}" in
  send)
    [ "$#" -eq 3 ] || { echo "$USAGE" >&2; exit 64; }
    case "$2" in
      kit | ledger) ;;
      *) echo "handoff.sh: unknown agent: $2 (kit | ledger)" >&2; exit 64 ;;
    esac
    [ -s "$3" ] || { echo "handoff.sh: body file missing or empty" >&2; exit 64; }
    for key in what evidence; do
      grep -q "^$key: *[^ ]" "$3" \
        || { echo "handoff.sh: the note needs a '$key:' line (see SKILL.md)" >&2; exit 64; }
    done
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' EXIT
    { printf 'task: dev problem from %s\n' "$2"; cat "$3"; } >"$tmp"
    rid="kit-$(date -u +%Y%m%d)-$2-$(sha256sum "$tmp" | cut -c1-16)"
    ssh -F "$CONF" relay note "$rid" "$HOME_ID" <"$tmp"
    ;;
  replies)
    [ "$#" -le 2 ] || { echo "$USAGE" >&2; exit 64; }
    case "${2-}" in
      *[!0-9]*) echo "$USAGE" >&2; exit 64 ;;
    esac
    exec ssh -F "$CONF" relay receipts ${2:+"$2"} "$HOME_ID" </dev/null
    ;;
  *)
    echo "$USAGE" >&2
    exit 64
    ;;
esac
