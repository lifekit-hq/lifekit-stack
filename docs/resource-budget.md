# Resource budget - memory per project

The box has 15.6 GiB of RAM and 8 GiB of swap (the live `/swapfile`; `scripts/bootstrap-vps.sh` creates 4 GiB on a fresh host, and the file was grown since). Every project and host group has a
memory budget: the expected sustained footprint, so "the box is slow" has an owner to
look at. The budgets are **documentation, not alert thresholds** (see Alerts): no alert
keys on them, and shorter bursts can exceed them. A budget is set against the measured
7-day maximum 30-minute average where that range exists; where it does not, the row's
own last column says what the number rests on instead - a sized estimate, a local
test, a single snapshot, a sum of per-container peaks - and more than one row does.
Read that column before treating a budget as measured. They sum to **17.10 GiB**; a **1.75 GiB burst pool** for
short-lived work brings the total to **18.85 GiB** against the 15.6 GiB of RAM. That
overcommit is intended: the groups do not peak together, and swap is the margin.

The weekly ops report on 2026-10-02 shows 15-minute-plus breaches for openclaw (124%),
platform (244%) and the OS (103%). Platform is outside this recalibration, and the
openclaw and OS budgets are left where they are.

## What a budget is

A budget is a **sustained** figure: the **30-minute average of resident memory,
excluding reclaimable page cache**, so a short spike stays inside it (the OpenClaw
gateway's 3.6 GiB peak is inside its 3.5 GiB budget as an average). The weekly ops
report (`scripts/ops-report/weekly.py`) still compares each group's 30-minute average
with its budget, so a group that drifts over shows up there. A minute in which a group has no container counts as 0, so a short-lived
group is averaged over the full 30 minutes, not just the minutes it was alive.

- Container groups read `docker_container_memory_usage_bytes` from
  `compose/container-exporter`, which is usage minus `inactive_file` - the number
  `docker stats` shows, without reclaimable cache.
- Every container series carries an `image` label (the container's configured image),
  and `docker_container_memory_swap_bytes` gives each running container's swapped-out
  bytes, so an uncapped transient container in the burst pool traces to an owner.
- **Swap is margin, not budget.** Nothing is allotted swap. It is what absorbs a
  breach, so a full swap means the margin is gone; it has its own alert (below).
- A budget is not a cap. Caps are a separate, per-service decision (compose
  `mem_limit`, `docker run --memory`); the groups that run interactive work -
  operator sessions, runners, the OS - are **never capped**, because a
  cap would kill work in progress.

## The budgets

Where the last column gives a 7-day peak it is the Prometheus maximum of the group's
containers to 2026-09-29; the rows resting on something else say so in that column.
The host groups (operator sessions, runners, OS) are sized from the `host_group_memory_bytes`
history to 2026-10-01 plus a live cgroup reading (measurements below the table).

| Group | Budget (GiB) | Contains | Basis |
| --- | --- | --- | --- |
| openclaw | 3.5 | `openclaw-gateway`, `google-workspace-mcp`, `google-workspace-mcp-fleet` (compose project `openclaw`) | 3723 + 153 MiB; the fleet connector is the same idle image as `google-workspace-mcp` (about 150 MiB), added 2026-11-08 inside the same budget, to be measured with the next window |
| platform | 1.5 | prometheus, loki, grafana, tempo, otel-collector, node-exporter, container-exporter, notify-relay (compose project `compose`) | 1.3 GiB (sum of peaks) |
| finance-sentry | 1.25 | api, postgres, mcp, gateway, frontend (`finance-sentry-*`, compose project `docker`) | 1.05 GiB (average 0.81) |
| devclaw-mcp | 0.25 | `devclaw-mcp` (compose project `devclaw`; its sandboxes count in the burst pool) | 130 MiB |
| dashboard | 0.25 | the dashboard service (compose project `dashboard`) | 114 MiB |
| xui | 0.25 | web, db (compose project `xui`) | 188 MiB |
| backstage | 0.15 | the Backstage service (compose project `backstage`, on `lifekit-shared`) | new 2026-10-12; sustained estimate, not yet measured - replace with the measurement (owner: maintainer, #484); its series already holds the seven days, so this waits on box access, not on a window |
| basket | 0.25 | the app and its Postgres (compose project `basket`; containers `basket-app`, `basket-db`) | new 2026-11-06; sized estimate, not yet measured (owner: maintainer, #484; see below) |
| identity | 0.5 | logto, postgres (compose project `identity`); capped at 512 + 256 MiB. Also the tailnet sign-in gate, traefik and oauth2-proxy (compose project `edge`), capped at 128 + 64 MiB | new 2026-10-02: logto 218-266 MiB, postgres 38-71 MiB just after first boot in local tests; traefik about 21 MiB, oauth2-proxy about 5 MiB in local tests |
| operator sessions | 6.8 | the operator's login slice (`user-1001.slice`): agent sessions, their browser helpers, tools | 4.3 GiB resident at the 5-worker cap (2026-10-01); 30-minute average 3.5 p50, 5.1 p95, 6.8 max over 7 days |
| runners | 1.4 | the CI runner services and their jobs | 0.2-0.45 GiB idle; 1.6 GiB raw and 1.0 GiB 30-minute average during the 2026-10-01 image-rebuild deploy; 1.37 GiB 30-minute average max over 7 days |
| OS | 1.0 | dockerd, containerd, shims, tailscaled, journald (the `os` gauge counts only these four units; cron and the other system services, about 80 MiB in the same snapshot, are not in any gauge and sit in this row's headroom) | about 0.75 GiB (snapshot) |
| **Sum** | **17.10** | | |
| burst pool | 1.75 | everything else that runs as a container: devclaw sandboxes, `openclaw-cli` runs, rehearsals, CI and worker validation containers, test compose projects, any orphan | 30-minute average 0.14 GiB p50, 0.78 p95, 1.66 max over 7 days; one worker container reached 3164 MiB (a spike, not sustained) |
| **Total** | **18.85** | of 15.6 GiB RAM | |

**Operator sessions, runners and burst pool, recalibrated 2026-10-01** (was 3.5, 0.75 and 3.0 GiB; the
per-group alerts that read them fired at 113-116% on the first day, with 4-5 worker
sessions and a full OpenClaw image rebuild running, no runaway process found, and were
then retired - see Alerts):

- Operator sessions: a worker `claude` session is 0.33-0.47 GiB resident (mean 0.39), the
  `claude -p` helper a no-mistakes run spawns about 0.27 GiB, each `npm exec` MCP helper
  0.10-0.15 GiB. At the standing cap of 5 parallel workers plus the supervising sessions
  that is about 6 x 0.4 + 3 x 0.27 + a handful of MCP helpers = 4.3 GiB measured
  (`user-1001.slice` minus `inactive_file`, 0.9 GiB more swapped), so the cap alone fits
  in 5.0 GiB. The budget is 6.8 GiB, the measured 7-day maximum 30-minute average (5.1
  GiB at p95), so a weekly-report breach means use above anything seen that week.
- Runners: idle they are 0.2-0.45 GiB; a full image-rebuild deploy took the 30-minute
  average to 1.0 GiB (1.6 GiB raw, 18:19-18:44 UTC on 2026-10-01; the worst 7-day average
  was 1.37 GiB). 1.4 GiB covers that worst case.
- The burst pool shrinks from 3.0 to 1.75 GiB, just above its 7-day 30-minute average
  maximum of 1.66 GiB (under 0.8 GiB 95% of the time). The budgets totalled 17.95 GiB at
  that date against 15.6 GiB of RAM, on purpose: the groups do not peak together and swap
  is the margin. No other group's budget changed. The platform group's budget (1.5 GiB) is below its
  measured 3 GiB 7-day median, which is outside this recalibration.

**basket, added 2026-11-06.** compose project `basket`. Selected on the project label alone, so
the member names do not matter to any query here; where they appear they are the container
names [`runbook.md`](./runbook.md) "Off-box backup (R2)" drives (`docker stop basket-app`,
`docker exec basket-db`), not the compose service labels the rows above use - the db service's
label is `db` (`scripts/offbox-backup/offbox_backup.py`), and nothing in this repo names the
app's. The 0.25 GiB is a **sized estimate, not a measurement** - xui is the same
app-plus-Postgres shape at a 188 MiB peak, identity's Postgres sits at 38-71 MiB - because no
reading of the box was reachable when the row was written. Replace it (owner: maintainer,
tracked in #484) with the real 7-day peak once
`docker_container_memory_usage_bytes{project="basket"}` holds seven full days - check that
range, do not take the report's first basket row for the measurement: a container group's query
ends `or vector(0)`, so a row prints from the first scrape onwards however little history
stands behind it. The container exporter reads every container on the box, so that history may
already be there. The same issue carries backstage's older estimate, which needs no waiting
window at all: its series has been recorded since 2026-10-12, so the seven days are already
behind it and the measurement waits on box access alone.

**Basket is scraped but has no alert rule (2026-11-12).** Its `basket-app` serves `/metrics` on
`:3310` over `lifekit-shared`, scraped as job `basket` in
`compose/observability/prometheus/prometheus.yml` (platform contract `scraped` and `labels`);
its only lines in `prometheus/rules.yml` are the `lifekit:group_memory_bytes`
branch and burst-pool exclusion the budget row above requires, and those read the container
exporter's series, not basket's. The deploy-time census does see its containers, and reports
them as undeclared: nothing in this repo settles which classification that is - a lifekit
product whose own repo adds the labels (the devclaw-mcp case, since there are no waivers for
products) or a project for `OUT_OF_CONTRACT`. Each branch lands somewhere different: a product
goes in [`platform-contract.md`](./platform-contract.md) "Known gaps", which still names only
devclaw-mcp, while a non-product goes in the `OUT_OF_CONTRACT` dict in
`scripts/platform-contract.py` (that doc's "Classifying a non-product container"). Making the
call belongs to whoever owns basket (owner: maintainer), not to this doc, and it has no tracked
issue yet: `scripts/deploy.sh` enforces the contract at runtime for the `compose`, `openclaw`,
`identity` and `edge` projects only, so basket's containers print as undeclared on every deploy
without ever failing one - reading the census is not a trigger that fires. The `box` rule group
still watches its containers through the container exporter: an abnormal exit, a restart loop,
a failing healthcheck or an OOM kill fires for them as for any container on the box. One hole
stays open: *long-running container is stopped or gone* is gated to
`project=~"compose|openclaw|docker|devclaw|dashboard|xui"`, so a cleanly stopped or deleted
`basket-db` is silent. That allowlist predates backstage, identity, edge and reminder-bot as
well; widening it to every production project is its own change, because it also decides how
much retirement noise the rule may make. Owner: maintainer; no tracked issue yet - #479 closes
with the PR that wrote this line, so it cannot carry the follow-up, and the trigger cannot be
waiting to notice a silent stop, since not noticing is the hole.

Membership is by compose project and service label, so a new container is in the burst
pool until it is added to a group; platform is every service of compose project
`compose`; `openclaw-cli` runs (compose project `openclaw`, outside the openclaw group)
count in the burst pool. The one non-label exception is
finance-sentry, whose compose project name (`docker`) is too generic to match on.

## Alerts

Grafana rules in `compose/observability/grafana/provisioning/alerting/rules.yml`, group
`memory-budget`, delivered through the existing contact point in the one message format
([`message-format.md`](./message-format.md) "The Grafana exemption") and picked up by
alert-inbox ([`runbook.md`](./runbook.md) "Pull-based alert-to-inbox polling").

Per-group budget alerts were retired on 2026-10-01 (`project-memory-over-budget`,
`host-group-over-budget`, `burst-pool-over-budget`): they fired three times in one
evening on legitimate work (parallel agent sessions, an image rebuild) and said "a group
is over a number" rather than "the box is struggling". One host-level rule replaced them.

| Rule | Fires when | Severity |
| --- | --- | --- |
| host memory is under pressure | available RAM is under 2 GiB, or swap-in is over 1000 pages/s (15-minute rate), for 15 minutes; the message lists the top three groups by resident memory | warning |
| a container is near its memory cap | a container with a real cap uses over 90% of it, for 10 minutes; containers with no cap are skipped (their reported limit is the whole box) | warning |
| host swap is over 75% used | swap used above 75% of swap total, for 15 minutes, with or without RAM pressure | warning |

**Why 2 GiB.** Over the 7 days to 2026-10-01 available RAM averaged 7.1 GiB (5th
percentile 5.0 GiB, minimum 0.89 GiB). It was under 1.5 GiB for 35 minutes of that week and
under 2 GiB for 155 minutes. The critical *host RAM available is critically low* rule
already fires under 10% (about 1.56 GiB) after 5 minutes, so a 1.5 GiB threshold would add
nothing; 2 GiB is its earlier sibling that leaves time to act. The swap-in half
catches the slowdown without a RAM shortage showing first (the box swapped in about 68
pages/s at the time of writing, no history yet; 1000 pages/s is about 4 MiB/s held for
15 minutes). The rate window is 15 minutes because the counter is rewritten only every
5 minutes: a shorter window can contain no new value and read as zero, and a 10-minute
window holds only one write if a write drifts by a single scrape. Tune both after a
week of `host_vmstat_pswpin_pages_total`.

**The top three in the message.** The Prometheus recording rule
`lifekit:group_memory_bytes` (`compose/observability/prometheus/rules.yml`) holds one
series per group below - the container groups, the burst pool and the host groups - and
the alert ranks it, so the diagnosis is in the alert itself. A group missing from it is
a group with no running member (the burst pool is absent while empty).

The existing *host swap and RAM are both under pressure* and *a container was OOM-killed*
rules are unchanged.

**Host groups.** Operator sessions, runners and the OS are not containers, so
`scripts/host-gauge/host-group-gauge.sh` (a systemd timer, every 5 minutes, installed by
`bootstrap-vps.sh`) reads their cgroup v2 memory and writes
`host_group_memory_bytes{group}` and `host_group_memory_swap_bytes{group}` for the
textfile collector. RAM is `memory.current` minus `inactive_file`, the same
"resident, no reclaimable cache" definition as the container groups. The same script
writes `host_vmstat_pswpin_pages_total` (from `/proc/vmstat`; node-exporter's vmstat
collector is off), the swap-in signal above. The groups are:

| `group` | cgroups read |
| --- | --- |
| `operator` | `user.slice/user-1001.slice` |
| `runners` | `system.slice/actions.runner.*.service` |
| `os` | `system.slice/{docker,containerd,tailscaled,systemd-journald}.service` (never all of `system.slice`: docker's container scopes live there; cron, sshd and the other system services are therefore not counted) |

The swap gauge is margin, not budget: it has no rule of its own, the host-wide
*host swap is over 75% used* rule covers it.

## Changing a budget

A budget is a table row above, and the weekly report reads it from there. A budget moves
because measured use moved: check the 7-day peak first. A merge to `main` reloads the
Prometheus and Grafana rules on deploy; nothing is recreated.

A **new group**, or a change to an existing group's members, is four edits that have to
agree:

1. the table row above (and the `Sum` / `Total` rows and the opening paragraph's three
   figures: the sum, the burst pool and the total);
2. `GROUP_SELECTORS` in `scripts/ops-report/weekly.py`;
3. the `lifekit:group_memory_bytes` recording rule
   (`compose/observability/prometheus/rules.yml`) - **twice**: the group's own
   `label_replace` branch and the burst pool's `unless (...)` list, or its containers are
   counted in both;
4. the compose-project list in [`runbook.md`](./runbook.md) "Production test for
   containers", which decides what an alert remedy may do to those containers.

`scripts/ops-report/tests/test_weekly.py` and `scripts/tests/test_alert_rules.py` fail when
1-3 disagree, the `Sum` / `Total` rows and the opening paragraph's figures included - the
report never reads those three, so `test_the_doc_totals_match_the_rows_they_sum` is what
holds them to the rows they sum. Nothing checks 4: it is prose.
