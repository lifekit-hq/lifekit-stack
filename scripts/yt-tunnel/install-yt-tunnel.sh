#!/usr/bin/env bash
# install-yt-tunnel.sh - install and start the yt-tunnel systemd USER unit
# (scripts/yt-tunnel/yt-tunnel.service): an SSH SOCKS forward to the owner's PC
# that skills/youtube-transcript uses to fetch YouTube captions from a home IP.
#
# Run as the admin account that owns the ssh alias (ADMIN_USER, README "VPS
# users"), on the VPS host, from the repo checkout. User scope only: no sudo, nothing under /etc. Idempotent: the
# unit is rewritten only when its rendered text changed, and restarted then.
#
# Without lingering (`loginctl show-user $USER -p Linger`) the user manager
# stops with the account's last session and the tunnel with it; this script
# only reports that state. Enabling it is an operator step:
#   sudo loginctl enable-linger <admin account>
#
# Modes:
#   (default)  render, install, daemon-reload, enable --now
#   --print    write the rendered unit to stdout and change nothing
#
# Env overrides:
#   YT_TUNNEL_HOST   ssh config alias of the PC          (required)
#   YT_TUNNEL_PORT   SOCKS listener port                 (default: 18081)
#   YT_TUNNEL_BIND   listener address (default: the docker0 address, the one
#                    host.docker.internal resolves to inside the gateway)
#   YT_TUNNEL_UNIT_DIR   where the unit goes (default: ~/.config/systemd/user)
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
template="${repo_root}/scripts/yt-tunnel/yt-tunnel.service"

host="${YT_TUNNEL_HOST:-}"
[ -n "$host" ] || { echo "set YT_TUNNEL_HOST to the ssh config alias of the PC" >&2; exit 1; }
port="${YT_TUNNEL_PORT:-18081}"
bind="${YT_TUNNEL_BIND:-}"
unit_dir="${YT_TUNNEL_UNIT_DIR:-${HOME}/.config/systemd/user}"

if [ -z "$bind" ]; then
  bind="$(ip -4 -o addr show docker0 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}')"
fi
[ -n "$bind" ] || { echo "no docker0 address; set YT_TUNNEL_BIND" >&2; exit 1; }
case "$bind" in
  0.0.0.0|::|'*') echo "refusing to bind $bind: the tunnel is for the docker bridge only" >&2; exit 1 ;;
esac
case "$port" in ''|*[!0-9]*) echo "YT_TUNNEL_PORT must be a number" >&2; exit 1 ;; esac

rendered="$(sed -e "s|@BIND@|${bind}|g" -e "s|@PORT@|${port}|g" -e "s|@HOST@|${host}|g" "$template")"

if [ "${1:-}" = "--print" ]; then
  printf '%s\n' "$rendered"
  exit 0
fi

dest="${unit_dir}/yt-tunnel.service"
mkdir -p "$unit_dir"
if [ -f "$dest" ] && [ "$(cat "$dest")" = "$rendered" ]; then
  echo "unit unchanged -> $dest"
  changed=0
else
  printf '%s\n' "$rendered" > "$dest"
  echo "unit installed -> $dest"
  changed=1
fi

systemctl --user daemon-reload
systemctl --user enable --now yt-tunnel.service
if [ "$changed" = 1 ]; then
  systemctl --user restart yt-tunnel.service
fi

echo "listener: ${bind}:${port} (via ssh alias ${host})"
echo "linger: $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo unknown)"
