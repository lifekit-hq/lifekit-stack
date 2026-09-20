// Renderer contract tests (docs/message-format.md). Pure: no server, no I/O.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_MSG_CHARS,
  envelopeFromDevclawRow,
  envelopeFromText,
  escapeHtml,
  render,
  validateEnvelope,
} from "../render.js";

const base = {
  source: "devclaw",
  subject: "issue-819",
  headline: "needs a decision",
};

function lines(html) {
  return html.split("\n");
}

test("act: red glyph, bold source/subject on line 1, action last as tap-to-copy code", () => {
  const html = render({
    ...base,
    level: "act",
    body: "CI is red.",
    action: "gh run rerun 1",
  });
  assert.deepEqual(lines(html), [
    "🔴 <b>devclaw</b> · <b>issue-819</b> — needs a decision",
    "CI is red.",
    "→ <code>gh run rerun 1</code>",
  ]);
});

test("wait: yellow glyph and the action rendered", () => {
  const html = render({ ...base, level: "wait", action: "decide(issue-819, artifactory)" });
  assert.equal(lines(html)[0], "🟡 <b>devclaw</b> · <b>issue-819</b> — needs a decision");
  assert.equal(lines(html).at(-1), "→ <code>decide(issue-819, artifactory)</code>");
});

test("good: green glyph; a supplied action is never rendered", () => {
  const html = render({
    ...base,
    level: "good",
    headline: "recovered after 5m",
    action: "docker compose -p devclaw up -d",
  });
  assert.equal(html, "🟢 <b>devclaw</b> · <b>issue-819</b> — recovered after 5m");
  assert.doesNotMatch(html, /docker compose/);
  assert.doesNotMatch(html, /<code>/);
});

test("info: square glyph; a supplied action is never rendered", () => {
  const html = render({ ...base, level: "info", headline: "PR opened", action: "rm -rf /" });
  assert.equal(html, "▪️ <b>devclaw</b> · <b>issue-819</b> — PR opened");
  assert.doesNotMatch(html, /rm -rf/);
});

test("a resolved message does not repeat the firing body", () => {
  const firing = {
    level: "act",
    source: "grafana",
    subject: "devclaw",
    headline: "is down",
    body: "Not scraped for 3m — dead, crash-looping, or off lifekit-shared.",
    action: "docker compose -p devclaw up -d",
  };
  const resolved = { ...firing, level: "good", headline: "recovered after 5m", body: "" };
  assert.deepEqual(lines(render(firing)), [
    "🔴 <b>grafana</b> · <b>devclaw</b> — is down",
    "Not scraped for 3m — dead, crash-looping, or off lifekit-shared.",
    "→ <code>docker compose -p devclaw up -d</code>",
  ]);
  assert.equal(render(resolved), "🟢 <b>grafana</b> · <b>devclaw</b> — recovered after 5m");
});

test("detail renders inside an expandable blockquote, after the body, before the action", () => {
  const html = render({
    ...base,
    level: "wait",
    body: "Which hosts count as private?",
    detail: "Spec 030 FR-005a names only npm.pkg.github.com",
    action: "decide(issue-819, artifactory)",
  });
  assert.deepEqual(lines(html), [
    "🟡 <b>devclaw</b> · <b>issue-819</b> — needs a decision",
    "Which hosts count as private?",
    "<blockquote expandable>Spec 030 FR-005a names only npm.pkg.github.com</blockquote>",
    "→ <code>decide(issue-819, artifactory)</code>",
  ]);
});

test("links render as anchors on their own line, escaped", () => {
  const html = render({
    ...base,
    level: "good",
    headline: "closed",
    body: "Nested .npmrc advisory shipped.",
    links: [
      { text: "PR #820 <a&b>", url: "https://example.test/pr/820?a=1&b=2" },
      { text: "no url" },
      { url: "https://example.test/no-text" },
    ],
  });
  assert.deepEqual(lines(html), [
    "🟢 <b>devclaw</b> · <b>issue-819</b> — closed",
    "Nested .npmrc advisory shipped.",
    '<a href="https://example.test/pr/820?a=1&amp;b=2">PR #820 &lt;a&amp;b&gt;</a>',
  ]);
});

test("every interpolated field is HTML-escaped", () => {
  const html = render({
    level: "act",
    source: "src<&>",
    subject: "sub<&>",
    headline: "head<&>",
    body: "body<&>",
    detail: "detail<&>",
    action: "act<&>",
    links: [{ text: "link<&>", url: "https://example.test/?q=<&>" }],
  });
  assert.equal(
    html,
    [
      "🔴 <b>src&lt;&amp;&gt;</b> · <b>sub&lt;&amp;&gt;</b> — head&lt;&amp;&gt;",
      "body&lt;&amp;&gt;",
      "<blockquote expandable>detail&lt;&amp;&gt;</blockquote>",
      '<a href="https://example.test/?q=&lt;&amp;&gt;">link&lt;&amp;&gt;</a>',
      "→ <code>act&lt;&amp;&gt;</code>",
    ].join("\n"),
  );
  // Nothing raw survives outside the tags the renderer itself emits.
  const stripped = html.replace(/<\/?(b|code|blockquote expandable|blockquote|a href="[^"]*"|a)>/g, "");
  assert.doesNotMatch(stripped, /[<>]/);
  assert.doesNotMatch(stripped, /&(?!amp;|lt;|gt;)/);
});

test("escapeHtml covers & < >", () => {
  assert.equal(escapeHtml("TypeError: expected <str> & got"), "TypeError: expected &lt;str&gt; &amp; got");
});

test("oversized envelope: detail truncated first, then body, headline intact, under the cap", () => {
  const headline = "h".repeat(200);
  const body = "b".repeat(3000);
  const detail = "d".repeat(5000);
  const html = render({ ...base, level: "act", headline, body, detail, action: "fix" });
  assert.ok(html.length <= MAX_MSG_CHARS, `${html.length} > ${MAX_MSG_CHARS}`);
  assert.ok(html.length < 4096);
  assert.equal(lines(html)[0], `🔴 <b>devclaw</b> · <b>issue-819</b> — ${headline}`);
  // Detail took the whole cut; the body is untouched and the action survived.
  assert.match(html, /\n[b]{3000}\n/);
  assert.match(html, /<blockquote expandable>d+…<\/blockquote>/);
  assert.equal(lines(html).at(-1), "→ <code>fix</code>");
});

test("oversized body with no detail is truncated, headline intact", () => {
  const html = render({ ...base, level: "info", body: "x".repeat(6000) });
  assert.ok(html.length <= MAX_MSG_CHARS);
  assert.equal(lines(html)[0], "▪️ <b>devclaw</b> · <b>issue-819</b> — needs a decision");
  assert.match(lines(html)[1], /^x+…$/);
});

test("truncation never splits an emoji into a lone surrogate", () => {
  const html = render({ ...base, level: "info", body: "🙂".repeat(4000) });
  assert.ok(html.length <= MAX_MSG_CHARS);
  assert.ok(html.isWellFormed());
  assert.equal(lines(html)[0], "▪️ <b>devclaw</b> · <b>issue-819</b> — needs a decision");
  // A prefix of the body survives — it is cut, not dropped.
  assert.match(lines(html)[1], /^(?:🙂)+…$/u);
});

test("escaping inside detail is accounted for when truncating", () => {
  const html = render({ ...base, level: "info", detail: "<&>".repeat(3000) });
  assert.ok(html.length <= MAX_MSG_CHARS, `${html.length} > ${MAX_MSG_CHARS}`);
  assert.doesNotMatch(html, /<&>/);
  assert.equal(lines(html)[0], "▪️ <b>devclaw</b> · <b>issue-819</b> — needs a decision");
  // The escape overhead must not swallow the whole field: a prefix survives.
  const quoted = html.match(/<blockquote expandable>([\s\S]*)<\/blockquote>/)[1];
  assert.ok(quoted.startsWith("&lt;&amp;&gt;"), quoted.slice(0, 40));
  assert.ok(quoted.endsWith("…"));
  assert.ok(quoted.length > 100, `detail kept only ${quoted.length} chars`);
});

test("validateEnvelope names every missing required field and rejects unknown levels", () => {
  assert.deepEqual(validateEnvelope({ ...base, level: "act" }), []);
  assert.deepEqual(validateEnvelope({}), [
    "missing 'level'",
    "missing 'source'",
    "missing 'subject'",
    "missing 'headline'",
  ]);
  assert.deepEqual(validateEnvelope({ ...base, level: "urgent" }), [
    "unknown level 'urgent' (act | wait | good | info)",
  ]);
  // Object.prototype keys are unknown levels like any other, not free passes.
  for (const level of ["constructor", "toString", "valueOf", "hasOwnProperty", "__proto__"]) {
    assert.deepEqual(validateEnvelope({ ...base, level }), [
      `unknown level '${level}' (act | wait | good | info)`,
    ]);
  }
  assert.deepEqual(validateEnvelope({ ...base, level: "act", subject: "  " }), ["missing 'subject'"]);
  assert.deepEqual(validateEnvelope(null), ["envelope must be a JSON object"]);
  assert.deepEqual(validateEnvelope([]), ["envelope must be a JSON object"]);
});

test("/devclaw row: status maps to level; failed error is the collapsed detail", () => {
  const failed = envelopeFromDevclawRow({
    task_id: "0123456789abcdef",
    kind: "task",
    status: "failed",
    goal: "ship the thing",
    error: "TypeError: expected <str>",
  });
  assert.deepEqual(failed, {
    level: "act",
    source: "devclaw",
    subject: "task 01234567",
    headline: "failed",
    body: "ship the thing",
    detail: "TypeError: expected <str>",
  });
  assert.equal(
    render(failed),
    [
      "🔴 <b>devclaw</b> · <b>task 01234567</b> — failed",
      "ship the thing",
      "<blockquote expandable>TypeError: expected &lt;str&gt;</blockquote>",
    ].join("\n"),
  );

  const done = envelopeFromDevclawRow({
    task_id: "abc",
    status: "done",
    goal: "ship",
    result_json: JSON.stringify({ message: "merged" }),
  });
  assert.equal(done.level, "good");
  assert.equal(done.body, "ship\n\nmerged");
  assert.equal(done.detail, "");

  assert.equal(envelopeFromDevclawRow({ status: "running" }).level, "info");
  assert.equal(envelopeFromDevclawRow({}).level, "info");
  assert.equal(envelopeFromDevclawRow({}).subject, "task ?");
  assert.equal(envelopeFromDevclawRow({}).headline, "unknown");
});

test("/text: first line is the headline, the rest the body, always info", () => {
  assert.deepEqual(envelopeFromText("  🚀 goal started\nline two\nline three\n"), {
    level: "info",
    source: "devclaw",
    subject: "goal",
    headline: "🚀 goal started",
    body: "line two\nline three",
  });
  assert.deepEqual(envelopeFromText("one liner"), {
    level: "info",
    source: "devclaw",
    subject: "goal",
    headline: "one liner",
    body: "",
  });
});
