// Trigger script of the `needs-you-mail-reader` cron row (scripts/ensure-needs-you-digest.sh).
// It only asks door.py mail-check, which owns every decision, the way the job-queue
// drain gate asks the queue: nothing for the needs-you mail reader to do fires nothing and costs
// no model turn; anything else (work, or a check that could not say) runs the
// job exactly as before. An unknown answer is a fault: throw, so the row shows it.
const res = await exec({
  command:
    "/home/node/.openclaw/agents/reader/workspace/skills/doc-door/door.py mail-check",
});
const out = String(res?.aggregated ?? res?.details?.aggregated ?? "").trim();
const [head, ...rest] = out.split("\n");
if (head === "MAIL idle") {
  return { fire: false };
}
if (head === "MAIL wake" && rest.length > 0) {
  return { fire: true, message: `Gate wake reason: ${rest.join("; ")}` };
}
throw new Error(`door.py mail-check: ${out.slice(0, 500) || "no output"}`);
