/**
 * task-row.test.js — the devclaw task row → v1 envelope mapping.
 *
 * The row shapes here are the ones devclaw actually posts: `result_json` as a
 * JSON string or as an already-parsed object, statuses outside the known two,
 * and multi-line errors. Every case must produce an envelope render.js accepts,
 * so each test validates before asserting on the mapping.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { renderEnvelope, validateEnvelope } from "./render.js";
import { envelopeFromTaskRow } from "./task-row.js";

/** Map, then prove the result is a valid envelope before asserting on it. */
function mapped(row) {
  const envelope = envelopeFromTaskRow(row);
  assert.deepEqual(validateEnvelope(envelope), [], JSON.stringify(envelope));
  return envelope;
}

const TASK_ID = "3f2a1b9c-7d10-4e55-9c21-000000000000";

describe("level", () => {
  it("maps done to good, so no action and a green glyph", () => {
    assert.equal(mapped({ status: "done", task_id: TASK_ID }).level, "good");
  });

  it("maps failed to act", () => {
    assert.equal(mapped({ status: "failed", task_id: TASK_ID }).level, "act");
  });

  it("maps any other status to info rather than inventing urgency", () => {
    for (const status of ["running", "cancelled", "", "  "]) {
      assert.equal(mapped({ status, task_id: TASK_ID }).level, "info", status);
    }
  });

  it("maps a missing status to info", () => {
    const envelope = mapped({ task_id: TASK_ID });
    assert.equal(envelope.level, "info");
    assert.equal(envelope.headline, "unknown");
  });

  // `status` is producer-controlled; a bare lookup would resolve these off
  // Object.prototype and hand render.js a level it cannot glyph.
  it("does not read the status off the prototype chain", () => {
    for (const status of ["constructor", "toString", "__proto__"]) {
      assert.equal(mapped({ status, task_id: TASK_ID }).level, "info", status);
    }
  });
});

describe("line 1", () => {
  it("names the kind and a truncated task id as the subject", () => {
    const envelope = mapped({ status: "done", kind: "implement_feature", task_id: TASK_ID });
    assert.equal(envelope.subject, "implement_feature 3f2a1b9c…");
    assert.equal(envelope.source, "devclaw");
  });

  it("leaves a task id short enough to fit unmarked", () => {
    assert.equal(mapped({ status: "done", task_id: "abc123" }).subject, "task abc123");
  });

  it("falls back to placeholders when the row is bare", () => {
    assert.equal(mapped({}).subject, "task ?");
  });

  it("puts the goal in the headline so the lock screen says which work", () => {
    const envelope = mapped({ status: "failed", goal: "ship the envelope" });
    assert.equal(envelope.headline, "failed: ship the envelope");
  });

  // slice() would cut a surrogate pair in half and render U+FFFD.
  it("truncates the task id by code point, not code unit", () => {
    assert.equal(mapped({ task_id: "a😀😀😀😀" }).subject, "task a😀😀😀😀");
    assert.equal(mapped({ task_id: "😀".repeat(9) }).subject, `task ${"😀".repeat(8)}…`);
  });
});

describe("body and detail", () => {
  it("shows a failure's first line and collapses the rest", () => {
    const envelope = mapped({
      status: "failed",
      task_id: TASK_ID,
      error: "change_class: gate-input edit\nTraceback:\n  line 1\n  line 2",
    });

    assert.equal(envelope.body, "change_class: gate-input edit");
    assert.equal(envelope.detail, "Traceback:\n  line 1\n  line 2");
  });

  it("leaves detail empty for a one-line failure", () => {
    const envelope = mapped({ status: "failed", error: "  boom  " });
    assert.equal(envelope.body, "boom");
    assert.equal(envelope.detail, "");
  });

  it("reads result_json when devclaw sends it as a JSON string", () => {
    const envelope = mapped({
      status: "done",
      task_id: TASK_ID,
      result_json: JSON.stringify({ message: "PR #136 opened\nverified green" }),
    });

    assert.equal(envelope.body, "PR #136 opened");
    assert.equal(envelope.detail, "verified green");
  });

  it("reads result_json when devclaw sends it already parsed", () => {
    const envelope = mapped({
      status: "done",
      task_id: TASK_ID,
      result_json: { message: "PR #136 opened" },
    });

    assert.equal(envelope.body, "PR #136 opened");
  });

  it("survives a result_json that is not JSON, or has no message", () => {
    for (const result_json of ["{not json", "null", '"a string"', {}, null, 7]) {
      const envelope = mapped({ status: "done", result_json });
      assert.equal(envelope.body, "", JSON.stringify(result_json));
    }
  });

  // The legacy formatter only read result_json on `done`, so an in-flight row
  // arrived as a bare status line. Any non-failure status reports it now.
  it("reports result_json on an in-flight status too", () => {
    const envelope = mapped({
      status: "running",
      task_id: TASK_ID,
      result_json: { message: "3 of 7 slices" },
    });

    assert.equal(envelope.level, "info");
    assert.equal(envelope.body, "3 of 7 slices");
  });

  it("ignores result_json on a failure — the error is what happened", () => {
    const envelope = mapped({
      status: "failed",
      error: "boom",
      result_json: JSON.stringify({ message: "should not appear" }),
    });

    assert.equal(envelope.body, "boom");
    assert.equal(envelope.detail, "");
  });
});

describe("rendered through render.js", () => {
  it("renders a failure as an act message with the log collapsed", () => {
    const text = renderEnvelope(
      envelopeFromTaskRow({
        status: "failed",
        kind: "implement_feature",
        task_id: TASK_ID,
        goal: "ship the envelope",
        error: "boom & <bust>\nat line 1",
      }),
    );

    assert.equal(
      text,
      "🔴 <b>devclaw</b> · <b>implement_feature 3f2a1b9c…</b> — failed: ship the envelope" +
        "\n\nboom &amp; &lt;bust&gt;" +
        "\n\n<blockquote expandable>at line 1</blockquote>",
    );
  });

  it("renders a bare done row as line 1 alone", () => {
    const text = renderEnvelope(envelopeFromTaskRow({ status: "done", task_id: "abc123" }));
    assert.equal(text, "🟢 <b>devclaw</b> · <b>task abc123</b> — done");
  });

  // The old formatter clipped the traceback to 600 characters and pasted it
  // inline; the envelope's budget now bounds it instead.
  it("keeps a huge error inside Telegram's limit", () => {
    const text = renderEnvelope(
      envelopeFromTaskRow({
        status: "failed",
        task_id: TASK_ID,
        error: `head\n${"trace & more ".repeat(2000)}`,
      }),
    );

    assert.ok(text.length <= 3500, `rendered ${text.length} chars`);
    assert.ok(text.includes("<blockquote expandable>"));
    assert.ok(!text.includes("&am…"), "clipped mid-entity");
  });
});
