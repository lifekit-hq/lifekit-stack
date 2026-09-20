/**
 * notify-relay application — the HTTP handler, built by createApp() so the
 * outbound Telegram transport is injected (tests hand in a recorder; the
 * entry point, server.js, hands in fetch).
 *
 * Endpoints:
 *   GET  /health   — liveness probe → {ok:true}
 *   GET  /ready    — readiness: the bot token works against Telegram (getMe,
 *                    cached) → 200 {ready:true} | 503 {ready:false}
 *   GET  /metrics  — Prometheus text (requests, Telegram sends, readiness)
 *   POST /notify   — body = message envelope (docs/message-format.md);
 *                    rendered to Telegram HTML by render.js
 *   POST /devclaw  — body = devclaw task row JSON (compatibility: mapped onto
 *                    the same renderer)
 *   POST /text     — body = {text} (compatibility: rendered as an `info`
 *                    envelope, text escaped)
 *
 * Logs are JSON lines on stdout, one per request, each with the W3C trace id
 * (the caller's traceparent, or a fresh one) — the platform contract
 * (scripts/platform-contract.py). Probe requests (/health, /ready,
 * /metrics) are logged only when they carry a traceparent, so the Docker
 * healthcheck and the Prometheus scrape do not flood the log.
 */

import { randomBytes } from "node:crypto";

import {
  envelopeFromDevclawRow,
  envelopeFromText,
  render,
  validateEnvelope,
} from "./render.js";

// A pass is cached for 5 minutes; a failure only briefly, so a startup blip
// clears on the next probe instead of reading not-ready for minutes.
const READY_TTL_MS = 5 * 60 * 1000;
const NOT_READY_TTL_MS = 15 * 1000;
const PROBES = new Set(["/health", "/ready", "/metrics"]);
const POST_ROUTES = new Set(["/notify", "/devclaw", "/text"]);
const TELEGRAM_API = "https://api.telegram.org";

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

async function readBody(req) {
  let body = "";
  for await (const chunk of req) body += chunk;
  return body;
}

function json(res, status, payload) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(payload));
}

/**
 * The default transport: a request object → fetch. `request` carries method,
 * url, headers, body (string) and an optional timeout in ms.
 */
export async function fetchTransport(request) {
  return fetch(request.url, {
    method: request.method,
    headers: request.headers,
    body: request.body,
    signal: request.timeoutMs ? AbortSignal.timeout(request.timeoutMs) : undefined,
  });
}

/**
 * Build the relay. `transport(request)` is the only way bytes leave the
 * process; it must resolve to a Response-like object ({ok, status, json()}).
 */
export function createApp({ token, chat, transport = fetchTransport, log }) {
  if (!token) throw new Error("token is required");
  if (!chat) throw new Error("chat is required");
  if (!log) throw new Error("log is required");

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
        const res = await transport({
          method: "GET",
          url: `${TELEGRAM_API}/bot${token}/getMe`,
          headers: {},
          timeoutMs: 3000,
        });
        const body = await res.json().catch(() => ({}));
        const ok = res.ok && body.ok === true;
        readiness = {
          ready: ok,
          checkedAt: Date.now(),
          reason: ok ? "" : `Telegram getMe ${res.status}`,
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
      "# HELP notify_relay_ready 1 when the last Telegram getMe succeeded.",
      "# TYPE notify_relay_ready gauge",
      `notify_relay_ready ${readiness.ready ? 1 : 0}`,
    );
    return `${out.join("\n")}\n`;
  }

  // Every message leaves through here: one renderer, one parse mode.
  async function sendTelegram(html) {
    let res;
    try {
      res = await transport({
        method: "POST",
        url: `${TELEGRAM_API}/bot${token}/sendMessage`,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          chat_id: chat,
          text: html,
          parse_mode: "HTML",
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
      throw new Error(`Telegram API ${res.status}: ${body.description ?? "unknown error"}`);
    }
    sends.ok += 1;
    return body.result;
  }

  // Read + parse a JSON POST body; on failure the 400 has been written.
  async function readJson(req, res, reqLog, route) {
    let raw;
    try {
      raw = await readBody(req);
    } catch (err) {
      reqLog("warn", `${route} read-body error`, { error: err.message });
      json(res, 400, { error: "could not read body" });
      return undefined;
    }
    try {
      return JSON.parse(raw);
    } catch {
      reqLog("warn", `${route} invalid json body`, { body_head: raw.slice(0, 200) });
      json(res, 400, { error: "invalid json body" });
      return undefined;
    }
  }

  async function deliver(res, reqLog, route, envelope, fields = {}) {
    try {
      const result = await sendTelegram(render(envelope));
      reqLog("info", `${route} delivered`, { ...fields, message_id: result?.message_id });
      json(res, 200, { ok: true });
    } catch (err) {
      reqLog("error", `${route} send failed`, { ...fields, error: err.message });
      json(res, 502, { error: err.message });
    }
  }

  async function handle(req, res, reqLog, path) {
    if (path === "/health") {
      json(res, 200, { ok: true, name: "notify-relay" });
      return;
    }

    if (path === "/ready") {
      const { ready, reason } = await checkReady();
      json(res, ready ? 200 : 503, ready ? { ready } : { ready, reason });
      return;
    }

    if (path === "/metrics") {
      res.writeHead(200, { "content-type": "text/plain; version=0.0.4; charset=utf-8" });
      res.end(metricsText());
      return;
    }

    if (req.method !== "POST" || !POST_ROUTES.has(path)) {
      json(res, 404, { error: "not found" });
      return;
    }

    const payload = await readJson(req, res, reqLog, path);
    if (payload === undefined) return;

    // POST /notify — the envelope contract (docs/message-format.md).
    if (path === "/notify") {
      const problems = validateEnvelope(payload);
      if (problems.length) {
        reqLog("warn", "/notify invalid envelope", { problems });
        json(res, 400, { error: problems.join("; ") });
        return;
      }
      await deliver(res, reqLog, path, payload, {
        level: payload.level,
        source: payload.source,
      });
      return;
    }

    // POST /text — the devclaw GOAL layer (goal_notify.py) composes the text
    // itself; it rides through as an `info` envelope so it gets escaped.
    if (path === "/text") {
      const text = String(payload?.text ?? "").trim();
      if (!text) {
        json(res, 400, { error: "missing 'text'" });
        return;
      }
      await deliver(res, reqLog, path, envelopeFromText(text));
      return;
    }

    // POST /devclaw — task-row callback, mapped onto the envelope.
    const taskId = payload?.task_id ?? "?";
    const status = payload?.status ?? "?";
    reqLog("info", "/devclaw received", { task: taskId, status });
    await deliver(res, reqLog, path, envelopeFromDevclawRow(payload), { task: taskId });
  }

  async function requestListener(req, res) {
    const trace = traceOf(req);
    // /devclaw has always accepted any sub-path (devclaw's notify_url may carry
    // one); it stays a prefix match so the switch to the renderer changes nothing.
    const rawPath = String(req.url ?? "").split("?")[0];
    const path = rawPath.startsWith("/devclaw") ? "/devclaw" : rawPath;
    // A fixed label set: unknown paths must not grow the metric's cardinality.
    const route = PROBES.has(path) || POST_ROUTES.has(path) ? path : "other";
    const quiet = PROBES.has(path) && !trace.propagated;
    const { trace_id, span_id } = trace;
    const reqLog = (level, msg, fields = {}) => log(level, msg, { trace_id, span_id, ...fields });
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
      await handle(req, res, reqLog, path);
    } catch (err) {
      reqLog("error", "unhandled", { error: err.message });
      if (!res.headersSent) res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "internal error" }));
    }
  }

  return { requestListener, checkReady };
}
