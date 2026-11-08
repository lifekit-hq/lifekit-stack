#!/usr/bin/env bash
# ensure-fleet-gate.sh - install the fleet-gate gateway plugin
# (compose/openclaw-gateway/plugins/fleet-gate) and its P16 table into the
# gateway's global extensions directory, and reload the plugin in the running
# gateway when its code changed.
#
# The plugin gates every agent tool call in `before_tool_call` and stops, then
# files for the owner, the ones the P16 table marks stop-and-file
# (docs/runbook.md, "Fleet gate"). platform.patch.json allows and enables it;
# this script only carries its files.
#
# The table is not in this repo: fleet-policy renders it from the P16 rows
# (p16-openclaw.json, schema p16-openclaw.v1) and the deploy copies it in, so
# there is no second copy of the rules. Where to find it:
#   P16_OPENCLAW_TABLE      default /var/lib/lifekit/fleet-policy/p16-openclaw.json
# A table that is absent is not an error (the plugin then gates nothing and
# logs `table-missing`, so the first deploy does not wait on the renderer);
# one that is not valid fails this script and leaves the installed copy alone.
# The plugin re-reads the table when its mtime changes: a new table needs no
# reload.
#
# The deploy runs it (deploy-openclaw.sh, configure phase, before the platform
# patch, so the restart that patch asks for on first enable finds the files).
# By hand, on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-fleet-gate.sh
#
# The gateway loads a plugin only from files owned by its own uid or root and
# not group- or world-writable, so the files are installed 0644 by the deploy
# account (uid 1000, the gateway's node user). Re-running converges.
#
# Env overrides:
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
set -euo pipefail

PLUGIN=fleet-gate
TABLE_NAME=p16-openclaw.json
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
config_dir="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
table_src="${P16_OPENCLAW_TABLE:-/var/lib/lifekit/fleet-policy/${TABLE_NAME}}"
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

if [ -f "$table_src" ]; then
  python3 - "$table_src" <<'PY' || { echo "fleet gate table $table_src is not a valid p16-openclaw.v1 table; installed copy left alone" >&2; exit 1; }
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc.get("schema") == "p16-openclaw.v1", "schema"
tools = doc.get("tools")
assert isinstance(tools, dict) and tools, "tools"
assert all(isinstance(r, dict) and r.get("class") in ("A", "L", "S") for r in tools.values()), "class"
PY
  if ! cmp -s "$table_src" "$dest/$TABLE_NAME"; then
    install -D -m 0644 "$table_src" "$dest/$TABLE_NAME"
    echo "fleet gate table installed -> $dest/$TABLE_NAME"
  else
    echo "fleet gate table unchanged -> $dest/$TABLE_NAME"
  fi
elif [ -f "$dest/$TABLE_NAME" ]; then
  echo "no table at $table_src; keeping the installed one"
else
  echo "no P16 table at $table_src yet: the gate logs table-missing and stops nothing until fleet-policy renders one" >&2
fi

if [ "$changed" -eq 0 ]; then
  echo "fleet gate unchanged -> $dest"
  exit 0
fi
echo "fleet gate installed -> $dest"

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
