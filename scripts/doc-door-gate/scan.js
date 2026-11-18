// Trigger script of the `doc-door` cron row (scripts/ensure-doc-door.sh).
// It only asks door.py check, which owns every decision, the way the job-queue
// drain gate asks the queue: nothing for the doc-door scan to do fires nothing and costs
// no model turn; anything else (work, or a check that could not say) runs the
// job exactly as before. An unknown answer is a fault: throw, so the row shows it.
const res = await exec({
  command:
    "/home/node/.openclaw/agents/reader/workspace/skills/doc-door/door.py check",
});
const out = String(res?.aggregated ?? res?.details?.aggregated ?? "").trim();
const [head, ...rest] = out.split("\n");
if (head === "DOOR idle") {
  return { fire: false };
}
if (head === "DOOR wake" && rest.length > 0) {
  return { fire: true, message: `Gate wake reason: ${rest.join("; ")}` };
}
throw new Error(`door.py check: ${out.slice(0, 500) || "no output"}`);
