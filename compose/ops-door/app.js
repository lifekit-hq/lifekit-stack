/**
 * ops-door core: an Alertmanager-compatible webhook payload (Grafana's webhook
 * contact point sends the same shape) becomes one GitHub issue per alert
 * fingerprint, through the shared envelope and receipt. The envelope and the
 * receipt are the build-standards entry "Envelope and receipt at every door"
 * (vault, 2026-11-16); this file enforces it where the ops item is made and
 * never restates it - docs/runbook.md "Ops door" cites the entry.
 *
 * Everything with a side effect is injected (`github`, `decide`, `now`, `log`),
 * so the whole lifecycle is tested against an in-memory GitHub and a stub
 * decide.
 *
 * Lifecycle per alert, keyed by fingerprint (alert name + identifying labels):
 *   firing, no open issue   -> open one (a closed match is linked, not reopened)
 *   firing, open issue      -> count + latest occurrence (the notice time, as
 *                              startsAt stays fixed while an alert keeps
 *                              firing); above the threshold, exactly one
 *                              Tier 2 call instead of a comment
 *   resolved                -> comment only; closing is the merged fix's job
 *
 * The count, the latest occurrence and the escalation mark live in a JSON
 * marker inside the issue body, so the receiver holds no state of its own and
 * a restart loses nothing.
 */

import { createHash } from "node:crypto";

// Labels that describe the rule's routing or its folder, not the thing that is
// broken: left out of the fingerprint so a severity change does not fork an
// incident.
export const IGNORED_LABELS = new Set(["severity", "grafana_folder"]);
export const OPS_LABEL = "ops";
const MARKER = /<!-- ops-door (\{.*?\}) -->/;
const MAX_TEXT = 1500;
const LIST_PAGES = 3;

const clip = (s, n = MAX_TEXT) => {
  const t = String(s ?? "").trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
};

/** Identifying labels, sorted, without routing noise or internal `__` labels. */
export function identifyingLabels(labels = {}) {
  return Object.keys(labels)
    .filter((k) => !k.startsWith("__") && !IGNORED_LABELS.has(k))
    .sort()
    .map((k) => [k, String(labels[k])]);
}

/** `{ text, id }`: the readable fingerprint and its short stable hash. */
export function fingerprintOf(labels = {}) {
  const name = labels.alertname ?? "unknown";
  const rest = identifyingLabels(labels).filter(([k]) => k !== "alertname");
  const text = rest.length
    ? `${name}{${rest.map(([k, v]) => `${k}="${v}"`).join(",")}}`
    : name;
  const id = createHash("sha256").update(text).digest("hex").slice(0, 12);
  return { text, id };
}

const safeJson = (o) =>
  JSON.stringify(o).replace(/</g, "\\u003c").replace(/>/g, "\\u003e");

export function parseState(body) {
  const m = MARKER.exec(String(body ?? ""));
  if (!m) return null;
  try {
    return JSON.parse(m[1]);
  } catch {
    return null;
  }
}

export function renderBody(s) {
  const lines = [
    `<!-- ops-door ${safeJson(s)} -->`,
    "```yaml",
    "origin: alertmanager",
    "owner: lifekit-stack",
    "class: ops",
    `fingerprint: ${JSON.stringify(s.text)}`,
    `acceptance: ${JSON.stringify(`the alert stayed resolved for its \`for\` window (${s.alertname})`)}`,
    `thread: ops-door:${s.fp}`,
    "```",
    "",
    `**${s.summary || s.alertname}**`,
  ];
  if (s.description) lines.push("", s.description);
  lines.push(
    "",
    `Count: **${s.count}** · first fired ${s.first} · latest occurrence ${s.latest}`,
    `Labels: ${identifyingLabels(s.labels)
      .map(([k, v]) => `\`${k}=${v}\``)
      .join(" ")}`,
  );
  if (s.reopens) lines.push("", `Fires again after #${s.reopens} was closed.`);
  if (s.escalated) lines.push("", `Repeated past the threshold: Tier 2 call \`${s.escalated}\` filed.`);
  return lines.join("\n");
}

const snapshot = (alert, fp) => ({
  fp: fp.id,
  text: fp.text,
  alertname: alert.labels?.alertname ?? "unknown",
  labels: Object.fromEntries(identifyingLabels(alert.labels)),
  summary: clip(alert.annotations?.summary, 300),
  description: clip(alert.annotations?.description),
});

export function createApp({
  github,
  decide,
  repoUrl = "",
  threshold = 3,
  now = () => new Date().toISOString(),
  log = () => {},
}) {
  const counters = { firing: 0, repeat: 0, resolved: 0, escalated: 0, ignored: 0, errors: 0 };
  let tail = Promise.resolve();

  async function findByFingerprint(id) {
    const open = [];
    const closed = [];
    for (let page = 1; page <= LIST_PAGES; page++) {
      const batch = await github.listIssues({ page });
      for (const issue of batch) {
        if (issue.pull_request) continue;
        if (parseState(issue.body)?.fp !== id) continue;
        (issue.state === "open" ? open : closed).push(issue);
      }
      if (batch.length < 100) break;
    }
    const byNewest = (a, b) => b.number - a.number;
    return { open: open.sort(byNewest)[0] ?? null, closed: closed.sort(byNewest)[0] ?? null };
  }

  const receipt = (issue, state, extra = {}) => ({
    id: issue.number,
    url: issue.html_url,
    state,
    ...extra,
  });

  async function onFiring(alert, fp) {
    const noticed = now();
    const at = alert.startsAt || noticed;
    const { open, closed } = await findByFingerprint(fp.id);
    if (!open) {
      const s = { ...snapshot(alert, fp), count: 1, first: at, latest: at, escalated: "", reopens: closed?.number ?? 0 };
      const issue = await github.createIssue({
        title: `Ops alert: ${s.summary || s.text}`.slice(0, 200),
        body: renderBody(s),
        labels: [OPS_LABEL],
      });
      counters.firing++;
      log("info", "ops issue opened", { issue: issue.number, fingerprint: fp.id, reopens: s.reopens });
      return receipt(issue, "submitted", { fingerprint: fp.id, count: 1 });
    }
    const prev = parseState(open.body);
    const s = { ...prev, ...snapshot(alert, fp), count: prev.count + 1, latest: noticed };
    counters.repeat++;
    if (s.count > threshold && !s.escalated) {
      const callId = `ops-repeat-${fp.id}-${open.number}`;
      await decide({
        id: callId,
        title: `Alert still repeating after ${prev.count} notices: ${s.alertname}`,
        question: `${s.text} fired ${s.count} times on ${open.html_url} and the fix did not close it. What next?`,
        repo: repoUrl,
        issue: open.html_url,
      });
      s.escalated = callId;
      counters.escalated++;
      await github.updateIssue(open.number, { body: renderBody(s) });
      await github.comment(open.number, `Fired ${s.count} times (latest ${noticed}). Past the threshold of ${threshold}: Tier 2 call \`${callId}\` filed.`);
      log("info", "ops repeat escalated", { issue: open.number, call: callId });
    } else if (s.escalated) {
      // Already escalated: keep the count honest, say nothing more.
      await github.updateIssue(open.number, { body: renderBody(s) });
    } else {
      await github.updateIssue(open.number, { body: renderBody(s) });
      await github.comment(open.number, `Fired again (count ${s.count}), latest occurrence ${noticed}.`);
    }
    return receipt(open, "submitted", { fingerprint: fp.id, count: s.count });
  }

  async function onResolved(alert, fp) {
    const { open } = await findByFingerprint(fp.id);
    if (!open) {
      counters.ignored++;
      return { fingerprint: fp.id, state: "ignored", reason: "no open issue" };
    }
    const at = alert.endsAt && !alert.endsAt.startsWith("0001") ? alert.endsAt : now();
    await github.comment(open.number, `Resolved at ${at}. The issue stays open: the merged fix closes it once the alert has stayed resolved for its \`for\` window.`);
    counters.resolved++;
    return receipt(open, "submitted", { fingerprint: fp.id });
  }

  async function handleAlert(alert) {
    const fp = fingerprintOf(alert.labels);
    return alert.status === "resolved" ? onResolved(alert, fp) : onFiring(alert, fp);
  }

  /** Serialise whole payloads: one receiver, no two alerts racing a search. */
  function handlePayload(payload) {
    const run = tail.then(async () => {
      const receipts = [];
      for (const alert of payload?.alerts ?? []) receipts.push(await handleAlert(alert));
      return { receipts };
    });
    tail = run.catch(() => {});
    return run.catch((err) => {
      counters.errors++;
      throw err;
    });
  }

  const metrics = () =>
    [
      "# HELP ops_door_events_total Alert notices handled, by outcome.",
      "# TYPE ops_door_events_total counter",
      ...Object.entries(counters).map(([k, v]) => `ops_door_events_total{outcome="${k}"} ${v}`),
      "",
    ].join("\n");

  return { handlePayload, metrics, counters };
}
