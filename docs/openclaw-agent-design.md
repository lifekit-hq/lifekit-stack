# OpenClaw Agent Design

How lifekit-stack shapes its OpenClaw agents: one general-purpose agent, a small set of domain agents, and the rules that keep each one reviewable.

This page is the design, not a mirror of live state. The live fleet is host state: `agents.entries`, `bindings` and `channels` in `/srv/openclaw/config/openclaw.json`, and the cron jobs in the gateway's own store. Read those for what is running today (`openclaw agents list --bindings`, `openclaw cron list --all`). History: rewritten 2026-05-25 to a single Kit after a failed multi-agent attempt (see [the lesson](#the-2026-05-25-lesson-still-holds)); rewritten again 2026-09-28 for the fleet reshape that retired the nameless `fable` and `main` agents and gave `career` and `social` real work; updated 2026-10-07 when kit became the front door (health folded into kit, `social` and `learning` retired, `career` paused); updated 2026-10-28 when the learning loop came back as `learnclaw`.

---

## Shape: kit plus domain agents

- **kit** is the front door and the general-purpose agent: the default and system agent, the catch-all for Telegram traffic nothing else claims, and the dispatcher for domain work that arrives there. Anything without a domain agent is kit's, including food and workouts (it runs the `nutrition-claw`, `workout-claw` and `life-state` CLIs itself, with targets read from the vault's `domains/health.md`) and dev work (which it hands off as a GitHub issue, see [DevClaw - retired](#devclaw---retired)).
- **Domain agents** each own one domain, with their own workspace, memory, sessions and, where the owner talks to them directly, their own Telegram bot. Since 2026-10-09: `finance` and `career`; since 2026-10-28 also `learnclaw`.
- **One gateway, one config.** Every agent runs in the same `openclaw-gateway` container on the `openclaw` agent runtime (`agentRuntime.id`), authenticated by the owner's Claude subscription. No API keys ([[pro-subscription-is-the-design]] in memory); a domain agent never brings a paid service with it.

| Agent | Domain | How work reaches it |
| --- | --- | --- |
| kit | Everything without a domain agent, including food, workouts and daily state; dispatch | Telegram default account plus the `telegram:*` catch-all |
| finance | Investing, finance-sentry companion | Own Telegram account, inbound hook |
| career | Career plan, job search, LinkedIn as a job-search tool (read-only, drafts-only). Paused: `career-weekly` is disabled until the job hunt restarts; the agent and its bot stay | Own Telegram account |
| learnclaw | The learning loop: the artefact record on the vault's `domains/learning.md`, predict-read-build-apply-post, the grilling. It reviews the artefact, never writes it | Control UI chat, and a subagent role under kit; no Telegram account and no cron in v0 (`docs/runbook.md` "learnclaw (the learning loop)") |

The table is the intended route; `openclaw agents list --bindings` is the truth.

Retired on 2026-10-07: `health` (its food and workout duties and its three disabled nudge crons moved to kit; it never had a channel), `social` and `learning` (deleted with `openclaw agents delete`, their Telegram accounts `social` and `reading` disabled rather than left to fall through to kit). Retired on 2026-10-09: `devclaw` (its binding, Telegram account, MCP server and morning-brief cron removed; build work became a GitHub issue that kit files, see [DevClaw - retired](#devclaw---retired)). Re-adding a domain means passing the test below again: the learning loop did on 2026-10-28 as `learnclaw`, with a contract in git, the Control UI chat and kit's subagent route, and the artefact record as its work. The old `learning` agent's state and the disabled `reading` bot are not reused.

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
| Workspace `MEMORY.md` / `USER.md` | the vault pointers in `defaults/workspace-pointers/`, written by `deploy.sh` |
| Workspace templates | `defaults/agents/<id>/workspace/`, copied into the agent's workspace by hand, except learnclaw's, which the deploy installs (`scripts/ensure-learnclaw.sh`) (resolve the path with `openclaw agents list --json`; container paths under `/home/node/.openclaw/` map to `/srv/openclaw/config/` on the host) |
| Skills | not vendored; see each workspace's `skills/README.md` |

The platform half is in git and deployed on merge; the per-agent half (entries, bindings, channels, crons, auth) is host state and ships as operator steps in the PR that changes it. See `docs/runbook.md`, "Host-config patch".

### Routing

A binding matches a channel and optionally an account or peer; the most specific match wins, so `telegram:career` beats kit's `telegram:*`. A Telegram bot with no binding of its own falls through to kit, so a domain agent reached by its own bot needs its own binding. Retiring an agent means removing its bindings and disabling or rebinding its bot, not only its entry. Disabling a Telegram bot also drops its `botToken` ref from the platform file (`docs/runbook.md` "Telegram bot tokens: enabled accounts only").

### Decisions waiting on the owner

Decisions go to the OpenClaw app, not Telegram. kit keeps the one ledger of open decisions in its workspace and asks each one with `ask_user` in its main Control UI chat. A decision that comes up anywhere else is recorded and handed to that chat. A digest of the open decisions, the fleet and the new mail posts there too, twice daily (09:00 and 17:00); the mail is read beforehand by a separate isolated cron run allowed only the two Gmail read tools, and the digest copies its summary; when nothing is open and no mail needs you it says "Nothing needs you today." See kit's `AGENTS.md` ("Needs you") and `docs/runbook.md` ("Kit's needs-you decisions and the daily digest").

### Workspaces and memory

Each agent's workspace holds its bootstrap files (`AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` and `MEMORY.md`, which are pointers to the vault that every deploy rewrites for kit, career, finance and learnclaw - `docs/runbook.md` "Workspace memory files are pointers"), daily notes under `memory/` (for Kit and Ledger that directory is the vault's `agents/<id>/memory/`, bind-mounted - `docs/runbook.md` "Agent memory in the vault"), and its installed skills. The vault is mounted read-only in spirit for every agent (`~/memory`, searchable through `memory.search`); agents propose vault edits rather than writing there, except through skills and CLIs that own their data.

Claude Code's own auto-memory (`~/.claude/projects/<workspace>/memory/`) is off for every agent: it is per-machine, invisible to the vault and to the projects that own the data, and it drifts. The OpenClaw image locks it off with a root-owned managed settings file that a workspace settings file cannot override (`docs/runbook.md` "lifekit Claude Code home").

### Finance data and Ledger

Ledger (agent id `finance`) holds no finance facts of its own; finance-sentry is the one store, and the flow is built so that stays true without cleanups:

- **Reads** go to finance-sentry over its MCP server (`finance-sentry-mcp` on the shared network), including the framework primer that is Ledger's only big-picture source (`docs/runbook.md` "Ledger persona from finance-sentry").
- **Changes** to finance data are proposals the owner confirms. finance-sentry refuses its research write tools and watchlist add/remove to service tokens such as Ledger's ([finance-sentry#1164](https://github.com/lifekit-hq/finance-sentry/pull/1164)); that boundary is enforced in finance-sentry, not here. The one direct write kept open is `decide_recommendation`, for a verdict the owner gives Ledger in chat.
- **Nowhere else to keep facts.** Auto-memory is locked off (above), and the workspace holds only bootstrap files, OpenClaw state, inbound media, vault daily notes, skills and Ledger's MCP helpers.
- **A check, not a cleanup.** `lifekit-ledger-drift.timer` reports any file in Ledger's auto-memory dir, or in its workspace outside that allowlist, as the *Ledger finance drift* alert: to the owner's Telegram and, through `alert-inbox`, to main. It never deletes or moves a file; the owner decides (`docs/runbook.md` "Ledger finance drift").

---

## Boundary rules: agent, skill, or MCP

| Question | If yes | If no |
| --- | --- | --- |
| Is it a separate domain the owner talks to directly, with its own voice, memory and automation? | Domain agent (and it must pass the test above) | Skill in kit or in an existing domain agent |
| Does it have a real backing CLI with its own data? | Workspace skill; the CLI owns the data | Direct handling |
| Does it outlive one chat turn (multi-hour, callback-driven) or already exist as an MCP server? | MCP (google-workspace, finance-sentry) | Skill or direct |

What this rules out:

- An agent per domain by default. Health was a kit subagent the owner never talked to directly, so on 2026-10-07 it became kit's own skills instead.
- An agent with no written purpose. If no one can say what it is for, it is retired, not tolerated.
- Wrapping a local CLI in an MCP server just to be consistent. Skills are the consistency.

---

## The 2026-05-25 lesson still holds

The first v2 design was a multi-agent modular monolith: an orchestrator dispatching to domain agents through `agentToAgent`, with per-agent tool restrictions as the isolation. Under `claude-cli` it failed: per-agent `tools.allow` / `tools.deny` were not enforced (the runtime spawned Claude Code with its full default toolbox), `agentToAgent` never appeared as a callable tool, and the orchestrator twice wrote fabricated workout logs into the vault instead of delegating.

The current shape does not repeat that design:

- **Routing is the gateway's job, not the model's.** Work reaches a domain agent through a binding or an explicit kit subagent call, not through an orchestrator deciding to delegate.
- **The separation that holds is workspace, memory, sessions and channel.** Treat per-agent tool policy (including `tools.exec.mode`) as advisory under `claude-cli` until it is re-verified; it is not an isolation boundary. Under the `openclaw` runtime the agents use now, a cron job's own tool allow-list (`--tools`, stored as `payload.toolsAllow`) is enforced per run; the needs-you mail reader relies on it. The hard rules in each `AGENTS.md` and the owner's review of drafts are.
- **Domain agents that act outward stay drafts-only.** `career` never posts, messages or submits on the owner's behalf.

---

## DevClaw - retired

The `devclaw` agent and its `devclaw` MCP server entry retired on 2026-10-09. Autonomous build work is a `kit-relay` note: kit (or a specialist such as Ledger) files no issue itself; the ask reaches firstmate through `secondmate-relay`, and firstmate files an issue only when the ask is work. The devclaw-mcp service and its compose project belong to the devclaw repo, not to this fleet; no agent calls it.

The agent entry and its system-owned `skill-collection-review-devclaw` cron are removed with `openclaw agents delete devclaw --force` once the deploy that drops the devclaw memory bind mount from the gateway has run (the delete moves the agent's directories to the trash, and the mount would be in that path). `config patch`/`config unset` refuse to drop a roster entry.

---

## Further reading

- `docs/runbook.md` - host-config patches, cron recreation, backups
- `defaults/agents/*/workspace/` - the agent contracts
- [OpenClaw multi-agent routing](https://docs.openclaw.ai/concepts/multi-agent) and [channel routing](https://docs.openclaw.ai/channels/channel-routing)
- [OpenClaw skills](https://docs.openclaw.ai/tools/skills) and [system prompt](https://docs.openclaw.ai/concepts/system-prompt)
