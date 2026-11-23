/**
 * study-door HTTP layer: probes, the bearer, the extension-only CORS, the rate limit and the body
 * cap in front of app.turn. A separate module so it is tested against a real listener.
 */

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

export const MAX_BODY = 64 * 1024;
const PROBES = new Set(["/health", "/ready", "/metrics"]);
const TRACEPARENT = /^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$/;

const digest = (s) => createHash("sha256").update(String(s)).digest();
/** Constant-time compare of two secrets, whatever their lengths. */
export const sameSecret = (a, b) => timingSafeEqual(digest(a), digest(b));

/** Fixed one-minute windows; one owner, so one global bucket (counted after auth). */
export function createLimiter({ perMinute, clock = Date.now }) {
  let windowStart = 0;
  let used = 0;
  return function take() {
    const t = clock();
    if (t - windowStart >= 60_000) {
      windowStart = t;
      used = 0;
    }
    if (used >= perMinute) return Math.max(1, Math.ceil((windowStart + 60_000 - t) / 1000));
    used += 1;
    return 0;
  };
}

/**
 * Readiness is a cheap authenticated gateway probe: it proves the edge secret, the route and the
 * gateway's acceptance of the identity. A success is cached for `okMs`, a failure for `failMs`.
 */
export function createReadiness({ configured, probe, log = () => {}, clock = Date.now, okMs = 60_000, failMs = 5_000 }) {
  let until = 0;
  let ok = false;
  return async function ready() {
    if (!configured) return false;
    if (clock() < until) return ok;
    try {
      await probe();
      ok = true;
    } catch (err) {
      log("warn", "gateway not ready", { reason: err.reason ?? err.message, upstream_status: err.status });
      ok = false;
    }
    until = clock() + (ok ? okMs : failMs);
    return ok;
  };
}

function readBody(req, max) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > max) reject(Object.assign(new Error("body too large"), { status: 413 }));
      else chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

export function createHandler({ app, token, origin, ready, limiter, log = () => {}, maxBody = MAX_BODY }) {
  return async (req, res) => {
    const reqOrigin = req.headers.origin;
    const send = (status, body, type = "application/json", extra = {}) => {
      const cors = reqOrigin && origin && reqOrigin === origin ? { "access-control-allow-origin": origin, vary: "Origin" } : {};
      res.writeHead(status, {
        "content-type": type,
        "cache-control": "no-store",
        ...cors,
        ...extra,
        ...(status === 413 ? { connection: "close" } : {}),
      });
      if (status === 413) res.once("finish", () => req.destroy());
      res.end(typeof body === "string" ? body : JSON.stringify(body));
    };
    const path = (req.url ?? "").split("?")[0];
    const m = TRACEPARENT.exec(String(req.headers.traceparent ?? "").trim());
    const trace_id = m ? m[1] : randomBytes(16).toString("hex");
    if (m && req.method === "GET" && PROBES.has(path)) {
      res.on("finish", () => log("info", "probe", { trace_id, route: path, status: res.statusCode }));
    }
    try {
      if (req.method === "GET" && path === "/health") return send(200, { ok: true });
      if (req.method === "GET" && path === "/ready") {
        return (await ready()) ? send(200, { ready: true }) : send(503, { ready: false });
      }
      if (req.method === "GET" && path === "/metrics") return send(200, app.metrics(), "text/plain; version=0.0.4");

      if (path === "/v1/turn") {
        // A browser sends Origin; only the extension's is let in. No Origin = not a browser (the operator's curl).
        if (reqOrigin !== undefined && (!origin || reqOrigin !== origin)) {
          app.count("study_door_events_total", { kind: "http", outcome: "bad_origin" });
          log("warn", "origin refused", { trace_id, outcome: "bad_origin" });
          return send(403, { error: "origin" });
        }
        if (req.method === "OPTIONS") {
          return send(204, "", "text/plain", {
            "access-control-allow-methods": "POST",
            "access-control-allow-headers": "authorization, content-type",
            "access-control-max-age": "600",
          });
        }
        if (req.method !== "POST") return send(405, { error: "method" }, "application/json", { allow: "POST, OPTIONS" });
        if (!token) return send(503, { error: "not configured" });
        const given = /^Bearer (.+)$/.exec(String(req.headers.authorization ?? ""));
        if (!given || !sameSecret(given[1], token)) {
          app.count("study_door_events_total", { kind: "http", outcome: "unauthorized" });
          log("warn", "unauthorized", { trace_id, outcome: "unauthorized" });
          return send(401, { error: "unauthorized" }, "application/json", { "www-authenticate": "Bearer" });
        }
        const wait = limiter();
        if (wait) {
          app.count("study_door_events_total", { kind: "http", outcome: "rate_limited" });
          return send(429, { error: "rate_limited" }, "application/json", { "retry-after": String(wait) });
        }
        if (!/^application\/json\b/i.test(String(req.headers["content-type"] ?? ""))) {
          return send(415, { error: "content_type" });
        }
        let raw;
        try {
          raw = await readBody(req, maxBody);
        } catch (err) {
          if (err.status === 413) {
            app.count("study_door_events_total", { kind: "http", outcome: "too_large" });
            log("warn", "body too large", { trace_id, outcome: "too_large", limit: maxBody });
          }
          return send(err.status ?? 400, { error: err.status === 413 ? "body_too_large" : "bad_request" });
        }
        let body;
        try {
          body = JSON.parse(raw.toString("utf8"));
        } catch {
          return send(400, { error: "bad_request", reason: "not_json" });
        }
        const out = await app.turn(body, { traceId: trace_id, bytes: raw.length });
        return send(out.status, out.body);
      }
      return send(404, { error: "not found" });
    } catch (err) {
      log("error", "request failed", { trace_id, error: err.message });
      return send(500, { error: "internal" });
    }
  };
}
