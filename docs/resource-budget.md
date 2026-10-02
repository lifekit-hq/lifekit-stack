# Resource budget - memory per project

The box has 15.6 GiB of RAM and 8 GiB of swap (the live `/swapfile`; `scripts/bootstrap-vps.sh` creates 4 GiB on a fresh host, and the file was grown since). Every project and host group has a
memory budget: the expected sustained footprint, so "the box is slow" has an owner to
look at. The budgets are **documentation, not alert thresholds** (see Alerts): no alert
keys on them, they are set against the measured 7-day maximum 30-minute average, and
shorter bursts can exceed them. They sum to **16.7 GiB**; a **1.75 GiB burst pool** for
short-lived work brings the total to **18.45 GiB** against the 15.6 GiB of RAM. That
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

The 7-day peaks are the Prometheus maximum of the group's containers to 2026-09-29.
The host groups (operator sessions, runners, OS) are sized from the `host_group_memory_bytes`
history to 2026-10-01 plus a live cgroup reading (measurements below the table).

| Group | Budget (GiB) | Contains | 7-day peak |
| --- | --- | --- | --- |
| openclaw | 3.5 | `openclaw-gateway`, `google-workspace-mcp` (compose project `openclaw`) | 3723 + 153 MiB |
| platform | 1.5 | prometheus, loki, grafana, tempo, otel-collector, node-exporter, container-exporter, notify-relay (compose project `compose`) | 1.3 GiB (sum of peaks) |
| finance-sentry | 1.25 | api, postgres, mcp, gateway, frontend (`finance-sentry-*`, compose project `docker`) | 1.05 GiB (average 0.81) |
| devclaw-mcp | 0.25 | `devclaw-mcp` (compose project `devclaw`; its sandboxes count in the burst pool) | 130 MiB |
| dashboard | 0.25 | the dashboard service (compose project `dashboard`) | 114 MiB |
| xui | 0.25 | web, db (compose project `xui`) | 188 MiB |
| identity | 0.5 | logto, postgres (compose project `identity`); capped at 512 + 256 MiB. Also the tailnet sign-in gate, traefik and oauth2-proxy (compose project `edge`), capped at 128 + 64 MiB | new 2026-10-02: logto 218-266 MiB, postgres 38-71 MiB just after first boot in local tests; traefik about 21 MiB, oauth2-proxy about 5 MiB in local tests |
| operator sessions | 6.8 | the operator's login slice (`user-1001.slice`): agent sessions, their browser helpers, tools | 4.3 GiB resident at the 5-worker cap (2026-10-01); 30-minute average 3.5 p50, 5.1 p95, 6.8 max over 7 days |
| runners | 1.4 | the CI runner services and their jobs | 0.2-0.45 GiB idle; 1.6 GiB raw and 1.0 GiB 30-minute average during the 2026-10-01 image-rebuild deploy; 1.37 GiB 30-minute average max over 7 days |
| OS | 1.0 | dockerd, containerd, shims, tailscaled, journald (the `os` gauge counts only these four units; cron and the other system services, about 80 MiB in the same snapshot, are not in any gauge and sit in this row's headroom) | about 0.75 GiB (snapshot) |
| **Sum** | **16.7** | | |
| burst pool | 1.75 | everything else that runs as a container: devclaw sandboxes, `openclaw-cli` runs, rehearsals, CI and worker validation containers, test compose projects, any orphan | 30-minute average 0.14 GiB p50, 0.78 p95, 1.66 max over 7 days; one worker container reached 3164 MiB (a spike, not sustained) |
| **Total** | **18.45** | of 15.6 GiB RAM | |

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
  maximum of 1.66 GiB (under 0.8 GiB 95% of the time). The budgets now total 17.95 GiB
  against 15.6 GiB of RAM, on purpose: the groups do not peak together and swap is the
  margin. No other group's budget changed. The platform group's budget (1.5 GiB) is below its
  measured 3 GiB 7-day median, which is outside this recalibration.

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

A budget is a table row above, and the weekly report reads it from there. When a group's
members change, update the member selectors in the `lifekit:group_memory_bytes` recording
rule (`compose/observability/prometheus/rules.yml`) and in `GROUP_SELECTORS` in
`scripts/ops-report/weekly.py` together; `scripts/tests/test_alert_rules.py` fails when
they differ. A budget moves because measured use moved: check
the 7-day peak first. A merge to `main` reloads the Prometheus and Grafana rules on
deploy; nothing is recreated.
