import assert from "node:assert/strict";
import { test } from "node:test";

import { sanitizeHtml, visibleText } from "../sanitize.js";
import { GOOD_REPLY } from "./fixtures.js";

const clean = (h) => sanitizeHtml(h).html;

test("the model's ordinary visual passes through untouched", () => {
  const out = sanitizeHtml(GOOD_REPLY.visual.html);
  assert.equal(out.removed, 0);
  assert.match(out.html, /<svg viewbox="0 0 120 40" width="240">/);
  assert.match(out.html, /<rect [^>]*fill="#eef"[^>]*\/>/);
  assert.match(out.html, /<text [^>]*>2<\/text><\/svg>$/);
});

for (const [name, html, gone] of [
  ["a script element and its content", "<div>a</div><script>alert(1)</script><p>b</p>", /alert|script/i],
  ["an uppercase script", "<SCRIPT SRC=x></SCRIPT>t", /script|src/i],
  ["an iframe", '<iframe src="https://evil.example"></iframe>', /iframe|evil/i],
  ["an object", '<object data="x"></object>', /object/i],
  ["an embed", '<embed src="x">', /embed/i],
  ["a form", '<form action="/x"><input name=a></form>', /form|input|action/i],
  ["a link", '<a href="https://evil.example">go</a>', /href|evil/i],
  ["an img", '<img src="https://evil.example/x.png" onerror="x()">', /img|evil|onerror/i],
  ["an svg image", '<svg><image href="https://evil.example/x.png"/></svg>', /image|evil/i],
  ["an svg use", '<svg><use href="#a"/></svg>', /<use/i],
  ["a foreignObject and what is in it", "<svg><foreignObject><div onclick=x>hi</div><script>1</script></foreignObject></svg>", /foreign|onclick|script|hi/i],
  ["a style element", "<style>body{background:url(https://evil.example)}</style><b>x</b>", /style|evil|background/i],
  ["an svg animate", '<svg><rect><animate attributeName="x" to="1"/></rect></svg>', /animate/i],
  ["an svg set", '<svg><set attributeName="href" to="javascript:x"/></svg>', /<set|javascript/i],
  ["a meta refresh", '<meta http-equiv="refresh" content="0;url=https://evil.example">', /meta|evil/i],
  ["a base", '<base href="https://evil.example/">', /base|evil/i],
  ["a link element", '<link rel="stylesheet" href="https://evil.example/x.css">', /link|evil/i],
  ["a comment", "<!-- <script>x</script> -->a", /script|--/],
  ["a CDATA section", "<![CDATA[<script>x</script>]]>a", /CDATA|script/],
  ["a doctype", "<!DOCTYPE html>a", /DOCTYPE/i],
  ["a textarea and its content", "<textarea><script>x</script></textarea>", /textarea|script/i],
]) {
  test(`strips ${name}`, () => {
    const out = sanitizeHtml(html);
    assert.doesNotMatch(out.html, gone);
    assert.ok(out.removed > 0, "counts what it removed");
  });
}

for (const attr of [
  'onclick="x()"',
  'onload="x()"',
  'onmouseover=x()',
  'href="https://evil.example"',
  'src="https://evil.example"',
  'xlink:href="https://evil.example"',
  'srcdoc="<script>x</script>"',
  'formaction="x"',
  'style="background:url(https://evil.example/x.png)"',
  "style=\"background:url('data:image/png;base64,AAAA')\"",
  'style="@import url(x)"',
  'style="width:expression(alert(1))"',
  'style="behavior:url(x.htc)"',
  'style="-moz-binding:url(x)"',
  'style="color:\\72 ed"',
  'fill="url(https://evil.example/x.svg#a)"',
  'fill="javascript:alert(1)"',
  'transform="&#106;avascript:x"',
]) {
  test(`drops the attribute ${attr}`, () => {
    const out = sanitizeHtml(`<div ${attr}>t</div>`);
    assert.equal(out.html, "<div>t</div>");
    assert.ok(out.removed >= 1);
  });
}

test("keeps a local url(#id) reference and a plain style", () => {
  const out = clean('<svg><defs><marker id="arrow"></marker></defs><path d="M0 0L10 10" marker-end="url(#arrow)" style="stroke:#333;stroke-width:2"/></svg>');
  assert.match(out, /marker-end="url\(#arrow\)"/);
  assert.match(out, /style="stroke:#333;stroke-width:2"/);
});

test("output is balanced whatever the input", () => {
  assert.equal(clean("<div><span>x"), "<div><span>x</span></div>");
  assert.equal(clean("x</div></b>y"), "xy");
  assert.equal(clean("<div/>x"), "<div></div>x");
});

test("a stray angle bracket never starts a tag", () => {
  assert.equal(clean("1 < 2 and <3"), "1 &lt; 2 and &lt;3");
});

test("nothing executable or fetchable survives a hostile pile", () => {
  const pile =
    '<svg onload=x><a xlink:href="javascript:x"><text>t</text></a></svg><img src=x onerror=x><iframe srcdoc="<script>x</script>"></iframe>' +
    '<div style="background:url(//evil.example)" onclick="x">ok</div><SCRIPT>x</SCRIPT><object data=x></object><math><mi>x</mi></math>';
  const out = clean(pile);
  assert.doesNotMatch(out, /script|onload|onclick|onerror|href|src|iframe|object|img|evil|url\(|math/i);
  assert.match(out, /ok/);
});

test("visibleText gives the words a reader sees", () => {
  assert.equal(visibleText("<b>a</b> &lt;b&gt; <svg><text>2</text></svg>").replace(/\s+/g, " ").trim(), "a <b> 2");
});

test("a 4000-char pathological input is sanitised in well under a second", () => {
  for (const c of ["<a ".repeat(1300), `<div ${'x="'.repeat(1300)}`, `<${'"'.repeat(3000)}`, "<b>".repeat(1300), "<script>".repeat(500), "<!--".repeat(1000)]) {
    const t = Date.now();
    sanitizeHtml(c.slice(0, 4000));
    assert.ok(Date.now() - t < 1000);
  }
});
