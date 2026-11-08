// fleet-gate - the OpenClaw side of the P16 permission engine.
//
// Every agent tool call passes through `before_tool_call` (the claude-cli
// runtime included). This plugin looks the call up in a table the fleet-policy
// tool renders from the P16 rows (p16-openclaw.json, copied in at deploy by
// scripts/ensure-fleet-gate.sh) and answers by the row's class:
//
//   A  allowed       nothing returned, nothing written.
//   L  allow-and-log nothing returned; one JSON line to the host-readable log
//                    now, and one for the outcome from `after_tool_call`.
//   S  stop-and-file the call is blocked and filed for the owner through the
//                    kit-relay forced command (request id oc-<day>-<hash>), so
//                    the agent carries on with other work and does not ask him
//                    in chat.
//
// A tool the table does not list is allowed and logged (P16: reversible and
// inside the fleet's space is taken, not asked). A table that is missing or
// unreadable blocks nothing and logs the reason (rate-limited). The host fails
// this hook closed after 15 s; the filing budget below stays well inside it and
// the timeout is left at the host default (docs/runbook.md, "Fleet gate").
//
// No network call and no model: the table is a local file, the filing one
// execFile of ssh with no shell and the body on stdin.
//
// Table (schema p16-openclaw.v1):
//   { "schema": "p16-openclaw.v1",
//     "tools": { "<tool name>" | "<prefix>*": {
//         "class": "A" | "L" | "S",
//         "id": "<resource.action>",     // shown in the stop and the log
//         "ruling": "<ruling pointer>",   // optional
//         "when": "non-owner" | "non-owner-to-main"   // optional
//     } } }
// An exact name beats a prefix, the longest prefix beats a shorter one, and a
// row whose `when` does not hold for this call is skipped. `when` is judged on
// the host-derived requester: a run whose requester.senderIsOwner is not true
// (or is missing) is a non-owner run, and sessions_send to the main agent from
// one is an ask. An unknown `when` counts as holding, so a row meant to stop
// something is never silently relaxed.

import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const MAIN_AGENT = "kit"; // the agent sessions_send reaches as "the main agent"
const SCHEMA = "p16-openclaw.v1";
const CLASSES = new Set(["A", "L", "S"]);
const FILE_MS = 9_000; // ssh gets this long; the host cuts the hook at 15 s
const LOG_MAX_BYTES = 8 * 1024 * 1024; // one older generation is kept
const PENDING_MS = 600_000; // how long an L call waits for its outcome line
const NOISE_MS = 60_000; // one line per table problem per minute
const PARAM_CHARS = 1500;
const SECRET_KEY = /token|secret|passw|authorization|api[_-]?key|cookie|credential/i;

const HERE = path.dirname(fileURLToPath(import.meta.url));

// Read at call time so a deploy-time table swap and a test's env both apply.
function settings() {
  const state = process.env.OPENCLAW_STATE_DIR || "/home/node/.openclaw";
  return {
    table: process.env.FLEET_GATE_TABLE || path.join(HERE, "p16-openclaw.json"),
    log: process.env.FLEET_GATE_LOG || path.join(state, "logs", "fleet-gate.jsonl"),
    relayConf: process.env.FLEET_GATE_RELAY_CONF || "/run/lifekit/oc-relay/ssh_config",
    ssh: process.env.FLEET_GATE_SSH || "ssh",
    // Only ever tightens the filing budget (rehearsals, tests); never past FILE_MS.
    fileMs: Math.min(FILE_MS, Number.parseInt(process.env.FLEET_GATE_FILE_MS || "", 10) || FILE_MS),
  };
}

// ── the table ───────────────────────────────────────────────────────────────

let cache = { path: null, stamp: null, value: null };

function parseTable(text) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    return { problem: "table-malformed", detail: "not JSON" };
  }
  if (!doc || typeof doc !== "object" || doc.schema !== SCHEMA) {
    return { problem: "table-malformed", detail: `schema is not ${SCHEMA}` };
  }
  if (!doc.tools || typeof doc.tools !== "object" || Array.isArray(doc.tools)) {
    return { problem: "table-malformed", detail: "tools is not an object" };
  }
  const exact = new Map();
  const prefixes = [];
  let skipped = 0;
  for (const [key, row] of Object.entries(doc.tools)) {
    const ok =
      key !== "" &&
      row &&
      typeof row === "object" &&
      CLASSES.has(row.class) &&
      (row.id === undefined || typeof row.id === "string") &&
      (row.ruling === undefined || typeof row.ruling === "string") &&
      (row.when === undefined || typeof row.when === "string");
    if (!ok) {
      skipped += 1;
      continue;
    }
    const entry = {
      cls: row.class,
      id: row.id || key.replace(/\*$/, ""),
      ruling: row.ruling || "",
      when: row.when || "",
    };
    if (key.endsWith("*")) prefixes.push({ prefix: key.slice(0, -1), entry });
    else exact.set(key, entry);
  }
  if (exact.size + prefixes.length === 0) {
    return { problem: "table-malformed", detail: `no usable row (${skipped} skipped)` };
  }
  prefixes.sort((a, b) => b.prefix.length - a.prefix.length);
  return { exact, prefixes, skipped };
}

function loadTable(file) {
  let stat;
  try {
    stat = fs.statSync(file);
  } catch {
    cache = { path: null, stamp: null, value: null };
    return { problem: "table-missing", detail: file };
  }
  const stamp = `${stat.mtimeMs}:${stat.size}`;
  if (cache.path === file && cache.stamp === stamp) return cache.value;
  let value;
  try {
    value = parseTable(fs.readFileSync(file, "utf8"));
  } catch (err) {
    value = { problem: "table-missing", detail: `unreadable: ${String(err?.code || err).slice(0, 60)}` };
  }
  cache = { path: file, stamp, value };
  return value;
}

// ── what the call is ────────────────────────────────────────────────────────

function targetsMain(params) {
  const key = typeof params.sessionKey === "string" ? params.sessionKey.trim() : "";
  if (key) return key === "main" || key.startsWith(`agent:${MAIN_AGENT}:`);
  const agent = typeof params.agentId === "string" ? params.agentId.trim() : "";
  if (agent) return agent === MAIN_AGENT;
  return true; // a label alone names no agent: unproven, so counted
}

function holds(when, event, ctx) {
  const ownerRun = ctx?.requester?.senderIsOwner === true;
  switch (when) {
    case "":
      return true;
    case "non-owner":
      return !ownerRun;
    case "non-owner-to-main":
      return !ownerRun && targetsMain(event.params && typeof event.params === "object" ? event.params : {});
    default:
      return true;
  }
}

function lookup(table, toolName, event, ctx) {
  const names = [toolName];
  if (toolName.startsWith("mcp__")) names.push(toolName.slice(5));
  for (const name of names) {
    const hit = table.exact.get(name);
    if (hit && holds(hit.when, event, ctx)) return hit;
  }
  for (const name of names) {
    for (const { prefix, entry } of table.prefixes) {
      if (name.startsWith(prefix) && holds(entry.when, event, ctx)) return entry;
    }
  }
  return null;
}

// ── the log ─────────────────────────────────────────────────────────────────

let logFailedAt = 0;

function writeLog(api, line) {
  const record = { ts: new Date().toISOString(), plugin: "fleet-gate", ...line };
  const file = settings().log;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    try {
      if (fs.statSync(file).size > LOG_MAX_BYTES) fs.renameSync(file, `${file}.1`);
    } catch {
      // no file yet
    }
    fs.appendFileSync(file, `${JSON.stringify(record)}\n`, { mode: 0o640 });
  } catch (err) {
    const now = Date.now();
    if (now - logFailedAt > NOISE_MS) {
      logFailedAt = now;
      api.logger?.warn?.(`fleet-gate: log write failed: ${String(err?.code || err)}`);
    }
  }
}

const noisy = new Map();

function writeProblem(api, reason, detail) {
  const now = Date.now();
  const last = noisy.get(reason);
  if (last && now - last < NOISE_MS) return;
  noisy.set(reason, now);
  writeLog(api, { event: reason, detail, effect: "allowed" });
  api.logger?.warn?.(`fleet-gate: ${reason}: ${detail}; calls are not gated until it is fixed`);
}

// ── the stop, and filing it ─────────────────────────────────────────────────

function redact(value) {
  const seen = JSON.stringify(
    value,
    (key, v) => {
      if (key && SECRET_KEY.test(key)) return "[redacted]";
      if (typeof v === "string" && v.length > 600) return `${v.slice(0, 600)}...`;
      return v;
    },
  );
  const text = seen === undefined ? "{}" : seen;
  return text.length > PARAM_CHARS ? `${text.slice(0, PARAM_CHARS)}...` : text;
}

function requestId(agent, tool, id, params) {
  const day = new Date().toISOString().slice(0, 10).replaceAll("-", "");
  const hash = createHash("sha256").update(`${agent}|${tool}|${id}|${params}`).digest("hex").slice(0, 16);
  return `oc-${day}-${hash}`;
}

function noteBody(rid, row, call) {
  return [
    `P16 stop ${rid}: an OpenClaw agent's tool call needs the owner and was blocked.`,
    `class: ${row.id}`,
    row.ruling ? `ruling: ${row.ruling}` : null,
    `agent: ${call.agent}`,
    `tool: ${call.tool}`,
    `session: ${call.session}`,
    `run: ${call.run}`,
    `at: ${new Date().toISOString()}`,
    `call: ${call.params}`,
    "",
  ]
    .filter((l) => l !== null)
    .join("\n");
}

function file(rid, body) {
  const cfg = settings();
  return new Promise((resolve) => {
    let done = false;
    const finish = (r) => {
      if (!done) {
        done = true;
        resolve(r);
      }
    };
    try {
      const child = execFile(
        cfg.ssh,
        [
          "-F", cfg.relayConf,
          "-o", "BatchMode=yes",
          "-o", "ConnectTimeout=6",
          "relay", "note", rid,
        ],
        { timeout: cfg.fileMs, maxBuffer: 64 * 1024, encoding: "utf8" },
        (err, _stdout, stderr) => {
          if (!err) return finish({ ok: true });
          const said = String(stderr || "").split("\n").find((l) => l.trim()) || "";
          const why = said || (err.killed ? "timed out" : `exit ${err.code ?? "?"}`);
          finish({ ok: false, error: why.trim().slice(0, 160) });
        },
      );
      child.stdin?.on("error", () => {});
      child.stdin?.end(body);
    } catch (err) {
      finish({ ok: false, error: String(err?.code || err).slice(0, 160) });
    }
  });
}

async function stop(api, row, toolName, event, ctx) {
  const params = redact(event.params ?? {});
  const call = {
    agent: ctx?.agentId || "unknown",
    tool: toolName,
    session: ctx?.sessionKey || "unknown",
    run: event.runId || ctx?.runId || "unknown",
    params,
  };
  const rid = requestId(call.agent, toolName, row.id, params);
  const filed = await file(rid, noteBody(rid, row, call));
  writeLog(api, {
    event: "stopped",
    agent: call.agent,
    tool: toolName,
    class: "S",
    id: row.id,
    ruling: row.ruling,
    runId: call.run,
    toolCallId: event.toolCallId,
    sessionKey: call.session,
    request: rid,
    filed: filed.ok,
    ...(filed.ok ? {} : { error: filed.error }),
  });
  const tail = "Carry on with other work; do not ask him in chat.";
  return {
    block: true,
    blockReason: filed.ok
      ? `Stopped under P16 (${row.id}): filed for the owner as ${rid}. ${tail}`
      : `Stopped under P16 (${row.id}): filing it for the owner failed (${filed.error}). ${tail}`,
  };
}

// ── the hooks ───────────────────────────────────────────────────────────────

const pending = new Map(); // toolCallId -> { id, at }

function register(api) {
  api.on("before_tool_call", async (event, ctx) => {
    const toolName = String(event?.toolName ?? "");
    let row;
    try {
      const table = loadTable(settings().table);
      if (table.problem) {
        writeProblem(api, table.problem, table.detail);
        return;
      }
      row = lookup(table, toolName, event, ctx);
      if (!row) {
        writeLog(api, {
          event: "unlisted",
          agent: ctx?.agentId,
          tool: toolName,
          class: "L",
          effect: "allowed",
          runId: event.runId || ctx?.runId,
          toolCallId: event.toolCallId,
        });
        return;
      }
      if (row.cls === "A") return;
      if (row.cls === "L") {
        const now = Date.now();
        for (const [k, v] of pending) if (now - v.at > PENDING_MS) pending.delete(k);
        if (event.toolCallId) pending.set(event.toolCallId, { id: row.id, tool: toolName, at: now });
        writeLog(api, {
          event: "taken",
          agent: ctx?.agentId,
          tool: toolName,
          class: "L",
          id: row.id,
          ruling: row.ruling,
          runId: event.runId || ctx?.runId,
          toolCallId: event.toolCallId,
          sessionKey: ctx?.sessionKey,
        });
        return;
      }
    } catch (err) {
      // A bug here must not block every tool call of every agent: allow, say so.
      writeProblem(api, "gate-error", String(err?.message || err).slice(0, 160));
      return;
    }
    return stop(api, row, toolName, event, ctx);
  });

  api.on("after_tool_call", (event, ctx) => {
    const mine = event?.toolCallId ? pending.get(event.toolCallId) : undefined;
    if (!mine) return;
    pending.delete(event.toolCallId);
    // Neither result nor error is not proof of success: record it as unknown.
    const outcome = event.error ? "error" : event.result !== undefined ? "returned" : "unknown";
    writeLog(api, {
      event: "outcome",
      agent: ctx?.agentId,
      tool: mine.tool,
      class: "L",
      id: mine.id,
      toolCallId: event.toolCallId,
      outcome,
      ...(event.error ? { error: String(event.error).slice(0, 200) } : {}),
      ...(typeof event.durationMs === "number" ? { durationMs: Math.round(event.durationMs) } : {}),
    });
  });
}

export default {
  id: "fleet-gate",
  name: "Fleet gate",
  description: "Stops an agent's P16 stop-and-file tool calls and files them for the owner instead of asking in chat",
  register,
};
