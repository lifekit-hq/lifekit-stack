import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";

import { createApp, createMetrics } from "../app.js";
import { MAX_BODY, createHandler, createLimiter, createReadiness, sameSecret } from "../http.js";
import { createSessions } from "../prompt.js";
import { GOOD_REPLY, PAGE, TURN, fakeGateway } from "./fixtures.js";

const TOKEN = "panel-token-aaaaaaaaaaaaaaaa";
const ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop";

async function door({ token = TOKEN, origin = ORIGIN, perMinute = 100, gateway = fakeGateway(), probe = async () => {} } = {}) {
  const logs = [];
  const log = (level, msg, fields) => logs.push({ level, msg, ...fields });
  const app = createApp({ gateway, sessions: createSessions(), metrics: createMetrics(), log });
  const ready = createReadiness({ configured: true, probe, log });
  const server = createServer(createHandler({ app, token, origin, ready, limiter: createLimiter({ perMinute }), log }));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const close = () => new Promise((r) => server.close(r));
  return { base, close, logs, gateway };
}

const post = (base, body, headers = {}) =>
  fetch(`${base}/v1/turn`, {
    method: "POST",
    headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });

test("a valid turn is answered 200 with the gated reply and CORS for the extension only", async () => {
  const d = await door();
  const res = await post(d.base, TURN, { origin: ORIGIN });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("access-control-allow-origin"), ORIGIN);
  assert.equal(res.headers.get("cache-control"), "no-store");
  const body = await res.json();
  assert.equal(body.fallback, false);
  assert.equal(body.reply.text, GOOD_REPLY.text);
  await d.close();
});

test("no token, a wrong token and a malformed header are each 401", async () => {
  const d = await door();
  for (const authorization of [undefined, "Bearer wrong", "Basic abc", `bearer ${TOKEN}`, "Bearer "]) {
    const headers = authorization === undefined ? { authorization: "" } : { authorization };
    const res = await fetch(`${d.base}/v1/turn`, { method: "POST", headers: { ...headers, "content-type": "application/json" }, body: JSON.stringify(TURN) });
    assert.equal(res.status, 401, String(authorization));
    assert.equal(res.headers.get("www-authenticate"), "Bearer");
  }
  assert.equal(d.gateway.calls.length, 0);
  await d.close();
});

test("a wrong Origin is 403 even with the right token, and gets no CORS grant", async () => {
  const d = await door();
  const res = await post(d.base, TURN, { origin: "https://evil.example" });
  assert.equal(res.status, 403);
  assert.equal(res.headers.get("access-control-allow-origin"), null);
  assert.equal(d.gateway.calls.length, 0);
  await d.close();
});

test("with no extension origin configured every browser origin is refused", async () => {
  const d = await door({ origin: "" });
  assert.equal((await post(d.base, TURN, { origin: ORIGIN })).status, 403);
  assert.equal((await post(d.base, TURN)).status, 200, "a non-browser caller with the token still works");
  await d.close();
});

test("the preflight answers the extension and refuses everyone else", async () => {
  const d = await door();
  const ok = await fetch(`${d.base}/v1/turn`, { method: "OPTIONS", headers: { origin: ORIGIN, "access-control-request-method": "POST" } });
  assert.equal(ok.status, 204);
  assert.equal(ok.headers.get("access-control-allow-origin"), ORIGIN);
  assert.match(ok.headers.get("access-control-allow-headers"), /authorization/);
  const bad = await fetch(`${d.base}/v1/turn`, { method: "OPTIONS", headers: { origin: "https://evil.example" } });
  assert.equal(bad.status, 403);
  await d.close();
});

test("a body over 64 KiB is 413 and never parsed", async () => {
  const d = await door();
  const res = await post(d.base, JSON.stringify({ pad: "x".repeat(MAX_BODY + 10) }));
  assert.equal(res.status, 413);
  assert.equal(d.gateway.calls.length, 0);
  assert.ok(d.logs.some((l) => l.outcome === "too_large"));
  await d.close();
});

test("bad json is 400, a wrong content type 415, a wrong method 405, an unknown path 404", async () => {
  const d = await door();
  assert.equal((await post(d.base, "{nope")).status, 400);
  assert.equal((await post(d.base, "{}", { "content-type": "text/plain" })).status, 415);
  assert.equal((await fetch(`${d.base}/v1/turn`, { headers: { authorization: `Bearer ${TOKEN}` } })).status, 405);
  assert.equal((await fetch(`${d.base}/v1/chat/completions`)).status, 404);
  assert.equal((await post(d.base, { ...TURN, model: "openclaw/kit" })).status, 400);
  await d.close();
});

test("the rate limit is a 429 with Retry-After, counted after auth", async () => {
  const d = await door({ perMinute: 2 });
  assert.equal((await post(d.base, TURN)).status, 200);
  assert.equal((await post(d.base, TURN)).status, 200);
  const limited = await post(d.base, TURN);
  assert.equal(limited.status, 429);
  assert.ok(Number(limited.headers.get("retry-after")) >= 1);
  const unauth = await fetch(`${d.base}/v1/turn`, { method: "POST", body: "{}" });
  assert.equal(unauth.status, 401, "unauthenticated floods neither spend nor hit the owner's budget");
  await d.close();
});

test("the limiter opens a new window after a minute", () => {
  let t = 0;
  const take = createLimiter({ perMinute: 1, clock: () => t });
  assert.equal(take(), 0);
  assert.ok(take() > 0);
  t = 60_001;
  assert.equal(take(), 0);
});

test("without a configured token /v1/turn is 503", async () => {
  const d = await door({ token: "" });
  assert.equal((await post(d.base, TURN)).status, 503);
  await d.close();
});

test("an upstream failure is a 502 with no reply body", async () => {
  const { UpstreamError } = await import("../gateway.js");
  const d = await door({ gateway: fakeGateway(new UpstreamError("status", 403)) });
  const res = await post(d.base, TURN);
  assert.equal(res.status, 502);
  assert.deepEqual(await res.json(), { error: "upstream", reason: "status" });
  await d.close();
});

test("/health is always 200, /ready follows the gateway probe, /metrics is text", async () => {
  let up = true;
  const d = await door({ probe: async () => { if (!up) throw new Error("down"); } });
  assert.equal((await fetch(`${d.base}/health`)).status, 200);
  assert.equal((await fetch(`${d.base}/ready`)).status, 200);
  const metrics = await fetch(`${d.base}/metrics`);
  assert.match(metrics.headers.get("content-type"), /^text\/plain/);
  assert.match(await metrics.text(), /study_door_gateway_seconds_count/);
  await d.close();
  up = false;
  const down = await door({ probe: async () => { throw new Error("down"); } });
  assert.equal((await fetch(`${down.base}/ready`)).status, 503);
  await down.close();
});

test("a probe carrying a traceparent logs its trace id", async () => {
  const d = await door();
  const trace = "0af7651916cd43dd8448eb211c80319c";
  await fetch(`${d.base}/health`, { headers: { traceparent: `00-${trace}-b7ad6b7169203331-01` } });
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(d.logs.some((l) => l.msg === "probe" && l.trace_id === trace));
  await d.close();
});

test("HTTP logs never carry the token or the page", async () => {
  const d = await door();
  await post(d.base, { ...TURN, page: { ...PAGE, code: "HTTP_CODE_MARKER_44" } }, { origin: "https://evil.example" });
  await post(d.base, TURN, { authorization: "Bearer wrong-token-value" });
  await post(d.base, TURN);
  const all = JSON.stringify(d.logs);
  for (const m of [TOKEN, "wrong-token-value", "HTTP_CODE_MARKER_44", "Bearer"]) assert.equal(all.includes(m), false, m);
  await d.close();
});

test("secrets compare in constant time over any length", () => {
  assert.equal(sameSecret("a", "a"), true);
  assert.equal(sameSecret("a", "ab"), false);
  assert.equal(sameSecret("", "x"), false);
});
