/**
 * The code-in-reply gates. A reply is the model's whole answer, so a solution has nowhere to live in
 * the schema; these gates are the floor under the persona rule, not a proof ("crude on purpose": a
 * pointer trace of a correct algorithm reveals the approach and no pattern here can see that). On any
 * hit the door substitutes its fixed fallback and logs the reason, never the reply.
 */

import { visibleText } from "./sanitize.js";

/** Consecutive tokens of the owner's code that a reply may not repeat (a trace shows values, not a rewrite). */
export const COPY_RUN = 12;
const INLINE_CODE_MAX = 40;

// Statement shapes of the languages the owner solves in. Matched on visible text only.
const STATEMENT_SHAPES = [
  [/\bdef\s+\w+\s*\(/, "stmt_def"],
  [/\bclass\s+[A-Z]\w*\s*[:({]/, "stmt_class"],
  [/\bfunction\s*\w*\s*\([^)]*\)\s*\{/, "stmt_function"],
  [/\bfor\s*\([^)]*\)\s*\{?/, "stmt_for_paren"],
  [/\bfor\s+\w+(?:\s*,\s*\w+)*\s+in\s+[^\n:]+:/, "stmt_for_in"],
  [/\bwhile\s*\([^)]*\)\s*\{/, "stmt_while"],
  [/\bif\s*\([^)]*\)\s*\{/, "stmt_if"],
  [/\breturn\b[^\n;]{1,}\s*;/, "stmt_return"],
  [/=>\s*[{(]/, "stmt_arrow"],
  [/\b(?:int|long|auto|var|let|const)\s+\w+\s*=\s*[^;\n]+;/, "stmt_decl"],
];

function tokens(s) {
  return String(s).match(/[A-Za-z_]\w*|\d+(?:\.\d+)?|[^\sA-Za-z_\d]/g) ?? [];
}

function sharesRun(a, b) {
  // Greedy window match over the owner's code: enough for a floor, linear in the reply's size.
  const index = new Map();
  for (let i = 0; i + COPY_RUN <= b.length; i++) {
    const key = b.slice(i, i + COPY_RUN).join("\u0001");
    index.set(key, true);
  }
  for (let i = 0; i + COPY_RUN <= a.length; i++) {
    if (index.has(a.slice(i, i + COPY_RUN).join("\u0001"))) return true;
  }
  return false;
}

/**
 * @param {{text: string, visual?: {html: string}}} reply the validated reply (visual html is the RAW one)
 * @param {string} ownerCode the owner's current code from the page
 * @returns {string|null} the reject reason, or null when the reply passes
 */
export function codeInReply(reply, ownerCode = "") {
  const html = reply.visual?.html ?? "";
  const shown = `${reply.text}\n${visibleText(html)}`;

  if (/```|~~~/.test(reply.text) || /```|~~~/.test(html)) return "fenced_code";
  for (const m of reply.text.matchAll(/`([^`]*)`/g)) {
    if (m[1].length > INLINE_CODE_MAX || /\n/.test(m[1])) return "inline_code_long";
  }
  for (const m of html.matchAll(/<(pre|code|samp|kbd|tt)\b[^>]*>([\s\S]*?)<\/\1\s*>/gi)) {
    const body = visibleText(m[2]).trim();
    if (m[1].toLowerCase() === "pre" && (body.length > INLINE_CODE_MAX || /\n/.test(body))) return "pre_block";
    if (body.length > INLINE_CODE_MAX || /\n/.test(body)) return "code_long";
  }
  if (/<pre\b/i.test(html) && /\n/.test(visibleText(html))) return "pre_block";

  for (const [re, why] of STATEMENT_SHAPES) if (re.test(shown)) return why;

  if (ownerCode && sharesRun(tokens(shown), tokens(ownerCode))) return "copies_owner_code";
  return null;
}
