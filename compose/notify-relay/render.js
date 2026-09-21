/**
 * notify-relay renderer — the one place a Telegram message is composed.
 *
 * Pure: no I/O, no env, no clock. `render(envelope)` turns a message envelope
 * (docs/message-format.md) into Telegram HTML; the compatibility mappers turn
 * the legacy /devclaw task row and /text payloads into envelopes so every
 * producer goes through the same grammar.
 *
 * Telegram entity rules honoured here: every interpolated value is escaped
 * (& < >); <code> never contains another tag; blockquotes never nest.
 */

export const LEVELS = Object.freeze({
  act: { glyph: "🔴", action: true },
  wait: { glyph: "🟡", action: true },
  good: { glyph: "🟢", action: false },
  info: { glyph: "▪️", action: false },
});

export const REQUIRED_FIELDS = Object.freeze(["level", "source", "subject", "headline"]);

// Telegram rejects text over 4096 characters; keep the relay's headroom.
export const MAX_MSG_CHARS = 3500;

const TRUNCATION_MARK = "…";

// Fields the renderer never cuts are bounded at validation instead, counted in
// characters after HTML escaping (what render() emits). Worst case with body
// and detail cut to nothing: line 1 is 23 + 64 + 128 + 800 = 1015; links
// 1 + 3 × (15 + 100 + 500) + 2 × 3 = 1852; action 1 + 15 + 400 = 416;
// 1015 + 1852 + 416 = 3283 < MAX_MSG_CHARS.
export const FIELD_LIMITS = Object.freeze({
  source: 64,
  subject: 128,
  headline: 800,
  action: 400,
  linkText: 100,
  linkUrl: 500,
});
export const MAX_LINKS = 3;

export function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll('"', "&quot;");
}

function text(value) {
  return value == null ? "" : String(value);
}

/**
 * Validate an envelope. Returns a list of problems; empty means valid.
 */
export function validateEnvelope(envelope) {
  const problems = [];
  if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) {
    return ["envelope must be a JSON object"];
  }
  for (const field of REQUIRED_FIELDS) {
    const value = envelope[field];
    if (typeof value !== "string" || value.trim() === "") {
      problems.push(`missing '${field}'`);
    }
  }
  if (typeof envelope.level === "string" && !Object.hasOwn(LEVELS, envelope.level)) {
    problems.push(`unknown level '${envelope.level}' (act | wait | good | info)`);
  }
  const tooLong = (name, rendered, limit) => {
    if (rendered.length > limit) {
      problems.push(`'${name}' is ${rendered.length} characters escaped; limit ${limit}`);
    }
  };
  for (const field of ["source", "subject", "headline"]) {
    if (typeof envelope[field] === "string") {
      tooLong(field, escapeHtml(envelope[field]), FIELD_LIMITS[field]);
    }
  }
  tooLong("action", escapeHtml(text(envelope.action).trim()), FIELD_LIMITS.action);
  if (Array.isArray(envelope.links)) {
    if (envelope.links.length > MAX_LINKS) {
      problems.push(`'links' has ${envelope.links.length} entries; limit ${MAX_LINKS}`);
    }
    envelope.links.forEach((link, i) => {
      if (typeof link?.text === "string") {
        tooLong(`links[${i}].text`, escapeHtml(link.text), FIELD_LIMITS.linkText);
      }
      if (typeof link?.url === "string") {
        tooLong(`links[${i}].url`, escapeAttr(link.url), FIELD_LIMITS.linkUrl);
      }
    });
  }
  return problems;
}

function renderLinks(links) {
  if (!Array.isArray(links)) return "";
  return links
    .filter((l) => l && typeof l.url === "string" && l.url && typeof l.text === "string" && l.text)
    .map((l) => `<a href="${escapeAttr(l.url)}">${escapeHtml(l.text)}</a>`)
    .join(" · ");
}

// Cut off the end of a string on code-point boundaries, so an emoji is never
// split into a lone surrogate. `over` is a count of rendered characters, which
// escaping inflates beyond the field's own length, so a pass never cuts more
// than half of what is left: the caller's loop re-measures and cuts again.
// Every pass removes at least one code point, so it terminates.
function shrink(value, over) {
  const chars = [...value];
  const room = chars.length - TRUNCATION_MARK.length;
  if (room <= 0) return "";
  const cut = Math.min(Math.max(over, 1), Math.ceil(room / 2));
  const keep = room - cut;
  return keep > 0 ? chars.slice(0, keep).join("") + TRUNCATION_MARK : "";
}

/**
 * Render an envelope to Telegram HTML. The envelope must already have passed
 * validateEnvelope: `level` is one of the four, and the required fields are
 * non-empty strings.
 *
 * Layout (docs/message-format.md, "Rendering rules"):
 *   1. <glyph> <b>source</b> · <b>subject</b> — headline      (always, never cut)
 *   2. body                                                    (optional)
 *   3. <blockquote expandable>detail</blockquote>              (optional, collapsed)
 *   4. links                                                   (optional)
 *   5. → <code>action</code>                                   (act / wait only)
 */
export function render(envelope) {
  const level = LEVELS[envelope.level];
  const headline =
    `${level.glyph} <b>${escapeHtml(text(envelope.source))}</b> · ` +
    `<b>${escapeHtml(text(envelope.subject))}</b> — ${escapeHtml(text(envelope.headline))}`;
  const links = renderLinks(envelope.links);
  const action = level.action && text(envelope.action).trim()
    ? `→ <code>${escapeHtml(text(envelope.action).trim())}</code>`
    : "";

  let body = text(envelope.body).trim();
  let detail = text(envelope.detail).trim();

  const build = () =>
    [
      headline,
      body ? escapeHtml(body) : "",
      detail ? `<blockquote expandable>${escapeHtml(detail)}</blockquote>` : "",
      links,
      action,
    ]
      .filter(Boolean)
      .join("\n");

  // Truncate before Telegram does: detail first, then body, never the headline.
  // Each pass strictly shortens the field, so both loops end either under the
  // cap or with the field emptied.
  let message = build();
  while (message.length > MAX_MSG_CHARS && detail) {
    detail = shrink(detail, message.length - MAX_MSG_CHARS);
    message = build();
  }
  while (message.length > MAX_MSG_CHARS && body) {
    body = shrink(body, message.length - MAX_MSG_CHARS);
    message = build();
  }
  return message;
}

// ---- compatibility mappers (POST /devclaw, POST /text) ----------------------

const STATUS_LEVEL = { done: "good", failed: "act" };

// A code point escapes to at most 5 characters (&amp;), so these clips keep the
// mapped subject (16 × 5 + 1 + 8 × 5 = 121) and headline (64 × 5 = 320) within
// FIELD_LIMITS.
function clip(value, codePoints) {
  return [...value].slice(0, codePoints).join("");
}

/**
 * A devclaw task row (POST /devclaw) as an envelope: level from `status`
 * (done → good, failed → act, else info); the goal is the body; a failure's
 * error is the collapsed detail; a done task's result message joins the body.
 */
export function envelopeFromDevclawRow(row) {
  const status = text(row?.status) || "unknown";
  const kind = clip(text(row?.kind) || "task", 16);
  const taskId = clip(text(row?.task_id) || "?", 8);
  const body = [text(row?.goal).slice(0, 240)];
  let detail = "";

  if (status === "failed" && row?.error) {
    detail = text(row.error).slice(0, 600);
  } else if (status === "done" && row?.result_json) {
    try {
      const parsed =
        typeof row.result_json === "string" ? JSON.parse(row.result_json) : row.result_json;
      if (parsed?.message) body.push(text(parsed.message).slice(0, 400));
    } catch {
      // result_json wasn't JSON; skip
    }
  }

  return {
    level: Object.hasOwn(STATUS_LEVEL, status) ? STATUS_LEVEL[status] : "info",
    source: "devclaw",
    subject: `${kind} ${taskId}`,
    headline: clip(status, 64),
    body: body.filter(Boolean).join("\n\n"),
    detail,
  };
}

// A /text producer composes its own string and can hand over a single line of
// any length, but render() never truncates a headline. So the headline is
// bounded here, where the envelope is built, and the overflow spills into the
// body — which render() does truncate. 160 × 5 escaped stays within
// FIELD_LIMITS.headline.
export const MAX_TEXT_HEADLINE_CHARS = 160;

// Split an over-long first line into a headline and its overflow, preferring
// the last word boundary in the second half of the bound.
function splitHeadline(line) {
  const chars = [...line];
  if (chars.length <= MAX_TEXT_HEADLINE_CHARS) return [line, ""];
  const head = chars.slice(0, MAX_TEXT_HEADLINE_CHARS).join("");
  const rest = chars.slice(MAX_TEXT_HEADLINE_CHARS).join("");
  const space = head.lastIndexOf(" ");
  if (space >= head.length / 2) return [head.slice(0, space), `${head.slice(space + 1)}${rest}`];
  return [head, rest];
}

/**
 * Free text (POST /text, the devclaw goal layer) as an `info` envelope: the
 * first line is the headline, the rest the body. Escaping happens in render.
 */
export function envelopeFromText(value) {
  const trimmed = text(value).trim();
  const newline = trimmed.indexOf("\n");
  const firstLine = newline === -1 ? trimmed : trimmed.slice(0, newline);
  const remainder = newline === -1 ? "" : trimmed.slice(newline + 1);
  const [headline, spill] = splitHeadline(firstLine);
  const body = [spill, remainder].filter(Boolean).join("\n");
  return { level: "info", source: "devclaw", subject: "goal", headline, body };
}
