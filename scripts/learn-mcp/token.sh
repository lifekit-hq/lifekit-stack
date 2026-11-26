#!/usr/bin/env bash
# token.sh - generate (or rotate) learn-mcp's bearer token, end to end.
#
#   bash scripts/learn-mcp/token.sh
#
# Run on the server from the admin account, in that account's own clone of
# this repo (not the deploy checkout /srv/lifekit-stack), with an operator age
# key (the captain's, or firstmate's via SOPS_AGE_KEY_FILE) - the same
# prerequisites as scripts/set-secret.sh, which does the SOPS half.
#
# It generates 32 random bytes as hex and, without printing them anywhere:
#   1. writes them, mode 0600, to the owner-only file (default
#      ~/.config/lifekit/learn-mcp.token; LEARN_MCP_TOKEN_FILE overrides) -
#      the one place the owner copies the value from, into the claude.ai
#      connector's `Authorization: Bearer` header;
#   2. hands them on stdin to scripts/set-secret.sh master LEARN_MCP_TOKEN,
#      which re-encrypts secrets/lifekit.env.sops and opens an auto-merging PR.
# The same command rotates: the old value stops working once the new one is
# rendered and learn-mcp is recreated (the steps it prints last).
# docs/runbook.md "learn-mcp (the study coach's connector)".
#
# Test hook (not for operators): LEARN_MCP_SET_SECRET replaces set-secret.sh.

set -euo pipefail
set +x
umask 077

# One function, parsed whole before it runs: set-secret.sh switches this
# checkout's branch, and bash must not read this file again mid-run.
main() {
  local root token_file dir tmp set_secret token
  root="$(cd "$(dirname "$0")/../.." && pwd)"
  token_file="${LEARN_MCP_TOKEN_FILE:-${HOME}/.config/lifekit/learn-mcp.token}"
  set_secret="${LEARN_MCP_SET_SECRET:-${root}/scripts/set-secret.sh}"
  command -v openssl >/dev/null 2>&1 || { echo "token: openssl not on PATH" >&2; exit 1; }

  dir="$(dirname "${token_file}")"
  mkdir -p "${dir}"
  chmod 700 "${dir}"
  tmp="$(mktemp "${token_file}.XXXXXX")"
  # Expanded now, not at exit: `tmp` is local and gone once main returns.
  # shellcheck disable=SC2064
  trap "rm -f -- '${tmp}'" EXIT

  token="$(openssl rand -hex 32)"
  [[ "${token}" =~ ^[0-9a-f]{64}$ ]] || { echo "token: openssl gave no token" >&2; exit 1; }
  printf '%s\n' "${token}" >"${tmp}"

  # The file is put in place only once the SOPS change is committed, so a
  # failed run leaves the owner's current token where it was.
  printf '%s' "${token}" | bash "${set_secret}" master LEARN_MCP_TOKEN
  unset token
  chmod 600 "${tmp}"
  mv -f "${tmp}" "${token_file}"
  trap - EXIT

  cat <<NEXT

learn-mcp token written to ${token_file} (mode 0600; this script printed it nowhere).
After the secrets PR above merges:
  1. sudo bash scripts/secrets/render-stack-env.sh
  2. redeploy (re-run the CI deploy, or scripts/deploy.sh as lifekit): learn-mcp is
     recreated with the new token
  3. paste the file's value into the claude.ai connector's Authorization header
     as "Bearer <value>" (docs/runbook.md "learn-mcp (the study coach's connector)")
NEXT
}

main "$@"
exit
