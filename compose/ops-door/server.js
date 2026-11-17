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

import { readFileSync } from "node:fs";
import { createServer } from "node:http";

import { OPS_LABEL, createApp } from "./app.js";
import { createHandler, createReadiness, createSpool } from "./http.js";

const PORT = Number(process.env.OPS_DOOR_PORT ?? 8097);
const REPO = process.env.OPS_DOOR_REPO ?? "lifekit-hq/lifekit-stack";
const THRESHOLD = Number(process.env.OPS_DOOR_REPEAT_THRESHOLD ?? 3);
const SPOOL = process.env.OPS_DOOR_SPOOL_DIR ?? "/spool";

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

const decide = createSpool(SPOOL);
const app = createApp({ github, decide, repoUrl: `https://github.com/${REPO}`, threshold: THRESHOLD, log });

const ready = createReadiness({ token: TOKEN, probe: () => gh("GET", `/repos/${REPO}`), log });
const server = createServer(createHandler({ app, token: TOKEN, ready, log }));

server.listen(PORT, "0.0.0.0", () => {
  log("info", "ops-door listening", { port: PORT, repo: REPO, threshold: THRESHOLD, token: TOKEN ? "set" : "missing" });
});

const shutdown = (sig) => {
  log("info", "shutting down", { signal: sig });
  server.close(() => process.exit(0));
};
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
