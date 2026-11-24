# lifekit-stack
A reference deployment of a personal-AI stack: bash bootstrap plus Docker Compose that put the OpenClaw gateway, an agent fleet, owner notifications, observability and sign-in on one VPS. The glossary covers the platform and fleet vocabulary a contributor meets in `docs/`, `compose/`, `scripts/` and `defaults/`.

## Language

**Box**:
The single VPS the whole stack runs on, treated as one shared platform for every product on it (this repo's, devclaw, finance-sentry, the dashboard). Alerting, the platform contract and the memory budgets are all "box-level".

**Compose project**:
One Docker Compose deployment unit with its own file and name: `compose` (the platform), `openclaw`, `identity`, `edge`, `reminder-bot`. Each deploys and recreates apart from the others.

**Platform contract**:
The deploy-time rule that every product on the box declares, in `lifekit.contract.*` labels, how it meets health, readiness, metrics, JSON logs with a trace id, edge and topics. A service is `v1` (opted in) or `none` (opted out); there are no waivers.
_Avoid_: guardrail 1 (the doc's own header calls the same thing that)

**Platform half / Host state**:
The OpenClaw config is split in two. The platform half (`compose/openclaw-gateway/platform.patch.json`) is in git and applied by `deploy.sh`; the personal half (agent entries, bindings, channels, auth profiles) is host state, hand-edited and shipped as operator steps in the PR.
_Avoid_: per-agent half (used as a synonym for the personal half in the runbook)

**OpenClaw gateway**:
The `openclaw-gateway` container: the runtime that owns channels, cron, skills and every agent. It is the `RuntimeGateway` adapter port's current adapter.

**Adapter port**:
A boundary interface the system owns so a third-party tool can be swapped: `RuntimeGateway`, `BuildEngine`, `Sandbox`, `LocalLLM`. `BuildEngine` and `Sandbox` are planned, not yet deployed; `LocalLLM` is Anthropic Haiku on the box. Nothing in the stack assumes OpenClaw or Anthropic forever.

**Kit**:
The general-purpose agent and front door: the default and system agent, the catch-all for Telegram traffic nothing else claims, and the dispatcher for domain work. Anything without a domain agent is Kit's.

**Domain agent**:
An agent that owns exactly one domain with its own workspace, memory, sessions and usually its own Telegram bot: `finance`, `career`, `learnclaw`. It earns an entry only with a contract in git, a route, and work that shows it is alive; otherwise the domain is a skill in Kit.

**Ledger**:
The finance domain agent (agent id `finance`). It holds no finance facts of its own; finance-sentry is the one store.
_Avoid_: finance agent (the id and the old docs use it for the same agent)

**Reader agent**:
The one agent that reads untrusted text (new mail and its attachments) and holds no write, send, web or delegation tool. No channel is bound to it; only its two cron jobs run it.

**Binding**:
The routing rule in `openclaw.json` that sends a channel (optionally an account or peer) to an agent; the most specific match wins. Changed with `openclaw agents bind` / `unbind`.

**Workspace skill**:
A Jinja2-templated, opt-in skill under `skills/` installed into the OpenClaw workspace. It wraps a backing CLI that owns its own data and is never vendored with personal values (`{{ user.* }}` only).

**Vault**:
The owner's plain-text memory store (`~/memory/`, on the box `/srv/memory`), mounted into the gateway and searchable by every agent. It is the single source of truth for durable knowledge; agents propose edits rather than writing there, except through skills and CLIs that own their data, and agent auto-memory is switched off.
_Avoid_: knowledge layer, `~/.life/` (older docs for the same store)

**Door**:
A narrow, scripted entry point through which work or a signal enters the stack with an envelope and returns a receipt at once: `ops-door`, `doc-door`, `vault door`, `study-door`, the video drop door.

**Envelope**:
A small typed payload a producer sends and a receiver validates. In notify-relay it is the JSON owner-notification (`level`, `source`, `subject`, `headline`); at a door it is the work-intake record (owner, class, acceptance, fingerprint).

**Receipt**:
The immediate answer a door returns for an envelope, saying what became of it (filed, counted onto a repeat, rejected).

**Fingerprint**:
The stable key a door derives per item (per alert, per problem) so a repeat counts onto one record instead of creating another.

**notify-relay**:
The only renderer of owner notifications. Producers `POST /notify` an envelope and it sends Telegram HTML; its `level` (`act`, `wait`, `good`, `info`) answers "do I act, now?".

**Decision**:
A typed ask that needs the owner (`docs/decision-contract.md`, with options and a recommended one). Open ones live in Kit's needs-you ledger (`needs-you/open.json`) and are asked with `ask_user` in the OpenClaw app, not Telegram.

**Fleet gate**:
The permission-engine hook that stops an agent at an action the owner has ruled stop-and-file and files the request instead of letting the agent ask in chat.

**Relay**:
A one-pipe forced-command bridge by which an agent passes the owner's words to the supervising firstmate's captain inbox and reads replies (`kit-relay`, `secondmate-relay`). The agent copies text; it never decides what to send.

**Shared job queue**:
One typed queue (`skills/job-queue`) for work that needs an egress the server lacks, such as the owner's PC; jobs wait and drain when the egress answers.

**Sign-in gate**:
The tailnet gate (`edge` project: traefik forward-auth plus oauth2-proxy) in front of the dashboard, the devclaw console and the OpenClaw dashboard, with Logto as the identity provider.

**Alert rule**:
A provisioned Grafana rule in `compose/observability/grafana/provisioning/alerting/`. Rules are files, reloaded by `deploy.sh`; `severity: critical` ones page Telegram directly.

**Idle lane**:
A free worker lane sitting beside ready work in a supervisor's home; `scripts/idle-lanes` escalates it at timed thresholds (a note, then an `act` envelope).

## Relationships
- A **Compose project** holds services; every product service in it carries a **Platform contract** label (`v1` or `none`) or `deploy.sh` stops before `up`.
- The **OpenClaw gateway** hosts **Kit** and every **Domain agent**; a **Binding** routes each channel message to exactly one agent, falling through to **Kit**.
- A **Domain agent** needs a contract in git, a **Binding** (or subagent role under **Kit**) and live work; failing that the domain is a **Workspace skill** in **Kit**.
- The **Reader agent** is, by design, the only agent that reads untrusted mail and documents, and it can only report what it read.
- A **Door** takes an **Envelope** and returns a **Receipt**; repeats of the same **Fingerprint** count onto one record.
- Producers reach the owner only through **notify-relay**; an owner-blocking ask becomes a **Decision** in Kit's needs-you ledger.
- The **Platform half** of the OpenClaw config deploys on merge; the personal half is **Host state** shipped as operator steps.
- A **Relay** carries the owner's words from **Kit** to the firstmate's captain inbox.

## Flagged ambiguities
- "Contract" names three things: the **Platform contract** (`docs/platform-contract.md`), an agent's contract in git (`docs/openclaw-agent-design.md`, "A contract in git"), and the decision contract (`docs/decision-contract.md`).
- "Envelope" names two payloads: the notification envelope (`docs/message-format.md`, "The envelope (v1)") and the door intake envelope (`docs/runbook.md`, "Envelope and receipt").
- The finance agent is both **Ledger** and `finance` (`docs/openclaw-agent-design.md`, "Finance data and Ledger": "Ledger (agent id `finance`)").
- The supervising agent is "second mate" (`docs/runbook.md`, "Asking another agent"), "secondmate" (skill `skills/secondmate-relay`, `kit-relay` script) and "firstmate"/"captain inbox" (`docs/runbook.md`); the relation between them is not defined in this repo.
- The knowledge store is `~/.life/` / "knowledge layer" in `docs/architecture.md` but "the vault" (`~/memory/`) in `docs/runbook.md` and AGENTS.md.
- "Tier 2" appears undefined in README's `ops-door` row; it names the escalation call the `ops-door-escalate` timer files through `fleet-policy decide`.

