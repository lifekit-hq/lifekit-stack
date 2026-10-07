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
   runtime, and every agent is on claude-cli since the 2026-09-16 all-agents
   switch to Claude primary (see the smoke-turn comment in
   `scripts/deploy-openclaw.sh`);
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
`diagnostics-otel` plugin enables, `gateway.auth.rateLimit`, `memory.search`
(`enabled` + `extraPaths`), `agents.defaults.heartbeat.every`, the inbound
`hooks` block and the finance agent's heartbeat - live in
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

#### Provider plugin allowlist (`plugins.allow`, lifekit-stack#175)

OpenClaw loads every bundled plugin that is enabled by default. That includes
more than two dozen model-provider plugins that nothing here routes to.
`plugins.allow` in the platform file lists the only plugin ids that load:

- **The routed providers.**
  - `anthropic`: every agent's model runs through its claude-cli backend.
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
             "project-context", "thesis-tracker", "catalyst-calendar",
             "idea-generation",
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

The vault's "dreaming" pass: a follow-up to the command-kind `memory_vault_audit`
cron (`scripts/memory-audit/`), an agent-turn job named `memory-vault-cleanup` that
runs the vault's own `.claude/skills/memory-audit` skill **unattended** 20 minutes
later on the same `kit` agent, so Rule 3's judgment items in `audits/latest.md` get
worked instead of only proposed. It applies the skill's whole "safe to apply alone"
tier itself (frontmatter/link/size fixes, stale restatements, log compaction,
stale-status refresh-or-conclude, the archive pass) and only ever escalates what the
skill's own text sends to "Needs Denys". Declared by
`scripts/ensure-memory-vault-cleanup.sh` - re-run it on a rebuilt host to recreate
the row; it converges (when a `memory-vault-cleanup` row already exists it leaves
everything but delivery untouched and re-applies `--no-deliver`, rather than
skipping). No chat id or other secret is needed by this script or job.

No skill install, no agent workspace change, and no gateway restart: the prompt
reads the skill straight off the vault's existing mount (`/home/node/memory` in
every agent, `compose/openclaw/docker-compose.yml`) and follows it directly. Vault edits are
left uncommitted for the host's `memory-sync.timer` to commit and push, same as
every other vault edit.

**Delivery is transition-only, through notify-relay, not an OpenClaw `--announce`.**
The job is created (and, on every re-run of the ensure script, converged) with
`--no-deliver`, so OpenClaw's own runner fallback delivery is off - without it,
the job's own default (`mode: "announce", channel: "last"`) fail-closes every run,
since an isolated-session job has no prior chat for "last" to resolve against
(confirmed live 2026-09-29: `cron show` reported exactly that fail-closed
`deliveryPreview`). Instead, the agent's own prompt builds the skill's "Needs Denys"
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
against a same-day
`audits/latest.md` should append one `## [date] audit | ...` line to the vault's
`log.md` every time (`nothing to apply` when the skill found nothing to fix or
archive); a run against a stale report appends a `skipped: audits/latest.md is
stale (<date>)` line instead and touches nothing else in the vault. Either way,
check `audits/needs-denys-state.json` reflects the run's Needs-Denys list, and that
a notify-relay call only happened when that list's content actually changed from the
file's previous contents.

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
  does not survive a write that replaces the file by rename. Folding it into
  the vault is separate work.
- **`dreaming/` and `.dreams/` are not copied.** They are memory-core dreaming
  output. Dreaming is off, so they are dead and are deleted rather than moved.
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

### Kit's needs-you decisions and the daily digest

Decisions waiting on the owner go to the OpenClaw app, not Telegram. Kit keeps one
ledger in its workspace (`needs-you/open.json`, closed ones in
`needs-you/closed.jsonl`). It asks each decision with `ask_user` in its main
Control UI / WebChat session (`agent:kit:main`), which shows as a card with buttons.
An answer closes the entry. The contract is kit's `AGENTS.md`, section "Needs you"
(`defaults/agents/kit/workspace/`). `ask_user` exists only in primary sessions, never
in subagent or ACP runs. So a decision that comes up in a Telegram DM, an A2A ask or a
subagent is recorded, then handed to the main session with `sessions_send`.

The daily `needs-you-digest` cron posts what is still open into that same chat.
`scripts/ensure-needs-you-digest.sh` declares it and converges on re-run. It is a
`current`-session agent turn bound to `agent:kit:main` with announce delivery: the
reply is committed to the conversation's transcript, and that session has no external
route, so nothing goes to Telegram. It replies `NO_REPLY` when the ledger is empty, so
nothing is posted. A run that cannot read the ledger says so in the chat. A run that
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

3. **Declare the digest:** `/srv/lifekit-stack/scripts/ensure-needs-you-digest.sh`
   (as lifekit; `NEEDS_YOU_DIGEST_CRON` / `NEEDS_YOU_DIGEST_TZ` override the default
   `0 9 * * *` Europe/Dublin).

**Verify:** `cron show <id>` previews delivery as `announce -> current session`.
`cron run <id> --expect-final --json` against an empty ledger records the run as
intentionally silent, and the main chat stays quiet. With one entry in `open.json`,
the digest shows up in the Control UI chat without a refresh.

**Rollback:** `openclaw cron rm <id>`. The ledger files are kit workspace state and
can stay. Push delivery of these cards to a phone comes with the Control UI's
HTTPS-origin setup, not with this job.

### Kit's second-mate relay

Kit can send the owner's words to the VPS second mate's captain inbox and read the
replies, over one SSH key whose only power is a forced command on the host. It is a
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
re-announces it.

**Rollback:** delete the authorized_keys line, `sudo rm -r /srv/lifekit-secrets/kit-relay`
and `/usr/local/libexec/kit-relay`, re-apply kit's old skills list, and remove the skill
folder from kit's workspace. The mount and `extra_hosts` can stay: an empty directory
gives the gateway nothing.

#### Dashboard sender (proposal box)

The dashboard can queue a `proposal:` or `task:` note for the second mate while Claude
quota is exhausted (Kit shares that login, so its relay cannot run then). It is a second
authorized key on the same root-owned forced command, not a second script:

- **Sender:** the key's `command=` passes the sender as an argument,
  `command="/usr/local/libexec/kit-relay dashboard"`; no argument is Kit's key. The
  sender picks the request-id prefix (`dash-` instead of `kit-`), the rate bucket
  (30 new `dash-` notes per rolling hour, counted apart from Kit's) and the `receipts`
  view (only `dash-` notes and their replies). Nothing the client sends picks the
  sender. Kit's key, prefix and bucket are unchanged.
- **Key:** `/srv/lifekit-secrets/dashboard-relay/` (`id_ed25519`, pinned `known_hosts`,
  `ssh_config`, the same shape as `kit-relay/`), mounted read-only into the dashboard
  container at `/run/lifekit/dashboard-relay` (the mount lives in lifekit-dashboard). The
  body starts with `proposal:` or `task:`; the relay does not interpret it.
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

#### Home argument (answers for the main home)

Both verbs take an optional last word, the home the verb acts on, so a dashboard answer
to a call owned by the main home lands in that home's inbox. The same two keys and the
same root-owned script serve it; there is no second key.

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
a Fleet row for them.

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
and the service of every `lifekit-*.timer`. For a timer, `active` tracks the
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
`sudo systemctl reset-failed <unit>.service` clears it.

Adding a unit means editing the allowlist at the top of the script. A new
`lifekit-*.timer` is picked up automatically.

**Operator step (live install):** the installer writes `/etc/systemd/system`,
so a merge does not install it. Run on the box, as the admin account:

```bash
sudo bash /srv/lifekit-stack/scripts/host-gauge/install-host-gauges.sh
systemctl list-timers 'unit-gauge*'
cat /var/lib/node_exporter/textfile/host_unit.prom
```

The merge's deploy reloads Grafana provisioning, which loads the alert rule;
until the timer is installed the rule sees no data and stays quiet.

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
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py ensure-app "<name>" \
  --redirect-uri <uri> --post-logout-uri <uri> --secret-file ~/<name>.secret'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py set-redirects <app> \
  --redirect-uri <uri> --post-logout-uri <uri>'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py set-sign-in-exp \
  --sign-in-identifiers email username --primary-color <#hex> --logo-url <url>'
```

`assign-role` takes a user id, primary email or username. `ensure-app`
creates a Traditional web app, or brings an existing one's URIs to the given
lists. `set-redirects` replaces the list it is given and keeps the other.
`--secret-file` writes the app's client secret to a new 0600 file for
`sops set` to read (`"\"$(cat ~/<name>.secret)\""`), then `shred -u` it.
`set-sign-in-exp` changes only the sign-in experience fields given (logo,
dark logo, favicon, dark favicon, colors, `--sign-in-identifiers`,
`--sign-up-identifiers`); the image options take an http(s) URL or an inline
`data:image/svg+xml;base64,...` URI, which needs no hosting.
`email username` signs in with either, by password;
`--code-sign-in-identifiers email` adds the emailed code
([Email sign-in (Gmail SMTP)](#email-sign-in-gmail-smtp), with
`set-email-connector` and `send-test-email`). It never sends the sign-in mode
or the social sign-in settings.

**Reversing a change.** Each change appends a line to the ledger
(`~/.local/state/lifekit/logto-admin.ledger.jsonl`, or `LOGTO_ADMIN_LEDGER`):
the ids it created or touched, and the inverse request. `logto-admin.py
ledger` lists it; `logto-admin.py undo [N]` sends entry N's inverse (default:
the newest not yet undone): delete the role or app it created, take the role
back, restore the previous redirect URIs, or restore the previous
sign-in experience objects, or delete the email connector it created.

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

Nothing here runs on deploy; the install is an operator command, as `lifekit`
on the host:

```bash
S=/srv/lifekit-stack/scripts/ledger-persona/ledger-persona.py
$S check     # exit 0 in sync; exit 1 prints DRIFT and a diff of live vs composed. Read-only.
$S compose   # print the composed persona
$S install   # write AGENTS.md (the old one is kept as AGENTS.md.pre-persona-install)
```

The workspace defaults to `/srv/openclaw/config/agents/finance/workspace`;
override with `--workspace DIR` or `LEDGER_WORKSPACE`. `install` refuses a
persona over OpenClaw's `bootstrapMaxChars` (20,000 characters), because the
agent would load it truncated; `--allow-oversize` overrides. At pin `5a3999e`
the composition is about 23,300 characters, so the first apply needs the
persona trimmed in finance-sentry (and a pin bump) first. Only `AGENTS.md` is
managed: `USER.md` and the other workspace files stay host state. `install`
changes a file the agent reads on its next session and restarts nothing.

First apply, once `check` shows no live-only change worth keeping (fold any
back into finance-sentry first): run `check`, read the diff, then `install`.
`--source DIR` composes from a local `agent/ledger` checkout instead of
fetching, and `--workspace` at a scratch directory rehearses the whole flow.

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

**On the maintainer's box** the same job runs as a `memory-sync.timer` /
`memory-sync.service` pair in `/etc/systemd/system/`, firing
`/usr/local/bin/memory-sync.sh` as `lifekit` every 15 minutes to sync
`/srv/memory/` with its private GitHub remote. Those units are specific to that
box - this repo installs `memory-rotate`, never `memory-sync` - and the script,
the units and the vault's own risk notes live with the vault; see the private
vault runbook for owner detail.

**Volumes to back up if you want full disaster recovery:**

- `/srv/memory/` — the vault (most important)
- `/srv/openclaw/config/` — OpenClaw config (the compose env file lives in `/srv/lifekit-secrets/`)
- `/srv/openclaw/secret-key/` — OpenClaw OAuth encryption key (lose this and you re-pair every channel)
- `/srv/openclaw/workspace/` — workspace skills (recoverable from this repo, but having a local copy is faster)
- `/srv/openclaw/backups/identity/` — nightly dumps of the identity provider's database (users, sign-in settings, OIDC clients and signing keys; [Identity provider (Logto)](#identity-provider-logto) has the restore)

Snapshot these via your provider's backups or rsync to another box.

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
resolving) via `fm-inbox.sh note`; a Prometheus outage is its own transition
so it is reported once, not every poll. Does not touch Grafana contact
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
4. **Anthropic auth failing** — every agent authenticates with the `anthropic:setup-token` profile, a `tokenRef` to `CLAUDE_OAUTH_TOKEN`. Run `docker exec openclaw-openclaw-gateway-1 openclaw models status --agent <id>`: the `anthropic` line must read `anthropic:setup-token=token:ref(exec:claude-oauth-token)` with `effective=` the agent's own store. Then run `openclaw secrets audit --allow-exec`, which must show no unresolved ref. The live check, and the yearly rotation if the token has expired, are in `docs/secrets-runbook.md`, "Claude service token".

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

# 4. Restore /srv/openclaw/secret-key/ from your private backup.
#    Without this, you have to re-pair every channel.
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
