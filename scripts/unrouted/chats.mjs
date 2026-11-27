// chats.mjs - the agents' chat messages of one window, read-only from the
// gateway session store (docs/runbook.md, "Daily unrouted check"). Runs inside
// the gateway container, fed on stdin by unrouted.py:
//
//   docker exec -i -e SINCE_MS=<ms> -e UNTIL_MS=<ms> <gateway> \
//     node --input-type=module - < chats.mjs
//
// Prints one JSON line per user or assistant text message created in
// [SINCE_MS, UNTIL_MS): {agent, session_key, channel, chat_type, ts, role,
// owner, text}; `owner` is the gateway's senderIsOwner mark (null when absent).
// Every per-agent SQLite store is opened read-only; nothing is written and the
// text never leaves the host (the caller keeps it in memory).
import { DatabaseSync } from "node:sqlite";
import { existsSync, readdirSync } from "node:fs";
import zlib from "node:zlib";

const root = `${process.env.OPENCLAW_STATE_DIR || "/home/node/.openclaw"}/agents`;
const since = Number(process.env.SINCE_MS);
const until = Number(process.env.UNTIL_MS);
if (!Number.isFinite(since) || !Number.isFinite(until) || until <= since) {
  process.stderr.write("chats.mjs: SINCE_MS and UNTIL_MS (epoch ms, since < until) are required\n");
  process.exit(2);
}

function textOf(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((c) => c && c.type === "text" && typeof c.text === "string")
    .map((c) => c.text)
    .join("\n");
}

for (const agent of existsSync(root) ? readdirSync(root).sort() : []) {
  const file = `${root}/${agent}/agent/openclaw-agent.sqlite`;
  if (!existsSync(file)) continue;
  const db = new DatabaseSync(file, { readOnly: true });
  try {
    const windows = new Map();
    for (const w of db.prepare("SELECT session_id, session_key, channel, chat_type FROM session_windows").iterate()) {
      windows.set(w.session_id, w);
    }
    const rows = db.prepare(
      "SELECT session_id, event_json, event_zstd FROM transcript_events " +
        "WHERE created_at >= ? AND created_at < ? AND message_role IN ('user', 'assistant') " +
        "ORDER BY created_at, seq",
    );
    for (const r of rows.iterate(since, until)) {
      let raw = r.event_json;
      if (!raw && r.event_zstd) {
        try {
          raw = zlib.zstdDecompressSync(r.event_zstd).toString("utf8");
        } catch {
          continue;
        }
      }
      let event;
      try {
        event = JSON.parse(raw);
      } catch {
        continue;
      }
      const m = event?.message;
      if (!m || (m.role !== "user" && m.role !== "assistant")) continue;
      const text = textOf(m.content);
      if (!text.trim()) continue;
      const w = windows.get(r.session_id) || {};
      process.stdout.write(
        `${JSON.stringify({
          agent,
          session_key: w.session_key ?? null,
          channel: w.channel ?? null,
          chat_type: w.chat_type ?? null,
          ts: event.timestamp ?? null,
          role: m.role,
          owner: m.__openclaw?.senderIsOwner ?? null,
          text,
        })}\n`,
      );
    }
  } finally {
    db.close();
  }
}
