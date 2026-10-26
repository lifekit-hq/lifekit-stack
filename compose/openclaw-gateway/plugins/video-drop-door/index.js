// video-drop-door - Kit's Telegram drop door for videos, in front of the model.
//
// A message to Kit in the owner's chat whose whole text is one YouTube or
// Instagram link, or that carries one video file, is queued for evaluation by
// the video-eval skill's script and answered with its one-line receipt; Kit's
// model never sees it. The verdict comes back the way every kit item's does:
// the job-queue drain turn announces it to the owner's chat. Everything else
// goes to the model unchanged (docs/runbook.md, "Video drop door").
//
// No model and no shell decide anything here: `video.py door` matches the text
// strictly and `video.py door-file` checks the file, both run with execFile and
// the message as one argument. Every doubt falls through to the model: another
// agent, account, chat or a group, no owner chat configured, a script that is
// missing, slow or crashing.
//
// before_dispatch carries no media, so the message_received hook (emitted for
// the same message just before it, its handler run synchronously) notes the
// one video file's saved path for it.

import { execFile } from "node:child_process";
import path from "node:path";

const AGENT = "kit";
const ACCOUNT = "default"; // the account the job-queue drain announces on
const STATE = process.env.OPENCLAW_STATE_DIR || "/home/node/.openclaw";
const VIDEO_PY = path.join(STATE, "agents", AGENT, "workspace", "skills", "video-eval", "video.py");
const MEDIA_ROOT = path.join(STATE, "media", "inbound") + path.sep;
const RUN_MS = 45_000;
const STASH_MS = 120_000;
// A cheap look before spawning python; video.py door is the real matcher.
const MAYBE_LINK = /^https?:\/\/\S*(youtu|instagr)/i;

const stash = new Map();

function ownerChat() {
  return String(process.env.LIFEKIT_TELEGRAM_CHAT || "").trim();
}

function chatOf(ctx, isGroup) {
  const conv = String(ctx.conversationId ?? "").replace(/^telegram:/, "");
  if (conv) return conv;
  return isGroup ? "" : String(ctx.senderId ?? "");
}

function ours(ctx, isGroup) {
  const chat = ownerChat();
  return (
    Boolean(chat) &&
    !isGroup &&
    ctx.channelId === "telegram" &&
    ctx.accountId === ACCOUNT &&
    chatOf(ctx, isGroup) === chat
  );
}

function stashKey(ctx, messageId) {
  return `${ctx.accountId}|${messageId ?? ""}`;
}

function onMessageReceived(event, ctx) {
  const now = Date.now();
  for (const [k, v] of stash) if (now - v.at > STASH_MS) stash.delete(k);
  const messageId = event.messageId ?? ctx.messageId;
  if (!messageId || !ours(ctx, event.isGroup === true)) return;
  const media = Array.isArray(event.media) ? event.media : [];
  if (media.length !== 1) return;
  const [m] = media;
  const video = String(m.contentType ?? "").startsWith("video/") || m.kind === "video";
  const saved = typeof m.path === "string" ? path.resolve(m.path) : "";
  if (video && saved.startsWith(MEDIA_ROOT)) stash.set(stashKey(ctx, messageId), { path: saved, at: now });
}

function run(args) {
  return new Promise((resolve) => {
    execFile(
      "python3",
      [VIDEO_PY, ...args],
      { timeout: RUN_MS, maxBuffer: 64 * 1024, encoding: "utf8" },
      (err, stdout, stderr) => {
        const code = err ? err.code : 0;
        if (code === 0 && stdout.trim()) return resolve(stdout.trim());
        const said = String(stderr || "").split("\n")[0].trim();
        // A script failure it explains is the owner's answer; anything else
        // (no match, a crash, a timeout, a missing file) is the model's turn.
        resolve(code === 1 && said.startsWith("video failed:") ? said.slice(0, 300) : null);
      },
    );
  });
}

async function onBeforeDispatch(event, ctx) {
  if (!String(ctx.sessionKey ?? event.sessionKey ?? "").startsWith(`agent:${AGENT}:`)) return;
  if (!ours(ctx, event.isGroup === true)) return;
  const key = stashKey(ctx, ctx.messageId ?? event.messageId);
  const file = stash.get(key);
  stash.delete(key);
  let text = null;
  if (file) {
    text = await run(["door-file", file.path]);
  } else {
    // content is the text as sent; body is the agent's copy, maybe enveloped.
    const said = String(event.content ?? "").trim();
    if (MAYBE_LINK.test(said) && !/\s/.test(said)) text = await run(["door", said]);
  }
  return text ? { handled: true, text } : undefined;
}

export default {
  id: "video-drop-door",
  name: "Video drop door",
  description: "Queues a bare video link or video file sent to Kit, before the model",
  register(api) {
    api.on("message_received", onMessageReceived);
    api.on("before_dispatch", onBeforeDispatch, { timeoutMs: RUN_MS + 15_000 });
  },
};
