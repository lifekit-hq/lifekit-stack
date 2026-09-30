#!/usr/bin/env bash
# install-alert-inbox.sh — install the alert-inbox poller as a systemd timer
# (root), replacing the operator-crontab line. bootstrap-vps.sh calls it; on a
# live box run it alone:
#
#   sudo FM_INBOX_BIN=/path/to/fm-inbox.sh FM_INBOX_HOME=/path/to/target/home \
#     bash /srv/lifekit-stack/scripts/alert-inbox/install-alert-inbox.sh
#
# FM_INBOX_BIN and FM_INBOX_HOME are written to /etc/lifekit/alert-inbox.env
# (the unit's EnvironmentFile). Without them the poller has nowhere to send
# transitions, so the install is skipped when neither the variables nor an
# existing env file are present. The poller runs as ALERT_INBOX_USER
# (default ADMIN_USER, then denys).

set -euo pipefail

ADMIN_USER="${ADMIN_USER:-denys}"
ALERT_INBOX_USER="${ALERT_INBOX_USER:-$ADMIN_USER}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$HERE/../.." && pwd)}"
ENV_FILE=/etc/lifekit/alert-inbox.env

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

if [[ -n "${FM_INBOX_BIN:-}" ]]; then
  install -d -m 755 /etc/lifekit
  {
    echo "FM_INBOX_BIN=$FM_INBOX_BIN"
    [[ -n "${FM_INBOX_HOME:-}" ]] && echo "FM_INBOX_HOME=$FM_INBOX_HOME"
  } > "$ENV_FILE"
  chmod 644 "$ENV_FILE"
elif [[ ! -f "$ENV_FILE" ]]; then
  echo "alert-inbox: set FM_INBOX_BIN (and FM_INBOX_HOME) or create $ENV_FILE; skipping install." >&2
  exit 0
fi

sed -e "s|__ALERT_INBOX_USER__|${ALERT_INBOX_USER}|g" -e "s|__REPO_DIR__|${REPO_DIR}|g" \
  "$HERE/alert-inbox.service" > /etc/systemd/system/alert-inbox.service
install -m 644 "$HERE/alert-inbox.timer" /etc/systemd/system/alert-inbox.timer
systemctl daemon-reload
systemctl enable --now alert-inbox.timer
