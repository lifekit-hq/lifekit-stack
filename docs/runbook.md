# Runbook

Operational guide. How to update, roll back, back up, and recover when things break.

> **Status:** pre-release. Some procedures are aspirational until v0.1.0 ships.

## Updating OpenClaw

The OpenClaw version is the `FROM` tag in `compose/openclaw-gateway/Dockerfile`
and nothing else. Dependabot (`.github/dependabot.yml`) watches that tag on
ghcr and opens a `chore(openclaw): bump ...` PR on Mondays, after a 7-day
cooldown so upstream's own hotfix cycle has landed. The loop:

1. Dependabot opens the PR. CI lint runs on it.
2. Skim the upstream release notes it links. Merge (squash).
3. The main-push deploy job runs `scripts/deploy.sh`: builds the new image,
   and because the version changed: tags the running image as
   `lifekit-openclaw:prev` (the [one-rollback rule](#the-one-rollback-rule)),
   stops the gateway, writes a config-only backup to
   `/srv/openclaw/config/backups/`, runs
   `doctor --fix --non-interactive` with the new image against the live
   state (config rewrite, SQLite migrations, official-plugin re-pin), then
   recreates the gateway and runs `openclaw doctor`, `health`,
   `channels status`. Read that log.
4. deploy.sh then runs one real agent turn for `kit` (`SMOKE_AGENTS`
   overrides) in a throwaway `deploy-smoke-<agent>` session - one turn per
   runtime, and every agent is Claude-primary since the 2026-09-16 all-agents
   switch (see the smoke-turn comment in `scripts/deploy-openclaw.sh`; the live
   `agents` config sets `agentRuntime.id` `openclaw` on each model entry);
   OpenAI/codex is not primary for any agent right now. `fable`, the second
   smoke agent, was retired in the 2026-09 fleet reshape. A runtime error fails the run; auth and quota
   errors (expired OAuth, weekly cap) only warn. On a version change it also
   refuses to migrate over files not owned by uid 1000 and asserts every
   official plugin matches the core version afterwards (one update attempt,
   then a red run). First real Telegram message + first cron run remain the
   final word.

Do NOT use the dashboard's "Update now" or `openclaw update` inside the
container. The install is an npm package baked into the image (no git
checkout, so the UI refuses it), and anything it did install would be gone
on the next container recreate while the Dockerfile still says the old
version. The image is the unit of deployment.

To force a version by hand (skip the Dependabot wait): edit the `FROM` tag,
open a PR, merge. Same path, same checks.

The agent CLIs (`claude`, `clawhub`, `summarize`) are baked into the same
image at the exact versions in `compose/openclaw-gateway/package.json`;
Dependabot's npm ecosystem opens their bump PRs. Only those top-level
versions are pinned, not their transitive trees. The entrypoint installs
only remain as a fallback when a command is missing. Skill CLIs stay
entrypoint-installed (bind-mounted host checkouts).

Before a jump across several months of releases, take a full verified state
backup first (the migrations are one-way; an older gateway cannot read the
migrated state, so the image tags alone do not roll back). Roughly 6 GB
compressed, 20+ minutes, the gateway can stay up:

```bash
ssh <your-vps-tailscale-name>
sudo docker run -d --name openclaw-backup --entrypoint openclaw \
  -e HOME=/home/node -e OPENCLAW_STATE_DIR=/home/node/.openclaw \
  -e OPENCLAW_CONFIG_PATH=/home/node/.openclaw/openclaw.json \
  --env-file /srv/lifekit-secrets/stack.env \
  -v /srv/openclaw/config:/home/node/.openclaw \
  -v /srv/openclaw/workspace:/home/node/.openclaw/workspace \
  -v /srv/openclaw/backups:/backups \
  lifekit-openclaw:local backup create --output /backups --verify --no-include-workspace
docker logs -f openclaw-backup     # ends with {"ok": true, ...}; then docker rm openclaw-backup
```

Run it detached (`-d`) as above: an SSH session that drops mid-archive
takes a foreground `docker run` with it. Everything under
`/srv/openclaw/config` must be owned by uid 1000 (the container user);
root-owned `.bak-*` leftovers from hand edits make the archive fail with
EACCES and stall the Codex session-sidecar migration. Fix with
`sudo find /srv/openclaw/config ! -user lifekit -exec chown lifekit:lifekit {} +`.

To rehearse a bump without touching live state, run
`scripts/rehearse-openclaw-bump.sh` as the `lifekit` account (`--help` lists
the flags). It builds the image as `lifekit-openclaw:rehearse` (never `:local`
or `:prev`), copies the state and workspace under `~/rehearsal/`, runs
`doctor --fix` twice plus the SQLite dry-run and a `doctor --json` lint on the
copy, checks plugin versions against core, and prints a summary of key paths and
counts with no config values. `--lint-only` is the quick read-only pass against
live state. The copy skips the gateway's `tmp/` and cache dirs (plugin-build
scratch that is deleted while it copies) and treats rsync exit 24 (files
vanished mid-copy) as a warning; any other rsync failure is red. The full
doctor logs stay in the copy, which a red run keeps (pruned after 7 days) and
a green run removes. If a green run cannot remove everything, it prints a
warning and keeps the verdict; delete the leftover copy by hand.

The lint verdict keys on each finding's `checkId`. Any warning or error is red
except three rehearsal-only classes, which the script tolerates: MCP servers
that can't be resolved (the container is off the compose network), Claude CLI
"not logged in" (no Claude credentials are mounted), and the gateway's "lan"
bind (by design: `--bind lan` inside the container, loopback-only publish on
the host). `doctor --fix` writes TOOLS.md migration backups under
`/srv/openclaw/config/backups/`, so that path must be a directory: a stray
file there fails the migration with ENOTDIR (the 2026-09-29 cleanup). On
2026.9.5 the SQLite dry-run exits 1 on every copy ("archived registry identity
changed"), because a copy can't keep the archived registry's inode. The live
record is intact, and 2026.9.6 reports this as an advisory warning.

Skill/vault/compose changes without an OpenClaw bump deploy the same way:
merge to `main`, CI deploys. Direct on the VPS only when CI is down:

```bash
ssh <your-vps-tailscale-name>
cd /srv/lifekit-stack && git pull
bash scripts/deploy.sh
```

### The diagnostics-prometheus plugin

`@openclaw/diagnostics-prometheus` is an official plugin installed into the
state dir, at the core's own version, once per host:

```bash
V="$(docker exec openclaw-openclaw-gateway-1 openclaw --version | awk '{print $2}')"
docker exec openclaw-openclaw-gateway-1 openclaw plugins install "@openclaw/diagnostics-prometheus@${V}"
docker exec openclaw-openclaw-gateway-1 openclaw plugins inspect diagnostics-prometheus | grep Trust
```

`Trust` must read `reason=trusted-official`. Do not bake it into the image or
load it through `plugins.load.paths`: OpenClaw only hands the internal
diagnostics feed to bundled or trusted-official installs, so such a copy
loads, answers the scrape with HTTP 200 and an empty body, and no alert can
fire. Being a state-dir install, the deploy's plugin-parity check re-pins it
on a version bump. The host-config patch below enables it.

Prometheus scrapes it as job `openclaw` with the gateway token (compose
secret `openclaw_gateway_token`, from `OPENCLAW_GATEWAY_TOKEN` in the env
file); rotating that token means recreating `prometheus` too. Check it with
`curl -s localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.labels.job=="openclaw") | .health'`.

### Host-config patch: the platform half is in git, the per-agent half is not

`/srv/openclaw/config/openclaw.json` has two halves. The platform keys -
`logging.consoleStyle`, `diagnostics.otel`, the `diagnostics-prometheus` and
`diagnostics-otel` plugin enables, memory-core dreaming off, `gateway.auth.rateLimit`, `memory.search`
(`enabled` + `extraPaths`), `agents.defaults.heartbeat.every`, the inbound
`hooks` block, the finance agent's heartbeat and
`skills.workshop.approvalPolicy` - live in
`compose/openclaw-gateway/platform.patch.json`. On every OpenClaw deploy (see
[Path-gated OpenClaw deploys](#path-gated-openclaw-deploys)) `deploy.sh`
compares that file with the live config, applies it with
`openclaw config patch` only when a key differs, and force-recreates the
gateway only when the CLI's apply hint says the changed keys need it
(`plugins.entries` and `memory.search.extraPaths` do; the rest hot-reloads
under the default `gateway.reload` hybrid mode). A deploy that finds nothing
to change writes nothing and leaves the gateway alone. A new platform key goes
in that file, never in a PR body; objects merge and scalars replace
(`openclaw config patch --help`), and the file is strict JSON so the deploy
can read it with stdlib Python. Check new keys against the installed schema
(`openclaw config schema`).

#### Skill Workshop applies wait for the owner (`skills.workshop.approvalPolicy`)

Unset, `skills.workshop.approvalPolicy` is `"auto"`: an agent that calls
`apply`, `reject` or `quarantine` on its own Skill Workshop proposal goes ahead
with no prompt, so an agent could rewrite its own skills. The platform file
sets `"pending"` (the gateway's `docs/tools/skills-config.md`: "`pending`
requires operator approval"), so an agent-initiated apply waits for the owner.
`autonomous.mode` (off on this host) stays host state. `skills` keys reload as
`none` in the gateway's reload plan: read at use, no restart.

#### Vault search for every agent (`memory.search`, in the platform file since 2026-09-23)

Retiring the memory-wiki plugin also retired the only search the agents had
over the vault. `memory.search.extraPaths` is memory-core's own knob for
indexing directories or files outside an agent's workspace, and the platform
file points it at the vault's existing gateway mount
(`compose/openclaw/docker-compose.yml`'s `/home/node/memory` bind for
`openclaw-gateway`), so every agent's `memory_search` covers vault pages
without a second indexer. The same block sets `memory.search.enabled: true`:
the deploy switches memory search on for every agent. The live config had
carried an explicit `enabled: false` since the migration; the 2026-09-18
decision supersedes that, and because `openclaw config patch` merges objects
the platform file has to flip the key itself or the host's `false` would
survive every deploy. To switch search off again, set `enabled` to `false` in
the platform file and let the next deploy converge it - a hand edit on the
host is undone by the following deploy. The index this key builds is a
derived layer: memory-core writes into each agent's own sqlite store under
its workspace, never under the vault, so the index writes nothing there even
though the gateway's `/home/node/memory` bind itself stays read-write for
cron jobs, skills, and agents (decision record: vault `system/proposals.md`,
`2026-09-18-memory-engines-as-derived-layers`). Applying it needs the gateway
restart `deploy.sh` already triggers for this key (confirmed against the
2026.9.5 schema: this key's apply hint is "Restart the gateway to apply.",
same as `plugins.entries`). After a deploy that changes this key, the owner
verifies on the host: one real agent turn whose tool summary shows a
`memory_search` hit on a vault path, and that the vault's `git status` (or
mirror equivalent) stays clean afterward - the first index over a vault-sized
directory takes a noticeable but bounded amount of time on first run, then
stays incremental.

#### Dreaming off (`plugins.entries.memory-core.config.dreaming.enabled`)

memory-core's dreaming is an upstream feature, on by default, that promotes
agent notes into the workspace `MEMORY.md` on a managed weekly cron ("Memory
Dreaming Promotion"). The vault is the one memory store, and `MEMORY.md` holds
a pointer to it (see [Workspace memory files are pointers](#workspace-memory-files-are-pointers)),
so the platform file sets `enabled: false`. The key is under `plugins.entries`,
so applying it recreates the gateway, and the restart's cron reconcile removes
the managed promotion job (the gateway's `docs/concepts/dreaming.md`: disabling
removes the explicitly declared jobs). The `timezone` and `frequency` already
in the live config stay, because objects merge. Turning dreaming back on is a
platform-file change, never a host edit.

#### Provider plugin allowlist (`plugins.allow`, lifekit-stack#175)

OpenClaw loads every bundled plugin that is enabled by default. That includes
more than two dozen model-provider plugins that nothing here routes to.
`plugins.allow` in the platform file lists the only plugin ids that load:

- **The routed providers.**
  - `anthropic`: every agent's model runs through the Anthropic provider.
  - `openai`: the openai auth profile.
  - `codex`: the agent runtime that `openai` runs on.
- **The non-provider plugins the gateway already runs.** These include
  `telegram`, `memory-core`, both diagnostics exporters, `browser` and
  `github`.

A new upstream default-on plugin stays off until someone adds it here. On
2026.9.8 that is `kie`. The rehearsal on a copy of the live config applied the
key with "Change will apply without restarting the gateway" and stored it
exactly as written, so a later deploy finds nothing to change.

To add a plugin, put its id in the list in the same PR that routes to it, or
that enables it under `plugins.entries`. An entry enabled there but missing
from `allow` never loads, and `scripts/tests/test_platform_patch.py` fails on
it.

#### Inbound hooks and the finance pulse (in the platform file since 2026-09-18)

The same file turns on OpenClaw's inbound HTTP hooks for one caller and one
agent, and gives the finance agent a heartbeat:

- `hooks`: enabled, `allowedAgentIds: ["finance"]`, caller-supplied session
  keys off, and one mapping - `POST <hook path>/finance-sentry` runs the
  finance agent in an isolated session and delivers to its Telegram chat. The
  template interpolates `kind` and `eventId` only: the push carries
  identifiers, the agent reads the detail back through its MCP tools. The
  template also tells the agent to end its message with the absolute `appUrl`
  that `get_pending_companion_events` returns per event (one bare link per
  line, none for an event without one, never a built or guessed link), so the
  Telegram message is clickable into the app.
- `agents.entries.finance.heartbeat`: **retired 2026-09-26, see the fs-685
  note below** - the key is now `{"every": "0m"}`, same shape as
  `agents.defaults.heartbeat`, so the finance agent no longer ticks on the
  heartbeat lane at all. It stays the one `agents.entries` key in the
  platform file; every other per-agent key stays host state.
- **No value is in git or in `openclaw.json`.** The file holds the literal
  references `${OPENCLAW_HOOK_TOKEN}`, `${OPENCLAW_HOOK_PATH}` and
  `${OPENCLAW_FINANCE_CHAT}`; OpenClaw resolves them from the container
  environment, which compose fills from `/srv/lifekit-secrets/stack.env` (see
  `.env.example`). `config patch` writes the reference back verbatim, so the
  deploy's compare sees equal strings and stays idempotent. The hook token is
  its own secret, never the gateway token (the gateway warns at startup if
  they match).
- **No new listener.** Hooks are routes on the gateway's existing 18789
  server: published on `127.0.0.1` only, reachable over the tailnet and from
  containers on the gateway's docker networks (that is how finance-sentry
  calls it), never from the public interface.
- **Order on a host that does not have them yet:** put the three variables in
  the env file first, then deploy. While one is missing or empty the deploy's
  dry run rejects the patch (`SecretRef assignment(s) could not be
  resolved`), nothing is written, the gateway stays as it was and the deploy
  ends red. After hooks are on, do not remove the token: a gateway with
  `hooks.enabled` and no token refuses to start. Rotate by changing the value
  in the env file (and in the caller's) and recreating the gateway.
- These keys hot-reload (`Change will apply without restarting the gateway.`);
  the patch step does not recreate the gateway for them. Adding the variables
  to the compose `environment` does change the service definition, so the
  deploy that first carries them recreates the gateway once in `up -d`.

The pulse's checklist is not config. It is the `payload.message` of the
`ledger-pulse` cron row: an `agentTurn` run receives only that message (the
per-job scratch, and the workspace `HEARTBEAT.md`, which is a no-op in
2026.9.4, never reach it; see "The finance heartbeat, retired for an isolated
cron job" below for why the pulse is a cron row and not the heartbeat lane).
`scripts/ensure-finance-pulse.sh` declares it from `scripts/finance-pulse.md`,
the same way `scripts/ensure-morning-brief.sh` declares its cron: the message
is a short contract preamble (`NO_REPLY` or exactly one message) followed by
the checklist verbatim. Run it on the host after the `ledger-pulse` row has
been created on the gateway; it is idempotent and rewrites the message with
`openclaw cron edit --message` only when it differs. The edit hot-reloads and
restarts nothing. The checklist's closing `Links:` paragraph has the pulse end
a sent message with the matching app page as a plain URL (`/events`,
`/alerts`, or the single event's own `appUrl`; none for the quiet-week digest
or a `NO_REPLY`); the app origin is not in git, so the paragraph tells the
agent to take it from a returned `appUrl`.

A pulse whose message carries no checklist still runs the model (scratch does
not gate an agentTurn run), but only calls the companion pull by chance - the
failure mode of 2026-09-26 to 2026-10-05, when a checklist seeded into the
scratch was never shown to the model and companion events went undelivered.
After any change to `scripts/finance-pulse.md`, re-run the script on the host:

```bash
/srv/lifekit-stack/scripts/ensure-finance-pulse.sh
```

#### The finance heartbeat, retired for an isolated cron job (fs-685, 2026-09-26)

The heartbeat lane above (`agents.entries.finance.heartbeat`) turned out not
to isolate the way its `isolatedSession` key implies. A heartbeat tick is a
system-owned automation row, not a cron job: every tick of a given agent
reuses one persistent session (`agent:<id>:main:heartbeat`), and the row has
no delivery block of its own - `isolatedSession`, `target`/`to` only steer
where a heartbeat *would* deliver, they do not give the tick its own session
or a tracked delivery the way a cron job's `sessionTarget`/`delivery` fields
do. In practice that let a tick's working notes and recaps leak into the
operator's own chat instead of staying confined to one tracked, delivered
message. A cron job with `payload.kind: "agentTurn"` is structurally
different: every run gets a fresh `agent:<id>:cron:<jobId>:run:<uuid>`
session, and its `delivery` block is tracked per run (`deliveryStatus`:
`delivered`/`not-requested`/etc.), the same shape `ledger-opportunity` already
uses.

The fix retires the heartbeat tick (`agents.entries.finance.heartbeat.every`
set to `"0m"` in the platform file, deployed) and replaces it with a new
cron row, `ledger-pulse`, that mirrors `ledger-opportunity`'s shape: isolated
session target, Telegram announce delivery, and a prompt that requires either
exactly one delivered message or a bare `NO_REPLY` with no other output. Both
changes hot-reload - `agents.entries` and cron rows are both "no restart"
categories - so applying either does not recreate the gateway container.

The cron row is host state (like every other `agents.entries` key and every
cron job), so it is not in git; recreate it on a host that does not have it
yet with the gateway CLI, using the env var (never a literal chat id) that
already backs the retired heartbeat:

```bash
docker exec openclaw-openclaw-gateway-1 sh -c '
openclaw cron create --json <<JSON
{
  "name": "ledger-pulse",
  "displayName": "Ledger pulse (finance)",
  "agentId": "finance",
  "schedule": {"kind": "cron", "expr": "20 9,15,21 * * *", "tz": "Europe/Dublin"},
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "placeholder - ensure-finance-pulse.sh replaces this with the preamble plus scripts/finance-pulse.md",
    "model": "anthropic/claude-sonnet-4-6",
    "timeoutSeconds": 180,
    "lightContext": true,
    "toolsAllow": ["*"]
  },
  "delivery": {
    "mode": "announce",
    "channel": "telegram",
    "to": "'"$OPENCLAW_FINANCE_CHAT"'",
    "accountId": "finance",
    "bestEffort": true
  }
}
JSON
'
```

Right after creating the row, run `/srv/lifekit-stack/scripts/ensure-finance-pulse.sh`
so the message carries the checklist (the placeholder above does not).

**Verify** after creating or after any change to the row: trigger one run
(`openclaw cron run <id> --expect-final --json`) and confirm the result's
`sessionKey` matches `agent:finance:cron:<id>:run:<uuid>` (a fresh uuid per
run, never the old `agent:finance:main:heartbeat`) and `deliveryStatus` is
`delivered` or `not-requested` (a `NO_REPLY` reply sends nothing, so
`not-requested` there is correct, not a failure). `openclaw cron show <id>`
should report the row `enabled` with `sessionTarget: "isolated"`.

**Rollback:** restore `agents.entries.finance.heartbeat` to its pre-fs-685
shape (`every: "6h"`, the same `activeHours`/`model`/`lightContext`/
`isolatedSession`/`target`/`to`/`accountId`/`timeoutSeconds` this file carried
before, from git history) and disable or remove the `ledger-pulse` row
(`openclaw cron edit <id> --disable` or `openclaw cron rm <id>`). Both sides
of the rollback hot-reload the same way the fix does.

**Known gap at ship time:** scheduled cron runs on this host currently fail
on an unrelated auth problem (tracked separately, not touched by this
change), so the acceptance criterion of one full day of scheduled wake-ups
observed clean is not yet signed off - it is blocked on that separate fix,
not on this one. The manual trigger above is the isolation/delivery proof
available until scheduled runs are healthy again.

#### The finance agent after `ledger-scan` (retired 2026-09-19)

The push path above and the pulse replace the polling scan. `ledger-scan` (two
parked rows on the finance agent, every 2h 07:00-23:00 Dublin, disabled since
2026-08-31) is retired, not re-enabled: finance-sentry's jobs detect, the hook
wakes the agent with ids, the pulse carries the digest and the quiet-week
check. `ledger-lit-digest` (Sunday 20:00 Dublin, two rows) is also parked; it
is neither retired nor revived by this change. Neither the cron store nor the
finance agent's workspace is in git, so this change is three operator steps on
the host. Step 1 ran on 2026-09-19 against the live gateway: the two parked
`ledger-scan` rows are gone from the cron store. Steps 2 and 3 are not
applied - the live agent still carries its fifteen skills and the untrimmed
persona - and stay listed here as operator steps, for this host and for a
rebuilt one.

1. **Cron store.** Remove the parked `ledger-scan` rows through the gateway's
   own cron command (the ids come from `openclaw cron list --all --json`,
   filtered on `name == "ledger-scan"`):

   ```bash
   docker exec openclaw-openclaw-gateway-1 openclaw cron rm <ledger-scan job id>
   ```

   The scan prompt (`state/ledger-scan.prompt.txt`), its backup and the
   agent-side dedup log (`state/event-log.jsonl`) stay on disk as history; the
   companion `DedupKey` and the hook's `Idempotency-Key` own dedup now.

2. **Skill allowlist.** A wake turn (hook mapping, pulse) loads every assigned
   skill's frontmatter into its bootstrap, and OpenClaw 2026.9.4 has no per-run
   skill filter: `agents.entries.<id>.skills` is the only knob and it applies
   to every turn of that agent. Drop the six deck and model skills from
   Anthropic's `equity-research` marketplace plugin that no wake turn uses
   (`earnings-analysis`, `earnings-preview`, `sector-overview`, `dcf-model`,
   `comps-analysis`, `competitive-analysis`); keep `thesis-tracker`,
   `catalyst-calendar` (the ceremonies the persona describes) and
   `idea-generation` (the daily `ledger-opportunity` job). Arrays replace
   under `config patch`, so the list below is the whole allowlist:

   ```json5
   {
     agents: {
       entries: {
         finance: {
           skills: [
             "github", "gh-issues", "summarize", "notion", "skill-creator",
             "thesis-tracker", "catalyst-calendar", "idea-generation",
           ],
         },
       },
     },
   }
   ```

   Dry-run first, then apply, the same two commands as the per-agent patch
   below; `agents.entries` keys hot-reload. Re-assign a deck or model skill
   for an interactive session by adding it back to the list.

3. **Persona.** The finance workspace `AGENTS.md` (host state, 18,667 bytes
   against `bootstrapMaxChars` 20,000) still carries the scan's mechanics.
   Move them out, nothing else:

   - "Research-first mode", first paragraph: replace "it stays in the event
     log. Thresholds live in the `ledger-scan` cron prompt (no config file)."
     with "it stays unsent. Detection and thresholds are server-side in
     finance-sentry: events reach you by push (the finance-sentry hook wakes
     you with ids) and by the 6h pulse; you interpret, you never poll."
   - "On-disk state": delete the `event-log.jsonl` dedup-log bullet and the
     "There is no `config.json`" sentence (the file exists). Keep one line:
     `state/` files (the learning journal) are reached with bash and an
     absolute path, workspace file tools cannot reach them; watchlist, theses,
     quotes and IPS live in Postgres via MCP, not on disk.
   - "Cadence": delete the `ledger-scan` `dry_run` bullet and the
     "Silence-check" bullet (the pulse checklist owns the quiet-week digest).
     Reword the first bullet to "Event-driven only. No polling scan, no daily
     digest, no morning brief. The pulse (`ledger-pulse` message) carries
     the digest and the quiet-week check." and the THESIS BREAK bullet to
     "always notify - bypasses every silence rule." Keep the Delivery bullet.

   On a scratch copy of the 2026-09-19 file the three edits leave 18,169
   bytes (17,933 characters) - the size step 3 will produce, not the live
   file's: the scan mechanics were about 500 characters of the persona;
   the rest of the bootstrap cut comes from the six skills and from
   `lightContext` on the pulse. Anything beyond these three edits is a persona
   rewrite, which this change does not do.

The per-agent half stays host state and is still applied by hand. The patch
below addresses the 2026-09-16 `openclaw security audit` warnings that name
agents (`tools.exec.security_full_configured`,
`tools.exec.agent_skill_mcp_boundary_drift`, `models.weak_tier`); the
platform file above clears `gateway.auth_no_rate_limit`. It is checked
against the 2026.9.4 schema. Save it as `openclaw.patch.json5`:

```json5
{
  agents: {
    defaults: { model: { fallbacks: [] } },
    entries: {
      kit: { model: { fallbacks: [] } },
      career: { model: { fallbacks: [] }, tools: { exec: { mode: "ask" } } },
      devclaw: { model: { fallbacks: [] } },
      fable: { tools: { exec: { mode: "ask" } } },
    },
  },
}
```

Apply it (dry run first). These keys hot-reload: the CLI prints
`Change will apply without restarting the gateway.` and no recreate is
needed (applied this way on 2026-09-17).

```bash
docker exec -i openclaw-openclaw-gateway-1 openclaw config patch --stdin --dry-run < openclaw.patch.json5
docker exec -i openclaw-openclaw-gateway-1 openclaw config patch --stdin < openclaw.patch.json5
```

Exec policy per agent:

- career: set to `ask` - 0 exec calls in 30 days and no domain cron. (learning and
  social had the same setting until they were deleted on 2026-10-07.)
- fable: `full` to `ask` - no sessions ever; its only automated turn is the deploy pong smoke.
- devclaw keeps `full` - its daily morning-brief cron sweeps repos with `gh` and writes briefs.
- kit keeps `full` - skills github, gh-issues and summarize need host binaries (145 exec calls in 30 days),
  and since health folded into kit on 2026-10-07 the three claw CLIs are kit's only write path for food,
  workouts and daily state.
- finance keeps `full` - it reaches its `state/` files (the learning journal)
  with bash (2160 exec calls in 30 days, counted before `ledger-scan` was
  retired).

Command-kind crons (`memory_vault_audit`, `weekly_log_summary`) bypass exec
policy.

Expected `openclaw security audit` after the patch: `gateway.auth_no_rate_limit`
and `models.weak_tier` cleared; `security_full_configured` (devclaw) and
`agent_skill_mcp_boundary_drift` (kit, finance) remain.

Follow-up, out of scope for this change: the boundary-drift remediation
(sandbox those agents, or split the sensitive MCP servers into a separate
gateway).

### The career weekly cron

Since the 2026-09 fleet reshape (see `docs/openclaw-agent-design.md`), career
has one delivering cron job, `career-weekly`. What each run does is in the
agent's `AGENTS.md` (`defaults/agents/career/workspace/`). It is paused: the
row was disabled on 2026-10-07 until the job hunt restarts, and it comes back
with `openclaw cron enable <id>` (id from `openclaw cron list --all`). Its
`social-weekly` sibling went with the `social` agent the same day. The rows
are host state in the gateway's store. To recreate the row on a rebuilt host,
use this shape, checked against 2026.9.5. `cron add` has no failure-alert flags, so the alert is a follow-up
`cron edit`. It goes through the kit `default` bot, so a broken domain bot
still reports its own failures. `OWNER_CHAT` is `LIFEKIT_TELEGRAM_CHAT` from
the host env file. Never write the value into git.

```bash
GW=openclaw-openclaw-gateway-1
docker exec "$GW" openclaw cron add --name career-weekly \
  --display-name "Career weekly pulse" --agent career \
  --cron "0 9 * * 1" --tz Europe/Dublin --session isolated \
  --message "Run the weekly career pulse (career-weekly) as your AGENTS.md defines it." \
  --timeout-seconds 600 --announce --channel telegram --account career --to "$OWNER_CHAT" --json
# take "id" from the output, then:
docker exec "$GW" openclaw cron edit <id> --failure-alert --failure-alert-after 1 \
  --failure-alert-channel telegram --failure-alert-account-id default \
  --failure-alert-to "$OWNER_CHAT" --failure-alert-mode announce --no-best-effort-deliver
```

Remove a row with `cron rm <id>`. The
`skill-collection-review-<agent>` rows are system-owned, so `cron rm`
refuses them. `openclaw agents delete <id>` removes them along with the
agent's entry and bindings.

### The memory-vault-cleanup cron

The vault's model pass: an agent-turn job named `memory-vault-cleanup` on the
`kit` agent, 20 minutes after the command-kind `memory_vault_audit` cron
(`scripts/memory-audit/`). It runs the vault design's model jobs, and only on the
findings that audit just wrote (`audits/findings.json`, plus the contradiction
candidates and the Rotation section of `audits/latest.md`):

| Job | Input | Does |
| --- | --- | --- |
| M1 | `page-too-long` | splits the page by subject into its owning pages, keeping every fact |
| M2 | `restated-subject` | replaces the copy with a link to the owning page, updating the owner first when the copy is newer |
| M3 | SUSPECT contradiction candidates | the newest dated evidence wins; two owner rulings with nothing newer become an M6 call |
| M4 | log items in the Rotation section | lifts any lasting fact from the moved entries into a page |
| M6 | what M3 cannot settle, findings on files it may not edit | one owner-call line each, with a recommendation |

Deterministic work (lint, index, rotation, archive moves) stays in the vault's
scripts. Every edit is a git commit by `memory-model` with a `model:` subject, so
`git log --author=memory-model` lists the pass and any commit reverts on its own.
Each job gates itself: it runs `bin/lint.sh --changed --tsv`, reads only the rows
for the files a finding changed and is all-or-nothing per finding: if any stays red
it restores every file the finding touched (`git checkout` of a tracked file,
removal of one it created) and names the finding as skipped in its closing log
line, otherwise it commits them together with `git commit --no-verify -- <its
paths>`. The vault hook is skipped because it lints every changed file on
disk, including the red ones memory-sync leaves uncommitted, which would refuse the
commit for files the job never touched. When `bin/log.sh` reports its entry
uncommitted (the same hook refuses it), the job commits that log file itself the
same way, as `model: log ...`. Before editing a file a job runs `git status
--porcelain -- <path>`: a path that is already dirty holds someone else's
uncommitted edits (a red file memory-sync kept), so the job skips that finding
this week, never restores or commits the file, and names it in its closing log
line; the next week's run retries it. No edit is left on disk for memory-sync to
sweep into an `auto:` commit; a crash or a run past the one-hour `.model-pass`
window can still do that, and is a known residual risk. The pass never pushes:
[memory-sync](#memory-vault-sync) pushes its commits on the next tick, and skips
its ticks while the pass holds `~/memory/.model-pass` (an hour at most). It never
edits `sources/`, `PLAN.md`, `audits/` or generated blocks; such a finding becomes
an M6 call. Declared by `scripts/ensure-memory-vault-cleanup.sh`: re-run it as
`lifekit` after a change to it or on a rebuilt host. On an existing row it
converges the message, description and delivery and leaves the schedule alone.

The audit rotates (`bin/audit.sh --rotate`) before this pass runs, so M4 reads
entries rotation has already moved rather than distilling before the move. That
loses nothing once the vault's rotation moves logs verbatim; running M4 before the
move needs the audit to report due rotations without applying them, a vault
change.

**Owner calls go through notify-relay, not `fleet-policy decide`.** fleet-policy
lives in the maintainer's home on the host, which neither the gateway container
nor the `lifekit` account can reach.

**Delivery is transition-only, through notify-relay, not an OpenClaw `--announce`.**
The job is created (and, on every re-run of the ensure script, converged) with
`--no-deliver`, so OpenClaw's own runner fallback delivery is off - without it,
the job's own default (`mode: "announce", channel: "last"`) fail-closes every run,
since an isolated-session job has no prior chat for "last" to resolve against
(confirmed live 2026-09-29: `cron show` reported exactly that fail-closed
`deliveryPreview`). Instead, the agent's own prompt builds the M6 owner-call
list, diffs it (as a set) against the previous run's list kept in
`audits/needs-denys-state.json` (vault-side state, not a knowledge page), and only
when that set changed does it `POST` one envelope itself to
`http://notify-relay:8090/notify` (reachable from the gateway container on the
compose network, confirmed live) in the shape `docs/message-format.md` defines -
`level: wait` while the list is non-empty, `level: good` the run it clears. An
unchanged list, including an unchanged empty one, sends nothing. This keeps every
owner-facing message on the stack's one grammar/one renderer instead of adding a
second, unformatted, every-run Telegram path.

**Verify** after creating: `docker exec openclaw-openclaw-gateway-1 openclaw cron show
<id>` reports `delivery.mode: "none"` (id from the create output or `cron list --all
--json` filtered on `name == "memory-vault-cleanup"`). Then `docker exec
openclaw-openclaw-gateway-1 openclaw cron run <id> --expect-final --json` and check
`cron show <id>` again reports `enabled` with `sessionTarget: "isolated"`. A run
against a same-day `audits/findings.json` appends one `audit | model pass: ...`
line to the vault's `log.md` every time (`model pass: nothing to apply` when no
finding needed it), with its edits as `model:` commits by `memory-model` and no
`.model-pass` left behind; a run against a stale report appends a `skipped:
audits/findings.json is stale (<date>)` line instead and touches nothing else.
Either way, check `audits/needs-denys-state.json` holds the run's owner calls, and
that a notify-relay call only happened when that list changed.

**Rollback:** `openclaw cron rm <id>` (or `cron edit <id> --disable`). Nothing else
depends on this row - `memory_vault_audit` keeps running unchanged either way.

### Agent memory in the vault

Kit (`kit`), Ledger (`finance`) and devclaw keep their workspace `memory/`
directory in the vault (lifekit-stack#175).
`compose/openclaw/docker-compose.yml` bind-mounts
`${LIFEKIT_LIFE_DIR}/agents/<id>/memory` over
`/home/node/.openclaw/agents/<id>/workspace/memory` on `openclaw-gateway`, and
on `openclaw-cli` too. The session-memory hook's notes, written on `/new` and
`/reset`, and the agents' own daily notes then land in the vault's episodic
class `agents/<agent>/memory/`, which is not wiki-linted. `memory-sync.timer`
commits and pushes them like any other vault edit. Only exact `YYYY-MM-DD.md`
daily notes rotate at 90 days; the session-memory notes (`YYYY-MM-DD-HHMM.md`
or `YYYY-MM-DD-<slug>.md`) do not rotate until the vault's rotate glob is
widened, a separate follow-up.

What the mounts leave out:

- **No config key.** OpenClaw has no key for an agent's `memory/` path, so the
  bind mount is the lever.
- **`MEMORY.md` stays at the workspace root, not mounted.** A single-file bind
  does not survive a write that replaces the file by rename. It holds a
  pointer to the vault, not facts (see
  [Workspace memory files are pointers](#workspace-memory-files-are-pointers)).
- **`dreaming/` and `.dreams/` are not copied.** They are memory-core dreaming
  output. Dreaming is off (the platform file, see
  [Dreaming off](#dreaming-off-pluginsentriesmemory-coreconfigdreamingenabled)),
  so they are dead and are deleted rather than moved.
- **Only these three agents.** The other agents keep their memory in their
  workspace.

The mounted notes are also under the vault path that `memory.search.extraPaths`
indexes, so a `memory_search` hit on one may show up twice: once as workspace
memory and once as a vault page.

**Host prep (operator, once, before the first deploy that carries the mounts).**
Docker creates a missing bind source as an empty root-owned directory, which
the gateway (uid 1000, the host `lifekit` account) cannot write to. So create
the directories first and copy the existing notes in. The step is idempotent
(`--ignore-existing` never overwrites a vault copy, and the log line is guarded):

```bash
sudo -u lifekit bash -euo pipefail <<'SH'
for id in kit finance devclaw; do
  src=/srv/openclaw/config/agents/$id/workspace/memory
  dst=/srv/memory/agents/$id/memory
  install -d -m 755 "$dst"
  [ -d "$src" ] || continue
  rsync -a --ignore-existing --exclude=/dreaming/ --exclude=/.dreams/ "$src/" "$dst/"
  rm -rf "$src/dreaming" "$src/.dreams"
done
cd /srv/memory
grep -q 'lifekit-stack#175' log.md ||
  sh bin/log.sh "agents/{kit,finance,devclaw}/memory now hold those agents' workspace memory notes, bind-mounted into the gateway (lifekit-stack#175)"
SH
```

`bin/log.sh` commits only its own `log.md` line. The copied notes are
committed by the next `memory-sync.timer` run, never by hand. An exact
`YYYY-MM-DD.md` daily note older than 90 days is retired by the next Sunday
`memory_vault_audit` rotation, and git history keeps it; session-memory notes
with a suffix after the date are not matched by the rotate glob and stay. The
originals stay in the workspace, hidden under the mount, which is what makes
the rollback lossless.

**Rollback:** remove the three mount lines from both services and let the
merge redeploy. The agents go back to their workspace copies. Notes written
while the mounts were live stay in the vault, under `agents/<id>/memory/`.

**Verify** after the deploy:

- Send `/new` to one of the three agents. A dated note appears under
  `/srv/memory/agents/<id>/memory/`, named `YYYY-MM-DD-HHMM.md` or
  `YYYY-MM-DD-<slug>.md`.
- A `memory_search` from that agent finds the note.
- `docker exec openclaw-openclaw-gateway-1 openclaw doctor` is clean.
- `curl -fsS http://127.0.0.1:18789/healthz` returns 200.

### Workspace memory files are pointers

`MEMORY.md` and `USER.md` are upstream OpenClaw bootstrap files: the gateway
loads them into each agent's startup context. The vault is the one memory
store, so in the kit, career and finance workspaces both files hold a pointer
to `~/memory/CORE.md` and no facts. `deploy.sh` runs
`scripts/workspace-pointers/install-workspace-pointers.sh` as `lifekit` on
every deploy, which writes `defaults/workspace-pointers/{MEMORY,USER}.md` over
whatever is there. A hand edit, or a fact an agent wrote into either file, is
rewritten at the next deploy; a durable fact goes to its owning vault page.
Dreaming, which would promote notes into `MEMORY.md`, is off (see
[Dreaming off](#dreaming-off-pluginsentriesmemory-coreconfigdreamingenabled)).

No backup is kept in the workspace: the openclaw-config mirror's git history
holds every version mirrored before the switch, and the mirror no longer
carries either file (see [openclaw-config mirror](#openclaw-config-mirror)).
An agent is added to the script's `AGENTS` list only once the facts in its
two files are confirmed in the vault. devclaw's are not yet.

**Rollback:** revert the change and let the merge redeploy; the step stops
running. To restore one agent's earlier files, take them from the mirror's
history and install them as `lifekit`.

### Kit's needs-you decisions and the daily digest

Decisions waiting on the owner go to the OpenClaw app, not Telegram. Kit keeps one
ledger in its workspace (`needs-you/open.json`, closed ones in
`needs-you/closed.jsonl`). It asks each decision with `ask_user` in its main
Control UI / WebChat session (`agent:kit:main`), which shows as a card with buttons.
An answer closes the entry. The contract is kit's `AGENTS.md`, section "Needs you"
(`defaults/agents/kit/workspace/`). `ask_user` exists only in primary sessions, never
in subagent or ACP runs. So a decision that comes up in a Telegram DM, an A2A ask or a
subagent is recorded, then handed to the main session with `sessions_send`.

The `needs-you-digest` cron runs at 09:00 and 17:00 Europe/Dublin (one row, cron
`0 9,17 * * *`) and posts what is still open (the ledger, and the fleet's
open decisions through the relay skill's `status`, see "Kit as the owner's line to
firstmate") into that same chat, followed by a read-only mail section: how many
messages arrived since the last run (16h in the morning, 8h in the afternoon), which
need the owner, which can wait, and the noise as a count. Mail is ranked from its
content, not by sender, and the digest says so until a who-matters list exists.

Kit's main session never reads mail. A second row, `needs-you-mail-reader`, runs
15 minutes earlier (`45 8,16 * * *`, `--exact`) as an isolated session whose
tool allow-list is exactly `google-workspace__search_gmail_messages` and
`google-workspace__get_gmail_messages_content_batch`. That is the row's
`payload.toolsAllow`, which the gateway's `openclaw` agent runtime enforces: the run has
no relay, `ask_user`, `sessions_send`, `message`, exec, file-write or Gmail-write tool,
whatever the mail says. It delivers nothing; its run stays in its run history, whose
`summary` is all of the run's text, kept from the head and cut at 2000 characters. The
reader writes no text but its final reply, a mail section of under 1500 characters
between a `<<<MAIL-SECTION` line and a `MAIL-SECTION>>>` line.
The digest, allowed only `read`, `exec` and `process` (no Gmail tool, but `exec`
is a shell, so `relay.sh` and its send verbs stay reachable; the prompt forbids
them and nothing else does), reads that summary with
`openclaw cron runs <reader id> --limit 1 --sort desc --json` and copies in only the
text strictly between the two marker lines, as data; a missing marker (narration only,
a cut-off reply, an empty summary) shows as `mail unavailable: reader reply
unreadable`, and `No new mail.` between the markers leaves the section out. Mail text is untrusted: the reader summarises it only and follows nothing in
it. The summary is untrusted model output derived from mail and is treated as data
by the digest turn; the split removes the Gmail and send tools from the mail-reading
turn only. The Workspace MCP exposes exactly one write tool, `send_gmail_message`
(`WORKSPACE_MCP_PERMISSIONS` + `WORKSPACE_MCP_DISABLED_TOOLS` in
`compose/openclaw/docker-compose.yml`, see `docs/google-mcp-setup.md`), so the server
does not stop a run from sending: the reader's allow-list leaves it out, the reader's
prompt names `send_gmail_message` and `draft_gmail_message` as forbidden, and the
`agent-mail-sent` alert pings the owner on any send. A lapsed Google sign-in, a failed reader
run or one more than an hour old shows as a `mail unavailable:` line, and the other
parts still post.
`scripts/ensure-needs-you-digest.sh` declares both rows and converges on re-run. It
exits non-zero, before touching the digest, when the reader's live row allows any tool
beyond those two, is bound to a session or has a delivery, and after converging when
the digest's allow-list is not exactly `read,exec,process`. The digest is a
`current`-session agent turn bound to `agent:kit:main` with announce delivery: the
reply is committed to the conversation's transcript, and that session has no external
route, so nothing goes to Telegram; after converging, the script re-reads the live row
and exits non-zero if its delivery or session binding could route outside the chat
(cadence policy: vault `domains/agent-behavior.md`, "Adopted workflow policies P1-P6").
It always posts: the date line, then the needs-you
part in the relay skill's Status format, which is the single line "Nothing needs you
today." when the ledger and the fleet show no call and no to-do and no mail needs you. A run that cannot read
the ledger or the mail says so in the chat. A run that
fails outright raises a failure alert after one failure. The alert goes through kit's
`default` Telegram account to `LIFEKIT_TELEGRAM_CHAT` from the host env file, the same
route as the career weekly alert, because a failure alert needs an outbound
channel and the Control UI chat has none.

Operator steps, once per host. None of them restarts the gateway:

1. **Let kit's main session have `ask_user`.** The global `tools.allow` list is host
   state, and `ask_user` is not on it. `tools.*` hot-reloads. Arrays replace in a
   config patch, so append to the live list instead of writing a new one:

   ```bash
   GW=openclaw-openclaw-gateway-1
   docker exec "$GW" openclaw config get tools.allow --json \
     | python3 -c 'import json,sys; t=sys.stdin.read(); a=json.loads(t[t.find("["):]); print(json.dumps({"tools": {"allow": a + ([] if "ask_user" in a else ["ask_user"])}}))' \
     | docker exec -i "$GW" openclaw config patch --stdin
   docker exec "$GW" openclaw gateway call tools.effective --timeout 90000 \
     --params '{"sessionKey":"agent:kit:main"}' --json | grep -c '"ask_user"'   # >= 1
   ```

2. **Merge the "Needs you" section into kit's live `AGENTS.md`**
   (`/srv/openclaw/config/agents/kit/workspace/AGENTS.md`). Also merge the changed
   A2A-intake line that records the go in the ledger. The live file has drifted from
   the template, so paste the section in rather than copying the whole file over it.

3. **Declare the digest and its mail reader:**
   `/srv/lifekit-stack/scripts/ensure-needs-you-digest.sh` (as lifekit;
   `NEEDS_YOU_DIGEST_CRON` / `NEEDS_YOU_DIGEST_TZ` override the default
   `0 9,17 * * *` Europe/Dublin, and `NEEDS_YOU_MAIL_CRON` the reader's
   `45 8,16 * * *`; keep each reader run within the hour before its digest). The
   same run moves a digest row declared before the reader split onto the new prompt
   and the `read,exec,process` allow-list. The mail section needs a live Google sign-in: if the
   refresh token is revoked (`invalid_grant` in `docker logs openclaw-google-workspace-mcp-1`),
   redo the consent in `docs/google-mcp-setup.md` first.

**Verify:** `cron show <id>` previews the digest's delivery as `announce -> current
session` and the reader's as none; `cron list --json` shows each row's
`payload.toolsAllow`. That is the stored field only; to see that a run is held to it,
run the reader once, `cron run <reader id> --expect-final --json`, and confirm from
`cron runs <reader id> --limit 1 --json` (or the `cron:<reader id>` session
transcript) that it called only the two Gmail read tools, and read that entry's
`summary` to check both marker lines are present with the mail section between
them. Then, within the hour,
run the digest, `cron run <id> --expect-final --json`:
against an empty ledger and no new mail it posts "Nothing needs you today." under
the date line. With one entry in `open.json`, the digest shows up in the Control UI
chat without a refresh. Run alone, the digest says the mail read last ran at its
time, or has not run yet.

**Mail section rollback:** `openclaw cron rm <reader id>` stops the mail read; the
digest then shows `mail unavailable: the mail read has not run yet` and the rest
posts. To drop the line too, re-run the script from the commit before the mail
section (or `NEEDS_YOU_DIGEST_CRON='0 9 * * *'` for one run a day only). Never move
the mail read back into the digest row: its main-session turn holds the relay and
`sessions_send`. To change what the Workspace MCP may write (not part of the digest),
change the `WORKSPACE_MCP_PERMISSIONS` / `WORKSPACE_MCP_DISABLED_TOOLS` literals in a
reviewed PR. After the prompt wording changes, re-run
`scripts/ensure-needs-you-digest.sh` as `lifekit` (deploy does not run it).

**Rollback:** `openclaw cron rm <id>` and `openclaw cron rm <reader id>`. The ledger files are kit workspace state and
can stay. Push delivery of these cards to a phone comes with the Control UI's
HTTPS-origin setup, not with this job.

### Kit's second-mate relay

Kit can send the owner's words to the VPS second mate's captain inbox (or, by home
argument, the main firstmate's: "Kit as the owner's line to firstmate" below) and read
the replies, over one SSH key whose only power is a forced command on the host. It is a
pipe: kit copies the text into a file and runs one script; it never decides what to send.

- **Host:** `scripts/kit-relay/kit-relay`, installed root-owned at
  `/usr/local/libexec/kit-relay` by `scripts/kit-relay/install-kit-relay.sh`, which
  pins the second mate's home (`FM_HOME`, the default) and optionally one more home
  (`FM_HOME_ALT`, see "Home argument" below) into the installed copy. Two verbs only:
  `note <kit-request-id> [<home>]` (body on stdin, 1 B to 32 KiB of UTF-8 without NUL, refused
  whole otherwise, exit 65) and `receipts [<12-digit cursor>] [<home>]` (only the notes whose
  request id starts with `kit-`, with their replies). Anything else exits 64. At most
  30 new `kit-` notes per rolling hour (exit 75); a replay of an existing request id is
  never limited. `fm-inbox.sh` runs under `env -i`, so nothing the client sends (SendEnv,
  `FM_*_OVERRIDE`) reaches it. Each note and refusal is logged with tag `kit-relay`
  (`journalctl -t kit-relay`).
- **Key:** `/srv/lifekit-secrets/kit-relay/` (`id_ed25519`, pinned `known_hosts`,
  `ssh_config`), mounted read-only into the gateway at `/run/lifekit/kit-relay`. The
  authorized_keys line carries `restrict` (no PTY, forwarding or agent),
  `command=` and `from="172.16.0.0/12"` (the docker bridges only, so a copied key is
  useless off the box). The gateway reaches the host as `host.docker.internal`
  (`extra_hosts: host-gateway` in `compose/openclaw/docker-compose.yml`).
- **Skill:** `skills/secondmate-relay` (`SKILL.md` + `relay.sh`), installed into kit's
  workspace by `scripts/ensure-secondmate-relay.sh`. The request id is
  `kit-<UTC yyyymmdd>-<sha256(body)[:16]>`, so a retry of the same text the same day is
  a replay of the same note, never a second one.
- **Answering:** the second mate answers a `kit-` note with `fm-inbox.sh reply <id>`
  (one reply per note) and then `drain --ack <id>`. Kit shows replies when asked; there
  is no push.

The key reaches the second mate as the owner: any process in the gateway that can read
it can send a note. `from=`, the two verbs, the rate limit, the `kit-` request ids and
the log line bound that; they do not close it.

Operator steps, once per host, after the PR that adds the mount has deployed. The
deploy recreated the gateway with the mount, and Docker created
`/srv/lifekit-secrets/kit-relay` empty and root-owned; step 2 resets it. Run as the admin
account (`ADMIN_USER`, README "VPS users"), the account that owns the second mate's
home and whose `authorized_keys` takes the key:

```bash
ADMIN_USER=<admin account>
FM_HOME=<the second mate's home, holding bin/fm-inbox.sh>

# 1. The forced command, root-owned, with FM_HOME pinned into it.
sudo FM_HOME="$FM_HOME" bash /srv/lifekit-stack/scripts/kit-relay/install-kit-relay.sh

# 2. Key, pinned host key and client config, readable by the gateway's uid 1000.
sudo install -d -o lifekit -g lifekit -m 0700 /srv/lifekit-secrets/kit-relay
sudo -u lifekit ssh-keygen -t ed25519 -N '' -C kit-relay@openclaw-gateway \
  -f /srv/lifekit-secrets/kit-relay/id_ed25519
printf 'lifekit-vps %s\n' "$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)" \
  | sudo -u lifekit tee /srv/lifekit-secrets/kit-relay/known_hosts >/dev/null
sudo -u lifekit tee /srv/lifekit-secrets/kit-relay/ssh_config >/dev/null <<EOF
Host relay
  HostName host.docker.internal
  HostKeyAlias lifekit-vps
  User ${ADMIN_USER}
  IdentityFile /run/lifekit/kit-relay/id_ed25519
  IdentitiesOnly yes
  UserKnownHostsFile /run/lifekit/kit-relay/known_hosts
  StrictHostKeyChecking yes
  BatchMode yes
  ConnectTimeout 10
  ServerAliveInterval 10
  ServerAliveCountMax 2
  RequestTTY no
  ForwardAgent no
  ClearAllForwardings yes
EOF
sudo chmod 0400 /srv/lifekit-secrets/kit-relay/id_ed25519
sudo chmod 0444 /srv/lifekit-secrets/kit-relay/known_hosts /srv/lifekit-secrets/kit-relay/ssh_config
sudo chmod 0500 /srv/lifekit-secrets/kit-relay

# 3. Authorize it for the admin account (append, never replace).
printf 'restrict,command="/usr/local/libexec/kit-relay",from="172.16.0.0/12" %s\n' \
  "$(sudo cat /srv/lifekit-secrets/kit-relay/id_ed25519.pub)" >> ~/.ssh/authorized_keys

# 4. The skill into kit's workspace (as lifekit).
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-secondmate-relay.sh

# 5. Kit's skill allowlist. agents.entries.kit.skills replaces the defaults and a
#    config patch replaces arrays, so derive the whole list from the live file.
#    Skip this step when step 4 reported kit as unfiltered (no skills list): every
#    skill is already allowed.
G=$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway)
docker exec "$G" node -e 'const c=require(process.env.OPENCLAW_CONFIG_PATH);const s=c.agents.entries.kit.skills;if(!Array.isArray(s))process.exit(1);if(!s.includes("secondmate-relay"))s.push("secondmate-relay");process.stdout.write(JSON.stringify({agents:{entries:{kit:{skills:s}}}}))' > /tmp/kit-skills.patch.json
docker exec -i "$G" openclaw config patch --stdin --dry-run < /tmp/kit-skills.patch.json
docker exec -i "$G" openclaw config patch --stdin < /tmp/kit-skills.patch.json
```

The skill watcher picks up the new `SKILL.md`. If kit does not see it, force-recreate
the gateway as in `scripts/ensure-morning-brief.sh`.

**Verify** from inside the gateway, then answer the smoke note from the second mate's
home (`fm-inbox.sh reply <id> …`, then `drain --ack <id>`) and check kit shows the reply:

```bash
S=/home/node/.openclaw/agents/kit/workspace/skills/secondmate-relay/relay.sh
docker exec "$G" sh "$S" replies
printf 'relay smoke test - ignore\n' > /tmp/s.txt && docker cp /tmp/s.txt "$G":/tmp/s.txt \
  && docker exec "$G" sh "$S" note /tmp/s.txt
docker exec "$G" ssh -F /run/lifekit/kit-relay/ssh_config relay 'id'   # refused, exit 64
journalctl -t kit-relay --since -5min
```

**Failures kit reports:** host or sshd down gives `ssh` exit 255 within
`ConnectTimeout` and nothing is saved, so a retry of the same text is safe.
`Permission denied (publickey)` (line removed, `from=` mismatch, key perms) and
`Host key verification failed` (host key changed; update the pinned `known_hosts`) need
the operator. fm-inbox exit 3 means saved but not announced; re-sending the same body
(a same-day replay) re-announces it and never duplicates the note. Kit reports "Saved as
note <id>, not yet announced." and re-sends only when the owner asks.

**Rollback:** delete the authorized_keys line, `sudo rm -r /srv/lifekit-secrets/kit-relay`
and `/usr/local/libexec/kit-relay`, re-apply kit's old skills list, and remove the skill
folder from kit's workspace. The mount and `extra_hosts` can stay: an empty directory
gives the gateway nothing.

#### Dashboard sender (proposal box)

The dashboard can queue a `proposal:` or `task:` note for the second mate while Claude
quota is exhausted (Kit shares that login, so its relay cannot run then). The same key
also carries the owner's `answer <id>: …` and `confirm <id>` notes to the owning home's
captain inbox ([`decision-contract.md`](./decision-contract.md), "Answering a decision").
It is a second authorized key on the same root-owned forced command, not a second script:

- **Sender:** the key's `command=` passes the sender as an argument,
  `command="/usr/local/libexec/kit-relay dashboard"`; no argument is Kit's key. The
  sender picks the request-id prefix (`dash-` instead of `kit-`), the rate bucket
  (30 new `dash-` notes per rolling hour, counted apart from Kit's) and the `receipts`
  view (only `dash-` notes and their replies). Nothing the client sends picks the
  sender. Kit's key, prefix and bucket are unchanged.
- **Key:** `/srv/lifekit-secrets/dashboard-relay/` (`id_ed25519`, pinned `known_hosts`,
  `ssh_config`, the same shape as `kit-relay/`), mounted read-only into the dashboard
  container at `/run/lifekit/dashboard-relay` (the mount lives in lifekit-dashboard). The
  body starts with `proposal:`, `task:`, `answer ` or `confirm `; the relay does not
  interpret it.
- **Operator step** (after the dashboard PR deploys; re-run step 1 above first so the
  installed copy knows the sender, then repeat step 2 with `dashboard-relay` in place of
  `kit-relay` and `-C dashboard-relay@dashboard`, and `IdentityFile` /
  `UserKnownHostsFile` under `/run/lifekit/dashboard-relay/`). Authorize it:

  ```bash
  printf 'restrict,command="/usr/local/libexec/kit-relay dashboard",from="172.16.0.0/12" %s\n' \
    "$(sudo cat /srv/lifekit-secrets/dashboard-relay/id_ed25519.pub)" >> ~/.ssh/authorized_keys
  ```

- **Rollback:** delete that authorized_keys line and `sudo rm -r /srv/lifekit-secrets/dashboard-relay`.
  Kit's relay is untouched. The second mate answers a `dash-` note like a `kit-` one.

#### finance-sentry sender (problem reports)

finance-sentry's API saves a user's problem report and a Hangfire job forwards it to the
second mate's inbox as `note fs-report-<id>`. It is a third authorized key on the same
root-owned forced command, not a second script:

- **Sender:** the key's `command=` passes the sender as an argument,
  `command="/usr/local/libexec/kit-relay finance-sentry"`. The sender picks the
  request-id prefix (`fs-`), the rate bucket (30 new `fs-` notes per rolling hour, per home,
  counted apart from Kit's and the dashboard's) and the `receipts` view
  (only `fs-` notes and their replies). Body limits are unchanged: 1 B to 32 KiB of UTF-8
  without NUL, refused whole otherwise (exit 65). Nothing the client sends picks the
  sender; Kit's and the dashboard's keys, prefixes and buckets are unchanged. Each note
  and refusal is logged with tag `kit-relay`.
- **Key:** `/srv/lifekit-secrets/fs-relay/` (`id_ed25519`, pinned `known_hosts`,
  `ssh_config`, the same shape as `kit-relay/`), mounted read-only into finance-sentry's
  API container at `/run/lifekit/fs-relay` (the mount, `extra_hosts: host-gateway` and
  `openssh-client` live in finance-sentry). The body is the report text; the relay does
  not interpret it. The relay has `receipts` only: it does not reply to the reporter.
- **Operator step** (once per host; this PR adds nothing to a running container, and the
  steps do nothing until finance-sentry's change has deployed and its Hangfire job
  sends). Run as the admin account (`ADMIN_USER`, README "VPS users"), the account whose
  `authorized_keys` takes the key. Step 1 must come first: an installed copy without the
  `finance-sentry` arm exits 78 for this key.

```bash
ADMIN_USER=<admin account>
FM_HOME=<the second mate's home, holding bin/fm-inbox.sh>
FM_HOME_ALT=<the main home, only if the live install pins one; same as today>

# 1. Reinstall the forced command (root). --print first to diff against the live copy;
#    pass the same FM_HOME / FM_HOME_ALT the live copy was installed with.
FM_HOME="$FM_HOME" FM_HOME_ALT="$FM_HOME_ALT" bash /srv/lifekit-stack/scripts/kit-relay/install-kit-relay.sh --print \
  | diff /usr/local/libexec/kit-relay - || true
sudo FM_HOME="$FM_HOME" FM_HOME_ALT="$FM_HOME_ALT" bash /srv/lifekit-stack/scripts/kit-relay/install-kit-relay.sh

# 2. Key, pinned host key and client config. The key is a per-box credential, never
#    committed (docs/secrets.md). It is generated as lifekit, then handed to the uid the
#    API container runs as: `docker exec <finance-sentry api container> id -u`.
API_UID=<uid the API container runs as>
sudo install -d -o lifekit -g lifekit -m 0700 /srv/lifekit-secrets/fs-relay
sudo -u lifekit ssh-keygen -t ed25519 -N '' -C fs-relay@finance-sentry-api \
  -f /srv/lifekit-secrets/fs-relay/id_ed25519
printf 'lifekit-vps %s\n' "$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)" \
  | sudo -u lifekit tee /srv/lifekit-secrets/fs-relay/known_hosts >/dev/null
sudo -u lifekit tee /srv/lifekit-secrets/fs-relay/ssh_config >/dev/null <<EOF
Host relay
  HostName host.docker.internal
  HostKeyAlias lifekit-vps
  User ${ADMIN_USER}
  IdentityFile /run/lifekit/fs-relay/id_ed25519
  IdentitiesOnly yes
  UserKnownHostsFile /run/lifekit/fs-relay/known_hosts
  StrictHostKeyChecking yes
  BatchMode yes
  ConnectTimeout 10
  ServerAliveInterval 10
  ServerAliveCountMax 2
  RequestTTY no
  ForwardAgent no
  ClearAllForwardings yes
EOF
sudo chown -R "$API_UID:$API_UID" /srv/lifekit-secrets/fs-relay
sudo chmod 0400 /srv/lifekit-secrets/fs-relay/id_ed25519
sudo chmod 0444 /srv/lifekit-secrets/fs-relay/known_hosts /srv/lifekit-secrets/fs-relay/ssh_config
sudo chmod 0500 /srv/lifekit-secrets/fs-relay

# 3. Authorize it for the admin account (append, never replace). The line is the
#    template: same restrict / from= as Kit's and the dashboard's, only the argument differs.
printf 'restrict,command="/usr/local/libexec/kit-relay finance-sentry",from="172.16.0.0/12" %s\n' \
  "$(sudo cat /srv/lifekit-secrets/fs-relay/id_ed25519.pub)" >> ~/.ssh/authorized_keys
```

  The block is flush-left so the heredoc terminator in step 2 sits at column 0. The `from=`
  range is the docker bridges, so a copied key is useless off the box.

- **Verify one note end to end**, from inside finance-sentry's API container (or any ssh
  with that key from a docker bridge address). `A` is the API container:

  ```bash
  A=<finance-sentry api container>
  printf 'relay smoke test - ignore\n' \
    | docker exec -i "$A" ssh -F /run/lifekit/fs-relay/ssh_config relay 'note fs-smoke-20261012'
  # {"schema":"kit-relay-received.v1",...} then fm-inbox's JSON, outcome "created"
  docker exec "$A" ssh -F /run/lifekit/fs-relay/ssh_config relay 'receipts'   # lists fs-smoke-20261012 as pending
  docker exec "$A" ssh -F /run/lifekit/fs-relay/ssh_config relay 'note kit-x' </dev/null   # refused, exit 64
  journalctl -t kit-relay --since -5min   # the note, and the refusal
  ```

  Then answer the smoke note from the second mate's home (`fm-inbox.sh reply <id> ...`,
  then `drain --ack <id>`), so it does not stay in the inbox. Re-running the same
  `note fs-smoke-...` is a replay of the same note, never a second one.

- **Failures:** `Permission denied (publickey)` (line missing, `from=` mismatch, key perms)
  and `Host key verification failed` need the operator; exit 78 `unknown sender` means
  step 1 was not run; exit 75 is the hourly bucket; host or sshd down is `ssh` exit 255
  and nothing was saved, so the same request id is safe to retry.
- **Rollback:** delete that authorized_keys line and `sudo rm -r /srv/lifekit-secrets/fs-relay`.
  The `finance-sentry` arm can stay in the installed copy (re-running step 1 without it is
  not needed); Kit's and the dashboard's relays are untouched.

#### Home argument (answers for the main home)

Both verbs take an optional last word, the home the verb acts on, so a dashboard answer
to a call owned by the main home lands in that home's inbox. Every sender's key and the
same root-owned script serve it; there is no extra key.

- **Allowlist:** the installed copy pins at most two homes: `FM_HOME`, the default, and
  an optional `FM_HOME_ALT`. A client names one by the **last part of its path**
  (for example `fm-vps`, `firstmate`) and the script compares that string exactly against the two
  pinned names. A path, `..`, a wrong case, a prefix, anything else exits 64 with
  nothing read or written; the client's string is never turned into a path. With no
  home word the default home is used and behaviour, including the log line, is as
  before. Requests, the rate bucket (per prefix, per home) and receipts are those of
  the home named. Naming a home appends ` home=<name>` to the log line.
- **Wire:** `note dash-<id> firstmate`, `receipts firstmate`,
  `receipts <12-digit cursor> firstmate`. The ssh_config and keys are unchanged.
- **Install** (operator step, after the PR deploys; this is the only host change, and
  `authorized_keys` stays as it is). Run as root; each home must hold `bin/fm-inbox.sh`
  and their last path parts must differ. `--print` first, to diff against the installed
  copy:

  ```bash
  sudo FM_HOME=<the second mate's home> FM_HOME_ALT=<the main home> \
    bash /srv/lifekit-stack/scripts/kit-relay/install-kit-relay.sh
  ```

  Verify through the dashboard key from inside the dashboard container (or any ssh with
  that key): `relay 'receipts firstmate'` returns the main home's receipts and
  `relay 'receipts nowhere'` exits 64; `journalctl -t kit-relay --since -5min` shows both.
- **Rollback:** re-run the installer without `FM_HOME_ALT`; a home word other than the
  default's name is then refused.

#### Kit as the owner's line to firstmate (status, tasks, answers)

Kit's `secondmate-relay` skill is also how the owner reaches the main firstmate without
a terminal. It adds no key, verb or host file: the home argument above is already there
and the status read is an HTTP GET Kit could always make.

- **Client** (`skills/secondmate-relay/relay.sh`): `note <body-file> [<home>]` and
  `replies [<cursor>] [<home>]` take `fm-vps` (the default, unchanged when omitted) or
  `firstmate`; any other word exits 64 before `ssh` runs (the host re-checks it).
  `status` runs `curl -fsS -m 10 http://lifekit-dashboard:8080/api/fleet` and prints the
  body unchanged; a curl failure is its exit code and stderr. The URL is the fleet
  page's own data over the `lifekit-shared` network, so the skill gates on `curl` too.
- **Rules** (`SKILL.md`): status questions run `relay.sh status` and answer in one
  format: "Your call" (numbered, options when the record names them, no ids), "Your
  to-dos" (verb first), then a "Later" count line (parked, dated and blocked, counted from
  `queued` and never listed) and a "Fleet" line (`counts.queued_open`, which leaves out
  held items); only a view older than 30 minutes adds "Fleet view is partial", and
  "+N not shown" appears only when `counts.decisions_open` really exceeds the list. Kit never calls `start_google_auth` (its link redirects to localhost and leaves
  a pending sign-in); on a Google sign-in failure it says so once and points at
  `docs/google-mcp-setup.md` steps 6-7. Tasks and proposals go to `firstmate` with the
  `task:`/`proposal:` prefix; an answer is `answer <id>: <his words>` sent only on his
  explicit words, only for an id in `decisions_open` at send time, to that item's
  `home_id`; Kit does not classify decisions or gate anything: under each firstmate
  home's standing answer-note rule an ordinary decision closes on the answer, while a
  decision that would authorize a merge or a destructive, irreversible or
  security-sensitive action gets firstmate's reply saying what it will do and runs only
  on a relayed `confirm <id>` (a records rule applied by firstmate, not enforced by the
  relay, the host forced command or this repo); an id missing from a capped
  `decisions_open` list is reported as "not in the shown list", not "not open"; a reply
  cursor is passed only with the home it came from; nothing is sent from a cron, a
  heartbeat or a turn he did not ask for; a refused call is reported verbatim and not
  retried, and an exit 3 ("saved, not yet announced") is
  reported as such and re-sent only when he asks.
- **Digest:** the 09:00 and 17:00 `needs-you-digest` (`scripts/ensure-needs-you-digest.sh`) also
  runs `relay.sh status` and posts the fleet part in that same format, under the date
  line, then the mail section `needs-you-mail-reader` wrote 15 minutes earlier, when
  mail arrived; it posts "Nothing needs you today." when
  nothing is open and no mail needs you, and is never silent. Re-run the script as
  `lifekit` to converge the live row; a cron turn sends no note.
- **Morning brief:** the 08:30 `today-brief` row is host state made by hand with kit,
  not declared in git. `scripts/ensure-today-brief-fleet.sh` (as `lifekit`) inserts the
  fleet step into its prompt at two fixed anchors and refuses, row untouched, when the
  prompt no longer has them; a prompt already carrying the step is left alone. An empty
  fleet part reads "No fleet calls today." there, so it never contradicts the brief's
  calendar, mail or PR sections.
- **Kit's `AGENTS.md`** is live host state (`/srv/openclaw/config/agents/kit/workspace/`),
  merged by hand like the "Needs you" section. Five lines move with this: the
  host-work line says host work goes to firstmate through the captain inbox, "the main
  home (`firstmate`)"; the Tools table's dashboard row reads `lifekit-dashboard:8080`
  (not the old loopback port `127.0.0.1:18790`); the plain-chat answer line also takes
  `<n> <letter>` for entry `<n>` of the numbered list Kit last sent; the digest
  line says it posts "Nothing needs you today." when nothing is open and no mail
  needs you, not `NO_REPLY`; and the Google Workspace section's line that Kit never calls
  `start_google_auth` and, on a sign-in failure, says so once and points at
  `docs/google-mcp-setup.md`.
- **Deploy:** `sudo -u lifekit /srv/lifekit-stack/scripts/ensure-secondmate-relay.sh`
  (copies `SKILL.md` and `relay.sh`; no gateway restart). Verify from inside the gateway:
  `docker exec "$G" sh "$S" status | head -c 200`, and one `note <file> firstmate`
  shows `home=firstmate` in `journalctl -t kit-relay`.
- **Rollback:** revert the skill and re-run the ensure script. Notes already sent stay
  in the inbox they were sent to.

## Rolling back OpenClaw

### The one-rollback rule

`deploy.sh` keeps exactly one fallback image, `lifekit-openclaw:prev`: the
image that ran the previous version, retagged only on a version change (so
later deploys of the same version do not overwrite it). When the retag
moves `:prev` to a new image, `deploy.sh` also drops the image the old
`:prev` pointed to, once nothing else tags or runs it - one rollback deep,
never more. Left unchecked (pre-2026.9, `deploy.sh` also stamped an ad-hoc
`pre-<version>` tag on every bump) these pile up at ~14 GB apiece and sit on
the box for months; don't reintroduce a second standing tag for this. If you
need to keep a bad build around past its rollback window - for a bug report,
or to compare two versions by hand - tag it explicitly for that one purpose
and untag it yourself once you're done; `deploy.sh` will not do that
bookkeeping for you.

The old image is pinned before the build, not tagged after it. Under the
containerd image store (the VPS's) an image id is addressable only while a tag
references it, and `docker compose build` moves `lifekit-openclaw:local` to the
new image, so the image the gateway is still running would be unreachable by
then (the 2026-10-07 deploy of #280 failed that way). `deploy.sh` therefore
tags the gateway container's image `lifekit-openclaw:pin` first, promotes the
pin to `:prev` on a version change, and removes it on every deploy. When the
running image cannot be pinned, the deploy accepts an existing `:prev` only if
it reports the running version; otherwise it stops before touching the gateway.
Rebuild that version from its commit and tag it `:prev` to unblock it.

If the new version misbehaves after the deploy checks passed:

```bash
ssh <your-vps-tailscale-name>
cd /srv/lifekit-stack/compose/openclaw
docker tag lifekit-openclaw:prev  lifekit-openclaw:local
docker compose -p openclaw --env-file /srv/lifekit-secrets/stack.env -f docker-compose.yml \
  up -d --no-build --force-recreate openclaw-gateway
```

Then revert the bump PR on `main` (below), or the next CI deploy rebuilds the
bad version over your rollback. This only works when the bump did not migrate
state. When it did (the deploy log shows "migrating state"), the old gateway
cannot read the migrated state dir: restore the full pre-upgrade backup
(`openclaw backup restore`, or untar the archive over `/srv/openclaw/config`
with the gateway stopped) before starting the `:prev` image.

## Path-gated OpenClaw deploys

A push to `main` always deploys the platform (observability, notify-relay, the
Grafana reloads, the contract check). The OpenClaw phases in
`scripts/deploy-openclaw.sh` (doctor, health checks, smoke turns, gateway build
and state migration) run only when a path listed in
`scripts/lib/openclaw-paths.sh` changed since the last successful deploy, and
always on `workflow_dispatch` or a manual `bash scripts/deploy.sh`
(`LIFEKIT_DEPLOY_OPENCLAW=always`, the default; CI sets `auto` on push).

- The deploy log says which: `OpenClaw phases: RUN (<why>)` or `SKIPPED (<why>)`.
- The range is `refs/lifekit/last-deployed..HEAD` in the VPS clone; the ref moves
  only when a deploy completes, so a failed or superseded run cannot hide an
  OpenClaw change. No ref, a non-ancestor ref (force push) or a failed diff runs
  the phases. Force one by hand: `git update-ref -d refs/lifekit/last-deployed`.
- `compose/openclaw/docker-compose.yml` counts as OpenClaw-relevant; the
  platform's `compose/docker-compose.yml` does not, since it defines no
  OpenClaw service. `secrets/lifekit-gateway.env.sops` counts too: the
  `secrets reload` that picks up a rotated gateway value is an OpenClaw phase.
- On a platform-only deploy the `openclaw` compose project is not brought up:
  the gateway, cli and google-workspace-mcp are not built, not recreated, and
  keep running as they are. A gateway that is down stays down until an OpenClaw
  deploy (dispatch CI or run `scripts/deploy.sh`).

## Rolling back the stack

If a deploy of anything else breaks something, revert on `main` and let CI
redeploy:

```bash
# On your laptop
git revert <bad-commit>
git push
# wait for CI deploy workflow to apply on the VPS
```

Only when CI itself is down, roll the VPS checkout back by hand:

```bash
ssh <your-vps-tailscale-name>
cd /srv/lifekit-stack
git log --oneline -n 5             # find the last-known-good commit
git checkout <good-commit>
bash scripts/deploy.sh
```

## The OpenClaw compose project: cutover and rollback

`openclaw-gateway`, `openclaw-cli` and `google-workspace-mcp` run as their own
compose project, `openclaw` (`compose/openclaw/docker-compose.yml`), apart from
the platform project `compose`. The gateway container is
`openclaw-openclaw-gateway-1`; scripts find it by its compose labels
(`com.docker.compose.project=openclaw`, `com.docker.compose.service=openclaw-gateway`).

**Cutover.** The first deploy after the move does it by itself, once:
`openclaw_phase_up` in `scripts/deploy-openclaw.sh` stops and removes the three
services' containers still labelled with the platform project, then brings up
the `openclaw` project. That recreates the gateway once. Old and new never run
together. Later deploys find no old containers and skip the step. Before the new
gateway starts, the deploy appends the `openclaw_default` network's gateway
address to `gateway.trustedProxies`. It keeps the old `compose_default` address
there, so a rollback needs no config change.

**Verify** after the cutover, as `lifekit` on the box:

```bash
GW=openclaw-openclaw-gateway-1
docker inspect --format '{{.State.Health.Status}}' "$GW"     # healthy
curl -fsS http://127.0.0.1:18789/healthz                    # gateway health through the host port
docker exec "$GW" openclaw channels status                  # all 6 Telegram accounts connected
docker exec "$GW" openclaw mcp probe                        # every MCP server answers
curl -fsS 'http://127.0.0.1:9090/api/v1/query?query=up{job="openclaw"}'   # value "1"
docker ps -a --filter label=com.docker.compose.project=compose \
  --filter label=com.docker.compose.service=openclaw-gateway -q          # empty
```

**Rollback** if any check fails. Rollback means redeploying the commit before
the move. Stop the `openclaw` project first: the old layout publishes the same
host port and polls the same Telegram bots, so the two gateways must never run
together.

```bash
ssh <your-vps-tailscale-name>
sudo -u lifekit -H bash
cd /srv/lifekit-stack
ids="$(docker ps -a -q --filter label=com.docker.compose.project=openclaw)"
[ -z "$ids" ] || { docker stop $ids && docker rm $ids; }
# deploy.sh hard-resets the checkout to DEPLOY_REF (default: origin/main), so
# pin it to the commit before the move rather than checking that commit out.
DEPLOY_REF="$(git rev-parse '<merge-commit>^')" bash scripts/deploy.sh
```

That brings the three services back in project `compose`.

Then revert the move on `main`, or the next CI deploy moves them again. Verify the
same way, with `GW=compose-openclaw-gateway-1`, and check that
`docker ps -a -q --filter label=com.docker.compose.project=openclaw` prints nothing.

**Gateway stop time.** `openclaw-gateway` has `stop_grace_period: 330s`, OpenClaw's
own stop policy for a non-systemd supervisor. A recreate, `compose stop` or
`docker stop` of the gateway can therefore take up to about 5.5 minutes when work
is in flight: it drains active sessions, then releases its owner lease. With
Docker's 10s default a draining gateway was killed with the lease still live, and
the next container (new hostname) could not start for 300s. Idle, the stop is
still seconds. Compose applies the grace period stored on the *old* container
when it recreates, so the deploy that first adds it stops the old gateway with
10s; merge that one when no session is in `processing` state
(`openclaw_session_queue_depth{state="processing"}` is 0).

## Applying the Docker builder cache cap (daemon.json)

`scripts/docker-builder-gc.sh` owns the repository's half of
`/etc/docker/daemon.json`: `builder.gc` (reserved and max used space at 50GB,
which dockerd reads as 50 GiB) and `live-restore: true`. `deploy.sh` runs it
with `--check` after every `up` and prints a red, report-only line while the
running daemon's ceiling is not that cap, live-restore is off, or the file is
missing (a yellow "undetermined" line when it cannot read the live policy at
all) - the deploy account has no sudo and dockerd reads `builder.*` at startup
only, so the deploy can see the drift but never close it. Closing it is one
operator sequence as `denys`, in this order:

```bash
sudo bash /srv/lifekit-stack/scripts/docker-builder-gc.sh    # writes the file only
sudo systemctl reload docker                                  # SIGHUP: picks up live-restore, nothing else
docker info --format '{{.LiveRestoreEnabled}}'                # must print true - do not go on until it does
sudo systemctl restart docker                                 # applies the cap; running containers stay up
bash /srv/lifekit-stack/scripts/docker-builder-gc.sh --check  # exit 0: Max Used Space 50GiB, live-restore true
```

`bash /srv/lifekit-stack/scripts/docker-builder-gc.sh --check` is read-only and
can be run at any later time to confirm the cap still holds.

Why the order matters: `live-restore` is on dockerd's SIGHUP reload list and
`builder.*` is not. A restart before live-restore reads `true` stops every
container on the box (this stack's `restart: on-failure` services do not come
back on their own; finance-sentry, devclaw, the dashboard and xui
go down with them). With live-restore active first, the restart is a no-op
for running containers.

What was proven (2026-09-20, throwaway `docker:29.5.2-dind` container on this
box - same engine version, swarm inactive, containerd image store, nothing
touched on the host daemon): a daemon started without live-restore picked it
up from the file on SIGHUP; a container started after that survived the
daemon's SIGTERM and restart with the same pid and start time and stayed
exec-able; the restarted daemon showed `Max Used Space: 50GiB` and
`Reserved Space: 50GiB` on the `All: true` rule, where the disk-scaled default
had shown 375.3GiB. Inferred, not proven: the same on this host's external
containerd (a systemd unit that never stops, the more favourable case than
dind's child containerd, which exited and still left the container running).
A `defaultKeepStorage`-only setting would have reported `Reserved Space:
50GiB` next to `Max Used Space: 375.3GiB` - a record that looks applied while
capping nothing, which is exactly what `--check` compares the ceiling for.

## Scheduled Docker image and build-cache prune

`scripts/docker-prune-policy.sh` installs `lifekit-docker-prune.timer`, which
runs nightly (03:30 with up to 30 minutes of random delay; `Persistent=true`
catches up a missed run) as root:

```bash
docker image prune -af --filter "until=336h"
docker builder prune -af --filter "until=336h"
```

Each prune prints its own `Total reclaimed space` line, tagged `[image]` or
`[builder]`; read them with `journalctl -u lifekit-docker-prune.service`. The
unit fails if either prune fails, after attempting both.

How it fits with the cache cap above: the cap is a size ceiling, this is an age
floor. `until=336h` only touches build-cache records idle for two weeks
(BuildKit refreshes the last-used time on every cache hit), so it cannot evict
the working set the cap's rationale protects; it reclaims the long tail the cap
would otherwise hold until the ceiling is reached. Two consequences to expect:
`until` on images is the image's creation time, so an image older than 14 days
that no container (running or stopped) references is removed and pulled or
rebuilt on next use - for instance the image of a profile-gated service
while its container does not exist; and a build stage nobody has run in two
weeks re-runs.

`bootstrap-vps.sh` installs it (root): the script writes a root-owned copy to
`/usr/local/bin/lifekit-docker-prune.sh`, two units, and enables the timer. It
never prunes while installing and never touches `daemon.json` or dockerd.
`deploy.sh` runs `--check` after every `up` and prints a red, report-only line
until the box has converged; the deploy account has no sudo, so a change to the
script or units reaches the timer only on the next root apply:

```bash
sudo bash /srv/lifekit-stack/scripts/docker-prune-policy.sh           # install/refresh units, enable timer
bash /srv/lifekit-stack/scripts/docker-prune-policy.sh --check         # read-only; exit 0 once converged
sudo bash /srv/lifekit-stack/scripts/docker-prune-policy.sh --prune    # run both prunes now
```

To undo: `sudo systemctl disable --now lifekit-docker-prune.timer`, remove the
copy and the two unit files (paths printed by `--check`), then `sudo systemctl
daemon-reload`.

## Weekly Docker image and anonymous-volume prune

`scripts/docker-weekly-prune/` holds `lifekit-docker-weekly-prune.timer`
(Sundays 04:15 with up to 30 minutes of random delay; `Persistent=true` catches
up a missed run), run as root, same shape as the other host timers. It runs:

```bash
docker image prune -a -f --filter "label!=lifekit.keep=rollback"
docker volume prune -f
```

It never touches build cache (the nightly prune and the cache cap above own
that), networks or named volumes (`volume prune` without `--all` removes
anonymous volumes only), and it restarts neither dockerd nor a container. Read
what each half reclaimed with `journalctl -u lifekit-docker-weekly-prune.service`
(lines tagged `[image]` / `[volume]`).

**Rollback images.** `image prune -a` removes every image no container uses,
which includes `lifekit-openclaw:prev` (the [one-rollback
rule](#the-one-rollback-rule)). The gateway Dockerfile stamps
`LABEL lifekit.keep="rollback"` and `:prev` is a retag of the image that ran
the previous version, so the filter skips it. An image built before the label
existed does not carry it, and a label cannot be added to an existing image:
if any `*:prev` image lacks the label the script skips the image prune, still
runs the volume prune and fails the unit with the image names on stderr. The
label reaches `:prev` on the first version bump after it merges; until then
the unit stays red rather than delete the rollback image. To clear it sooner,
rebuild the running version from its commit (with the label) and tag it
`:prev`. The nightly `lifekit-docker-prune` above filters by age, not by
label, and does not honour the keep label.

**Operator step (install).** Merging does not install it, and
`bootstrap-vps.sh` does not call the installer; run on the box when asked to
enable it:

```bash
sudo bash /srv/lifekit-stack/scripts/docker-weekly-prune/install-docker-weekly-prune.sh
systemctl list-timers 'lifekit-docker-weekly-prune*'
```

Once installed, the host unit gauge covers the timer (`lifekit-*.timer`). To
undo: `sudo systemctl disable --now lifekit-docker-weekly-prune.timer`, remove
`/usr/local/bin/lifekit-docker-weekly-prune.sh` and the two unit files under
`/etc/systemd/system`, then `sudo systemctl daemon-reload`.

## Moving /tmp off RAM (agent scratch)

`scripts/tmp-scratch-policy.sh` owns the repository's half of getting
build/session scratch off a RAM-backed `/tmp` and onto disk, and retiring
scratch that finished tasks leave behind instead of letting it accumulate
until it breaks something:

- it masks the distro's default tmpfs-on-`/tmp` mount unit (`tmp.mount`), so
  `/tmp` is an ordinary directory on the root filesystem;
- an `/etc/tmpfiles.d` drop-in turns off `systemd-tmpfiles`' age-only
  cleanup of `/tmp`, which cannot tell whether a running task still uses an
  entry;
- `lifekit-tmp-scratch-sweep.timer` runs the script's `--sweep` daily, as
  root, from a root-owned copy the script installs in `/usr/local/bin` when
  applied as root - never from the deploy account's checkout, so a change to
  the sweep reaches the timer only on the next root apply (`--check` reports
  the copy stale until then). It removes a top-level `/tmp` entry only when no running process has a file
  under it open, as its working directory, or as its executable, and it holds
  no socket; an age backstop (a week without writes) narrows the candidates
  but never decides alone. If it cannot read every process's open files it
  removes nothing.

What this covers and what it does not: this repository controls where
scratch lives and can safely reclaim abandoned scratch, but it does not know
when an agent task ends - that signal belongs to the supervisor that starts
and ends tasks, not to host policy. Retiring a task's scratch the moment the
task ends is follow-up work owned by that supervisor; the sweep here is the
backstop for whatever it leaves behind.

`deploy.sh` runs the script with `--check` after every `up` and prints a red,
report-only line while the box has not converged - including while `/tmp` is
still live on tmpfs, which is the incident state even with the unit masked.
The deploy account has no sudo, so it can see the drift but never close it.

`bootstrap-vps.sh` applies the policy unattended: it writes the drop-in, the
sweep copy and units, enables the timer, and masks `tmp.mount`. Masking only prevents
future mounts - an already-mounted tmpfs `/tmp` stays on RAM until the next
boot. Moving it now is a deliberate operator sequence, because **stopping
`tmp.mount` discards everything on the tmpfs, including scratch that running
tasks are still using**. Finish or stop active agent work first:

```bash
sudo bash /srv/lifekit-stack/scripts/tmp-scratch-policy.sh   # drop-in + sweep timer, masks tmp.mount
sudo systemctl stop tmp.mount                                 # unmounts the tmpfs; its contents are gone
bash /srv/lifekit-stack/scripts/tmp-scratch-policy.sh --check  # exit 0 only once /tmp is no longer tmpfs
```

If the stop is refused because the target is busy, something still has files
open under `/tmp`: list the holders with `sudo fuser -vm /tmp`, finish or stop
that work, and retry. Do not force it with a lazy unmount - that hides files
still in use instead of freeing them. Rebooting also completes the move, since
the masked unit keeps `/tmp` off tmpfs at the next boot.

`bash /srv/lifekit-stack/scripts/tmp-scratch-policy.sh --check` is read-only
and can be run at any later time; `sudo bash
/srv/lifekit-stack/scripts/tmp-scratch-policy.sh --sweep` runs the sweep on
demand and prints what it removed or kept.

To undo: `sudo systemctl unmask tmp.mount && sudo systemctl start tmp.mount`
puts `/tmp` back on tmpfs; `sudo systemctl disable --now
lifekit-tmp-scratch-sweep.timer` stops the sweep; removing the drop-in, the
sweep copy and the two unit files (paths printed by `--check`) followed by `sudo systemctl
daemon-reload` restores the distro defaults.

## /tmp usage alert

`scripts/tmp-gauge/tmp-usage-gauge.sh` publishes `/tmp`'s size and available
bytes as node-exporter textfile metrics every 5 minutes, the same shape as
`scripts/quota-gauge/claude-quota-gauge.sh`. It exists because `/tmp` is
tmpfs (RAM-backed) and node-exporter's own filesystem collector excludes
tmpfs (`compose/docker-compose.yml` node-exporter
`fs-types-exclude`), so nothing else on the box reported it before this -
the gap behind the 2026-09-29 incident, where `/tmp` filled to 100% and
every Claude session on the box lost its tool output. The `box` rule group's
*`/tmp` is nearly full* (`rules.yml`, `tmp-filesystem-nearly-full`) fires
warning when free space drops under 20% for 5 minutes; the existing
*textfile metrics are stale* rule covers this gauge's file too, since its
query reads every file in the textfile directory generically.

**What the alert means:** `/tmp` is filling up. On this box the response is
the host cleanup job (a user crontab, outside this repo) that removes stale
scratch leftovers hourly - it reports root-owned leftovers it cannot remove
rather than deleting them, so a firing alert can mean genuine growth outdoing
that job's hourly pace, not only its absence. `df -h /tmp` and `du -sh
/tmp/* 2>/dev/null | sort -rh | head` on the box show what is filling it.

The gauge is a bootstrap-installed systemd timer (`tmp-usage-gauge.timer`,
every 5 minutes, running as the admin account that owns the textfile
directory), installed next to the quota and host-group gauges by
`scripts/bootstrap-vps.sh` (which runs `scripts/host-gauge/install-host-gauges.sh`).
To install or update it on the box, run that install script with sudo (it needs
no Tailscale variables and touches nothing else), then remove the old operator
crontab line so two writers do not share the file:

```bash
sudo bash /srv/lifekit-stack/scripts/host-gauge/install-host-gauges.sh
systemctl list-timers 'tmp-usage-gauge*' 'host-group-gauge*'
crontab -e   # delete: */5 * * * * bash .../scripts/tmp-gauge/tmp-usage-gauge.sh ...
```

The metric names (`tmp_filesystem_size_bytes`, `tmp_filesystem_avail_bytes`)
and the 5-minute cadence are unchanged, so the alert and the staleness rule
need nothing.

## Host group memory gauge

`scripts/host-gauge/host-group-gauge.sh` writes `host_group_memory_bytes` and
`host_group_memory_swap_bytes` per `group` (`operator`, `runners`, `os`) every 5
minutes from `host-group-gauge.timer`, installed by the same install script
as above, plus `host_vmstat_pswpin_pages_total` (the swap-in counter the pressure
alert rates). Groups and their budgets are in `docs/resource-budget.md`; the
*host memory is under pressure* alert reads them (it lists the top three groups).
`cat /var/lib/node_exporter/textfile/host_group.prom` shows the current values;
`systemd-cgtop -m` and `ps --sort=-rss` show who inside a group is growing.

## Fleet publisher

`scripts/fleet-publisher/fleet-publisher.sh` runs every minute from
`lifekit-fleet-publisher.timer`, as the admin account that owns the fleet homes
and the Lavish state. The unit's `FM_HOMES` lists the fleet homes (a unit that
still sets only `FM_HOME` publishes that one home). For each home it reads
`state/home-summary.json` (task names, states and reasons only; the script
refuses to publish a home whose summary ever matches a credential pattern),
joins each open decision to the URL of the open Lavish session that home's
captain-hold binding feeds it (`board_url`; else the session whose file lives
under the home's `data/<decision id>/`; else `null`), and copies finished
reports to `/var/lib/lifekit-fleet/reports/` (`report_url` on each landed item).
It merges the homes into one summary, tagging every task, hold and decision
with its `home_id` and listing the homes in `homes[]` (output contract in
`scripts/fleet-publisher/README.md`), and writes it atomically to
`/var/lib/lifekit-fleet/home-summary.json`, world-readable, for the dashboard
to bind-mount read-only. It also writes `fleet.prom` into the textfile
directory, totals over every home: `fleet_workers{state}`,
`fleet_decisions_open`, `fleet_oldest_decision_age_seconds` (from the home
ledger's `needs-decision` line for the decision, else its hold age),
`fleet_usage_limit_events_1h` (ledger lines naming a usage limit),
`fleet_summary_valid` (every home valid), `fleet_summary_generated_timestamp_seconds`
(the oldest home's), and per home `fleet_home_summary_valid{home}` and
`fleet_home_summary_generated_timestamp_seconds{home}`, plus
`fleet_publisher_last_success_timestamp_seconds`. Grafana's `box` dashboard has
a Fleet row for them. It also copies each home's validated
`data/fleet-metrics/daily.jsonl` rows into
`/var/lib/lifekit-fleet/metrics/daily.jsonl`, one row per (date, home) with the
last row winning (contract in `scripts/fleet-publisher/README.md`). Invalid rows
are logged to the unit's journal and skipped. The same run publishes the PC's
state and last-seen time (from the first home's cached secondmate summary),
per-item `started_at`, `landed_at`, `priority` and `ask` fields, and sanitised
`metrics/quota.jsonl` / `metrics/quota-snapshot.json` (contract in the README).
The new output appears only once the
installed script is refreshed: re-run the install command below (it
re-installs the script and units, idempotent).

**Alerts:** *fleet summary is stale* fires when the oldest home summary's
`generated_epoch` is over 15 minutes old (that home's watch loop died; the
per-home series names it); *textfile metrics are stale* covers a dead publisher
(`fleet.prom` not rewritten). A home whose summary is malformed or
credential-bearing is left out and listed unpublished, the rest still publish,
and the run exits nonzero, so *host systemd unit is down or failed* fires for
`lifekit-fleet-publisher.service`. When no home is readable it keeps the
previous files, so both stale rules fire rather than an empty fleet showing.

**Install** (operator; needs sudo, installs the script and units like the other gauges):

```bash
sudo FM_HOMES="<fleet home> [<fleet home> ...]" bash /srv/lifekit-stack/scripts/fleet-publisher/install-fleet-publisher.sh
systemctl list-timers 'lifekit-fleet-publisher*'
cat /var/lib/node_exporter/textfile/fleet.prom
```

`bootstrap-vps.sh` runs the same installer when `FM_HOMES` (or `FM_HOME`) is
set and skips it otherwise. Re-running the installer is how a home is added or
removed: it reinstalls the script and rewrites the unit's `FM_HOMES`. The `lifekit-*.timer` glob in the unit gauge picks the timer up with
no further change. Merge deploys the Grafana rule and dashboard.

## Host unit gauge

`scripts/unit-gauge/unit-gauge.sh` writes `host_unit_active{unit}` and
`host_unit_failed{unit}` every 5 minutes from `unit-gauge.timer` into the
textfile directory (`host_unit.prom`). Units: every `actions.runner.*`
service, `tailscaled`, `docker`, `containerd`, `cron`, `unattended-upgrades`,
the service of every `lifekit-*.timer`, and the services of three timers named
before that convention: `memory-sync`, `openclaw-config-sync` and
`memory-rotate`. For a timer, `active` tracks the
`.timer` (its oneshot service is inactive between runs by design) and `failed`
is the service's last `Result` not being `success`. A unit not installed on
the host is skipped, and so is one that is not enabled and not running unless it
has failed. If `systemctl` itself errors the script exits nonzero without
rewriting `host_unit.prom`, so `unit-gauge.service` goes failed and the
*textfile metrics are stale* alert fires. node-exporter's systemd collector
stays off.

The *host systemd unit is down or failed* alert (`host-unit-down-or-failed`,
`rules.yml`) fires warning when any unit is inactive or failed for 10 minutes.
`systemctl status <unit>` and `journalctl -u <unit> -n 50` show why. A failed
timer run stays reported until the next successful run; after fixing the cause,
`sudo systemctl reset-failed <unit>.service` clears it. A failed `memory-sync`
has its own response in "Memory-vault sync".

Adding a unit or a timer outside the `lifekit-*` naming means editing the
allowlists at the top of the script. A new `lifekit-*.timer` is picked up
automatically.

**Operator step (live install):** the installer writes `/etc/systemd/system`,
so a merge does not install it. Run on the box, as the admin account:

```bash
sudo bash /srv/lifekit-stack/scripts/host-gauge/install-host-gauges.sh
systemctl list-timers 'unit-gauge*'
cat /var/lib/node_exporter/textfile/host_unit.prom
```

The merge's deploy reloads Grafana provisioning, which loads the alert rule;
until the timer is installed the rule sees no data and stays quiet.

## Weekly release gauge

Each lifekit-hq repo's `.github/workflows/weekly-release.yml` (finance-sentry, lifekit-stack,
lifekit-dashboard, devclaw; cron Mondays 00:07 UTC plus `workflow_dispatch`) merges the open
`autorelease: pending` Release Please PR after `gh pr checks --watch --fail-fast`. A failing or
missing check ends the run red with the PR still open; no pending PR is a green no-op. Those runs
are on GitHub-hosted runners that cannot reach the box, so the box reads GitHub instead.

`scripts/release-gauge/lifekit-release-gauge.sh` runs every 5 minutes from
`lifekit-release-gauge.timer` as the admin account, on that account's existing `gh` login
(read-only; no new token). Per repo it reads the latest completed Weekly Release run and, when that
run did not succeed, the open pending release PR, and writes
`weekly_release_blocked{repo,pr_url,run_url}` into the textfile directory
(`weekly_release.prom`): 1 when the run did not succeed and the PR it should have merged (one
created before the run started) is still open, else 0 with empty URLs. `RELEASE_REPOS` overrides
the repo list. Any read failure (an expired `gh` login, GitHub down) exits nonzero without
rewriting the file, so *host systemd unit is down or failed* and then *textfile metrics are
stale* report it instead of a blocked release reading as healthy.

The *Weekly Release stopped without merging* alert (`weekly-release-not-merged`, `rules.yml`)
fires warning per repo while the gauge reads 1; the message names the repo, the release PR and the
run. Fix the PR's checks (the run log names the cause), then
`gh workflow run weekly-release.yml -R <owner/repo>`; the alert resolves once a run succeeds or the
PR is merged. The default notification policy repeats a still-firing alert every 4h.

**Operator step (live install):** the installer writes `/etc/systemd/system`, so a merge does not
install the timer. Run on the box, as the admin account:

```bash
sudo bash /srv/lifekit-stack/scripts/host-gauge/install-host-gauges.sh
systemctl list-timers 'lifekit-release-gauge*'
cat /var/lib/node_exporter/textfile/weekly_release.prom
```

The merge's deploy reloads Grafana provisioning, which loads the alert rule; until the timer is
installed the rule sees no data and stays quiet.

## Runner restart drop-ins

`svc.sh` installs every `actions.runner.*` unit with `Restart=no`, so a crashed
runner stays down until someone notices (the *host systemd unit is down or
failed* alert is the backstop). `scripts/runner-restart/install-runner-restart.sh`
(called by `scripts/bootstrap-vps.sh`) writes
`/etc/systemd/system/<unit>.d/restart.conf` for each installed runner unit with
`Restart=on-failure` and `RestartSec=10`, then runs `daemon-reload`. It is
idempotent and does not restart a running runner; the setting applies from the
next start.

**Operator step (live install):** the drop-in lives in `/etc/systemd/system`,
so a merge does not install it. Run on the box, as the admin account:

```bash
sudo bash /srv/lifekit-stack/scripts/runner-restart/install-runner-restart.sh
systemctl show 'actions.runner.*' -p Id -p Restart
```

Needs no other setup variables. Every runner unit should report
`Restart=on-failure`.

## Host firewall (nftables)

`scripts/host-firewall.sh` owns one nftables table, `inet lifekit`
(ruleset: `scripts/firewall/lifekit-firewall.nft`), loaded at boot by
`lifekit-firewall.service`:

- **Host input:** policy drop. Open: loopback, established/related flows,
  everything arriving on `tailscale0` or a Docker bridge (`docker0`, `br-*`,
  `lifekit-edge`), Tailscale's WireGuard port (udp/41641), tcp/80 and tcp/443
  for the public edge, rate-limited ping, and the ICMP/ICMPv6
  traffic the network needs. SSH is tailnet-only; the provider's web console is
  the break-glass.
- **Published container ports:** a forward-hook chain admits new flows into
  containers only from loopback, the tailnet, a Docker bridge, or - for
  connections dialled to host port 80 or 443 - the `lifekit-edge` bridge. It
  plays the part of a `DOCKER-USER` rule, but in its own table: rules written
  into Docker's iptables-nft chains with `nft` break `iptables` there, are lost
  on every reboot, and that chain does not exist in Docker's nftables mode.

It composes with Docker's and tailscaled's chains rather than replacing them:
every base chain on a hook runs, an accept in one does not skip the others,
and a drop in any one is final
([Docker with nftables](https://docs.docker.com/engine/network/firewall-nftables/)).
The script never runs `nft flush ruleset`, and nothing else on this box may
either. Debian's `nftables.service` does, in stock `/etc/nftables.conf` and in
its `ExecStop`, and that drops Docker's NAT until dockerd restarts. Keep
`nftables.service` disabled and `ufw` masked; `--check` (a report-only line in
every deploy) flags either one. A compose network given a custom bridge name
other than `lifekit-edge` (`com.docker.network.bridge.name`) needs a line in
the ruleset's `inside` chain.

`scripts/tests/test_host_firewall.py` loads the ruleset into a throwaway
user + network namespace beside a Docker-like table and probes it with real
traffic.

**Operator step (live cutover).** A merge does not apply it (`/etc`, root).
Run on the box as the admin account, **from an SSH session over the tailnet**
(`ssh <user>@<tailnet-name>`), never over the public address; `--trial`
refuses a session that is not from the tailnet. Keep a second way in at hand:
the provider's web console.

```bash
cd /srv/lifekit-stack            # after the merge has deployed

# 1. For the record (the trial also saves both under
#    /var/lib/lifekit/firewall/baseline-<UTC time>/):
sudo nft list ruleset; sudo iptables -S

# 2. Load the ruleset live with a 5-minute automatic rollback. Nothing is
#    persisted yet; a reboot also undoes it.
sudo bash scripts/host-firewall.sh --trial 300
```

3. Keep that session open. **Open a new SSH session over the tailnet** - that
   one getting in is the real test - and check, inside the window:

   ```bash
   sudo nft list table inet lifekit                        # the table is loaded
   systemctl list-timers lifekit-firewall-rollback.timer   # rollback still armed
   tailscale status                                        # peers still listed
   docker ps --format '{{.Names}} {{.Status}}'             # containers still up
   ```

   From a laptop on the tailnet, open a tailnet-served page (Grafana, the
   dashboard). From a device **off** the tailnet (a phone on mobile data with
   Tailscale off), confirm public SSH is gone: `nc -vz -w5 <public-ip> 22`
   must time out, and so must `nc -6 -vz -w5 <public-ipv6> 22`.

4. All good - persist it. This cancels the rollback, installs
   `/etc/lifekit/firewall.nft` and the unit, and enables it for boot:

   ```bash
   sudo bash scripts/host-firewall.sh --confirm
   bash scripts/host-firewall.sh --check       # exit 0: matches, enabled, active
   ```

   Anything wrong - roll back at once with
   `sudo bash scripts/host-firewall.sh --rollback`, or do nothing: the timer
   restores the previous state when the window ends. A confirm after the
   window has lapsed refuses; run the trial again.

**Break-glass (locked out).** From the provider's web console,
`sudo nft delete table inet lifekit` opens the box until the next boot, and
`sudo systemctl disable --now lifekit-firewall.service` keeps it off across
reboots too. Docker's and Tailscale's tables are untouched either way.

**Later ruleset changes** go through the same trial and confirm. A plain
`sudo bash scripts/host-firewall.sh` installs and reloads with no rollback;
it is meant for the fresh box `bootstrap-vps.sh` sets up.

## Identity provider (Logto)

Compose project `identity` (`compose/identity/`) is the org sign-in: Logto
1.44 as the OIDC provider, with its own Postgres 17. Every app on the box
(lifekit dashboard, devclaw, finance-sentry, Grafana) signs in against it:
the dashboard and the devclaw console through the
[tailnet sign-in gate](#tailnet-sign-in-gate), Grafana as its own client
([Grafana sign-in through Logto](#grafana-sign-in-through-logto)), and
finance-sentry as its own client in a separate change.
`deploy.sh` brings the project up once `LOGTO_DB_PASSWORD` is in the rendered
env file, and until then skips it with a yellow warning.

| | URL | Reach |
| --- | --- | --- |
| Sign-in pages, OIDC (issuer `https://<name>:3001/oidc`) | `https://<name>:3001` | tailnet |
| Admin console | `https://<name>:3002` | tailnet |

`<name>` is this host's tailnet name (`<host>.<tailnet>.ts.net`). Both ports
bind to loopback; Tailscale Serve terminates HTTPS on the tailnet name and
proxies to them. `deploy.sh` derives both URLs from `tailscale status`, so an
explicit `IDENTITY_ENDPOINT` / `IDENTITY_ADMIN_ENDPOINT` in the master file is
only for moving the issuer.

**Why dedicated Serve ports, not a path on 443.** Port 443 on this node
carries a Funnel (the public edge), so anything served there is on the public
internet; the identity provider stays tailnet-only until a public domain is
decided. Ports 8443 and 10000 are avoided too: they are the other
Funnel-capable ports, one `--funnel` away from public. Logto also expects to
own its origin (it does not run cleanly under a path prefix), and its issuer
URL is baked into every token and client registration. When the public domain
lands the issuer moves once, and every client registers again against it.

**Operator steps, once** (admin account; the deploy account cannot run them):

```bash
# 1. After merge, render the committed LOGTO_DB_PASSWORD into the env file,
#    then redeploy:
sudo bash scripts/secrets/render-stack-env.sh
# 2. Publish both ports on the tailnet. Serve only - never `tailscale funnel`
#    for these ports:
sudo tailscale serve --bg --https=3001 http://127.0.0.1:3001
sudo tailscale serve --bg --https=3002 http://127.0.0.1:3002
tailscale serve status        # both listed as "(tailnet only)"
# 3. The nightly dump timer (bootstrap-vps.sh installs it on a new box):
sudo LIFEKIT_USER=lifekit bash /srv/lifekit-stack/scripts/identity-backup/install-identity-backup.sh
```

`LOGTO_DB_PASSWORD` is already in `secrets/lifekit.env.sops`. Only a rebuild
from scratch creates it again, with `sops set --input-type dotenv
--output-type dotenv secrets/lifekit.env.sops '["LOGTO_DB_PASSWORD"]'
"\"$(openssl rand -hex 32)\""` (captain's age key).

Every deploy then prints a report-only "identity provider (host facts)"
block (`scripts/deploy-identity.sh check`): each Serve port published
tailnet-only, and the sign-in mode `SignIn`. A red `✗` there means a step
here is not done yet. A Funnel on either port is called out by name.

**First boot, in the admin console** (`https://<name>:3002`). Do it as soon
as the project first turns healthy: until the admin exists, anyone on the
tailnet who opens the console can create it.

1. Create the admin account. Logto closes admin sign-up after the first one.
   Keep the password in KeePassXC.
2. **Sign-in mode.** *Sign-in experience > Sign-up and sign-in*: turn user
   registration off, so the mode is `SignIn` (members are created by the
   admin, below). The deploy's report line checks it.
3. **Google.** In Google Cloud, create an OAuth client of type *Web
   application*. Then *Connectors > Social connectors > Add > Google*: paste
   its client id and secret. Copy the connector's redirect URI,
   `https://<name>:3001/callback/<connectorId>`, into the Google client's
   *Authorized redirect URIs*. Add Google to the sign-in page. Under *Social
   sign-in* turn on automatic account linking, so a member's first Google
   sign-in attaches to the user created for that address instead of
   prompting.
4. **Passkeys.** *Sign-in experience > Sign-up and sign-in > Passkey
   sign-in*: on, with the passkey button and autofill. A member adds a
   passkey after their first sign-in. Passkeys are bound to the host name,
   so they need registering again after the issuer moves.
5. **Email: leave it off** until the captain has made a Google app password
   ([Email sign-in (Gmail SMTP)](#email-sign-in-gmail-smtp)). With a verified
   sending domain instead, use Resend: create an API key, then *Connectors >
   Email and SMS > SMTP*: host `smtp.resend.com`, port `465`, `secure` on,
   username `resend`, password the API key, `fromEmail` an address on the
   verified domain. Send the test email from the connector page. Finally add
   *Email address* (verification code) as a sign-in identifier. The API key
   lives in Logto's database, not in the secrets files; rotate it in the
   connector page.

**Creating a member.** *User management > Add user*. Use the same email as
the finance-sentry invite, so the two accounts meet on one address. Hand the
generated password over out of band, or leave it unused and let them sign in
with Google.

**Registering an OIDC client** (finance-sentry, oauth2-proxy, Grafana, each
in its own change). A Traditional web app with a name and the app's redirect
URI(s), for example `https://<name>:3000/login/generic_oauth` for Grafana
(the sign-in gate's own values are in its section below), created with
`logto-admin.py ensure-app` ([Logto admin through the Management
API](#logto-admin-through-the-management-api)) or in the console under
*Applications > Create application > Traditional web*. The app then needs:

- issuer `https://<name>:3001/oidc`; discovery is at
  `https://<name>:3001/oidc/.well-known/openid-configuration`;
- the application's client id and secret (the secret into the app's secrets
  boundary, inventoried like any other);
- scopes `openid profile email`.

**Upgrading Logto.** Bump the image tag in `compose/identity/docker-compose.yml`
through a PR. Take a dump first (`sudo systemctl start
lifekit-identity-backup.service`). On start the container applies the schema
alterations up to the new version, so a rollback to the old tag needs the
pre-upgrade dump restored, not just the old tag.

**Backup and restore.** `lifekit-identity-backup.timer` dumps the whole
cluster nightly at 02:45 (`pg_dumpall`: Logto's per-tenant roles as well as
its data). The dump goes to
`/srv/openclaw/backups/identity/identity-<UTC stamp>.sql.gz`, 0600, newest 14
kept. A failed run turns the oneshot failed, and the host unit gauge reports
it. To restore, as the deploy account, from `/srv/lifekit-stack`:

```bash
docker stop identity-logto-1
docker rm -f identity-postgres-1 && docker volume rm identity_postgres_data
docker compose -p identity --env-file /srv/lifekit-secrets/stack.env \
  -f compose/identity/docker-compose.yml up -d --wait postgres
gunzip -c /srv/openclaw/backups/identity/identity-<stamp>.sql.gz \
  | docker exec -i identity-postgres-1 psql -q -U logto -d postgres
bash scripts/deploy.sh      # brings logto back; its log says "Seeding skipped"
```

The restore prints `role "logto" already exists` and `database "logto"
already exists`: the fresh volume created both, and those lines are expected.
Wait for `up --wait` to return before restoring. A restore into a Postgres
still starting fails.

## Email sign-in (Gmail SMTP)

Members sign in with a code mailed to their address, sent through Logto's SMTP
connector over `smtp.gmail.com`. Gmail needs no verified sending domain, which
is why it is the first path; a domain and Resend replace it later (the
connector is one object, re-pointed then). Mail is only for members who
already exist: the sign-in mode stays `SignIn`, so nobody registers by email.

**The one input: a Google app password.** From the sending Google Account, with
2-Step Verification on: *Security > 2-Step Verification > App passwords*,
name it for this stack, copy the 16 letters (Google shows them in four groups;
the spaces are dropped). Put it in the master file's reserved slot
`PARKED_LOGTO_SMTP_APP_PASSWORD` (the slot exists, empty):

```bash
scripts/secrets/edit.sh master    # fill PARKED_LOGTO_SMTP_APP_PASSWORD, save
```

Merge that change; nothing needs a render or a redeploy: no service
interpolates it, and the `PARKED_` prefix keeps the render from writing it
out. Gmail sends as the account's own address (a different `--from-email` is
rewritten unless it is a verified "Send mail as" alias of that account), and a
Gmail account has a daily sending limit, plenty for sign-in codes.

**Apply** (admin account, Management API credentials as in the next section;
`<m2m>.sops` is the operator's M2M file, `<address>` the Gmail address):

```bash
# 1. The connector, from the master file's slot:
LOGTO_SMTP_PASSWORD="$(sops decrypt --extract '["PARKED_LOGTO_SMTP_APP_PASSWORD"]' \
    --input-type dotenv --output-type dotenv secrets/lifekit.env.sops)" \
  sops exec-env <m2m>.sops 'python3 scripts/identity/logto-admin.py \
    set-email-connector --user <address> --from-name "<sender name>"'
# 2. A real message through the stored connector - the proof the path works:
sops exec-env <m2m>.sops 'python3 scripts/identity/logto-admin.py \
  send-test-email <address>'
# 3. Only after the test mail arrives: turn email-code sign-in on, keeping
#    each member's password (and username) sign-in as it is:
sops exec-env <m2m>.sops 'python3 scripts/identity/logto-admin.py \
  set-sign-in-exp --sign-in-identifiers email username --code-sign-in-identifiers email'
```

Adjust step 3 to the methods in force: `--sign-in-identifiers` and
`--code-sign-in-identifiers` together replace the whole methods list (`email`
in both means password or code for email). Logto refuses step 3 while no email
connector exists. `set-email-connector` is idempotent and reports changed
fields by name only; a changed password re-applies the same way, and it
reports `already set` when nothing differs. The connector is always
`smtp.gmail.com:465` over TLS.

**Verify.** `send-test-email` succeeds and the message arrives (look in spam
the first time); then sign in on the sign-in page with *Email address*,
receive the code, and finish. A failed send names the SMTP error: `Invalid
login` is a wrong or revoked app password, `ENOTFOUND` a wrong host, a timeout
no route from the Logto container to `smtp.gmail.com:465`.

**Reverse.** `logto-admin.py undo` takes the newest change back: the
sign-in methods, then the connector (a create is undone by deleting it). An
update of an existing connector has no undo, because the previous config holds
the previous password: run the command again with the old values.

**Rehearsal.** `scripts/identity/rehearse-logto-admin.sh` runs this against a
throwaway Logto with an unreachable SMTP host (`smtp.invalid`, through the
script's test-only `LOGTO_ADMIN_TEST_ONLY_SMTP_HOST`): connector
create, update and no-op, Logto's refusal of email-code sign-in without a
connector, the sign-in methods as stored, the test-send route, and the undo.
A real send needs the real password, so it is the operator step above.

## Logto admin through the Management API

The admin-console steps below (roles, role assignments, OIDC clients and
their redirect URIs) are scripted: `scripts/identity/logto-admin.py` calls
Logto's Management API as the machine-to-machine app `firstmate-ops`, so they
need no console session. Every command is idempotent (it reads first and
changes only what differs) and never prints a secret.

**Bootstrap, once** (admin account, docker group, from a checkout). The app
is inserted straight into Logto's database, in tenant `default`, holding the
seeded role "Logto Management API access" (scope `all` on
`https://default.logto.app/api`). One transaction; re-running changes
nothing:

```bash
sudo systemctl start lifekit-identity-backup.service    # a dump first
bash scripts/identity/logto-m2m-bootstrap.sh apply --env-out ~/m2m.env
bash scripts/identity/logto-m2m-bootstrap.sh status
```

`--env-out` writes `LOGTO_M2M_APP_ID` and `LOGTO_M2M_APP_SECRET` to a new
0600 file, straight from Postgres. It is operator tooling, kept out of every
git repo: encrypt it to the operator's own SOPS file and delete the plain
copy (`sops encrypt --input-type dotenv --output-type dotenv ~/m2m.env >
<file>.sops && shred -u ~/m2m.env`). Logto needs no restart: the app works
as soon as the transaction commits.

**Use.** Credentials come from the environment; `LOGTO_ENDPOINT` defaults to
`http://127.0.0.1:3001`, Logto's loopback port:

```bash
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py token-check'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py ensure-role admin'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py assign-role <email> admin'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py create-user <email> [--name <text>]'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py ensure-app "<name>" \
  --redirect-uri <uri> --post-logout-uri <uri> --secret-file ~/<name>.secret'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py set-redirects <app> \
  --redirect-uri <uri> --post-logout-uri <uri>'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py set-sign-in-exp \
  --sign-in-identifiers email username --primary-color <#hex> --logo-url <url>'
```

`assign-role` takes a user id, primary email or username. `create-user` makes
a passwordless user with that primary email (it signs in with Google or an
emailed code), only when no user has the email. `ensure-app`
creates a Traditional web app, or brings an existing one's URIs to the given
lists. `set-redirects` replaces the list it is given and keeps the other.
`--secret-file` writes the app's client secret to a new 0600 file for
`sops set` to read (`"\"$(cat ~/<name>.secret)\""`), then `shred -u` it.
`set-sign-in-exp` changes only the fields given (logo,
dark logo, favicon, dark favicon, colors, `--sign-in-identifiers`,
`--sign-up-identifiers`); the image options take an http(s) URL or an inline
`data:image/svg+xml;base64,...` URI, which needs no hosting.
`email username` signs in with either, by password;
`--code-sign-in-identifiers email` adds the emailed code
([Email sign-in (Gmail SMTP)](#email-sign-in-gmail-smtp), with
`set-email-connector` and `send-test-email`). It never sends the sign-in mode
or the social sign-in settings.
`--clear-terms-links` empties its Terms/Privacy links. `set-app-sign-in-exp <app>` sets one
application's own name, logo and colors ([Sign-in page branding](#sign-in-page-branding)).

**Reversing a change.** Each change appends a line to the ledger
(`~/.local/state/lifekit/logto-admin.ledger.jsonl`, or `LOGTO_ADMIN_LEDGER`):
the ids it created or touched, and the inverse request. `logto-admin.py
ledger` lists it; `logto-admin.py undo [N]` sends entry N's inverse (default:
the newest not yet undone): delete the user, role or app it created, take the role
back, restore the previous redirect URIs, or restore the previous
sign-in experience objects (tenant or per application), or delete the email
connector it created.

**Removing the app** ends the delegation:

```bash
bash scripts/identity/logto-m2m-bootstrap.sh remove
```

New tokens stop at once. A token already issued stays valid until it
expires, at most an hour, so a suspected leak also waits that hour out (or
restarts Logto, which does not end it either: the token is a signed JWT).

**Rehearsal.** `bash scripts/identity/rehearse-logto-admin.sh` runs the
bootstrap, every admin command, `undo` and the removal against a throwaway
Logto (its own compose project, `logto-rehearsal`, with its own database,
the same images as `compose/identity/`, loopback ports 13001/13002), then
tears it down. It never touches the `identity` project. Run it after a Logto
upgrade, before the bootstrap or the script is used against the new
version: the bootstrap writes Logto's tables directly.

### Inviting a person: `people invite` / `people status`

`scripts/people/people.py` is the owner-facing wrapper over the commands
above (the Logto half; the tailnet half and the mail are the next sections). `invite`
ensures the Logto user (primary email, no password: they sign in with Google,
or an emailed code once the connector is live), ensures and assigns one role
per right, and prints the invite email for you to send. It sends mail only
with `--send` or `--resend` ([Sending the invite email](#sending-the-invite-email)).
Rights live in `scripts/people/rights.json` (the role name is the Logto role;
`PEOPLE_RIGHTS` points at another catalog). It is idempotent, an unknown right
is refused before any write, and every write lands in the `logto-admin.py`
ledger, so `logto-admin.py undo` reverses it (role grant, then role, then user).
It uses the same M2M credentials and ledger as `logto-admin.py`; the `all`
scope on the Management API covers creating users.

```bash
sops exec-env <m2m>.sops 'python3 scripts/people/people.py invite <email> \
  --right backstage [--right finance-sentry] --host <host> [--invite-url <link>]'
sops exec-env <m2m>.sops 'python3 scripts/people/people.py status [<email>]'
```

`--host` (or `PEOPLE_HOST`) is the tailnet DNS name the products are served on;
it is not stored in the repo. Without `--invite-url` the email carries a marked
placeholder where the Tailscale invite link goes. `status` lists each user's
roles and suspended flag, and flags drift: a user with no product role (the
`admin` platform role is exempt), or a suspended user who still holds one.
The rehearsal script runs `invite` and `status` on the throwaway Logto too.

### People: the tailnet half

`people.py invite` and the new `people.py sync` also drive the tailnet policy,
so a person reaches exactly the products they hold a right for. The Logto roles
are the source of truth; the policy's tailnet groups are a projection of them,
one `group:<right id>` per catalog right. The tool edits **only** the lines
between a `BEGIN`/`END people-managed` marker pair inside `groups` (every other
byte of the hand-written policy, comments included, stays), validates the
result, and writes it with `If-Match` (a concurrent edit gets one re-read and
retry). It also refuses to write while a policy grant lets a managed group reach
a `never_ports` port of `scripts/people/rights.json` (the admin surfaces), and
`status` reports the same.

```bash
sops exec-env <people>.sops 'python3 scripts/people/people.py sync [--dry-run]'
sops exec-env <people>.sops 'python3 scripts/people/people.py invite <email> \
  --right backstage --host <host>'
```

`<people>.sops` stands for whatever SOPS file holds the `LOGTO_M2M_*` and the
`TAILSCALE_*` variables below; they are read from the environment only and
never printed.

- `invite` adds the email to `group:<right>`; if the email is not yet a tailnet
  member it creates a user invite (no email field, so Tailscale sends nothing)
  and puts the link in the email draft. With no user token, or one Tailscale
  rejects, it prints "create an invite link in the admin console and paste it":
  create the link there (Users > Invite), then re-run with `--invite-url <link>`
  to render the email with it. Re-running for a non-member creates another
  invite, since invites carry no email to match.
- `sync` rebuilds every catalog group from the Logto roles (a suspended user is
  in none) and writes only when something differs. Groups in the block that the
  catalog does not know are left alone and reported. Run it after any role
  change made by hand in the Logto console, or on a timer.
- `status` adds each person's tailnet membership (devices, last seen) and
  groups, and drift: a role without its group, a group without its role, a
  right with no grant for its port or the sign-in port, a policy with no
  `tests`, and a tailnet member no Logto user matches. Tailscale does not say
  which login accepted an invite, so a person who accepts under another account
  than the invited email shows up as unmatched: the email tells them to sign in
  with the invited address.
- Without the OAuth client every tailnet step prints "tailnet: not configured"
  and is skipped; `invite --no-tailnet` skips it on purpose. `revoke` instead
  exits non-zero (the Logto half still runs): the network is not cut until the
  client is set and `revoke` is run again.

**One-time operator step (owner, live: changes who can reach what).** Do it in
the admin console policy editor (Access controls) with *Preview* before saving.
`<host>` is the box's MagicDNS name and `<ip>` its tailnet address
(`tailscale ip -4` on the box).

1. A `hosts` alias, so grants and tests read well: `"hosts": { "lifekit-vps": "<ip>" },`
2. The marker pair inside `groups`, exactly these comment lines (the group
   lines between them are written by the tool; start them empty):

   ```hujson
   "groups": {
     // BEGIN people-managed: written by `people sync`, hand edits are overwritten
     "group:finance-sentry": [],
     "group:backstage": [],
     // END people-managed
   },
   ```

3. One static grant per right, from its group to the alias on the right's port
   plus every `always_ports` entry (the Logto sign-in the browser is
   redirected to). Never one on a `never_ports` port:

   ```hujson
   { "src": ["group:backstage"], "dst": ["lifekit-vps"], "ip": ["tcp:<right port>", "tcp:<always port>"] },
   ```

4. A `tests` block, so a bad edit cannot save: per group accept the right's
   port and deny every `never_ports` port (a test takes one port per entry):

   ```hujson
   "tests": [
     { "src": "group:backstage", "accept": ["lifekit-vps:<right port>"],
       "deny": ["lifekit-vps:<never port>", "lifekit-vps:<another never port>"] },
   ],
   ```

5. Save, then check with the credentials below: `people.py status` should show
   no `no grant` / `no tests block` drift, and `sync --dry-run` no error.

**The credentials** (both ride in a SOPS file the people tool alone reads, never
in OpenClaw or agent env; the first live use is the owner's step, nothing here is
placed by the repo):

- *OAuth client* (`TAILSCALE_OAUTH_CLIENT_ID`, `TAILSCALE_OAUTH_CLIENT_SECRET`):
  admin console > Settings > OAuth clients > Generate, scopes **Policy File**
  (`policy_file`), **Users** read (`users:read`) and **Devices Core** read
  (`devices:core:read`), no tags. It does not expire and mints one-hour tokens.
  It runs `sync`, `status`, `revoke` and the policy half of `invite`.
- *User token* (`TAILSCALE_USER_TOKEN`), optional: only a user-owned API token
  can create invites (an OAuth client cannot). Settings > Keys > Generate
  access token, the shortest expiry that covers the invite (1 day is plenty),
  from an Owner or Admin login. It carries that user's full power, so put it in
  place only for the invite and remove it after; the tool falls back to the
  paste-a-link step when it is absent or expired.
- `TAILSCALE_TAILNET` (default `-`, the credential's own tailnet) and
  `TAILSCALE_API_URL` (default `https://api.tailscale.com`) are overrides for
  rehearsals; leave them unset on the box.

Tests: `scripts/tests/test_people_tailnet.py` against fake Logto and Tailscale
APIs (ETag conflict, idempotent sync, the never-ports refusal, the missing
token path). The live API is not part of any test or CI step.

### People: revoke and drift alerts

`people.py revoke <email> [--right <id>]... [--dry-run] [--suspend-tailnet-user]`
takes access away. The tailnet half runs first, because it is the network cut:
the email leaves `group:<right>` for each right (every right without `--right`)
through the same marker-block, validate and `If-Match` path as `sync`. Then the
Logto role for each right is removed, and the Logto user is **suspended** when no
product role remains and no platform role (`admin`) is held. Each Logto write
lands in the `logto-admin.py` ledger with undo data, plus a closing
`people-revoke` entry. Re-running finds nothing to do; `--dry-run` prints
`would ...` lines and writes nothing. A tailnet failure does not stop the Logto
half; revoke then exits 1 with "run revoke again", and the re-run finishes it.
A policy with a never-grant drift never blocks a revoke. Inviting a revoked
person again unsuspends them, but only when the ledger shows `people revoke`
made the suspension; a user suspended any other way is refused (exit 1, nothing
written) until someone unsuspends them in Logto.

```bash
sops exec-env <people>.sops 'python3 scripts/people/people.py revoke <email> --dry-run'
sops exec-env <people>.sops 'python3 scripts/people/people.py revoke <email>'
```

The Tailscale user and devices are never deleted. `--suspend-tailnet-user` (all
rights only) also suspends the Tailscale user, which stops their devices; it is
reversible in the admin console and needs the OAuth client's `users` write
scope (the client in [People: the tailnet half](#people-the-tailnet-half) has
read only), so add that scope before using the flag. Without it the network cut
is the group removal alone, which also takes the person off every product port.

`people.py status --notify` posts what `status` finds to notify-relay
(`docs/message-format.md`), so drift reaches the owner without anyone running
`status`:

- one `act` envelope (source `people`, subject `drift`) listing every drift line:
  a Logto user with no product role, a role without its group or the reverse, a
  right with no grant, a policy with no `tests`, a tailnet member no Logto user
  matches. No drift, no message.
- one `wait` envelope (subject `tailnet-token`) when `TAILSCALE_USER_TOKEN`
  expires within 14 days, has expired, is revoked, or its expiry cannot be read.
  The expiry comes from the token's own key record (`GET .../keys/<id>` with the
  token itself), so a tailnet with no token set checks nothing. Fix: generate a
  new token for `<people>.sops`, or remove the line when no invite is pending.

It posts through `scripts/ledger-persona/drift-alert.py`'s transport (`docker
exec` into the notify-relay container), so it must run as the account that can
use docker on the box. A relay that refuses the envelope exits 1. A timer
re-reports unresolved drift on every run. **Operator step, not installed by the
repo**: schedule it, for example a daily systemd timer whose service runs

```text
sops exec-env <people>.sops 'python3 /srv/lifekit-stack/scripts/people/people.py status --notify'
```

then check one run with `systemctl start` and the journal. Confirm live, before
relying on the token warning, that a user-owned API token can read its own key
record; if Tailscale answers 403 the warning reads "cannot check
TAILSCALE_USER_TOKEN" instead of a date. Tests:
`scripts/tests/test_people_revoke.py` against fake Logto, Tailscale and
notify-relay (a docker stand-in); nothing runs against the live services.

### Sending the invite email

`people.py invite <email> --right <id> --send` mails the one invite email
(`scripts/people/invite-email.txt`, rendered only for the chosen rights: install
Tailscale, accept the invite with the invited address, turn on "Use Tailscale
DNS", each product's URL and hint, sign in with Google, troubleshooting). It goes
through Gmail SMTP (`scripts/people/mailer.py`, stdlib `smtplib`, STARTTLS on
`smtp.gmail.com:587`) from the owner's own address. The tool mails the invitee
only; Tailscale's own invite email is avoided because the invite is created
without an `email` and its link is embedded in ours.

- `--send` does the whole invite and then mails. `--resend` is for someone
  already invited: it writes nothing to Logto or the policy, requires the Logto
  user to hold each given right, mints a fresh tailnet invite link if the email
  is not yet a tailnet member (or takes `--invite-url`), and mails it.
- **No app password set** (today): `--send` prints the draft and a `mail not
  configured` line and exits 0, so the invite itself never fails on mail. The
  same happens, with a `not sent` line, when the email would still carry the
  invite-link placeholder (no `TAILSCALE_USER_TOKEN` and no `--invite-url`).
- A failed send exits 1 after the invite is done; re-run with `--resend`.

**The one-time owner step** (not done by the repo): the same Google app
password as [Email sign-in (Gmail SMTP)](#email-sign-in-gmail-smtp). From the
sending Google Account turn on 2-Step Verification, then *Security > 2-Step
Verification > App passwords*, name it for this stack, and put the 16 letters in
the master file's slot `PARKED_LOGTO_SMTP_APP_PASSWORD` (`scripts/secrets/edit.sh
master`). The people tool reads it from the environment of its SOPS file, so
add to `<people>.sops`:

```text
PEOPLE_SMTP_USER=<sending Gmail address>
PEOPLE_SMTP_PASSWORD=<the app password>
PEOPLE_SMTP_FROM_NAME=<optional display name>   # optional
```

Confirm Google still offers app passwords on the account before relying on it.
`PEOPLE_SMTP_HOST`, `PEOPLE_SMTP_PORT` and `PEOPLE_SMTP_CA_FILE` exist only for
rehearsals against a local server. Tests:
`scripts/tests/test_people.py` against a socket fake SMTP server with STARTTLS.

### People serve

`scripts/people/serve.py` is the people tool as a small JSON API, for the
lifekit-dashboard People page (the page is built in that repo). The dashboard
holds no Logto, Tailscale or SMTP secret: it calls this service, which runs the
same functions as `people.py` (so Logto, tailnet and mail behave exactly as on
the CLI) and holds the credentials. It is compose service `people-serve`
(`compose/docker-compose.yml`, `compose/people-serve/`), contract `none`, on the
internal network `lifekit-people` (no egress, no host port) plus `default` for
its own egress. The dashboard reaches it at `http://people-serve:8095`.

**It is off.** The service sits behind the compose profile `people`, so the
merge deploy's `up -d` neither builds nor starts it, and `deploy.sh` only
checks its contract label. Turning it on needs the owner's word (it puts real
secrets in place and widens who can change sign-in):

1. Choose the shared secret (32+ random characters) and put it, with the
   people tool's credentials, in the stack env file the compose project reads:
   `PEOPLE_SERVE_SECRET`, `PEOPLE_HOST` (the tailnet DNS name the products are
   served on), `PEOPLE_LOGTO_ENDPOINT` (a Logto URL the container can reach:
   Logto binds loopback, so the tailnet URL or the identity network),
   `LOGTO_M2M_APP_ID` / `LOGTO_M2M_APP_SECRET`, `PEOPLE_SERVE_ACTORS` (the
   owner's email; comma-separated emails allowed to act, required: the service
   refuses to start without it), the tailnet credentials
   (["People: the tailnet half"](#people-the-tailnet-half)) and the mail ones
   (["Sending the invite email"](#sending-the-invite-email)). An unset tailnet
   or mail block just skips that step, as on the CLI.
2. On the VPS: `docker compose -p compose --env-file /srv/lifekit-secrets/stack.env
   -f compose/docker-compose.yml --profile people up -d --build people-serve`.
   This creates the `lifekit-people` network.
3. In the dashboard's compose project, declare `lifekit-people` as an external
   network, attach the dashboard to it, and give it the same secret
   (`PEOPLE_SERVE_SECRET`) as an env var. Nothing else.

To turn it off: `docker compose -p compose ... --profile people stop people-serve`
(or `rm -sf`), and detach the dashboard. A later merge deploy does not restart it.

**Endpoint contract.** All bodies and responses are JSON. Every route but
`/healthz` needs both headers, else `401` and nothing runs:

| Header | Value |
| --- | --- |
| `X-People-Secret` | the shared secret (constant-time compare) |
| `X-Auth-Request-Email` | the signed-in owner's email (the header the gate already forwards); recorded as the actor |

Each invite and resend adds one line to the Logto admin ledger
(`logto-admin.py ledger`, the `people-state` volume): `action: people-request`,
`actor`, `request` (`invite` / `resend`), `email`, `rights`, `send`, `result`
(`ok` / `error`), next to the invite's own undoable writes. Entries without
`undo` data are skipped by `logto-admin.py undo`. A request refused before it runs
(401, 403, 400) is not recorded.

| Route | Request | Response (200) |
| --- | --- | --- |
| `GET /healthz` | no headers | `{"ok": true}` |
| `GET /v1/rights` | | `{"rights": [{"id", "name", "port"}]}`: the catalog, for the checkboxes |
| `GET /v1/people[?email=]` | | `{"people": [{"email", "name", "roles", "rights", "suspended", "tailnet", "drift"}], "tailnet_configured", "tailnet_notes", "drift"}`; `rights` are catalog ids held, `tailnet` is a one-line membership and groups summary or `null`, `drift` lists per-person problems, the top-level `drift` the tailnet-wide ones |
| `POST /v1/people/invite` | `{"email", "rights": [id...], "send"?: bool, "invite_url"?: "https://..."}` | `{"ok": true, "email", "output": [line...]}`; `output` is the CLI's progress and, without `send`, the draft email |
| `POST /v1/people/resend` | `{"email", "rights": [id...], "invite_url"?}` | as invite; writes nothing to Logto or the policy, requires the user to hold each right, and mails |

Invite is idempotent. `send` mails only with the mail settings and a tailnet
invite link in place; otherwise the response carries the draft and a `not sent`
line in `output`, with status 200, as on the CLI. The host in the email is the
service's `PEOPLE_HOST`, never the request's.

Errors are `{"ok": false, "error": {"code", "message", "output"?}}`:

| Status | `code` | When |
| --- | --- | --- |
| 400 | `bad_request`, `unknown_right` | bad email, rights, `send` or `invite_url`; nothing was written |
| 401 | `unauthorized` | missing or wrong secret, or no valid owner email |
| 403 | `forbidden` | the email is not in `PEOPLE_SERVE_ACTORS` |
| 404 / 405 | `not_found`, `method_not_allowed` | |
| 413 | `too_large` | body over 8 KiB |
| 502 | `failed` | a Logto, tailnet or mail step failed (or the user was not found); `output` shows what completed first, and a failed send after a finished invite is retried with resend |
| 500 | `internal` | an unexpected error (the detail is in the container log, not the response); an invite or resend that died this way is still recorded in the ledger with `result: error` |
| 503 | `not_configured` | `PEOPLE_HOST` is not set on the service |

Revoke is not served yet: it follows `people revoke` (a separate slice). Its
seam is `route_table` in `serve.py`: add the route and one handler beside resend.

Tests: `scripts/tests/test_people_serve.py` (auth rejections, every route, the
ledger actor, and that a deploy render does not include the service) against
fake Logto, Tailscale and SMTP servers.

## Sign-in page branding

The Logto sign-in page shows each application's own mark and primary color:
the lifekit mark in Indigo `#4f46e5` for the sign-in gate (dashboard and devclaw
console share that one Logto client), the fs mark in Petrol `#175a6d` for
finance-sentry's client, the tenant default (lifekit mark, Indigo) for anything
else. Logto's per-application sign-in experience carries the logo and color;
the tenant's Terms/Privacy links are cleared, because lifekit publishes no such
pages. `scripts/identity/branding/README.md` has the seeds and where the marks
come from. Operator step, with the M2M credentials; it changes the live
sign-in page, so run it at go time:

```bash
sops exec-env <file>.sops 'bash scripts/identity/apply-branding.sh'
```

Idempotent; an application it cannot find by name is reported and skipped
(override with `GATE_APP`, `FS_APP`, `GRAFANA_APP`). Reverse each change with
`logto-admin.py undo`. Logto 1.44 stores an app display name but does not draw
it on the sign-in page. No restart is needed. Re-add Terms/Privacy links once
real pages exist, in the console or by extending `set-sign-in-exp`.

## Grafana sign-in through Logto

Grafana's `generic_oauth` against the identity provider above: a "Sign in with
lifekit" button next to the password form. Off by default; the compose
settings are `GF_AUTH_GENERIC_OAUTH_*` on the `grafana` service, driven by
`GRAFANA_OIDC_*` in the master file (`.env.example`). Roles: a Logto user with
the role `admin` is a Grafana **Admin**; every other member is **refused**
(`role_attribute_path` reads the `roles` claim and yields no role for a
non-admin, and `role_attribute_strict` turns that into a denied sign-in;
`roles` is in the requested scopes). Nobody becomes a Grafana server admin through it.

How the pieces connect: the browser goes to the tailnet issuer
(`https://<name>:3001/oidc/auth`). Grafana's own token and userinfo calls go to
`http://logto:3001/oidc/...` over `identity-oidc`, an external network only
Logto and its on-box OIDC clients join (the host firewall does not reliably let
a container reach the host's own tailnet address; `lifekit-shared` carries the
agent runtime and stays off Logto). `deploy.sh` creates `identity-oidc`
idempotently before the platform `up`; both compose projects reference it as
external. Logto signs the id_token with the tailnet issuer whichever
name answered, so it matches the issuer `deploy.sh` derives
(`scripts/deploy-grafana-oidc.sh`, an explicit `IDENTITY_ENDPOINT` wins).
Grafana's `root_url` (`GRAFANA_ROOT_URL`) must stay the tailnet HTTPS address:
it builds the redirect URI.

**Logto side**, with `scripts/identity/logto-admin.py` ([Logto admin
through the Management API](#logto-admin-through-the-management-api); each
command under `sops exec-env` as shown there):

1. The role `admin` (a User role, no API permissions), assigned to the
   owner. Members without it are refused at Grafana sign-in.

   ```bash
   python3 scripts/identity/logto-admin.py ensure-role admin
   python3 scripts/identity/logto-admin.py assign-role <owner email> admin
   ```

2. The Traditional web app `Grafana`. Redirect URI
   `https://<name>:3000/login/generic_oauth` (the same value as
   `GRAFANA_ROOT_URL` plus `/login/generic_oauth`); post sign-out redirect
   `https://<name>:3000/login`. It prints the client id; the secret goes to
   the file:

   ```bash
   python3 scripts/identity/logto-admin.py ensure-app Grafana \
     --redirect-uri https://<name>:3000/login/generic_oauth \
     --post-logout-uri https://<name>:3000/login --secret-file ~/grafana.secret
   ```

   On an existing `Grafana` app this sets the two URIs instead
   (`set-redirects Grafana` does only that).

In the admin console (`https://<name>:3002`) the same is *Roles > Create
role*, then *User management > the user > Roles*, then *Applications >
Create application > Traditional web*.

**Enable** (operator, captain's age key for the secret):

```bash
# 1. Add the secret to the master file; nothing prints it:
sops set --input-type dotenv --output-type dotenv secrets/lifekit.env.sops \
  '["GRAFANA_OIDC_CLIENT_SECRET"]' "\"$(cat ~/grafana.secret)\""
shred -u ~/grafana.secret
sops set --input-type dotenv --output-type dotenv secrets/lifekit.env.sops \
  '["GRAFANA_OIDC_CLIENT_ID"]' "\"<client id>\""
sops set --input-type dotenv --output-type dotenv secrets/lifekit.env.sops \
  '["GRAFANA_OIDC_ENABLED"]' '"true"'
# 2. Commit the file through a PR, then render and redeploy:
sudo bash scripts/secrets/render-stack-env.sh
```

The deploy then recreates Grafana (and, once, Logto: it joined
`identity-oidc`). It turns red and leaves sign-in off if the client id,
secret or issuer is missing. Sign in with the button as the owner and check
the role under *Administration > Users and access*.

**Password form off.** The local admin (`GRAFANA_ADMIN_USER`) is the
break-glass until then. After one member has signed in, set
`GRAFANA_OIDC_ONLY` to `true` the same way: Grafana then redirects straight
to Logto and hides the form. The admin password still works against the API
(`curl -u`) for a fix; the way back is unsetting `GRAFANA_OIDC_ONLY` and
redeploying.

**Rotating or moving.** A new client secret is `sops set` plus render and
redeploy. When the issuer moves (a public domain), register the application
again against the new issuer and update `IDENTITY_ENDPOINT` / the redirect
URI.

## Tailnet sign-in gate

Compose project `edge` (`compose/edge/`) puts one Logto sign-in in front of
the lifekit dashboard and the devclaw console. Once signed in on either,
the other opens without asking again. Two services:

- `traefik`, the forward-auth proxy, with one loopback entrypoint per
  surface: `127.0.0.1:18890` for the dashboard and `127.0.0.1:18891` for
  devclaw. Routes are in `compose/edge/traefik/dynamic.yml`, mounted as a
  directory and watched, so a route change merged to `main` applies live.
- `oauth2-proxy`, a confidential OIDC client of Logto. It answers Traefik's
  check for every request from its session cookie. With no session, it sends
  the browser to Logto and back.

The surfaces keep their URLs, `https://<name>:18790` and `https://<name>:18791`.
The cutover only points Tailscale Serve's two ports at the gate instead of at
the apps, so rolling back is pointing them back.

| | Value |
| --- | --- |
| Who gets in | Logto users with the role `admin` (others get 403), the role that also makes a Grafana Admin |
| Session | cookie `_lifekit_edge`, 30 days, refreshed against Logto at most hourly, so a removed role or a deleted user is out within the hour. Host-only, so it covers every port of the tailnet name: one sign-in for both surfaces |
| Dashboard `/api/*` with no session | 401, not a redirect (a `fetch()` cannot follow the sign-in page) |
| devclaw machine clients | unchanged: a request with an `Authorization` header or a `?token=` query, and `/mcp`, `/webhooks/`, `/health`, `/metrics`, go straight to devclaw, which checks them itself |
| devclaw console | after sign-in, the gate adds devclaw's bearer (`DEVCLAW_MCP_TOKEN`), so no token is asked for |
| Dashboard relay proof | after sign-in, the dashboard route adds `X-Lifekit-Edge-Proof` (`RELAY_EDGE_PROOF`) and the signed-in `X-Auth-Request-Email`; the dashboard's relay endpoint requires both, so a container on `lifekit-shared` that calls the dashboard directly is refused. The dashboard's own env carries the same `RELAY_EDGE_PROOF` (`docs/secrets-runbook.md`) |
| Not behind it | finance-sentry (its own Logto client), Grafana, the OpenClaw gateway, Logto itself |

**Why Traefik and oauth2-proxy.** Tailscale Serve cannot ask anyone before
proxying, so a proxy has to sit between it and the apps. Traefik is the
proxy the platform contract already names for the public edge
(`docs/platform-contract.md`, `ingress`), so the public routers join this
one later instead of a second proxy product arriving. oauth2-proxy is the
usual forward-auth OIDC client. It keeps the session in an encrypted cookie
(no session store to run) and re-checks it with Logto on refresh. Here
Traefik reads its routes from a file, not from container labels; the
contract's `edge` item stays a check for the public edge.

**Operator steps, once** (admin account; the Logto steps with
`scripts/identity/logto-admin.py`, [Logto admin through the Management
API](#logto-admin-through-the-management-api), or the admin console):

1. **Logto application.** A Traditional web app, named for example
   `lifekit sign-in gate`, with both surfaces' callback and sign-out URIs.
   It prints the client id; the secret goes to the file for step 3:

   ```bash
   python3 scripts/identity/logto-admin.py ensure-app "lifekit sign-in gate" \
     --redirect-uri https://<name>:18790/oauth2/callback \
     --redirect-uri https://<name>:18791/oauth2/callback \
     --post-logout-uri https://<name>:18790/ \
     --post-logout-uri https://<name>:18791/ --secret-file ~/gate.secret
   ```

2. **Role.** The gate lets in only Logto users with the role `admin`. If
   Grafana sign-in already created it, both commands report it is there:

   ```bash
   python3 scripts/identity/logto-admin.py ensure-role admin
   python3 scripts/identity/logto-admin.py assign-role <owner email> admin
   ```

   The owner's Logto account also needs an email address (a member created
   as above has one): oauth2-proxy refuses a sign-in whose token carries
   none with a 500 on `/oauth2/callback`.
3. **Secrets** (captain's age key). Add the client id, the client secret
   and a new cookie secret to the master file; nothing prints them:

   ```bash
   sops set --input-type dotenv --output-type dotenv secrets/lifekit.env.sops \
     '["EDGE_OIDC_CLIENT_ID"]' "\"<client id>\""
   sops set --input-type dotenv --output-type dotenv secrets/lifekit.env.sops \
     '["EDGE_OIDC_CLIENT_SECRET"]' "\"$(cat ~/gate.secret)\""
   shred -u ~/gate.secret
   sops set --input-type dotenv --output-type dotenv secrets/lifekit.env.sops \
     '["EDGE_COOKIE_SECRET"]' "\"$(openssl rand -hex 16)\""
   ```

   Commit the file through a PR; the three secrets' rows are already in the
   master inventory of `docs/secrets.md` (the inventory test requires a row
   for every secret in the file). After merge, render, then redeploy (CI `workflow_dispatch`, or
   `scripts/deploy.sh` as the deploy account):

   ```bash
   sudo bash scripts/secrets/render-stack-env.sh
   ```

   That deploy brings the project up, but nothing reaches it yet. Its
   report-only "sign-in gate (host facts)" block shows red until step 4.
4. **Cutover.** Check the gate before moving anything: from the box,
   `curl -sI http://127.0.0.1:18890/` answers `302` to
   `https://<name>:3001/oidc/auth`. Then point the two Serve ports at the
   gate (Serve only, never `tailscale funnel`):

   ```bash
   sudo tailscale serve --bg --https=18790 http://127.0.0.1:18890
   sudo tailscale serve --bg --https=18791 http://127.0.0.1:18891
   tailscale serve status        # 18790 -> :18890, 18791 -> :18891, tailnet only
   ```

   Open `https://<name>:18791/` (the devclaw console): Logto's sign-in, then
   the console. Then open `https://<name>:18790/`: the dashboard, with no
   second sign-in. If the dashboard still shows its old page, see the
   service-worker note below.

**Rollback** (restores direct access to both apps; the project can stay up):

```bash
sudo tailscale serve --bg --https=18790 http://127.0.0.1:18790
sudo tailscale serve --bg --https=18791 http://127.0.0.1:18791
```

**Signing out.** `https://<name>:18791/oauth2/sign_out` ends the gate's
session on both surfaces. Logto stays signed in, so the next visit comes back
without a prompt. To end the Logto session too, add `rd` with Logto's
end-session URL, URL-encoded:

```
https://<name>:18791/oauth2/sign_out?rd=https%3A%2F%2F<name>%3A3001%2Foidc%2Fsession%2Fend%3Fclient_id%3D<client id>%26post_logout_redirect_uri%3Dhttps%253A%252F%252F<name>%253A18791%252F
```

**The dashboard's service worker.** The dashboard is an installable app. Its
service worker answers every page navigation from its cache, `/oauth2/...`
included. So on a device that already has it installed, sign-in and
sign-out on the dashboard's port never reach the gate. Sign in or out on
the devclaw port instead: the cookie is shared. Or clear the dashboard's site
data (or hard-reload) once after the cutover. The fix belongs to the
dashboard repository: exclude `/oauth2/` from the service worker's
navigation fallback, and send the browser to `/oauth2/start?rd=<page>` when
`/api` answers 401.

**Logs.** Neither service writes an access log: devclaw's old `?token=`
links would put its bearer into Loki. Sign-in failures appear in
`docker logs edge-oauth2-proxy-1`. Traefik logs only warnings and errors.

## Container console logs in Loki

The OTel collector tails every container's Docker json-file log (read-only) and
ships it to Loki's OTLP endpoint, so a container's last stdout/stderr lines
survive the container being recreated. Retention is Loki's 14 days; only lines
written after the collector started are captured (`start_at: end`).

Crash forensics: dockerd's exit line (`journalctl -u docker`) gives the container
id; query it in Grafana Explore (Loki):

```
{service_name="docker"} | container_id=~"<id prefix>.*"
```

`container_id` and `log_iostream` (`stdout`/`stderr`) are structured metadata,
not labels. The log line does not carry a name; map id to name with
`docker ps -a --no-trunc --format '{{.ID}} {{.Names}}'` (live containers) or the
dockerd journal for containers already gone.

The collector's own Loki export errors are not shipped (they would feed back
into the pipeline while Loki is down); they stay visible via `docker logs`.

## Ledger persona from finance-sentry

finance-sentry's `agent/ledger/` is the source of truth for the finance agent's
persona (its README, "Composition model"): the OpenClaw persona is
`persona.core.md` + `adapters/openclaw.md`. `scripts/ledger-persona/ledger-persona.py`
fetches those two files at the commit in `scripts/ledger-persona/PIN` (read-only,
through `gh api` on the contents API with gh's existing login; no secret is
added), joins them with a `---` rule, and writes the result to the finance
agent's workspace `AGENTS.md`. Bump the pin by a PR that edits `PIN`.

Every deploy runs `install` and then `check` as `lifekit` (no sudo; a step near
the end of `scripts/deploy.sh`), so a merged `PIN` bump reaches the live persona
without a manual step. `install` is idempotent: when `AGENTS.md` already equals
the composed persona it reports `unchanged` and writes nothing. A failed
`install` (exit 2: fetch, pin, oversize or workspace error) skips the `check`
and turns the deploy red once the rest of it has run; a `check` that cannot run
(exit 2) does the same. A `check` exit 1 after a successful install can only be a
retired big-picture copy in host-state files, which the deploy does not own: it
prints a red line and leaves the deploy green. The drift-alert timer below stays
an operator install (it needs root). The same commands by hand, as `lifekit` on
the host:

```bash
S=/srv/lifekit-stack/scripts/ledger-persona/ledger-persona.py
$S check     # exit 0 in sync; exit 1 prints DRIFT (a diff of live vs composed) and/or a stale: line per retired copy; exit 2 any other failure. Read-only.
$S compose   # print the composed persona
$S install   # write AGENTS.md (the old one is kept as AGENTS.md.pre-persona-install)
```

The workspace defaults to `/srv/openclaw/config/agents/finance/workspace`;
override with `--workspace DIR` or `LEDGER_WORKSPACE`. `install` refuses a
persona over OpenClaw's `bootstrapMaxChars` (20,000 characters), because the
agent would load it truncated; `--allow-oversize` overrides. The pin must be
at or past finance-sentry's trim under that limit (#968; the composition is about
19,800 characters at the 2026-10-15 pin, it was about 23,300 before the trim). Only `AGENTS.md` is
written: `MEMORY.md` and `USER.md` are vault pointers set by their own deploy
step (see [Workspace memory files are pointers](#workspace-memory-files-are-pointers)),
and the other workspace files stay host state. `install`
changes a file the agent reads on its next session and restarts nothing.

finance-sentry's framework primer (served by its MCP server) is Ledger's only
big-picture source, so `check` also fails when a retired copy is back in the
workspace: a `RETIRED_PATHS` entry (`skills/project-context`,
`repos/finance-sentry`) exists, or a line of `MEMORY.md`, `USER.md` or `SOUL.md`
matches a `STALE_MARKERS` pattern (the old stack, Plaid, DevClaw, the wiki
tools, the old repo location, the Kit relay). Each hit prints as a `stale:`
line before the `AGENTS.md` diff. `SOUL.md` holds Ledger's own text, and
`MEMORY.md` and `USER.md` are vault pointers that carry no facts, so the three
are scanned for known markers, never compared with canonical text: only a
reintroduced copy alerts. A new retired copy is a new entry in either list.

A deploy overwrites a live edit of `AGENTS.md` (the old file is kept as
`AGENTS.md.pre-persona-install`), so fold any live-only change worth keeping into
finance-sentry before merging. To preview what a deploy will write, run `check`
and read the diff.
`--source DIR` composes from a local `agent/ledger` checkout instead of
fetching, and `--workspace` at a scratch directory rehearses the whole flow.

### Ledger persona drift alert

Nobody edits the finance agent's `AGENTS.md` by hand, but three things did
(2026-09-29 to 2026-10-09: an `openclaw doctor --fix` migration, a crewmate's
sweep, Kit acting on a chat message). The rule is that agent instructions change
only through the repo that owns them (Kit's `AGENTS.md` carries it: no live edit
of another agent's persona; tell the owner and file an issue or PR in
finance-sentry `agent/ledger`), and this timer is the backstop that notices when
a live edit happens anyway. The file is not locked read-only: that would need a
gateway restart and break upgrade migrations.

`lifekit-ledger-persona-check.timer` runs `scripts/ledger-persona/drift-alert.py`
daily at 07:15 (server time) as `lifekit`, the account that can read the finance
workspace and holds the `gh` login the fetch uses. It runs `ledger-persona.py
check` against `PIN`:

| Outcome | What happens |
| --- | --- |
| live equals the pinned composition | silent, exit 0 |
| live differs or is missing, or a retired big-picture copy is back | one `act` envelope through notify-relay (the owner's Telegram, with the `stale:` lines and the first diff lines collapsed), exit 0; reported again each day until fixed |
| the check cannot run (fetch, unreadable file, bad pin) or the relay refuses the envelope | reason in the journal, exit 1: the service fails, `unit-gauge` reports `host_unit_failed{unit="lifekit-ledger-persona-check"}` and the existing "host systemd unit is down or failed" rule alerts after 10 minutes |

No new secret and no container change: the envelope is POSTed from inside the
notify-relay container with `docker exec` (the port is not published), and the
unit runs the checkout in place, so a `PIN` bump applies with the deploy.
`bootstrap-vps.sh` installs the units but leaves the timer **disabled**: while
the live file still carries edits that are not folded into finance-sentry, an
enabled timer would alert every day. Order:

1. Fold the wanted live edits into finance-sentry `agent/ledger` by PR and merge it.
2. Bump `PIN` here to the merge commit (PR) and let it deploy.
3. Install the units (idempotent, does not enable), then apply the persona and
   check it is green:

   ```bash
   sudo bash /srv/lifekit-stack/scripts/ledger-persona/install-ledger-persona-check.sh
   sudo -u lifekit python3 /srv/lifekit-stack/scripts/ledger-persona/ledger-persona.py install
   sudo -u lifekit python3 /srv/lifekit-stack/scripts/ledger-persona/ledger-persona.py check
   ```

4. Enable the daily timer, as root:

   ```bash
   sudo ENABLE=1 bash /srv/lifekit-stack/scripts/ledger-persona/install-ledger-persona-check.sh
   systemctl list-timers lifekit-ledger-persona-check.timer
   ```

Verify without waiting for 07:15 once enabled (a drift sends one real Telegram message):

```bash
sudo systemctl start lifekit-ledger-persona-check.service
journalctl -u lifekit-ledger-persona-check -n 20 --no-pager
```

When it alerts: read the diff with `ledger-persona.py check` (above). A change worth
keeping goes into finance-sentry `agent/ledger` by PR, then the pin is bumped here
and `install` applies it; an unwanted one is reverted by `install` at the current
pin. A `stale:` line is fixed on the host, not in a repo: delete the retired
path, or cut the matching text down to owner facts and a pointer to the
finance-sentry primer. A failed run stays reported until the next success; after fixing the cause,
`sudo systemctl reset-failed lifekit-ledger-persona-check.service` clears it.

**Undo:** `sudo systemctl disable --now lifekit-ledger-persona-check.timer && sudo
rm /etc/systemd/system/lifekit-ledger-persona-check.{service,timer} && sudo
systemctl daemon-reload`, and remove the installer call from `bootstrap-vps.sh` in
the same PR so a re-bootstrap does not bring it back. Nothing else holds state.

**Kit's rule reaches the live Kit workspace by hand**, like every
`defaults/agents/*/workspace/` change (docs/openclaw-agent-design.md): merge the
new "Never edit another agent's instructions" hard rule into
`/srv/openclaw/config/agents/kit/workspace/AGENTS.md` as `lifekit`. No restart;
Kit reads it on its next session.

### Ledger finance drift

Finance facts live only in finance-sentry (Ledger's `AGENTS.md`, and
`docs/openclaw-agent-design.md` "Finance data and Ledger"). That rule is a
prompt; the backstop is that Ledger has nowhere of its own to keep facts, and a
check that reports when a file turns up anyway. In October 2026 the finance
agent's Claude Code auto-memory had grown theses and book-state files that
went stale while finance-sentry moved on; they were cleaned up by hand twice.

- **No auto-memory.** The OpenClaw image bakes
  `/etc/claude-code/managed-settings.json` (see *lifekit Claude Code home*), so
  no session in the gateway can write
  `~/.claude/projects/-home-node--openclaw-agents-finance-workspace/memory/`.
- **The check.** `scripts/ledger-drift/lifekit-ledger-drift.sh` lists that dir
  and the finance workspace through `docker exec` into the gateway, read-only.
  Any file in the auto-memory dir is a hit. A workspace file is a hit unless it
  matches `WORKSPACE_ALLOW` in the script: the OpenClaw bootstrap files and
  `AGENTS.md` backups, OpenClaw's own state, inbound chat media, the vault
  daily notes `memory/YYYY-MM-DD.md`, installed `skills/`, and Ledger's MCP
  helper scripts. It prints one `<area>: <path>` line per hit and exits 1 (0
  clean, 2 when it cannot run). It never deletes or moves a file.
- **The report.** `lifekit-ledger-drift.timer` runs it every 5 minutes as the
  admin account with `--textfile`, which writes
  `ledger_finance_drift_file{area,path}` (1 per hit) and
  `ledger_finance_drift_files` into the textfile directory
  (`ledger_finance_drift.prom`). The *Ledger finance drift* rule
  (`ledger-finance-drift`, `rules.yml`) fires warning per file, which reaches
  the owner's Telegram through the default policy and main through
  `alert-inbox` (`scripts/alert-inbox/poll_alerts.py`, one "alert firing" note
  per file). A run that cannot finish leaves the file untouched, so *host
  systemd unit is down or failed* and then *textfile metrics are stale* report
  it instead.

By hand, on the box as the admin account (read-only):

```bash
bash /srv/lifekit-stack/scripts/ledger-drift/lifekit-ledger-drift.sh; echo "exit=$?"
```

When it alerts: read the file. Anything still true goes into finance-sentry as
a proposal the owner confirms; then retire the file by hand (the check never
does). A legitimate new workspace file is a PR that adds it to
`WORKSPACE_ALLOW`. The alert resolves on the next run after the file is gone.

**Operator step (live install):** the installer writes `/etc/systemd/system`,
so a merge does not install the timer. Run on the box, as the admin account:

```bash
sudo bash /srv/lifekit-stack/scripts/host-gauge/install-host-gauges.sh
systemctl list-timers 'lifekit-ledger-drift*'
cat /var/lib/node_exporter/textfile/ledger_finance_drift.prom
```

The merge's deploy reloads Grafana provisioning, which loads the alert rule;
until the timer is installed the rule sees no data and stays quiet.

**Undo:** `sudo systemctl disable --now lifekit-ledger-drift.timer && sudo rm
/etc/systemd/system/lifekit-ledger-drift.{service,timer}
/usr/local/bin/lifekit-ledger-drift.sh /var/lib/node_exporter/textfile/ledger_finance_drift.prom
&& sudo systemctl daemon-reload`, and drop it from `install-host-gauges.sh` in
the same PR.

## YouTube transcripts through the PC

YouTube answers "Sign in to confirm you're not a bot" to this host's datacenter IP,
with or without yt-dlp, a JS runtime, a PO-token provider or other player clients, and
audio download is blocked the same way. So the one yt-dlp call that talks to YouTube
leaves through the owner's home connection. `scripts/yt-tunnel/` is two systemd **user**
units (the admin account `ADMIN_USER`, README "VPS users"; nothing under `/etc`):

- `yt-tunnel.service` runs `ssh -N -D 127.0.0.1:18082 <PC ssh alias>`, an open SOCKS
  proxy into the PC's network, so it binds **loopback only**. No container can reach host
  loopback, and the installer refuses any other bind.
- `yt-relay.service` runs `yt-relay.py`, a stdlib allow-list SOCKS5 relay, and is the
  **only listener on the docker0 address** (`172.17.0.1:18081`, what
  `host.docker.internal` resolves to inside the gateway via `extra_hosts: host-gateway`).
  The installer refuses a wildcard bind. `skills/youtube-transcript` fetches captions with
  `--proxy socks5h://host.docker.internal:18081`, i.e. only ever talks to the relay.

No API key, no cookies, no paid service, nothing installed on the PC, and `-N` runs no
command there.

- **Allow-list:** the relay takes SOCKS5 no-auth CONNECT with a domain-name address only,
  and only for `youtube.com`, `googlevideo.com`, `ytimg.com` or a subdomain of one of them
  (label-boundary match, case-insensitive). Everything else (other names, lookalikes such
  as `evilyoutube.com` or `youtube.com.evil.com`, IPv4/IPv6 address types, IP literals sent
  as names) gets SOCKS reply `0x02` and a log line in `journalctl --user -u yt-relay`.
  Allowed names go on to the tunnel unresolved, so the PC resolves them. The handshake and
  idle time are bounded, so a stuck client cannot hold the relay.
- **PC off:** ssh opens its listener only after it has connected, so with the PC off the
  tunnel unit retries with backoff (10s up to 5min, no start limit) while the relay stays
  up and answers a CONNECT with SOCKS failure `0x05` at once. The skill's pre-check is a
  real SOCKS5 CONNECT to `www.youtube.com:443` through the relay, so any failure of it
  (relay down, tunnel down, PC unreachable) answers `transcript unavailable - the PC is off`
  (exit 0, not an error); so does a yt-dlp connection failure to the relay or a timeout.
- **Failed fetch is not "no captions":** yt-dlp reports caption trouble (a refused
  download such as an HTTP 429, a missing PO token) as a warning about subtitles or
  captions and can exit 0 with no file. The skill answers `this video has no captions`
  only after clean runs; any subtitle/caption warning or error without a transcript is
  exit 1 with that line on stderr. Likewise only a relay-down or SOCKS `0x05`
  (upstream unreachable) failure reads as the PC-off line; an allow-list refusal
  (`0x02`) or other SOCKS error is exit 1 with the real reason.
- **Captions only:** human captions first, auto-generated as the fallback, original
  language track preferred, VTT cleaned to plain text. A video with none answers
  `transcript unavailable - this video has no captions`; there is no audio fallback.
- **Linger:** the user manager stops with the account's last session, and both units
  with it. `loginctl show-user <admin account> -p Linger` says which; `sudo loginctl
  enable-linger <admin account>` is the operator step that makes the tunnel survive logout and reboot.

Both units stay disabled and uninstalled until the go-time install below; merging the PR
changes nothing on the host. Install, as the admin account on the host (user scope, no
sudo; idempotent). The alias is the PC's `Host` entry in that account's `~/.ssh/config`
(key auth, no passphrase prompt):

```bash
export YT_TUNNEL_HOST=<PC ssh alias>
/srv/lifekit-stack/scripts/yt-tunnel/install-yt-tunnel.sh          # render, install, enable --now
/srv/lifekit-stack/scripts/yt-tunnel/install-yt-tunnel.sh --print  # show the rendered units only
systemctl --user status yt-tunnel.service yt-relay.service
```

`YT_TUNNEL_PORT` / `YT_TUNNEL_BIND` (18082, `127.0.0.1`; loopback only),
`YT_TUNNEL_RELAY_PORT` / `YT_TUNNEL_RELAY_BIND` (18081, docker0) and `YT_TUNNEL_UNIT_DIR`
override the defaults. The relay units run `yt-relay.py` from the checkout, so a deploy
that changes it needs `systemctl --user restart yt-relay.service`.

Install the skill, as `lifekit` on the host (after the PR is deployed, so the checkout
has it):

```bash
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-youtube-transcript.sh
```

It copies the skill into the gateway's shared skills directory
(`/srv/openclaw/config/skills/youtube-transcript`, `~/.openclaw/skills` in the
container, visible to every agent whose allowlist does not narrow it) with a pinned,
checksum-verified yt-dlp zipapp beside it. The directory is a bind mount and yt-dlp is
python (3.11 in the image), so no image rebuild and no container recreate is needed;
the skill watcher picks up `SKILL.md`. It prints each agent's allowlist state. An agent
with a `skills` list needs the name added: derive the whole list from the live file, as
in "Kit's second-mate relay" step 5 (`agents.entries.<id>.skills`, a config patch
replaces arrays). Bump yt-dlp by editing `YT_DLP_VERSION` and `YT_DLP_SHA256` in that
script (the sum is the release's `SHA2-256SUMS` line for `yt-dlp`) and re-running it;
YouTube changes break old yt-dlp, so bump first when fetches start failing with
anything but the PC-off line.

**Verify** from inside the gateway (the tunnel must be up):

```bash
G=$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway)
docker exec "$G" python3 /home/node/.openclaw/skills/youtube-transcript/transcript.py bQPU6UJ4iCw | head
YT_TRANSCRIPT_PROXY=socks5h://host.docker.internal:1 \
  docker exec -e YT_TRANSCRIPT_PROXY "$G" python3 /home/node/.openclaw/skills/youtube-transcript/transcript.py bQPU6UJ4iCw
# -> transcript unavailable - the PC is off
```

## Backups

`/srv/memory/` (the memory vault, mounted on your laptop as `~/memory/`) is your
data. Back it up.

**Set up your own mirror.** The simplest backup that works is a private git
repo the VPS pushes to on a timer:

```bash
# Run as the lifekit account - it owns /srv/memory (0750), and the timer runs as
# lifekit too. From the admin account: sudo -u lifekit -H bash
#
# One-time: bootstrap-vps.sh leaves /srv/memory a plain directory, so make it a
# repo pointing at your private remote before the timer runs. The identity is
# repo-local: the unattended timer has no ~/.gitconfig to fall back on.
cd /srv/memory
git init -b main
git remote add origin <your-private-vault-repo>
git config user.email <you@example.com>
git config user.name "lifekit backup"

# /usr/local/bin/memory-backup.sh, run from a systemd timer or cron
cd /srv/memory
git add -A
git commit -m "snapshot $(date -Iseconds)" || true
git push origin main
```

That gives you an off-machine copy and git history for point-in-time recovery
of anything that was pushed. It is a mirror, not an independent backup: the
remote holds the vault in plaintext, and there is no encrypted copy of the
vault anywhere. Pair it with the volume snapshots below if you want more.
Restoring from such a mirror is step 3 of
[Recovering from a complete VPS loss](#recovering-from-a-complete-vps-loss).

**On the maintainer's box** the vault is a clone of a private GitHub repo and
`memory-sync.timer` keeps it in sync both ways; see
[Memory-vault sync](#memory-vault-sync).

**Volumes to back up if you want full disaster recovery:**

- `/srv/memory/` — the vault (most important)
- `/srv/openclaw/config/` — OpenClaw config (the compose env file lives in `/srv/lifekit-secrets/`)
- `/srv/openclaw/secret-key/` — OpenClaw OAuth encryption key (lose this and you re-pair every channel)
- `/srv/openclaw/workspace/` — workspace skills (recoverable from this repo, but having a local copy is faster)
- `/srv/openclaw/backups/identity/` — nightly dumps of the identity provider's database (users, sign-in settings, OIDC clients and signing keys; [Identity provider (Logto)](#identity-provider-logto) has the restore)

On the maintainer's box the OpenClaw state, the secret key and the identity
dumps go off-box nightly, encrypted, with the backstage database:
[Off-box backup (R2)](#off-box-backup-r2). Snapshot the rest via your
provider's backups or rsync to another box.

### Memory-vault sync

`scripts/sync/memory-sync.sh`, installed as `/usr/local/bin/memory-sync.sh`,
runs from `memory-sync.timer` as `lifekit` every 15 minutes. Each tick commits
the vault's changes (`auto: lifekit-vps sync <time>`), fetches, rebases onto
`origin/main` and pushes `HEAD:main`. It never stashes and never picks a side:
a rebase conflict aborts the rebase, fails the run and leaves both versions
where they were. `log.md` files merge by union through the vault's
`.gitattributes`. The service skips (not fails) while `/srv/memory` has no
`.git`, so a fresh box does not page before the vault is copied in.

Once the vault ships `bin/hooks/pre-commit`, each tick also:

- re-sets `core.hooksPath=bin/hooks`, so every other commit in that clone
  (agents, `bin/log.sh`, the weekly model pass) goes through the hook;
- runs `bin/lint.sh --changed`, commits every file it passes and leaves the red
  ones on disk uncommitted, named in `/srv/memory/.lint-failed`. The marker goes
  away on the first tick with no red file. The tick commits with `--no-verify`
  because it has just run the same check on exactly what it stages;
- rebases in a temporary worktree when red edits to tracked files are on disk,
  then moves the branch with `git checkout`, which keeps them. If the remote
  changed one of those same files the checkout refuses and the run fails.

Until that file exists the tick commits everything, as before the gate. While
the model pass's lock (`/srv/memory/.model-pass`, see
[The memory-vault-cleanup cron](#the-memory-vault-cleanup-cron)) is under an
hour old, the tick does nothing.

**When it fails** (the *host systemd unit is down or failed* alert names
`memory-sync`, or the deploy's report-only line is red):

```bash
cat /srv/memory/.sync-failed       # time, step=<commit|lint|fetch|rebase|checkout|push>, exit code
cat /srv/memory/.lint-failed       # red files left uncommitted, if any
journalctl -u memory-sync.service -n 50
```

- `step=rebase`: a real conflict with the other clone. Resolve it by hand as
  `lifekit` (`sudo -u lifekit -H bash`, then `cd /srv/memory`):
  `git rebase origin/main`, fix each conflicted file, `git add` it,
  `git rebase --continue`. The next tick pushes. Never `-X theirs`/`-X ours`.
- `step=checkout`: the remote changed a file that has an uncommitted red edit
  here. Fix the lint finding (or move the edit aside), and the next tick goes
  through.
- `step=lint`: `bin/lint.sh` itself failed without naming a file; run
  `sh bin/lint.sh --changed` in the vault to see why.
- `step=fetch` / `step=push`: network or credentials; the journal has git's
  error.

The next successful tick removes `.sync-failed`; `sudo systemctl reset-failed
memory-sync.service` clears the unit state once the cause is gone. A red file
in `.lint-failed` is not a failure: the rest of the vault keeps syncing.

**Operator step (live install):** the installer writes `/usr/local/bin` and
`/etc/systemd/system`, so a merge does not install it (the deploy only reports
drift). Run on the box, as the admin account:

```bash
sudo bash /srv/lifekit-stack/scripts/sync/install-memory-sync.sh
sudo systemctl start memory-sync.service && systemctl show memory-sync.service -p Result
```

### openclaw-config mirror

`scripts/sync/openclaw-config-sync.sh`, installed as
`/usr/local/bin/openclaw-config-sync.sh`, runs from
`openclaw-config-sync.timer` as `lifekit` every 15 minutes. It copies the live
`openclaw.json` and each listed agent's top-level workspace files into the
`/srv/openclaw-config` clone, and commits and pushes when anything changed.
`MEMORY.md` and `USER.md` are not mirrored: they are vault pointers (see
[Workspace memory files are pointers](#workspace-memory-files-are-pointers)).
Each run also `git rm`s any mirrored copy of either file, retired agents'
included. That is a no-op once the first run after the change has pushed;
the git history keeps the earlier versions.

**Operator step (live install):** the installer writes `/usr/local/bin` and
`/etc/systemd/system`, so a merge does not install it (the deploy only reports
drift). Run on the box, as the admin account:

```bash
sudo bash /srv/lifekit-stack/scripts/sync/install-config-sync.sh
sudo systemctl start openclaw-config-sync.service && systemctl show openclaw-config-sync.service -p Result
```

### Runtime-state rotation

`memory-rotate.timer` (monthly, the 1st at 04:00) runs
`/usr/local/bin/memory-rotate.sh` as `lifekit`. It rotates runtime state only,
with no LLM: logrotate for the `.jsonl` logs in
`scripts/rotate/logrotate-memory.conf`, then `scripts/rotate/rotate-state.py`,
straight from the repo checkout:

- the morning brief's `inbox.md`: dated items older than 30 days move to
  `inbox-archive.md` under a `<!-- rotated <date> -->` banner;
- OpenClaw agent `*.trajectory.jsonl`: gzipped after 7 days, the `.gz` deleted
  after 60;
- a report-only session footprint, `heartbeat.txt` and one `run-log.jsonl`
  line in `/var/lib/lifekit/rotation/`.

The vault rotates its own pages (its `bin/rotate.sh`, from the weekly
`memory_vault_audit` cron); nothing here edits `/srv/memory`. The closed
proposals archive the old `rotate-extras.py` did is the vault's job too.

**Morning-brief inputs.** `inbox.md`, `inbox-archive.md` and `topics.yaml`
live in `/var/lib/lifekit/morning-brief/` (`~/.life-state/morning-brief/` in
the gateway), not in the vault. `scripts/vault-state-moves.sh`, run by every
deploy, seeds each missing file once from the vault's copy, or from the
generic template in `defaults/morning-brief/` when the vault has none, and
never overwrites. The same script moves the retired ops-agent's run dumps and
`.seen` markers from the vault's `projects/ops-agent/` to
`/var/lib/lifekit/ops-agent/`, verbatim; a marker the vault still tracks waits
until the vault untracks it, and `plan.md` / `log.md` stay in the vault.

Kit's `morning_brief` cron reads `topics.yaml` (not the inbox) and is host
state, so pointing it at the new file is an operator step (once, as `lifekit`;
it prints nothing of the message):

```bash
docker exec openclaw-openclaw-gateway-1 sh -c '
  job=$(openclaw cron list --all --json | jq -c ".jobs[] | select(.name == \"morning_brief\")")
  id=$(printf %s "$job" | jq -r .id)
  msg=$(printf %s "$job" | jq -r .payload.message)
  new=$(printf %s "$msg" | sed "s|~/memory/topics.yaml|~/.life-state/morning-brief/topics.yaml|g")
  if [ "$new" != "$msg" ]; then openclaw cron edit "$id" --message "$new" >/dev/null && echo repointed; else echo unchanged; fi'
```

**Operator step (live install):** the installer writes `/usr/local/bin` and
`/etc/systemd/system`, so a merge does not install it (the deploy only reports
drift). Run on the box, as the admin account:

```bash
sudo bash /srv/lifekit-stack/scripts/rotate/install-memory-rotate.sh
systemctl list-timers 'memory-rotate*'
```

### lifekit Claude Code home

`/home/lifekit/.claude` is the gateway's `~/.claude`, so every Claude Code
session there (devclaw, the agents' Claude CLI) reads its settings.
`scripts/claude-home/install-claude-home.sh` converges it on the same vault
harness the owner's own machine runs. Every deploy runs it as `lifekit`, and
bootstrap through `sudo -u lifekit`:

- `settings.json` gets `env.CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (the provider
  memory store is off; the vault is the one store) and two SessionStart hooks:
  the vault's `bin/session-context.sh` and `~/.claude/hooks/vault-core.sh`,
  which injects `~/memory/CORE.md`. Both are silent where there is no vault.
  It is a merge: other keys and other hooks stay as they are.
- `hooks/vault-core.sh` and `CLAUDE.md`, the vault pointer, come from
  `defaults/claude-home/`. The `CLAUDE.md` it replaces the first time is kept
  once as `CLAUDE.md.pre-vault-pointer`. A deploy rewrites hand edits to either.

Inside the OpenClaw image the switch is also locked: the image bakes
`/etc/claude-code/managed-settings.json` (root-owned, from
`compose/openclaw-gateway/managed-settings.json`) with `autoMemoryEnabled: false`
and the same env key. Managed settings outrank every other source, so a
workspace `.claude/settings*.json` an agent writes cannot turn auto-memory back
on, for the gateway and openclaw-cli; devclaw's own container is not covered by
it. *Ledger finance drift* below reports a file that appears anyway.

The auto-memory files already under `projects/*/memory/` are left in place:
the env key only stops new ones. `lifekit-stack migrate` copies the laptop's
`settings.json` over this one; the next deploy merges the keys back.

### Off-box backup (R2)

Everything above lives on the box's one disk. `lifekit-offbox-backup.timer`
copies the four stores that had no other copy to Cloudflare R2 each night,
encrypted with `age` before they leave the box, and
`lifekit-offbox-verify.timer` proves once a month that the newest copy
restores. Both run `scripts/offbox-backup/offbox_backup.py` from the checkout
as `lifekit` (it owns the state, reads `stack.env` and is in the docker group).
AWS is not involved.

**What one set holds.** Nightly at 03:30 (server time, after the 02:45
identity dump), one set under `lifekit/daily/<UTC stamp>/` in the bucket;
Sunday's run goes to `lifekit/weekly/`:

| Object | Store | Taken how |
| --- | --- | --- |
| `identity.sql.gz.age` | identity provider (Logto) | the newest dump `lifekit-identity-backup.timer` wrote; no second dump |
| `backstage.dump.age` | backstage's Postgres | `pg_dump -Fc` through `docker exec` into compose project `backstage`'s `backstage-db`; skipped (and recorded) where backstage is not deployed |
| `openclaw-secret-key.tar.age` | `/srv/openclaw/secret-key/` | tar piped into `age`: the key never touches disk in plaintext |
| `openclaw-sqlite.tar.zst.age` | the state's `*.sqlite` databases | copied with SQLite's backup API (consistent while the gateway writes) into the unit's private `/tmp`, removed after |
| `openclaw-state.tar.zst.age` | `/srv/openclaw/config/` | tar + zstd, minus the exclusions below |
| `MANIFEST.json` | | written last: names, sizes and sha256 of the ciphertexts, the recipients, skipped stores. A set without it is incomplete and is deleted by the next run |

The state archive leaves out what is regenerated or reinstalled:
browser caches and model downloads, `tools/` (agent toolchains), `npm/` (the
OpenClaw npm plugins: after a restore, reinstall each with `openclaw plugins
install @openclaw/<id>@<core version>`, the trusted install the gateway needs),
`cache/`, `tmp/`, `logs/`, `node_modules`, .NET build output, the archives of
sessions already migrated into SQLite, codex `logs_*` databases, and
pre-migration `.bak` copies. The full list is `STATE_EXCLUDES` in the script.
The workspace and the vault are not in the set: the workspace is rebuilt from
this repo, the vault has its own mirror (above).

**Encryption.** Every object is encrypted to the captain's age key (the same
identity as the master secrets file and finance-sentry) and, once
`OFFBOX_BACKUP_AGE_IDENTITY` is set, to the on-box restore-check key too. The
captain's key alone restores everything; the on-box key exists so the monthly
check can decrypt without the captain's key ever being on a service account.

**Size, retention, cost** (measured 2026-10-14): one set is about 0.31 GB
(state 281 MB, SQLite 30 MB, identity 1.4 MB, backstage 32 KB), written in
under a minute. Retention keeps the newest 7 complete daily and 4 complete
weekly sets, about 3.4 GB, inside R2's 10 GB-month free tier even next to
finance-sentry's backups on the same account; past it R2 charges $0.015 per
GB-month (this retention: about $0.05 a month) and nothing for egress. Size it
again with:

```bash
sudo -u lifekit python3 /srv/lifekit-stack/scripts/offbox-backup/offbox_backup.py estimate
```

**Shared bucket, own prefix.** The job uses finance-sentry's R2 bucket and
key pair (captain's call, 2026-10-14: no new bucket or token to make, and he
accepts that either job's credential can delete the other's objects). The
values are copies of finance-sentry's `BACKUP_R2_*` in the master file
(`docs/secrets.md`). The two jobs never overlap: finance-sentry writes, lists
and prunes only `daily/` and `weekly/` at the bucket root (S3 `ListObjectsV2`
with that prefix, then deletes only the keys it listed), and every remote path
this job touches goes through `scoped()`, which refuses anything not strictly
under `<bucket>/lifekit/`; its prune deletes only directories named like a set
(`lifekit/<daily|weekly>/<UTC stamp>`). A prefix that is empty, nested, or
`daily`/`weekly` stops the job before any call. `scripts/tests/test_offbox_backup.py`
holds both sides.

**Switching it on.** Until `stack.env` has the four R2 values, both timers run,
log one `not configured (missing ...)` line and exit 0: nothing is uploaded,
nothing alerts. The values and `OFFBOX_BACKUP_AGE_IDENTITY` are in the master
file already (firstmate copied them sops-to-sops and minted the identity
straight into it); on the live box, once:

1. Render: `sudo bash scripts/secrets/render-stack-env.sh` (or with firstmate's
   key: `sudo SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/firstmate.agekey" bash
   scripts/secrets/render-stack-env.sh`). No redeploy: the timers read
   `stack.env` on each run.
2. Install the units (the deploy has no sudo; the installer also adds `age`
   and `zstd` from Debian and a pinned rclone from rclone.org, checked
   against its sha256, at `/usr/local/lib/lifekit-offbox/rclone-<version>`):

   ```bash
   sudo bash /srv/lifekit-stack/scripts/offbox-backup/install-offbox-backup.sh
   systemctl list-timers 'lifekit-offbox-*'
   ```

3. First run by hand, then the restore check (it sends one Telegram message):

   ```bash
   sudo systemctl start lifekit-offbox-backup.service
   journalctl -u lifekit-offbox-backup -n 20 --no-pager
   sudo systemctl start lifekit-offbox-verify.service
   journalctl -u lifekit-offbox-verify -n 20 --no-pager
   ```

**Why not Debian's rclone.** trixie ships rclone 1.60.1, which reads the
object back after each upload with a `HEAD ...?versionId=` request. R2 answers
that with `501 Not Implemented`, so every upload is reported as failed
although the object was written (the first live run on 2026-10-14 failed this
way, and its failure path deleted the set). Upstream rclone does a plain
`HEAD`. The units set `RCLONE` to the pinned binary; bump it in
`install-offbox-backup.sh` (the version plus both zips' sums from that
release's `SHA256SUMS`) and rerun the installer.

**Failures.** A failed store deletes the set being written and fails the
oneshot; a file `lifekit` cannot read counts as a failed store (tar exits
non-zero), so one root-owned file under the state dir turns every night red
until it is fixed. A listing error during retention also fails the oneshot and
deletes nothing. `unit-gauge` reports `host_unit_failed{unit="lifekit-offbox-backup"}`
and the "host systemd unit is down or failed" rule alerts, like the identity
dump. Fix the cause, start the service again, and `sudo systemctl
reset-failed lifekit-offbox-backup.service`.

**The monthly restore check** (first Sunday, 05:00) fetches the newest complete
set (it fails when that is older than 48 hours), checks each ciphertext
against the manifest's sha256, decrypts with the on-box key and proves each
store restores: the gzip and the `pg_dumpall` footer of the identity dump,
`pg_restore --list` on the backstage dump in a throwaway `--network none`
container, a tar listing of the state with `openclaw.json` in it, `PRAGMA
quick_check` on every SQLite copy, and the secret key against the live one by
hash only. The verdict goes to the owner through notify-relay: `good` with one
line per store, or `act` naming what failed. A verdict the relay refuses fails
the unit. Scratch lives in the unit's private `/tmp` and is removed.

**Restoring by hand.** On any machine with `age`, `rclone` and the captain's
key (`~/.config/sops/age/keys.txt` on the admin account, or KeePassXC), or on
the box. Reading works with any rclone, Debian's included. Never add `-v` or
`-vv` with this environment: rclone's debug log prints every
`RCLONE_CONFIG_*` value, the secret key included. Pick a set in the
Cloudflare dashboard, or list them:

```bash
# the four values: sops decrypt --extract '["OFFBOX_BACKUP_R2_..."]' secrets/lifekit.env.sops
export RCLONE_CONFIG_OFFBOX_TYPE=s3 RCLONE_CONFIG_OFFBOX_PROVIDER=Cloudflare \
  RCLONE_CONFIG_OFFBOX_REGION=auto RCLONE_CONFIG_OFFBOX_NO_CHECK_BUCKET=true \
  RCLONE_CONFIG_OFFBOX_ENDPOINT=<endpoint> RCLONE_CONFIG_OFFBOX_ACCESS_KEY_ID=<id> \
  RCLONE_CONFIG_OFFBOX_SECRET_ACCESS_KEY=<secret>
rclone lsf offbox:<bucket>/lifekit/daily/
rclone copy offbox:<bucket>/lifekit/daily/<stamp> ./set
cat set/MANIFEST.json; sha256sum set/*.age        # compare with the manifest
K=~/.config/sops/age/keys.txt
```

- Identity: `age -d -i $K set/identity.sql.gz.age > identity-<stamp>.sql.gz`,
  then the restore in [Identity provider (Logto)](#identity-provider-logto)
  with that file.
- Backstage, into a running `backstage-db` (stop the app first, start it after):

  ```bash
  docker stop backstage
  age -d -i $K set/backstage.dump.age | docker exec -i backstage-db \
    sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner'
  docker start backstage
  ```

- Secret key, before the gateway starts:
  `age -d -i $K set/openclaw-secret-key.tar.age | sudo -u lifekit tar -x -C /srv/openclaw/secret-key`
- OpenClaw state, with the gateway stopped, the state first and the databases
  over it (delete any `*.sqlite-wal`/`*.sqlite-shm` left beside them first):

  ```bash
  age -d -i $K set/openclaw-state.tar.zst.age | zstd -dc | sudo -u lifekit tar -x -C /srv/openclaw/config
  age -d -i $K set/openclaw-sqlite.tar.zst.age | zstd -dc | sudo -u lifekit tar -x -C /srv/openclaw/config
  ```

  Then reinstall the npm plugins (above) and start the gateway with `bash scripts/deploy.sh`.

**Undo:** `sudo systemctl disable --now lifekit-offbox-backup.timer
lifekit-offbox-verify.timer && sudo rm
/etc/systemd/system/lifekit-offbox-{backup,verify}.{service,timer} && sudo
rm -r /usr/local/lib/lifekit-offbox && sudo systemctl daemon-reload`, and
remove the installer call from `bootstrap-vps.sh` in the same PR. The bucket
keeps the sets until `lifekit/` is deleted:
`rclone purge offbox:<bucket>/lifekit` with the environment above, never the
bucket or its root (finance-sentry's backups live there). Drop the four
`OFFBOX_BACKUP_R2_*` lines from the master file, not the token: finance-sentry
still uses it.

## Pull-based alert-to-inbox polling

Grafana's alerts route to Telegram only, so nothing reaches an agent unless
something pulls. `scripts/alert-inbox/poll_alerts.py` (see
`scripts/alert-inbox/README.md`) reads the provisioned alert rules' own
PromQL from `rules.yml` and evaluates them against Prometheus's open,
loopback-bound query API — no Grafana admin credentials, no new listener.
Installed by `bootstrap-vps.sh` as the `alert-inbox.timer` systemd unit (every
5 minutes, as the admin account) when `FM_INBOX_BIN` / `FM_INBOX_HOME` are in
the bootstrap environment; they are kept in `/etc/lifekit/alert-inbox.env`.
On a live box, adopt or refresh it alone:

```bash
sudo FM_INBOX_BIN=/path/to/fm-inbox.sh FM_INBOX_HOME=/path/to/target/home \
  bash /srv/lifekit-stack/scripts/alert-inbox/install-alert-inbox.sh
# then delete the old crontab line so two pollers do not share the state file:
crontab -e   # delete: */5 * * * * ... scripts/alert-inbox/poll_alerts.py ...
```

Logs go to the journal (`journalctl -u alert-inbox`).

Notifies only on transitions (a rule starting to fire, the same rule
resolving; a `warning` rule's resolve is silent) via `fm-inbox.sh note`; a
Prometheus outage is its own transition so it is reported once, not every poll. Does not touch Grafana contact
points, policies, or credentials.

## External heartbeat (dead-man)

The box's own alerts watch the box, so they cannot report the box, Docker,
Grafana's alerting or the notifier being dead. An always-firing Watchdog rule
routes to a webhook contact point that pings a heartbeat check hosted outside
the box every few minutes; the external service alerts the owner when the pings
stop. Because the ping is Grafana's own notification, it proves rule
evaluation and delivery are alive, not just the host.

**What its alert means:** the check has not been pinged within its grace
period. Suspect the host, Docker, Grafana, or the box's outbound network, in
that order; `docker ps`, then Grafana logs.

**Enable:** create a heartbeat/cron-style check with an external service (an
account only the owner can create; expected period a few minutes plus a grace
period, notifying the owner by a channel independent of the box), then set its
ping URL as `LIFEKIT_EXTERNAL_HEARTBEAT_URL` in the box env file
and deploy. The URL is a secret: env file only, never in the repo. Unset =
disabled; deploy provisions a delete of the watchdog rule (so disabling after enabling stops the pings) and continues. Changing the
variable needs the Grafana container recreated (a deploy does it).

## When Telegram goes silent

Symptom: your bot stops responding, no errors visible.

```bash
ssh <your-vps-tailscale-name>
cd /srv/lifekit-stack/compose/openclaw    # compose project `openclaw`
docker compose logs -f openclaw-gateway --tail 100
docker compose --profile cli run --rm -T openclaw-cli openclaw doctor
docker compose --profile cli run --rm -T openclaw-cli openclaw channels list
```

Common causes:

1. **Telegram token rotated** — compare the account's bot token (a SOPS secret, see `docs/secrets.md`) against your BotFather token. Rotate it there, then `openclaw secrets reload`.
2. **Polling stalled** — `docker compose -p openclaw restart openclaw-gateway`.
3. **OpenClaw OOM** — `dmesg | grep -i oom`. If yes, scale up the VPS.
4. **Anthropic auth failing** — every agent authenticates with the `anthropic:api-key` profile (a ref to `ANTHROPIC_API_KEY`, prepaid credits that expire 2026-11-09), first in `auth.order`, falling back to the `anthropic:setup-token` profile (a `tokenRef` to `CLAUDE_OAUTH_TOKEN`) when the credits lapse. Run `docker exec openclaw-openclaw-gateway-1 openclaw models status --agent <id>`: the `anthropic` line must list both profiles as exec refs (`exec:anthropic-api-key`, `exec:claude-oauth-token`) with `effective=` the agent's own store. Then run `openclaw secrets audit --allow-exec`, which must show no unresolved ref. The live check, and the yearly rotation if the token has expired, are in `docs/secrets-runbook.md`, "Claude service token".

## When a skill says "command not found" or "sharp: missing native binary"

Symptom: a skill that worked on the laptop fails on the VPS with `command not
found: life-state`, or `Error: Could not load the "sharp" module using the
linux-arm64 runtime` (nutrition-claw and any other skill with native deps).

Root cause: `rsync` from the laptop copies the skill source, but skips the
platform-specific bits — `node_modules/` built for the wrong arch, or a CLI
binary that was `npm install -g`'d on the laptop and never replayed on the
VPS.

`scripts/deploy.sh` now reinstalls every skill's `package.json` inside the
gateway container after rsync, and `compose/openclaw-gateway/Dockerfile`
bakes in `python3 / make / g++` so native modules (sharp ships a vendored libvips;
better-sqlite3, etc.) can rebuild on-host. That covers the per-skill
`node_modules` case automatically — re-run `scripts/deploy.sh` and the
`sharp` error should clear.

Skill CLIs that live outside this repo (e.g. `life-state`, which is built
from `~/projects/life-state` on the maintainer's laptop) are NOT handled
automatically — they have to be installed once on the VPS after the first
rsync. Inside the gateway container:

```bash
docker compose -p openclaw -f compose/openclaw/docker-compose.yml --env-file /srv/lifekit-secrets/stack.env \
  exec openclaw-gateway npm install -g /home/node/.openclaw/workspace/external/life-state
```

…and then restart the gateway. If `external/life-state` isn't there yet,
rsync it across alongside the skills directory.

## When `queue.jsonl` grows without draining

Symptom: `/var/lib/lifekit/queue.jsonl` (the runtime-state dir, split from the
vault in the 2026-05-27 runtime-knowledge split) keeps growing; domain files
under `/srv/memory/domains/` don't update.

Nothing in this stack drains that queue into the domain files. The component
that did, `lifekit-curator`, was retired 2026-05-25 and is not a compose
service at all. `lifekit-orchestrator` took over its two cron jobs
(`task_dispatch_15m` and `curator_30m`); it is retired too and no longer
defined in this stack, and its stated replacement — devclaw-mcp's in-process
queue — covers the task-dispatch half. Nothing here has taken over the curation half.

So a growing queue and stale domains are the expected state on this stack
today, not a fault to restart your way out of: domain curation is unowned here
until something claims it.

What you can still check is that the queue file is where it should be and that
the gateway — which appends to it — has both of its mounts:

```bash
ssh <your-vps-tailscale-name>
sudo -u lifekit -H bash            # owns /var/lib/lifekit and the docker group
cd /srv/lifekit-stack
ls -l /var/lib/lifekit/queue.jsonl                 # is it still growing?
docker compose -p openclaw -f compose/openclaw/docker-compose.yml --env-file /srv/lifekit-secrets/stack.env \
  exec openclaw-gateway ls -la /home/node/.life-state/queue.jsonl /home/node/memory/domains/
```

Inside the container the runtime-state dir is `/home/node/.life-state` and the
vault is `/home/node/memory` — neither is reachable at its host path.

## SSHFS auto-mount

To auto-mount `/srv/memory/` on your laptop at login:

```bash
# Add to /etc/fstab (Linux) or ~/Library/LaunchAgents (macOS)
<vps-tailscale-name>:/srv/memory /home/<you>/memory fuse.sshfs \
  noauto,x-systemd.automount,_netdev,user,idmap=user,follow_symlinks,IdentityFile=/home/<you>/.ssh/id_ed25519,allow_other,default_permissions,uid=1000,gid=1000  0 0
```

For macOS use `macFUSE` + the equivalent `LaunchAgent` config. For WSL, a `systemd-user` automount is the cleanest path.

If the mount is flaky over Tailscale, try:

```bash
sshfs -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3 ...
```

## Recovering from a complete VPS loss

You provisioned a VPS, it died, and you want to come back up on a fresh one.

```bash
# 1. New VPS, fresh Debian 13 (Ubuntu 24.04 also works). SSH in (over its temporary public address).
# 2. From your laptop. The bootstrap joins the tailnet, then closes public SSH
#    (see "Host firewall (nftables)" above): reconnect over the tailnet after.
cd lifekit-stack
lifekit init-stack --target <new-vps-ip>
# Wizard reuses your saved wizard.yaml (from your private backup, NOT this repo).

# 3. Restore /srv/memory/ from your private vault mirror (see "Backups" above -
#    it is plaintext, and only as fresh as its last push). Step 2 already
#    populated /srv/memory/ (system/modules.yaml), so a clone into it would
#    refuse - point the directory at the remote and reset onto it instead:
ssh <new-vps>
sudo -u lifekit -H bash            # /srv/memory is the lifekit account's
cd /srv/memory
git init -b main
git remote add origin <your-private-vault-repo>
git config user.email <you@example.com>
git config user.name "lifekit backup"
git fetch origin
git reset --hard origin/main
# OR rsync from a snapshot.

# 4. Restore /srv/openclaw/secret-key/, the OpenClaw state, the identity dump
#    and the backstage database from the newest R2 set ("Off-box backup (R2)"
#    above, restoring by hand). Without the secret key, you re-pair every channel.
```

Total recovery time: ~30 minutes if your backups are current.

## Health check chain

After every deploy, the wizard runs:

```bash
cd /srv/lifekit-stack/compose/openclaw    # compose project `openclaw`
docker compose --profile cli run --rm -T openclaw-cli openclaw doctor
docker compose --profile cli run --rm -T openclaw-cli openclaw health
# Plus a synthetic Telegram self-message round-trip
```

If any of these fail, the wizard surfaces the error and does NOT mark the deploy as green. Manual `git revert` + re-deploy is the v0.x rollback path; auto-revert is on the v1 roadmap.

## When to scale up

Symptoms of an undersized VPS:

- `dmesg | grep -i oom` shows the kernel killing containers.
- `docker stats` shows persistent CPU saturation.
- OpenClaw logs say "polling timeout" frequently.

Most VPS providers offer a live resize to a larger plan (no data loss). For larger jumps, snapshot first.

## When to ask for help

Open a [GitHub Issue](https://github.com/lifekit-hq/lifekit-stack/issues) with:

- `lifekit init-stack --version`
- `docker compose version`
- VPS provider + plan
- `docker compose logs --tail 200` (sanitized — never paste tokens)
- What you tried before opening the issue

Other providers are accepted, but no official SLA in v0.x.
