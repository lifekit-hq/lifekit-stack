#!/bin/sh
# relay.sh - Kit's client for the kit-relay forced command on the host, and a
# read of the fleet status the dashboard serves.
#   relay.sh note <body-file> [<home>] [ENVELOPE]
#                                        send the file's bytes, unchanged
#   relay.sh hold <item-file> [<home>] ENVELOPE --fingerprint F --thread T
#                                        file a held item that names a fleet task
#                                        as a shaping item on the Calls page
#   relay.sh replies [<cursor>] [<home>] notes sent from here, with their replies
#   relay.sh status                      the fleet summary JSON, unchanged
# <home> is fm-vps (the default) or firstmate; the host checks it again against
# the homes it pins. The request id is derived from the body and the UTC day, so
# a retry of the same text is a replay, never a second note.
#
# ENVELOPE is --origin O --owner W --class C --acceptance A [--thread T]
# [--fingerprint F] (class: dev | ops | finance | life | unclear). With any of
# them the note body starts with the envelope as a leading block, the request id
# comes from the thread (default: the body's digest) instead of the day, so a
# note with the same thread is a replay of the first, never a second note, and
# a third line follows the host's two: the receipt {"id","url","state"}, state
# submitted for a new note and working for a thread already open. The relay's
# fingerprint is the thread. Docs: docs/runbook.md, "Envelope and receipt".
#
# hold takes the whole envelope, all six fields, and files the item the moment
# Kit holds it instead of leaving it in a daily note (lifekit-stack#684). The
# fingerprint names the item: the same fingerprint again counts a repeat on the
# open shaping item. Owner kit (a reminder to Kit itself) is refused - that
# stays in the note. The host prints the receipt {"id","url","state","count"}.
# Docs: docs/runbook.md, "Held items become shaping items".
set -eu
CONF=/run/lifekit/kit-relay/ssh_config
FLEET_URL=http://lifekit-dashboard:8080/api/fleet
USAGE="usage: relay.sh note <body-file> [<home>] [--origin O --owner W --class C --acceptance A [--thread T]] | hold <item-file> [<home>] --origin O --owner W --class C --acceptance A --thread T --fingerprint F | replies [<cursor>] [<home>] | status  (<home>: fm-vps | firstmate)"

home_ok() {
  case "$1" in
    fm-vps | firstmate) return 0 ;;
  esac
  echo "relay.sh: unknown home: $1 (fm-vps | firstmate)" >&2
  return 1
}

case "${1-}" in
  note | hold)
    verb=$1
    shift
    env_origin='' env_owner='' env_class='' env_acceptance='' env_thread='' env_fp=''
    enveloped='' pos=0 body='' home=''
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --origin | --owner | --class | --acceptance | --thread | --fingerprint)
          [ "$#" -ge 2 ] || { echo "$USAGE" >&2; exit 64; }
          case "$2" in
            *'
'*) echo "relay.sh: envelope values are one line" >&2; exit 64 ;;
          esac
          v=$(printf '%s' "$2" | tr -s ' \t' ' ')
          enveloped=1
          case "$1" in
            --origin) env_origin=$v ;;
            --owner) env_owner=$v ;;
            --class) env_class=$v ;;
            --acceptance) env_acceptance=$v ;;
            --thread) env_thread=$v ;;
            --fingerprint) env_fp=$v ;;
          esac
          shift 2
          ;;
        --*) echo "$USAGE" >&2; exit 64 ;;
        *)
          pos=$((pos + 1))
          case "$pos" in
            1) body=$1 ;;
            2) home=$1 ;;
            *) echo "$USAGE" >&2; exit 64 ;;
          esac
          shift
          ;;
      esac
    done
    [ -s "$body" ] || { echo "relay.sh: body file missing or empty" >&2; exit 64; }
    [ -z "$home" ] || home_ok "$home" || exit 64
    if [ "$verb" = hold ]; then
      missing=
      [ -n "$env_origin" ] || missing="$missing origin"
      [ -n "$env_owner" ] || missing="$missing owner"
      [ -n "$env_class" ] || missing="$missing class"
      [ -n "$env_fp" ] || missing="$missing fingerprint"
      [ -n "$env_acceptance" ] || missing="$missing acceptance"
      [ -n "$env_thread" ] || missing="$missing thread"
      [ -z "$missing" ] || { echo "relay.sh: envelope missing:$missing" >&2; exit 64; }
      case "$env_class" in
        dev | ops | finance | life | unclear) ;;
        *) echo "relay.sh: envelope class must be dev, ops, finance, life or unclear" >&2; exit 64 ;;
      esac
      case "$(printf '%s' "$env_owner" | tr '[:upper:]' '[:lower:]')" in
        kit | me | self | myself)
          echo "relay.sh: a reminder to Kit itself is not filed; keep it in the daily note" >&2
          exit 65
          ;;
      esac
      sent=$(mktemp)
      trap 'rm -f "$sent"' EXIT
      {
        printf -- '---\norigin: %s\nowner: %s\nclass: %s\nfingerprint: %s\nacceptance: %s\nthread: %s\n---\n' \
          "$env_origin" "$env_owner" "$env_class" "$env_fp" "$env_acceptance" "$env_thread"
        cat "$body"
      } >"$sent"
      # Same text the same UTC day is a replay (no second count); a repeat on
      # a later day, or in other words, counts.
      rid="kit-held-$(sha256sum "$sent" | cut -c1-16)-$(date -u +%Y%m%d)"
      # No exec: the EXIT trap removes the temp file.
      st=0
      ssh -F "$CONF" relay hold "$rid" ${home:+"$home"} <"$sent" || st=$?
      exit "$st"
    fi
    if [ -z "$enveloped" ]; then
      rid="kit-$(date -u +%Y%m%d)-$(sha256sum "$body" | cut -c1-16)"
      exec ssh -F "$CONF" relay note "$rid" ${home:+"$home"} <"$body"
    fi
    # A door refuses an item missing a field.
    missing=
    [ -n "$env_origin" ] || missing="$missing origin"
    [ -n "$env_owner" ] || missing="$missing owner"
    [ -n "$env_class" ] || missing="$missing class"
    [ -n "$env_acceptance" ] || missing="$missing acceptance"
    [ -z "$missing" ] || { echo "relay.sh: envelope missing:$missing" >&2; exit 64; }
    case "$env_class" in
      dev | ops | finance | life | unclear) ;;
      *) echo "relay.sh: envelope class must be dev, ops, finance, life or unclear" >&2; exit 64 ;;
    esac
    [ -n "$env_thread" ] || env_thread="kit-$(sha256sum "$body" | cut -c1-16)"
    # The relay's fingerprint is the thread id.
    env_fp=$env_thread
    case "$env_thread" in
      *[!A-Za-z0-9._:\ -]*) echo "relay.sh: thread: letters, digits, space and ._:- only" >&2; exit 64 ;;
    esac
    rid="kit-thread-$(printf '%s' "$env_thread" | sha256sum | cut -c1-16)"
    sent=$(mktemp)
    trap 'rm -f "$sent"' EXIT
    {
      printf -- '---\norigin: %s\nowner: %s\nclass: %s\nfingerprint: %s\nacceptance: %s\nthread: %s\n---\n' \
        "$env_origin" "$env_owner" "$env_class" "$env_fp" "$env_acceptance" "$env_thread"
      cat "$body"
    } >"$sent"
    st=0
    out=$(ssh -F "$CONF" relay note "$rid" ${home:+"$home"} <"$sent") || st=$?
    [ -z "$out" ] || printf '%s\n' "$out"
    # The receipt: the id the host returned, in the A2A state names. A thread
    # already open answers with the first note's id and state working.
    nid=$(printf '%s\n' "$out" | sed -n 's/.*"id": *"\([A-Za-z0-9._:-]*\)".*/\1/p' | tail -n 1)
    if [ -n "$nid" ]; then
      case "$out" in
        *'"outcome":'*'"replay"'*) state=working ;;
        *) state=submitted ;;
      esac
      printf '{"id":"%s","url":"relay.sh replies %s","state":"%s"}\n' "$nid" "${home:-fm-vps}" "$state"
    fi
    exit "$st"
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
