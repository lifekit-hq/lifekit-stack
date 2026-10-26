// The job-queue drain gate: the trigger script of kit's job-queue-drain cron
// row (docs/runbook.md, "Shared job queue"). It only asks the queue; the queue
// owns every decision, so this stays the same when kinds or the egress change.
// A ready job fires one agent turn with the job's run instructions appended;
// an idle queue or an egress that does not answer fires nothing and leaves no
// run history. Any other answer is a fault: throw, so the row shows the error.
const res = await exec({
  command: "python3 /home/node/.openclaw/skills/job-queue/jobq.py next",
});
const out = String(res?.aggregated ?? res?.details?.aggregated ?? "").trim();
const [head, ...rest] = out.split("\n");
if (head === "JOBQ idle" || head === "JOBQ wait") {
  return { fire: false };
}
if (head === "JOBQ ready" && rest.length > 0) {
  return { fire: true, message: rest.join("\n") };
}
throw new Error(`jobq.py next: ${out.slice(0, 500) || "no output"}`);
