/**
 * ops-door entry point: reads the env, wires the GitHub transport and the
 * escalation spool into app.js, and listens. Everything testable is in app.js.
 *
 * Env:
 *   OPS_DOOR_GITHUB_TOKEN_FILE  path to a file holding the token (preferred), or
 *   OPS_DOOR_GITHUB_TOKEN       the token itself (a fine-grained token with
 *                               Issues read/write on the one repo - nothing else)
 *   OPS_DOOR_REPO               owner/name (default lifekit-hq/lifekit-stack)
 *   OPS_DOOR_REPEAT_THRESHOLD   occurrences before one Tier 2 call (default 3)
 *   OPS_DOOR_SPOOL_DIR          where Tier 2 requests are written for the host
 *                               unit that runs `fleet-policy decide`
 *   OPS_DOOR_PORT               default 8097
 *
 * The token is never logged. Without one the process still starts (so a deploy
 * ahead of the host step does not crash-loop) but /ready and /alerts answer 503.
 */

import { randomBytes } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { join } from "node:path";

import { OPS_LABEL, createApp } from "./app.js";

const PORT = Number(process.env.OPS_DOOR_PORT ?? 8097);
const REPO = process.env.OPS_DOOR_REPO ?? "lifekit-hq/lifekit-stack";
const THRESHOLD = Number(process.env.OPS_DOOR_REPEAT_THRESHOLD ?? 3);
const SPOOL = process.env.OPS_DOOR_SPOOL_DIR ?? "/spool";
const MAX_BODY = 1_000_000;

function log(level, msg, fields = {}) {
  process.stdout.write(`${JSON.stringify({ time: new Date().toISOString(), level, msg, ...fields })}\n`);
}

function readToken() {
  const file = process.env.OPS_DOOR_GITHUB_TOKEN_FILE;
  try {
    if (file) return readFileSync(file, "utf8").trim();
  } catch {
    return "";
  }
  return (process.env.OPS_DOOR_GITHUB_TOKEN ?? "").trim();
}

const TOKEN = readToken();

async function gh(method, path, body) {
  const res = await fetch(`https://api.github.com${path}`, {
    method,
    headers: {
      authorization: `Bearer ${TOKEN}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "lifekit-ops-door",
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) throw new Error(`github ${method} ${path.split("?")[0]} -> ${res.status}`);
  return res.json();
}

const github = {
  listIssues: ({ page }) =>
    gh("GET", `/repos/${REPO}/issues?state=all&labels=${OPS_LABEL}&sort=created&direction=desc&per_page=100&page=${page}`),
  createIssue: (issue) => gh("POST", `/repos/${REPO}/issues`, issue),
  updateIssue: (n, patch) => gh("PATCH", `/repos/${REPO}/issues/${n}`, patch),
  comment: (n, body) => gh("POST", `/repos/${REPO}/issues/${n}/comments`, { body }),
};

// One file per call id, created exclusively: a retry after a partial failure
// finds the file and files nothing twice. The host unit runs the decide.
async function decide(call) {
  const file = join(SPOOL, `${call.id}.json`);
  const request = { ...call, kind: "call", tier: 2, origin: "alertmanager", at: new Date().toISOString() };
  try {
    writeFileSync(file, `${JSON.stringify(request, null, 2)}\n`, { flag: "wx", mode: 0o644 });
  } catch (err) {
    if (err.code !== "EEXIST") throw err;
  }
}

const app = createApp({ github, decide, repoUrl: `https://github.com/${REPO}`, threshold: THRESHOLD, log });

let readyAt = 0;
let readyOk = false;
async function ready() {
  if (!TOKEN) return false;
  if (Date.now() - readyAt < 60_000) return readyOk;
  try {
    await gh("GET", `/repos/${REPO}`);
    readyOk = true;
  } catch (err) {
    log("warn", "github not ready", { error: err.message });
    readyOk = false;
  }
  readyAt = Date.now();
  return readyOk;
}

const TRACEPARENT = /^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$/;
function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        reject(Object.assign(new Error("body too large"), { status: 413 }));
        req.destroy();
      } else chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

const server = createServer(async (req, res) => {
  const send = (status, body, type = "application/json") => {
    res.writeHead(status, { "content-type": type });
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
      if (!TOKEN) return send(503, { error: "no github token configured" });
      let payload;
      try {
        payload = JSON.parse(await readBody(req));
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
});

server.listen(PORT, "0.0.0.0", () => {
  log("info", "ops-door listening", { port: PORT, repo: REPO, threshold: THRESHOLD, token: TOKEN ? "set" : "missing" });
});

const shutdown = (sig) => {
  log("info", "shutting down", { signal: sig });
  server.close(() => process.exit(0));
};
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
