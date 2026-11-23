/**
 * study-door core: one panel turn in, one gated reply out. No HTTP in here (http.js) and
 * no environment (server.js), so everything below is tested with a fake gateway.
 *
 * The pipeline per turn: validate the request, ask the study-panel agent for its one `reply`
 * call, validate that call's shape, run the code-in-reply gates on it, sanitise its visual. A
 * reply that fails any step of the last three becomes the fixed fallback, and the reject reason
 * is logged. Nothing here ever logs or returns the request or the rejected reply: the log carries
 * sizes, hashes and reasons only; the optional audit spool is the one place replies are kept.
 */

import { createHash, randomBytes } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { codeInReply } from "./gates.js";
import { UpstreamError } from "./gateway.js";
import { buildPrompt } from "./prompt.js";
import { sanitizeHtml } from "./sanitize.js";
import { validateReply, validateTurn } from "./schema.js";

/** The reply the owner sees when the model's own reply does not pass. It asks for nothing the page could abuse. */
export const FALLBACK_REPLY = Object.freeze({
  text: "I could not give you a good answer to that. Tell me in one word where you are stuck, or try again.",
  expects: "tap",
  options: Object.freeze([Object.freeze({ id: "retry", label: "Try again" })]),
});

const sha = (s, n = 12) => createHash("sha256").update(String(s)).digest("hex").slice(0, n);
const BUCKETS = [1, 2, 5, 10, 20, 40, 90];

export function createMetrics() {
  const counters = new Map();
  const latency = { buckets: BUCKETS.map(() => 0), sum: 0, count: 0 };
  return {
    inc(name, labels = {}) {
      const pairs = Object.entries(labels).map(([k, v]) => `${k}="${String(v).replace(/[^a-zA-Z0-9_.:-]/g, "_")}"`);
      const key = pairs.length ? `${name}{${pairs.join(",")}}` : name;
      counters.set(key, (counters.get(key) ?? 0) + 1);
    },
    observe(seconds) {
      latency.sum += seconds;
      latency.count += 1;
      BUCKETS.forEach((b, i) => {
        if (seconds <= b) latency.buckets[i] += 1;
      });
    },
    render() {
      const out = [
        "# HELP study_door_events_total Door events by kind and outcome.",
        "# TYPE study_door_events_total counter",
      ];
      for (const [k, v] of [...counters].sort()) out.push(`${k} ${v}`);
      out.push("# HELP study_door_gateway_seconds Gateway turn latency.", "# TYPE study_door_gateway_seconds histogram");
      BUCKETS.forEach((b, i) => out.push(`study_door_gateway_seconds_bucket{le="${b}"} ${latency.buckets[i]}`));
      out.push(
        `study_door_gateway_seconds_bucket{le="+Inf"} ${latency.count}`,
        `study_door_gateway_seconds_sum ${latency.sum}`,
        `study_door_gateway_seconds_count ${latency.count}`,
        "",
      );
      return out.join("\n");
    },
  };
}

/**
 * Optional audit spool: the replies (and the rejected raw ones) so a leak is found rather than guessed.
 * Mode 700 directory, mode 600 files. Never the request, never the page.
 */
export function createAudit(dir, log = () => {}) {
  if (!dir) return () => {};
  return function audit(entry) {
    try {
      mkdirSync(dir, { recursive: true, mode: 0o700 });
      const name = `${entry.at.replace(/[:.]/g, "-")}-${entry.trace_id}.json`;
      writeFileSync(join(dir, name), `${JSON.stringify(entry, null, 2)}\n`, { flag: "wx", mode: 0o600 });
    } catch (err) {
      log("warn", "audit spool write failed", { error: err.code ?? err.message });
    }
  };
}

export function createApp({ gateway, sessions, metrics = createMetrics(), audit = () => {}, log = () => {}, now = () => new Date() }) {
  const clock = () => performance.now();

  async function turn(body, { traceId = randomBytes(16).toString("hex"), bytes = 0 } = {}) {
    const started = clock();
    const parsed = validateTurn(body);
    if (!parsed.ok) {
      metrics.inc("study_door_events_total", { kind: "turn", outcome: "bad_request" });
      log("info", "turn rejected", { trace_id: traceId, outcome: "bad_request", reject: parsed.reason, req_bytes: bytes });
      return { status: 400, body: { error: "bad_request", reason: parsed.reason } };
    }
    const { input, page } = parsed.value;
    const session = sessions(page);
    const base = { trace_id: traceId, session: sha(session.key, 8), turn: session.turn, input: input.kind, req_bytes: bytes };

    let args;
    const t0 = clock();
    try {
      args = await gateway.turn(buildPrompt(input, page), session.key, { traceparent: `00-${traceId}-${randomBytes(8).toString("hex")}-01` });
    } catch (err) {
      const reason = err instanceof UpstreamError ? err.reason : "internal";
      metrics.observe((clock() - t0) / 1000);
      metrics.inc("study_door_events_total", { kind: "turn", outcome: "upstream_error" });
      log("warn", "gateway turn failed", { ...base, outcome: "upstream_error", reject: reason, upstream_status: err.status, ms: Math.round(clock() - started) });
      return { status: reason === "timeout" ? 504 : 502, body: { error: "upstream", reason } };
    }
    metrics.observe((clock() - t0) / 1000);

    let reject = null;
    let reply = null;
    let removed = 0;
    const shape = validateReply(args);
    if (!shape.ok) reject = shape.reason;
    else {
      reject = codeInReply(shape.value, page.code);
      if (!reject) {
        reply = { ...shape.value };
        if (reply.visual) {
          const clean = sanitizeHtml(reply.visual.html);
          removed = clean.removed;
          if (clean.html.trim()) reply.visual = { html: clean.html };
          else delete reply.visual;
        }
      }
    }

    const fallback = reject !== null;
    const out = fallback ? FALLBACK_REPLY : reply;
    metrics.inc("study_door_events_total", { kind: "turn", outcome: fallback ? "fallback" : "ok" });
    if (fallback) metrics.inc("study_door_events_total", { kind: "reject", reason: reject });
    if (removed) metrics.inc("study_door_events_total", { kind: "sanitized", reason: "stripped" });
    const payload = JSON.stringify(out);
    log(fallback ? "warn" : "info", "turn", {
      ...base,
      outcome: fallback ? "fallback" : "ok",
      reject: reject ?? undefined,
      stripped: removed || undefined,
      reply_bytes: payload.length,
      reply_sha: sha(payload),
      ms: Math.round(clock() - started),
    });
    audit({
      at: now().toISOString(),
      trace_id: traceId,
      session: base.session,
      turn: session.turn,
      outcome: fallback ? "fallback" : "ok",
      reject,
      reply: fallback ? args : reply,
    });
    return { status: 200, body: { v: 1, fallback, turn: session.turn, reply: out } };
  }

  return { turn, metrics: () => metrics.render(), count: (name, labels) => metrics.inc(name, labels) };
}
