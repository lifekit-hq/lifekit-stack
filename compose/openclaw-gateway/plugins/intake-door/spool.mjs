#!/usr/bin/env node
// spool.mjs - the intake door's spool from the outside (docs/runbook.md,
// "Intake door"). Runs in the gateway container:
//   node spool.mjs count   one JSON line: {schema, pending, stuck, oldest_epoch}
//   node spool.mjs retry   file every spooled occurrence now; one JSON summary
// The plugin's own service runs the same retry every ten minutes.
import { count, retry } from "./door.js";

const verb = process.argv[2];
if (verb === "count") {
  process.stdout.write(`${JSON.stringify(count())}\n`);
} else if (verb === "retry") {
  const got = await retry();
  process.stdout.write(`${JSON.stringify({ ...got, ...count() })}\n`);
  process.exitCode = got.failed ? 1 : 0;
} else {
  process.stderr.write("usage: spool.mjs count|retry\n");
  process.exitCode = 2;
}
