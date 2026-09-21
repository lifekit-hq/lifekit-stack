// Route tests: the relay's outbound HTTP is injected, so every assertion is
// on the request object the relay hands to its transport. Nothing here
// reaches the network.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { after, before, beforeEach, test } from "node:test";

import { createApp } from "../app.js";
import { MAX_MSG_CHARS, render } from "../render.js";

const TOKEN = "123456:test-token";
const CHAT = "424242";

let calls = [];
let server;
let origin;

const transport = async (request) => {
  calls.push(request);
  return {
    ok: true,
    status: 200,
    json: async () => ({ ok: true, result: { message_id: calls.length } }),
  };
};

before(async () => {
  const app = createApp({ token: TOKEN, chat: CHAT, transport, log: () => {} });
  server = createServer(app.requestListener);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
});

after(() => new Promise((resolve) => server.close(resolve)));

beforeEach(() => {
  calls = [];
});

async function post(path, body) {
  const res = await fetch(`${origin}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  return { status: res.status, body: await res.json() };
}

function sent(call) {
  assert.equal(call.method, "POST");
  assert.equal(call.url, `https://api.telegram.org/bot${TOKEN}/sendMessage`);
  assert.equal(call.headers["content-type"], "application/json");
  return JSON.parse(call.body);
}

const envelope = {
  level: "wait",
  source: "devclaw",
  subject: "issue-819",
  headline: "needs a decision",
  body: "Which hosts count as private?",
  action: "decide(issue-819, artifactory)",
};

test("POST /notify: a valid envelope makes exactly one HTML sendMessage call", async () => {
  const { status, body } = await post("/notify", envelope);
  assert.equal(status, 200);
  assert.deepEqual(body, { ok: true });
  assert.equal(calls.length, 1);
  const payload = sent(calls[0]);
  assert.equal(payload.parse_mode, "HTML");
  assert.equal(payload.chat_id, CHAT);
  assert.equal(payload.text, render(envelope));
  assert.equal(
    payload.text,
    [
      "🟡 <b>devclaw</b> · <b>issue-819</b> — needs a decision",
      "Which hosts count as private?",
      "→ <code>decide(issue-819, artifactory)</code>",
    ].join("\n"),
  );
});

for (const field of ["level", "source", "subject", "headline"]) {
  test(`POST /notify: missing '${field}' is a 400 and zero outbound calls`, async () => {
    const { [field]: _dropped, ...partial } = envelope;
    const { status, body } = await post("/notify", partial);
    assert.equal(status, 400);
    assert.equal(body.error, `missing '${field}'`);
    assert.equal(calls.length, 0);
  });
}

test("POST /notify: an unknown level is a 400 and zero outbound calls", async () => {
  const { status, body } = await post("/notify", { ...envelope, level: "urgent" });
  assert.equal(status, 400);
  assert.match(body.error, /unknown level 'urgent'/);
  assert.equal(calls.length, 0);
});

for (const [field, overrides] of [
  ["action", { action: "a".repeat(5000) }],
  ["headline", { headline: "h".repeat(5000) }],
]) {
  test(`POST /notify: an over-long ${field} is a 400 and zero outbound calls`, async () => {
    const { status, body } = await post("/notify", { ...envelope, ...overrides });
    assert.equal(status, 400);
    assert.match(body.error, new RegExp(`^'${field}' is 5000 characters escaped; limit \\d+$`));
    assert.equal(calls.length, 0);
  });
}

test("POST /notify: malformed JSON is a 400 and zero outbound calls", async () => {
  const { status } = await post("/notify", "{not json");
  assert.equal(status, 400);
  assert.equal(calls.length, 0);
});

test("POST /devclaw: task row with raw HTML characters is escaped and sent once", async () => {
  const { status } = await post("/devclaw", {
    task_id: "0123456789abcdef",
    kind: "task",
    status: "failed",
    goal: "handle <input> & <output>",
    error: "TypeError: expected <str> & got <int>",
  });
  assert.equal(status, 200);
  assert.equal(calls.length, 1);
  const payload = sent(calls[0]);
  assert.equal(payload.parse_mode, "HTML");
  assert.equal(
    payload.text,
    [
      "🔴 <b>devclaw</b> · <b>task 01234567</b> — failed",
      "handle &lt;input&gt; &amp; &lt;output&gt;",
      "<blockquote expandable>TypeError: expected &lt;str&gt; &amp; got &lt;int&gt;</blockquote>",
    ].join("\n"),
  );
  assert.doesNotMatch(payload.text, /<str>|<input>|<output>|<int>/);
});

test("POST /devclaw accepts a sub-path and query string, as before the renderer", async () => {
  const { status } = await post("/devclaw/callback?task=1", { task_id: "abc", status: "running" });
  assert.equal(status, 200);
  assert.equal(calls.length, 1);
  assert.equal(sent(calls[0]).text, "▪️ <b>devclaw</b> · <b>task abc</b> — running");
});

test("POST /devclaw: done row renders as good", async () => {
  await post("/devclaw", { task_id: "abc", status: "done", goal: "ship" });
  assert.equal(calls.length, 1);
  assert.equal(sent(calls[0]).text, "🟢 <b>devclaw</b> · <b>task abc</b> — done\nship");
});

test("POST /text: free text with raw HTML characters is escaped and sent once", async () => {
  const { status } = await post("/text", {
    text: "🟡 goal paused: <workspace> & friends\nsee a > b for details",
  });
  assert.equal(status, 200);
  assert.equal(calls.length, 1);
  const payload = sent(calls[0]);
  assert.equal(payload.parse_mode, "HTML");
  assert.equal(
    payload.text,
    "▪️ <b>devclaw</b> · <b>goal</b> — 🟡 goal paused: &lt;workspace&gt; &amp; friends\nsee a &gt; b for details",
  );
});

test("POST /text: a single-line payload over the cap is delivered, not refused", async () => {
  const { status } = await post("/text", { text: `goal paused: ${"detail ".repeat(900)}` });
  assert.equal(status, 200);
  assert.equal(calls.length, 1);
  const payload = sent(calls[0]);
  assert.equal(payload.parse_mode, "HTML");
  assert.ok(
    payload.text.length <= MAX_MSG_CHARS,
    `outbound message is ${payload.text.length} chars, over the ${MAX_MSG_CHARS} cap`,
  );
  assert.ok(payload.text.startsWith("▪️ <b>devclaw</b> · <b>goal</b> — goal paused: detail"));
  assert.ok(payload.text.endsWith("…"));
});

test("POST /text: empty text is a 400 and zero outbound calls", async () => {
  const { status } = await post("/text", { text: "  " });
  assert.equal(status, 400);
  assert.equal(calls.length, 0);
});

test("unknown routes are 404 with zero outbound calls", async () => {
  const res = await fetch(`${origin}/nope`, { method: "POST", body: "{}" });
  assert.equal(res.status, 404);
  assert.equal(calls.length, 0);
});

test("GET /health needs no transport; /metrics counts routes by fixed label", async () => {
  const health = await fetch(`${origin}/health`);
  assert.equal(health.status, 200);
  const metrics = await (await fetch(`${origin}/metrics`)).text();
  assert.match(metrics, /notify_relay_requests_total\{route="\/notify",code="200"\} 1/);
  assert.match(metrics, /notify_relay_requests_total\{route="other",code="404"\} 1/);
  assert.match(metrics, /notify_relay_telegram_sends_total\{outcome="ok"\} \d+/);
  assert.equal(calls.length, 0);
});

test("GET /ready probes getMe through the transport and caches the answer", async () => {
  const first = await fetch(`${origin}/ready`);
  assert.equal(first.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "GET");
  assert.equal(calls[0].url, `https://api.telegram.org/bot${TOKEN}/getMe`);
  await fetch(`${origin}/ready`);
  assert.equal(calls.length, 1);
});

test("a Telegram error is a 502 and counts as a failed send", async () => {
  const failing = async () => ({
    ok: false,
    status: 400,
    json: async () => ({ ok: false, description: "Bad Request: can't parse entities" }),
  });
  const app = createApp({ token: TOKEN, chat: CHAT, transport: failing, log: () => {} });
  const srv = createServer(app.requestListener);
  await new Promise((resolve) => srv.listen(0, "127.0.0.1", resolve));
  try {
    const res = await fetch(`http://127.0.0.1:${srv.address().port}/notify`, {
      method: "POST",
      body: JSON.stringify(envelope),
    });
    assert.equal(res.status, 502);
    assert.match((await res.json()).error, /Telegram API 400/);
  } finally {
    await new Promise((resolve) => srv.close(resolve));
  }
});
