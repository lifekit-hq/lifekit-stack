/**
 * notify-relay — HTTP webhook that turns a producer's POST into a Telegram
 * message via Telegram's Bot API.
 *
 * Endpoints:
 *   GET  /health   — liveness probe → {ok:true}
 *   POST /notify   — body = a v1 notify envelope; rendered to Telegram HTML by
 *                    render.js. The format every producer should be on; see
 *                    docs/notify-envelope.md.
 *   POST /devclaw  — legacy: body = devclaw task row JSON. Mapped onto an
 *                    envelope by task-row.js and rendered by render.js, so this
 *                    relay has exactly one renderer.
 *   POST /text     — legacy: body.text is a pre-composed string from the devclaw
 *                    goal layer, escaped and sent as HTML. Retired once devclaw
 *                    posts envelopes (US3).
 *
 * Required env:
 *   TELEGRAM_BOT_TOKEN     — bot token (from @BotFather)
 *   LIFEKIT_TELEGRAM_CHAT  — chat id to send to
 *
 * No docker socket, no exec, no npm deps.
 */

import { createServer } from "node:http";

import {
  MAX_MSG_CHARS,
  escapeClipped,
  renderEnvelope,
  validateEnvelope,
} from "./render.js";
import { envelopeFromTaskRow } from "./task-row.js";

const PORT = Number(process.env.NOTIFY_RELAY_PORT ?? 8090);
// The largest envelope worth reading; `detail` is clipped to ~3.5k anyway, so
// anything past this is a producer bug and gets a 400 instead of our memory.
const MAX_BODY_BYTES = 256 * 1024;
const TOKEN = process.env.TELEGRAM_BOT_TOKEN ?? "";
const CHAT = process.env.LIFEKIT_TELEGRAM_CHAT ?? "";

if (!TOKEN) {
  process.stderr.write("TELEGRAM_BOT_TOKEN env var is required\n");
  process.exit(1);
}
if (!CHAT) {
  process.stderr.write("LIFEKIT_TELEGRAM_CHAT env var is required\n");
  process.exit(1);
}

// Every path through this relay escapes its producer's text, so every message
// goes out as HTML — there is no plain-text mode to fall back to.
async function sendTelegram(text) {
  const url = `https://api.telegram.org/bot${TOKEN}/sendMessage`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      chat_id: CHAT,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.ok !== true) {
    throw new Error(
      `Telegram API ${res.status}: ${body.description ?? "unknown error"}`,
    );
  }
  return body.result;
}

// Chunks are concatenated as bytes, not decoded one by one: a multi-byte
// character split across two chunks would otherwise become U+FFFD, and an
// envelope full of emoji rides this path.
//
// Returns null when the body is over MAX_BODY_BYTES. An over-long body is
// drained rather than abandoned: bailing out mid-upload makes Node reset the
// socket, and the producer then sees a connection error instead of the status
// telling it what it did wrong. Nothing is retained past the cap, so draining
// costs bandwidth, not memory.
async function readBody(req) {
  const chunks = [];
  let size = 0;
  let overflowed = false;
  for await (const chunk of req) {
    size += chunk.length;
    if (overflowed) continue;
    if (size > MAX_BODY_BYTES) {
      overflowed = true;
      chunks.length = 0;
      continue;
    }
    chunks.push(chunk);
  }
  return overflowed ? null : Buffer.concat(chunks).toString("utf8");
}

function respond(res, status, payload) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(payload));
}

const log = (msg) =>
  process.stderr.write(`[${new Date().toISOString()}] ${msg}\n`);

/** Reads + parses the request body, answering with a 400 itself on failure. */
async function readJson(req, res, route) {
  let raw;
  try {
    raw = await readBody(req);
  } catch (err) {
    log(`${route} read-body error: ${err.message}`);
    respond(res, 400, { error: "could not read body" });
    return undefined;
  }
  if (raw === null) {
    log(`${route} body over ${MAX_BODY_BYTES} bytes`);
    respond(res, 413, { error: "body too large" });
    return undefined;
  }
  try {
    return JSON.parse(raw);
  } catch {
    log(`${route} invalid json (first 200 chars): ${raw.slice(0, 200)}`);
    respond(res, 400, { error: "invalid json body" });
    return undefined;
  }
}

/** Sends `text`, answering 200 or 502. `context` only labels the log lines. */
async function deliver(res, text, context) {
  try {
    const result = await sendTelegram(text);
    log(`${context} delivered message_id=${result?.message_id ?? "?"}`);
    respond(res, 200, { ok: true });
  } catch (err) {
    log(`${context} send failed: ${err.message}`);
    respond(res, 502, { error: err.message });
  }
}

// POST /notify — the one format. Producers send an envelope, render.js turns it
// into Telegram HTML. See docs/notify-envelope.md.
async function handleNotify(req, res) {
  const envelope = await readJson(req, res, "/notify");
  if (envelope === undefined) return;

  const errors = validateEnvelope(envelope);
  if (errors.length) {
    log(`/notify rejected envelope: ${errors.join("; ")}`);
    respond(res, 400, { error: "invalid envelope", details: errors });
    return;
  }

  const context = `/notify ${envelope.level} ${envelope.source}/${envelope.subject}`;
  log(context);
  await deliver(res, renderEnvelope(envelope), context);
}

// POST /text — passthrough for the devclaw GOAL layer. Unlike /devclaw (which
// maps a task-row payload onto an envelope), the goal layer (goal_notify.py) has
// already composed the owner-facing message, so its text is the message. It is
// still producer text going out as HTML, so it is escaped and clipped by the
// same helper the renderer uses: the reader sees the string verbatim, and a `<`
// in it can neither forge markup nor fail the send.
async function handleText(req, res) {
  const payload = await readJson(req, res, "/text");
  if (payload === undefined) return;

  const text = String(payload?.text ?? "").trim();
  if (!text) {
    respond(res, 400, { error: "missing 'text'" });
    return;
  }
  await deliver(res, escapeClipped(text, MAX_MSG_CHARS), "/text");
}

// POST /devclaw — legacy devclaw task-row callback, rendered through the
// envelope. The row shape is the contract here; the message grammar is not.
async function handleDevclaw(req, res) {
  const payload = await readJson(req, res, "/devclaw");
  if (payload === undefined) return;

  const context = `/devclaw task=${payload?.task_id ?? "?"} status=${payload?.status ?? "?"}`;
  log(context);
  const text = renderEnvelope(envelopeFromTaskRow(payload));
  await deliver(res, text, context);
}

async function route(req, res) {
  // Route on the path alone: producers append query strings (devclaw's
  // notify_url does), and one must never turn a delivery into a 404.
  const path = new URL(req.url ?? "/", "http://notify-relay").pathname;

  if (path === "/health") {
    respond(res, 200, { ok: true, name: "notify-relay" });
    return;
  }
  if (req.method === "POST" && path === "/notify") {
    await handleNotify(req, res);
    return;
  }
  if (req.method === "POST" && path === "/text") {
    await handleText(req, res);
    return;
  }
  // Sub-paths stay accepted here: this endpoint is live and its callers predate
  // the contract. New producers use /notify.
  if (req.method === "POST" && (path === "/devclaw" || path.startsWith("/devclaw/"))) {
    await handleDevclaw(req, res);
    return;
  }
  respond(res, 404, { error: "not found" });
}

// Exported so server.test.js can drive the real HTTP surface: it imports this
// module with NOTIFY_RELAY_PORT=0, reads the ephemeral port off the listener and
// makes actual requests. Startup is therefore the same code path in test and in
// production — nothing is conditional on being the entrypoint.
export const server = createServer(async (req, res) => {
  // An unhandled rejection in this callback is fatal to the process, and the
  // relay dying silently is exactly the failure the dead-man rules exist for.
  try {
    await route(req, res);
  } catch (err) {
    log(`unhandled error on ${req.method} ${req.url}: ${err.stack ?? err}`);
    if (!res.headersSent) respond(res, 500, { error: "internal error" });
    else res.end();
  }
});

server.listen(PORT, "0.0.0.0", () => {
  process.stderr.write(
    `notify-relay listening on 0.0.0.0:${server.address().port}, chat=${CHAT}\n`,
  );
});

const shutdown = (sig) => {
  process.stderr.write(`received ${sig}, shutting down\n`);
  server.close(() => process.exit(0));
};
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
