# ops-report

Read-only, stdlib-only reports on the VPS. `weekly.py` prints the weekly Markdown report.

## Per-job weekly cost (`jobs.py`)

`consumers.py` (printed by `weekly.py`) says who spends; `jobs.py` says which recurring job
inside each consumer, so a token-saving change can be checked week over week.

```bash
python3 -I scripts/ops-report/jobs.py --days 7 --min-usd 2
```

Flags: `--days`, `--min-usd` (rows below it fold into `<consumer>,other(<min)`), `--no-openclaw`
(skip the gateway-container scan), `--claude-projects`, `--clip-data`, `--nm-db`. Figures are
list-price weighting, not spend. Output is one `jobs[N]{consumer,job,runs,usd,per_run}` table,
a `cold_cache` overlay (cache writes after more than an hour idle) and a total; it carries job
names and dollar figures only, never message text, tokens or chat ids. It is standalone: not
part of `weekly.py` and not scheduled.
