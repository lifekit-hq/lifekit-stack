import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  MAX_MSG_CHARS,
  escapeClipped,
  renderEnvelope,
  validateEnvelope,
} from "./render.js";

const base = {
  level: "act",
  source: "devclaw",
  subject: "issue-819",
  headline: "needs a decision",
};

describe("validateEnvelope", () => {
  it("accepts a minimal envelope", () => {
    assert.deepEqual(validateEnvelope(base), []);
  });

  it("accepts the full envelope", () => {
    const errors = validateEnvelope({
      ...base,
      body: "Which hosts count as private?",
      detail: "stack trace",
      action: "decide(issue-819, artifactory)",
      links: [{ text: "PR #820", url: "https://github.com/x/y/pull/820" }],
    });
    assert.deepEqual(errors, []);
  });

  it("rejects a non-object envelope", () => {
    assert.deepEqual(validateEnvelope("nope"), ["envelope must be a JSON object"]);
    assert.deepEqual(validateEnvelope([base]), ["envelope must be a JSON object"]);
  });

  it("rejects an unknown level", () => {
    const errors = validateEnvelope({ ...base, level: "urgent" });
    assert.equal(errors.length, 1);
    assert.match(errors[0], /^level must be one of act, wait, good, info$/);
  });

  it("names every missing required field", () => {
    const errors = validateEnvelope({ level: "info", subject: "   " });
    assert.deepEqual(errors, [
      "source is required and must be a non-empty string",
      "subject is required and must be a non-empty string",
      "headline is required and must be a non-empty string",
    ]);
  });

  it("rejects non-string optional fields", () => {
    const errors = validateEnvelope({ ...base, body: 42, action: ["x"] });
    assert.deepEqual(errors, [
      "body must be a string when present",
      "action must be a string when present",
    ]);
  });

  it("rejects link URLs that are not http(s)", () => {
    const errors = validateEnvelope({
      ...base,
      links: [{ text: "tap", url: "javascript:alert(1)" }],
    });
    assert.deepEqual(errors, ["links[0].url must be http or https"]);
  });

  it("rejects malformed links", () => {
    assert.deepEqual(validateEnvelope({ ...base, links: "PR #820" }), [
      "links must be an array",
    ]);
    assert.deepEqual(validateEnvelope({ ...base, links: [{ url: "not a url" }] }), [
      "links[0].text must be a non-empty string",
      "links[0].url is not a valid URL",
    ]);
    assert.deepEqual(validateEnvelope({ ...base, links: ["https://x.dev"] }), [
      "links[0] must be an object",
    ]);
  });

  it("rejects more links than fit on one line", () => {
    const link = { text: "PR", url: "https://example.com/" };
    assert.deepEqual(validateEnvelope({ ...base, links: [link, link, link, link] }), [
      "links accepts at most 3 entries",
    ]);
  });

  it("rejects a URL too long to render whole", () => {
    const errors = validateEnvelope({
      ...base,
      links: [{ text: "PR", url: `https://example.com/${"p".repeat(400)}` }],
    });
    assert.deepEqual(errors, ["links[0].url must be at most 300 characters"]);
  });

  // Python's json.dumps writes an unset optional as null, not as a missing key.
  it("treats a null optional as absent", () => {
    const errors = validateEnvelope({
      ...base,
      body: null,
      detail: null,
      action: null,
      links: null,
    });
    assert.deepEqual(errors, []);
  });

  it("does not mistake an inherited Object property for a level", () => {
    const errors = validateEnvelope({ ...base, level: "constructor" });
    assert.deepEqual(errors, ["level must be one of act, wait, good, info"]);
  });
});

describe("escapeClipped", () => {
  it("escapes the Telegram-significant characters", () => {
    assert.equal(escapeClipped('a & b < c > d "e"', 100), "a &amp; b &lt; c &gt; d &quot;e&quot;");
  });

  it("never cuts an entity in half", () => {
    // "&&&&&" escapes to 25 chars; clipped to 12 it must stop on a whole entity.
    const out = escapeClipped("&&&&&", 12);
    assert.ok(out.length <= 12, `got ${out.length} chars: ${out}`);
    assert.equal(out, "&amp;&amp;…");
  });

  it("marks a clipped string with an ellipsis", () => {
    assert.equal(escapeClipped("abcdefghij", 5), "abcd…");
  });

  it("never leaves half an emoji behind", () => {
    for (let max = 2; max <= 24; max++) {
      const out = escapeClipped("😀".repeat(10), max);
      assert.ok(out.length <= max, `max=${max}: ${out.length} chars`);
      assert.equal(out, out.toWellFormed(), `max=${max} split a surrogate pair`);
    }
  });

  it("degrades safely when there is no room at all", () => {
    assert.equal(escapeClipped("abc", 0), "");
    assert.equal(escapeClipped("abc", 1), "…");
    assert.equal(escapeClipped("abc", 3), "abc");
  });

  // A Python producer using `surrogatepass` can send a half emoji it never
  // clipped; one lone surrogate anywhere makes the Bot API reject the send.
  it("repairs a lone surrogate the producer sent", () => {
    const highOnly = JSON.parse('"lone \\ud83d tail"');
    const lowOnly = JSON.parse('"lone \\udc00 tail"');
    assert.equal(escapeClipped(highOnly, 100), "lone � tail");
    assert.equal(escapeClipped(lowOnly, 100), "lone � tail");
    for (const max of [4, 5, 6, 7, 8]) {
      const out = escapeClipped(lowOnly, max);
      assert.equal(out, out.toWellFormed(), `max=${max}: ${JSON.stringify(out)}`);
    }
  });
});

describe("renderEnvelope", () => {
  it("puts source, subject and headline on a stand-alone first line", () => {
    const first = renderEnvelope(base).split("\n")[0];
    assert.equal(
      first,
      "🔴 <b>devclaw</b> · <b>issue-819</b> — needs a decision",
    );
  });

  it("uses one glyph per level", () => {
    for (const [level, glyph] of [
      ["act", "🔴"],
      ["wait", "🟡"],
      ["good", "🟢"],
      ["info", "▪️"],
    ]) {
      const out = renderEnvelope({ ...base, level });
      assert.ok(out.startsWith(`${glyph} <b>devclaw</b>`), `${level}: ${out}`);
    }
  });

  it("renders action last, tap-to-copy, for act and wait", () => {
    for (const level of ["act", "wait"]) {
      const out = renderEnvelope({
        ...base,
        level,
        body: "some context",
        action: "decide(issue-819)",
      });
      assert.ok(out.endsWith("→ <code>decide(issue-819)</code>"), out);
    }
  });

  it("drops action for good and info even when the producer sends one", () => {
    for (const level of ["good", "info"]) {
      const out = renderEnvelope({
        ...base,
        level,
        headline: "recovered",
        action: "docker ps on the box",
      });
      assert.ok(!out.includes("docker ps"), out);
      assert.ok(!out.includes("<code>"), out);
    }
  });

  it("folds newlines out of the fields that must stay on one line", () => {
    const out = renderEnvelope({
      level: "act",
      source: "grafana\nEVIL",
      subject: "  spaced \n out  ",
      headline: "line one\nline two",
      action: "restart\nrm -rf /",
    });
    const lines = out.split("\n");
    assert.equal(
      lines[0],
      "🔴 <b>grafana EVIL</b> · <b>spaced out</b> — line one line two",
    );
    assert.equal(lines.at(-1), "→ <code>restart rm -rf /</code>");
  });

  it("renders detail collapsed", () => {
    const out = renderEnvelope({ ...base, detail: "Traceback:\n  boom" });
    assert.ok(
      out.includes("<blockquote expandable>Traceback:\n  boom</blockquote>"),
      out,
    );
  });

  it("orders the blocks headline, body, detail, links, action", () => {
    const out = renderEnvelope({
      ...base,
      body: "BODY",
      detail: "DETAIL",
      action: "ACTION",
      links: [{ text: "PR", url: "https://example.com/pr" }],
    });
    const order = ["— needs a decision", "BODY", "DETAIL", ">PR<", "ACTION"].map(
      (needle) => out.indexOf(needle),
    );
    assert.ok(
      order.every((i) => i !== -1),
      out,
    );
    assert.deepEqual(order, [...order].sort((a, b) => a - b));
  });

  // URL parsing drops tabs and newlines, so rendering the raw string would put
  // a control character inside href="…" that validation had already accepted.
  it("renders the normalised URL, not the raw one", () => {
    const url = "https://example.com/a\nb\tc?q=1 2";
    assert.deepEqual(validateEnvelope({ ...base, links: [{ text: "PR", url }] }), []);
    const out = renderEnvelope({ ...base, links: [{ text: "PR", url }] });
    assert.ok(out.includes('href="https://example.com/abc?q=1%202"'), out);
  });

  it("escapes producer text everywhere it lands", () => {
    const out = renderEnvelope({
      ...base,
      source: "dev&claw",
      subject: "<b>injected</b>",
      headline: "5 < 6 & 7 > 2",
      body: "body: 5 < 6 & 7 > 2",
      detail: "<script>alert(1)</script>",
      action: 'run --flag="x"',
      links: [{ text: "<i>a</i> & b > c", url: "https://example.com/?a=1&b=2" }],
    });
    assert.ok(!out.includes("<script>"), out);
    assert.ok(!out.includes("<i>"), out);
    assert.ok(out.includes("<b>dev&amp;claw</b>"), out);
    assert.ok(out.includes("&lt;b&gt;injected&lt;/b&gt;"), out);
    assert.ok(out.includes("— 5 &lt; 6 &amp; 7 &gt; 2"), out);
    assert.ok(out.includes("body: 5 &lt; 6 &amp; 7 &gt; 2"), out);
    // The link's TEXT is producer input just as much as its href is.
    assert.ok(
      out.includes(
        '<a href="https://example.com/?a=1&amp;b=2">&lt;i&gt;a&lt;/i&gt; &amp; b &gt; c</a>',
      ),
      out,
    );
    assert.ok(out.includes("&quot;x&quot;"), out);
  });

  it("keeps headline and action when detail is enormous", () => {
    const out = renderEnvelope({
      ...base,
      body: "b".repeat(5000),
      detail: "d".repeat(50000),
      action: "decide(issue-819)",
    });
    assert.ok(out.length <= MAX_MSG_CHARS, `message was ${out.length} chars`);
    assert.ok(out.startsWith("🔴 <b>devclaw</b>"), out);
    assert.ok(out.endsWith("→ <code>decide(issue-819)</code>"), out);
    // The point of the budget is that detail SURVIVES, clipped, rather than
    // being dropped to make room.
    assert.match(out, /<blockquote expandable>d{100,}…<\/blockquote>/);
    assert.match(out, /\n\nb{100,}…\n\n/);
  });

  it("stays inside the limit with every field at its maximum", () => {
    const out = renderEnvelope({
      level: "act",
      source: "s".repeat(500),
      subject: "j".repeat(500),
      headline: "h".repeat(500),
      body: "&".repeat(5000),
      detail: "&".repeat(5000),
      action: "a".repeat(500),
      links: [1, 2, 3].map((n) => ({
        text: "t".repeat(200),
        url: `https://example.com/${"p".repeat(270)}/${n}`,
      })),
    });
    assert.ok(out.length <= MAX_MSG_CHARS, `message was ${out.length} chars`);
    assert.ok(out.includes("<blockquote expandable>"), "detail was dropped");
  });

  it("omits blocks the producer did not send", () => {
    const out = renderEnvelope(base);
    assert.equal(out, "🔴 <b>devclaw</b> · <b>issue-819</b> — needs a decision");
  });
});
