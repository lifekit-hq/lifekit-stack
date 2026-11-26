// intake-door - the one intake door every agent files through, which cannot
// refuse (docs/runbook.md, "Intake door"; vault decision 0019).
//
// Registers the agent tool `file_intake` (project, title, body, fingerprint,
// envelope). The call goes to devclaw's `file_intake` (one GitHub issue per
// repo + fingerprint, a repeat adds a count, decision 0016). When that fails
// for any reason the item is written to the local intake spool and the agent
// still gets a receipt (`spool:<key>`), so a failed hand-off is never the
// owner's work. A service retries the spool every ten minutes in the gateway;
// spool.mjs runs the same retry and prints the count the fleet page shows.
// The logic lives in door.js.

import { count, file, retry } from "./door.js";

export const TOOL = "file_intake";
const RETRY_MS = 10 * 60_000;

const PARAMETERS = {
  type: "object",
  additionalProperties: false,
  required: ["project", "title"],
  properties: {
    project: {
      type: "string",
      description:
        "Where the item belongs: the devclaw intake project of the repository that owns it (lifekit-stack, finance-sentry, lifekit-dashboard, lifekit-common, devclaw, ...), `vault` for the memory vault, or `fleet` for the fleet's ops queue when the owner is unclear. An unknown project is still accepted: the item is held in the intake spool, counted, and filed under that project once devclaw knows it.",
    },
    title: { type: "string", description: "One line, free of per-run noise (ids, times)." },
    body: { type: "string", description: "What failed or what was asked, with the evidence (error text, id, time)." },
    fingerprint: {
      type: "string",
      description: "One stable id for the item ([a-z0-9._-], e.g. repo plus failure kind). A repeat with the same fingerprint adds a count to the open item instead of a second one.",
    },
    envelope: {
      type: "object",
      additionalProperties: false,
      description: "The decision 0016 envelope.",
      properties: {
        origin: { type: "string", description: "Where it was said or found; defaults to the calling agent." },
        owner: { type: "string", description: "Who acts next." },
        class: { type: "string", enum: ["dev", "ops", "finance", "life", "unclear"] },
        acceptance: { type: "string", description: "The done-when." },
        thread: { type: "string", description: "The conversation it belongs to." },
      },
    },
  },
};

const DESCRIPTION =
  "File one item to intake: a development-class problem, an owner ask or commitment, or a door that failed. " +
  "Returns a receipt {id, url, state}: `submitted` (new), `working` (a repeat that added a count). " +
  "It never refuses: when devclaw or GitHub is down or the project is unknown, the item is held in the local " +
  "intake spool (`id` spool:<key>, `spooled` true) and filed automatically later. Show the receipt id and carry on; " +
  "never hand the item back to the owner.";

function register(api) {
  api.registerTool(
    (toolContext) => ({
      name: TOOL,
      label: "File to intake",
      description: DESCRIPTION,
      parameters: PARAMETERS,
      async execute(_id, params) {
        const receipt = await file(params, toolContext?.agentId);
        return { content: [{ type: "text", text: JSON.stringify(receipt) }], details: { receipt } };
      },
    }),
    { name: TOOL },
  );

  if (api.registrationMode && api.registrationMode !== "full") return;
  if (typeof api.registerService !== "function") return;
  const once = async () => {
    try {
      const got = await retry();
      if (got.filed || got.failed) {
        api.logger?.info?.(`intake-door: spool retry filed ${got.filed}, failed ${got.failed}; ${JSON.stringify(count())}`);
      }
    } catch (err) {
      api.logger?.warn?.(`intake-door: spool retry error: ${String(err?.message || err).slice(0, 200)}`);
    }
  };
  let timer = null;
  api.registerService({
    id: "intake-spool-retry",
    start(ctx) {
      if (ctx?.scheduler?.schedule) {
        ctx.scheduler.schedule({ id: "retry", delayMs: 60_000, everyMs: RETRY_MS, run: once });
        return;
      }
      timer = setInterval(once, RETRY_MS);
      timer.unref?.();
    },
    stop() {
      if (timer) clearInterval(timer);
      timer = null;
    },
  });
}

export default {
  id: "intake-door",
  name: "Intake door",
  description: "The file_intake tool: files to devclaw intake, spools and retries a failed hand-off",
  register,
};
