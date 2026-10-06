# Fleet publisher output contract

`fleet-publisher.sh` reads each fleet home's `state/home-summary.json`
(schema `fm-secondmate-home-summary.v1`, read as-is) and writes one merged
`OUT_DIR/home-summary.json` plus `OUT_DIR/reports/`. The homes are `FM_HOMES`, a
space-separated list of absolute paths (default `FM_HOME` alone); a home's id is
its directory's basename and must be unique. The dashboard binds to this output,
not to the source summaries. Operations (install, alerts, metrics) are in
`docs/runbook.md` "Fleet publisher"; behaviour is pinned by
`scripts/tests/test_fleet_publisher.py`.

Every source field passes through unchanged; the fields below are added or, with
more than one home, aggregated. The fleet homes are only ever read (files,
`fm-captain-hold.sh binding`, `tasks-axi show`).

| Where | Field | Meaning |
| --- | --- | --- |
| top | `published_epoch` | Publish time |
| top | `homes[]` | One entry per home, in `FM_HOMES` order: `id`, `path`, `published`, `valid`, `reason`, and for a published home its `schema`, `generated_epoch`, `state`, `counts` |
| every item of every top-level list | `home_id` | The id of the home the item came from (`active_children`, `decisions_open`, `holds`, `queued`, `landed`, `endpoints`, `omitted`) |
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
