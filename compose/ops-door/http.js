/**
 * ops-door HTTP and spool layer, separate from server.js so it is tested
 * against a real listener and a temp spool directory.
 */

import { randomBytes } from "node:crypto";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

export const MAX_BODY = 1_000_000;
const TRACEPARENT = /^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$/;

/**
 * One file per call id, created exclusively: a retry after a partial failure
 * finds the file and files nothing twice. The host unit runs the decide.
 */
export function createSpool(dir, now = () => new Date().toISOString()) {
  return async function decide(call) {
    const file = join(dir, `${call.id}.json`);
    const request = { ...call, kind: "call", tier: 2, origin: "alertmanager", at: now() };
    try {
      writeFileSync(file, `${JSON.stringify(request, null, 2)}\n`, { flag: "wx", mode: 0o644 });
    } catch (err) {
      if (err.code !== "EEXIST") throw err;
    }
  };
}

/**
 * Readiness is a GitHub probe. A success is cached for `okMs`; a failure only
 * for `failMs`, short enough that a transient first failure after start does
 * not outlast the deploy's wait for /ready.
 */
export function createReadiness({ token, probe, log = () => {}, clock = Date.now, okMs = 60_000, failMs = 5_000 }) {
  let until = 0;
  let ok = false;
  return async function ready() {
    if (!token) return false;
    if (clock() < until) return ok;
    try {
      await probe();
      ok = true;
    } catch (err) {
      log("warn", "github not ready", { error: err.message });
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
      if (size > max) {
        reject(Object.assign(new Error("body too large"), { status: 413 }));
      } else chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

export function createHandler({ app, token, ready, log = () => {}, maxBody = MAX_BODY }) {
  return async (req, res) => {
    const send = (status, body, type = "application/json") => {
      res.writeHead(status, { "content-type": type, ...(status === 413 ? { connection: "close" } : {}) });
      if (status === 413) res.once("finish", () => req.destroy());
      res.end(typeof body === "string" ? body : JSON.stringify(body));
    };
    const path = (req.url ?? "").split("?")[0];
    try {
      if (req.method === "GET" && path === "/health") return send(200, { ok: true });
      if (req.method === "GET" && path === "/ready") {
        return (await ready()) ? send(200, { ready: true }) : send(503, { ready: false });
      }
      if (req.method === "GET" && path === "/metrics") {
        return send(200, app.metrics(), "text/plain; version=0.0.4");
      }
      if (req.method === "POST" && path === "/alerts") {
        const m = TRACEPARENT.exec(String(req.headers.traceparent ?? "").trim());
        const trace_id = m ? m[1] : randomBytes(16).toString("hex");
        if (!token) return send(503, { error: "no github token configured" });
        let payload;
        try {
          payload = JSON.parse(await readBody(req, maxBody));
        } catch (err) {
          return send(err.status ?? 400, { error: err.status === 413 ? "body too large" : "invalid json" });
        }
        try {
          const out = await app.handlePayload(payload);
          log("info", "alerts handled", { trace_id, alerts: out.receipts.length });
          return send(200, out);
        } catch (err) {
          // 502 so the sender retries; nothing here is half-applied that a retry cannot finish.
          log("error", "alerts failed", { trace_id, error: err.message });
          return send(502, { error: "upstream failure" });
        }
      }
      return send(404, { error: "not found" });
    } catch (err) {
      log("error", "request failed", { error: err.message });
      return send(500, { error: "internal" });
    }
  };
}
