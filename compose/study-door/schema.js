/**
 * The two wire shapes: the panel's request and the model's `reply` call.
 * Both are closed: an unknown key is a reject, so a `code` field can never
 * ride along in a reply and the panel can never smuggle a field into the prompt.
 */

export const REPLY_LIMITS = Object.freeze({ text: 280, html: 4000, label: 24, options: 4 });
export const PAGE_LIMITS = Object.freeze({
  url: 300,
  title: 200,
  problem: 20_000,
  code: 20_000,
  language: 32,
  selection: 4_000,
  word: 80,
});

const EXPECTS = new Set(["tap", "word", "none"]);
const OPTION_ID = /^[A-Za-z0-9_-]{1,16}$/;
// Control characters other than newline and tab never belong in a reply or a request.
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;

const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const closed = (obj, keys) => Object.keys(obj).every((k) => keys.includes(k));
const str = (v, max, min = 0) => typeof v === "string" && v.length >= min && v.length <= max && !CONTROL.test(v);

/** The one function the model may call. Sent on every request by the door, never by the panel. */
export const REPLY_TOOL = Object.freeze({
  type: "function",
  function: {
    name: "reply",
    description:
      "Your whole answer to the owner: one short plain-text message, an optional small static visual, " +
      "and how the owner answers. There is no field for code and none may appear in text or visual.",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["text", "expects"],
      properties: {
        text: { type: "string", maxLength: REPLY_LIMITS.text, description: "Plain text, no markup." },
        visual: {
          type: "object",
          additionalProperties: false,
          required: ["html"],
          properties: {
            html: {
              type: "string",
              maxLength: REPLY_LIMITS.html,
              description: "Static HTML with inline CSS and inline SVG only. No script, no links, no images.",
            },
          },
        },
        expects: { type: "string", enum: ["tap", "word", "none"] },
        options: {
          type: "array",
          maxItems: REPLY_LIMITS.options,
          items: {
            type: "object",
            additionalProperties: false,
            required: ["id", "label"],
            properties: {
              id: { type: "string", pattern: "^[A-Za-z0-9_-]{1,16}$" },
              label: { type: "string", maxLength: REPLY_LIMITS.label },
            },
          },
        },
      },
    },
  },
});

/** @returns {{ok: true, value: object} | {ok: false, reason: string}} */
export function validateReply(r) {
  if (!isObject(r)) return { ok: false, reason: "reply_not_object" };
  if (!closed(r, ["text", "visual", "expects", "options"])) return { ok: false, reason: "reply_unknown_field" };
  if (!str(r.text, REPLY_LIMITS.text, 1)) return { ok: false, reason: "reply_text" };
  if (!EXPECTS.has(r.expects)) return { ok: false, reason: "reply_expects" };
  const value = { text: r.text, expects: r.expects };
  if (r.visual !== undefined) {
    if (!isObject(r.visual) || !closed(r.visual, ["html"])) return { ok: false, reason: "reply_visual" };
    if (!str(r.visual.html, REPLY_LIMITS.html, 1)) return { ok: false, reason: "reply_visual" };
    value.visual = { html: r.visual.html };
  }
  const options = r.options ?? [];
  if (!Array.isArray(options) || options.length > REPLY_LIMITS.options) return { ok: false, reason: "reply_options" };
  const seen = new Set();
  const clean = [];
  for (const o of options) {
    if (!isObject(o) || !closed(o, ["id", "label"])) return { ok: false, reason: "reply_options" };
    if (typeof o.id !== "string" || !OPTION_ID.test(o.id) || seen.has(o.id)) return { ok: false, reason: "reply_options" };
    if (!str(o.label, REPLY_LIMITS.label, 1)) return { ok: false, reason: "reply_options" };
    seen.add(o.id);
    clean.push({ id: o.id, label: o.label });
  }
  // Options are the tap buttons: required for a tap, meaningless for anything else.
  if (r.expects === "tap" ? clean.length === 0 : clean.length > 0) return { ok: false, reason: "reply_options" };
  value.options = clean;
  return { ok: true, value };
}

/** @returns {{ok: true, value: object} | {ok: false, reason: string}} */
export function validateTurn(b) {
  if (!isObject(b) || !closed(b, ["v", "input", "page"])) return { ok: false, reason: "turn_shape" };
  if (b.v !== 1) return { ok: false, reason: "turn_version" };
  const i = b.input;
  if (!isObject(i) || !closed(i, ["kind", "id", "text"])) return { ok: false, reason: "turn_input" };
  const input = { kind: i.kind };
  if (i.kind === "open") {
    if (i.id !== undefined || i.text !== undefined) return { ok: false, reason: "turn_input" };
  } else if (i.kind === "tap") {
    if (typeof i.id !== "string" || !OPTION_ID.test(i.id) || i.text !== undefined) return { ok: false, reason: "turn_input" };
    input.id = i.id;
  } else if (i.kind === "word") {
    if (!str(i.text, PAGE_LIMITS.word, 1) || i.id !== undefined) return { ok: false, reason: "turn_input" };
    input.text = i.text;
  } else {
    return { ok: false, reason: "turn_input" };
  }
  const p = b.page;
  if (!isObject(p) || !closed(p, ["url", "title", "problem", "code", "language", "selection"])) {
    return { ok: false, reason: "turn_page" };
  }
  if (!str(p.url, PAGE_LIMITS.url, 1) || !str(p.problem, PAGE_LIMITS.problem, 1) || !str(p.code, PAGE_LIMITS.code)) {
    return { ok: false, reason: "turn_page" };
  }
  let host;
  try {
    const u = new URL(p.url);
    if (u.protocol !== "https:") return { ok: false, reason: "turn_page" };
    host = u.hostname;
  } catch {
    return { ok: false, reason: "turn_page" };
  }
  for (const [k, max] of [
    ["title", PAGE_LIMITS.title],
    ["language", PAGE_LIMITS.language],
    ["selection", PAGE_LIMITS.selection],
  ]) {
    if (p[k] !== undefined && !str(p[k], max)) return { ok: false, reason: "turn_page" };
  }
  const page = { url: p.url, host, problem: p.problem, code: p.code };
  for (const k of ["title", "language", "selection"]) if (p[k] !== undefined) page[k] = p[k];
  return { ok: true, value: { input, page } };
}
