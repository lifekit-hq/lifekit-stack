#!/usr/bin/env bash
# rehearse-fleet-gate.sh - play the fleet-gate plugin against a scratch table,
# a scratch state dir and a recording ssh, and print what an allowed (A), a
# logged (L) and a stop-and-file (S) call each came to. Touches no container, no
# production state and no relay key: the plugin is the repo's own file loaded in
# node, so it shows what the next deploy will install (docs/runbook.md, "Fleet gate").
#
#   scripts/rehearse-fleet-gate.sh [<p16-openclaw.json>]
#
# With a table argument the three calls are the first A, L and S row of it (a
# prefix row's `*` becomes `x`); without one a built-in sample table is used.
# Exit 0 only when A was untouched, L was logged with its outcome, and S was
# blocked with a request filed.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
plugin="${repo_root}/compose/openclaw-gateway/plugins/fleet-gate/index.js"
command -v node >/dev/null || { echo "rehearse-fleet-gate: node is required" >&2; exit 2; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/state/logs"

if [ "$#" -ge 1 ]; then
  cp -- "$1" "$work/table.json"
else
  cat >"$work/table.json" <<'JSON'
{"schema": "p16-openclaw.v1", "tools": {
  "read": {"class": "A", "id": "openclaw.own-workspace"},
  "write": {"class": "L", "id": "openclaw.own-workspace", "ruling": "agent-behavior.md#P16.1"},
  "ask_user": {"class": "S", "id": "openclaw.ask-user", "ruling": "agent-behavior.md#P16.2"}
}}
JSON
fi

# The recording ssh: what the plugin would send the relay, nothing leaves the box.
cat >"$work/ssh" <<'SH'
#!/bin/sh
printf '%s\n' "$@" >"$SSH_TEST_DIR/argv"
cat >"$SSH_TEST_DIR/stdin"
SH
chmod +x "$work/ssh"

# shellcheck disable=SC2016  # the JS is one single-quoted argument on purpose
FLEET_GATE_TABLE="$work/table.json" \
OPENCLAW_STATE_DIR="$work/state" \
FLEET_GATE_SSH="$work/ssh" \
FLEET_GATE_RELAY_CONF="/rehearsal/ssh_config" \
SSH_TEST_DIR="$work/ssh.d" \
REHEARSAL_WORK="$work" \
  node --input-type=module -e '
import fs from "node:fs";
const [plugin] = process.argv.slice(1);
fs.mkdirSync(process.env.SSH_TEST_DIR, { recursive: true });
const table = JSON.parse(fs.readFileSync(process.env.FLEET_GATE_TABLE, "utf8")).tools;
const pick = (cls) => {
  const key = Object.keys(table).find((k) => table[k].class === cls && !table[k].when);
  if (!key) { console.error(`rehearsal: the table has no unconditional class ${cls} row`); process.exit(2); }
  return key.replace(/\*$/, "x");
};
const hooks = {};
(await import(plugin)).default.register({ on: (n, f) => { hooks[n] = f; }, logger: { warn: console.error } });
const ctx = { agentId: "rehearsal", sessionKey: "agent:rehearsal:cron:1", runId: "r1", requester: { senderIsOwner: false } };
const call = (tool, id) => hooks.before_tool_call({ toolName: tool, params: { note: "rehearsal" }, runId: "r1", toolCallId: id }, ctx);
const a = await call(pick("A"), "a1");
const l = await call(pick("L"), "l1");
await hooks.after_tool_call({ toolName: pick("L"), params: {}, toolCallId: "l1", result: "ok", durationMs: 1 }, ctx);
const s = await call(pick("S"), "s1");
const log = fs.readFileSync(`${process.env.OPENCLAW_STATE_DIR}/logs/fleet-gate.jsonl`, "utf8").trim().split("\n").map(JSON.parse);
const filed = fs.existsSync(`${process.env.SSH_TEST_DIR}/argv`);
console.log(`A ${pick("A")}: ${a === undefined ? "untouched" : "UNEXPECTED " + JSON.stringify(a)}`);
console.log(`L ${pick("L")}: ${l === undefined ? "allowed" : "UNEXPECTED " + JSON.stringify(l)}; log: ${log.filter((x) => x.class === "L").map((x) => x.event + (x.outcome ? `=${x.outcome}` : "")).join(", ")}`);
console.log(`S ${pick("S")}: ${s?.block ? "BLOCKED" : "UNEXPECTED allowed"}; ${s?.blockReason ?? ""}`);
console.log(`filed: ${filed ? fs.readFileSync(`${process.env.SSH_TEST_DIR}/argv`, "utf8").trim().split("\n").slice(-3).join(" ") : "nothing"}`);
const ok = a === undefined && l === undefined && s?.block === true && filed &&
  log.some((x) => x.event === "taken") && log.some((x) => x.event === "outcome") && log.some((x) => x.event === "stopped" && x.filed);
console.log(ok ? "rehearsal: ok" : "rehearsal: FAILED");
process.exit(ok ? 0 : 1);
' "$(printf 'file://%s' "$plugin")"
