---
name: workout-log
description: Log a gym session from one typed or spoken line with the workout-claw CLI and confirm it back in one line, or answer what the user last did on a lift. Use when the user reports sets, reps or weights ("bench press 8,8,6@60kg, squat 5x5@100kg"), sends a voice note about a session, corrects a session just logged, or asks "what did I do last <lift>".
metadata: { "openclaw": { "requires": { "bins": ["workout-claw"] } } }
---

# Workout log

workout-claw owns the data (`~/.workout-claw/`). Never write session data anywhere else, and never say "logged" before the read-back below shows it.

A voice note reaches you as its transcript. Treat it exactly like a typed line.

## Log a session
1. Turn the user's line into workout-claw's syntax, one exercise per comma-separated part, keeping their words otherwise:
   - `<name> <sets>x<reps>@<weight>` for even sets: `squat 5x5@100kg`.
   - `<name> <reps>,<reps>,...@<weight>` for uneven sets: `bench press 8,8,6@60kg`.
   - Names may contain spaces (`bench press`, `leg curl`). Weight takes `kg` or `lb`; a bare number is kg; `@bw` is bodyweight.
   - Spoken numbers become digits ("three sets of eight at sixty kilos" -> `3x8@60kg`).
   - Cardio goes in `--cardio "<entry>"` (e.g. `"incline-walk 20min @4.5kmh i6"`), anything else worth keeping in `--note`.
   If an exercise lacks reps or a weight you cannot infer from their words, ask for that one thing. Do not guess numbers.
2. Run `workout-claw log "<exercises>" --date <YYYY-MM-DD>` with the day they trained in the user's local time (today unless they said otherwise). Add `--muscle <group>` only when they named the session ("leg day").
3. Read it back: `workout-claw summary --date <same date>`. The `log` output is not the read-back.
4. Reply in one line, from the summary: `Logged <date>: <name> <sets>, <name> <sets> - <total_sets> sets, <total_volume_kg> kg volume.` Write each exercise's sets as the summary lists them (`8,8,6@60kg`, `5x5@100kg`, `3x10@bw`). Weights are stored in kg; when they sent lb, add their lb figure in brackets.

A line that says they trained but names no exercise ("done", "did it", "gym done") is not a log line for this skill. If your workspace instructions define a planned-session flow for it, follow that; otherwise ask what they did.

## Correct a session
Only for a session you logged in this conversation and only on their words: `workout-claw delete <session-id>` (the id from the summary), log the corrected line, and confirm it in one line as above.

## What did I do last on a lift
1. `workout-claw history --weeks 52` lists sessions with their exercise names (lowercase, dashed: `bench-press`, `incline-bench-press`).
2. Pick the names that are the lift they asked about. When more than one fits ("bench" -> `bench-press` and `incline-bench-press`), answer for the exact match if there is one, else ask which.
3. Take the most recent date with that name and run `workout-claw summary --date <date>` for its sets.
4. Reply in one line: `<name>, <date>: <sets>` (e.g. `bench-press, 2026-10-14: 8,8,6@60kg`). If it is not in the last 52 weeks, say so.

## Errors
If workout-claw exits non-zero, show its error line and stop. A "corrupt JSON" error means a log file needs repair: tell the user which file and do not retry, delete or write anything.
