#!/usr/bin/env bash
# google-mcp-bootstrap.sh — prepare the host for the google-workspace-mcp
# compose service.
#
# What it does:
#   - Creates /srv/google-workspace-mcp/credentials/ if missing.
#   - Sets owner to lifekit:lifekit (matches sibling /srv/openclaw/* dirs created
#     by bootstrap-vps.sh).
#   - Sets mode 0700 (refresh tokens are bearer-equivalent secrets).
#
# Idempotent: re-running it is safe; each step is a no-op when state is already
# correct.
#
# Run as root on the VPS, before `docker compose -p openclaw up -d google-workspace-mcp`:
#
#     sudo ./scripts/google-mcp-bootstrap.sh
#
# After this script: scp your refresh-token credentials from your PC, then
# bring the service up. See docs/google-mcp-setup.md for the full flow.

set -euo pipefail

LIFEKIT_USER="${LIFEKIT_USER:-lifekit}"
CREDS_DIR="${GOOGLE_MCP_CREDS_DIR:-/srv/google-workspace-mcp/credentials}"

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

if ! id -u "$LIFEKIT_USER" >/dev/null 2>&1; then
  echo "User '$LIFEKIT_USER' does not exist — run scripts/bootstrap-vps.sh first." >&2
  exit 1
fi

# install -d is the idempotent way: creates if missing, no-ops if present,
# and re-applies owner/mode on every run.
install -d -o "$LIFEKIT_USER" -g "$LIFEKIT_USER" -m 0700 "$CREDS_DIR"

echo "✓ $CREDS_DIR ready (owner=$LIFEKIT_USER, mode=0700)"
echo
echo "Next steps:"
echo "  1. On your PC, generate the refresh token (see docs/google-mcp-setup.md)."
echo "  2. scp ~/.google_workspace_mcp/credentials/* \\"
echo "         ${LIFEKIT_USER}@<vps>:${CREDS_DIR}/"
echo "  3. docker compose -p openclaw -f /srv/lifekit-stack/compose/openclaw/docker-compose.yml \\"
echo "         --env-file /srv/lifekit-secrets/stack.env up -d google-workspace-mcp"
