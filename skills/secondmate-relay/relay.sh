#!/bin/sh
# relay.sh - Kit's client for the kit-relay forced command on the host, and a
# read of the fleet status the dashboard serves.
#   relay.sh note <body-file> [<home>]   send the file's bytes, unchanged
#   relay.sh replies [<cursor>] [<home>] notes sent from here, with their replies
#   relay.sh status                      the fleet summary JSON, unchanged
# <home> is fm-vps (the default) or firstmate; the host checks it again against
# the homes it pins. The request id is derived from the body and the UTC day, so
# a retry of the same text is a replay, never a second note.
set -eu
CONF=/run/lifekit/kit-relay/ssh_config
FLEET_URL=http://lifekit-dashboard:8080/api/fleet
USAGE="usage: relay.sh note <body-file> [<home>] | replies [<cursor>] [<home>] | status  (<home>: fm-vps | firstmate)"

home_ok() {
  case "$1" in
    fm-vps | firstmate) return 0 ;;
  esac
  echo "relay.sh: unknown home: $1 (fm-vps | firstmate)" >&2
  return 1
}

case "${1-}" in
  note)
    [ "$#" -le 3 ] || { echo "$USAGE" >&2; exit 64; }
    [ -s "${2-}" ] || { echo "relay.sh: body file missing or empty" >&2; exit 64; }
    [ -z "${3-}" ] || home_ok "$3" || exit 64
    rid="kit-$(date -u +%Y%m%d)-$(sha256sum "$2" | cut -c1-16)"
    exec ssh -F "$CONF" relay note "$rid" ${3:+"$3"} <"$2"
    ;;
  replies)
    [ "$#" -le 3 ] || { echo "$USAGE" >&2; exit 64; }
    cursor=
    home=
    case "${2-}" in
      '') ;;
      *[!0-9]*) home=$2 ;;
      *) cursor=$2 ;;
    esac
    if [ -n "$cursor" ]; then
      home=${3-}
    else
      [ "$#" -le 2 ] || { echo "$USAGE" >&2; exit 64; }
    fi
    [ -z "$home" ] || home_ok "$home" || exit 64
    exec ssh -F "$CONF" relay receipts ${cursor:+"$cursor"} ${home:+"$home"} </dev/null
    ;;
  status)
    [ "$#" -eq 1 ] || { echo "$USAGE" >&2; exit 64; }
    exec curl -fsS -m 10 "$FLEET_URL"
    ;;
  *)
    echo "$USAGE" >&2
    exit 64
    ;;
esac
