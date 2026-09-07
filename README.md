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

[`compose/docker-compose.yml`](./compose/docker-compose.yml) defines eight services — one of which, `openclaw-cli`, is gated behind the `cli` compose profile (on-demand only). `devclaw-mcp` and the former `devclaw-sandbox` build image moved to the devclaw repo (devclaw spec 005), so they no longer appear below; the `ops-agent` watchdog that used to be built here was retired on 2026-09-06 in favour of the observability stack's alert rules. All long-running services inherit the `x-policy` anchor (see [Uniform service policy](#uniform-service-policy)).

| Service | Image | Role |
| --- | --- | --- |
| `openclaw-gateway` | `lifekit-openclaw:local` (built from `compose/openclaw-gateway/`) | Runtime gateway — channels, cron, skills, agent. Loopback bind on `127.0.0.1:18789`. |
| `openclaw-cli` | `lifekit-openclaw:local` | Same image as the gateway, joined into its network namespace via `network_mode: service:openclaw-gateway`. Used for one-shot `openclaw <command>` invocations against the gateway. **On-demand only** — gated behind the `cli` compose profile so `docker compose up -d` does not start it. Invoke via `docker compose --profile cli run --rm openclaw-cli <command>` (preferred) or `docker compose --profile cli up -d openclaw-cli` for a persistent session. |
| `lifekit-orchestrator` | `lifekit-openclaw:local` | Long-running Python scheduler (`devclaw-orchestrator daemon`) that replaced the OpenClaw cron entries `task_dispatch_15m` and `curator_30m`. Editable-installed from the bind-mounted source on every container start to undo `pip install -e .` hijacks from code-task runners. |
| `notify-relay` | `notify-relay:local` (built from `compose/notify-relay/`) | The stack's only Telegram renderer: producers POST the [notify envelope](./docs/notify-envelope.md) to `/notify` and the relay renders it as Telegram HTML via direct Bot API call. DevClaw's legacy `notify_url` paths (`/devclaw`, `/text`) still work while they migrate. Internal-only on `:8090`. |
| `prometheus` | `prom/prometheus:v2.54.1` | Box-level metrics: scrapes finance-sentry's API and devclaw-mcp's `/metrics` (the dead-man signal). 30d / 5GB retention. Loopback `:9090`. |
| `loki` | `grafana/loki:3.1.1` | Structured logs from finance-sentry (fire-and-forget push). ~14d retention. Loopback `:3100`. |
| `grafana` | `grafana/grafana:11.2.0` | Dashboards (each product repo hands its JSON over via a mounted dir) and the **provisioned alert rules** in `compose/observability/grafana/provisioning/alerting/` — Telegram straight from Grafana, no relay in the path. Loopback `:3000`, fronted by Tailscale Serve. |
| `google-workspace-mcp` | `ghcr.io/taylorwilsdon/google_workspace_mcp:1.21.0` | Single-user MCP bridge to Gmail/Drive/Calendar/Docs/Sheets/Tasks. Internal-only (`expose: "8000"`, no host port); reached by the gateway via compose DNS at `http://google-workspace-mcp:8000/mcp/`. |

### Uniform service policy

Every service merges the `x-policy` anchor at the top of `compose/docker-compose.yml`:

- `init: true`
- `restart: on-failure:5` — restart loop circuit-breaker; gives up after 5 consecutive failures instead of pinning a CPU forever.
- `logging.driver: json-file` with `max-size: 50m` and `max-file: 3` — caps each service's on-disk log footprint at ~150MB.
- `deploy.resources.limits.memory: 1g` — per-service ceiling. Individual services override (gateway 2g, openclaw-cli/orchestrator/google-mcp 512m).

Rationale lives in the [2026-05-20 VPS-freeze postmortem](#) — an unbounded log + no memory cap on a runaway agent loop ate the disk and pinned RAM until the host froze. The host also gained a **2 GB `/swapfile`** as a second line of defense; `scripts/bootstrap-vps.sh` provisions it.

## Monitoring

Per-container resource and process telemetry is collected by **[Netdata](https://www.netdata.cloud/)** installed on the host (not in a container). It is the canonical monitoring layer for this stack.

- **Dashboard:** `http://<tailnet-ip>:19999` — bound to the tailnet interface only, no public ingress.
- **Alerts:** delivered to Telegram chat `123456789`.

If you want app-level logs, `docker compose logs <service>` is still the path — Netdata only watches the host + container resource envelopes.

**App-level observability** (since 2026-09-06, moved here from finance-sentry) is the `prometheus` + `loki` + `grafana` trio in this compose file. Netdata stays the host layer; Grafana is the app layer and the **alerting** layer:

- Prometheus scrapes finance-sentry's API over `compose_default` and devclaw-mcp's `/metrics` over `lifekit-shared`.
- Alert rules are files, not clicks: `compose/observability/grafana/provisioning/alerting/`. (`contact-points.yml` is the one exception: `deploy.sh` renders it from the committed `.tmpl` so the owner's chat id never enters git, and fails the deploy when it is unset — a Grafana with no contact point starts happily and drops every alert.) Today's rules are devclaw's dead-man watch — *devclaw is down* (no scrape for 3 min) and *devclaw heartbeat is hung* (tick age over three tick lengths while dispatch is open, for 5 min) — delivered to Telegram directly, so a dead `notify-relay` cannot swallow them. This replaced the LLM-driven `ops-agent`. Their message template re-implements the [notify envelope](./docs/notify-envelope.md)'s grammar — the duplication that exemption costs, held by `compose/observability/contact-points.test.js`.
- Dashboards keep one home per product: finance-sentry's deploy copies its JSON into `${LIFEKIT_FINANCE_SENTRY_DASHBOARDS}` and Grafana loads that directory as a provider.
- Data volumes are external (`docker_*`, created by finance-sentry's former project) so the history survived the move; see `.env.example`.

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
├── compose/              # docker-compose.yml, Dockerfiles, OpenClaw sources
│   └── observability/    # prometheus + loki config, Grafana provisioning (datasources, dashboard providers, ALERT RULES)
├── scripts/              # bootstrap-vps.sh, deploy.sh, oclaw
├── skills/               # parameterized workspace skills (opt-in via wizard)
├── docs/                 # quickstart, architecture, runbook, google-mcp-setup, customizing-skills, notify-envelope (the one Telegram message format), PRIVATE.md (the never-commit audit checklist)
└── .github/workflows/    # CI (pre-commit lint, gitleaks full-history, tests, deploy — VPS self-hosted runner), doc-drift, release-please + weekly release
```

## VPS users

The reference VPS has two service accounts with different responsibilities — keep them straight when SSH'ing in:

- **`denys`** — the human admin account. Has `NOPASSWD` sudo. Use this for any host-level change (systemd, `apt`, firewall, Netdata config).
- **`lifekit`** — the deploy + automation account. Owns `/srv/lifekit-stack`, `/srv/life`, `/srv/openclaw/*`, the Claude CLI session under `/home/lifekit/.claude/`, and runs the **GitHub Actions self-hosted runner** that CI jobs in `.github/workflows/` dispatch to. No sudo.

The compose bind-mounts (Claude session, `gh` config, `.gitconfig`) all resolve to `/home/lifekit/...` for this reason.

## Dashboard access

The lifekit-dashboard web UI is **not part of this stack anymore** — it deploys from its own repo ([`lifekit-hq/lifekit-dashboard`](https://github.com/lifekit-hq/lifekit-dashboard), see its `deploy/`) as its own `dashboard` compose project, integrated with this stack only through read-only host mounts (ecosystem decoupling, 2026-08-16). It still binds loopback-only on `127.0.0.1:18790`; external access goes through Tailscale serve:

```bash
sudo tailscale serve --bg --https=443 / http://127.0.0.1:18790
```

The dashboard is then reachable at `https://<hostname>.<tailnet>.ts.net/` from any device on your tailnet. `tailscale serve status` lists the resulting URL.

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

- **Host:** [Hetzner](https://www.hetzner.com/cloud) CX22 (2 vCPU / 4GB RAM, ≈€4/mo, EU), Ubuntu 24.04. Any other Debian-family VPS with comparable specs should work; the only setup that gets active issue-tracking is this one.
- **Mesh VPN:** [Tailscale](https://tailscale.com/) with an [unattended-join auth key](https://login.tailscale.com/admin/settings/keys). The host's UFW closes all public ports except ICMP; admin access (SSH, SSHFS) goes through the mesh.
- **Chat transport:** Telegram long-polling — the gateway dials out to Telegram, no inbound webhook needed. Create a bot via [@BotFather](https://t.me/BotFather) (grab the token), then DM [@userinfobot](https://t.me/userinfobot) to get your own numeric user ID (this becomes the owner allowlist).
- **Monitoring:** [Netdata](https://www.netdata.cloud/) on the host (not containerized). Tailnet-only dashboard at `http://<tailnet-ip>:19999`; alerts to Telegram chat `123456789`.
- **Local LLM:** Anthropic Haiku on the same VPS (CPU-only, no GPU required).

Swap any of these by writing a new adapter against the corresponding port — the rest of the stack doesn't know or care.
