# Resource budget - memory per project

The box has 15.6 GiB of RAM and 8 GiB of swap (the live `/swapfile`; `scripts/bootstrap-vps.sh` creates 4 GiB on a fresh host, and the file was grown since). Every project and host group gets a
memory budget so a breach names an owner instead of "the box is slow". The budgets sum
to **14.25 GiB**; a **1.75 GiB burst pool** for short-lived work brings the total to
**16.0 GiB** against the 15.6 GiB of RAM. That overcommit is intended: the groups do not
peak together, and swap is the margin.

## What a budget is

A budget is a **sustained** ceiling: the **30-minute average of resident memory,
excluding reclaimable page cache**. An alert fires after that average has been over
budget for **15 minutes**, so a short spike stays quiet (the OpenClaw gateway's
3.6 GiB peak is inside its 3.5 GiB budget as an average) while a leak or a step up in
use is heard. A minute in which a group has no container counts as 0, so a short-lived
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
  operator sessions, runners, the OS - are **alert only, never capped**, because a
  cap would kill work in progress.

## The budgets

The 7-day peaks are the Prometheus maximum of the group's containers to 2026-09-29.
The host groups (operator sessions, runners, OS) are sized from the `host_group_memory_bytes`
history to 2026-10-01 plus a live cgroup reading (measurements below the table).

| Group | Budget (GiB) | Contains | 7-day peak |
| --- | --- | --- | --- |
| openclaw | 3.5 | `openclaw-gateway`, `google-workspace-mcp` (compose project `openclaw`) | 3723 + 153 MiB |
| platform | 1.5 | prometheus, loki, grafana, tempo, otel-collector, node-exporter, container-exporter, notify-relay (compose project `compose`) | 1.3 GiB (sum of peaks) |
| finance-sentry | 1.25 | api, postgres, mcp, gateway, frontend, uptime-probe (`finance-sentry-*`, compose project `docker`) | 1.05 GiB (average 0.81) |
| devclaw-mcp | 0.25 | `devclaw-mcp` (compose project `devclaw`; its sandboxes count in the burst pool) | 130 MiB |
| dashboard | 0.25 | the dashboard service (compose project `dashboard`) | 114 MiB |
| xui | 0.25 | web, db (compose project `xui`) | 188 MiB |
| operator sessions | 5.0 | the operator's login slice (`user-1001.slice`): agent sessions, their browser helpers, tools | 4.3 GiB resident at the 5-worker cap (2026-10-01); 30-minute average 3.5 p50, 5.1 p95, 6.8 max over 7 days |
| runners | 1.25 | the CI runner services and their jobs | 0.2-0.45 GiB idle; 1.6 GiB raw and 1.0 GiB 30-minute average during the 2026-10-01 image-rebuild deploy |
| OS | 1.0 | dockerd, containerd, shims, tailscaled, journald | about 0.75 GiB (snapshot) |
| **Sum** | **14.25** | | |
| burst pool | 1.75 | everything else that runs as a container: devclaw sandboxes, `openclaw-cli` runs, rehearsals, CI and worker validation containers, test compose projects, any orphan | 30-minute average 0.14 GiB p50, 0.78 p95, 1.66 max over 7 days; one worker container reached 3164 MiB (a spike, not sustained) |
| **Total** | **16.0** | of 15.6 GiB RAM | |

**Operator sessions and runners, recalibrated 2026-10-01** (was 3.5 and 0.75 GiB; both
alerted at 113-116% on the first day, with 4-5 worker sessions and a full OpenClaw image
rebuild running, no runaway process found):

- Operator sessions: a worker `claude` session is 0.33-0.47 GiB resident (mean 0.39), the
  `claude -p` helper a no-mistakes run spawns about 0.27 GiB, each `npm exec` MCP helper
  0.10-0.15 GiB. At the standing cap of 5 parallel workers plus the supervising sessions
  that is about 6 x 0.4 + 3 x 0.27 + a handful of MCP helpers = 4.3 GiB measured
  (`user-1001.slice` minus `inactive_file`, 0.9 GiB more swapped); 5.0 GiB leaves 0.7 GiB
  for a browser or test helper without alerting. The 30-minute average exceeded 5 GiB
  about 5% of the time over 7 days, so a sustained alert still means above-cap use.
- Runners: idle they are 0.2-0.45 GiB; a full image-rebuild deploy took the 30-minute
  average to 1.0 GiB (1.6 GiB raw, 18:19-18:44 UTC on 2026-10-01; the worst 7-day average
  was 1.37 GiB). 1.25 GiB covers the typical rebuild; the rare worst case alerts.
- The burst pool shrinks from 3.0 to 1.75 GiB, just above its 7-day 30-minute average
  maximum of 1.66 GiB (under 0.8 GiB 95% of the time). The budgets now total 16.0 GiB
  against 15.6 GiB of RAM, on purpose: the groups do not peak together and swap is the
  margin. No other group's budget changed. The platform group's budget (1.5 GiB) is below its
  measured 3 GiB 7-day median, which is outside this recalibration.

Membership is by compose project and service label, so a new container is in the burst
pool until it is added to a group; platform is every service of compose project
`compose` that is not the retired `lifekit-dashboard`; `openclaw-cli` runs (compose project
`openclaw`, outside the openclaw group) count in the burst pool. The one non-label exception is
finance-sentry, whose compose project name (`docker`) is too generic to match on.

## Alerts

Grafana rules in `compose/observability/grafana/provisioning/alerting/rules.yml`, group
`memory-budget`, delivered through the existing contact point in the one message format
([`message-format.md`](./message-format.md) "The Grafana exemption") and picked up by
alert-inbox ([`runbook.md`](./runbook.md) "Pull-based alert-to-inbox polling").

| Rule | Fires when | Severity |
| --- | --- | --- |
| a project is over its memory budget | a container group's 30-minute average is over its budget above, for 15 minutes; one alert per group, named in the subject | warning |
| the burst pool is over its memory budget | the burst pool's 30-minute average is over 1.75 GiB, for 15 minutes | warning |
| a container is near its memory cap | a container with a real cap uses over 90% of it, for 10 minutes; containers with no cap are skipped (their reported limit is the whole box) | warning |
| host swap is over 75% used | swap used above 75% of swap total, for 15 minutes, with or without RAM pressure | warning |

The existing *host RAM available is critically low*, *host swap and RAM are both under
pressure* and *a container was OOM-killed* rules are unchanged; they cover real
pressure and kills, these cover the slow drift before it.

**Host groups.** Operator sessions, runners and the OS are not containers, so
`scripts/host-gauge/host-group-gauge.sh` (a systemd timer, every 5 minutes, installed by
`bootstrap-vps.sh`) reads their cgroup v2 memory and writes
`host_group_memory_bytes{group}` and `host_group_memory_swap_bytes{group}` for the
textfile collector. RAM is `memory.current` minus `inactive_file`, the same
"resident, no reclaimable cache" definition as the container groups. The rule
*a host group is over its memory budget* (`host-group-over-budget`, warning, same
30-minute average and 15 minutes) is alert only, never a cap. The groups are:

| `group` | cgroups read |
| --- | --- |
| `operator` | `user.slice/user-1001.slice` |
| `runners` | `system.slice/actions.runner.*.service` |
| `os` | `system.slice/{docker,containerd,tailscaled,systemd-journald}.service` (never all of `system.slice`: docker's container scopes live there) |

The swap gauge is margin, not budget: it has no rule of its own, the host-wide
*host swap is over 75% used* rule covers it.

## Changing a budget

The budget lives in three places that change together: the table above, the
`project-memory-over-budget` (or `burst-pool-over-budget`) expression in `rules.yml`,
the `host-group-over-budget` expression for a host group,
and, when a group's members change, the member selectors in both of those rules. A
budget moves because measured use moved, not because an alert was annoying: check the
7-day peak first. A merge to `main` reloads the rules on deploy; nothing is recreated.
