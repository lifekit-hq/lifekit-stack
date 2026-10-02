# Architecture

A short tour of the design decisions behind `lifekit-stack`. Long-form thinking on each is in the linked references.

## The stack

```
┌─────────────────────────────────────────────────────────────────┐
│  Your VPS (Debian-family, arm64 reference)                      │
│                                                                  │
│  ┌────────────────────────────────────────────────────────┐    │
│  │  OpenClaw gateway (Docker container)                   │    │
│  │  • Telegram long-polling                                │    │
│  │  • Workspace skills (mounted from /srv/openclaw/...)    │    │
│  │  • Native cron scheduler                                │    │
│  │  • Loopback only — 127.0.0.1:18789                      │    │
│  └────┬───────────────────────────────────────────────────┘    │
│       │ reads/writes                                             │
│       ▼                                                           │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  /srv/life/  (your knowledge layer — ~/.life/)          │   │
│  │  • domains/*.md, journal/, queue.jsonl, scout/          │   │
│  │  • plain text + YAML, no DB, fully portable             │   │
│  └─────────────────────────────────────────────────────────┘   │
│       ▲                                                           │
│       │ drains queue, updates domains                            │
│  ┌────┴───────────────────────────────────────────────────┐    │
│  │  lifekit-curator  [RETIRED 2026-05-25]                  │    │
│  │  • Drains /srv/life/queue.jsonl every ~15 min           │    │
│  │  • Only process that mutates domain files               │    │
│  │  • Slow loop, low blast radius                          │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Tailscale (host-level service)                          │   │
│  │  • SSH, SSHFS, admin                                     │   │
│  │  • No public ports beyond ICMP                           │   │
│  └─────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
            ▲
            │ SSHFS over Tailscale
            │
┌───────────┴────────────────┐
│  Your laptop                │
│  • mounts /srv/life/        │
│  • Claude Code reads it     │
│  • thin client              │
└─────────────────────────────┘
```

## Design principles

### 1. Data and code are different things

`~/.life/` is plain Markdown + YAML + JSONL on a POSIX filesystem. Anyone with a text editor can read it. No DB, no proprietary format, no lock-in. The portability invariant is: migration between hosts is `rsync -a /srv/life/ newhost:/srv/life/`. Nothing more.

This is why `~/.life/` content **never** belongs in a deploy repo. Code lives in git; data lives on the host filesystem.

### 2. Adapter pattern for every replaceable component

Every third-party dependency lives behind a port (interface) that the system owns. Swapping an upstream tool = write a new adapter + flip config.

| Port | Current adapter | Why this one | Alternatives |
|---|---|---|---|
| `RuntimeGateway` | OpenClaw | Mature multi-channel runtime, bundled compose, Telegram + voice + cron + skills out of the box. | Custom Telegram bot, custom skill loader. |
| `BuildEngine` | OpenHands (planned) / Codex CLI | OpenHands has a real autonomous loop; Codex CLI is the migration target for the June 2026 Anthropic SDK restriction. | Aider batch, Cline. |
| `Sandbox` | NemoClaw (planned) | Per-spawn sandboxing for autonomous code or money-touching agents. | gVisor, Firecracker, plain containers. |
| `LocalLLM` | Anthropic Haiku (on VPS) | No GPU on CPU VPS; Haiku is cheap, fast, no infra. | Ollama (if a GPU host joins the Tailscale net). |

The point is: nothing in the stack assumes "OpenClaw" or "Anthropic" forever. Both are swap-points behind an explicit boundary.

### 3. Blast-radius decomposition

Two peer containers in v0.x, each restartable without affecting the other:

| Service | Job | Failure mode | Safety mechanism |
|---|---|---|---|
| OpenClaw gateway | All I/O — Telegram, voice, cron, conversational agent | Inbound stops; data layer untouched. | Read-only on `~/.life/` except via lightweight `queue.jsonl` appends. |
| `lifekit-curator` _(retired 2026-05-25)_ | Drains `queue.jsonl` → updates domain files in `~/.life/domains/` | Queue grows; nothing else affected. | Every domain edit is a git commit on `~/.life/`; bad edit = `git revert`. Audit trail is permanent. Plus supervised-mode option per PLAN.md curator protocol. |

**Why swarm is NOT a sibling container.** Autonomous code-writing workloads (the future swarm build engine, autonomous research workers, anything that mutates code or money) live behind the `BuildEngine` / `Sandbox` ports. They are invoked *by* OpenClaw via the LangGraph platform API, run *inside* a NemoClaw sandbox spawned per invocation, and exit when the build is done. They are not long-lived sibling containers in the compose stack. Putting them at the same level as OpenClaw would defeat the per-spawn sandboxing the design relies on.

**Why the curator WAS a sibling container** _(retired 2026-05-25 — kept as design history)_. Curator's blast radius is bounded by git history on `~/.life/`, not by a runtime sandbox. The trust mechanism is the audit trail + supervised-mode patch review. A NemoClaw wrap would add overhead without a security gain for this specific workload.

The split is at the process / container boundary. Within each container, the runtime's native concurrency handles the rest.

### 4. Loopback-only network posture

OpenClaw binds to `127.0.0.1:18789` per upstream Hetzner guidance. The host's nftables firewall (`scripts/host-firewall.sh`, see the runbook's "Host firewall (nftables)") closes every public port except Tailscale's WireGuard port and tcp/80+443 for a public edge, and keeps published container ports off the public interface. Admin access (SSH, SSHFS) goes through Tailscale.

This means:

- **No public attack surface** beyond ICMP.
- **Telegram works** because long-polling is the default — the gateway dials out to Telegram, no inbound webhook needed.
- **You can never type your domain into a browser** and reach the gateway. That's a feature.

Webhook mode is supported as an opt-in (`channels.telegram.webhookUrl`) if you ever need it; the wizard's v0.x default is polling.

### 5. Single source of truth

Once deployed, your VPS's `/srv/life/` is the canonical knowledge layer. Your laptop SSHFS-mounts it. Your phone talks to Telegram which talks to OpenClaw on the VPS. There is exactly one copy of your `~/.life/` data, and the system stays coherent without sync conflicts.

(You should still back it up to a private git repo on a regular schedule. That's a separate concern — see [`runbook.md`](./runbook.md#backups).)

### 6. Templates everywhere, secrets nowhere

Every config that contains user-specific values is a Jinja2 template. The wizard renders them at deploy time, reading values from a `wizard.yaml` it generated. Secrets (tokens, keys) live only in:

- The wizard's local prompt during interactive use, OR
- GitHub Actions encrypted secrets (for the maintainer's own deploy workflow), OR
- The SOPS-encrypted master and gateway files, rendered on the VPS into `/srv/lifekit-secrets/stack.env` ([`secrets.md`](./secrets.md)).

They never appear in this repo. The `gitleaks` pre-commit hook enforces this.

## What we explicitly do NOT do

- **Run an orchestrator that supervises other orchestrators.** OpenClaw is the supervisor; swarm is a peer called by OpenClaw. We don't nest.
- **Use RAG / vector DB on the curated `~/.life/` layer.** It's small, structured, hot. Direct file reads outperform retrieval. Vector indexes belong on external unstructured corpora only (Tier 3 memory — see the [Lifekit framework docs](https://github.com/lifekit-hq/lifekit) for the three-tier model).
- **Tie identity to a specific channel.** Skills don't know whether they're called from Telegram or the OpenClaw web UI or voice. The same skill works on any surface OpenClaw routes to.
- **Bundle the data layer.** `lifekit-stack` deploys empty `/srv/life/` on the VPS; you bootstrap content via `lifekit init` or copy from your laptop.

## Further reading

- [`runbook.md`](./runbook.md) — operational: update, rollback, backup, debug.
- [`customizing-skills.md`](./customizing-skills.md) — how to write a parameterized workspace skill.
- [OpenClaw architecture](https://docs.openclaw.ai/) — the runtime gateway internals.
- [Lifekit framework](https://github.com/lifekit-hq/lifekit) — the Python framework + CLI + memory model.
