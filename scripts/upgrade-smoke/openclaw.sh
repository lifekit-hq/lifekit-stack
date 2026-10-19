#!/usr/bin/env bash
# Post-upgrade smoke check for the OpenClaw pillar: is the running gateway
# healthy on the version it was upgraded to? Read-only against production: it
# only runs status and doctor commands inside the running gateway container
# (`docker exec`), never `--fix`, and never restarts, recreates or reconfigures
# anything. No model call.
#
# Output: one summary line on stdout, `upgrade-smoke openclaw: PASS|FAIL ...`,
# carrying versions, counts and check names only - never a config value.
# Exit 0 on pass, 1 on fail, 2 on a usage error.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: openclaw.sh [--expect-version <version>]

  --expect-version <v>  Fail unless the gateway reports this core version
                        (e.g. 2026.9.8): proves the upgrade is the one running.
  -h, --help            Show this help.

Environment: OPENCLAW_GATEWAY (container name; default: found by its compose
labels), OPENCLAW_HEALTHZ (a URL to curl from the host instead of probing
/healthz inside the gateway container; the host port exists only in token
mode), SMOKE_TIMEOUT (seconds per command, default 180).
USAGE
}

EXPECT=""
while (($#)); do
  case "$1" in
    --expect-version) EXPECT="${2:?--expect-version needs a value}"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

HEALTHZ="${OPENCLAW_HEALTHZ:-}"
TIMEOUT="${SMOKE_TIMEOUT:-180}"
START="$(date +%s)"
RED=()
VERSION="unknown" CHANNELS="-" MCP="-" DOCTOR_ERRORS="-"

summary() {
  local verdict="PASS"
  ((${#RED[@]} == 0)) || verdict="FAIL"
  local line="upgrade-smoke openclaw: ${verdict} version=${VERSION} channels=${CHANNELS} mcp=${MCP} doctor-errors=${DOCTOR_ERRORS} elapsed=$(($(date +%s) - START))s"
  ((${#RED[@]} == 0)) || line+=" red=$(IFS=,; echo "${RED[*]}")"
  echo "${line}"
  ((${#RED[@]} == 0))
}

gw() { timeout "${TIMEOUT}" docker exec "${GW}" openclaw "$@" </dev/null 2>/dev/null; }

# json_check <python body> reads stdin as JSON into `d` and prints the body's
# result; an unparseable document prints PARSE_ERROR.
json_check() {
  python3 -c '
import json, sys
try:
    raw = sys.stdin.read()
    d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    if not isinstance(d, dict):
        raise ValueError
except Exception:
    print("PARSE_ERROR"); sys.exit()
'"$1"
}

# ---------------------------------------------------------------- container
GW="${OPENCLAW_GATEWAY:-}"
if [[ -z "${GW}" ]]; then
  ids="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
    --filter label=com.docker.compose.service=openclaw-gateway \
    --filter label=com.docker.compose.oneoff=False 2>/dev/null || true)"
  if [[ "$(grep -c . <<<"${ids}")" != 1 ]]; then
    RED+=("gateway-not-found")
    summary || exit 1
  fi
  GW="${ids}"
fi
state="$(docker inspect --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${GW}" 2>/dev/null || true)"
if [[ "${state}" != "true healthy" ]]; then
  [[ -n "${state}" ]] || state="missing"
  RED+=("container-${state// /-}")
  [[ "${state}" == true* ]] || { summary || exit 1; }
fi

# ---------------------------------------------------------------- version
VERSION="$(gw --version | awk '{print $2}' || true)"
[[ -n "${VERSION}" ]] || { VERSION="unknown"; RED+=("version-unreadable"); }
if [[ -n "${EXPECT}" && "${VERSION}" != "${EXPECT}" ]]; then
  RED+=("version-not-${EXPECT}")
fi

# ---------------------------------------------------------------- /healthz
# The same endpoint the container healthcheck and deploy.sh probe, from inside
# the container: the host port is gone once the gateway sits behind the edge.
if [[ -n "${HEALTHZ}" ]]; then
  hz="$(curl -fsS -m 10 "${HEALTHZ}" 2>/dev/null || true)"
else
  hz="$(timeout "${TIMEOUT}" docker exec "${GW}" node -e \
    "fetch('http://127.0.0.1:18789/healthz',{signal:AbortSignal.timeout(5000)}).then((r)=>r.ok?r.text():Promise.reject()).then((t)=>console.log(t)).catch(()=>process.exit(1))" \
    </dev/null 2>/dev/null || true)"
fi
[[ "$(json_check 'print("ok" if d.get("ok") is True else "bad")' <<<"${hz}")" == ok ]] || RED+=("healthz")

# ---------------------------------------------------------------- health
# Gateway-side runtime state: plugins loaded without errors, the model and
# child runtimes usable, and every enabled channel account connected.
out="$(gw health --json | json_check '
bad = []
if d.get("ok") is not True: bad.append("health-not-ok")
p = d.get("plugins") or {}
if p.get("errors"): bad.append("plugin-errors")
if p.get("unavailable"): bad.append("plugins-unavailable")
if (d.get("modelRuntime") or {}).get("degraded"): bad.append("model-runtime-degraded")
if (d.get("childRuntime") or {}).get("available") is False: bad.append("child-runtime-unavailable")
up = total = 0
for ch in (d.get("channels") or {}).values():
    accounts = (ch or {}).get("accounts") or {}
    for a in accounts.values():
        if not a.get("enabled"): continue
        total += 1
        if a.get("running") and a.get("connected"): up += 1
if up < total: bad.append("channels-down")
print(f"{up}/{total}", ",".join(bad) or "-")
' || true)"
if [[ "${out}" == PARSE_ERROR || -z "${out}" ]]; then
  RED+=("health-unreadable")
else
  CHANNELS="${out%% *}"
  [[ "${out##* }" == "-" ]] || RED+=("${out##* }")
fi

# ---------------------------------------------------------------- plugin compat
# `doctor --post-upgrade` is OpenClaw's own post-upgrade probe set (plugin
# index, entry, manifest and version drift); any finding is red.
out="$(gw doctor --post-upgrade --json | json_check 'print(len(d.get("findings") or []) if "findings" in d else "PARSE_ERROR")' || true)"
[[ "${out}" == 0 ]] || RED+=("post-upgrade-findings")

# ---------------------------------------------------------------- MCP
# Every configured MCP server must connect: an upgrade that breaks the MCP
# client breaks all of them. The probe exits non-zero naming the first failure.
rc=0
mcp="$(gw mcp probe --json)" || rc=$?
out="$(json_check '
if d.get("ok") is False: print("PARSE_ERROR"); sys.exit()
s = d.get("servers") or {}
n = sum(1 for v in s.values() if isinstance(v, dict) and isinstance(v.get("tools"), int))
print(f"{n}/{len(s)}")
' <<<"${mcp}" || true)"
if ((rc != 0)) || [[ "${out}" == PARSE_ERROR || -z "${out}" ]]; then
  RED+=("mcp-probe")
else
  MCP="${out}"
  [[ "${MCP%/*}" == "${MCP#*/}" ]] || RED+=("mcp-probe")
fi

# ---------------------------------------------------------------- doctor lint
# Bare `doctor --json` is OpenClaw's advisory read-only health check set. The
# live install carries standing warnings (the by-design `lan` bind among them),
# so only error-class findings are red; their check ids name them.
out="$(gw doctor --json | json_check '
import re
if not isinstance(d.get("findings"), list): print("PARSE_ERROR"); sys.exit()
bad = sorted({re.sub(r"[^A-Za-z0-9_./-]", "?", str(f.get("checkId") or "unknown"))[:80]
              for f in d["findings"] if isinstance(f, dict)
              and str(f.get("severity") or "").lower() in ("error", "critical", "high", "fatal")})
print(len(bad), ",".join(bad) or "-")
' || true)"
if [[ "${out}" == PARSE_ERROR || -z "${out}" ]]; then
  RED+=("doctor-unreadable")
else
  DOCTOR_ERRORS="${out%% *}"
  [[ "${DOCTOR_ERRORS}" == 0 ]] || RED+=("doctor:${out##* }")
fi

summary || exit 1
