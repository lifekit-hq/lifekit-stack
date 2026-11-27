// The job-queue drain gate: the trigger script of kit's job-queue-drain cron
// row (docs/runbook.md, "Shared job queue"). It only asks the queue; the queue
// owns every decision, so this stays the same when kinds or the egress change.
// A ready job fires one agent turn with the job's run instructions appended;
// an idle queue or an egress that does not answer fires nothing and leaves no
// run history. Any other answer is a fault: throw, so the row shows the error.
// A fault always reads "job-queue gate failed: ...". A slow gateway (one that
// does not finish the evaluation within OpenClaw's fixed 30 s) reads "code mode
// timeout exceeded" instead, whatever this script was doing.
const fail = (why) => {
  throw new Error(`job-queue gate failed: ${why}`);
};
let res;
try {
  res = await exec({
    command: "python3 /home/node/.openclaw/skills/job-queue/jobq.py next",
  });
} catch (e) {
  fail(`could not run jobq.py next: ${String(e?.message ?? e).slice(0, 500)}`);
}
const out = String(res?.aggregated ?? res?.details?.aggregated ?? "").trim();
const [head, ...rest] = out.split("\n");
if (head === "JOBQ idle" || head === "JOBQ wait") {
  return { fire: false };
}
if (head === "JOBQ ready" && rest.length > 0) {
  return { fire: true, message: rest.join("\n") };
}
fail(`jobq.py next: ${out.slice(0, 500) || "no output"}`);
