// Decision contract tests: the builder is pure; the route tests inject the
// transport, so nothing here reaches the network.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { after, before, beforeEach, test } from "node:test";

import { createApp } from "../app.js";
import {
  buildDecision,
  buildLink,
  buildTitle,
  DECISION_SCHEMA,
  NO_ACTION_BODY,
  renderDecision,
  validateDecisionInput,
} from "../decision.js";

const LINKS = { boardBaseUrl: "https://box.example.ts.net:4387", dashboardBaseUrl: "https://box.example.ts.net/" };

const input = {
  kind: "decision",
  project: "Billing dashboard",
  ask: "Which export format should ship first?",
  decision_id: "export-format",
  options: [{ label: "CSV" }, { label: "JSON", recommended: true }, { label: "XML" }],
  free_text: undefined,
};

test("schema file equals the code's schema", () => {
  const file = JSON.parse(
    readFileSync(new URL("../../../docs/decision-contract.schema.json", import.meta.url), "utf8"),
  );
  assert.deepEqual(file, JSON.parse(JSON.stringify(DECISION_SCHEMA)));
});

test("payload carries every contract field", () => {
  const d = buildDecision({ ...input, free_text_allowed: true }, LINKS);
  assert.deepEqual(Object.keys(d).sort(), [...DECISION_SCHEMA.required].sort());
  assert.equal(d.free_text_allowed, true);
  assert.equal(buildDecision(input, LINKS).free_text_allowed, false);
});

test("title: severity word first, then project and ask", () => {
  assert.equal(buildTitle("decision", "Billing dashboard", "Which format?"), "Decision: Billing dashboard — Which format?");
  for (const [kind, word] of [["blocked", "Blocked"], ["done", "Done"], ["info", "FYI"]]) {
    assert.ok(buildDecision({ ...input, kind }, LINKS).title.startsWith(`${word}: `));
  }
});

for (const [name, patch] of [
  ["a task id", { ask: "Resolve issue-819 first?" }],
  ["a branch name", { ask: "Merge fm/cdp-notify-contract now?" }],
  ["a status prefix", { ask: "working: pick a format" }],
  ["a status prefix in project", { project: "needs-decision - Billing" }],
  ["a hash", { ask: "Revert 7f65f14e?" }],
]) {
  test(`title rules reject ${name}`, () => {
    const problems = validateDecisionInput({ ...input, ...patch });
    assert.ok(problems.some((p) => /plain words/.test(p)), problems.join("; "));
  });
}

test("plain input validates", () => {
  assert.deepEqual(validateDecisionInput(input), []);
});

test("recommended option is first and marked; others keep order", () => {
  const d = buildDecision(input, LINKS);
  assert.deepEqual(d.options.map((o) => o.label), ["JSON", "CSV", "XML"]);
  assert.deepEqual(d.options.map((o) => o.recommended), [true, false, false]);
  assert.equal(d.body, "1. JSON (recommended)\n2. CSV\n3. XML");
});

test("no options means 'no action needed'", () => {
  const d = buildDecision({ ...input, kind: "done", options: [] }, LINKS);
  assert.equal(d.body, NO_ACTION_BODY);
  assert.deepEqual(d.options, []);
});

test("more than one recommended option is rejected", () => {
  const options = [{ label: "a", recommended: true }, { label: "b", recommended: true }];
  assert.ok(validateDecisionInput({ ...input, options }).some((p) => /at most one/.test(p)));
});

test("link: the board when one exists, else the dashboard deep link", () => {
  assert.equal(buildLink("export-format", "abc123", LINKS), "https://box.example.ts.net:4387/session/abc123");
  assert.equal(buildLink("export-format", undefined, LINKS), "https://box.example.ts.net/decisions/export-format");
  // no board base configured → falls back to the deep link even with a board id
  assert.equal(buildLink("export-format", "abc123", { dashboardBaseUrl: "https://d.example" }), "https://d.example/decisions/export-format");
  assert.equal(buildLink("export-format", undefined, {}), "/decisions/export-format");
});

test("rendered message escapes text and carries the link", () => {
  const html = renderDecision(buildDecision({ ...input, ask: "Use <b> tags & more?" }, LINKS));
  assert.ok(html.startsWith("<b>Decision: Billing dashboard — Use &lt;b&gt; tags &amp; more?</b>"));
  assert.ok(html.includes('<a href="https://box.example.ts.net/decisions/export-format">'));
});

// ---- routes: one notification per decision id ------------------------------

let calls = [];
let server;
let origin;
let failEdit = false;

const transport = async (request) => {
  calls.push(request);
  const method = request.url.split("/").pop();
  if (method === "editMessageText" && failEdit) {
    return { ok: false, status: 400, json: async () => ({ ok: false, description: "message to edit not found" }) };
  }
  return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 100 + calls.length } }) };
};

before(async () => {
  const app = createApp({ token: "1:t", chat: "42", transport, log: () => {}, links: LINKS });
  server = createServer(app.requestListener);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
});
after(() => new Promise((resolve) => server.close(resolve)));
beforeEach(() => {
  calls = [];
  failEdit = false;
});

async function post(path, body) {
  const res = await fetch(`${origin}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return { status: res.status, body: await res.json() };
}
const verb = (call) => call.url.split("/").pop();

test("POST /decision sends one message; the same id edits it in place; clear deletes it", async () => {
  const id = { ...input, decision_id: "flow-1" };
  const first = await post("/decision", id);
  assert.equal(first.status, 200);
  assert.equal(first.body.replaced, false);
  assert.equal(first.body.decision.link, "https://box.example.ts.net/decisions/flow-1");
  assert.deepEqual(calls.map(verb), ["sendMessage"]);
  const messageId = 101;

  const changed = await post("/decision", { ...id, ask: "Which format should ship now?" });
  assert.equal(changed.body.replaced, true);
  assert.deepEqual(calls.map(verb), ["sendMessage", "editMessageText"]);
  const edit = JSON.parse(calls[1].body);
  assert.equal(edit.message_id, messageId);
  assert.match(edit.text, /Which format should ship now\?/);

  const cleared = await post("/decision/clear", { decision_id: "flow-1" });
  assert.deepEqual(cleared.body, { ok: true, cleared: true });
  assert.deepEqual(calls.map(verb), ["sendMessage", "editMessageText", "deleteMessage"]);
  assert.equal(JSON.parse(calls[2].body).message_id, messageId);

  // after the clear the id is new again
  await post("/decision", id);
  assert.equal(verb(calls[3]), "sendMessage");
});

test("different decision ids get different messages", async () => {
  await post("/decision", { ...input, decision_id: "a" });
  await post("/decision", { ...input, decision_id: "b" });
  assert.deepEqual(calls.map(verb), ["sendMessage", "sendMessage"]);
});

test("a failed edit falls back to a fresh message", async () => {
  await post("/decision", { ...input, decision_id: "gone" });
  failEdit = true;
  const res = await post("/decision", { ...input, decision_id: "gone" });
  assert.equal(res.body.replaced, false);
  assert.deepEqual(calls.map(verb), ["sendMessage", "editMessageText", "sendMessage"]);
});

test("clearing an unknown id is a no-op", async () => {
  const res = await post("/decision/clear", { decision_id: "never-sent" });
  assert.deepEqual(res.body, { ok: true, cleared: false });
  assert.equal(calls.length, 0);
});

test("an invalid decision is a 400 and zero outbound calls", async () => {
  const res = await post("/decision", { ...input, ask: "issue-819 status?" });
  assert.equal(res.status, 400);
  assert.equal(calls.length, 0);
});
