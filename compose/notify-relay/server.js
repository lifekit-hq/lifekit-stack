/**
 * notify-relay — HTTP webhook that turns devclaw's notify_url POST into a
 * Telegram message via Telegram's Bot API.
 *
 * Endpoints:
 *   GET  /health   — liveness probe → {ok:true}
 *   GET  /ready    — readiness: the bot token works against Telegram (getMe,
 *                    cached) → 200 {ready:true} | 503 {ready:false}
 *   GET  /metrics  — Prometheus text (requests, Telegram sends, readiness)
 *   POST /devclaw  — body = devclaw task row JSON; formats + sends as
 *                    Telegram message to $LIFEKIT_TELEGRAM_CHAT
 *   POST /text     — body = {text}; sent verbatim
 *
 * Logs are JSON lines on stdout, one per request, each with the W3C trace id
 * (the caller's traceparent, or a fresh one) — the platform contract
 * (scripts/platform-contract.py). Probe requests (/health, /ready,
 * /metrics) are logged only when they carry a traceparent, so the Docker
 * healthcheck and the Prometheus scrape do not flood the log.
 *
 * Required env:
 *   TELEGRAM_BOT_TOKEN     — bot token (from @BotFather)
 *   LIFEKIT_TELEGRAM_CHAT  — chat id to send to
 *
 * No docker socket, no exec, no extra deps.
 */

import { randomBytes } from "node:crypto";
import { createServer } from "node:http";

const PORT = Number(process.env.NOTIFY_RELAY_PORT ?? 8090);
const TOKEN = process.env.TELEGRAM_BOT_TOKEN ?? "";
const CHAT = process.env.LIFEKIT_TELEGRAM_CHAT ?? "";
const MAX_MSG_CHARS = 3500; // Telegram is 4096; leave headroom

// A pass is cached for 5 minutes; a failure only briefly, so a startup blip
// clears on the next probe instead of reading not-ready for minutes.
const READY_TTL_MS = 5 * 60 * 1000;
const NOT_READY_TTL_MS = 15 * 1000;
const PROBES = new Set(["/health", "/ready", "/metrics"]);

function log(level, msg, fields = {}) {
  process.stdout.write(
    `${JSON.stringify({ time: new Date().toISOString(), level, msg, ...fields })}\n`,
  );
}

if (!TOKEN) {
  log("error", "TELEGRAM_BOT_TOKEN env var is required");
  process.exit(1);
}
if (!CHAT) {
  log("error", "LIFEKIT_TELEGRAM_CHAT env var is required");
  process.exit(1);
}

// W3C trace context: continue the caller's trace, or start one.
const TRACEPARENT = /^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$/;
function traceOf(req) {
  const m = TRACEPARENT.exec(String(req.headers.traceparent ?? "").trim());
  return {
    propagated: Boolean(m),
    trace_id: m ? m[1] : randomBytes(16).toString("hex"),
    span_id: randomBytes(8).toString("hex"),
  };
}

// Prometheus counters, hand-rolled: no deps.
const requests = new Map(); // "route\u0000code" -> n
const sends = { ok: 0, error: 0 };
function countRequest(route, code) {
  const key = `${route}\u0000${code}`;
  requests.set(key, (requests.get(key) ?? 0) + 1);
}

// Readiness = the bot token is accepted by Telegram. A probe inside the cache
// window gets the cached answer, so probes never hammer the Bot API. The
// token never reaches a log or a response.
let readiness = { ready: false, checkedAt: 0, reason: "not checked yet" };
let readinessCheck = null;
async function checkReady() {
  const ttl = readiness.ready ? READY_TTL_MS : NOT_READY_TTL_MS;
  if (Date.now() - readiness.checkedAt < ttl) return readiness;
  readinessCheck ??= (async () => {
    try {
      const res = await fetch(`https://api.telegram.org/bot${TOKEN}/getMe`, {
        signal: AbortSignal.timeout(3000),
      });
      const body = await res.json().catch(() => ({}));
      readiness = {
        ready: res.ok && body.ok === true,
        checkedAt: Date.now(),
        reason: res.ok && body.ok === true ? "" : `Telegram getMe ${res.status}`,
      };
    } catch (err) {
      readiness = {
        ready: false,
        checkedAt: Date.now(),
        reason: `Telegram unreachable (${err.name})`,
      };
    } finally {
      readinessCheck = null;
    }
    return readiness;
  })();
  return readinessCheck;
}

function metricsText() {
  const out = [
    "# HELP notify_relay_requests_total HTTP requests by route and status code.",
    "# TYPE notify_relay_requests_total counter",
  ];
  for (const [key, n] of requests) {
    const [route, code] = key.split("\u0000");
    out.push(`notify_relay_requests_total{route="${route}",code="${code}"} ${n}`);
  }
  out.push(
    "# HELP notify_relay_telegram_sends_total Telegram sendMessage calls by outcome.",
    "# TYPE notify_relay_telegram_sends_total counter",
    `notify_relay_telegram_sends_total{outcome="ok"} ${sends.ok}`,
    `notify_relay_telegram_sends_total{outcome="error"} ${sends.error}`,
    "# HELP notify_relay_ready 1 when the last Telegram getMe check passed.",
    "# TYPE notify_relay_ready gauge",
    `notify_relay_ready ${readiness.ready ? 1 : 0}`,
  );
  return `${out.join("\n")}\n`;
}

const STATUS_ICON = {
  done: "✅",
  failed: "❌",
};

function formatMessage(row) {
  const status = String(row?.status ?? "unknown");
  const icon = STATUS_ICON[status] ?? "ℹ️";
  const kind = String(row?.kind ?? "task");
  const goal = String(row?.goal ?? "").slice(0, 240);
  const taskId = String(row?.task_id ?? "?");

  const lines = [`${icon} devclaw ${kind} — ${status}`];
  if (goal) lines.push(`> ${goal}`);
  lines.push(`task_id: ${taskId.slice(0, 8)}…`);

  if (status === "failed" && row?.error) {
    lines.push("");
    lines.push(`error: ${String(row.error).slice(0, 600)}`);
  } else if (status === "done" && row?.result_json) {
    try {
      const parsed =
        typeof row.result_json === "string"
          ? JSON.parse(row.result_json)
          : row.result_json;
      if (parsed?.message) {
        lines.push("");
        lines.push(String(parsed.message).slice(0, 400));
      }
    } catch {
      // result_json wasn't JSON; skip
    }
  }

  let msg = lines.join("\n");
  if (msg.length > MAX_MSG_CHARS) {
    msg = msg.slice(0, MAX_MSG_CHARS - 14) + "\n… [truncated]";
  }
  return msg;
}

async function sendTelegram(text) {
  const url = `https://api.telegram.org/bot${TOKEN}/sendMessage`;
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: CHAT,
        text,
        disable_web_page_preview: true,
      }),
    });
  } catch (err) {
    sends.error += 1;
    throw err;
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.ok !== true) {
    sends.error += 1;
    throw new Error(
      `Telegram API ${res.status}: ${body.description ?? "unknown error"}`,
    );
  }
  sends.ok += 1;
  return body.result;
}

async function readBody(req) {
  let body = "";
  for await (const chunk of req) body += chunk;
  return body;
}

async function handle(req, res, reqLog) {
  if (req.url === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true, name: "notify-relay" }));
    return;
  }

  if (req.url === "/ready") {
    const { ready, reason } = await checkReady();
    res.writeHead(ready ? 200 : 503, { "content-type": "application/json" });
    res.end(JSON.stringify(ready ? { ready } : { ready, reason }));
    return;
  }

  if (req.url === "/metrics") {
    res.writeHead(200, { "content-type": "text/plain; version=0.0.4" });
    res.end(metricsText());
    return;
  }

  // POST /text — plain-text passthrough for the devclaw GOAL layer. Unlike
  // /devclaw (which formats a task-row payload), the goal layer (goal_notify.py)
  // has already composed the owner-facing message, so we send body.text verbatim.
  if (req.method === "POST" && req.url === "/text") {
    let raw;
    try {
      raw = await readBody(req);
    } catch (err) {
      reqLog("warn", "/text read-body error", { error: err.message });
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "could not read body" }));
      return;
    }
    let text;
    try {
      text = String(JSON.parse(raw)?.text ?? "").trim();
    } catch {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "invalid json body" }));
      return;
    }
    if (!text) {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "missing 'text'" }));
      return;
    }
    if (text.length > MAX_MSG_CHARS) {
      text = text.slice(0, MAX_MSG_CHARS - 14) + "\n… [truncated]";
    }
    try {
      const result = await sendTelegram(text);
      reqLog("info", "/text delivered", { message_id: result?.message_id });
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    } catch (err) {
      reqLog("error", "/text send failed", { error: err.message });
      res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: err.message }));
    }
    return;
  }

  if (req.method !== "POST" || !req.url?.startsWith("/devclaw")) {
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "not found" }));
    return;
  }

  let body;
  try {
    body = await readBody(req);
  } catch (err) {
    reqLog("warn", "/devclaw read-body error", { error: err.message });
    res.writeHead(400, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "could not read body" }));
    return;
  }

  let payload;
  try {
    payload = JSON.parse(body);
  } catch {
    reqLog("warn", "/devclaw invalid json body", { body_head: body.slice(0, 200) });
    res.writeHead(400, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "invalid json body" }));
    return;
  }

  const taskId = payload?.task_id ?? "?";
  const status = payload?.status ?? "?";
  reqLog("info", "/devclaw received", { task: taskId, status });

  const message = formatMessage(payload);
  try {
    const result = await sendTelegram(message);
    reqLog("info", "/devclaw delivered", { task: taskId, message_id: result?.message_id });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
  } catch (err) {
    reqLog("error", "/devclaw send failed", { task: taskId, error: err.message });
    res.writeHead(502, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: err.message }));
  }
}

const server = createServer(async (req, res) => {
  const trace = traceOf(req);
  const path = String(req.url ?? "").split("?")[0];
  // A fixed label set: unknown paths must not grow the metric's cardinality.
  let route = "other";
  if (path.startsWith("/devclaw")) route = "/devclaw";
  else if (PROBES.has(path) || path === "/text") route = path;
  const quiet = PROBES.has(path) && !trace.propagated;
  const { trace_id, span_id } = trace;
  const reqLog = (level, msg, fields = {}) =>
    log(level, msg, { trace_id, span_id, ...fields });
  const started = Date.now();
  res.on("finish", () => {
    countRequest(route, res.statusCode);
    if (!quiet) {
      reqLog("info", "request", {
        method: req.method,
        route,
        status: res.statusCode,
        duration_ms: Date.now() - started,
      });
    }
  });
  try {
    await handle(req, res, reqLog);
  } catch (err) {
    reqLog("error", "unhandled", { error: err.message });
    if (!res.headersSent) res.writeHead(500, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "internal error" }));
  }
});

server.listen(PORT, "0.0.0.0", () => {
  // The chat id is personal data: never logged.
  log("info", "notify-relay listening", { port: PORT });
  checkReady();
});

const shutdown = (sig) => {
  log("info", "shutting down", { signal: sig });
  server.close(() => process.exit(0));
};
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
