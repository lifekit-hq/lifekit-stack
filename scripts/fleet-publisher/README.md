# Fleet publisher output contract

`fleet-publisher.sh` reads the fleet home's `state/home-summary.json`
(schema `fm-secondmate-home-summary.v1`, read as-is) and writes
`OUT_DIR/home-summary.json` plus `OUT_DIR/reports/`. The dashboard binds to this
output, not to the source summary. Operations (install, alerts, metrics) are in
`docs/runbook.md` "Fleet publisher"; behaviour is pinned by
`scripts/tests/test_fleet_publisher.py`.

Every source field passes through unchanged; the fields below are added. The
fleet home is only ever read (files, `fm-captain-hold.sh binding`,
`tasks-axi show`).

| Where | Field | Meaning |
| --- | --- | --- |
| top | `published_epoch` | Publish time |
| `decisions_open[]` | `board_url` | The open Lavish session for the decision, else `null` |
| `landed[]` | `report_url` | The served copy of the item's finished `report.md`, else `null` |

- **`board_url`**: an open Lavish session fed by the captain-hold binding
  (`fm-captain-hold.sh binding lavish-<session id>` succeeds) whose file lives under
  the hold's origin task directory (`Captain hold origin:` in `tasks-axi show`), or
  whose binding names the decision. Falls back to the newest open session under
  `DATA_DIR/<decision id>/`, else `null`. `tasks-axi` finds the backlog from its
  working directory, so the publisher runs it from `FM_HOME`, at the absolute path
  in `TASKS_AXI` (the installer resolves it for the admin account and the unit
  carries it; the publisher puts its directory, and so node, on `PATH`). A failed
  lookup is logged to stderr and the decision falls back to the directory lookup.
- **`report_url`**: landed items whose `report_path` is a regular `.md` file under
  `DATA_DIR`, free of credential patterns and at most 2 MiB, are copied to
  `OUT_DIR/reports/<id>/report.md`; the url is `REPORT_URL_BASE/<id>/report.md`
  (default base `reports`). The url is relative to the published summary and the
  dashboard resolves it; the route serving `OUT_DIR/reports/` is the dashboard's.
  Reports that fail a check are skipped, never linked. Files are
  replaced one rename at a time, so a report the previous summary linked is never
  briefly absent.
