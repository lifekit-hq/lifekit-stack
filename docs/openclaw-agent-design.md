# OpenClaw Agent Design

How lifekit-stack shapes its OpenClaw agents: one general-purpose agent, a small set of domain agents, and the rules that keep each one reviewable.

This page is the design, not a mirror of live state. The live fleet is host state: `agents.entries`, `bindings` and `channels` in `/srv/openclaw/config/openclaw.json`, and the cron jobs in the gateway's own store. Read those for what is running today (`openclaw agents list --bindings`, `openclaw cron list --all`). History: rewritten 2026-05-25 to a single Kit after a failed multi-agent attempt (see [the lesson](#the-2026-05-25-lesson-still-holds)); rewritten again 2026-09-28 for the fleet reshape that retired the nameless `fable` and `main` agents and gave `career` and `social` real work.

---

## Shape: kit plus domain agents

- **kit** is the general-purpose agent: the default and system agent, the catch-all for Telegram traffic nothing else claims, and the dispatcher for domain work that arrives there. Anything without a domain agent is kit's, including dev work (which it hands to DevClaw through the intake doorway).
- **Domain agents** each own one domain, with their own workspace, memory, sessions and, where the owner talks to them directly, their own Telegram bot. After the 2026-09 reshape: `health`, `finance`, `learning`, `devclaw`, `career` and `social`.
- **One gateway, one config.** Every agent runs in the same `openclaw-gateway` container on the `claude-cli` runtime, authenticated by the owner's Claude subscription. No API keys ([[pro-subscription-is-the-design]] in memory); a domain agent never brings a paid service with it.

| Agent | Domain | How work reaches it |
| --- | --- | --- |
| kit | Everything without a domain agent; dispatch | Telegram default account plus the `telegram:*` catch-all |
| health | Fitness, nutrition, daily state | kit subagent only, by design |
| finance | Investing, finance-sentry companion | Own Telegram account, inbound hook |
| learning | Reading and learning | Own Telegram account |
| devclaw | Dev harness, repo operations | Own Telegram account |
| career | Career plan, job search, LinkedIn as a job-search tool (read-only, drafts-only) | Own Telegram account |
| social | LinkedIn and TikTok content, the posting calendar (drafts-only) | Own Telegram account |

The table is the intended route; `openclaw agents list --bindings` is the truth. At the 2026-09-28 inventory the `learning` bot had no binding of its own, so its traffic fell through to kit.

### What makes an agent earn its entry

The 2026-09-28 inventory found four of nine agents with no real work: two with no identity or contract at all, two fully provisioned but reached by nothing. An agent entry exists only when it has all of:

1. **A contract in git** - `defaults/agents/<id>/workspace/` with `IDENTITY.md` (name, emoji, one-line domain), `SOUL.md` (persona) and `AGENTS.md` (domain, what it hands back, sources of truth, hard rules).
2. **A route** - a binding that sends real traffic to it, or a documented subagent role under kit.
3. **Work that shows it is alive** - at least one delivering automation with failure delivery, or an interactive channel the owner actually uses. An agent whose only automation reviews its own skill list is idle, and nothing would notice it breaking.

A domain that fails the test is a skill in kit, not an agent.

---

## Where things live

| What | Where |
| --- | --- |
| Agent entries (identity, model, skills allowlist, tools, subagents) | `agents.entries.<id>` in the live `openclaw.json` (host state) |
| Routing | top-level `bindings` in the live `openclaw.json`; change with `openclaw agents bind` / `unbind` |
| Telegram bots | `channels.telegram.accounts.<id>` in the live `openclaw.json` |
| Cron jobs | the gateway's store, not `openclaw.json`; `openclaw cron add` / `edit` / `rm`. Git-side declarations where they exist: `scripts/ensure-*.sh` and `docs/runbook.md` |
| Platform keys (logging, diagnostics, plugins, memory search, heartbeat, hooks) | `compose/openclaw-gateway/platform.patch.json`, applied by `deploy.sh` |
| Workspace templates | `defaults/agents/<id>/workspace/`, copied into the agent's workspace by hand (resolve the path with `openclaw agents list --json`; container paths under `/home/node/.openclaw/` map to `/srv/openclaw/config/` on the host) |
| Skills | not vendored; see each workspace's `skills/README.md` |

The platform half is in git and deployed on merge; the per-agent half (entries, bindings, channels, crons, auth) is host state and ships as operator steps in the PR that changes it. See `docs/runbook.md`, "Host-config patch".

### Routing

A binding matches a channel and optionally an account or peer; the most specific match wins, so `telegram:career` beats kit's `telegram:*`. A Telegram bot with no binding of its own falls through to kit, so a domain agent reached by its own bot needs its own binding. Retiring an agent means removing its bindings and disabling or rebinding its bot, not only its entry.

### Decisions waiting on the owner

Decisions go to the OpenClaw app, not Telegram. kit keeps the one ledger of open decisions in its workspace and asks each one with `ask_user` in its main Control UI chat. A decision that comes up anywhere else is recorded and handed to that chat. A daily digest of what is still open posts there too, and stays silent when nothing is. See kit's `AGENTS.md` ("Needs you") and `docs/runbook.md` ("Kit's needs-you decisions and the daily digest").

### Workspaces and memory

Each agent's workspace holds its bootstrap files (`AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `MEMORY.md`), daily notes under `memory/` (for Kit, Ledger and devclaw that directory is the vault's `agents/<id>/memory/`, bind-mounted - `docs/runbook.md` "Agent memory in the vault"), and its installed skills. The vault is mounted read-only in spirit for every agent (`~/memory`, searchable through `memory.search`); agents propose vault edits rather than writing there, except through skills and CLIs that own their data.

---

## Boundary rules: agent, skill, or MCP

| Question | If yes | If no |
| --- | --- | --- |
| Is it a separate domain the owner talks to directly, with its own voice, memory and automation? | Domain agent (and it must pass the test above) | Skill in kit or in an existing domain agent |
| Does it have a real backing CLI with its own data? | Workspace skill; the CLI owns the data | Direct handling |
| Does it outlive one chat turn (multi-hour, callback-driven) or already exist as an MCP server? | MCP (devclaw, google-workspace) | Skill or direct |

What this rules out:

- An agent per domain by default. `health` stays a kit subagent because the owner never talks to it directly.
- An agent with no written purpose. If no one can say what it is for, it is retired, not tolerated.
- Wrapping a local CLI in an MCP server just to be consistent. Skills are the consistency.

---

## The 2026-05-25 lesson still holds

The first v2 design was a multi-agent modular monolith: an orchestrator dispatching to domain agents through `agentToAgent`, with per-agent tool restrictions as the isolation. Under `claude-cli` it failed: per-agent `tools.allow` / `tools.deny` were not enforced (the runtime spawned Claude Code with its full default toolbox), `agentToAgent` never appeared as a callable tool, and the orchestrator twice wrote fabricated workout logs into the vault instead of delegating.

The current shape does not repeat that design:

- **Routing is the gateway's job, not the model's.** Work reaches a domain agent through a binding or an explicit kit subagent call, not through an orchestrator deciding to delegate.
- **The separation that holds is workspace, memory, sessions and channel.** Treat per-agent tool policy (including `tools.exec.mode`) as advisory under `claude-cli` until it is re-verified; it is not an isolation boundary. The hard rules in each `AGENTS.md` and the owner's review of drafts are.
- **Domain agents that act outward stay drafts-only.** `career` and `social` never post, message or submit on the owner's behalf.

---

## DevClaw - the autonomous coding boundary

DevClaw is the one out-of-process runtime for agent work: an MCP server that runs autonomous coding tasks through [OpenHands](https://github.com/All-Hands-AI/OpenHands) in a sandbox and reports back through `notify-relay`. kit reaches it through the intake doorway (`file_intake`, then dispatch on the owner's go); the `devclaw` agent entry is the dev-harness domain agent with its own daily brief (`scripts/ensure-morning-brief.sh`). See [DevClaw architecture v2](https://github.com/lifekit-hq/devclaw/blob/main/docs/architecture-v2.md).

---

## Further reading

- `docs/runbook.md` - host-config patches, cron recreation, backups
- `defaults/agents/*/workspace/` - the agent contracts
- [OpenClaw multi-agent routing](https://docs.openclaw.ai/concepts/multi-agent) and [channel routing](https://docs.openclaw.ai/channels/channel-routing)
- [OpenClaw skills](https://docs.openclaw.ai/tools/skills) and [system prompt](https://docs.openclaw.ai/concepts/system-prompt)
