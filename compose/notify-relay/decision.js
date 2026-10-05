/**
 * notify-relay decision contract — the typed, owner-facing push for a decision.
 *
 * Pure: no I/O, no env, no clock. `buildDecision(input, links)` turns what a
 * producer knows (project, the ask, the options) into the contract payload
 * (docs/decision-contract.md); `renderDecision(payload)` turns that payload into
 * Telegram HTML. The dashboard decision page consumes the same payload, so the
 * schema below is the one definition (mirrored in docs/decision-contract.schema.json,
 * a test keeps the two equal).
 */

import { escapeHtml } from "./render.js";

export const KINDS = Object.freeze(["decision", "blocked", "done", "info"]);

// The severity word that opens every title, so the lock screen says how loud it is.
export const SEVERITY_WORDS = Object.freeze({
  decision: "Decision",
  blocked: "Blocked",
  done: "Done",
  info: "FYI",
});

export const NO_ACTION_BODY = "no action needed";

export const LIMITS = Object.freeze({
  project: 64,
  ask: 200,
  decisionId: 64,
  optionLabel: 120,
  options: 6,
  boardId: 64,
});

const ID = "^[A-Za-z0-9][A-Za-z0-9._-]*$";

export const DECISION_SCHEMA = Object.freeze({
  $schema: "https://json-schema.org/draft/2020-12/schema",
  title: "notify-relay decision payload (v1)",
  type: "object",
  additionalProperties: false,
  required: [
    "kind",
    "project",
    "title",
    "body",
    "decision_id",
    "options",
    "free_text_allowed",
    "link",
  ],
  properties: {
    kind: { enum: [...KINDS] },
    project: { type: "string", minLength: 1, maxLength: LIMITS.project },
    title: { type: "string", minLength: 1 },
    body: { type: "string", minLength: 1 },
    decision_id: { type: "string", pattern: ID, maxLength: LIMITS.decisionId },
    options: {
      type: "array",
      maxItems: LIMITS.options,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["id", "label", "recommended"],
        properties: {
          id: { type: "string", minLength: 1 },
          label: { type: "string", minLength: 1, maxLength: LIMITS.optionLabel },
          recommended: { type: "boolean" },
        },
      },
    },
    free_text_allowed: { type: "boolean" },
    link: { type: "string", minLength: 1 },
  },
});

// A title is plain words: the project plus the ask. These are the internal
// terms that must never reach a phone — task ids, branches, status prefixes.
const FORBIDDEN_IN_TITLE = [
  [/\b[A-Za-z]+-\d+\b/, "a task or issue id"],
  [/\b(?:fm|fix|feat|chore|docs|refactor)\/[\w.-]+/i, "a branch name"],
  [/\b[0-9a-f]{7,}\b/i, "a hash or opaque id"],
  [/^\s*(?:status|state|working|blocked|done|failed|paused|needs[- ]decision)\s*[:\-\]]/i,
    "a status prefix"],
  [/\[at=\d+\]/i, "a status stamp"],
];

const text = (v) => (v == null ? "" : String(v).trim());

/** Validate producer input. Returns a list of problems; empty means valid. */
export function validateDecisionInput(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    return ["decision must be a JSON object"];
  }
  const problems = [];
  if (!KINDS.includes(input.kind)) problems.push(`unknown kind '${input.kind}' (${KINDS.join(" | ")})`);
  for (const [field, limit] of [["project", LIMITS.project], ["ask", LIMITS.ask]]) {
    const value = text(input[field]);
    if (!value) problems.push(`missing '${field}'`);
    else if ([...value].length > limit) problems.push(`'${field}' is over ${limit} characters`);
    else if (field === "ask" || field === "project") {
      for (const [re, what] of FORBIDDEN_IN_TITLE) {
        if (re.test(value)) problems.push(`'${field}' contains ${what}; the title is plain words`);
      }
    }
  }
  const id = text(input.decision_id);
  if (!id) problems.push("missing 'decision_id'");
  else if (!new RegExp(ID).test(id) || id.length > LIMITS.decisionId) {
    problems.push(`'decision_id' must match ${ID} and be at most ${LIMITS.decisionId} characters`);
  }
  const board = text(input.board_id);
  if (board && (!new RegExp(ID).test(board) || board.length > LIMITS.boardId)) {
    problems.push(`'board_id' must match ${ID} and be at most ${LIMITS.boardId} characters`);
  }
  if (input.options != null) {
    if (!Array.isArray(input.options)) problems.push("'options' must be a list");
    else {
      if (input.options.length > LIMITS.options) problems.push(`'options' has more than ${LIMITS.options} entries`);
      input.options.forEach((o, i) => {
        const label = text(typeof o === "string" ? o : o?.label);
        if (!label) problems.push(`options[${i}] needs a 'label'`);
        else if ([...label].length > LIMITS.optionLabel) problems.push(`options[${i}].label is over ${LIMITS.optionLabel} characters`);
      });
      const recommended = input.options.filter((o) => o && typeof o === "object" && o.recommended === true);
      if (recommended.length > 1) problems.push("at most one option may be recommended");
    }
  }
  return problems;
}

/** The title: severity word first, then the project and the ask. */
export function buildTitle(kind, project, ask) {
  return `${SEVERITY_WORDS[kind]}: ${text(project)} — ${text(ask)}`;
}

/**
 * The link the owner taps: the board when one exists, else the dashboard
 * decision page. `links` = { boardBaseUrl, dashboardBaseUrl } (no trailing slash
 * needed); an unset dashboard base yields the bare route, which the dashboard
 * resolves against its own origin.
 */
export function buildLink(decisionId, boardId, links = {}) {
  const trim = (u) => text(u).replace(/\/+$/, "");
  const board = trim(links.boardBaseUrl);
  if (text(boardId) && board) return `${board}/session/${encodeURIComponent(text(boardId))}`;
  return `${trim(links.dashboardBaseUrl)}/decisions/${encodeURIComponent(text(decisionId))}`;
}

// Recommended option first; otherwise the producer's order is kept.
function orderOptions(options) {
  const list = (options ?? []).map((o, i) => {
    const obj = typeof o === "string" ? { label: o } : o;
    return {
      id: text(obj.id) || String(i + 1),
      label: text(obj.label),
      recommended: obj.recommended === true,
    };
  });
  return [...list.filter((o) => o.recommended), ...list.filter((o) => !o.recommended)];
}

export function buildBody(options) {
  if (!options.length) return NO_ACTION_BODY;
  return options
    .map((o, i) => `${i + 1}. ${o.label}${o.recommended ? " (recommended)" : ""}`)
    .join("\n");
}

/** Producer input (already validated) → the contract payload. */
export function buildDecision(input, links = {}) {
  const options = orderOptions(input.options);
  const kind = input.kind;
  return {
    kind,
    project: text(input.project),
    title: buildTitle(kind, input.project, input.ask),
    body: buildBody(options),
    decision_id: text(input.decision_id),
    options,
    free_text_allowed: input.free_text_allowed === true,
    link: buildLink(input.decision_id, input.board_id, links),
  };
}

/** The contract payload as Telegram HTML: bold title, body, one tap-through link. */
export function renderDecision(payload) {
  const lines = [`<b>${escapeHtml(payload.title)}</b>`, escapeHtml(payload.body)];
  if (payload.free_text_allowed) lines.push("You can also reply in your own words.");
  lines.push(`<a href="${escapeHtml(payload.link).replaceAll('"', "&quot;")}">Open to answer</a>`);
  return lines.join("\n");
}
