/**
 * render.js — the one renderer for the notify envelope (v1).
 *
 * Producers span four languages, so they share a wire format rather than a
 * library: they POST an envelope, this module turns it into Telegram HTML.
 * See docs/notify-envelope.md for the producer-facing contract.
 */

export const MAX_MSG_CHARS = 3500; // Telegram is 4096; leave headroom

export const LEVELS = {
  act: { glyph: "🔴", actionable: true },
  wait: { glyph: "🟡", actionable: true },
  good: { glyph: "🟢", actionable: false },
  info: { glyph: "▪️", actionable: false },
};

// Rendered (post-escape) caps. Line 1, links and action are bounded so that
// what is left for body/detail is always positive.
const CAP = {
  source: 48,
  subject: 96,
  headline: 200,
  action: 256,
  linkText: 96,
  linkUrl: 300,
  body: 1200,
};
const MAX_LINKS = 3;
const SEP = "\n\n";

function escape(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// slice() cuts by UTF-16 code unit, so it can leave the high half of an emoji
// behind; JSON.stringify then emits a lone `\ud83d` and the Bot API refuses the
// whole message. Input is normalised first (see escapeClipped), so by here the
// only lone surrogate possible is a high one this clip just created.
function dropLoneSurrogate(text) {
  const last = text.charCodeAt(text.length - 1);
  return last >= 0xd800 && last <= 0xdbff ? text.slice(0, -1) : text;
}

/**
 * Escape `raw`, shrinking it until the ESCAPED form fits `max` characters.
 * Clipping escaped HTML directly would cut `&amp;` in half and Telegram then
 * rejects the whole message, so the raw string is what gets shortened.
 */
export function escapeClipped(raw, max) {
  // A producer can hand us a lone surrogate it never clipped — Python's
  // `surrogatepass` does exactly that — and one anywhere in the payload makes
  // the Bot API reject the send. Fold those to U+FFFD once, on the way in, so
  // everything downstream is well-formed.
  const text = String(raw).toWellFormed();
  const full = escape(text);
  if (full.length <= max) return full;
  if (max <= 1) return "…".slice(0, max);
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (escape(text.slice(0, mid)).length <= max - 1) lo = mid;
    else hi = mid - 1;
  }
  return escape(dropLoneSurrogate(text.slice(0, lo)).trimEnd()) + "…";
}

// Line 1 has to stand alone on a lock screen, so the fields that compose it —
// and `action`, which is one tap-to-copy command — are folded to a single line.
// A Grafana-style summary carrying a newline would otherwise forge a new block.
function oneLine(text) {
  return String(text).replace(/\s+/g, " ").trim();
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.trim() !== "";
}

// An optional field a producer chose not to fill. Python's `json` turns a
// missing value into `null`, not a missing key, and devclaw is Python — so
// `null` has to mean "absent" or every producer needs a pruning step.
function isAbsent(value) {
  return value === undefined || value === null;
}

function validateLink(link, index, errors) {
  if (typeof link !== "object" || link === null || Array.isArray(link)) {
    errors.push(`links[${index}] must be an object`);
    return;
  }
  if (!isNonEmptyString(link.text)) {
    errors.push(`links[${index}].text must be a non-empty string`);
  }
  if (!isNonEmptyString(link.url)) {
    errors.push(`links[${index}].url must be a non-empty string`);
    return;
  }
  let parsed;
  try {
    parsed = new URL(link.url);
  } catch {
    errors.push(`links[${index}].url is not a valid URL`);
    return;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    errors.push(`links[${index}].url must be http or https`);
    return;
  }
  // Measured on the same normalised form the renderer emits, and rejected
  // rather than clipped: half a URL is a link that silently 404s.
  if (escape(parsed.href).length > CAP.linkUrl) {
    errors.push(`links[${index}].url must be at most ${CAP.linkUrl} characters`);
  }
}

/** Returns a list of human-readable problems; empty means the envelope is valid. */
export function validateEnvelope(envelope) {
  const errors = [];
  if (
    typeof envelope !== "object" ||
    envelope === null ||
    Array.isArray(envelope)
  ) {
    return ["envelope must be a JSON object"];
  }

  if (!Object.prototype.hasOwnProperty.call(LEVELS, envelope.level)) {
    errors.push(`level must be one of ${Object.keys(LEVELS).join(", ")}`);
  }
  for (const field of ["source", "subject", "headline"]) {
    if (!isNonEmptyString(envelope[field])) {
      errors.push(`${field} is required and must be a non-empty string`);
    }
  }
  for (const field of ["body", "detail", "action"]) {
    if (!isAbsent(envelope[field]) && typeof envelope[field] !== "string") {
      errors.push(`${field} must be a string when present`);
    }
  }

  if (!isAbsent(envelope.links)) {
    if (!Array.isArray(envelope.links)) {
      errors.push("links must be an array");
    } else if (envelope.links.length > MAX_LINKS) {
      errors.push(`links accepts at most ${MAX_LINKS} entries`);
    } else {
      envelope.links.forEach((link, i) => validateLink(link, i, errors));
    }
  }

  return errors;
}

function assemble(headLine, body, detail, tail) {
  const parts = [headLine];
  if (body) parts.push(body);
  if (detail) parts.push(`<blockquote expandable>${detail}</blockquote>`);
  parts.push(...tail);
  return parts.join(SEP);
}

/**
 * Render a VALIDATED envelope as Telegram HTML. Call validateEnvelope first —
 * this assumes the shape is already good.
 */
export function renderEnvelope(envelope) {
  const level = LEVELS[envelope.level];

  const headLine =
    `${level.glyph} <b>${escapeClipped(oneLine(envelope.source), CAP.source)}</b>` +
    ` · <b>${escapeClipped(oneLine(envelope.subject), CAP.subject)}</b>` +
    ` — ${escapeClipped(oneLine(envelope.headline), CAP.headline)}`;

  const tail = [];
  const links = Array.isArray(envelope.links) ? envelope.links : [];
  if (links.length) {
    tail.push(
      links
        .map(
          (link) =>
            // The parsed form, not the raw string: URL parsing strips the
            // tabs and newlines that would otherwise land inside href="…",
            // and percent-encodes the rest. validateEnvelope already proved
            // this parses.
            `<a href="${escapeClipped(new URL(link.url).href, CAP.linkUrl)}">` +
            `${escapeClipped(oneLine(link.text), CAP.linkText)}</a>`,
        )
        .join(" · "),
    );
  }
  // Rule: `action` is last, tap-to-copy, and only for the levels that ask the
  // reader to do something. A `good` message that repeats the firing
  // remediation is the 3am bug this drops in the renderer, not in producers.
  if (level.actionable && isNonEmptyString(envelope.action)) {
    tail.push(
      `→ <code>${escapeClipped(oneLine(envelope.action), CAP.action)}</code>`,
    );
  }

  const body = isNonEmptyString(envelope.body)
    ? escapeClipped(envelope.body, CAP.body)
    : "";

  // Every other field is capped, and those caps sum well under MAX_MSG_CHARS, so
  // `detail` can always be given whatever is left rather than being dropped:
  // `<blockquote expandable></blockquote>` plus its separator is the overhead a
  // detail block costs on top of what is already rendered.
  const fixedLen = assemble(headLine, body, "", tail).length;
  const detail = isNonEmptyString(envelope.detail)
    ? escapeClipped(
        envelope.detail,
        MAX_MSG_CHARS -
          fixedLen -
          SEP.length -
          "<blockquote expandable></blockquote>".length,
      )
    : "";

  return assemble(headLine, body, detail, tail);
}
