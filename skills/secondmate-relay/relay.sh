#!/bin/sh
# relay.sh - Kit's client for the kit-relay forced command on the host.
#   relay.sh note <body-file>    send the file's bytes, unchanged
#   relay.sh replies [<cursor>]  notes sent from here, with their replies
# The request id is derived from the body and the UTC day, so a retry of the
# same text is a replay, never a second note.
set -eu
CONF=/run/lifekit/kit-relay/ssh_config
case "${1-}" in
  note)
    [ -s "${2-}" ] || { echo "relay.sh: body file missing or empty" >&2; exit 64; }
    rid="kit-$(date -u +%Y%m%d)-$(sha256sum "$2" | cut -c1-16)"
    exec ssh -F "$CONF" relay note "$rid" <"$2"
    ;;
  replies)
    exec ssh -F "$CONF" relay receipts ${2:+"$2"} </dev/null
    ;;
  *)
    echo "usage: relay.sh note <body-file> | replies [<cursor>]" >&2
    exit 64
    ;;
esac
