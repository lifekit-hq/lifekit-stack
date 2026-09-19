#!/usr/bin/env bash
# import-legacy-env.sh - one-time split of the legacy plaintext env into the
# two SOPS boundaries (docs/secrets.md). Run as the captain (the account that
# holds the captain age key), with sudo available to read the legacy file:
#
#   scripts/secrets/import-legacy-env.sh [/srv/openclaw/config/.env]
#
# Writes secrets/lifekit.env.sops (master) and secrets/lifekit-gateway.env.sops
# (gateway) with the recipients from .sops.yaml, so the gateway recipient must
# already be there (init-gateway-key.sh prints it). No value is ever printed:
# the legacy file goes through one Python pass straight into two sops
# encrypt processes. Only KEY NAMES are echoed, so the run can be compared
# with the inventory. Refuses to overwrite an existing .sops file.
#
# Boundary rules applied (the same ones docs/secrets.md states):
#   gateway  - the bot tokens only the OpenClaw gateway uses; TELEGRAM_BOT_TOKEN
#              (the default account, Kit) is renamed KIT_BOT_TOKEN so every
#              name is <CONSUMER>_<PURPOSE>.
#   parked   - BINANCE_API_KEY / BINANCE_API_SECRET have no consumer since
#              2026-07-11 and are kept for the captain to rotate later:
#              renamed PARKED_*, master file, never rendered.
#   dropped  - LIFEKIT_DASHBOARD_TOKEN (reserved, never used).
#   master   - everything else, secrets and settings alike.

set -euo pipefail

cd "$(dirname "$0")/../.."

LEGACY="${1:-/srv/openclaw/config/.env}"
MASTER=secrets/lifekit.env.sops
GATEWAY=secrets/lifekit-gateway.env.sops

command -v sops >/dev/null 2>&1 || { echo "import-legacy-env: sops not on PATH" >&2; exit 1; }
for f in "${MASTER}" "${GATEWAY}"; do
  if [[ -s "${f}" ]]; then
    echo "import-legacy-env: ${f} already exists; edit it with scripts/secrets/edit.sh instead." >&2
    exit 1
  fi
done
if ! grep -q 'secrets/lifekit-gateway' .sops.yaml || [[ "$(grep -c '^ *age1' .sops.yaml)" -lt 1 ]]; then
  echo "import-legacy-env: .sops.yaml has no gateway rule with recipients" >&2
  exit 1
fi

read_legacy() {
  if [[ -r "${LEGACY}" ]]; then cat "${LEGACY}"; else sudo cat "${LEGACY}"; fi
}

split() {  # stdin: legacy dotenv; $1: master|gateway; stdout: that boundary's dotenv
  python3 - "$1" <<'PY'
import re, sys
which = sys.argv[1]
GATEWAY = {"TELEGRAM_BOT_TOKEN": "KIT_BOT_TOKEN", "FABLE_BOT_TOKEN": "FABLE_BOT_TOKEN",
           "FINANCE_BOT_TOKEN": "FINANCE_BOT_TOKEN", "LEARNING_BOT_TOKEN": "LEARNING_BOT_TOKEN",
           "SOCIAL_BOT_TOKEN": "SOCIAL_BOT_TOKEN"}
PARKED = {"BINANCE_API_KEY": "PARKED_BINANCE_API_KEY", "BINANCE_API_SECRET": "PARKED_BINANCE_API_SECRET"}
DROPPED = {"LIFEKIT_DASHBOARD_TOKEN"}
line_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
out, names = [], []
for raw in sys.stdin.read().splitlines():
    m = line_re.match(raw)
    if not m:
        continue  # comments and blank lines are not carried over
    key, value = m.group(1), m.group(2)
    if key in DROPPED:
        continue
    if key in GATEWAY:
        if which == "gateway":
            out.append(f"{GATEWAY[key]}={value}"); names.append(GATEWAY[key])
        continue
    if which == "master":
        key = PARKED.get(key, key)
        out.append(f"{key}={value}"); names.append(key)
sys.stdout.write("\n".join(out) + "\n")
sys.stderr.write(f"{which}: " + " ".join(names) + "\n")
PY
}

encrypt() {  # $1: boundary, $2: output path (the name selects the .sops.yaml rule)
  local target="$2"
  sops --encrypt --input-type dotenv --output-type dotenv --filename-override "${target}" \
    --output "${target}.tmp" /dev/stdin
  mv "${target}.tmp" "${target}"
}

echo "import-legacy-env: reading ${LEGACY} (names only are printed)" >&2
read_legacy | split master  | encrypt master  "${MASTER}"
read_legacy | split gateway | encrypt gateway "${GATEWAY}"
echo "import-legacy-env: wrote ${MASTER} and ${GATEWAY}. Next: pytest scripts/tests/test_secrets.py, commit."
