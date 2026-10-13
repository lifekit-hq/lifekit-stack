# Fleet publisher output contract

`fleet-publisher.sh` reads each fleet home's `state/home-summary.json`
(schema `fm-secondmate-home-summary.v1`, read as-is) and writes one merged
`OUT_DIR/home-summary.json` plus `OUT_DIR/reports/` and, from the homes' daily
fleet metrics, `OUT_DIR/metrics/daily.jsonl` (last section). The homes are
`FM_HOMES`, a space-separated list of absolute paths (default `FM_HOME` alone); a
home's id is its directory's basename and must be unique. The dashboard binds to
this output, not to the source summaries. Operations (install, alerts, metrics) are in
`docs/runbook.md` "Fleet publisher"; behaviour is pinned by
`scripts/tests/test_fleet_publisher.py`.

Every source field passes through unchanged, except that `counts.decisions_open`
no longer includes a child home's folded decisions (below); the fields below are
added or, with more than one home, aggregated. The fleet homes are only ever read (files,
`fm-captain-hold.sh binding`, `tasks-axi show`).

| Where | Field | Meaning |
| --- | --- | --- |
| top | `published_epoch` | Publish time |
| top | `homes[]` | One entry per home, in `FM_HOMES` order: `id`, `path`, `published`, `valid`, `reason`, and for a published home its `schema`, `generated_epoch`, `state`, `counts` |
| every item of every top-level list | `home_id` | The id of the home the item came from (`active_children`, `decisions_open`, `holds`, `queued`, `landed`, `endpoints`, `omitted`) |
| `counts` (object, and each `homes[].counts`) | `queued_open` | The queued items with no hold: neither `hold_kind` nor `hold_bucket` set. `counts.queued` still includes held ones |
| `decisions_open[]` | `board_url` | The open Lavish session for the decision, else `null` |
| `landed[]` | `report_url` | The served copy of the item's finished `report.md`, else `null` |

- **One home** (today's single `FM_HOME`): the output is that home's summary with
  the fields above added; nothing else changes.
- **More than one home**: every top-level list is the homes' lists concatenated
  in `FM_HOMES` order. `generated_epoch` is the oldest home's (so staleness shows
  when any home stops refreshing), `valid` is true only when every home is valid
  and published, `reason` is each home's reason as `<id>: <reason>` joined with
  `; ` (else `null`), and `counts` sums each numeric count. Every other top-level
  field (`schema`, `home`, `state`, `contributions`, ...) is the first home's;
  `homes[]` has each home's own.
- **A child home's decisions count once.** A home's `data/secondmates.md` lists its
  secondmates as `- <id> - ... (home: <path>; ...)`. When that `home:` path is a home
  this run also publishes, the parent's `decisions_open` items whose `id` is that
  secondmate id (the parent folding its child's waits in) are left out of the lists,
  `counts.decisions_open` (the home's own and the summed one), `fleet_decisions_open`
  and the oldest-decision age: the child's own summary is authoritative, and the
  published count equals the listed items. A count beyond the list's cap loses only
  the folded items that are listed. Items naming a secondmate whose home is not
  published (or whose summary is unreadable) are kept.
- **`queued_open`** is read from each home's `queued` list, so it is exact while that
  list holds every queued item; if the cap cuts the list it counts the listed unheld
  items (the live-held ones sort last, so they are cut first). A home with no `counts` object, or
  no `queued` array, gets no `queued_open`.
- **Unreadable home**: a home whose summary is missing, malformed, or matches a
  credential pattern is left out of the lists and listed in `homes[]` with
  `published: false`, `valid: false` and a `reason`; the rest still publish and
  the run exits nonzero. When no home can be published nothing is written.
- **Ids are per home.** Two homes can hold the same task or decision id (one
  home can even repeat an id across its own decisions), so a consumer keys an
  item by `home_id` plus `id`, and routes an answer to the inbox of `home_id`.
- **`board_url`**: an open Lavish session fed by the home's captain-hold binding
  (`fm-captain-hold.sh binding lavish-<session id>`, run with `FM_HOME` set to that
  home, succeeds) whose file lives under the hold's origin task directory
  (`Captain hold origin:` in `tasks-axi show`), or whose binding names the
  decision. Falls back to the newest open session under the home's
  `data/<decision id>/`, else `null`. `tasks-axi` finds the backlog from its
  working directory, so the publisher runs it from the home, at the absolute
  path in `TASKS_AXI` (the installer resolves it for the admin account and the
  unit carries it; the publisher puts its directory, and so node, on `PATH`). A
  failed lookup is logged to stderr and the decision falls back to the directory
  lookup.
- **`report_url`**: landed items whose `report_path` (relative to the home) is a
  regular `.md` file under the home's `data/`, free of credential patterns and
  at most 2 MiB, are copied to `OUT_DIR/reports/<id>/report.md`; the url is
  `REPORT_URL_BASE/<id>/report.md` (default base `reports`). The url is relative
  to the published summary and the dashboard resolves it; the route serving
  `OUT_DIR/reports/` is the dashboard's. Report ids share one namespace across
  homes: on a clash the first home listed keeps the report and the later item's
  `report_url` is `null`. Reports that fail a check are skipped, never linked.
  Files are replaced one rename at a time, so a report the previous summary
  linked is never briefly absent.

## Daily fleet metrics (`OUT_DIR/metrics/daily.jsonl`)

Each home's `data/fleet-metrics/daily.jsonl` (schema `fleet-metrics-daily.v1`, one
row per date per home, appended by that home's capture job) is validated and
copied to `OUT_DIR/metrics/daily.jsonl`, world-readable, beside
`home-summary.json`. The dashboard reads this file, never the home.

- **Guards** match the reports': a regular file whose real path lies under the
  home's `data/`, at most 4 MiB, free of credential patterns; otherwise the home
  contributes nothing and the reason is logged.
- **Validation** per row: the schema tag, `date` as `YYYY-MM-DD`, `home` equal to
  the publishing home's id, `computed_at`, boolean `partial`/`backfilled`,
  non-negative integer counts, a non-negative `stall_task_hours`, `asks_new`
  (`t0`, `t1`, `t2`, `untiered`), `by_tier` (`runs`, `first_pass` per tier), and
  number-or-null gauges. A bad row is skipped and counted on stderr; it never
  stops the summary. Only the schema's fields are published; unknown ones are
  dropped.
- **One row per (date, home)**: the last valid row in file order wins, and it
  replaces a row already published for that date, so a rerun or a final row
  superseding a `partial` one never duplicates. Rows already published stay
  until a newer row for their date replaces them, so history survives the source
  being trimmed. The file is sorted by date, then home.
- **Atomic and quiet**: temp file plus rename, only when the content changed. A
  missing or wholly invalid source leaves the file as it was. A write failure is
  logged and fails the run (the summary is already published).
