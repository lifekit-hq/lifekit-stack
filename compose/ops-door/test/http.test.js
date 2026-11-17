// The HTTP and spool layer against a real listener and a temp spool directory;
// GitHub is an in-memory fake and nothing leaves the loopback.
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";

import { createApp, fingerprintOf } from "../app.js";
import { createHandler, createReadiness, createSpool } from "../http.js";

let dir;
let server;
let base;
let issues;
let failGithub;

function fakeGithub() {
  return {
    listIssues: async () => {
      if (failGithub) throw new Error("boom");
      return [...issues].sort((a, b) => b.number - a.number);
    },
    createIssue: async ({ title, body, labels }) => {
      const n = issues.length + 1;
      const issue = { number: n, title, body, labels, state: "open", html_url: `https://github.test/o/r/issues/${n}` };
      issues.push(issue);
      return issue;
    },
    updateIssue: async (n, patch) => Object.assign(issues.find((i) => i.number === n), patch),
    comment: async () => {},
  };
}

async function start({ token = "t", ready = async () => true, maxBody } = {}) {
  const app = createApp({
    github: fakeGithub(),
    decide: createSpool(dir),
    repoUrl: "https://github.test/o/r",
    threshold: 1,
  });
  server = createServer(createHandler({ app, token, ready, maxBody }));
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
}

const post = (body) => fetch(`${base}/alerts`, { method: "POST", body: typeof body === "string" ? body : JSON.stringify(body) });
const firing = { alerts: [{ status: "firing", labels: { alertname: "a", job: "j" }, annotations: {}, startsAt: "2026-11-17T09:00:00Z" }] };

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "ops-door-"));
  issues = [];
  failGithub = false;
});

afterEach(async () => {
  await new Promise((resolve) => server.close(resolve));
  rmSync(dir, { recursive: true, force: true });
});

test("POST /alerts answers 200 with one receipt per alert", async () => {
  await start();
  const res = await post(firing);
  assert.equal(res.status, 200);
  const { receipts } = await res.json();
  assert.equal(receipts.length, 1);
  assert.equal(receipts[0].id, 1);
  assert.equal(receipts[0].state, "submitted");
});

test("no token: /alerts answers 503 and files nothing", async () => {
  await start({ token: "", ready: async () => false });
  assert.equal((await post(firing)).status, 503);
  assert.equal(issues.length, 0);
  assert.equal((await fetch(`${base}/ready`)).status, 503);
});

test("a body past the cap is refused with 413", async () => {
  await start({ maxBody: 100 });
  assert.equal((await post(JSON.stringify({ pad: "x".repeat(500) }))).status, 413);
  assert.equal(issues.length, 0);
});

test("invalid JSON is 400; unknown path is 404", async () => {
  await start();
  assert.equal((await post("{nope")).status, 400);
  assert.equal((await fetch(`${base}/nope`)).status, 404);
});

test("an upstream GitHub failure is 502 so the sender retries", async () => {
  await start();
  failGithub = true;
  assert.equal((await post(firing)).status, 502);
});

test("/ready follows the injected probe, /health and /metrics answer", async () => {
  await start({ ready: async () => true });
  assert.equal((await fetch(`${base}/ready`)).status, 200);
  assert.equal((await fetch(`${base}/health`)).status, 200);
  assert.match(await (await fetch(`${base}/metrics`)).text(), /ops_door_events_total\{outcome="firing"\} 0/);
});

test("past the threshold the spool holds exactly one request, and retries do not add or fail", async () => {
  await start();
  for (let i = 0; i < 5; i++) assert.equal((await post(firing)).status, 200);
  const files = readdirSync(dir);
  assert.deepEqual(files, [`ops-repeat-${fingerprintOf({ alertname: "a", job: "j" }).id}-1.json`]);
  const request = JSON.parse(readFileSync(join(dir, files[0]), "utf8"));
  assert.equal(request.kind, "call");
  assert.equal(request.tier, 2);
  assert.equal(request.issue, "https://github.test/o/r/issues/1");
});

test("the spool writer keeps the first file for a call id and still throws on other errors", async () => {
  const decide = createSpool(dir);
  await decide({ id: "c1", title: "first" });
  await decide({ id: "c1", title: "second" });
  assert.equal(JSON.parse(readFileSync(join(dir, "c1.json"), "utf8")).title, "first");
  await assert.rejects(createSpool(join(dir, "missing"))({ id: "c2" }), { code: "ENOENT" });
});

test("readiness: a failed probe is retried within the deploy wait, a success is cached", async () => {
  let t = 0;
  let probes = 0;
  let failing = true;
  const ready = createReadiness({
    token: "t",
    clock: () => t,
    probe: async () => {
      probes++;
      if (failing) throw new Error("dns");
    },
  });
  assert.equal(await ready(), false);
  assert.equal(await ready(), false);
  assert.equal(probes, 1, "failure cached briefly");
  failing = false;
  t = 10_000;
  assert.equal(await ready(), true, "recovers well inside a 40s wait");
  t = 30_000;
  assert.equal(await ready(), true);
  assert.equal(probes, 2, "success cached");
  assert.equal(await createReadiness({ token: "", probe: async () => {} })(), false);
});
