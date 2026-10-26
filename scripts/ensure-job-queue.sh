#!/usr/bin/env bash
# ensure-job-queue.sh - install the shared job queue (skills/job-queue) into
# the gateway's shared skills directory, and report whether its drain cron
# row exists and runs the gate this checkout ships.
#
# The queue holds work that waits for an egress (today the owner's PC, through
# the yt-tunnel relay); skills such as reel-claim-check add jobs, and a cron
# row on kit, job-queue-drain, runs skills/job-queue/drain-gate.js as its
# trigger script and hands kit one ready job per turn (docs/runbook.md,
# "Shared job queue"). Cron rows are gateway host state, so this script only
# reads the row: creating it, or re-applying a changed gate, is an operator
# step. The queue's jobs live in the gateway's private state
# (/var/lib/lifekit/job-queue), never in this directory.
#
# The deploy runs it (deploy-openclaw.sh, post-up phase), so a merge that changes
# the queue or this script installs it. By hand, on the VPS host as lifekit, from
# the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-job-queue.sh
#
# Re-running converges (every file is overwritten in place).
#
# Env overrides:
#   JOB_QUEUE_CRON          the drain cron row's name (default: job-queue-drain)
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
set -euo pipefail

SKILL=job-queue
CRON_NAME="${JOB_QUEUE_CRON:-job-queue-drain}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
skill_src="${repo_root}/skills/${SKILL}"
skill_dest="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}/skills/${SKILL}"

install -D -m 0644 "$skill_src/SKILL.md" "$skill_dest/SKILL.md"
install -D -m 0755 "$skill_src/jobq.py" "$skill_dest/jobq.py"
install -D -m 0644 "$skill_src/drain-gate.js" "$skill_dest/drain-gate.js"
echo "job queue installed -> $skill_dest"

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; drain cron not checked"
  exit 0
fi

# Read-only: does the drain row exist, is it enabled, and is its stored gate
# the one installed above? The JSON is parsed by the container's node, as the
# host need not have one.
# shellcheck disable=SC2016  # JavaScript, not shell
row="$(docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null \
  | docker exec -i -e CRON_NAME="$CRON_NAME" "$GATEWAY" node -e '
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  const j = JSON.parse(s);
  const job = (j.jobs || j).find((x) => x.name === process.env.CRON_NAME);
  if (!job) return process.stdout.write("missing");
  const gate = require("fs").readFileSync(
    "/home/node/.openclaw/skills/job-queue/drain-gate.js", "utf8");
  const same = (job.trigger?.script || "").trim() === gate.trim();
  process.stdout.write(`${job.enabled ? "enabled" : "disabled"} ${same ? "same" : "differs"}`);
});
' 2>/dev/null || echo unknown)"
case "$row" in
  missing) echo "cron: '$CRON_NAME' is not set up - nothing drains the queue (docs/runbook.md, \"Shared job queue\")" ;;
  unknown) echo "cron: could not read the gateway's cron list" ;;
  "enabled same") echo "cron: '$CRON_NAME' is enabled and runs the shipped gate" ;;
  *differs) echo "cron: '$CRON_NAME' ($row) runs a gate that DIFFERS from drain-gate.js - re-apply it (docs/runbook.md, \"Shared job queue\")" ;;
  *) echo "cron: '$CRON_NAME' is ${row%% *} - nothing drains the queue until it is enabled" ;;
esac
