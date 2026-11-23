import assert from "node:assert/strict";
import { test } from "node:test";

import { REPLY_TOOL, validateReply, validateTurn } from "../schema.js";
import { GOOD_REPLY, PAGE, TURN } from "./fixtures.js";

test("a well-formed reply passes and is returned in closed form", () => {
  const r = validateReply(GOOD_REPLY);
  assert.equal(r.ok, true);
  assert.deepEqual(Object.keys(r.value).sort(), ["expects", "options", "text", "visual"]);
});

test("a reply with no visual and no options is valid for word and none", () => {
  assert.equal(validateReply({ text: "ok", expects: "word" }).ok, true);
  assert.equal(validateReply({ text: "ok", expects: "none" }).ok, true);
});

for (const [name, reply, reason] of [
  ["not an object", "hi", "reply_not_object"],
  ["a code field", { ...GOOD_REPLY, code: "x = 1" }, "reply_unknown_field"],
  ["a tool field", { ...GOOD_REPLY, tool: "exec" }, "reply_unknown_field"],
  ["empty text", { ...GOOD_REPLY, text: "" }, "reply_text"],
  ["text over 280", { ...GOOD_REPLY, text: "x".repeat(281) }, "reply_text"],
  ["a control character", { ...GOOD_REPLY, text: "a\u0000b" }, "reply_text"],
  ["unknown expects", { ...GOOD_REPLY, expects: "click" }, "reply_expects"],
  ["html over 4000", { ...GOOD_REPLY, visual: { html: "x".repeat(4001) } }, "reply_visual"],
  ["extra visual key", { ...GOOD_REPLY, visual: { html: "<b>x</b>", js: "1" } }, "reply_visual"],
  ["a tap without options", { text: "t", expects: "tap" }, "reply_options"],
  ["options on a word reply", { text: "t", expects: "word", options: [{ id: "a", label: "A" }] }, "reply_options"],
  ["five options", { ...GOOD_REPLY, options: ["a", "b", "c", "d", "e"].map((id) => ({ id, label: id })) }, "reply_options"],
  ["a label over 24", { ...GOOD_REPLY, options: [{ id: "a", label: "x".repeat(25) }] }, "reply_options"],
  ["a bad option id", { ...GOOD_REPLY, options: [{ id: "a b", label: "A" }] }, "reply_options"],
  ["duplicate ids", { ...GOOD_REPLY, options: [{ id: "a", label: "A" }, { id: "a", label: "B" }] }, "reply_options"],
  ["an option with an extra key", { ...GOOD_REPLY, options: [{ id: "a", label: "A", run: "x" }] }, "reply_options"],
]) {
  test(`a reply with ${name} is rejected`, () => {
    assert.deepEqual(validateReply(reply), { ok: false, reason });
  });
}

test("the one offered tool is `reply` and its schema is closed with no code field", () => {
  assert.equal(REPLY_TOOL.function.name, "reply");
  const p = REPLY_TOOL.function.parameters;
  assert.equal(p.additionalProperties, false);
  assert.deepEqual(Object.keys(p.properties).sort(), ["expects", "options", "text", "visual"]);
});

test("a well-formed turn passes", () => {
  const t = validateTurn(TURN);
  assert.equal(t.ok, true);
  assert.equal(t.value.page.host, "leetcode.com");
});

test("tap and word turns carry their own field", () => {
  assert.equal(validateTurn({ ...TURN, input: { kind: "tap", id: "a" } }).ok, true);
  assert.equal(validateTurn({ ...TURN, input: { kind: "word", text: "pointers" } }).ok, true);
});

for (const [name, turn] of [
  ["a wrong version", { ...TURN, v: 2 }],
  ["an extra top-level key", { ...TURN, agent: "learnclaw" }],
  ["a model field", { ...TURN, model: "openclaw/learnclaw" }],
  ["a tools field", { ...TURN, tools: [] }],
  ["an unknown input kind", { ...TURN, input: { kind: "exec" } }],
  ["open with a text", { ...TURN, input: { kind: "open", text: "x" } }],
  ["a tap with no id", { ...TURN, input: { kind: "tap" } }],
  ["a tap with a bad id", { ...TURN, input: { kind: "tap", id: "../x" } }],
  ["a word with no text", { ...TURN, input: { kind: "word" } }],
  ["a word over 80", { ...TURN, input: { kind: "word", text: "x".repeat(81) } }],
  ["a non-https page", { ...TURN, page: { ...PAGE, url: "http://leetcode.com/problems/a/" } }],
  ["a page url that is not a url", { ...TURN, page: { ...PAGE, url: "not a url" } }],
  ["an empty problem", { ...TURN, page: { ...PAGE, problem: "" } }],
  ["an oversize code", { ...TURN, page: { ...PAGE, code: "x".repeat(20_001) } }],
  ["an unknown page key", { ...TURN, page: { ...PAGE, cookies: "a=b" } }],
]) {
  test(`a turn with ${name} is rejected`, () => {
    assert.equal(validateTurn(name === "x" ? null : turn).ok, false);
  });
}

test("a turn that is not an object is rejected", () => {
  for (const v of [null, [], "x", 1]) assert.equal(validateTurn(v).ok, false);
});
