#!/usr/bin/env bash
# ask-agent.sh <agent> "<question>" - ask a gateway agent a question over the
# inbound hook. The agent answers firstmate with its relay skill, so the
# answer arrives as an ordinary inbox note carrying the request id this script
# prints. Nothing is queued: one POST, one id.
#
# Configuration (environment; nothing is read from the repo):
#   ASK_AGENT_HOOK_URL         full URL of the gateway's direct agent hook
#                              (required)
#   ASK_AGENT_HOOK_TOKEN_FILE  path of a file holding the hook token (required
#                              unless ASK_AGENT_HOOK_TOKEN is set)
#   ASK_AGENT_HOOK_TOKEN       the token itself, for callers that already hold it
#   ASK_AGENT_RELAY_HOME       home name the answer note goes to (default firstmate)
#   ASK_AGENT_CURL             curl binary (tests)
#
# The token is only ever passed to curl on stdin (a header config), never on a
# command line, and is never printed. See docs/runbook.md "Asking another agent".

set -euo pipefail

CURL="${ASK_AGENT_CURL:-curl}"
RELAY_HOME="${ASK_AGENT_RELAY_HOME:-firstmate}"

die() {
  echo "ask-agent: $*" >&2
  exit "${2:-1}"
}

(($# == 2)) || die 'usage: ask-agent.sh <finance|kit> "<question>"' 64
agent="$1"
question="$2"

case "$agent" in
  finance | kit) ;;
  *) die "agent must be finance or kit (the hook's allowedAgentIds), got: $agent" 64 ;;
esac
[[ -n "${question//[[:space:]]/}" ]] || die "the question is empty" 64
[[ "$RELAY_HOME" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] || die "ASK_AGENT_RELAY_HOME must match [a-z0-9][a-z0-9-]*" 64

url="${ASK_AGENT_HOOK_URL:-}"
[[ -n "$url" ]] || die "ASK_AGENT_HOOK_URL is not set (the gateway's /hooks/agent URL); the operator provisions it" 78

token="${ASK_AGENT_HOOK_TOKEN:-}"
if [[ -z "$token" ]]; then
  tfile="${ASK_AGENT_HOOK_TOKEN_FILE:-}"
  [[ -n "$tfile" ]] || die "no hook token configured: set ASK_AGENT_HOOK_TOKEN_FILE to the token file the operator provisions" 78
  [[ -r "$tfile" ]] || die "hook token file is missing or unreadable: $tfile" 78
  token="$(head -n 1 "$tfile")"
fi
[[ "$token" =~ ^[A-Za-z0-9._~+/=-]{8,}$ ]] || die "hook token is empty or malformed" 78

req_id="ask-$(date +%s)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"

message="Question from firstmate, request id ${req_id}. Answer it yourself from your own tools and knowledge; do not guess. Send the answer to firstmate, not to Telegram or any chat: write a relay note whose first line is 'ask ${req_id}' followed by the answer, and send it with relay.sh note <body file> ${RELAY_HOME} (secondmate-relay skill). Send nothing else and nothing to the owner. If you cannot answer, say so in that same note. Question: ${question}"

payload="$(jq -n --arg m "$message" --arg a "$agent" --arg n "firstmate-ask ${req_id}" \
  '{message: $m, name: $n, agentId: $a, deliver: false, wakeMode: "now", thinking: "low", timeoutSeconds: 300}')"

status="$(printf 'header = "Authorization: Bearer %s"\n' "$token" |
  "$CURL" -sS -o /dev/null -w '%{http_code}' --max-time 30 -K - \
    -X POST -H 'Content-Type: application/json' -H "Idempotency-Key: ${req_id}" \
    --data "$payload" "$url")" || die "could not reach the hook" 69

case "$status" in
  2??) echo "asked ${agent}: request ${req_id}" ;;
  401 | 403) die "the gateway refused the hook token (HTTP ${status})" 77 ;;
  *) die "the gateway answered HTTP ${status}" 69 ;;
esac
