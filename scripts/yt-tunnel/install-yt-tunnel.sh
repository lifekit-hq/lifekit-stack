#!/usr/bin/env bash
# install-yt-tunnel.sh - install and start the two systemd USER units behind
# skills/youtube-transcript, which fetches YouTube captions from a home IP:
#   yt-tunnel.service  ssh -D SOCKS forward to the owner's PC, loopback only
#   yt-relay.service   allow-list SOCKS5 relay (yt-relay.py) on the docker
#                      bridge address; the only thing the skill talks to
#
# Run as the admin account that owns the ssh alias (ADMIN_USER, README "VPS
# users"), on the VPS host, from the repo checkout. User scope only: no sudo, nothing under /etc. Idempotent: a
# unit is rewritten only when its rendered text changed, and restarted then.
#
# Without lingering (`loginctl show-user $USER -p Linger`) the user manager
# stops with the account's last session and the units with it; this script
# only reports that state. Enabling it is an operator step:
#   sudo loginctl enable-linger <admin account>
#
# Modes:
#   (default)  render, install, daemon-reload, enable --now
#   --print    write both rendered units to stdout and change nothing
#
# Env overrides:
#   YT_TUNNEL_HOST        ssh config alias of the PC                     (required)
#   YT_TUNNEL_PORT        ssh SOCKS forward port                         (default: 18082)
#   YT_TUNNEL_BIND        ssh SOCKS forward address, 127.x only          (default: 127.0.0.1)
#   YT_TUNNEL_RELAY_PORT  relay port, what the skill's proxy names       (default: 18081)
#   YT_TUNNEL_RELAY_BIND  relay address (default: the docker0 address, the one
#                         host.docker.internal resolves to inside the gateway)
#   YT_TUNNEL_UNIT_DIR    where the units go (default: ~/.config/systemd/user)
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
tdir="${repo_root}/scripts/yt-tunnel"

host="${YT_TUNNEL_HOST:-}"
[ -n "$host" ] || { echo "set YT_TUNNEL_HOST to the ssh config alias of the PC" >&2; exit 1; }
tport="${YT_TUNNEL_PORT:-18082}"
tbind="${YT_TUNNEL_BIND:-127.0.0.1}"
rport="${YT_TUNNEL_RELAY_PORT:-18081}"
rbind="${YT_TUNNEL_RELAY_BIND:-}"
unit_dir="${YT_TUNNEL_UNIT_DIR:-${HOME}/.config/systemd/user}"

case "$tbind" in
  127.*.*.*) ;;
  *) echo "refusing tunnel bind $tbind: the ssh forward is an open proxy into the PC's network, loopback only" >&2; exit 1 ;;
esac
if [ -z "$rbind" ]; then
  rbind="$(ip -4 -o addr show docker0 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}')"
fi
[ -n "$rbind" ] || { echo "no docker0 address; set YT_TUNNEL_RELAY_BIND" >&2; exit 1; }
case "$rbind" in
  0.0.0.0|::|'*') echo "refusing to bind $rbind: the relay is for the docker bridge only" >&2; exit 1 ;;
esac
for p in "$tport" "$rport"; do
  case "$p" in ''|*[!0-9]*) echo "ports must be numbers" >&2; exit 1 ;; esac
done
[ "$tport" != "$rport" ] || { echo "tunnel and relay ports must differ" >&2; exit 1; }

render() {
  sed -e "s|@REPO@|${repo_root}|g" \
    -e "s|@TUNNEL_BIND@|${tbind}|g" -e "s|@TUNNEL_PORT@|${tport}|g" \
    -e "s|@RELAY_BIND@|${rbind}|g" -e "s|@RELAY_PORT@|${rport}|g" \
    -e "s|@HOST@|${host}|g" "${tdir}/$1.service"
}

units="yt-tunnel yt-relay"

if [ "${1:-}" = "--print" ]; then
  for u in $units; do
    printf '# ==> %s.service <==\n%s\n' "$u" "$(render "$u")"
  done
  exit 0
fi

mkdir -p "$unit_dir"
changed=""
for u in $units; do
  rendered="$(render "$u")"
  dest="${unit_dir}/${u}.service"
  if [ -f "$dest" ] && [ "$(cat "$dest")" = "$rendered" ]; then
    echo "unit unchanged -> $dest"
  else
    printf '%s\n' "$rendered" > "$dest"
    echo "unit installed -> $dest"
    changed="$changed $u.service"
  fi
done

systemctl --user daemon-reload
systemctl --user enable --now yt-tunnel.service yt-relay.service
if [ -n "$changed" ]; then
  # shellcheck disable=SC2086
  systemctl --user restart $changed
fi

echo "tunnel: ${tbind}:${tport} (via ssh alias ${host})"
echo "relay: ${rbind}:${rport}"
echo "linger: $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo unknown)"
