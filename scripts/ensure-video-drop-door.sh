#!/usr/bin/env bash
# ensure-video-drop-door.sh - install the video-drop-door gateway plugin
# (compose/openclaw-gateway/plugins/video-drop-door) into the gateway's global
# extensions directory, and reload it in the running gateway when it changed.
#
# The plugin answers a bare YouTube or Instagram link, or a video file, sent
# to kit on Telegram with the video-eval skill's queue receipt, before kit's
# model sees it (docs/runbook.md, "Video drop door"). platform.patch.json
# allows and enables it; this script only carries its files.
#
# The deploy runs it (deploy-openclaw.sh, configure phase, before the platform
# patch, so the restart that patch asks for on first enable finds the files).
# By hand, on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-video-drop-door.sh
#
# The gateway loads a plugin only from files owned by its own uid or root and
# not group- or world-writable, so the files are installed 0644 by the deploy
# account (uid 1000, the gateway's node user). Re-running converges.
#
# Env overrides:
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
set -euo pipefail

PLUGIN=video-drop-door
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
config_dir="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
src="${repo_root}/compose/openclaw-gateway/plugins/${PLUGIN}"
dest="${config_dir}/extensions/${PLUGIN}"

changed=0
for f in index.js openclaw.plugin.json package.json; do
  if ! cmp -s "$src/$f" "$dest/$f"; then
    install -D -m 0644 "$src/$f" "$dest/$f"
    changed=1
  fi
done
chmod 0755 "$dest"
if [ "$changed" -eq 0 ]; then
  echo "video drop door unchanged -> $dest"
  exit 0
fi
echo "video drop door installed -> $dest"

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; it loads the plugin when it starts"
  exit 0
fi
# Before platform.patch.json first allows the plugin there is nothing to
# reload: the restart that patch asks for loads it.
allowed="$(python3 - "${config_dir}/openclaw.json" "$PLUGIN" <<'PY' 2>/dev/null || echo unknown
import json, sys
plugins = json.load(open(sys.argv[1])).get("plugins", {})
on = sys.argv[2] in plugins.get("allow", []) and plugins.get("entries", {}).get(sys.argv[2], {}).get("enabled")
print("yes" if on else "no")
PY
)"
case "$allowed" in
  yes)
    docker exec "$GATEWAY" openclaw plugins reload "$PLUGIN"
    echo "reloaded $PLUGIN in the running gateway"
    ;;
  no) echo "$PLUGIN is not enabled in the live config yet; the platform patch enables it" ;;
  *)
    echo "could not read the live config; reloading $PLUGIN anyway"
    docker exec "$GATEWAY" openclaw plugins reload "$PLUGIN"
    ;;
esac
