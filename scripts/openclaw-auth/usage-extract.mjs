// Print one JSON line per API-billed assistant turn in every agent's transcript
// store, run INSIDE the gateway container by spend_guard.py:
//
//   docker exec -i -e SINCE_MS=<epoch ms> <gateway> node --input-type=module - < usage-extract.mjs
//
// Only turns the direct runtime answered count: their provider is "anthropic"
// (api "anthropic-messages"). Turns on the Claude Code runtime are logged with
// provider "claude-cli" and are covered by the subscription, so they are never
// emitted. Transcript events are plain JSON, or zstd-compressed when large.
// Read-only; no message text leaves this process, only ids, time, model and
// token counts: {"id","agent","ts","model","in","out","cr","cw"}.
import { DatabaseSync } from "node:sqlite";
import { readdirSync, existsSync } from "node:fs";
import zlib from "node:zlib";

const ROOT = process.env.OPENCLAW_AGENTS_DIR || "/home/node/.openclaw/agents";
const since = Number(process.env.SINCE_MS || 0);
const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);

for (const agent of readdirSync(ROOT)) {
  const file = `${ROOT}/${agent}/agent/openclaw-agent.sqlite`;
  if (!existsSync(file)) continue;
  const db = new DatabaseSync(file, { readOnly: true });
  const rows = db
    .prepare("SELECT event_json, event_zstd FROM transcript_events WHERE created_at >= ?")
    .iterate(since);
  for (const row of rows) {
    let text = row.event_json;
    if (!text) {
      try {
        text = zlib.zstdDecompressSync(row.event_zstd).toString("utf8");
      } catch {
        continue;
      }
    }
    if (!text.includes('"usage"')) continue;
    let event;
    try {
      event = JSON.parse(text);
    } catch {
      continue;
    }
    const m = event.message;
    if (!m || m.role !== "assistant" || m.provider !== "anthropic" || !m.usage) continue;
    const ts = num(m.timestamp) || Date.parse(event.timestamp) || 0;
    if (ts < since) continue;
    console.log(
      JSON.stringify({
        id: event.id,
        agent,
        ts,
        model: String(m.model || ""),
        in: num(m.usage.input),
        out: num(m.usage.output),
        cr: num(m.usage.cacheRead),
        cw: num(m.usage.cacheWrite),
      }),
    );
  }
  db.close();
}
