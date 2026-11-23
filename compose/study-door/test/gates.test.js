import assert from "node:assert/strict";
import { test } from "node:test";

import { COPY_RUN, codeInReply } from "../gates.js";
import { GOOD_REPLY, PAGE } from "./fixtures.js";

const gate = (reply, code = PAGE.code) => codeInReply({ expects: "none", ...reply }, code);

test("the model's ordinary reply passes", () => {
  assert.equal(gate(GOOD_REPLY), null);
});

test("a trace of values and a short inline name pass", () => {
  assert.equal(gate({ text: "After step 2, `seen` holds 2 -> 0 and i is 1. Which index is next?", visual: { html: "<div>seen = {2: 0}</div><code>seen[n]</code>" } }), null);
});

for (const [name, reply, reason] of [
  ["a fenced block", { text: "Try:\n```python\nx = 1\n```" }, "fenced_code"],
  ["a tilde fence", { text: "~~~\nx\n~~~" }, "fenced_code"],
  ["a fence in the html", { text: "see", visual: { html: "<div>```x```</div>" } }, "fenced_code"],
  ["a long inline code span", { text: `Use \`${"a".repeat(41)}\`` }, "inline_code_long"],
  ["a pre block over 40 chars", { text: "see", visual: { html: `<pre>${"x".repeat(41)}</pre>` } }, "pre_block"],
  ["a multi-line pre block", { text: "see", visual: { html: "<pre>a\nb</pre>" } }, "pre_block"],
  ["a long code element", { text: "see", visual: { html: `<code>${"y".repeat(60)}</code>` } }, "code_long"],
  ["a def statement", { text: "def solve(nums, target):" }, "stmt_def"],
  ["a class statement", { text: "class Solution:" }, "stmt_class"],
  ["a for loop", { text: "for (int i = 0; i < n; i++) {" }, "stmt_for_paren"],
  ["a python for-in", { text: "for i, n in enumerate(nums):" }, "stmt_for_in"],
  ["a while loop", { text: "while (l < r) {" }, "stmt_while"],
  ["an if block", { text: "if (x > 1) {" }, "stmt_if"],
  ["a return statement", { text: "return seen[target - n];" }, "stmt_return"],
  ["an arrow body", { text: "xs.map(n => { n + 1 })" }, "stmt_arrow"],
  ["a function", { text: "function twoSum(nums, t) {" }, "stmt_function"],
  ["a declaration", { text: "const seen = new Map();" }, "stmt_decl"],
  ["a statement hidden in the visual", { text: "see", visual: { html: "<div>def twoSum(self, nums):</div>" } }, "stmt_def"],
]) {
  test(`rejects ${name}`, () => {
    assert.equal(gate(reply), reason);
  });
}

test(`rejects a reply that repeats ${COPY_RUN} consecutive tokens of the owner's code`, () => {
  const run = "if target - n in seen: return [seen[target - n], i]";
  const reason = gate({ text: `Your line ${run} is`, visual: undefined });
  assert.ok(reason, "a statement shape or a copied run fires");
  const owner = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi";
  assert.equal(gate({ text: "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu" }, owner), "copies_owner_code");
});

test("a shorter overlap with the owner's code is a quote, not a rewrite", () => {
  const owner = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi";
  assert.equal(gate({ text: "alpha beta gamma delta epsilon zeta" }, owner), null);
});

test("no owner code means no copy check", () => {
  assert.equal(gate({ text: "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu" }, ""), null);
});
