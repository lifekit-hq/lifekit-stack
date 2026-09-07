/**
 * server.test.js — drives the relay's real HTTP surface.
 *
 * render.test.js proves the rendering rules; this proves they reach Telegram:
 * the endpoint exists, rejects a bad envelope before sending anything, and hands
 * the Bot API the rendered HTML with `parse_mode: HTML`.
 */
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { request } from "node:http";
import { after, before, beforeEach, describe, it } from "node:test";

process.env.TELEGRAM_BOT_TOKEN = "test-token";
process.env.LIFEKIT_TELEGRAM_CHAT = "-1001234567890";
process.env.NOTIFY_RELAY_PORT = "0"; // ephemeral: never collide with a real relay

const { server } = await import("./server.js");

// The relay calls the Bot API through global fetch, so the stub has to let the
// test's own client requests through untouched.
const realFetch = globalThis.fetch;
let sent; // the sendMessage payloads the relay tried to deliver
let telegramReply;

globalThis.fetch = async (url, options) => {
  if (!String(url).startsWith("https://api.telegram.org/")) {
    return realFetch(url, options);
  }
  sent.push(JSON.parse(options.body));
  return telegramReply();
};

const ok = () =>
  new Response(JSON.stringify({ ok: true, result: { message_id: 7 } }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

let base;

async function post(path, body) {
  const res = await realFetch(`${base}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  return { status: res.status, body: await res.json() };
}

// Sends the body as separate, deliberately delayed writes so the server sees
// more than one chunk — the only way to exercise a split multi-byte character.
function postChunked(path, chunks) {
  return new Promise((resolve, reject) => {
    const req = request(
      `${base}${path}`,
      { method: "POST", headers: { "content-type": "application/json" } },
      (res) => {
        let raw = "";
        res.setEncoding("utf8");
        res.on("data", (d) => (raw += d));
        res.on("end", () =>
          resolve({ status: res.statusCode, body: JSON.parse(raw) }),
        );
      },
    );
    req.on("error", reject);
    chunks.forEach((chunk, i) => setTimeout(() => req.write(chunk), i * 10));
    setTimeout(() => req.end(), chunks.length * 10);
  });
}

before(async () => {
  if (!server.listening) {
    await new Promise((resolve) => server.once("listening", resolve));
  }
  base = `http://127.0.0.1:${server.address().port}`;
});

after(() => {
  globalThis.fetch = realFetch;
  server.close();
});

beforeEach(() => {
  sent = [];
  telegramReply = ok;
});

const envelope = {
  level: "act",
  source: "devclaw",
  subject: "issue-819",
  headline: "needs a decision",
  action: "decide(issue-819)",
};

describe("POST /notify", () => {
  it("renders the envelope and sends it as HTML", async () => {
    const res = await post("/notify", { ...envelope, detail: "boom & bust" });

    assert.equal(res.status, 200);
    assert.deepEqual(res.body, { ok: true });
    assert.equal(sent.length, 1);
    assert.equal(sent[0].parse_mode, "HTML");
    assert.equal(sent[0].chat_id, "-1001234567890");
    assert.ok(
      sent[0].text.startsWith("🔴 <b>devclaw</b> · <b>issue-819</b> —"),
      sent[0].text,
    );
    assert.ok(sent[0].text.includes("<blockquote expandable>boom &amp; bust"));
    assert.ok(sent[0].text.endsWith("→ <code>decide(issue-819)</code>"));
  });

  it("drops the action for a good envelope on the wire, not just in render", async () => {
    const res = await post("/notify", {
      ...envelope,
      level: "good",
      headline: "recovered",
      action: "docker ps on the box",
    });

    assert.equal(res.status, 200);
    assert.ok(!sent[0].text.includes("docker ps"), sent[0].text);
  });

  it("rejects an invalid envelope without calling Telegram", async () => {
    const res = await post("/notify", { level: "urgent", source: "devclaw" });

    assert.equal(res.status, 400);
    assert.equal(res.body.error, "invalid envelope");
    assert.ok(res.body.details.length >= 2, JSON.stringify(res.body));
    assert.deepEqual(sent, []);
  });

  it("rejects a body that is not JSON", async () => {
    const res = await post("/notify", "{not json");

    assert.equal(res.status, 400);
    assert.equal(res.body.error, "invalid json body");
    assert.deepEqual(sent, []);
  });

  it("keeps an emoji whole when the body arrives split mid-character", async () => {
    const raw = Buffer.from(
      JSON.stringify({ ...envelope, body: "deploy 🔥 burned" }),
    );
    const split = raw.indexOf(Buffer.from("🔥")) + 2; // mid-emoji, mid-UTF-8
    const res = await postChunked("/notify", [
      raw.subarray(0, split),
      raw.subarray(split),
    ]);

    assert.equal(res.status, 200);
    assert.ok(sent[0].text.includes("deploy 🔥 burned"), sent[0].text);
  });

  it("takes the query string devclaw's notify_url appends", async () => {
    const res = await post("/notify?src=goal-layer", envelope);
    assert.equal(res.status, 200);
    assert.equal(sent.length, 1);
  });

  // The producer has to get the status back, so the over-long body is drained
  // rather than abandoned mid-upload — abandoning it resets the socket and the
  // caller sees a connection error instead of being told what it did wrong.
  it("refuses a body too large to be an envelope, and still answers", async () => {
    const res = await post("/notify", {
      ...envelope,
      detail: "d".repeat(2 * 1024 * 1024),
    });

    assert.equal(res.status, 413);
    assert.equal(res.body.error, "body too large");
    assert.deepEqual(sent, []);
  });

  it("answers 502 when Telegram refuses the send", async () => {
    telegramReply = () =>
      new Response(JSON.stringify({ ok: false, description: "chat not found" }), {
        status: 400,
        headers: { "content-type": "application/json" },
      });

    const res = await post("/notify", envelope);

    assert.equal(res.status, 502);
    assert.match(res.body.error, /chat not found/);
  });
});

describe("the surface around it", () => {
  it("keeps /health answering", async () => {
    const res = await realFetch(`${base}/health`);
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: true, name: "notify-relay" });
  });

  // The goal layer's string is the message, but it is still producer text going
  // out as HTML: escaped, so a `<` in it renders instead of failing the send.
  it("escapes the legacy /text payload and sends it as HTML", async () => {
    const res = await post("/text", { text: "5 < 6 & unescaped" });

    assert.equal(res.status, 200);
    assert.equal(sent[0].text, "5 &lt; 6 &amp; unescaped");
    assert.equal(sent[0].parse_mode, "HTML");
  });

  it("clips a /text payload too long for Telegram", async () => {
    const res = await post("/text", { text: "x".repeat(9000) });

    assert.equal(res.status, 200);
    assert.ok(sent[0].text.length <= 3500, `sent ${sent[0].text.length} chars`);
    assert.ok(sent[0].text.endsWith("…"), sent[0].text.slice(-20));
  });

  it("rejects a /text payload with no text", async () => {
    const res = await post("/text", { text: "   " });

    assert.equal(res.status, 400);
    assert.deepEqual(sent, []);
  });

  it("renders the legacy /devclaw row through the envelope", async () => {
    const res = await post("/devclaw", { status: "done", task_id: "abcdef123" });

    assert.equal(res.status, 200);
    assert.equal(sent[0].parse_mode, "HTML");
    assert.equal(
      sent[0].text,
      "🟢 <b>devclaw</b> · <b>task abcdef12…</b> — done",
    );
  });

  // The row is producer-controlled text going out as HTML, so the escaping the
  // renderer does has to hold on this path too.
  it("escapes a task row's error on the way to Telegram", async () => {
    const res = await post("/devclaw", {
      status: "failed",
      task_id: "abcdef123",
      error: "<script>alert(1)</script> & more",
    });

    assert.equal(res.status, 200);
    assert.ok(!sent[0].text.includes("<script>"), sent[0].text);
    assert.ok(sent[0].text.includes("&lt;script&gt;alert(1)&lt;/script&gt; &amp; more"));
  });

  // Callers of this endpoint predate the contract, so its sub-paths stay routed.
  it("keeps routing /devclaw sub-paths", async () => {
    const res = await post("/devclaw/callback", {
      status: "failed",
      task_id: "abcdef123",
      error: "the runner vanished",
    });

    assert.equal(res.status, 200);
    assert.equal(
      sent[0].text,
      "🔴 <b>devclaw</b> · <b>task abcdef12…</b> — failed\n\nthe runner vanished",
    );
  });

  it("404s an unknown path", async () => {
    const res = await post("/nope", {});
    assert.equal(res.status, 404);
  });
});

// CI builds no images (the runner is the production VPS), so a module added
// beside server.js but not COPYied crashes the container at deploy and nothing
// before that catches it. This suite is the deployment-facing one, so the check
// lives here.
describe("the image", () => {
  it("copies every runtime module into /app", async () => {
    const dir = new URL(".", import.meta.url);
    const dockerfile = await readFile(new URL("Dockerfile", dir), "utf8");
    // The sources of every COPY: each line is `COPY <src>... <dest>`, so the
    // last token is the destination. Matching the filename anywhere in the file
    // would be satisfied by a comment — or, for server.js, by the CMD.
    const copied = new Set(
      dockerfile
        .split("\n")
        .filter((line) => line.startsWith("COPY "))
        .flatMap((line) => line.slice(5).trim().split(/\s+/).slice(0, -1)),
    );
    const modules = (await readdir(dir)).filter(
      (name) => name.endsWith(".js") && !name.endsWith(".test.js"),
    );

    assert.ok(modules.length >= 3, `found only ${modules.join(", ")}`);
    for (const name of modules) {
      assert.ok(copied.has(name), `${name} is not COPYied into the image`);
    }
  });
});
