// The ledger-pulse gate: the trigger script of the `ledger-pulse` cron row
// (docs/runbook.md, "The finance heartbeat, retired for an isolated cron job").
// It only asks pulse_gate.py, which owns every decision, the way the
// job-queue drain gate asks the queue. A run with nothing pending, no stale
// provider and outside the morning slot fires nothing and costs no model turn;
// any other answer is a fault: throw, so the row shows the error.
const res = await exec({
  command:
    "python3 /home/node/.openclaw/gates/ledger-pulse/pulse_gate.py check",
});
const out = String(res?.aggregated ?? res?.details?.aggregated ?? "").trim();
const [head, ...rest] = out.split("\n");
if (head === "PULSE idle") {
  return { fire: false };
}
if (head === "PULSE wake" && rest.length > 0) {
  return { fire: true, message: `Gate wake reason: ${rest.join("; ")}` };
}
throw new Error(`pulse_gate.py check: ${out.slice(0, 500) || "no output"}`);
