/**
 * study-door entry point: reads the env, wires the gateway client, the audit spool and the HTTP
 * layer, and listens. Everything testable is in app.js and http.js.
 *
 * Env:
 *   STUDY_DOOR_TOKEN          the panel's bearer (minted by the operator, pasted once into the
 *                             extension's settings). Without it /v1/turn answers 503.
 *   STUDY_DOOR_EDGE_SECRET    the door's own bearer at the edge's internal entrypoint
 *                             (compose/edge/traefik/openclaw.yml). Not the gateway token.
 *   STUDY_DOOR_GATEWAY_URL    default http://openclaw-edge:18893
 *   STUDY_DOOR_ORIGIN         the one allowed browser Origin (chrome-extension://<id>)
 *   STUDY_DOOR_RATE_PER_MIN   default 20
 *   STUDY_DOOR_SPOOL_DIR      optional audit spool for replies (mode 700); off when unset
 *   STUDY_DOOR_PORT           default 8098
 *
 * No secret is ever logged. Without the secrets the process still starts (a deploy ahead of the
 * operator step must not crash-loop) but /ready and /v1/turn answer 503.
 */

import { createServer } from "node:http";

import { createApp, createAudit, createMetrics } from "./app.js";
import { createGateway } from "./gateway.js";
import { createHandler, createLimiter, createReadiness } from "./http.js";
import { createSessions } from "./prompt.js";

const PORT = Number(process.env.STUDY_DOOR_PORT ?? 8098);
const TOKEN = (process.env.STUDY_DOOR_TOKEN ?? "").trim();
const EDGE_SECRET = (process.env.STUDY_DOOR_EDGE_SECRET ?? "").trim();
const GATEWAY = (process.env.STUDY_DOOR_GATEWAY_URL ?? "http://openclaw-edge:18893").replace(/\/+$/, "");
const ORIGIN = (process.env.STUDY_DOOR_ORIGIN ?? "").trim();
const RATE = Number(process.env.STUDY_DOOR_RATE_PER_MIN ?? 20);
const SPOOL = (process.env.STUDY_DOOR_SPOOL_DIR ?? "").trim();

function log(level, msg, fields = {}) {
  process.stdout.write(`${JSON.stringify({ time: new Date().toISOString(), level, msg, ...fields })}\n`);
}

const gateway = createGateway({ baseUrl: GATEWAY, secret: EDGE_SECRET });
const app = createApp({ gateway, sessions: createSessions(), metrics: createMetrics(), audit: createAudit(SPOOL, log), log });
const ready = createReadiness({ configured: Boolean(TOKEN && EDGE_SECRET), probe: () => gateway.probe(), log });
const server = createServer(createHandler({ app, token: TOKEN, origin: ORIGIN, ready, limiter: createLimiter({ perMinute: RATE }), log }));

server.listen(PORT, "0.0.0.0", () => {
  log("info", "study-door listening", {
    port: PORT,
    token: TOKEN ? "set" : "missing",
    edge_secret: EDGE_SECRET ? "set" : "missing",
    origin: ORIGIN ? "set" : "missing",
    audit: SPOOL ? "on" : "off",
  });
});

const shutdown = (sig) => {
  log("info", "shutting down", { signal: sig });
  server.close(() => process.exit(0));
};
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
