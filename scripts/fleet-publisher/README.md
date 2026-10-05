# Fleet publisher output contract

`fleet-publisher.sh` reads the fleet home's `state/home-summary.json`
(schema `fm-secondmate-home-summary.v1`, read as-is) and writes
`OUT_DIR/home-summary.json` plus `OUT_DIR/reports/`. The dashboard binds to this
output, not to the source summary. Operations (install, alerts, metrics) are in
`docs/runbook.md` "Fleet publisher"; behaviour is pinned by
`scripts/tests/test_fleet_publisher.py`.

The output carries `publisher_schema: "lifekit-fleet-publisher.v1"`. Every source
field passes through unchanged; the fields below are added. The fleet home is
only ever read (files, `fm-captain-hold.sh binding`, `tasks-axi show`).

## Fields

| Where | Field | Meaning |
| --- | --- | --- |
| top | `publisher_schema`, `published_epoch` | Output version; publish time |
| top | `decisions_total` | Open captain holds in the source, asks or not |
| top | `holds_total` | Full hold count (`counts.holds`), alongside the capped `holds` list |
| `decisions_open[]` | `ask`, `board_url`, `restart`, `title_plain`, `project` | **Only real asks stay here** (below) |
| `decisions_parked[]` | the same row fields, no `ask` | Captain holds that are not a question waiting for an answer |
| `active_children[]` | `title_plain`, `since_epoch`, `produces`, `open_url`, `project` | See below |
| `landed[]` | `title_plain`, `since_epoch`, `produces`, `open_url`, `project`, `report_url` | See below |
| `holds[]` | `restart`, `title_plain`, `project` | |

- **`ask`** is the decision-contract v1 shape (`docs/decision-contract.md`):
  `{question, options[{id, label, recommended}], free_text_allowed, link}`,
  recommended option first. It is present only when a structured record exists at
  `STATE_DIR/decisions/<decision id>.json` (the notify-relay producer shape:
  `ask`, `options`, `free_text`/`free_text_allowed`) **and** the hold is live and
  undated. No record means no `ask`; the publisher never parses a hold's prose into
  options. `link` is the board when one is bound, else `/decisions/<decision id>`.
- **`board_url`**: an open Lavish session fed by the captain-hold binding
  (`fm-captain-hold.sh binding lavish-<session id>` succeeds) whose file lives under
  the hold's origin task directory (`Captain hold origin:` in `tasks-axi show`), or
  whose binding names the decision. Falls back to the newest open session under
  `DATA_DIR/<decision id>/`, else `null`.
- **`restart`**: `{kind, until?, blocker_ids?, text}`. `kind` is `after_work` when
  blockers are unresolved (`blocker_ids`), `date` for a future `hold_until` or a
  `restart <YYYY-MM-DD>` in the reason (`until`), `proposals`, `captain_word`
  (a structured ask record, or the reason names the captain's word/answer), else
  `event`. `text` is the source reason.
- **`title_plain`**: the title without conventional-commit or kind prefixes
  (`chore(x):`, `Scout:`), `repo#N:` ids and status words; a title the snapshot cut
  loses its partial last word and ends in `…`.
- **`since_epoch`**: children, the task `.meta` mtime (`null` when missing); landed,
  midnight UTC of `completion.date`.
- **`produces`**: `pr`, `report` or `landing`. Children: from `kind` (`ship` → `pr`,
  `scout`/`investigate` → `report`); landed: from `pr_url`, then `report_path`.
- **`open_url`**: children, the PR url last named in the task's status file, else
  the board for a report-producing task, else the issue (`repo#N` in the name, org
  known from any landed PR url); landed, `pr_url`, else `report_url`; otherwise `null`.
- **`project`**: the repo in a `repo#N` title, else the PR url's repo, else the row's
  `repo`; otherwise `null`.
- **`report_url`**: landed items whose `report_path` is a regular `.md` file under
  `DATA_DIR`, free of credential patterns and at most 2 MiB, are copied to
  `OUT_DIR/reports/<id>/report.md`; the url is `REPORT_URL_BASE/<id>/report.md`
  (default base `reports`, relative to the published summary). The dashboard serves
  `OUT_DIR/reports/`. Reports that fail a check are skipped, never linked.

`fleet.prom` still counts the source's open decisions (`fleet_decisions_open`), so
alerts do not change with the asks-only `decisions_open`.
