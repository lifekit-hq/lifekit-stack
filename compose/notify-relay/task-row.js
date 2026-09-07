/**
 * task-row.js — devclaw's legacy task-row callback, expressed as a v1 envelope.
 *
 * `/devclaw` predates the envelope and its callers are deployed separately from
 * this relay, so the row is mapped here rather than at the producer. The relay
 * then has exactly one renderer even for producers that have not migrated
 * (spec 001, US2). Pure mapping — no I/O — so it lives beside render.js rather
 * than in server.js.
 */

// A task row carries no command to run, so no envelope built here has an
// `action`. Anything outside this table is progress, not a call to act.
const STATUS_LEVEL = {
  done: "good",
  failed: "act",
};

const SHORT_ID_CHARS = 8;

// devclaw serialises result_json as a JSON string on some versions and hands
// back an already-parsed object on others.
function resultMessage(resultJson) {
  if (typeof resultJson !== "string") {
    return typeof resultJson?.message === "string" ? resultJson.message : "";
  }
  try {
    const parsed = JSON.parse(resultJson);
    return typeof parsed?.message === "string" ? parsed.message : "";
  } catch {
    return "";
  }
}

// The first line shows without a tap; whatever follows is the log, and `detail`
// renders it collapsed. The legacy formatter pasted 600 characters of traceback
// inline instead, which is what made the channel unreadable.
function splitFirstLine(text) {
  const trimmed = String(text ?? "").trim();
  const cut = trimmed.indexOf("\n");
  if (cut === -1) return { body: trimmed, detail: "" };
  return {
    body: trimmed.slice(0, cut).trim(),
    detail: trimmed.slice(cut + 1).trim(),
  };
}

function field(value, fallback) {
  const text = String(value ?? "").trim();
  return text === "" ? fallback : text;
}

/** Map a devclaw task row onto a v1 envelope. Always valid per validateEnvelope. */
export function envelopeFromTaskRow(row) {
  const status = field(row?.status, "unknown");
  const kind = field(row?.kind, "task");
  const taskId = field(row?.task_id, "?");
  const goal = field(row?.goal, "");
  // By code point, not by slice(): a task_id is a hex uuid in practice, but
  // cutting one mid-emoji leaves a lone surrogate that renders as U+FFFD.
  const idChars = [...taskId];
  const shortId =
    idChars.length > SHORT_ID_CHARS
      ? `${idChars.slice(0, SHORT_ID_CHARS).join("")}…`
      : taskId;

  // `error` wins on a failure; every other status reports whatever devclaw put
  // in result_json — including the in-flight ones the legacy formatter left
  // blank, which is where a `running` row's progress message now shows up.
  const { body, detail } = splitFirstLine(
    status === "failed" ? row?.error : resultMessage(row?.result_json),
  );

  return {
    // hasOwnProperty, not a bare lookup: `status` is producer-controlled and
    // "constructor" would otherwise resolve to a function off the prototype.
    level: Object.prototype.hasOwnProperty.call(STATUS_LEVEL, status)
      ? STATUS_LEVEL[status]
      : "info",
    source: "devclaw",
    subject: `${kind} ${shortId}`,
    // A colon, not a dash: line 1 already joins subject to headline with " — ".
    headline: goal ? `${status}: ${goal}` : status,
    body,
    detail,
  };
}
