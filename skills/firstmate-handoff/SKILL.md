---
name: firstmate-handoff
description: File a dev-class problem straight to firstmate's inbox instead of telling the owner. Use whenever you find a defect in a fleet app, a data source that keeps failing, or a tool that is broken, and the fix is code, config or an outside party, not an answer from the owner. The owner is never the messenger; firstmate places the problem in the owning repo's queue.
metadata: { "openclaw": { "requires": { "bins": ["ssh"] } } }
---

# Firstmate handoff

A problem outside your own domain goes to firstmate, which puts it in the queue of the repo that owns it (a defect becomes an issue there). You do not tell the owner about it, ask him to relay it, or file an issue yourself.

Dev-class means: a defect in a fleet app (finance-sentry, the dashboard, a skill's CLI), a data source that fails (EDGAR, a feed, an API), or a tool you call that is broken or missing. It is not a decision only the owner can make (that is a Needs-you call), a question about his own data, or something you can fix with your own tools.

## File it
1. Write the note with the Write tool to `/tmp/firstmate-handoff/body.txt` (never through a shell echo or heredoc). Plain lines, one fact each, no header and no `task:` prefix (the script adds the header naming you):
   - `what:` the failing thing in one line: app, source or tool, and the call that fails.
   - `evidence:` the exact error text, when it started, how often, what last worked. Numbers and messages as you saw them.
   - `impact:` what of the owner's work waits on it (omit when none).
   - `repo:` the owning repo when you know it (`owner/name`); omit when you do not.
   - `blocked-on:` only when the fix is in an outside party's hands (a vendor, a source site, a quota). Write `external` and the party. Firstmate then holds the item as an external wait and it shows as a state on the fleet page; you do not announce it to the owner.
   Never put a secret, a token, an account number or the owner's personal data in the note.
2. Run `sh {baseDir}/handoff.sh send <kit|ledger> /tmp/firstmate-handoff/body.txt` with your own agent id.
3. Stdout is two JSON lines: `kit-relay-received.v1`, then the note result (`outcome`, `id`, `announced`). Remember the `id`.
   - `created` → filed; a replay of the same note the same day is `replay`: do not send it twice.
   - exit 3 → saved, not yet announced; the note is durable, do not resend.
   - any failure → report the stderr line once and stop; never route around a refusal.

Sample, for Ledger:

```
what: EDGAR filings fetch fails (finance-sentry source sec_edgar, get_filings call)
evidence: HTTP 403 on 3 consecutive pulses since 06:10 UTC today; last good fetch yesterday 17:40 UTC
impact: thesis reviews that need a filing cannot refresh
repo: lifekit-hq/finance-sentry
blocked-on: external (SEC request policy)
```

## Rules
- Send on any turn, including a cron, heartbeat or A2A turn: this is your own finding, not the owner's words. One note per distinct problem; look first for one already filed (`sh {baseDir}/handoff.sh replies`) and do not file it again.
- The note is the whole handoff. Do not tell the owner "X is broken, please tell firstmate". On his own turn, when the problem blocks what he just asked, say so in one line with the note id and that it is filed; do not ask him to act.
- Firstmate may answer a note. Read answers with `sh {baseDir}/handoff.sh replies [<cursor>]`; show a reply only when it changes what you tell the owner.
- Failures: exit 255 with "Connection refused"/"timed out" means the host is unreachable (nothing saved; the same note re-sends safely later); "Permission denied (publickey)" or "Host key verification failed" need the operator, say so verbatim and stop; exit 64/65 is a refused note, show the refusal line; exit 75 is the host's hourly note limit.
