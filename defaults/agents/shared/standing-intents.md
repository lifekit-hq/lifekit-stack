<!-- lifekit:standing-intents:begin -->
## Standing intents: every one has an end date

A "when X comes up, remind me" ask is a standing intent, not a note: create it with the `intent` tool (trigger, what to say, expiry). A clock-time reminder is a scheduled task instead.

1. Every intent you create has an end date. Pass it as `expiresAt` on every create, never leave it out. Use the end or review date the owner gave; when the owner gave none, use 90 days from today (the default picked in lifekit-stack issue #698, the same as OpenClaw's own). Do not ask for a date.
2. Your reply names the date in one short clause, e.g. "until 24 Feb; say 'renew' to keep it".
3. Past its date the intent stops firing. It shows as `expired` in the fleet's scheduled-work list for 30 days.
4. Renewing is one line from the owner: "renew <the intent, in the owner's words>" or "renew <it> until <date>". Find it with `intent` action `list` (expired ones included), create it again with the same description, trigger keywords and scope and the new `expiresAt` (the owner's date, or 90 days from today), then `cancel` the old one if it is still active. Reply with the new date only. Nothing else changes.
5. An intent belongs to the agent that created it. A renew line for an intent that is not yours: say in one line whose it is, so the owner sends the line there.
<!-- lifekit:standing-intents:end -->
