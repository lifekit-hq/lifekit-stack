#!/usr/bin/env bash
# render-stack-env.sh - decrypt the master file into the compose env file.
#
#   sudo bash scripts/secrets/render-stack-env.sh
#
# Runs as root via sudo from an operator's account: sops finds the operator
# age key (captain's by default, or firstmate's via SOPS_AGE_KEY_FILE) at the
# INVOKING user's ~/.config/sops/age/keys.txt (SUDO_USER), the
# plaintext goes through a pipe straight into `install`, and lands at
# /srv/lifekit-secrets/stack.env as root:lifekit 0640 - readable by the deploy
# account for `docker compose --env-file`, mounted into no container, and
# outside the OpenClaw state dir. PARKED_* entries (secrets with no consumer,
# docs/secrets.md) are dropped on the way: the rendered file carries only
# what compose interpolates.
#
# Re-run after every merged change to secrets/lifekit.env.sops, then
# scripts/deploy.sh (or re-run the CI deploy) so the changed services are
# recreated. docs/secrets-runbook.md has the per-class sequence.

set -euo pipefail

cd "$(dirname "$0")/../.."

SECRETS_DIR="${LIFEKIT_SECRETS_DIR:-/srv/lifekit-secrets}"
LIFEKIT_USER="${LIFEKIT_USER:-lifekit}"
MASTER=secrets/lifekit.env.sops
OUT="${SECRETS_DIR}/stack.env"

# Unprivileged runs (the rebuild dry run in docs/secrets-runbook.md, with
# LIFEKIT_SECRETS_DIR pointing at a scratch directory) keep the mode and skip
# the ownership; the production path is root:lifekit.
INSTALL_OWNER=()
if [[ $EUID -eq 0 ]]; then
  INSTALL_OWNER=(-o root -g "${LIFEKIT_USER}")
elif [[ "${SECRETS_DIR}" == /srv/* ]]; then
  echo "render-stack-env: run with sudo (the file is installed root:${LIFEKIT_USER} 0640)." >&2
  exit 1
fi

# The operator key (captain's by default, or firstmate's): SOPS_AGE_KEY_FILE if set (sudo -E), else the invoking
# user's default sops location.
if [[ -z "${SOPS_AGE_KEY_FILE:-}" && -n "${SUDO_USER:-}" ]]; then
  SOPS_AGE_KEY_FILE="$(getent passwd "${SUDO_USER}" | cut -d: -f6)/.config/sops/age/keys.txt"
fi
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-/root/.config/sops/age/keys.txt}"
if [[ ! -r "${SOPS_AGE_KEY_FILE}" ]]; then
  echo "render-stack-env: no operator age key at ${SOPS_AGE_KEY_FILE} (restore the captain's from KeePassXC, or point SOPS_AGE_KEY_FILE at firstmate's)." >&2
  exit 1
fi
SOPS="$(command -v sops || true)"
if [[ -z "${SOPS}" && -n "${SUDO_USER:-}" ]]; then
  SOPS="$(getent passwd "${SUDO_USER}" | cut -d: -f6)/.local/bin/sops"
fi
[[ -x "${SOPS}" ]] || { echo "render-stack-env: sops not found" >&2; exit 1; }
[[ -s "${MASTER}" ]] || { echo "render-stack-env: ${MASTER} is missing" >&2; exit 1; }

install -d "${INSTALL_OWNER[@]}" -m 0750 "${SECRETS_DIR}"
"${SOPS}" --decrypt --input-type dotenv --output-type dotenv "${MASTER}" \
  | grep -v -E '^PARKED_' \
  | install "${INSTALL_OWNER[@]}" -m 0640 /dev/stdin "${OUT}"
echo "render-stack-env: wrote ${OUT} ($(grep -c -E '^[A-Z]' "${OUT}") entries, mode 0640)"
