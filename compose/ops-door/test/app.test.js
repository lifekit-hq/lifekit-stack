// Lifecycle tests against an in-memory GitHub and a stub decide: nothing here
// touches the network.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { createApp, fingerprintOf, parseState } from "../app.js";

let issues;
let comments;
let decisions;
let app;
let clock;

function fakeGithub() {
  return {
    listIssues: async ({ page }) => (page === 1 ? [...issues].sort((a, b) => b.number - a.number) : []),
    createIssue: async ({ title, body, labels }) => {
      const issue = { number: issues.length + 1, title, body, labels, state: "open", html_url: `https://github.test/o/r/issues/${issues.length + 1}` };
      issues.push(issue);
      return issue;
    },
    updateIssue: async (n, patch) => Object.assign(issues.find((i) => i.number === n), patch),
    comment: async (n, body) => comments.push({ n, body }),
  };
}

beforeEach(() => {
  clock = "2026-11-17T10:00:00Z";
  issues = [];
  comments = [];
  decisions = [];
  app = createApp({
    github: fakeGithub(),
    decide: async (call) => decisions.push(call),
    repoUrl: "https://github.test/o/r",
    threshold: 3,
    now: () => clock,
  });
});

const alert = (over = {}) => ({
  status: "firing",
  labels: { alertname: "devclaw is down", severity: "critical", grafana_folder: "box", job: "devclaw" },
  annotations: { summary: "devclaw is down", description: "no scrape for 5m" },
  startsAt: "2026-11-17T09:00:00Z",
  endsAt: "0001-01-01T00:00:00Z",
  ...over,
});
const send = (a) => app.handlePayload({ alerts: [a] });

test("fingerprint is alert name plus identifying labels, without routing noise", () => {
  const a = fingerprintOf({ alertname: "x", severity: "critical", grafana_folder: "f", job: "j", __x: "1" });
  const b = fingerprintOf({ alertname: "x", severity: "warning", job: "j" });
  assert.equal(a.id, b.id);
  assert.equal(a.text, 'x{job="j"}');
  assert.notEqual(a.id, fingerprintOf({ alertname: "x", job: "other" }).id);
});

test("first firing opens one issue with the envelope and returns the receipt", async () => {
  const { receipts } = await send(alert());
  assert.equal(issues.length, 1);
  assert.deepEqual(issues[0].labels, ["ops"]);
  for (const line of ["origin: alertmanager", "owner: lifekit-stack", "class: ops", "fingerprint:", "acceptance:", "thread:"]) {
    assert.ok(issues[0].body.includes(line), line);
  }
  assert.deepEqual(receipts[0], { id: 1, url: "https://github.test/o/r/issues/1", state: "submitted", fingerprint: receipts[0].fingerprint, count: 1 });
  assert.equal(parseState(issues[0].body).count, 1);
});

test("a repeat adds a count and the notice time as latest occurrence, though startsAt never moves", async () => {
  const first = await send(alert());
  clock = "2026-11-17T14:00:00Z";
  const second = await send(alert());
  assert.equal(issues.length, 1);
  const state = parseState(issues[0].body);
  assert.equal(state.count, 2);
  assert.equal(state.first, "2026-11-17T09:00:00Z");
  assert.equal(state.latest, "2026-11-17T14:00:00Z");
  assert.match(comments[0].body, /latest occurrence 2026-11-17T14:00:00Z/);
  assert.equal(first.receipts[0].id, second.receipts[0].id);
  assert.equal(second.receipts[0].count, 2);
  assert.equal(comments.length, 1);
  assert.deepEqual(decisions, []);
});

test("a resolve comments and does not close", async () => {
  await send(alert());
  const { receipts } = await send(alert({ status: "resolved", endsAt: "2026-11-17T09:45:00Z" }));
  assert.equal(issues[0].state, "open");
  assert.equal(comments.length, 1);
  assert.match(comments[0].body, /Resolved at 2026-11-17T09:45:00Z/);
  assert.equal(receipts[0].id, 1);
});

test("a resolve with no open issue files nothing", async () => {
  const { receipts } = await send(alert({ status: "resolved" }));
  assert.equal(issues.length, 0);
  assert.equal(receipts[0].state, "ignored");
});

test("a firing after close opens a second issue that links the first", async () => {
  await send(alert());
  issues[0].state = "closed";
  const { receipts } = await send(alert());
  assert.equal(issues.length, 2);
  assert.equal(receipts[0].id, 2);
  assert.match(issues[1].body, /Fires again after #1 was closed/);
  assert.equal(parseState(issues[1].body).count, 1);
  assert.equal(parseState(issues[1].body).reopens, 1);
});

test("passing the threshold files exactly one Tier 2 call, instead of another comment, and never a second", async () => {
  for (let i = 0; i < 3; i++) await send(alert());
  assert.equal(decisions.length, 0);
  const commentsBefore = comments.length;
  await send(alert());
  assert.equal(decisions.length, 1);
  assert.equal(decisions[0].id, `ops-repeat-${fingerprintOf(alert().labels).id}-1`);
  assert.equal(decisions[0].issue, "https://github.test/o/r/issues/1");
  assert.equal(parseState(issues[0].body).escalated, decisions[0].id);
  assert.equal(comments.length, commentsBefore + 1, "one note that the call was filed");
  await send(alert());
  await send(alert());
  assert.equal(decisions.length, 1, "no second call");
  assert.equal(comments.length, commentsBefore + 1, "no further comments once escalated");
  assert.equal(parseState(issues[0].body).count, 6);
});

test("a GitHub failure surfaces so the sender retries", async () => {
  app = createApp({
    github: { ...fakeGithub(), listIssues: async () => { throw new Error("boom"); } },
    decide: async () => {},
  });
  await assert.rejects(send(alert()), /boom/);
  assert.equal(app.counters.errors, 1);
});

test("state markers survive hostile alert text", async () => {
  await send(alert({ annotations: { summary: "x --> <script>", description: "-->" } }));
  assert.equal(parseState(issues[0].body).count, 1);
});
