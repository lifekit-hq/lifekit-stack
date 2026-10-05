#!/usr/bin/env bash
# init-gateway-key.sh - mint the gateway resolver's age identity on the host.
#
# Run once per box, as root (sudo). docs/secrets.md, docs/secrets-runbook.md.
#
#   /srv/lifekit-secrets/                       root:lifekit 0750  (holds the rendered stack.env; mounted nowhere)
#   /srv/lifekit-secrets/gateway/               root:lifekit 0750  (the only part any container sees)
#   /srv/lifekit-secrets/gateway/lifekit-gateway.agekey   lifekit:lifekit 0400
#
# The private key is written straight from age-keygen into the file: it is
# never printed, never in a shell variable, never in this process's stdout.
# Only the PUBLIC recipient is printed, for .sops.yaml. Idempotent: an
# existing key is left alone and its recipient re-printed.
#
# The lifekit account (uid 1000) is the container user, so the gateway's
# resolver can read the file and no other account can. OpenClaw's own caveat
# still holds: an exec-capable agent inside the gateway runs as that same uid
# and can read what the gateway can read - which is exactly why this key
# opens the gateway file only.

set -euo pipefail

SECRETS_DIR="${LIFEKIT_SECRETS_DIR:-/srv/lifekit-secrets}"
LIFEKIT_USER="${LIFEKIT_USER:-lifekit}"
KEY_FILE="${SECRETS_DIR}/gateway/lifekit-gateway.agekey"

if [[ $EUID -ne 0 ]]; then
  echo "init-gateway-key: run as root (sudo)." >&2
  exit 1
fi

# age-keygen: on this box the admin account carries it (~/.local/bin), so
# look there too when sudo's secure_path hides it.
AGE_KEYGEN="$(command -v age-keygen || true)"
if [[ -z "${AGE_KEYGEN}" && -n "${SUDO_USER:-}" ]]; then
  AGE_KEYGEN="$(getent passwd "${SUDO_USER}" | cut -d: -f6)/.local/bin/age-keygen"
fi
if [[ ! -x "${AGE_KEYGEN}" ]]; then
  echo "init-gateway-key: age-keygen not found (https://github.com/FiloSottile/age/releases)." >&2
  exit 1
fi

install -d -o root -g "${LIFEKIT_USER}" -m 0750 "${SECRETS_DIR}" "${SECRETS_DIR}/gateway"

if [[ -f "${KEY_FILE}" ]]; then
  echo "init-gateway-key: ${KEY_FILE} already exists; leaving it alone."
else
  "${AGE_KEYGEN}" 2>/dev/null | install -o "${LIFEKIT_USER}" -g "${LIFEKIT_USER}" -m 0400 /dev/stdin "${KEY_FILE}"
  echo "init-gateway-key: wrote ${KEY_FILE} (${LIFEKIT_USER}, 0400)"
fi

echo "gateway recipient (public, goes into .sops.yaml under secrets/lifekit-gateway.env.sops):"
"${AGE_KEYGEN}" -y "${KEY_FILE}"
echo
echo "Next: add it to .sops.yaml, then 'sops updatekeys -y --input-type dotenv secrets/lifekit-gateway.env.sops' (or the first"
echo "import-legacy-env.sh run) as the captain, merge, and copy the private key into KeePassXC:"
echo "  sudo cat ${KEY_FILE}"
