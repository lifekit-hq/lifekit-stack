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
# The plugin refuses to start without its table, so the table comes first and
# the plugin code only after it: with no source table and no installed one this
# script exits non-zero before touching the plugin (the plugin already running
# stays as it is, a merge before the host copy leaves the live gate alone); a
# source that is not valid fails it the same way and leaves the installed copy
# alone. The plugin re-reads the table when its mtime changes: a new table
# needs no reload.
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
# The production gateway container, never a second labelled one (issue 716).
# shellcheck source=scripts/lib/openclaw-gateway.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/openclaw-gateway.sh"

PLUGIN=fleet-gate
TABLE_NAME=p16-openclaw.json
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
config_dir="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
table_src="${P16_OPENCLAW_TABLE:-/var/lib/lifekit/fleet-policy/${TABLE_NAME}}"
src="${repo_root}/compose/openclaw-gateway/plugins/${PLUGIN}"
dest="${config_dir}/extensions/${PLUGIN}"

if [ -f "$table_src" ]; then
  python3 - "$table_src" <<'PY' || { echo "fleet gate table $table_src is not a valid p16-openclaw.v1 table; installed copy and plugin left alone" >&2; exit 1; }
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc.get("schema") == "p16-openclaw.v1", "schema"
tools = doc.get("tools")
assert isinstance(tools, dict) and tools, "tools"
assert all(isinstance(r, dict) and r.get("class") in ("A", "L", "S") for r in tools.values()), "class"
PY
  table_msg="$(if cmp -s "$table_src" "$dest/$TABLE_NAME"; then echo "fleet gate table unchanged -> $dest/$TABLE_NAME"; else
    install -D -m 0644 "$table_src" "$dest/$TABLE_NAME"
    echo "fleet gate table installed -> $dest/$TABLE_NAME"; fi)"
elif [ -f "$dest/$TABLE_NAME" ]; then
  table_msg="no table at $table_src; keeping the installed one"
else
  echo "no P16 table at $table_src and none installed: not installing the fleet-gate plugin (it refuses to start without its table; any plugin already installed keeps running). Run fleet-policy install and copy p16-openclaw.json to $table_src, then re-run." >&2
  exit 1
fi
echo "$table_msg"

changed=0
for f in index.js openclaw.plugin.json package.json; do
  if ! cmp -s "$src/$f" "$dest/$f"; then
    install -D -m 0644 "$src/$f" "$dest/$f"
    changed=1
  fi
done
chmod 0755 "$dest"

reload_gateway() {
  GATEWAY="$(openclaw_gateway_id_lenient)" || exit 1
  if [ -z "$GATEWAY" ]; then
    echo "no running openclaw-gateway container; it loads the plugin when it starts"
    return 0
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
}

if [ "$changed" -eq 0 ]; then
  echo "fleet gate unchanged -> $dest"
else
  echo "fleet gate installed -> $dest"
  reload_gateway
fi

# Record the installed commit (issue 689; the release gauge reads it).
bash "$(dirname "${BASH_SOURCE[0]}")/lib/install-stamp.sh" fleet-gate
