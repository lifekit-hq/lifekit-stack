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
| top | `scheduled` | The fleet's scheduled work as one list: every repo timer, every OpenClaw cron row, and Kit's and Ledger's active standing intents (below, "Scheduled work"); `[]` when it cannot be built |
| top | `homes[]` | One entry per home, in `FM_HOMES` order: `id`, `path`, `published`, `valid`, `reason`, and for a published home its `schema`, `generated_epoch`, `state`, `counts`, `invalidity` |
| every item of every top-level list | `home_id` | The id of the home the item came from (`active_children`, `decisions_open`, `holds`, `queued`, `landed`, `endpoints`, `omitted`) |
| `homes[]` | `invalidity` | The home summary's own `invalidity` (`{kind, ids}`: why `valid` is false, e.g. kind `terminal_in_flight` for finished work still waiting on validation or merge), passed through as-is; `null` when the summary has none |
| `counts` (object, and each `homes[].counts`) | `queued_open` | The queued items with no hold: neither `hold_kind` nor `hold_bucket` set. `counts.queued` still includes held ones |
| `decisions_open[]` | `board_url` | The open Lavish session for the decision, else the first http(s) address in its `question`, `reason`, `summary`, `status` or ask question (a Lavish `/session/<id>` address on a host the home's Lavish sessions use, any host when none is known, before any other link), else `null` |
| `decisions_open[]` | `ask.link` | When the item has an `ask` with no link, its `board_url` |
| `active_children[]`, `queued[]`, `holds[]` | `open_url` | The same board address as `board_url`, found the same way; absent when the item has none |
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
  (`Captain hold origin:` in `tasks-axi show`, run only for decisions the home's own
  `tasks-axi list` returned, so one with no backlog record is silent), or whose binding names the
  decision. Falls back to the newest open session under the home's
  `data/<decision id>/`, else the http(s) address in the item's own text (see the
  table), else `null`. `tasks-axi` finds the backlog from its
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
  time, or `null`). It carries no counts and contributes no list items (its
  status page capacity row is below). It never affects the fleet's `valid`,
  `reason`, `counts` or `generated_epoch`, never fails the run, and is left out of the `fleet_home_summary_*` metrics: a PC
  that is off is normal.

## Run cadence and what a loaded host does

The timer fires every minute, and a run that outlasts a minute delays the next
one, so a heavily loaded host cannot be allowed to turn the slow helpers into
the stamp's critical path. Order and cadence of a run:

| Section | Cadence | Writes | Unloaded cost (live host) |
| --- | --- | --- | --- |
| homes (read each home, join boards, reports, hold origins) | every run | the merged summary | 8 s |
| `status_page` (`fleet_status.py`) | every run | `status_page` in the summary | 3 s |
| publish (the summary, `fleet.prom`) | every run | `home-summary.json`, `fleet.prom` | under 1 s |
| daily metrics, quota history and snapshot | every run | `metrics/` | under 1 s |
| taken-for-you feed | every run | `taken-for-you.json` | under 1 s |
| screen changes feed (`screen_changes.py`, gh) | every `FLEET_SCREENS_EVERY` s (default 300) | `screen-changes.json`, `screen-changes/` | 18 s |
| activity journal (`activity_journal.py`) | every `FLEET_JOURNAL_EVERY` s (default 900) | `journal/` | 19 s |
| scheduled work, gateway side (`scheduled.py gateway`) | every `FLEET_SCHEDULED_EVERY` s (default 300) | `scheduled-gateway.json` in the state directory, joined into `scheduled` from the next run | 11 s |

- **The stamp comes first.** `fleet.prom` (`fleet_summary_generated_timestamp_seconds`,
  `fleet_publisher_last_success_timestamp_seconds`) is replaced before any slow
  helper starts, so a slow helper never delays it.
- **A section that is not due leaves its previous output as it was.** Cadence stamps
  are the files `cadence-<section>` in the publisher's state directory, kept on the
  run's own clock (`FLEET_NOW` when pinned); a stamp from the future counts as due.
  A helper that fails is retried on its cadence, not every minute.
- **A run budget.** A slow section does not start once the run is `FLEET_SLOW_BUDGET`
  seconds old (default 120); it keeps no stamp, so it is due again next run.
- **A run that fails still refreshes the freshness gauges.** If a run dies before
  `fleet.prom` is replaced, the four freshness gauges (`fleet_summary_valid`,
  `fleet_summary_generated_timestamp_seconds`, `fleet_home_summary_valid{home}`,
  `fleet_home_summary_generated_timestamp_seconds{home}`) are rewritten in place from
  each home's own summary; every other line, and the last-success time, stay the
  last good run's. The value is the home's real `generated_epoch`, so a home whose
  own loop died still ages. With no readable home nothing is touched, so the
  stale-file rules fire.
- **The run owns its working directory.** It is `OUT_DIR/.work.*` (mode 700), not a
  `/tmp/tmp.*`, so no temp cleaner on a busy host can remove it between two steps.
  A directory a killed run left is removed once it is a day old.
- **Timings.** Every run ends with one stderr line in the unit's journal, e.g.
  `fleet-publisher: timings_ms total=19950 homes=14560 status_page=3907 publish=189 ... skipped=screens:cadence,journal:cadence`.

## Scheduled work (`scheduled`)

One entry per thing that runs on a schedule, built by `scheduled.py` (installed
as `lifekit-fleet-scheduled.py`), sorted timers, cron rows, intents, then name:

| Field | Meaning |
| --- | --- |
| `kind` | `timer` (a `scripts/**/*.timer` unit), `cron` (an OpenClaw cron row) or `intent` (a standing intent) |
| `name` | The timer unit, the cron row's name, or the intent's description (whitespace folded, 120 characters at most) |
| `owner`, `owner_kind` | The service the timer starts (`unit`), or the agent id of the cron row or intent (`agent`; Ledger is `finance`); `null` for a missing cron row |
| `schedule` | The timer's `On*=` keys joined with `; `; `cron <expr> (<tz>)`, `every <n><unit>` or `at <iso>` for a cron row; `when: <trigger keywords>` for an intent |
| `next_run_epoch`, `last_run_epoch` | From `systemctl list-timers` or the cron row's state; an intent's last run is its last firing and it has no next run; `null` when unknown |
| `last_result` | The service's systemd `Result` (`running` while it runs), the cron row's last run status, or the intent's status (`pending`, `armed`, `fired`) |
| `script` | The repo file that declares it: the `.timer` file, or the `scripts/ensure-*.sh` naming the cron row; `null` for a live row nothing in the repo declares, and for intents |
| `state` | `live`; `disabled` (a cron row switched off); `missing` (declared in the repo but absent from systemd or the gateway); `unknown` (that side could not be read, or its cached gateway rows are stale) |
| `stale_seconds` | Cron rows and intents only, and only when `unknown` from the gateway side: how long ago that part was last read; `null` when it never was |
| `expires_epoch`, `id` | Intents only: when it lapses, and its id |

- **Declared comes from the checkout, at install.** The installer writes
  `lifekit-fleet-scheduled.json` beside the script (`scheduled.py manifest`): the
  repo's timer files and the cron rows each declaring script names
  (`check-cron-declarations.sh --list`, through `cron_check_declares` in
  `scripts/lib/cron-check.sh`, the same declarations the deploy sweep checks).
  The publisher never reads the checkout; a new timer or declaring script shows
  after a re-install.
- **Live comes from systemd every run, and from the gateway on cadence.**
  `openclaw cron list --all --json` takes about ten seconds, so the slow
  `scheduled` section caches it with the intents in the state directory, and
  every run joins the cache. Each part (cron rows, intents) is stamped with
  the time it was read; a read that fails keeps that part and its old stamp.
  A part last read more than three cadences ago (`3 × FLEET_SCHEDULED_EVERY`)
  lists its rows, and for cron the declared ones too, as `unknown` with
  `stale_seconds`, never as `live` or `missing`.
- **Standing intents** live in each agent's own SQLite store,
  `agents/<agent id>/agent/openclaw-agent.sqlite` under the gateway state
  directory, table `standing_intents` (OpenClaw docs, concepts/standing-intents).
  They are read read-only in the gateway container for the agents in
  `FLEET_INTENT_AGENTS` (default `kit finance`: Kit and Ledger); only `pending`,
  `armed` and `fired` ones are listed. An agent with no store file or no
  `standing_intents` table is skipped; the other agents' intents are still
  listed. Any other store error (a changed schema, a lock) fails the intents
  read, so it keeps its previous rows and ages to `unknown`.
- **Nothing private is kept.** Payloads, prompts, delivery targets, senders and
  cron row ids are dropped before the cache is written; the summary's credential
  check covers the list too.
- Env for tests and drop-ins: `FLEET_SCHEDULED` (helper), `FLEET_SCHEDULED_MANIFEST`,
  `FLEET_SYSTEMCTL`, `FLEET_CRON_CMD`, `FLEET_INTENTS_CMD` (a command printing the
  CLI's JSON or a JSON list of intent rows; empty skips that read).

## Status page sections (`status_page`)

`fleet_status.py` (beside the script; installed as
`/usr/local/bin/lifekit-fleet-status.py`; `FLEET_STATUS` overrides it) builds
the sections the dashboard landing renders. It reads structured sources only,
and only the on-host homes in `FM_HOMES`: the PC appears through its summary
alone, which the publisher hands it as `--remote`. Each home is read with
`FM_HOME` set to it, through its own `bin/fm-tasks-axi.sh` (with `TASKS_AXI`'s
directory on `PATH`) and `bin/fm-capacity.sh --json`. Per-home freshness stays in `homes[]` (above).

| Field | Meaning |
| --- | --- |
| `schema`, `generated_epoch` | `fleet-status-page.v1`, the publish time |
| `records[]` | Every open record each home's backlog lists, and each done record closed within the last 7 days (`list --limit 1000`; a list that fills the limit adds an `errors[]` line): `id`, `home_id`, `title`, `state` (`queued`, `held`, `in_flight`, `done`), `repo` (the record's own, `null` when it has none), `kind`, `priority` (number or `null`), `blocked`, `closed` (date or `null`), `pr_url` (its first `pr:` link), `hold` |
| `records[]` | `created`, `stalled` | `created` is the record's creation date (`null` when it has none). `stalled` is `null` or `{since, threshold, owner}` (below); only a queued or in-flight record can carry it |
| `records[].hold` | `null` unless held: `{kind, owner, until, reason}`. `owner` is `captain` (a captain call), `external` (an outside wait), else `firstmate` (parked, or a kind it does not know); `until` and `reason` are the restart condition |
| `projects[]` | One row per project (see "One row per project"), then `name: "no project"` (`repo: null`) for records without one: `github` (owner/name), `in_flight`, `queued`, `held`, `held_external` (the held ones whose `hold.owner` is `external`: blocked on an outside party, shown as a state and never sent as a message), `last_landed` (`{id, home_id, title, closed, pr_url}` of the latest done record closed in the last 7 days, else `null`), `open_prs` and `open_prs_as_of` (epoch), `plan_url`. Busiest first, "no project" last |
| `capacity[]` | Per on-host home: `home_id`, `machine`, `running` (lanes), `allowed` (the declared cap, `null` when none is), `cap_status` (`ok`, `none` for no cap declared, which the page shows as "no cap declared", or `invalid`), `quota` (runway status or `null`), `generated_epoch`, `idle_reason`; then one PC row (below) |
| `queue` | `count` (open queued records, all homes), `held`, `held_external` (as in `projects[]`), `top` (10: `id`, `home_id`, `title`, `repo`, `priority`, `blocked`, `stalled`), `throughput` (`{days: 7, from, to, days_with_data, merged_prs, runs}` over the 7 whole UTC days before today, from the daily metrics), from which the dashboard computes an ETA |
| `intake` | The shared job queue's depth: `{waiting, running, done_24h, failed_24h}`, or `null` |
| `how_it_works[]` | `{id, title, url}` for `intake`, `queue-to-merge`, `monday-loop`: the workflow's vault owner page (`system/workflows/<id>.md`), `url: null` when the vault lacks it |
| `stalled[]` | The stalled items grouped by owner (see "Stalled items"): one group per configured owner, `{owner, count, items[]}`, or `null` when the thresholds could not be read |
| `stalled_sources` | Per source (`backlog`, `github`, `jobq`, `a2a`, `clip`, `scout`, `kit_held`): `{read, reason}` (`kit_held` adds `open`, every listed item, stalled or not); `read: false` says why a source contributed nothing, or `null` with `stalled` |
| `problems` | The brief's Problems line (below): `{count, shown[], more, line}`, or `null` when there are none |
| `side_hustles` | The side-hustle lines and week-sheet rows (below): `{channels[], lines[], week_rows[], credential, brief_day, week_of, feed_read}`, or `null` when `FLEET_CLIP_STATS_FEED` is unset |
| `clip_queue` | One row per clip channel (see "Clip queue"): `{schema, channels[], egress, queue_run, unavailable[]}`, or `null` when `FLEET_CLIP_STATS_FEED` is unset |
| `items[]`, `item_sources` | One record per open finance item and needs-you decision, with the item envelope (see "Queue items"), and each source's `{read, reason}` |
| `delivered[]` | The 3-line outcomes of accepted decisions whose items are all closed (see "Delivered decisions"), or `null` when the mark file could not be read |
| `follow_ups[]` | The owner pages still to be written |
| `errors[]` | One line per source that could not be read this run |

- **Records** are read whole. A row the list truncates (a long title) is read
  again with `show <id> --full`; the resolved row is cached in the publisher's
  state directory (`status-records.json`) against the list line, so an
  unchanged record is not shown again. The shows share a 20 s budget per run;
  a cut row past it, or whose `show` fails, keeps the list's cut cells (as
  does a field `show` leaves out), is not cached, and is counted in `errors`.
  The cache still saves, so a cold cache fills over a few runs. A record whose
  text matches a credential pattern is left out and counted in `errors`. A missing `repo` is
  never an error: the record shows under "no project".
- **Repo to GitHub**: a `repo` already of the form owner/name is used as is;
  any other is the `origin` of the first home's `projects/<repo>` clone. No
  clone means `github`, `open_prs` and so on are `null`.
- **One row per project**: spellings of one repo are one row. A project is the
  repo's name (after any owner/) or its `FLEET_PLAN_MAP` entry, so `x`, `owner/x`
  and an alias mapped to `x` (`"vault=x"`) count together, with their counts
  summed; a spelling with no slug joins the project's only known slug, while
  `a/x` and `b/x` stay two rows. The row's `repo`/`name` is its owner/name
  spelling when a record used one, else the alphabetically first short name.
  `vault=memory` is built in; further aliases are host config (`FLEET_PLAN_MAP`),
  which the installer carries into the unit like `FM_HOMES`
  (`sudo FM_HOMES="..." FLEET_PLAN_MAP="<repo>=<project> ..." bash install-fleet-publisher.sh`),
  and a re-install without it leaves the unit with none.
- **Clip stats feed**: `FLEET_CLIP_STATS_FEED` (absolute path of clip's
  `stats.jsonl`; only its parent directory is read) switches on the clip queue
  rows. It is host config the installer carries into the unit the same way
  (`sudo FM_HOMES="..." FLEET_CLIP_STATS_FEED=/home/denys/clip-data/queue/stats.jsonl bash install-fleet-publisher.sh`,
  plain characters `[A-Za-z0-9._/-]` only); a re-install without it leaves the
  unit with none and the rows `null`.
- **Open PRs**: `FLEET_GH` (default `gh`; empty skips) `pr list -R <owner/name>
  --state open`, cached per repo for `FLEET_PR_TTL` seconds (default 600) in
  `status-open-prs.json`. Refreshes share a 15 s budget per run; a failed or
  skipped refresh keeps the last count and its `open_prs_as_of` and adds an
  `errors[]` line.
- **Plan link**: the vault (`FLEET_VAULT`, default `/srv/memory`) page
  `projects/<p>/plan.md`, where `<p>` is the repo's name (after any owner/) or
  its entry in `FLEET_PLAN_MAP` (`"<repo>=<project> ..."`, host config; built
  in: `vault=memory`), linked
  on GitHub from the vault's own `origin`; `null` when there is no such page.
- **Idle reason**, the first that holds: `cap-reached` (running at or over a
  declared cap; a home with no cap declared never reads as this), `quota`
  (runway `exhausted_now`), `all-held` (nothing queued, something held),
  `queue-empty`; else `null` (room and open work). A home whose backlog is
  unreadable gets `null`.
- **The PC row** comes from the publisher's read of the PC's cached summary
  (`--remote`), never from the PC itself. Its state `off` gives a row of nulls
  with `idle_reason` `host-offline` and `generated_epoch` its last-seen time.
  On, a summary that carries `lane_capacity` (the PC home's
  `fm-lane-capacity.v1` document, as its `fm-capacity.sh --publish` writes it)
  fills the row like an on-host home's, with the idle reason's queue counts
  from the summary's `queued` list (an item with `hold_kind` or `hold_bucket`
  is held; no list, no queue-based reason). A summary with no `lane_capacity`
  gives the row of nulls with `idle_reason` `capacity-unpublished`; one that
  is not that document does the same and adds an `errors[]` line.
- **Intake** runs `jobq.py depth` in the running `openclaw-gateway` container
  (found by compose labels) with `docker exec`; `FLEET_INTAKE_CMD` replaces
  that command (empty skips intake). Counts only: no job, label or result.
- **Failure** of any source empties its own part and adds an `errors` line.
  The helper is bounded to 50 s; if it fails, overruns, prints no JSON object,
  or prints anything matching a credential pattern, `status_page` is `null` and
  the rest of the summary publishes as before.

## Stalled items (`status_page.stalled`)

One mechanical check over native age, no model (decision 0016, queue mechanics;
the envelope and receipt every door carries are the vault build standard
"Envelope and receipt at every door", 2026-11-16, and are not restated here).
An item whose state has not changed for its queue's threshold is `stalled`,
with its owner. The thresholds are **one config block**, `fleet-stalled.json`
beside `fleet_status.py` (installed beside it as `lifekit-fleet-stalled.json`;
`FLEET_STALLED_CONFIG` names another file). It is the only place the numbers
live: the dashboard reads the result, never decides. Change a number there and
the next run changes the result; there is no code change. A missing or malformed
file marks nothing (`stalled` and `stalled_sources` are `null`, one `errors[]`
line), never a guess.

| Queue | Stalled when (default) | Config key | `since` | Owner |
| --- | --- | --- | --- | --- |
| GitHub issue of a registered project (a project whose backlog records name a GitHub repo) | open, no `held_labels` label, no update for 7 days | `github.days`, `github.held_labels` | the issue's `updatedAt` | its `ops`/`dev`/`finance`/`life` label (or `class:<x>`), else `github.repo_owners[<owner/name>]`, else `github.default_owner` (`dev`) |
| Firstmate backlog record | `queued` or `in_flight` (held and done keep their own buckets), no status line for 48 hours | `backlog.hours` | the newer of the record's creation date and the last `[at=<epoch>]` in `<home>/state/<id>.status` | `firstmate` |
| jobq job | attempts exhausted (`attempts` at the queue's `max_attempts`, `failed` or `queued`), or `queued` more than 24 hours | `jobq.waiting_hours` | finish time (exhausted) or `queued_at` | the envelope's `class` when it is an owner, else `firstmate` |
| A2A thread | open and past its own `deadline`, or no turn for 24 hours | `a2a.idle_hours` | the `deadline`, or the last turn | the thread's `owner` when it is one, else `firstmate` |
| Clip channel | `buffer_days` under `clip.buffer_floor_days` (1), or no post inside `clip.slot_window_hours` (30; a channel that never posted counts) | `clip.buffer_floor_days`, `clip.slot_window_hours` | the last post (or the slot, never posted), or the check time for a short buffer | `clip.owner` (`firstmate`) |
| Scout | no new candidates file for `scout.idle_hours` (96) | `scout.idle_hours` | the newest `candidates/*.txt` of `FLEET_SCOUT_STATE`, by modification time | `scout.owner` (`firstmate`) |
| Kit's held item | an open line of Kit's daily notes that names a fleet task and has no shaping item, for `kit_held.hours` (24) | `kit_held.hours` | when `kit_held.py check` first saw it unfiled | `kit_held.owner` (`firstmate`) |
| Captain call | unchanged: days waited, as before | - | - | - |

- **Shape.** `records[].stalled` and `queue.top[].stalled` are `null` or
  `{since, threshold, owner}` (epoch seconds; `threshold` 0 for a rule with no
  duration, an exhausted job or a passed deadline). `stalled[]` is
  `[{owner, count, items: [{source, id, home_id, title, url, reason, since,
  threshold}]}]` in the config's `owners` order (a count of 0 is kept), oldest
  `since` first. `source` is `backlog`, `issue`, `job`, `thread`, `clip`, `scout` or `held`; `reason` is
  `no-status-line`, `no-update`, `attempts-exhausted`, `waiting`,
  `past-deadline`, `no-turn`, `no-post`, `buffer-short`, `no-output` or `not-filed`. The schema stays `fleet-status-page.v1`; these
  are new optional keys, and a consumer that ignores them is unchanged. The
  dashboard's rendering is its own issue.
- **GitHub issues** come from `gh issue list -R <owner/name> --state open`
  (200 at most; a full page adds an `errors[]` line), read in the same per-repo
  refresh, TTL and 15 s budget as the open PR count and cached beside it in
  `status-open-prs.json`; a failed refresh keeps the last issues.
- **Jobs** come from `jobq.py ages` in the gateway (`docker exec`, as intake):
  id, kind, state, times and the envelope class, never a label, payload or
  result. `FLEET_JOBS_CMD` replaces the command (empty skips); unset, it is
  skipped when `FLEET_INTAKE_CMD` is set.
- **A2A threads** come from `FLEET_A2A_CMD`, a command printing a JSON list of
  `{thread, owner, deadline, last_turn, state, title}` (times as epoch or ISO;
  a thread whose `state` is completed, failed, canceled or rejected is closed).
  Today no relay or session store the publisher can reach holds those, so
  unset, `stalled_sources.a2a` is `{read: false, reason: "no relay or session
  records reachable"}`: the page says so rather than guessing.
- **Clip and scout** thresholds are optional blocks of `fleet-stalled.json`; a
  file without one leaves that source unread (`stalled_sources` says so). The clip
  rows are read from the queue directory (see "Clip queue" below); the scout reads
  `FLEET_SCOUT_STATE` (its state dir, `~/.local/state/lifekit-scout` by default for
  the scout's own unit; the publisher does not guess it, so unset the source is
  `read: false`), and a state dir with no candidates file yet is unread, not stalled.
- **Kit's held items** (lifekit-stack#684) come from `FLEET_KIT_HELD_CMD`, a
  command printing the JSON list `scripts/kit-held/kit_held.py check` prints
  (`{id, note, title, since}`: open lines of Kit's daily notes that name a fleet
  task and carry no `(shaping: <id>)` of an existing shaping item). An item is a
  stalled row (`source` `held`, `reason` `not-filed`) once it has waited
  `kit_held.hours`; `stalled_sources.kit_held.open` counts all of them. Unset,
  or with no `kit_held` block, the source is `read: false`. Setup: docs/runbook.md,
  "Held items become shaping items".
- **Failure** of one source empties only its own rows, sets its
  `stalled_sources` entry to `read: false` and adds an `errors[]` line.

## Problems (`status_page.problems`)

What is wrong today, computed mechanically from the stalled rows above (stalled
backlog items, issues, jobs and threads; an exhausted job is the failed
scheduled job). No new detector: red CI on a default branch is not read by the
publisher, so it is not a problem here until a source for it exists.

- **Shape.** `count` (all problems), `shown` (the 3 oldest by first-seen, each
  `{source, id, home_id, owner, reason, label, url, first_seen, age_days}`),
  `more` (`count` minus 3, else 0) and `line`, the one brief line
  `Problems: <what> (<owner>, <n>d); ...; +<N> more`. No problems: `problems`
  is `null` and the brief prints no line.
- **Age** is whole days since the first run that saw the problem, not the item's
  own `since`. The first-seen mark lives in the publisher's state
  (`problems-seen.json`) and is dropped only when a run that read the problem's
  source no longer finds it, so the age resets only when the problem clears; a
  source that could not be read keeps its marks. An unreadable mark file gives
  `problems: null` and an `errors[]` line.
- **The brief** (`scripts/ensure-today-brief-fleet.sh`) prints `line` exactly,
  as one line of its Fleet part, so the 12-line cap holds.

## Side hustles (`status_page.side_hustles`)

Per-channel numbers for the owner's side hustles, read only from clip's stats
feed (`stats.jsonl`, written by `clip stats`; `FLEET_CLIP_STATS_FEED` is its
path, unset leaves the section `null`). Nothing is scraped, and clip's
collection is not touched.

- **When.** `lines` (the brief's) are filled on Monday and Thursday and empty on
  other days; `week_rows` (the Monday week sheet's, one weekly row per channel,
  `week_of` the Monday's date) on Monday only. The day is read in the brief's time
  zone (`fleet-decisions.json`). `channels[]` always holds each channel's verdict.
- **The line.** `<channel>: 7d views N · subs N · uploads N · best Short <clip> (N views)`,
  from the channel's newest `channel` row: `days7.views`, `subscribers`, uploads
  in the period (its `videos_recorded` less the count at the last row at least 7
  days older) and the most-viewed Short of that run's `video` rows. A number the feed
  does not hold is the value `no data` (never `0`; a real zero stays `0`); a channel
  whose newest row is older than 7 days or has no Analytics window is `<channel>: no data`.
- **Credential.** A channel whose newest row is `needs_reconsent` (or clip's
  credentials-rejected error) shows no numbers; one shared line `Side hustles: YouTube
  Analytics needs consent again for <channels> - owner: run clip auth ...` stands for all.
- **Unreadable feed.** One line `Side hustles: no data (stats feed unreadable)`, one
  `errors[]` line, and one `no_data` week row.
- **The brief** (`scripts/ensure-today-brief-fleet.sh`) prints `lines` as given, 3 at
  most (the credential line is always kept; the publisher folds channel lines beyond the
  rest into `+N more channels not shown`), so the
  12-line cap holds. The week sheet itself is drafted by firstmate in the vault from
  `week_rows`.

## Clip queue (`status_page.clip_queue`)

One row per clip channel, read only from the clip queue's own outputs in the
directory holding the stats feed (`FLEET_CLIP_STATS_FEED`'s parent; unset leaves
the section `null`): `daily-check.json`, `egress.json`, `queue-run.json`, and
each channel's `pending/` sources (their `added:` line, for the oldest age).
Nothing is written to them and clip is not changed.

- **Rows.** `channels[]` has `cosmos`, `mind`, `neural`, `yc` and `vice-footage`
  always (then any other channel `daily-check.json` lists), each
  `{channel, status, waiting, oldest_age_s, buffer_days, egress, stalled,
  stalled_reason, unavailable[]}`: `waiting` is `buffer_waiting`, `oldest_age_s` the
  age of the oldest waiting source (`null` when nothing waits), `buffer_days` as
  clip counts it, `egress` the queue-wide state (`up`, `pc-down`, `relay-down`,
  `bot-check`, `cookies-stale`), `stalled` the flag the stalled sources use
  (`stalled_reason` `no-post` or `buffer-short`). Each row also carries the item
  envelope (see "Queue items"): `owner: "clip"` (the clip pipeline), `class:
  "ops"`, `store: "clip-queue"`, `ref` the channel name; no `acceptance`.
- **Unavailable, never zero.** A missing or unreadable input, or a channel absent
  from it, gives `null` for the numbers it feeds and names them in the row's
  `unavailable[]`; a real zero stays `0`. `status` is `unavailable` when the channel
  has no `daily-check.json` entry. `unavailable[]` at the section level lists the
  unreadable input files, with one `errors[]` line.
- **Section.** `{schema: "fleet-clip-queue.v1", channels[], egress: {state, since},
  queue_run: {at, age_s}, unavailable[]}`.

## Queue items (`status_page.items`)

One record per open item of the low-volume queues that have no record of their
own on the page, each carrying the item envelope of decision 0019 (it extends
0016's): who owns the next move, the class, and the one store that owns the item,
so the dashboard can link through. Read-only; neither store is written.

| Source | Read from | `owner` / `class` | `store` | `ref` |
| --- | --- | --- | --- | --- |
| `finance` | `FLEET_FINANCE_CMD`, a command printing a JSON list of `{kind, id, title, acceptance, url, since}` (`kind` one of `alert`, `companion-event`, `thesis-revision`, `recommendation`; the last three keys optional) | `finance` / `finance` | `finance-db` | `url`, else `<kind>/<id>` |
| `needs-you` | `FLEET_NEEDS_YOU_LEDGER`, the path of Kit's `needs-you/open.json` (`kind` `decision`; a missing file is an empty ledger) | `kit` / `life` | `needs-you` | the entry `id` |

- **Shape.** `items[]` is `[{id, source, kind, title, owner, class, store, ref,
  since, acceptance?, origin?}]`: `id` is `<store>:<native id>`, `since` the item's
  own time (epoch, `null` when it has none), `acceptance` its one-line done-when and
  `origin` its door. Both are present only when the source holds them: a record
  without one publishes without the key, never fails. The needs-you ledger holds no
  acceptance line, so its records carry none; they carry the entry's `origin`.
- **Sources.** `item_sources` is `{finance, needs_you}`, each `{read, reason}`
  like `stalled_sources`: unset, the source is `read: false` and says so. An
  unreadable source adds an `errors[]` line; an item matching `CREDENTIAL_RE` is
  withheld and counted in `errors[]`.
- **Schema.** Still `fleet-status-page.v1`: `items`, `item_sources` and the clip
  rows' envelope keys are new optional keys, and a consumer that ignores them is
  unchanged. The dashboard's rendering is its own issue.

## Delivered decisions (`status_page.delivered`)

When every item of an accepted decision record is closed, the publisher marks the
decision delivered, once, and posts a 3-line outcome (no model: it is composed from
the record and its closed items). The dashboard's fleet page reads it from
`status_page.delivered`; the 08:30 `today-brief` carries it through
`relay.sh status` (`scripts/ensure-today-brief-fleet.sh`).

- **Records** are `FLEET_VAULT/system/decisions/NNNN-*.md` with `status: accepted`.
  A record's items are its frontmatter `items: [owner/repo#N, ...]`; a record without
  one takes its items from `backfill` in `fleet-decisions.json` (installed beside the
  helper as `lifekit-fleet-decisions.json`; `FLEET_DECISIONS_CONFIG` names another
  file), which is how 0015 and 0016 are posted on the first run. A record with no
  items is never delivered.
- **Closed** is `gh issue view` (`FLEET_GH`, cached in `status-decisions.json`; a
  closed issue for good, an open one for `FLEET_PR_TTL`). An item gh cannot read
  counts as open and adds an `errors[]` line.
- **Once.** The mark lives in the publisher's state, `decisions-delivered.json`
  (never the vault), with the outcome text kept verbatim. An unreadable mark file posts
  nothing (`delivered` is `null`) rather than risk a repeat.
- **Shape.** `delivered[]`, oldest first: `{id, title, date, delivered_epoch,
  brief_date, lines: [shipped, changed, still open], items: [{ref, title, url,
  closed_at}]}`. `changed` is the record's `summary`; `still open` is its optional
  `still_open:` line, else "nothing from this decision".
- **Brief.** `brief_date` is the one brief that carries the outcome: the first brief
  (`brief` `at`/`tz` in the config) after delivery that no other outcome holds, so a
  brief carries at most one outcome, three lines, and the 12-line cap holds.

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

## Taken for you feed (`OUT_DIR/taken-for-you.json`)

The actions the fleet took under a standing ruling instead of asking the owner
(permission engine, `agent-behavior.md#P16`), one short line each, newest first,
for the dashboard's "Taken for you" feed. `taken_feed.py` (beside the script;
installed as `/usr/local/bin/lifekit-fleet-taken.py`; `FLEET_TAKEN` overrides)
builds it and the publisher writes it world-readable, atomically, beside
`home-summary.json`. Nothing is pushed or sent: the dashboard reads the file.

**Sources**, read-only, from `FLEET_POLICY_DIR` (default
`~/.local/state/fleet-policy` of the admin account the unit runs as):

| File | Fields read | Becomes |
| --- | --- | --- |
| `p16.jsonl` (permission engine audit log) | `ts`, `ruling`, `subject`, `outcome`; also `class`, `actor`, `home` | one item as written |
| `self-calls.jsonl` (calls the fleet answered itself) | `ts`, `question`, `home`; the ruling is the first `P<n>`, `Tier <n>` or `Q<n>` cited in `record` | `subject` = the question, `outcome` = `self-answered`, `ruling` = the one cited (else `self-answered`); `answer` and `record` are never published |

```json
{"schema": "fleet-taken-feed.v1", "generated_epoch": 1800000000,
 "sources": {"p16": {"read": true, "lines": 12, "skipped": 1},
             "self_calls": {"read": false, "lines": 0, "skipped": 0}},
 "items": [{"id": "p16-0123456789abcdef", "source": "p16", "ts": 1799999000,
            "ruling": "P8", "subject": "lifekit-dashboard#212", "outcome": "merged",
            "line": "merged lifekit-dashboard#212 (P8)",
            "class": "github.merge-green", "actor": "firstmate", "home_id": "fm-vps"}]}
```

- **`items`** merge both logs, newest `ts` first (ties by `id`), at most 200.
  `line` is `<outcome> <subject> (<ruling>)`. `ts` is epoch seconds, from a
  number or an ISO 8601 time in the log. `class`, `actor` and `home_id` are
  `null` when the log has no such field (self-calls carry no class or actor).
- **`id`** is stable: `p16-` or `self-calls-` plus the first 16 hex of the
  sha256 of the raw log line (with its occurrence number, so a line repeated
  verbatim stays two items). The dashboard keys a reverse action on it; it
  survives reruns and the tail moving, and changes only if the line does.
- **Text**: every string is one line (control characters become spaces);
  `subject` is cut at 160 characters with `…`, the other fields at 64. No free
  text beyond the subject line is published. An item matching a credential
  pattern is withheld and counted as skipped, and the whole file is checked
  again before it is written.
- **Skipped lines**: a line that is not a JSON object, is over 64 KiB, or lacks
  a parseable `ts` or a non-empty `subject`/`outcome`/`ruling` (self-calls:
  `question`) is skipped and counted in `sources.<name>.skipped`; `lines` is
  the non-blank lines read. Only the last 4 MiB of a log is read (a cut first
  line is dropped, not counted).
- **Missing sources**: a log that does not exist or is not a regular file shows
  `read: false`. When neither exists the file is not written and the previous
  one stays; an existing but empty log is `read: true` with no items.
- **Failure**: a helper that fails, overruns 20 s, or prints non-JSON is logged
  and fails the run; the summary is already published. A missing helper script
  is only logged, as for the status page. The dashboard backend's
  `backend/infra.json` needs an entry for the file before it can serve it.

## Screen changes feed (`OUT_DIR/screen-changes.json`)

The open pull requests, and those merged in the last 7 days, that add, change or
remove a screen-baseline image (`*-linux.png`), with the images, for the dashboard's "Screen changes" section.
The shape is the dashboard's contract (`docs/SCREEN-CHANGES.md` in
lifekit-dashboard): schema `fleet-screen-changes.v1`, `generated_epoch`, and
`items[]` of `id`, `home_id`, `repo`, `pr`, `title`, `url`, `state`
(`open|merged`), `ts`, `merged_epoch` (merged items only), `screens[]`
(`name`, `status` `changed|added|removed`, `before`, `after`). `screen_changes.py`
(beside the script; installed as `/usr/local/bin/lifekit-fleet-screens.py`;
`FLEET_SCREENS` overrides) writes the file and the images itself, world-readable
and atomically, the images first, beside `home-summary.json`. Nothing is pushed.

- **Source**: `gh` (`FLEET_GH`, read-only: `pr list` open and merged, and GET `compare` and
  `contents`) for the repos in `FLEET_SCREEN_REPOS` (space-separated
  `owner/name`; default `lifekit-hq/lifekit-dashboard`; empty turns the feed
  off). A repo outside that list is never read. Only paths matching
  `<name>-<project>-linux.png` are fetched, before at the PR's merge base and
  after at its head; an image must be a PNG of at most 4 MiB, else the screen is
  skipped (so is one missing the image its `status` needs).
- **Items**: one per PR with at least one such screen, at most 20, newest `ts`
  first (the head commit's time, else the PR's last activity). An open PR is
  `state: "open"`. A merged PR stays as `state: "merged"` with `merged_epoch`
  for 7 days after the merge (fleet PRs merge within minutes, so the owner
  would otherwise never see the change), its before and after images kept; past
  the window, or when closed unmerged, it drops out and its images are deleted.
  Merged PRs come from the last 50 merged per repo, reusing what was cached
  while the PR was open (a PR first seen already merged is compared at its
  listed base, which may yield no screens). At most 40
  screens per item. `id` is `<repo name>-<pr number>`; `home_id` is
  `FLEET_SCREEN_HOME`, else the first home's id. `title` is one line, cut at 160.
  A veto is not read back: the item stays until its window ends (the dashboard
  remembers the veto per device and hides the item on its side; the revert it
  asks for is an ordinary PR).
- **Images** are copied to `OUT_DIR/screen-changes/<id>/before-<name>.png` and
  `after-<name>.png`, named exactly as the feed lists them. The dashboard serves
  only what the feed lists.
- **Cache**: what was fetched per PR head commit is kept in
  `FLEET_STATE_DIR/screen-changes-cache.json`, so a run with no new push costs
  one `gh pr list` per repo. A run has a 40 s gh budget; a PR not reached is
  picked up on the next run.
- **Failure**: gh failing on the PR list, or a credential-looking item (withheld),
  never publishes a half feed: a failed run leaves the previous file and images
  and fails the unit. A missing helper script is only logged.

## Activity journal and retro board (`OUT_DIR/journal/`)

`activity_journal.py` (installed as `lifekit-fleet-journal.py`, `FLEET_JOURNAL` overrides) runs at the end of a publisher run, every `FLEET_JOURNAL_EVERY` seconds (see "Run cadence"). The store is `OUT_DIR/journal/activity.jsonl` (schema `fleet-activity-journal.v1`), on the box's disk under `/var/lib/lifekit-fleet`, never in the vault, so it outlives a publisher restart. The dashboard reads it from there.

- **One row per finished UTC day**, written once the day has been over for `FLEET_JOURNAL_DELAY` seconds (default 7200, after the day's fleet metrics exist). A date that already has a row is never written again, so a rerun adds nothing. After downtime the missing days since the last row are appended (at most 14); the very first run writes yesterday only.
- **Fields**, all counts or identifiers (no free text): `merged_prs_by_repo` (`{owner/name: n}` from the ledgers' `task.merged` PR URLs), `decisions_owner` (ledger `resolved` lines that carry a decision key), `decisions_taken_for_owner` (`{total, by_ruling}` from `taken-for-you.json`, which keeps the last 200 actions), `failed_tasks` (distinct ledger tasks with a `failed` status), `red_ci_runs` (failed `main` workflow runs through gh over `FLEET_JOURNAL_REPOS`, default the repos the ledger merged into in the prior 14 days), `stalled_task_hours` and `fleet_weighted_tokens` (the day's final non-partial fleet metrics row), `devclaw_sessions`, `openclaw_runs` and `openclaw_errors` (Prometheus `increase` over the day, `FLEET_PROM_URL`), and `clip_stats` (the numeric top-level fields of the JSON at `FLEET_CLIP_STATS`, a path or URL; see below).
- **A source that cannot be read** writes the string `"unavailable"` for its field (and a stderr line); the row is still appended. No key is ever missing. A field fed by several sources is `"unavailable"` unless every one was read: every home's ledger (the publisher passes all of them), and a final non-partial metrics row from every home (`--home ID` per home).
- **Clip stats feed (operator step)**: `FLEET_CLIP_STATS` is unset today, so `clip_stats` is `"unavailable"` on every row until an operator points it at the side-hustle feed: a path or `http(s)` URL serving a JSON object whose numeric top-level fields are the numbers to keep (other fields are dropped). The retro board reads the variable when it is built: while it is unset the board lists `clip_stats` once under `not_configured` ("Not configured" in the `.md`) and neither counts it as a recurring problem nor derives a proposed change from it; once it is set, a feed that fails to read counts like any other unavailable source.
- **Retro board**: once 14 journal days exist after the last board (or after the first row), `OUT_DIR/journal/retro/<from>_<to>.json` and `.md` are written (`fleet-retro-board.v1`): `went_well`, `went_wrong`, `recurring_problems` (a problem on 3 or more of the 14 days: `failed-tasks`, `red-ci`, `stalled-work`, `openclaw-errors`, `unavailable:<field>`; a day counts as clean only when failed tasks, red CI, stalled hours and OpenClaw errors were all read and zero) and exactly three `proposed_changes`, each with its `basis` problem and day count, taken from the most frequent problems (padded with a keep-practice line only when fewer than three problems occurred).
- **Failure**: a missing helper is only logged; a write failure or a credential-looking output fails the run.
