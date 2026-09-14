---
name: memory-defrag
description: Defragment the memory vault - dedupe/merge overlapping pages (the lessons-vs-concepts seam), split oversized pages, fix INDEX drift, surface orphaned sources. Manual-first by decision of record - invoke as /defrag in a deliberate session; NEVER schedule as a cron without a separate graded proposal backed by measured token cost and at least 2 productive manual runs. Reorganizes existing content only; authors no new knowledge.
---

# memory-defrag - background-quality reorganization, run in the foreground

**Decision of record:** vault `system/proposals.md` entry `2026-07-20-dreaming-as-defragmentation` (accepted with conditions). This skill IS those conditions - if an instruction below feels like ceremony, it is load-bearing; do not skip it. The vault's `README.md` remains the contract and wins on any conflict. General vault procedures (sync, scanners, log formats) live in the sibling `memory-vault` skill; this skill only adds the defrag pass.

**What defrag is:** reorganization of existing content - merge duplicates, split oversized pages, repair index/link drift, surface dead weight. **What defrag is not:** authoring. A defrag run adds no new knowledge, no new claims, no new prose beyond hub/pointer text created by a split or merge.

## 0. Hard boundaries

- **Never touch:** `sources/` content, `PLAN.md`, `journal/`, `audits/` (existing files), dated `proposals` entries, generated blocks between `<!-- openclaw:... -->` markers, runtime state (`state/`, `scout/`, `queue.jsonl`, gitignored artifacts).
- **Propose, don't apply** (lane B below): merges, deletions, and any rewrite that drops or rewords prose. These land in a dated report for the human to grade - never applied in the same breath they are found.
- **No cron.** If you are reading this from a scheduled/headless context: stop, do nothing, and report that scheduling requires its own graded proposal (see the decision of record - it must arrive with measured token cost per run and >=2 productive manual runs as evidence).

## 1. Preconditions (all of them, in order)

1. Locate the vault: `$MEMORY_VAULT` -> `~/memory` -> `/srv/memory`. Call it `$VAULT`.
2. `git -C $VAULT pull --ff-only` and require a **clean tree** (`git status --porcelain` empty). A dirty tree means another writer is mid-flight; do not defrag over it.
3. **Sync-timer coordination** (the main operational risk - per-change commits racing the auto-sync timers):
   - **VPS:** stop the timer for the session: `sudo systemctl stop memory-sync.timer`. Restart it in step 6 no matter how the run ends.
   - **PC / workstation:** if an auto-backup task runs on this machine (git log showing `auto-backup PC ...` commits is the tell), pause it if you control it. If you cannot pause it, use the **atomic fallback**: keep each edit->commit cycle under a minute, check `git status` before every commit, and reconcile any interleaved timer commit with `git pull --rebase` before continuing.
4. Baseline scan: `sh $VAULT/bin/lint.sh` (the vault harness, since 2026-09-14). Record the finding count - the run must not end with more findings than it started with.
5. Note the session token count (or start a fresh session so the delta is readable). The report in step 5 needs it.

## 2. Scan for candidates

```bash
python3 scripts/defrag_scan.py [--vault $VAULT]
```

Deterministic, read-only. Reports four candidate classes:

- **near-duplicates** - page pairs (within and across `lessons/` and `concepts/`) whose titles + summaries overlap past a threshold. The lessons-vs-concepts seam shows up here.
- **oversized pages** - wiki pages past ~500 lines (the contract says propose a split).
- **orphan sources** - `pageType: source` pages cited by zero `claims[]` evidence and linked from zero pages ("sources are for citing"; an uncited source after ingestion is a smell).
- **INDEX drift** - pages in flat indexed categories (`lessons/`, `domains/`, `system/`) missing their hook line in the category `INDEX.md`.

The scanner finds candidates; it does not decide. Every candidate gets a human-judgment pass: open both pages of a duplicate pair and read them before calling them mergeable.

## 3. Lane A - auto-apply (mechanical, guarded)

May be applied directly during the run, one commit each:

- Missing/stale hook lines in category `INDEX.md` and root `index.md`.
- Broken wikilinks whose target was renamed (fix via frontmatter `aliases:` on the target, never by rewriting dated records).
- **Prose-preserving splits** of oversized pages: every line of the original survives verbatim into the parts; the original becomes a hub page (frontmatter + summary + links to parts); category INDEX and root `index.md` get the new lines; inbound links keep resolving (aliases if needed).

Guard for every applied change: the touched files still parse (frontmatter YAML loads; wikilinks resolve) and a `vault_scan.py` re-run shows no new findings. A change that fails the guard is reverted (`git checkout -- <file>` or revert the commit), not patched forward.

## 4. Lane B - propose only

Merges of near-duplicate pairs, deletions of dead pages, any rewording. For each: which pages, direction of the merge (which slug survives, which becomes an alias), what content moves where, what gets dropped and why. Write the set into the report (step 5). **Denys grades; a later session applies the accepted items** (those applications then count as lane A of that session, with the same guards).

## 5. Report + the scheduling evidence

Write `audits/YYYY-MM-DD-defrag-report.md` (structural frontmatter: `name`, `summary`, `updatedAt`, `tags: [defrag]`) containing:

- Candidates found per class (scanner output distilled).
- Lane A changes applied (one line each, with commit SHAs).
- Lane B proposals awaiting grade.
- **Run cost: tokens spent (session delta), wall time, count of applied/proposed.** This block is the evidence a future scheduling proposal must cite - a run that skips it does not count toward the >=2 threshold.

## 6. Close out (every run, even aborted ones)

1. `sh $VAULT/bin/lint.sh` - finding count <= the baseline from step 1.4.
2. Commits: one per change, message `defrag(<area>): <what and why in one line>`. No batch commits, no agent co-author line. Push.
3. Restart anything paused in step 1.3 (VPS: `sudo systemctl start memory-sync.timer`).
4. If typed pages (`concepts/`, `sources/`, ...) changed: `openclaw wiki compile` on the VPS (not installed on PC - note "compile pending" if running there).
5. Append to vault `log.md`: `## [YYYY-MM-DD] defrag | applied <N> mechanical, proposed <M> merges/deletions -> audits/YYYY-MM-DD-defrag-report.md`.
