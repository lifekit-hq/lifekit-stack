#!/usr/bin/env bash
# edit.sh - change a secret in place. sops decrypts into $EDITOR and
# re-encrypts on save; no plaintext file is written (finance-sentry's
# docker/secrets-edit.sh, same shape).
#
#   scripts/secrets/edit.sh master     # secrets/lifekit.env.sops   (operator key)
#   scripts/secrets/edit.sh gateway    # secrets/lifekit-gateway.env.sops (operator key)
#
# Then: pytest scripts/tests/test_secrets.py, commit, PR. Git history is the
# change log. After the merge the class-specific steps in
# docs/secrets-runbook.md apply (render + deploy, or `openclaw secrets reload`).

set -euo pipefail

cd "$(dirname "$0")/../.."

case "${1:-}" in
  master)  FILE=secrets/lifekit.env.sops ;;
  gateway) FILE=secrets/lifekit-gateway.env.sops ;;
  *) echo "usage: $0 <master|gateway>" >&2; exit 2 ;;
esac

command -v sops >/dev/null 2>&1 || { echo "edit: sops not on PATH" >&2; exit 1; }
KEYFILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"
[[ -f "${KEYFILE}" ]] || { echo "edit: no age key at ${KEYFILE} (restore it from KeePassXC)" >&2; exit 1; }
[[ -s "${FILE}" ]] || { echo "edit: ${FILE} does not exist yet (scripts/secrets/import-legacy-env.sh)" >&2; exit 1; }

SOPS_AGE_KEY_FILE="${KEYFILE}" exec sops --input-type dotenv --output-type dotenv "${FILE}"
