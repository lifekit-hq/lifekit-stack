# lifekit-stack

> One-command personal-AI stack on your own VPS. Reference deployment for [lifekit](https://github.com/lifekit-hq/lifekit).

`lifekit-stack` is a starter-template that deploys a complete personal-AI environment to a fresh VPS in under 15 minutes:

- **[OpenClaw](https://openclaw.ai/)** — the runtime gateway (the `RuntimeGateway` adapter port). Chat bot, voice, scheduled briefs, conversational agent, workspace skills.
- **[lifekit](https://github.com/lifekit-hq/lifekit)** — the Python framework that owns your `~/.life/` knowledge layer and the wizard.
- **Workspace skills** — opt-in skills bundled with this template (morning brief, learning coach, brainstorm, calendar/gmail integration, and more — see [`skills/`](./skills/)).
- **Docker Compose + a single bash bootstrap script** — infrastructure-as-code. Reproducible from `git clone`.
- **Mesh-VPN + loopback-only** — no public ingress, no domain, no TLS to manage. Outbound long-polling only.

The design is provider-neutral by construction: every swappable component sits behind an adapter port — `RuntimeGateway`, `BuildEngine`, `Sandbox`, `LocalLLM` — documented in [`docs/architecture.md`](./docs/architecture.md). The [Reference deployment](#reference-deployment) section below names the exact tested combination, but each component is a swap-point, not a hard requirement.

Autonomous build/agent workloads (swarm and similar) are explicitly **not** sibling containers here — when they land in a future release, they run *inside* a NemoClaw sandbox spawned per invocation by OpenClaw. See [`docs/architecture.md`](./docs/architecture.md) for the boundary.

## Status

**Pre-release.** Active development. Not yet ready for general adoption; first cohort of users coming soon. Watch the repo or open an issue if you'd like a heads-up.

## Services

[`compose/docker-compose.yml`](./compose/docker-compose.yml), compose project `compose`, defines the services below. OpenClaw's three services run as their own compose project, `openclaw` (see [OpenClaw services](#openclaw-services)). `devclaw-mcp` and the former `devclaw-sandbox` build image moved to the devclaw repo (devclaw spec 005), so they no longer appear below; the `ops-agent` watchdog that used to be built here was retired on 2026-09-06 in favour of the observability stack's alert rules. All long-running services inherit the `x-policy` anchor (see [Uniform service policy](#uniform-service-policy)).

| Service | Image | Role |
| --- | --- | --- |
| `notify-relay` | `notify-relay:local` (built from `compose/notify-relay/`) | The one renderer of owner notifications: producers `POST /notify` a JSON envelope ([`docs/message-format.md`](./docs/message-format.md)) and it sends Telegram HTML via direct Bot API call; DevClaw's legacy `notify_url` POSTs (`/devclaw`, `/text`) map onto the same renderer. Internal-only on `:8090`. |
| `prometheus` | `prom/prometheus:v2.54.1` | Box-level metrics: scrapes finance-sentry's API, devclaw-mcp's `/metrics` (the dead-man signal), the gateway's `/api/diagnostics/prometheus` (bearer token from the `openclaw_gateway_token` compose secret), and the observability stack's own `/metrics` - Grafana, Prometheus itself, Loki, Tempo, otel-collector. 30d / 5GB retention. Loopback `:9090`. |
| `loki` | `grafana/loki:3.1.1` | Structured logs from finance-sentry (fire-and-forget push) and every container's console output via `otel-collector`. ~14d retention. Loopback `:3100`. |
| `grafana` | `grafana/grafana:11.2.0` | Dashboards (each product repo hands its JSON over via a mounted dir) and the **provisioned alert rules** in `compose/observability/grafana/provisioning/alerting/` — Telegram straight from Grafana, no relay in the path. Loopback `:3000`, fronted by Tailscale Serve. Iframe embedding is allowed only for the single origin in `GRAFANA_EMBED_ORIGIN` (CSP `frame-ancestors`; optional - when unset, deploy derives it from the host's tailnet name and the dashboard's served HTTPS port, and if that fails it stays `'none'`); anonymous access stays off. Optional sign-in through the org identity provider (`GRAFANA_OIDC_ENABLED`, off by default; `docs/runbook.md` "Grafana sign-in through Logto"). |
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.110.0` | OTLP trace receiver (`:4317` gRPC, `:4318` HTTP) — apps export traces here, the batch processor fans them into Tempo. Also tails every container's Docker json-file log (`/var/lib/docker/containers`, read-only; offsets in the `otel_filelog_state` volume) and ships it to Loki's OTLP endpoint for crash forensics, bounded by a `memory_limiter`. Also serves its own `/metrics` on `:8888` (`service.telemetry.metrics.address` bound to `0.0.0.0`, otherwise loopback-only inside the container) for the `prometheus` scrape job. Same cross-project seam as `loki`/`prometheus`: joins `default` and `lifekit-shared` so finance-sentry-api and devclaw-mcp can both reach it. Internal-only, no host ports. |
| `tempo` | `grafana/tempo:2.6.1` | Trace storage, local backend on a project volume. Retention bounded at `compactor.block_retention: 168h` (7d) — the disk was 83% full at grounding time. Internal-only, no host ports; Grafana reaches it at `tempo:3200`. |
| `container-exporter` | `container-exporter:local` (built from `compose/container-exporter/`) | The Docker daemon's own facts about **every** container on the box - running, restart count, health, exit code, memory, swap - as Prometheus metrics, each labelled with its image. The one signal the `box` alert rules read, whichever repo owns the container. Read-only socket access as `nobody` + the docker group; internal-only on `:9417`. |
| `node-exporter` | `prom/node-exporter:v1.8.2` | Host metrics - root filesystem free space, memory, load average and pressure-stall (PSI) CPU/memory/IO pressure (meminfo/loadavg/pressure collectors; the rest stay disabled; PSI is recorded only, no alert), the signal that was missing when the box hit 85% used unnoticed, plus the textfile collector for host timers (`scripts/quota-gauge/` writes the shared Claude account's remaining quota per window and its runway every 5 minutes, and each consumer's share of it hourly; `scripts/tmp-gauge/` writes `/tmp` size and available bytes every 5 minutes, since `/tmp` is tmpfs and node-exporter's own filesystem collector excludes tmpfs; `scripts/host-gauge/` writes RAM and swap for the operator sessions, CI runners and OS groups every 5 minutes - `scripts/unit-gauge/` writes `host_unit_active` / `host_unit_failed` per host systemd unit - the CI runners, tailscaled, docker, containerd, cron, unattended-upgrades and each lifekit timer's service - every 5 minutes, read by the *host systemd unit is down or failed* alert; all five are systemd timers installed by `bootstrap-vps.sh`; `scripts/fleet-publisher/` is a sixth, every minute, and writes the `fleet_*` agent-fleet metrics shown in the dashboard's Fleet row - see `docs/runbook.md` "Fleet publisher"). Read-only root mount. Loopback `:9100`. |

### Uniform service policy

Every service merges the `x-policy` anchor at the top of `compose/docker-compose.yml` (`compose/openclaw/docker-compose.yml`, `compose/identity/docker-compose.yml` and `compose/edge/docker-compose.yml` carry the same anchor):

- `init: true`
- `restart: on-failure:5` — restart loop circuit-breaker; gives up after 5 consecutive failures instead of pinning a CPU forever.
- `logging.driver: json-file` with `max-size: 50m` and `max-file: 3` — caps each service's on-disk log footprint at ~150MB.
- `deploy.resources.limits.memory: 1g` — per-service ceiling. Most services override it with their own limit; the compose files are the source for each value. Every service, profile-gated ones included, must resolve to a limit (a container without one reports the host's total RAM as its limit, which breaks any memory-share signal); `scripts/tests/test_memory_limits.py` fails when one does not.
- `deploy.resources.limits.pids: 256` — per-service process cap, sized per service in the compose files like the memory limit.
- `cap_drop: [ALL]` and `security_opt: [no-new-privileges:true]` — no service gets a capability back; each runs as its image's non-root user (otel-collector is the one root process, for the Docker log files it tails). Services also set `read_only: true` where they can; the compose files comment each one that does not.

Rationale lives in the [2026-05-20 VPS-freeze postmortem](#) — an unbounded log + no memory cap on a runaway agent loop ate the disk and pinned RAM until the host froze. The host also gained a `/swapfile` as a second line of defense: `scripts/bootstrap-vps.sh` creates a 4 GiB one on a fresh host (skipped when `/swapfile` is already active), and the reference host currently runs 8 GiB (see [`docs/resource-budget.md`](docs/resource-budget.md)).

## OpenClaw services

[`compose/openclaw/docker-compose.yml`](./compose/openclaw/docker-compose.yml) runs as its own compose project, `openclaw`, so OpenClaw deploys and recreates apart from the platform. The gateway joins the external `lifekit-shared` network next to its project's own `default`, so Prometheus still scrapes it at `openclaw-gateway:18789` and it still reaches notify-relay, otel-collector and the product MCP servers. Scripts find the gateway container (`openclaw-openclaw-gateway-1`) by its compose labels. `docs/runbook.md` "The OpenClaw compose project: cutover and rollback" covers the move.

| Service | Image | Role |
| --- | --- | --- |
| `openclaw-gateway` | `lifekit-openclaw:local` (built from `compose/openclaw-gateway/`) | Runtime gateway — channels, cron, skills, agent. Loopback bind on `127.0.0.1:18789`. |
| `openclaw-cli` | `lifekit-openclaw:local` | Same image as the gateway, joined into its network namespace via `network_mode: service:openclaw-gateway`. Used for one-shot `openclaw <command>` invocations against the gateway. **On-demand only** — gated behind the `cli` compose profile so `docker compose up -d` does not start it. Invoke from `compose/openclaw/` via `docker compose --profile cli run --rm openclaw-cli <command>` (preferred) or `docker compose --profile cli up -d openclaw-cli` for a persistent session. |
| `google-workspace-mcp` | `ghcr.io/taylorwilsdon/google_workspace_mcp:1.21.0` | Single-user MCP bridge to Gmail/Drive/Calendar/Docs/Sheets/Tasks. Internal-only (`expose: "8000"`, no host port); reached by the gateway via compose DNS at `http://google-workspace-mcp:8000/mcp/`. |

## Identity services

[`compose/identity/docker-compose.yml`](./compose/identity/docker-compose.yml) is compose project `identity`: the org identity provider every app on the box signs in through. Tailscale Serve publishes both ports on the host's tailnet name over HTTPS, tailnet-only. `deploy.sh` brings the project up once `LOGTO_DB_PASSWORD` is in the env file. `docs/runbook.md` "Identity provider (Logto)" covers the Serve ports, first boot, clients and backup; "Logto admin through the Management API" covers the scripted admin steps (`scripts/identity/`). Logto and its on-box OIDC clients (Grafana first) share the external `identity-oidc` network, which `deploy.sh` creates; `lifekit-shared` stays off Logto.

| Service | Image | Role |
| --- | --- | --- |
| `logto` | `ghcr.io/logto-io/logto:1.44.0` | OIDC provider and sign-in pages on loopback `:3001` (issuer `https://<name>:3001/oidc`), admin console on loopback `:3002`. Sign-in only (no self-registration): Google and passkeys; email waits for a sending domain. |
| `postgres` | `postgres:17-alpine` | Logto's database, on an internal network with no egress and no host port. Dumped nightly by `scripts/identity-backup/` (host timer). |

## Edge services

[`compose/edge/docker-compose.yml`](./compose/edge/docker-compose.yml) is compose project `edge`: the tailnet sign-in gate. One sign-in with the identity provider covers the lifekit dashboard and the devclaw console; devclaw's MCP and other bearer clients pass through unchanged. finance-sentry is not behind it: it becomes its own Logto client. `deploy.sh` brings the project up once the identity project is up and `EDGE_OIDC_CLIENT_ID`, `EDGE_OIDC_CLIENT_SECRET` and `EDGE_COOKIE_SECRET` are in the env file. Nothing reaches it until the operator points Tailscale Serve at it: `docs/runbook.md` "Tailnet sign-in gate" has the Logto client, the cutover and the rollback.

| Service | Image | Role |
| --- | --- | --- |
| `traefik` | `traefik:v3.7.13` | Forward-auth proxy. Loopback entrypoints `:18890` (dashboard) and `:18891` (devclaw), which Tailscale Serve's `18790` and `18791` point at after the cutover. Routes in `compose/edge/traefik/dynamic.yml`. |
| `oauth2-proxy` | `quay.io/oauth2-proxy/oauth2-proxy:v7.15.5` | Confidential OIDC client of Logto. Answers Traefik's check from a 30-day session cookie that it refreshes against Logto hourly. Only users with the Logto role `admin` get through. Reaches Logto's token endpoints over `identity-oidc`. No host port. |

## Monitoring

**Alerting is one pipeline for every service on the box**: Prometheus scrapes, Grafana evaluates provisioned rules, Telegram receives - the same path whether the container belongs to this stack, devclaw, finance-sentry or the dashboard. A service does not have to export anything to be covered; it only has to be a container.

- **Box layer - `container-exporter`** (`compose/container-exporter/`, since 2026-09-13): the daemon's own facts about every container, running or not. The `box` rule group in `compose/observability/grafana/provisioning/alerting/rules.yml` reads them: *scrape target is down* (`up` < 1 for 3 min, any job - the old *devclaw is down* folded in), *container exited abnormally* (stopped, non-zero exit, not being restarted, 3 min), *long-running container is stopped or gone* (any exit code, including a clean stop, or a service that had a running series 30 min ago and has none now, for 15 min; limited to the `compose`, `openclaw`, `docker`, `devclaw`, `dashboard` and `xui` projects minus `openclaw-cli` - a deliberate retirement fires once for about 30 min; the gone leg stays quiet while the exporter's scrape target is down - `scrape target is down`'s job), *container is restart-looping* (more than 2 daemon restarts in 15 min, 5 min), *container healthcheck is failing* (5 min), *docker exporter scrape errors above zero* (the exporter can't read the Docker daemon socket, so every other `box` rule goes blind, 3 min), *a container was OOM-killed* (the kernel killed a process for using too much memory, fires immediately; it replaces the old near-limit ratio rule, which could not fire for uncapped containers), */tmp is nearly full* (`tmp_filesystem_avail_bytes` / `tmp_filesystem_size_bytes` below 20% free, 5 min - `scripts/tmp-gauge/` publishes the two gauges since `/tmp`'s tmpfs is outside node-exporter's own filesystem collector; response is the host cleanup job, see `docs/runbook.md` "`/tmp` usage alert"), *host systemd unit is down or failed* (`host_unit_active` / `host_unit_failed` from `scripts/unit-gauge/`, 10 min), *fleet summary is stale* (the fleet home's summary older than 15 min, 5 min), *textfile metrics are stale* (a host-timer gauge older than 15 min) and *textfile collector cannot read a file*. The restart-loop rule is the one that would have reported finance-sentry-mcp's three-day crash loop (2026-09-10..13, 4211 restarts, found by hand). *root filesystem is read-only* was retired 2026-09-28 as a permanent false positive - node-exporter's own read-only `/host` bind is indistinguishable from a genuine remount, see the `deleteRules` entry in `rules.yml`.
- **Memory budgets** ([`docs/resource-budget.md`](docs/resource-budget.md)): each project and host group has a documented sustained 30-minute-average budget (who uses what, not an alert threshold); the `memory-budget` rule group has one host-level *memory is under pressure* alert (available RAM under 2 GiB or sustained swap-in, 15 min, listing the top three groups by RSS), plus a capped container passing 90% of its cap and swap alone passing 75%.
- **App layer** - a service that exports its own `/metrics` gets application rules on top: devclaw's *heartbeat is hung* (tick age over three tick lengths while dispatch is open, 5 min), notify-relay's *cannot deliver* (Telegram sends failing with zero successes in the same 15 min window, 5 min - liveness is `scrape target is down`'s job, not this rule's). finance-sentry's API is scraped for its dashboards and, in the `finance-sentry-jobs` group, for its job-age gauges: *backup verification is stale* (no restore-verified backup for over 9 days, or none at all while the API is up, 1h) and *retention run is stale* (a retention run older than 2 days, or no Purge series while the API is up, 1h; an absent Downsample series is deliberately not alerted on). Both stay quiet when the scrape target is down - `scrape target is down`'s job.
- **OpenClaw layer** - the `diagnostics-prometheus` plugin is an official `openclaw plugins install` in the gateway state dir (same version as the core; only a trusted install gets the internal diagnostics feed, a load-path copy exports an empty body), re-pinned by `deploy.sh` on version bumps like the other official plugins. The `openclaw` rule group's *scheduled or background run failed* fires on the first failed agent run in 15 minutes: `error`, `aborted` or `blocked` for cron runs (a timeout reads `aborted`), `error` for every other trigger except `user`. OpenClaw 2026.9.4 exports no cron- or task-ledger series, so command-kind cron jobs and `lost` tasks are not covered. The trigger values seen in OpenClaw 2026.9.4 core are `user`, `cron`, `heartbeat`, `memory`, `overflow`, `manual` and `cli`; interactive turns (`trigger=user`) are excluded on purpose, so background task or subagent runs labelled `user` are not covered either. `openclaw tasks audit` is still the check for those.
- **Observability layer - the stack watching itself** (`prometheus.yml` scrapes Grafana, Prometheus, Loki, Tempo and the otel-collector's own `/metrics`; since 2026-09-25). The `observability` rule group alerts on the alert path's own failure modes: *Grafana alert notification delivery is failing* and *Grafana alert rule evaluation is failing* (5 min each), *Prometheus config reload is not successful* (5 min - a bad `prometheus.yml` would otherwise leave the old config running behind a green deploy), *Prometheus TSDB compaction is failing* and *Prometheus is dropping notifications* (fire immediately).
- **Rules are files, not clicks.** `deploy.sh` reloads them through Grafana's admin API on every deploy (Grafana reads provisioning only at startup, and `up -d` does not recreate it for a changed bind-mounted file). `contact-points.yml` is rendered from the committed `.tmpl` so the owner's chat id never enters git, and the deploy fails when it is unset - a Grafana with no contact point starts happily and drops every alert. Delivery is Telegram straight from Grafana, so a dead `notify-relay` cannot swallow the alarm. This replaced the LLM-driven `ops-agent`.
- **Netdata** on the host was the earlier resource layer. It is not running (`systemctl is-active netdata` = `inactive`, verified 2026-09-13) and nothing depends on it; `docker compose logs <service>` and Loki are the log path.
- The `lifekit` provider (`provisioning/dashboards/lifekit/`) holds this repo's own dashboards: `devclaw` and **Box at a glance** (health, resources, the shared Claude account, agents, notifications, and the alert-rule list).
- Dashboards keep one home per product: finance-sentry's deploy copies its JSON into `${LIFEKIT_FINANCE_SENTRY_DASHBOARDS}` and Grafana loads that directory as a provider.
- Data volumes are external (`docker_*`, created by finance-sentry's former project) so the history survived the move; see `.env.example`.

## Platform contract

Every product on the box declares, in `lifekit.contract.*` compose labels, how it meets the platform contract (health + readiness, metrics in Prometheus, JSON logs with a trace id, and - once their platform pieces land - traces, edge, topics). `deploy.sh` refuses to `up` an undeclared service and goes red when a running one fails; product repos run the same `scripts/platform-contract.py` on their own project. No waivers. See [`docs/platform-contract.md`](docs/platform-contract.md).

## How it fits together

```
                  ┌──────────────────┐
   You (phone) ───┤ Chat transport   │
                  └────────┬─────────┘
                           │ long-polling (or webhook)
                  ┌────────▼─────────────────────────────┐
                  │ Your VPS (Debian/Ubuntu, loopback)   │
                  │                                       │
                  │   ┌─────────────────────────────────┐ │
                  │   │ OpenClaw gateway (loopback)     │ │
                  │   │   channels, cron, skills, agent │ │
                  │   └────┬─────────────────────┬──────┘ │
                  │        │ reads/writes        │ calls   │
                  │        ▼                     ▼         │
                  │   ┌──────────┐                         │
                  │   │ ~/memory/│                         │
                  │   │ (data)   │                         │
                  │   └──────────┘                         │
                  └───────────────────────────────────────┘
                           ▲
                           │ SSHFS over mesh VPN
   You (laptop) ───────────┘
```

Your laptop SSHFS-mounts the VPS's `~/.life/` over a mesh VPN. The VPS is canonical; your laptop is a thin client. The stack survives your laptop being closed.

## Prerequisites

Before you start the wizard, have these ready:

- **A VPS.** Any Debian or Ubuntu 24.04+ host with at least 2GB RAM. (See [Reference deployment](#reference-deployment) for the exact provider/plan we test against.)
- **A mesh-VPN account** with an unattended-join auth key.
- **A chat-transport bot** that supports long-polling (or webhook) delivery, plus your own user ID for the owner allowlist. The [Reference deployment](#reference-deployment) section has the concrete bot-creation steps for the tested transport.
- **Anthropic auth.** Either an active Claude CLI login (`claude login`) or an API key.
- **Optional:** OpenAI API key (for Codex CLI integration), provider keys for finance skills (Plaid, Monobank, IBKR, Binance).

You'll also need [`pipx`](https://pipx.pypa.io/) on your laptop to install the `lifekit` CLI.

## Quickstart

The high-level flow: install the CLI, clone this template, run the wizard. The wizard handles the rest end-to-end. Full walkthrough (with the tested-stack copy-paste commands) lives in [`docs/quickstart.md`](./docs/quickstart.md).

### Reference walkthrough

The exact commands the maintainer runs against the reference deployment:

```bash
# Install the lifekit CLI (one-time)
pipx install lifekit

# Clone this template
git clone https://github.com/lifekit-hq/lifekit-stack.git
cd lifekit-stack

# Run the wizard — prompts you for tokens + identity, generates configs,
# bootstraps the VPS, starts the stack, verifies a Telegram round-trip
lifekit init-stack
```

Full walkthrough: [`docs/quickstart.md`](./docs/quickstart.md).

## What's inside

```
lifekit-stack/
├── compose/              # docker-compose.yml (platform project), Dockerfiles, OpenClaw sources
│   ├── openclaw/         # docker-compose.yml for compose project `openclaw` (gateway, cli, google-workspace-mcp)
│   ├── identity/         # docker-compose.yml for compose project `identity` (logto, postgres)
│   ├── edge/             # docker-compose.yml for compose project `edge` (traefik, oauth2-proxy: the tailnet sign-in gate)
│   └── observability/    # prometheus + loki config, Grafana provisioning (datasources, dashboard providers, ALERT RULES)
├── scripts/              # bootstrap-vps.sh, deploy.sh, oclaw
├── skills/               # parameterized workspace skills (opt-in via wizard)
├── docs/                 # quickstart, architecture, runbook, google-mcp-setup, customizing-skills, PRIVATE.md (the never-commit audit checklist)
└── .github/workflows/    # CI (pre-commit lint + doc-drift, gitleaks full-history, tests on hosted runners, deploy on the VPS self-hosted runner), release-please + weekly release
```

## VPS users

The reference VPS has two service accounts with different responsibilities — keep them straight when SSH'ing in:

- **`denys`** — the human admin account. Has `NOPASSWD` sudo. Use this for any host-level change (systemd, `apt`, firewall, Netdata config).
- **`lifekit`** — the deploy + automation account. Owns `/srv/lifekit-stack`, `/srv/life`, `/srv/openclaw/*`, the Claude CLI session under `/home/lifekit/.claude/`, and runs the **GitHub Actions self-hosted runner** that CI jobs in `.github/workflows/` dispatch to. No sudo.

The compose bind-mounts (Claude session, `gh` config, `.gitconfig`) all resolve to `/home/lifekit/...` for this reason.

## Dashboard access

The lifekit-dashboard web UI is **not part of this stack anymore** — it deploys from its own repo ([`lifekit-hq/lifekit-dashboard`](https://github.com/lifekit-hq/lifekit-dashboard), see its `deploy/`) as its own `dashboard` compose project, integrated with this stack only through read-only host mounts (ecosystem decoupling, 2026-08-16). It still binds loopback-only on `127.0.0.1:18790`; external access goes through Tailscale serve:

```bash
sudo tailscale serve --bg --https=<port> / http://127.0.0.1:18790
```

The dashboard is then reachable at `https://<hostname>.<tailnet>.ts.net:<port>/` from any device on your tailnet (omit `:<port>` only if you serve it on 443). `tailscale serve status` lists the resulting URL. Deploy reads that serve configuration, matched against the dashboard container's published port, to derive Grafana's embed origin when `GRAFANA_EMBED_ORIGIN` is unset.

## Updating

```bash
cd lifekit-stack
git pull
lifekit init-stack --target <your-host>   # idempotent — reapplies templates, restarts services
```

The wizard saves your choices to `wizard.yaml` on first run, so re-runs are non-interactive.

## What's NOT in this repo

This repository contains **only code, config templates, and deploy logic**. Personal data and secrets stay out by design — see [`docs/PRIVATE.md`](./docs/PRIVATE.md) for the full audit checklist. Briefly:

- **Your `~/.life/` data** — your journal, domains, knowledge layer. Lives on your VPS. Optionally back it up to your own private git repo, never this one.
- **Secrets** — bot tokens, API keys, encryption keys. Generated locally by the wizard, never committed.
- **Anything PII** — names, schedules, locations, dietary constraints, financial details. These get injected into skills at deploy time via the wizard's user-context.

The wizard enforces this; pre-commit hooks ([gitleaks](https://github.com/gitleaks/gitleaks)) catch slip-ups before they push.

## Customizing skills

Each skill in `skills/` is parameterized via Jinja2 templates that read from a user-context file the wizard generates. Want to write your own? See [`docs/customizing-skills.md`](./docs/customizing-skills.md).

## Architecture, in depth

See [`docs/architecture.md`](./docs/architecture.md) for adapter choices, port catalog, blast-radius topology, and why we made the trade-offs we did.

## Contributing

Issues and PRs welcome. See [`CONTRIBUTING.md`](./.github/CONTRIBUTING.md).

Setups outside the [Reference deployment](#reference-deployment) are not officially supported in v0.x — pull requests adding adapters (new chat transports, new mesh VPNs, new hosts) are very welcome, but we won't promise responsiveness on issues for setups outside the supported path.

## License

[MIT](./LICENSE) © 2026 Denys Sychov.

## Related

- [lifekit-hq/lifekit](https://github.com/lifekit-hq/lifekit) — the Python framework + wizard CLI this template uses.
- [OpenClaw](https://openclaw.ai/) — the runtime gateway.
- [The story behind it](#) — _coming soon: blog post on building a file-based framework for personal AI memory._

## Reference deployment

The exact combination this stack is tested against. Every component below is a swap-point via the adapter ports in [`docs/architecture.md`](./docs/architecture.md#2-adapter-pattern-for-every-replaceable-component), not a hard dependency — these are simply the ones the maintainer runs in production.

- **Host:** an arm64 VPS (10 vCPU / 15.6 GiB RAM), Debian 13. Any other Debian-family VPS with comparable specs should work; the only setup that gets active issue-tracking is this one.
- **Mesh VPN:** [Tailscale](https://tailscale.com/) with an [unattended-join auth key](https://login.tailscale.com/admin/settings/keys). The host's nftables firewall (`scripts/host-firewall.sh`) closes every public port except Tailscale's WireGuard port and tcp/80+443 for a public edge; admin access (SSH, SSHFS) goes through the mesh.
- **Chat transport:** Telegram long-polling — the gateway dials out to Telegram, no inbound webhook needed. Create a bot via [@BotFather](https://t.me/BotFather) (grab the token), then DM [@userinfobot](https://t.me/userinfobot) to get your own numeric user ID (this becomes the owner allowlist).
- **Monitoring:** [Netdata](https://www.netdata.cloud/) on the host (not containerized). Tailnet-only dashboard at `http://<tailnet-ip>:19999`; alerts to Telegram chat `123456789`.
- **Local LLM:** Anthropic Haiku on the same VPS (CPU-only, no GPU required).

Swap any of these by writing a new adapter against the corresponding port — the rest of the stack doesn't know or care.
