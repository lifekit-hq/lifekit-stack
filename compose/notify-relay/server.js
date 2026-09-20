/**
 * notify-relay entry point — reads the env, builds the app (app.js) with the
 * real fetch transport, and listens. Everything testable lives in app.js and
 * render.js; this file only wires the process.
 *
 * Required env:
 *   TELEGRAM_BOT_TOKEN     — bot token (from @BotFather)
 *   LIFEKIT_TELEGRAM_CHAT  — chat id to send to
 *
 * No docker socket, no exec, no extra deps.
 */

import { createServer } from "node:http";

import { createApp } from "./app.js";

const PORT = Number(process.env.NOTIFY_RELAY_PORT ?? 8090);
const TOKEN = process.env.TELEGRAM_BOT_TOKEN ?? "";
const CHAT = process.env.LIFEKIT_TELEGRAM_CHAT ?? "";

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

const app = createApp({ token: TOKEN, chat: CHAT, log });
const server = createServer(app.requestListener);

server.listen(PORT, "0.0.0.0", () => {
  // The chat id is personal data: never logged.
  log("info", "notify-relay listening", { port: PORT });
  app.checkReady();
});

const shutdown = (sig) => {
  log("info", "shutting down", { signal: sig });
  server.close(() => process.exit(0));
};
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
