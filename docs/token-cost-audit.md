# Recurring AI jobs: token-cost audit (2026-11-16)

Read-only audit for issue 456. Nothing was removed or changed; every verdict below is a recommendation
for a follow-up issue, not an action taken.

## Method and limits

- **Measured** figures come from `python3 -I scripts/ops-report/jobs.py --days 7` and `--days 30`
  (run 2026-11-16). They are **list-price weighting of logged tokens, not billed spend** (the account is
  a subscription; `jobs.py` docstring). Per-run = usd / runs.
- **Job definitions** come from the repo: `scripts/ensure-*.sh`, `docs/runbook.md`,
  `compose/openclaw-gateway/platform.patch.json`, and the systemd `.timer` units under `scripts/`.
- Not read: live config, secrets, or any host state under `/srv`. A job whose cron row exists only
  as host state (no declaration in git) is marked *host-state*; its definition is not checked here.
- 7 days is a short window: a job that fired 1-3 times has no stable per-run figure.

Whole box, 7 days: **$1,815** list-price (30 days: $7,889). Where it goes:

| Consumer | 7d list-USD | 30d list-USD | Share of 7d |
| --- | ---: | ---: | ---: |
| workers (one-off task sessions) | 662 | 2,660 | 36% |
| no-mistakes (review pipeline) | 509 | 1,657 | 28% |
| vps supervisor | 281 | 1,091 | 15% |
| main supervisor | 224 | 1,490 | 12% |
| **openclaw (gateway)** | **59** | **559** | **3%** |
| memory (scouts) | 49 | 173 | 3% |
| clip | 32 | 258 | 2% |

The finding that frames everything else: the gateway crons the issue first names are **3% of the box**
(about $36 of the $59 openclaw line in 7 days). The recurring spend is in the review pipeline and in
supervisor wakes. Savings sized below are therefore on the 7-day figure and are upper bounds.

Verdict key: **keep**, **script** (replace the model turn with a script, or gate the turn behind one),
**CLI** (agent-friendly CLI the agent calls instead of reasoning), **cheaper** (smaller model),
**remove**.

## 1. Gateway crons (agent turns), OpenClaw

| Job | Schedule | 7d runs | 7d USD | per run | Verdict |
| --- | --- | ---: | ---: | ---: | --- |
| `ledger-pulse` | 09:20, 15:20, 21:20 | 21 | 9.2 | 0.44 | **script** (gate) |
| `job-queue-drain` | every 2 min, gated | 28 | 9.3 | 0.33 | **script** for no-judgement outputs, keep for video-eval |
| `today-brief` | 08:30 | 7 | 5.8 | 0.83 | **keep**, verify `--light-context` |
| `needs-you-digest` | 09:00, 17:00 | 14 | 3.3 | 0.24 | **keep** (already trimmed) |
| `needs-you-mail-reader` | 08:45, 16:45 | 14 | 1.9 | 0.14 | **keep**; Haiku is a candidate |
| `ledger-opportunity` | daily | 7 | 2.1 | 0.30 | **keep** |
| `evening-plan-note` | 21:00 | 5 | 1.2 | 0.24 | **keep** |
| `memory-vault-cleanup` | Sun 03:50 | 1 | 1.5 | 1.48 | **keep**, gate on empty findings |
| `doc-door` | 07:20, 13:20, 19:20 | 19 | 0.7 | 0.04 | **keep** |
| `sunday-review` | host-state | 1 | 0.6 | 0.62 | **keep**, undeclared in git |

- **`ledger-pulse`** is the clearest script candidate. Its checklist (`scripts/finance-pulse.md`) says
  "silence is the default" and ends in `NO_REPLY` unless a held event, a stale provider or a Sunday/morning
  step applies. Steps 1-5 are deterministic reads of finance-sentry MCP data. The model turn costs
  $0.44 whether or not there is anything to say, 21 times a week. `job-queue-drain` already shows the
  shape: a trigger script stored in the cron row decides whether to wake the agent at all. A gate that
  checks pending companion events and provider staleness, and wakes the model only when something is
  pending or on the two scheduled digest slots, keeps the judgement and drops the empty turns.
  Upper bound: most of the $9.2/week.
- **`job-queue-drain`** already starts the handler itself and wakes the agent only when a run printed an
  answer (runbook "Shared job queue"), so it is paying for answers, not polling. What remains is a turn
  that runs `jobq.py output` and relays it. For outputs that are a fixed receipt or a failure line, a
  command-kind announce needs no model; video-eval verdicts need kit's rubric judgement and stay a turn.
  The saving depends on the mix of job kinds, which the 7-day figure does not break out.
- **`today-brief`** costs more per run than the 2026-11-03 baseline ($0.74 -> $0.83), after the fleet and
  learning-line steps were added. The runbook's `--light-context` edit ("Light context on `today-brief` and
  `ledger-opportunity`") is an operator step, so whether it is applied is host state this audit could not
  read. `ledger-opportunity` did fall ($0.49 -> $0.30), which suggests it was applied there and not on
  `today-brief`. Check with `openclaw cron show <id>` before any other change. The brief is the owner's
  one synthesis of mail, calendar, fleet and plan, so there is no cheaper form to swap in.
- **`needs-you-digest`** is the success case: 30-day average $0.62 per run (62 runs, $38.3) fell to
  $0.24 in the last 7 days after the Sonnet pin and the precomputed `needs-you-digest.mjs` reads (#465).
  `needs-you-mail-reader` is a pure summarising turn at $0.14. Haiku would cost less per run (not measured here),
  but the reader exists to resist prompt injection from mail text, and a weaker model is a worse
  injection reader. $1.9/week does not pay for that risk; revisit only with an injection test.
- **`memory-vault-cleanup`**: weekly, $1.2-1.5. The runbook says it works only on the findings the audit
  just wrote; when `audits/findings.json` is empty a script check can skip the turn. Small.
- **`doc-door`**: $0.04 per run. The scan is a script and most runs find nothing; already cheap.
- **`sunday-review`** has no declaration in git (3 runs / 30 days, about $0.63 each). Declare it
  (`ensure-*.sh` or runbook) before judging it.

### Already scripted, no model turn

`workout-nudge` and `evening-day-plan` are command-kind crons (`nudge.sh`, `day-check.sh`; runbook
"Kit's workout log and gym-day nudge", "Kit's evening day plan"). The one `evening-day-plan` row in the
30-day window ($0.33) predates the conversion. `weekly_log_summary` and `memory_vault_audit` are also
command-kind. Nothing to do.

### Off or retired: zero cost

- **Heartbeats**: `agents.defaults.heartbeat.every` is `"0m"` and the finance agent's heartbeat was
  retired 2026-09-26 for the `ledger-pulse` cron (`platform.patch.json`, runbook fs-685). No heartbeat
  turns run.
- **memory-core dreaming** is disabled in `platform.patch.json`.
- `career-weekly` is disabled; `ledger-scan` was retired 2026-09-19; the `health`, `social` and
  `learning` agents were deleted 2026-10-07 along with their crons.
- **Unmeasured**: the system-owned `skill-collection-review-<agent>` crons show no rows in either
  window. Either they have not run or `jobs.py` does not classify them. Check
  `openclaw cron list --all` for their last-run times.

## 2. Companion-signal hooks (OpenClaw, event-driven, not cron)

7 days: 22 runs, $8.6. 30 days: 75 runs, $31.5. About $0.40-0.50 each for Digest, MarketStructure,
NewsCluster, Opportunity, ThesisBreak, and $0.20 for PerformanceBrief, OperationalFailure, FamilyStatement.

Verdict: **keep** the severe kinds (ThesisBreak, Opportunity, OperationalFailure), where Ledger's judgement
is the point. For routine kinds (`PerformanceBrief`, `FxSpread`, `MacroEventAhead`, `EarningsAhead`)
check whether the payload already carries the facts and a **script** render would do; that needs the
finance-sentry payload shape, which is outside this repo. About $8.6/week at most.

## 3. Other OpenClaw lines

- **`deploy-smoke`**: 8 runs, $2.0 (7d). One model turn per deploy to prove the runtime answers.
  Keep the check; use the cheapest model for it (the Claude Code smoke already pins Haiku,
  `scripts/upgrade-smoke/claude-code.sh`). **cheaper**, about $1.5/week.
- **`interactive/other`**: 16 runs, $12.9 (7d); 71 runs, **$424** (30d), the largest openclaw line. These are
  owner chats, not jobs, so this audit does not give a verdict. Per-run ($6) means long sessions; worth
  its own look at context size.
- **Key probe** (`scripts/openclaw-auth/key-probe.py`, every 10 min under the spend-guard timer): one
  1-token Haiku request. Negligible. **keep**.

## 4. Clip pipeline

Pipeline runs (`run.json cost_usd`): $1.8 in 7 days, $26.9 in 30 days. The remaining clip spend
($32 / 7d, $258 / 30d) is one-off development task sessions (e.g. the quality-gate and judge batches),
which are not recurring. **keep**; the recurring part is small. The per-step split lives in the clip
repo's `run.json` and was not read.

## 5. Review pipeline (no-mistakes): the largest recurring line

| Step | 7d runs | 7d USD | per run | 30d USD |
| --- | ---: | ---: | ---: | ---: |
| test | 365 | 158.2 | 0.43 | 470.3 |
| review | 369 | 140.7 | 0.38 | 491.6 |
| review-fix | 137 | 53.6 | 0.39 | 226.2 |
| document | 177 | 41.2 | 0.23 | 118.0 |
| ci | 132 | 38.1 | 0.29 | 84.5 |
| pr | 219 | 35.0 | 0.16 | 115.3 |
| test-fix | 45 | 15.8 | 0.35 | 41.2 |
| document/housekeeping | 54 | 11.7 | 0.22 | 50.8 |
| review-coverage | 30 | 11.0 | 0.37 | 49.0 |
| rebase, lint-fix | 21 | 3.5 | | |

Total $509 (7d), 28% of the box.

- **`test`** is the biggest single recurring line ($158, 365 runs a week, 31% of the pipeline). Running
  the repo's test command is deterministic. Verdict: **script** the run and bring in the model only on a
  failure (that is what `test-fix` already is). Before committing to it, check what the test step asks the
  agent to do beyond running the command (choosing tests, adding tests); if it only runs and reports,
  most of the $158 goes.
- **`review`** is the point of the gate: **keep**. Re-review rounds and `review-coverage` ($0.37) are a
  **cheaper**-model candidate if the first-round model is the stronger one. 369 reviews against 137
  fix rounds means about 2.7 reviews per fix round.
- **`pr`** ($0.16) writes a title and body from the diff: **cheaper** model (Haiku-class).
- **`document`** and **`housekeeping`** ($0.22-0.23): **cheaper** model, and skip when the diff touches
  no documented surface.
- **`ci`** ($0.29) monitors a PR for merge. If it is polling through model turns, that is a **script**
  (`gh` status plus a wake on change).
- 7-day runs outnumber PRs several times over (365 `test` runs): the number of rounds per PR is the
  multiplier on every row. Fewer rounds beats a cheaper step.

The pipeline's own configuration (per-step model, whether a step may be scripted) lives in the
no-mistakes tool, not this repo; the verdicts above need a change there.

## 6. Supervisor wakes (vps + main)

Not gateway crons, but the next-largest recurring spend: 7 days across `vps` and `main`.

| Wake class | 7d runs | 7d USD | per run | Verdict |
| --- | ---: | ---: | ---: | --- |
| `supervision-host:branch-outcome` | 310 | 99.2 | 0.30-0.41 | **script** triage |
| `signal:worker-status` | 182 | 55.3 | 0.30-0.42 | **script** triage |
| `check:rearm-resurface` | 135 | 35.5 | 0.23-0.33 | **script** |
| `captain/other` (interactive) | 1,037 | 204.7 | 0.18-0.21 | not a job |
| `bg-task-notice` | 55 | 16.0 | 0.25-0.38 | **script** |
| `compaction-resume` | 57 | 12.6 | 0.15-0.28 | keep |
| `supervision-host-engine` | 516 | 28.8 | 0.05-0.06 | keep |

- `check:rearm-resurface` is a timer re-arm and needs no judgement: **script**, about $35/week.
- `branch-outcome` and `worker-status` wake a full supervisor turn per event. A script that classifies
  the event (green and merged, or progress with nothing to decide) and wakes the model only when a decision
  is needed would cut most of these. Upper bound about $150/week; the real figure depends on how many
  events need a decision, which the log does not say.
- **Cold cache**: `cold_cache` overlay is $37.4 on `main` (17% of its spend) and $8.6 on `vps`: calls
  after an hour idle re-write the whole context at 2x input. Keeping wakes inside the cache window, or
  starting a fresh short session for rare wakes, targets that.

## 7. Memory jobs and timers

- **Weekly scouts** (`memory`, `recurring:agentic-scout-*`, `harness-ecosystem-scout`): $5.5-9.2 each,
  about $6.4/week for the weekly one, $49 / 7d for the consumer. Keep one weekly scout and ask whether its
  output is used before it runs more; **keep** pending that answer. The daily `lifekit-scout.timer`
  fetch is a script (`feed2exec`) and ends with no model call when nothing is new.
- **Systemd timers** (`lifekit-vault-audit`, `memory-sync`, `memory-rotate`, `learnharness-sync`,
  `lifekit-intake-to-vault`, the gauges, backups, prune, `lifekit-ledger-drift`,
  `lifekit-fleet-publisher`): none of these call a model. **keep**.

## Ranked actions

Sizes are upper bounds on the 7-day list-price figure.

| # | Change | Verdict | Up to $/week |
| ---: | --- | --- | ---: |
| 1 | no-mistakes `test` step: run the command as a script, model only on failure | script | 158 |
| 2 | `branch-outcome` / `worker-status` wakes: classify by script, wake on decision | script | 150 |
| 3 | `check:rearm-resurface` wakes | script | 35 |
| 4 | no-mistakes `pr`, `document`, `housekeeping`, re-review rounds | cheaper | 99 |
| 5 | `ledger-pulse`: gate the turn behind a pending-events check | script | 9 |
| 6 | Companion hooks, routine kinds | script/cheaper | 5 |
| 7 | `job-queue-drain` relay turns with no judgement | script | 5 |
| 8 | `deploy-smoke` on the cheapest model | cheaper | 1.5 |

Items 1-4 are not gateway work and live in the no-mistakes tool and the Firstmate supervision host. Items
5-8 are the gateway crons the issue names, and together they are under $25 a week.

Re-measure after any change with `python3 -I scripts/ops-report/jobs.py --days 7`.
