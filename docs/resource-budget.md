# Resource budget - memory per project

The box has 15.6 GiB of RAM and 8 GiB of swap (the live `/swapfile`; `scripts/bootstrap-vps.sh` creates 4 GiB on a fresh host, and the file was grown since). Every project and host group gets a
memory budget so a breach names an owner instead of "the box is slow". The budgets sum
to **12.25 GiB**; a **3 GiB burst pool** for short-lived work brings the total to
**15.25 GiB** of the 15.6 GiB.

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

The 7-day peaks are the Prometheus maximum of the group's containers to 2026-09-29;
host groups have no history yet, so their figure is a one-off snapshot.

| Group | Budget (GiB) | Contains | 7-day peak |
| --- | --- | --- | --- |
| openclaw | 3.5 | `openclaw-gateway`, `google-workspace-mcp` (compose project `openclaw`) | 3723 + 153 MiB |
| platform | 1.5 | prometheus, loki, grafana, tempo, otel-collector, node-exporter, container-exporter, notify-relay (compose project `compose`) | 1.3 GiB (sum of peaks) |
| finance-sentry | 1.25 | api, postgres, mcp, gateway, frontend, uptime-probe (`finance-sentry-*`, compose project `docker`) | 1.05 GiB (average 0.81) |
| devclaw-mcp | 0.25 | `devclaw-mcp` (compose project `devclaw`; its sandboxes count in the burst pool) | 130 MiB |
| dashboard | 0.25 | the dashboard service (compose project `dashboard`) | 114 MiB |
| xui | 0.25 | web, db (compose project `xui`) | 188 MiB |
| operator sessions | 3.5 | the operator's login slice (`user-1001.slice`): agent sessions, their browser helpers, tools | about 2.2 GiB resident + 1.2 GiB swapped (snapshot) |
| runners | 0.75 | the CI runner services and their jobs | about 0.3 GiB idle, 0.7 GiB during a deploy job (snapshot) |
| OS | 1.0 | dockerd, containerd, shims, tailscaled, journald | about 0.75 GiB (snapshot) |
| **Sum** | **12.25** | | |
| burst pool | 3.0 | everything else that runs as a container: devclaw sandboxes, `openclaw-cli` runs, rehearsals, CI and worker validation containers, test compose projects, any orphan | 2.8 GiB unlabelled sum; one worker container reached 3164 MiB |
| **Total** | **15.25** | of 15.6 GiB RAM | |

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
| the burst pool is over its memory budget | the burst pool's 30-minute average is over 3 GiB, for 15 minutes | warning |
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
