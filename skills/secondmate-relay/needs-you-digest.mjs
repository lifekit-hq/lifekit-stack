// needs-you-digest.mjs - the deterministic reads behind kit's needs-you digest
// (docs/runbook.md, "Kit's needs-you decisions and the daily digest").
//
//   node needs-you-digest.mjs [<open.json>]
//
// Prints, compactly, what the digest turn used to gather by hand: the open
// ledger entries (default needs-you/open.json under the current directory) and
// the fleet status from the sibling relay.sh (override: NEEDS_YOU_RELAY), with
// the Status rules' filtering and counting already applied. It reads only and
// sends nothing. The model keeps the judgment calls (call vs to-do, the mail
// section) and writes the message. A failed read prints one line starting
// 'needs-you digest failed:' or 'fleet status unavailable:' and still exits 0,
// so the turn reports it as the prompt says.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const one = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
const letters = (opts) => opts.map((o, i) => `${String.fromCharCode(65 + i)}) ${one(o)}`).join(" ");
const out = [];

// 1. The ledger.
const ledgerPath = process.argv[2] || "needs-you/open.json";
if (!existsSync(ledgerPath)) {
  out.push("ledger: none");
} else {
  try {
    const rows = JSON.parse(readFileSync(ledgerPath, "utf8"));
    if (!Array.isArray(rows)) throw new Error("not a JSON array");
    const old = (a, b) => String(a.opened ?? "").localeCompare(String(b.opened ?? ""));
    const sorted = [...rows].sort(old);
    out.push(`ledger: ${sorted.length} open, oldest first`);
    for (const e of sorted) {
      const opts = Array.isArray(e.options) ? e.options : [];
      out.push(
        `- ${one(e.ask)}${e.context ? ` | context: ${one(e.context)}` : ""}${opts.length ? ` | options: ${letters(opts)}` : ""}`,
      );
    }
  } catch (err) {
    out.push(`needs-you digest failed: ${one(err.message)}`);
  }
}

// 2. The fleet status.
const relay = process.env.NEEDS_YOU_RELAY || join(dirname(fileURLToPath(import.meta.url)), "relay.sh");
let fleet = null;
try {
  fleet = JSON.parse(execFileSync("sh", [relay, "status"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }));
} catch (err) {
  const msg = String(err.stderr || err.message).split("\n").find((l) => l.trim()) || "no output";
  out.push(`fleet status unavailable: ${one(msg)}`);
}

if (fleet) {
  const counts = fleet.counts ?? {};
  const list = (k) => (Array.isArray(fleet[k]) ? fleet[k] : []);
  const age = Math.floor(Date.now() / 1000) - Number(fleet.generated_epoch);
  if (Number.isFinite(age) && age > 30 * 60) {
    out.push(`fleet: partial, last updated ${age >= 3600 ? `${Math.floor(age / 3600)}h` : `${Math.floor(age / 60)}m`} ago`);
  }
  const kept = list("decisions_open").filter((d) => !d.hold_bucket || d.hold_bucket === "live");
  out.push(`fleet decisions: ${kept.length} live`);
  for (const d of kept) {
    const f = [one(d.summary || d.title), d.reason && `reason: ${one(d.reason)}`, d.board_url && `board: ${d.board_url}`];
    out.push(`- ${f.filter(Boolean).join(" | ")}`);
  }
  const queued = list("queued");
  const later = {
    parked: queued.filter((q) => q.hold_bucket === "aged" || (q.hold_kind && !q.hold_bucket)).length,
    dated: queued.filter((q) => q.hold_bucket === "dated").length,
    blocked: queued.filter((q) => q.hold_bucket === "blocked").length,
  };
  const laterParts = Object.entries(later).filter(([, n]) => n > 0).map(([k, n]) => `${n} ${k}`);
  if (laterParts.length) out.push(`later: ${laterParts.join(" · ")}`);
  const today = new Date().toLocaleDateString("en-CA", { timeZone: process.env.TZ || "Europe/Dublin" });
  const landed = list("landed").filter((l) => l.completion?.date === today).length;
  const fl = [`${counts.active_children ?? list("active_children").length} working`];
  if (counts.queued_open !== undefined) fl.push(`${counts.queued_open} queued`);
  fl.push(`${landed} landed today`);
  out.push(`fleet: ${fl.join(" · ")}`);
  const listed = list("decisions_open").length;
  if (Number(counts.decisions_open) > listed) out.push(`not shown: ${Number(counts.decisions_open) - listed}`);
}

console.log(out.join("\n"));
