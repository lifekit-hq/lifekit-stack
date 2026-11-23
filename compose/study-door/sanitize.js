/**
 * HTML allow-list sanitiser for the reply's visual. The panel renders the result in
 * a sandboxed, script-less iframe with a CSP, so this is the second wall, not the first:
 * it keeps the stored and logged artefact free of anything executable or fetchable, and it
 * is the thing the tests pin. Anything not on a list is dropped; elements that carry
 * their own content model (script, style, iframe, foreignObject ...) are dropped with their content.
 *
 * Output is always balanced: an unmatched close tag is ignored and open tags are closed at the end.
 */

const VOID = new Set(["br", "hr", "rect", "circle", "ellipse", "line", "polyline", "polygon", "path", "stop"]);

const TAGS = new Set([
  // text and layout
  "div", "span", "p", "b", "i", "em", "strong", "small", "sub", "sup", "br", "hr",
  "h1", "h2", "h3", "h4", "ul", "ol", "li", "table", "thead", "tbody", "tr", "td", "th",
  // svg: shapes and text only. No `use`, `image`, `a`, `foreignObject`, `animate`, `set`, `script`, `style`.
  "svg", "g", "defs", "marker", "rect", "circle", "ellipse", "line", "polyline", "polygon", "path", "text", "tspan", "title", "stop",
  "lineargradient",
]);

/** Dropped together with everything inside them. */
const DROP_WITH_CONTENT = new Set([
  "script", "style", "iframe", "frame", "frameset", "object", "embed", "applet", "template", "noscript",
  "foreignobject", "textarea", "select", "option", "button", "form", "math", "audio", "video", "canvas",
  "head", "xmp", "plaintext", "listing", "noembed", "noframes",
]);

const ATTRS = new Set([
  "style", "colspan", "rowspan",
  // svg presentation and geometry
  "viewbox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "d", "points",
  "fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin", "opacity",
  "fill-opacity", "stroke-opacity", "transform", "text-anchor", "dominant-baseline", "font-size", "font-weight",
  "font-family", "marker-end", "marker-start", "id", "refx", "refy", "markerwidth", "markerheight", "orient",
  "markerunits", "preserveaspectratio", "offset", "stop-color", "stop-opacity", "dy", "dx", "fill-rule",
]);

const MAX_ATTR = 400;
// A value that can fetch, script or escape: never allowed in any attribute or in a style.
// Any `&` is refused: the browser decodes named and numeric entities inside attribute values, so
// `&lpar;`, `&bsol;` or `&colon;` would rebuild a `url(`, an escape or a scheme the checks below never saw.
const HOSTILE = /javascript:|vbscript:|data:|expression\s*\(|behavio[u]?r\s*:|-moz-binding|@import|\\|<|>|&/i;

/** url(#id) pointing inside the document is the only url() there is. */
function onlyLocalUrls(v) {
  const urls = v.match(/url\s*\(([^)]*)\)/gi) ?? [];
  if (!urls.every((u) => /^url\s*\(\s*['"]?#[A-Za-z0-9_-]{1,40}['"]?\s*\)$/i.test(u))) return false;
  return !/url\s*\(/i.test(v.replace(/url\s*\([^)]*\)/gi, ""));
}

function cleanValue(name, value) {
  if (value.length > MAX_ATTR) return null;
  if (HOSTILE.test(value)) return null;
  if (name === "style" && /url\s*\(/i.test(value)) return null; // no image or font fetch from CSS at all
  if (name !== "style" && !onlyLocalUrls(value)) return null;
  return value.replace(/"/g, "&quot;");
}

const TOKEN =
  /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<[!?][^>]*>|<\/\s*([A-Za-z][A-Za-z0-9:-]*)\s*>|<([A-Za-z][A-Za-z0-9:-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>|[^<]+|</g;
const ATTR = /([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;

function attributes(raw) {
  const out = [];
  let removed = 0;
  for (const m of raw.matchAll(ATTR)) {
    const name = m[1].toLowerCase();
    const value = m[2] ?? m[3] ?? m[4] ?? "";
    if (!ATTRS.has(name)) {
      removed++;
      continue;
    }
    const clean = cleanValue(name, value);
    if (clean === null) {
      removed++;
      continue;
    }
    out.push(` ${name}="${clean}"`);
  }
  return { text: out.join(""), removed };
}

/** @returns {{html: string, removed: number}} removed counts every dropped tag, attribute and comment. */
export function sanitizeHtml(input) {
  const out = [];
  const stack = [];
  let removed = 0;
  let skipping = null; // {name, depth}
  for (const m of String(input).matchAll(TOKEN)) {
    const [tok, closeName, openName, rawAttrs] = m;
    if (skipping) {
      const n = (closeName ?? openName ?? "").toLowerCase();
      if (n === skipping.name) {
        if (closeName) {
          if (--skipping.depth === 0) skipping = null;
        } else if (!/\/\s*$/.test(rawAttrs ?? "")) skipping.depth++;
      }
      continue;
    }
    if (closeName) {
      const n = closeName.toLowerCase();
      const at = stack.lastIndexOf(n);
      if (at === -1) {
        if (!TAGS.has(n)) removed++;
        continue;
      }
      while (stack.length > at) out.push(`</${stack.pop()}>`);
      continue;
    }
    if (openName) {
      const n = openName.toLowerCase();
      if (DROP_WITH_CONTENT.has(n)) {
        removed++;
        if (!/\/\s*$/.test(rawAttrs ?? "")) skipping = { name: n, depth: 1 };
        continue;
      }
      if (!TAGS.has(n)) {
        removed++;
        continue;
      }
      const a = attributes(rawAttrs ?? "");
      removed += a.removed;
      if (VOID.has(n)) out.push(n === "br" || n === "hr" ? `<${n}${a.text}>` : `<${n}${a.text}/>`);
      else if (/\/\s*$/.test(rawAttrs ?? "")) out.push(`<${n}${a.text}></${n}>`);
      else {
        out.push(`<${n}${a.text}>`);
        stack.push(n);
      }
      continue;
    }
    if (tok.startsWith("<!") || tok.startsWith("<?")) {
      removed++;
      continue;
    }
    // Text: a stray `<` never starts a tag in the output.
    out.push(tok === "<" ? "&lt;" : tok);
  }
  while (stack.length) out.push(`</${stack.pop()}>`);
  return { html: out.join(""), removed };
}

/** The visible text of a visual, for the gates. */
export function visibleText(html) {
  return String(html)
    .replace(/<(script|style)[\s\S]*?<\/\1\s*>/gi, " ")
    .replace(/<[^>]*>/g, " ")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&#0*39;/g, "'");
}
