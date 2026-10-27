# Fleet publisher output contract

`fleet-publisher.sh` reads each fleet home's `state/home-summary.json`
(schema `fm-secondmate-home-summary.v1`, read as-is) and writes one merged
`OUT_DIR/home-summary.json` plus `OUT_DIR/reports/` and, from the homes' daily
fleet metrics, `OUT_DIR/metrics/daily.jsonl`, plus the sanitised quota copies (last sections). The homes are
`FM_HOMES`, a space-separated list of absolute paths (default `FM_HOME` alone); a
home's id is its directory's basename and must be unique. The dashboard binds to
this output, not to the source summaries. Operations (install, alerts, metrics) are in
`docs/runbook.md` "Fleet publisher"; behaviour is pinned by
`scripts/tests/test_fleet_publisher.py`.

Every source field passes through unchanged, except that `counts.decisions_open`
no longer includes a child home's folded decisions (below); the fields below are
added or, with more than one home, aggregated. The fleet homes are only ever read (files,
`fm-captain-hold.sh binding`, `tasks-axi show`, and for the status page
`fm-tasks-axi.sh list`/`show` and `fm-capacity.sh --json`).

| Where | Field | Meaning |
| --- | --- | --- |
| top | `published_epoch` | Publish time |
| top | `status_page` | The living status page's sections (schema `fleet-status-page.v1`, below), or `null` when they could not be built |
| top | `homes[]` | One entry per home, in `FM_HOMES` order: `id`, `path`, `published`, `valid`, `reason`, and for a published home its `schema`, `generated_epoch`, `state`, `counts`, `invalidity` |
| every item of every top-level list | `home_id` | The id of the home the item came from (`active_children`, `decisions_open`, `holds`, `queued`, `landed`, `endpoints`, `omitted`) |
| `homes[]` | `invalidity` | The home summary's own `invalidity` (`{kind, ids}`: why `valid` is false, e.g. kind `terminal_in_flight` for finished work still waiting on validation or merge), passed through as-is; `null` when the summary has none |
| `counts` (object, and each `homes[].counts`) | `queued_open` | The queued items with no hold: neither `hold_kind` nor `hold_bucket` set. `counts.queued` still includes held ones |
| `decisions_open[]` | `board_url` | The open Lavish session for the decision, else `null` |
| `landed[]` | `report_url` | The served copy of the item's finished `report.md`, else `null` |
| `active_children[]` | `started_at` | Epoch of the task's latest `task.dispatched` in the home's ledger; absent when the ledger has none |
| `landed[]` | `landed_at` | Epoch of the task's latest `task.merged` in the ledger, else its latest `task.cleaned_up`; absent when neither exists |
| `queued[]` | `priority` | The number in the item's `(priority: N)` on its `- [ ] <id> - ...` line in the home's `data/backlog.md`; absent when the line has none |
| `decisions_open[]` | `ask` | The validated `data/<id>/ask.json` when the snapshot's own item has no `ask` (below) |

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

- **`ask`**: read only for a decision whose snapshot item has no `ask` (the
  snapshot's own wins). The file must be a regular file whose real path lies
  under the home's `data/`, at most 64 KiB, free of credential patterns, and
  the contract's shape (`docs/decision-contract.md` "Where the options come
  from"): a non-empty `question`, at most 6 `options` (`id` slug, `label`,
  at most one `recommended`, and it first), boolean `free_text_allowed`, and
  an optional `https://` `link`. It is published normalised (`recommended`
  always a boolean, `link` `null` when absent). An invalid file is logged and
  skipped; the decision then carries no `ask`, as before.
- **The PC (remote home).** A fleet home caches the PC's secondmate summary in
  its `state/secondmate-summary-cache/`; `PC_SUMMARY_FILE` names the file, else
  the first home in `FM_HOMES` order whose cache holds exactly one `*.json`
  gives it (a home with none, or several, is skipped; no such home means no PC
  entry). `homes[]`
  gains one entry appended after the aggregation:
  `{id: "pc" (PC_HOME_ID), path: null, published: true, remote: true, valid: true, reason: null, last_seen_epoch, state}`.
  `state` is `working` (the summary has active children), `idle`, or `off`
  (older than `PC_STALE_SECONDS`, default 1200, or missing, unreadable, not a
  home summary, or credential-bearing; then `last_seen_epoch` is the summary's
  time, or `null`). It carries no counts and contributes no list items. It never
  affects the fleet's `valid`, `reason`, `counts` or `generated_epoch`, never
  fails the run, and is left out of the `fleet_home_summary_*` metrics: a PC
  that is off is normal.

## Status page sections (`status_page`)

`fleet_status.py` (beside the script; installed as
`/usr/local/bin/lifekit-fleet-status.py`; `FLEET_STATUS` overrides it) builds
the sections the dashboard landing renders. It reads structured sources only,
and only the on-host homes in `FM_HOMES`: the PC appears through its summary
alone. Each home is read with `FM_HOME` set to it, through its own
`bin/fm-tasks-axi.sh` (with `TASKS_AXI`'s directory on `PATH`) and
`bin/fm-capacity.sh --json`. Per-home freshness stays in `homes[]` (above).

| Field | Meaning |
| --- | --- |
| `schema`, `generated_epoch` | `fleet-status-page.v1`, the publish time |
| `records[]` | Every open record each home's backlog lists, and each done record closed within the last 7 days (`list --limit 1000`; a list that fills the limit adds an `errors[]` line): `id`, `home_id`, `title`, `state` (`queued`, `held`, `in_flight`, `done`), `repo` (the record's own, `null` when it has none), `kind`, `priority` (number or `null`), `blocked`, `closed` (date or `null`), `pr_url` (its first `pr:` link), `hold` |
| `records[].hold` | `null` unless held: `{kind, owner, until, reason}`. `owner` is `captain` (a captain call), `external` (an outside wait), else `firstmate` (parked, or a kind it does not know); `until` and `reason` are the restart condition |
| `projects[]` | One row per `repo` value, then `name: "no project"` (`repo: null`) for records without one: `github` (owner/name), `in_flight`, `queued`, `held`, `last_landed` (`{id, home_id, title, closed, pr_url}` of the latest done record closed in the last 7 days, else `null`), `open_prs` and `open_prs_as_of` (epoch), `plan_url`. Busiest first, "no project" last |
| `capacity[]` | Per on-host home: `home_id`, `machine`, `running` (lanes), `allowed` (the declared cap, `null` when none is), `cap_status`, `quota` (runway status or `null`), `generated_epoch`, `idle_reason`; then one PC row (`running`/`allowed` `null`, `generated_epoch` its last-seen time) |
| `queue` | `count` (open queued records, all homes), `held`, `top` (10: `id`, `home_id`, `title`, `repo`, `priority`, `blocked`), `throughput` (`{days: 7, from, to, days_with_data, merged_prs, runs}` over the 7 whole UTC days before today, from the daily metrics), from which the dashboard computes an ETA |
| `intake` | The shared job queue's depth: `{waiting, running, done_24h, failed_24h}`, or `null` |
| `how_it_works[]` | `{id, title, url}` for `intake`, `queue-to-merge`, `monday-loop`: the workflow's vault owner page, `url: null` until that page exists |
| `follow_ups[]` | The owner pages still to be written |
| `errors[]` | One line per source that could not be read this run |

- **Records** are read whole. A row the list truncates (a long title) is read
  again with `show <id> --full`; the resolved row is cached in the publisher's
  state directory (`status-records.json`) against the list line, so an
  unchanged record is not shown again. The shows share a 20 s budget per run;
  a cut row past it keeps the list's cut cells and is counted in `errors`,
  and the cache still saves, so a cold cache fills over a few runs. A record whose text matches a
  credential pattern is left out and counted in `errors`. A missing `repo` is
  never an error: the record shows under "no project".
- **Repo to GitHub**: a `repo` already of the form owner/name is used as is;
  any other is the `origin` of the first home's `projects/<repo>` clone. No
  clone means `github`, `open_prs` and so on are `null`. Two spellings of one
  repo are two rows; the fix is at the record.
- **Open PRs**: `FLEET_GH` (default `gh`; empty skips) `pr list -R <owner/name>
  --state open`, cached per repo for `FLEET_PR_TTL` seconds (default 600) in
  `status-open-prs.json`. A failed refresh keeps the last count and its
  `open_prs_as_of`.
- **Plan link**: the vault (`FLEET_VAULT`, default `/srv/memory`) page
  `projects/<p>/plan.md`, where `<p>` is the repo's name (after any owner/) or
  its entry in `FLEET_PLAN_MAP` (`"<repo>=<project> ..."`, host config), linked
  on GitHub from the vault's own `origin`; `null` when there is no such page.
- **Idle reason**, the first that holds: `cap-reached` (running at or over a
  declared cap), `quota` (runway `exhausted_now`), `all-held` (nothing queued,
  something held), `queue-empty`; else `null` (room and open work). A home whose
  backlog is unreadable gets `null`. The PC row is `host-offline` when its
  state is `off`, else `null`.
- **Intake** runs `jobq.py depth` in the running `openclaw-gateway` container
  (found by compose labels) with `docker exec`; `FLEET_INTAKE_CMD` replaces
  that command (empty skips intake). Counts only: no job, label or result.
- **Failure** of any source empties its own part and adds an `errors` line.
  The helper is bounded to 50 s; if it fails, overruns, prints no JSON object,
  or prints anything matching a credential pattern, `status_page` is `null` and
  the rest of the summary publishes as before.

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

## Quota history and snapshot (`OUT_DIR/metrics/quota.jsonl`, `quota-snapshot.json`)

Both files are percentages, reset times and slot numbers only: no account key,
email, plan detail, source tool or error text ever leaves the host.

- **`quota.jsonl`**: each home's `data/fleet-metrics/quota.jsonl` (hourly
  `quota-axi` samples), under the daily metrics' guards (regular file under the
  home's `data/`, at most 4 MiB), reduced to
  `{ts, slot, five_hour, seven_day, fable_week}` where each window is
  `{percent_used, resets_at}` (`resets_at` an ISO time; both `null` for an
  unreadable sample). `slot` is stable: the account key's slot in
  `QUOTA_SLOT_MAP` (`"<key>=<slot> ..."`, host config) if pinned there, else the
  next free slot the first time the key is seen, kept in a private slot ledger
  (`quota-slots`, sha256 of the key to slot, mode 600) under the publisher's
  state directory (`FLEET_STATE_DIR`, else `STATE_DIRECTORY`, else
  `~/.local/state/lifekit-fleet-publisher`), never under `OUT_DIR`, and never
  reassigned; `null` for a row with no key, and a row whose key has no slot
  (characters outside `A-Za-z0-9._:-`) is skipped. A row with a non-integer `ts` or a percentage outside
  0-100 is skipped. One row per (`ts`, `slot`), the later winning, sorted by
  `ts`; rows already published stay when the source is trimmed. The file is
  rewritten only when its content changed. A source with a credential pattern
  is not published.
- **`quota-snapshot.json`**: the quota gauge's textfile
  (`QUOTA_PROM`, default `TEXTFILE_DIR/claude_quota.prom`) as
  `{schema: "fleet-quota-snapshot.v1", generated_epoch (the file's mtime), windows: {<window id>: {percent_remaining, burn_multiple, reserve_percent_points, resets_at (epoch)}}, runway_seconds, projected_exhausted_at (epoch), runway_status, limiting_window}`.
  The gauge's source is `quota-axi`'s one active account, so the snapshot has no
  slot. Absent when the gauge file is missing.
- A failed write is logged and fails the run; missing sources leave the files as
  they were. The dashboard backend's `backend/infra.json` needs entries for the
  two new files before it can serve them.
