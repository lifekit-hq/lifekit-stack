// door.js - the one intake door every agent files through (docs/runbook.md,
// "Intake door"), shared by the plugin (index.js) and its CLI (spool.mjs).
//
// file(item) hands the item to devclaw's `file_intake` MCP tool, which files or
// counts onto one GitHub issue per repo + fingerprint (decision 0016). Any
// failure - devclaw unreachable or erroring, GitHub unavailable behind it, an
// unknown project, an envelope it refuses - is written to the local spool
// instead, and the caller still gets a receipt: the door never refuses.
//
// The spool is one JSON file per occurrence under <state>/intake-spool/. A
// writer only ever creates a new file (temp + rename), so a filing turn never
// races the retry. retry() files the oldest first, one devclaw call per
// occurrence, so a repeat that waited in the spool still lands as a count on
// the same issue rather than a second issue. An item whose project devclaw
// does not know stays in the spool, counted and retried under that same
// project until devclaw knows it; the door never files an item under a project
// the filer did not name. A file that keeps failing stays and is retried
// again; nothing is ever dropped.

import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const CLASSES = ["dev", "ops", "finance", "life", "unclear"];
const LOCK_STALE_MS = 15 * 60_000;
const NO_ACCEPTANCE = "unclear: the filer gave no done-when; shape it before work starts";

export function settings() {
  const state = process.env.OPENCLAW_STATE_DIR || "/home/node/.openclaw";
  return {
    url: process.env.INTAKE_DOOR_DEVCLAW_URL || "http://devclaw-mcp:8000/mcp",
    token: process.env.DEVCLAW_MCP_TOKEN || "",
    spool: process.env.INTAKE_DOOR_SPOOL || path.join(state, "intake-spool"),
    timeoutMs: Number.parseInt(process.env.INTAKE_DOOR_TIMEOUT_MS || "", 10) || 20_000,
  };
}

const text = (v) => (typeof v === "string" ? v.trim() : v == null ? "" : String(v).trim());
const line = (v) => text(v).split(/\s+/).filter(Boolean).join(" ");

// The tool's arguments, made whole: nothing missing is refused, it is filled
// with what the item itself says or marked unclear.
export function normalize(params, agentId) {
  const p = params && typeof params === "object" ? params : {};
  const env = p.envelope && typeof p.envelope === "object" ? p.envelope : {};
  const title = line(p.title) || line(text(p.body).split("\n")[0]).slice(0, 120) || "untitled intake item";
  const cls = line(env.class).toLowerCase();
  return {
    project: line(p.project).toLowerCase(),
    title,
    body: text(p.body),
    fingerprint: text(p.fingerprint).toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, ""),
    envelope: {
      origin: line(env.origin) || line(agentId) || "unknown",
      owner: line(env.owner),
      class: CLASSES.includes(cls) ? cls : "unclear",
      acceptance: line(env.acceptance) || NO_ACCEPTANCE,
      thread: line(env.thread),
    },
  };
}

// One key per item: the project and the fingerprint, else the title, as
// devclaw derives it, so a repeat in the spool is the same item.
export function itemKey(item) {
  const fp = item.fingerprint || item.title.toLowerCase().replace(/\b[0-9a-f]{7,}\b|\d+/g, "#");
  return createHash("sha256").update(`${item.project}|${fp}`).digest("hex").slice(0, 16);
}

function bodyFor(item) {
  const e = item.envelope;
  const rows = [
    "Filed through the lifekit intake door.",
    "",
    "```intake-envelope",
    `origin: ${e.origin}`,
    `owner: ${e.owner || item.project}`,
    `class: ${e.class}`,
    `acceptance: ${e.acceptance}`,
    `thread: ${e.thread}`,
    "```",
  ];
  if (item.body) rows.push("", item.body);
  return rows.join("\n");
}

// devclaw grades `owner` as the target repo and takes class `dev` only; the
// caller's own owner and class travel in the body's envelope block.
function devclawArgs(item) {
  return {
    project_id: item.project,
    title: item.title,
    owner: item.project,
    acceptance: item.envelope.acceptance,
    body: bodyFor(item),
    fingerprint: item.fingerprint,
    origin: item.envelope.origin,
    class_: "dev",
    thread: item.envelope.thread,
  };
}

function parseRpc(raw) {
  const t = String(raw || "").trim();
  if (t.startsWith("{")) return JSON.parse(t);
  const data = t
    .split("\n")
    .filter((l) => l.startsWith("data:"))
    .map((l) => l.slice(5).trim())
    .filter(Boolean);
  for (const d of data.reverse()) {
    const msg = JSON.parse(d);
    if (msg && (msg.result !== undefined || msg.error !== undefined)) return msg;
  }
  throw new Error("no JSON-RPC result in the response");
}

// One devclaw `file_intake` call: the MCP handshake (initialize, then
// notifications/initialized, echoing the session id when the server hands one
// out) and the tools/call, all within the one timeout. Resolves
// { ok, receipt } or { ok: false, reason }; never throws.
export async function callDevclaw(args, cfg = settings()) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), cfg.timeoutMs);
  let session = "";
  const post = async (msg) => {
    const res = await fetch(cfg.url, {
      method: "POST",
      signal: ctl.signal,
      headers: {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        ...(cfg.token ? { authorization: `Bearer ${cfg.token}` } : {}),
        ...(session ? { "mcp-session-id": session } : {}),
      },
      body: JSON.stringify({ jsonrpc: "2.0", ...msg }),
    });
    return { res, raw: await res.text() };
  };
  try {
    const init = await post({
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "lifekit-intake-door", version: "1" } },
    });
    if (!init.res.ok) return { ok: false, reason: `devclaw answered HTTP ${init.res.status}` };
    session = init.res.headers.get("mcp-session-id") || "";
    const ack = await post({ method: "notifications/initialized" });
    if (!ack.res.ok) return { ok: false, reason: `devclaw answered HTTP ${ack.res.status}` };
    const { res, raw } = await post({ id: 2, method: "tools/call", params: { name: "file_intake", arguments: args } });
    if (!res.ok) return { ok: false, reason: `devclaw answered HTTP ${res.status}` };
    const msg = parseRpc(raw);
    if (msg.error) return { ok: false, reason: `devclaw error: ${line(msg.error.message).slice(0, 200)}` };
    const content = (msg.result?.content || []).map((c) => c?.text || "").join("");
    if (msg.result?.isError) return { ok: false, reason: `devclaw: ${line(content).slice(0, 200)}` };
    const receipt = JSON.parse(content);
    if (!receipt || typeof receipt !== "object") return { ok: false, reason: "devclaw returned no receipt" };
    if (receipt.state === "rejected" || !receipt.id) {
      const why = line(receipt.grade?.reason) || "rejected with no reason";
      return { ok: false, reason: `devclaw refused: ${why}` };
    }
    return { ok: true, receipt };
  } catch (err) {
    const why = err?.name === "AbortError" ? `timed out after ${cfg.timeoutMs} ms` : line(err?.cause?.code || err?.message || err);
    return { ok: false, reason: `devclaw unreachable: ${why.slice(0, 160)}` };
  } finally {
    clearTimeout(timer);
  }
}

// ── the spool ───────────────────────────────────────────────────────────────

function spoolFiles(dir) {
  let names;
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  return names.filter((n) => n.endsWith(".json")).sort();
}

export function spoolWrite(item, reason, cfg = settings()) {
  const key = itemKey(item);
  fs.mkdirSync(cfg.spool, { recursive: true, mode: 0o700 });
  const repeat = spoolFiles(cfg.spool).some((n) => n.split("-")[1] === key);
  const now = Date.now();
  const name = `${String(now).padStart(14, "0")}-${key}-${randomBytes(4).toString("hex")}.json`;
  const doc = { schema: "intake-spool.v1", key, spooled_at: now, attempts: 0, last_reason: reason, item };
  const tmp = path.join(cfg.spool, `.${name}.tmp`);
  fs.writeFileSync(tmp, `${JSON.stringify(doc)}\n`, { mode: 0o600 });
  fs.renameSync(tmp, path.join(cfg.spool, name));
  return { key, repeat };
}

function spooledReceipt(key, repeat, reason) {
  return {
    id: `spool:${key}`,
    url: "",
    state: repeat ? "working" : "submitted",
    spooled: true,
    reason,
    next: "Held in the local intake spool; it is retried and filed on its own. Carry on, and never hand it to the owner.",
  };
}

// The tool's whole job: file it, or spool it. Always a receipt.
export async function file(params, agentId, cfg = settings()) {
  const item = normalize(params, agentId);
  const got = await callDevclaw(devclawArgs(item), cfg);
  if (got.ok) return { ...got.receipt, spooled: false };
  try {
    const { key, repeat } = spoolWrite(item, got.reason, cfg);
    return spooledReceipt(key, repeat, got.reason);
  } catch (err) {
    return {
      id: "",
      url: "",
      state: "failed",
      spooled: false,
      reason: `${got.reason}; the spool write failed too (${line(err?.code || err?.message || err)})`,
      next: "Say in one line that filing failed and carry on; file it again on your next turn. Never hand it to the owner.",
    };
  }
}

function lock(dir) {
  const at = path.join(dir, ".retry.lock");
  try {
    fs.mkdirSync(at);
    return at;
  } catch (err) {
    if (err?.code !== "EEXIST") return null;
    try {
      if (Date.now() - fs.statSync(at).mtimeMs > LOCK_STALE_MS) {
        fs.rmSync(at, { recursive: true, force: true });
        fs.mkdirSync(at);
        return at;
      }
    } catch {
      // another retrier won the race
    }
    return null;
  }
}

// File every spooled occurrence, oldest first. Returns a summary.
export async function retry(cfg = settings()) {
  const summary = { filed: 0, failed: 0, busy: false, receipts: [] };
  if (!fs.existsSync(cfg.spool)) return summary;
  const held = lock(cfg.spool);
  if (!held) return { ...summary, busy: true };
  try {
    for (const name of spoolFiles(cfg.spool)) {
      const file = path.join(cfg.spool, name);
      let doc;
      try {
        doc = JSON.parse(fs.readFileSync(file, "utf8"));
      } catch {
        continue; // unreadable: left for a person, counted as pending
      }
      const item = doc.item;
      const got = await callDevclaw(devclawArgs(item), cfg);
      if (got.ok) {
        fs.rmSync(file, { force: true });
        summary.filed += 1;
        summary.receipts.push({ key: doc.key, id: got.receipt.id, url: got.receipt.url, state: got.receipt.state });
        continue;
      }
      summary.failed += 1;
      const next = { ...doc, attempts: (doc.attempts || 0) + 1, last_reason: got.reason, last_try: Date.now() };
      const tmp = path.join(cfg.spool, `.${name}.tmp`);
      fs.writeFileSync(tmp, `${JSON.stringify(next)}\n`, { mode: 0o600 });
      fs.renameSync(tmp, file);
    }
  } finally {
    fs.rmSync(held, { recursive: true, force: true });
  }
  return summary;
}

// What the fleet page shows: occurrences waiting, and the oldest one's age.
export function count(cfg = settings()) {
  const out = { schema: "intake-spool-count.v1", pending: 0, oldest_epoch: null };
  for (const name of spoolFiles(cfg.spool)) {
    out.pending += 1;
    let doc = null;
    try {
      doc = JSON.parse(fs.readFileSync(path.join(cfg.spool, name), "utf8"));
    } catch {
      // still pending; its age comes from the name
    }
    const at = Math.floor((doc?.spooled_at || Number.parseInt(name, 10) || 0) / 1000);
    if (at && (out.oldest_epoch === null || at < out.oldest_epoch)) out.oldest_epoch = at;
  }
  return out;
}
