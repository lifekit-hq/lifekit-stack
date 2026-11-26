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
   `agents` config sets `agentRuntime.id` `claude-cli` on each Claude model entry; only
   the `openai/gpt-5.5` entry keeps `openclaw`);
   OpenAI/codex is not primary for any agent right now. `fable`, the second
   smoke agent, was retired in the 2026-09 fleet reshape. A runtime error fails the run; auth and quota
   errors (expired OAuth, weekly cap) only warn. On a version change it also
   refuses to migrate over files not owned by uid 1000 and asserts every
   official plugin matches the core version afterwards (one update attempt,
   then a red run). First real Telegram message + first cron run remain the
   final word.
5. Last, it runs the read-only post-upgrade smoke check
   (`scripts/upgrade-smoke/openclaw.sh --expect-version <new>`, and
   `claude-code.sh --no-call` inside the gateway when the Claude Code pin
   moved) and prints its `upgrade-smoke <pillar>: PASS|FAIL` line. A FAIL
   turns the deploy red, and the next deploy runs the check again
   (`scripts/upgrade-smoke/README.md` "Where each check runs").

Do NOT use the dashboard's "Update now" or `openclaw update` inside the
container. The install is an npm package baked into the image (no git
checkout, so the UI refuses it), and anything it did install would be gone
on the next container recreate while the Dockerfile still says the old
version. The image is the unit of deployment.

To force a version by hand (skip the Dependabot wait): edit the `FROM` tag,
open a PR, merge. Same path, same checks.

The agent CLIs (`claude`, `clawhub`, `summarize`) and the `workout-claw` skill CLI
are baked into the same image at the exact versions in `compose/openclaw-gateway/package.json`;
Dependabot's npm ecosystem opens their bump PRs. Only those top-level
versions are pinned, not their transitive trees. The entrypoint installs
only remain as a fallback when a command is missing. The other skill CLIs
(`nutrition-claw`, `life-state`) stay entrypoint-installed (host checkouts).

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
counts with no config values. Before doctor it clears the copied gateway
ownership record with `gateway recover-lock` (inspect, then `--confirm` the
fingerprint; the copy holds the live gateway's owner lease, which 2026.11.7+
doctor will not reclaim from another pid namespace and waits ~94 s on); it
never deletes lock files by hand. After doctor it runs `config validate` and
starts a real gateway on the copy (no published port, `/healthz` polled, container
removed), so a migrated config the new schema rejects is red. `--lint-only` is the quick read-only pass against
live state. The copy skips the gateway's `tmp/` and cache dirs (plugin-build
scratch that is deleted while it copies) and treats rsync exit 24 (files
vanished mid-copy) as a warning; any other rsync failure is red. The full
doctor logs stay in the copy, which a red run keeps (pruned after 7 days) and
a green run removes. If a green run cannot remove everything, it prints a
warning and keeps the verdict; delete the leftover copy by hand.

The lint verdict keys on each finding's `checkId`. Any warning or error is red
except four classes the script tolerates: MCP servers that can't be resolved
(the container is off the compose network), Claude CLI "not logged in" (no
Claude credentials are mounted), the gateway's "lan" bind (by design:
`--bind lan` inside the container, loopback-only publish on the host), and the
node-hosting `machine-client-auth` finding at `gateway.auth` (live state since
the trusted-proxy sign-in cutover: the doctor sees identity-header auth with no
token/password and cannot see the edge's node-host routes, which are how a node
host enrols - [Node host enrolment](#node-host-enrolment-through-the-gateway-edge)). `doctor --fix` writes TOOLS.md
migration backups under
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
(`enabled` + `extraPaths`), `tools.codeMode`, the inbound
`hooks` block (with `defaultSessionKey`), the bootstrap limit, the 5.5 default models and the
reading Telegram door (learnclaw's own bot) - live in
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

`tools.codeMode` is authored as `{ "enabled": "auto", "executor": "quickjs" }`.
OpenClaw runs Code Mode with the key absent too (absent means `"auto"`), but on
the default `node` executor, which uses `node:vm` and is not a security
boundary; Kit reads untrusted mail through google-workspace, so the executor is
pinned to the bundled QuickJS WASM one, and an authored value keeps a version
bump from silently changing the tool surface. Selecting `quickjs` loads the
bundled `code-mode-quickjs` plugin without a `plugins.allow` entry; an engaged
run fails closed if that executor is unavailable. **Undo:** set `executor` back
to `"node"` in the patch file (or delete the `tools` block to return to the
runtime default) and merge; the deploy applies it. Because objects merge, a
deleted block does not remove the live key - also run
`openclaw config unset tools.codeMode` in the gateway container, or use the
`"node"` edit instead.

The deploy's CLI calls that touch live gateway state (this patch's dry run and
apply, `secrets audit`, the `gateway.trustedProxies` write) go through
`docker compose exec` into the running gateway, not a `run --rm` one-shot:
OpenClaw 2026.10.2 refuses those from another PID namespace ("cannot verify
Gateway ownership"). With no gateway running, the patch apply and the audit
fail the deploy with a clear message; `deploy-trusted-proxies.sh` falls back to
the one-shot, since there is no owner to verify. The upgrade steps run with the
gateway stopped and stay `run --rm`.

#### DevClaw MCP server (per-agent half)

Kit calls DevClaw's MCP tools (and every agent its intake tool, see "Development-class
problems: file and carry on") through `mcp.servers.devclaw`, which lives in the
per-agent half of `openclaw.json` (host state), not in `platform.patch.json`.
Its `url` is `http://devclaw-mcp:8000/mcp`, reached over the external
`lifekit-shared` network that the gateway and `devclaw-mcp` already share, and
its `headers` carry `Authorization: Bearer ${DEVCLAW_MCP_TOKEN}` as an env
template, like finance-sentry's `${FINANCE_SENTRY_MCP_TOKEN}`. The gateway
resolves the template from its own environment, which
`compose/openclaw/docker-compose.yml` fills from `DEVCLAW_MCP_TOKEN` in
`stack.env`; the value never appears in git or in the config file. Check with
`docker exec openclaw-openclaw-gateway-1 openclaw mcp status`
(`mcp probe` needs the gateway stopped since 2026.11.4; `scripts/upgrade-smoke/openclaw.sh`
fetches each server's URL from the gateway instead).

**Undo:** run `openclaw mcp unset devclaw` in the gateway container, then
remove the `DEVCLAW_MCP_TOKEN` line from the gateway's `environment` in
`compose/openclaw/docker-compose.yml` and redeploy. The edge keeps reading the
same token for the devclaw console, so it stays in the master SOPS file.

#### Skill Workshop approval key retired (OpenClaw 2026.10.5)

OpenClaw 2026.10.5 retired Skill Workshop proposals and removed
`skills.workshop.approvalPolicy` (and `maxPending`) from the schema; the patch
dry run rejects it as `Unrecognized key`, and a rejected dry run applies none of
the file. There is no equivalent key: with no proposal flow there is nothing for
the owner to approve, so the platform file sets no `skills` key. Automatic
learning is `skills.workshop.autonomous.mode`, off on this host and host state.

#### Telegram bot tokens: enabled accounts only

The platform file owns every Telegram account's `botToken`: a SOPS exec
SecretRef (`docs/secrets.md`) for an enabled account, and `null` for a disabled
one. In a patch, `null` deletes the key. The gateway resolves secrets only for
enabled accounts, but a Telegram channel restart (any `channels.telegram.*`
edit) stops every configured account, including disabled ones. OpenClaw
2026.9.8 reads the token while stopping an account. A disabled account that
still holds a ref therefore fails the restart with `unresolved SecretRef`, and
hot-reload recovery escalates to a full gateway restart (SIGUSR2). With no ref
on disabled accounts, the same edit restarts only the Telegram channel.

- Disabling an account: set `enabled: false` on the box, and in the same PR set
  the account's `botToken` to `null` here. Until that PR deploys, any Telegram
  edit restarts the gateway.
- Re-enabling an account: in one `openclaw config patch`, set
  `enabled: true` and the ref (copy it from git history), then put the ref back
  here. The next deploy finds the ref already applied. Until the PR merges, a
  deploy deletes the ref again, so merge the PR first or straight after.

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
- **Not `codex`.** The codex agent runtime is off: no agent's model, fallback
  or cron job runs on it, and its seven tools had no callers. Dropping it
  from the list changes the plugin set, so the deploy that ships it recreates
  the gateway. Add it back in the same PR that routes an agent to an
  `openai/*` model on that runtime.
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

The same file turns on OpenClaw's inbound HTTP hooks for two callers and two
agents:

- `hooks`: enabled, `allowedAgentIds: ["finance", "kit"]` (the second is the
  firstmate-reply mapping below; it also lets a token holder target kit
  through the direct `/hooks/agent` endpoint, which the token already lets
  them do for finance), caller-supplied session
  keys off, and two mappings. The first: `POST <hook path>/finance-sentry` runs the
  finance agent in an isolated session and delivers to its Telegram chat. The
  template interpolates `kind` and `eventId` only: the push carries
  identifiers, the agent reads the detail back through its MCP tools. The
  template also tells the agent to end its message with the absolute `appUrl`
  that `get_pending_companion_events` returns per event (one bare link per
  line, none for an event without one, never a built or guessed link), so the
  Telegram message is clickable into the app.
  The second, `POST <hook path>/firstmate-reply`, is called by the reply
  pusher (see "Reply pusher"): it runs kit in an isolated session with a
  template that interpolates `home` and `noteId` only, and delivers to the
  owner's Kit DM through an explicit `channel: "telegram"` and
  `to: ${OPENCLAW_KIT_CHAT}`. Kit's persistent main session has no last route
  to that chat, so the mapping never delivers to `"last"`.
- `agents.defaults.heartbeat` and `agents.entries.finance.heartbeat`: **removed
  from the platform file with the 2026.11.9 bump** - that release retired both
  keys (`doctor --fix` migrates them into ordinary automation jobs), so
  `config patch` would re-add keys the schema rejects. The post-deploy doctor
  pass does the migration; the finance agent stays off the heartbeat lane (see
  the fs-685 note below). No `agents.entries` key remains in the platform file;
  every per-agent key is host state.
- **No value is in git or in `openclaw.json`.** The file holds the literal
  references `${OPENCLAW_HOOK_TOKEN}`, `${OPENCLAW_HOOK_PATH}`,
  `${OPENCLAW_FINANCE_CHAT}` and `${OPENCLAW_KIT_CHAT}`; OpenClaw resolves
  them from the container environment, which compose fills from
  `/srv/lifekit-secrets/stack.env` (see `.env.example`). `config patch` writes the reference back verbatim, so the
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

#### Asking another agent (firstmate to finance or kit)

`scripts/ask-agent/ask-agent.sh <finance|kit> "<question>"` is the outbound half of
agent-to-agent asking; the return half is the relay both agents already have. It
POSTs once to the gateway's direct `/hooks/agent` endpoint (the token already
reaches it; `allowedAgentIds` lists both agents) with `deliver: false`, so nothing
goes to Telegram. The message carries a request id and tells the agent to answer
firstmate with a relay note whose first line is `ask <request id>`; the answer
lands as an ordinary inbox note and the script printed the id to match it. No
queue, no broker, no mapping, no change to the platform file.

The script reads its configuration from the environment: `ASK_AGENT_HOOK_URL` (the
full `/hooks/agent` URL) and `ASK_AGENT_HOOK_TOKEN_FILE` (a path; or
`ASK_AGENT_HOOK_TOKEN`). **Operator step:** the hook credential is provisioned
on the VPS outside any repo after the owner's recorded approval; the script fails
with exit 78 and a message naming the missing setting until then, exit 77 when the
gateway refuses the token. It passes the token to curl on stdin only and never prints it.

The pulse's checklist is not config. It is the `payload.message` of the
`ledger-pulse` cron row: an `agentTurn` run receives only that message (the
per-job scratch, and the workspace `HEARTBEAT.md`, which is a no-op in
2026.9.4, never reach it; see "The finance heartbeat, retired for an isolated
cron job" below for why the pulse is a cron row and not the heartbeat lane).
`scripts/ensure-finance-pulse.sh` declares it from `scripts/finance-pulse.md`:
the message is a short contract preamble (`NO_REPLY` or exactly one message) followed by
the checklist verbatim. The same script declares the row's `payload.timeoutSeconds`
(`TIMEOUT_SECONDS` in the script, with the headroom rationale). Run it on the host
after the `ledger-pulse` row has been created on the gateway; it is idempotent and
rewrites the message with `openclaw cron edit --message` and the timeout with
`--timeout-seconds`, each only when it differs. The edit hot-reloads and
restarts nothing. The checklist's closing `Links:` paragraph has the pulse end
a sent message with the matching app page as a plain URL (`/events`,
`/alerts`, or the single event's own `appUrl`; none for the weekly positioning digest
or a `NO_REPLY`); the app origin is not in git, so the paragraph tells the
agent to take it from a returned `appUrl`.

**The ledger-pulse gate.** The row is gated by a trigger script, so a run with nothing to do costs
no model turn (the token-cost audit, `docs/token-cost-audit.md`: about 0.44 USD a run, most runs ending in
`NO_REPLY`). `scripts/finance-pulse-gate/gate.js` is stored in the row and runs
`pulse_gate.py check` in the gateway; that read-only check calls two finance-sentry MCP tools
(`get_pending_companion_events` with held events included, `get_sync_health`) with the gateway's
`FINANCE_SENTRY_MCP_TOKEN`, and acknowledges or records nothing. It wakes the agent when the run starts at
or before 09:22 Europe/Dublin (the checklist's coverage-memo step, and the Sunday weekly digest), when any
companion event is pending, or when an account provider's last good sync is over 24h old; otherwise the
row fires nothing and leaves no run history, like `job-queue-drain`. A fault (finance-sentry down, token
refused) throws, so the row shows the error and its failure alert fires. What the agent does on a run is
unchanged: the checklist is still its whole task list. `scripts/ensure-finance-pulse.sh` installs the two
gate files to `/srv/openclaw/config/gates/ledger-pulse/` and sets the row's `--trigger-script`, only when
the stored script differs; the same script converges the row's failure alert (`--failure-alert
--failure-alert-after 1 --failure-alert-mode announce --failure-alert-channel telegram
--failure-alert-account-id default --failure-alert-to <chat>`, the chat from `FINANCE_PULSE_ALERT_CHAT` or
`LIFEKIT_TELEGRAM_CHAT` in the host env file), so a gate fault always reaches Telegram; after merge run it on the host as `lifekit`, then check one
gated run: `openclaw cron run <id> --expect-final --json` (afternoon runs with nothing pending report no run). **Undo:**
`openclaw cron edit <id> --clear-trigger`, and keep the script from re-adding it (revert this change).

**The model for recurring jobs and the deploy smoke turn.** One model, defined once: `agents.defaults.model.primary`
in `compose/openclaw-gateway/platform.patch.json`, the same file as `modelPolicy.allow`. `scripts/lib/cron-model.sh`
(`cron_model`) reads it and refuses a model that is not on the allow list. The deploy smoke turn
(`openclaw agent --model`), `ensure-kit-cron-models.sh` (every Kit cron row, strictly: a row on another model,
an older Opus included, is repinned), `ensure-needs-you-digest.sh` (digest and mail reader), `ensure-doc-door.sh` and
`ensure-finance-pulse.sh` all take it from there, so no script names a model. `scripts/tests/test_cron_model_policy.py`
fails when a script spells a model out or the policy model leaves the allow list; a dropped `--model` is caught by the
per-script behavioral tests (`test_deploy_openclaw.py`, `test_kit_cron_models.py`, `test_reader_agent.py`,
`test_finance_pulse.py`), which assert the policy model in the stubbed argv. Kit's chat model
is a per-agent entry (host state) and is not covered. The scripts are host steps after the merge: the deploy runs
`ensure-doc-door.sh`, and only the `--check` of the others ("Cron declarations check"); run `ensure-kit-cron-models.sh`,
`ensure-needs-you-digest.sh` and `ensure-finance-pulse.sh` as `lifekit` (each with `--check` first). No heartbeat runs (2026.11.9 retired the key;
the finance pulse is the gated `ledger-pulse` row above), so it makes no model call.

**Gates for the scheduled mail jobs.** Like `ledger-pulse` and `job-queue-drain`, the doc-door scan and the needs-you mail
reader are gated by a trigger script, so a tick with nothing to do starts no model turn and leaves no run history. Both
gates only ask `door.py` in the reader agent's workspace (`scripts/doc-door-gate/scan.js` runs `door.py check`,
`mail.js` runs `door.py mail-check`; each is one read-only mail search, nothing filed, converted or reported). `check` wakes the
scan when a document waits for its report, a Drive filing, report copy or vault log line is pending, or the search shows a message
the scan has not seen; `mail-check` wakes the reader when the reader's own search window has mail. Both also wake on a search that
fails or an answer they cannot read, so a fault never silences the job. A skipped reader tick would make the digest read "the mail
read has not run": `mail-check` stamps its last empty check, and the digest prompt (when the reader is gated) runs
`door.py mail-idle` before it adds a "mail unavailable" line, treating a stamp under 60 minutes old as "No new mail." The deploy installs
`scan.js` (`ensure-doc-door.sh`); `ensure-needs-you-digest.sh` installs `mail.js` and sets the reader row's `--trigger-script`
only when the reader agent and its doc-door skill are in place (otherwise the row runs every tick, as before, and a stale trigger is
cleared). Check one idle and one busy tick: `openclaw cron run <id> --expect-final --json`. **Undo:**
`openclaw cron edit <id> --clear-trigger`, and revert the script change.

**Lanes for market signals.** finance-sentry delivers a severe intraday move (a held name moving 2x its alert bar) immediately by wake; routine `MarketStructure` and `NewsCluster` signals are held and batched into the three-times-a-day `ledger-pulse`, step 1 of `scripts/finance-pulse.md`.

**The payload is stateless (the rule).** Every `agentTurn` cron run is a fresh
session that receives only its `payload.message`, so a rule that needs to know
what an earlier run sent ("no message in the last 7 days", "not yet delivered",
"since the last digest") is answered by guessing, and the guess is almost always
"nothing sent": the run sends again, every slot. The pulse did exactly that with
the rebalance notice (about three a day for two weeks). A payload may decide only
from its own inputs: acknowledged events, live tool data, the run's own date and
time, or a state file the job itself writes and reads (the memory-vault cleanup's
`audits/needs-denys-state.json`). So a notice that must reach the operator once
rides on the event finance-sentry emits once per episode (the `RebalanceProposal`
and `ThesisBreak` companion events, relayed and acknowledged in step 1;
finance-sentry classifies a `ThesisBroken` alert as a `ThesisBreak` event, and the
alert's default dedupe (an open alert on the same thesis suppresses a new one, then
a 24 h silence window) gives one alert and one event per break, which step 1 relays
once it is held for the digest or older than 6 h, so a break is never dropped), live
state such as allocation drift is context for a message that is going out anyway,
and a periodic digest is a fixed slot the run reads from its clock (the Sunday
09:20 run, `session_status` for the time). `scripts/tests/test_cron_payloads_stateless.py`
fails on memory-of-delivery phrasing in any declared cron payload
(`scripts/finance-pulse.md`, the `ensure-*.sh` payloads, the runbook's `--message`
examples). The test gate sees phrasing only; the rule is the review question
"could a run with no memory answer this?".

**A timed-out run that already sent is retried (not fixed here).** The scheduler
re-runs a recurring job after an execution error (30 s, 1 m, 5 m backoff) as a new
isolated session, so the retry cannot see that the first run's message settled; on
2026-10-24 a run delivered and was then marked timed out, and the retry sent the
same notice. Checked against OpenClaw 2026.10.5: no `cron` config key or `cron
add`/`edit` flag turns the retry off or makes it conditional on a settled send, and
a payload rule cannot cover it without reading run history, which the stateless rule
forbids. With the repeating steps gone a retry finds less to send, but it can still
resend: after a timed-out run that already sent, events still pending because the
run was cut off between the send and the acknowledgement (the verdict is recorded
before the acknowledge on purpose), and the Sunday digest or the step 7 coverage read when the first run fails
fast near 09:20, since the first retry starts 30 s after a failure, before the
09:22 cutoff of steps 6 and 7. No guarantee covers these until the OpenClaw change below.
Closing the gap properly takes an OpenClaw change: retry only when no outbound
message settled in the failed run.

**Rollout after a change to `scripts/finance-pulse.md`:** nothing to restart. Once
the change is on `main` and deployed, run `/srv/lifekit-stack/scripts/ensure-finance-pulse.sh`
on the host as `lifekit`; it rewrites the row's `payload.message` (hot-reload),
and `docker exec <gateway> openclaw cron show <id>` confirms it. The rebalance
episode already notified stays quiet until finance-sentry opens a new one. The
mail-reader payload of the needs-you digest changed in the same release:
re-run `/srv/lifekit-stack/scripts/ensure-needs-you-digest.sh` too.

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
set to `"0m"` in the platform file, deployed; the key itself is gone since
2026.11.9, see "Inbound hooks and the finance pulse") and replaces it with a new
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
    "model": "anthropic/claude-sonnet-5-5",
    "timeoutSeconds": 300,
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
so the message carries the checklist (the placeholder above does not), the
timeout matches the script's declared value, and the row gets its gate and its
failure alert (the creation payload above sets neither).

**Verify** after creating or after any change to the row: trigger one run
(`openclaw cron run <id> --expect-final --json`) and confirm the result's
`sessionKey` matches `agent:finance:cron:<id>:run:<uuid>` (a fresh uuid per
run, never the old `agent:finance:main:heartbeat`) and `deliveryStatus` is
`delivered` or `not-requested` (a `NO_REPLY` reply sends nothing, so
`not-requested` there is correct, not a failure). `openclaw cron show <id>`
should report the row `enabled` with `sessionTarget: "isolated"`.

**Rollback** (on 2026.11.8 or older; 2026.11.9 rejects the key): restore `agents.entries.finance.heartbeat` to its pre-fs-685
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
wakes the agent with ids, the pulse carries the digest and the weekly
positioning digest. `ledger-lit-digest` (Sunday 20:00 Dublin, two rows) is also parked; it
is neither retired nor revived by this change. Neither the cron store nor the
finance agent's workspace is in git, so this change is three operator steps on
the host. Step 1 ran on 2026-09-19 against the live gateway: the two parked
`ledger-scan` rows are gone from the cron store. Step 3 (the persona) is
done: the Ledger persona is composed from finance-sentry by
`scripts/ledger-persona/` (pinned in `PIN`), so the hand edits below no
longer apply and stay as the record. Step 2 (the skill allowlist) is pending
the finance agent skill cleanup, which runs once the new persona is live: it
removes the retired-scan leftovers (`thesis-tracker`, `catalyst-calendar`,
`idea-generation`) from the allowlist and the workspace, and the stale
`event-log.jsonl` / `learning-journal.jsonl` files from the workspace
`state/`. Once that cleanup has run, steps 2 and 3 are done; the steps stay
listed for a rebuilt host.

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
   `comps-analysis`, `competitive-analysis`) and the three leftovers of the
   retired scan (`thesis-tracker`, `catalyst-calendar`, `idea-generation`);
   keep `github`, `notion`, `skill-creator`. Arrays replace under
   `config patch`, so the list below is the whole allowlist:

   ```json5
   {
     agents: {
       entries: {
         finance: {
           skills: [
             "github", "notion", "skill-creator",
           ],
         },
       },
     },
   }
   ```

   Dry-run first, then apply, the same two commands as the per-agent patch
   below; `agents.entries` keys hot-reload. Re-assign a deck or model skill
   for an interactive session by adding it back to the list. The current
   full list, with the deck and model skills back, is in
   [Per-agent skill allowlists](#per-agent-skill-allowlists).

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
     "Silence-check" bullet (the pulse checklist owns the weekly positioning digest).
     Reword the first bullet to "Event-driven only. No polling scan, no daily
     digest, no morning brief. The pulse (`ledger-pulse` message) carries
     the digest and the Sunday positioning digest." and the THESIS BREAK bullet to
     "always notify - bypasses every silence rule." Keep the Delivery bullet.

   On a scratch copy of the 2026-09-19 file the three edits leave 18,169
   bytes (17,933 characters) - the size step 3 will produce, not the live
   file's: the scan mechanics were about 500 characters of the persona;
   the rest of the bootstrap cut comes from the dropped skills and from
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
- kit keeps `full` - the github skill needs a host binary (145 exec calls in 30 days),
  and since health folded into kit on 2026-10-07 the three claw CLIs are kit's only write path for food,
  workouts and daily state.
- finance keeps `full` - it reaches its `state/` files (the learning journal)
  with bash (2160 exec calls in 30 days, counted before `ledger-scan` was
  retired).

Command-kind crons (`weekly_log_summary`, and `memory_vault_audit` until it moved to a host
timer) bypass exec policy.

Expected `openclaw security audit` after the patch: `gateway.auth_no_rate_limit`
and `models.weak_tier` cleared; `security_full_configured` (devclaw, until it was retired) and
`agent_skill_mcp_boundary_drift` (kit, finance) remain.

Follow-up, out of scope for this change: the boundary-drift remediation
(sandbox those agents, or split the sensitive MCP servers into a separate
gateway).

#### Per-agent skill allowlists

`agents.entries.<id>.skills` is host state. These are the whole lists; arrays
replace under `config patch`, so a change patches the full list for that
agent (dry-run first, the same two commands as above; the keys hot-reload):

```json5
{
  agents: {
    entries: {
      kit: {
        skills: [
          "github", "skill-vetter", "skill-creator", "secondmate-relay",
          "openai-whisper", "youtube-transcript", "workout-log", "job-queue",
          "video-eval",
        ],
      },
      career: {
        skills: [
          "github", "notion", "browser-automation", "diagram-maker",
          "youtube-transcript",
        ],
      },
      finance: {
        skills: [
          "github", "notion", "skill-creator", "earnings-analysis",
          "earnings-preview", "sector-overview", "dcf-model", "comps-analysis",
          "competitive-analysis", "youtube-transcript",
        ],
      },
    },
  },
}
```

Not on any list:

- `gh-issues`: it tells an agent to spawn fix agents and open PRs; builds go
  through the relay note instead.
- `summarize`, `nutrition-claw`, `workout-claw`, `life-state`: their
  `skills.entries` are disabled, so an allowlist name shows the model
  nothing. The CLIs stay: Kit runs them by command from its persona and its
  cron prompts, and their workspace directories stay where they are.
- `video-frames`: no skill by that name is installed.

The [openclaw-config mirror](#openclaw-config-mirror) picks up the live lists
on its next run.

### OpenClaw audit bundle A (2026-11, lifekit-stack#530)

One change set from the 2026-11-15 setup audit. The repo carries the platform half; the rest is
host state and runs once, in the order below. The deploy applies the patch and, because the
image tag and compose env changed, recreates the gateway once.

**In git (`platform.patch.json`, compose, Dockerfile):**

- `agents.defaults.bootstrapMaxChars: 24000` - Kit's and finance's `AGENTS.md` are over the 20,000
  default and were truncated in the system prompt.
- `duckduckgo` in `plugins.allow` (`tools.web.search.provider` already names it; without the
  allowlist entry it was disabled). `lossless-claw`, `codex` and `browser-automation` entries
  are dropped (`null`): the last two were enabled but never in `plugins.allow`, so they never
  loaded, and dropping keeps today's behaviour (`codex` stays out on purpose, see
  `test_platform_patch.py`; `browser` is the allowed one). `plugins.slots.contextEngine` is
  dropped too: lossless-claw degraded to "legacy" on every turn because the `claude-cli` runtime
  host cannot assemble-before-prompt, compact or call the runtime LLM. The `claude-cli` boundary
  stays.
- `hooks.defaultSessionKey: "hook:lifekit"` (security audit warn; mappings are all `isolated`).
- `agents.defaults.model`, `subagents.model` and `modelPolicy.allow` on
  `anthropic/claude-sonnet-5-5` (plus `anthropic/claude-opus-5-5` allowed for Kit's own chat).
  `ensure-needs-you-digest.sh` defaults to the same Sonnet.
- `channels.telegram.accounts.reading.enabled: false`; learnclaw answers through Kit only. (Re-enabled on its own token by the daily LeetCode problem, #594; see "Daily LeetCode problem".)
- Gateway env `NODE_COMPILE_CACHE=/var/tmp/openclaw-compile-cache` (dir made in the image) and
  `OPENCLAW_NO_RESPAWN=1`.
- OpenClaw 2026.10.9 -> 2026.11.3 (Dockerfile tag). The 2026.10.10 changelog has no breaking
  change for these keys; the deploy's upgrade path backs up the config and re-pins official
  plugins as usual.

**Host steps (operator, gateway container `openclaw-openclaw-gateway-1`).** Steps 1 and 2 (per-agent
models, then the cron model scripts) run BEFORE the merge, from a checkout of the PR branch:
`/srv/lifekit-stack` only gets the new `scripts/ensure-kit-cron-models.sh` (and the 5.5 default in
`scripts/ensure-needs-you-digest.sh`) when the merge deploys. Alternatively, run the digest script
from `/srv/lifekit-stack` with `NEEDS_YOU_DIGEST_MODEL=anthropic/claude-sonnet-5-5` set. The single
gateway recreate comes last, after the merge deploy; it must not run before steps 1 and 2, because
the new `modelPolicy.allow` lists only the 5.5 models. Steps 3 onward run after the deploy.

1. Per-agent models (host-state half; `openclaw config patch` merges, so unlisted keys stay):
   career, finance and learnclaw `model` -> `anthropic/claude-sonnet-5-5`; Kit's `model` ->
   `anthropic/claude-opus-5-5`. Check the agent ids with `openclaw agents list`.
2. `scripts/ensure-kit-cron-models.sh --check`, then without `--check`: every Kit cron job whose
   `payload.model` is empty or is not the repo's cron model (`agents.defaults.model.primary` in
   `compose/openclaw-gateway/platform.patch.json`, read by `scripts/lib/cron-model.sh`) is pinned to it,
   so a row left on an older Opus (`claude-opus-4-7`) converges too (`openclaw cron list` must then show
   the cron model on each model-running job; `command` jobs run no model and are skipped). Also run
   `scripts/ensure-needs-you-digest.sh`, `ensure-doc-door.sh` and `ensure-finance-pulse.sh`: each
   declares its own row's model from the same place.
3. Pin the plugin installs: `openclaw plugins install "@openclaw/diagnostics-prometheus@<core>" --force`
   and the same for `@openclaw/duckduckgo-plugin` (the core version is `openclaw --version`); then
   `openclaw plugins inspect <id>` shows the exact version and `openclaw security audit` drops
   the unpinned-spec warning.
4. Tool allow-list: run one verification turn first (`openclaw agent --agent kit -m "list your
   tools" --json`) and confirm the Workspace and finance-sentry MCP tools reach Kit under their
   real names. Only then remove the unknown `tools.allow` names startup warns about (`edit`,
   `process`, `video_generate`, `show_widget`, `progress_card`, `ask_user`, `mcp__*`, `memory_*`,
   `openclaw__*`) and add `intent` to Kit's and finance's allow. `tools.allow` is a replacing
   array, so send the whole list.
5. After Kit's `AGENTS.md` is under the new limit, add one line to it naming what Kit
   backgrounds to a sub-agent (research, long tool work).
6. Firewall on 18789: `gateway.bind` `lan`, `gateway.auth.mode` `trusted-proxy`,
   `gateway.trustedProxies` exactly `10.231.78.2`, and direct access blocked
   (`curl -m 3 http://<host tailnet ip>:18789/healthz` fails; `127.0.0.1:18789` is unpublished in
   trusted-proxy mode). `openclaw security audit` must show no critical finding.

**Acceptance (48 h after):** no "truncating in injected context", no "[context-engine] ...
degraded" and no duckduckgo config warning in the gateway log; one real Kit turn where
`web_search` returns results; `openclaw models status` shows the 5.5 models; `openclaw channels
status` shows `reading` disabled (until the daily LeetCode problem's operator steps enable it again).

### The career weekly cron

Since the 2026-09 fleet reshape (see `docs/openclaw-agent-design.md`), career
has one delivering cron job, `career-weekly`. What each run does is in the
agent's `AGENTS.md` (`defaults/agents/career/workspace/`). It is paused: the
row was disabled on 2026-10-07 until the job hunt restarts, and it comes back
with `openclaw cron enable <id>` (id from `openclaw cron list --all`). Turn
the disabled `career` Telegram account back on first: see "Telegram bot
tokens: enabled accounts only" above. Its
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
`kit` agent, 20 minutes after the weekly vault audit
(`scripts/memory-audit/`, "Weekly vault audit (host timer)" below). It runs the vault design's model jobs, and only on the
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
depends on this row - the weekly audit keeps running unchanged either way.

### Weekly vault audit (host timer)

The mechanical half of the vault's upkeep: lint, index, rotation and the report
`audits/latest.md`, once a week. `scripts/memory-audit/memory-vault-audit.sh` runs the vault's
own `bin/audit.sh --rotate --log` (the logic lives in the vault, see
`scripts/memory-audit/README.md`) and checks a fresh report for today exists.
It ran as the OpenClaw command-kind cron `memory_vault_audit` until the gateway's API credits
ran out (2026-11-09); the scheduler is now `lifekit-vault-audit.timer`, the audit itself is
unchanged.

| What | Value |
| --- | --- |
| Schedule | Sunday 03:30 `Europe/London` (the cron's slot; the model pass follows at 03:50), `Persistent=true`: a Sunday the box was off runs at the next boot |
| Runs as | `lifekit` (owns `/srv/memory`; in the `docker` group for the notification) |
| Command | `run-vault-audit.py` from the checkout in `/srv/lifekit-stack`, which runs the wrapper with `MEMORY_VAULT=/srv/memory`. A merge changes the next run; only the two unit files are installed |
| Output | `audits/latest.md`, `audits/findings.json`, the rotation moves and one `audit` line in the vault `log.md`; `memory-sync.timer` commits and pushes them |
| Notification | one `info` envelope through notify-relay with the wrapper's summary line (`Vault audit \| 0 high / ...`); on failure an `act` envelope with the `AUDIT FAIL:` line and the service fails, which the unit gauge and the "host systemd unit is down or failed" rule report. The cron's Telegram announce and its failure alert are replaced by these two |

What changed in the wrapper for the host: its temp file is a `mktemp` file instead of a fixed
`/tmp` path (a host `/tmp` is shared, the container's was not), and `jq` exists on the host,
so `findings.json` is written by the `jq` branch of `audit.sh` instead of the `awk` one (same
shape). The `/home/node/memory` default stays so the cron keeps working until it is removed.

**Install (operator, `denys`, once, after the merge has deployed):**

```bash
sudo bash /srv/lifekit-stack/scripts/memory-audit/install-vault-audit.sh
systemctl list-timers lifekit-vault-audit.timer    # NEXT = the coming Sunday 03:30
```

`bootstrap-vps.sh` runs the same installer, and the `lifekit-*.timer` glob in the unit gauge
picks the timer up. The unit is skipped while `/srv/memory` has no clone.

**Rehearsal and cutover.** Both schedulers fire at Sunday 03:30, and two audits must never
write the vault at once, so the cron is switched off before the first Sunday, not after:

1. `sudo systemctl start lifekit-vault-audit.service`, then
   `journalctl -u lifekit-vault-audit -n 20`: one `Vault audit | N high / ...` line and no
   `AUDIT FAIL`. A run takes one to three minutes. It is a real run: it rotates and logs.
2. `head -12 /srv/memory/audits/latest.md` shows `# Vault audit - <today>`; `log.md` ends with
   one `audit |` line; the Telegram `info` message arrived.
3. Park the cron, reversibly: `docker exec openclaw-openclaw-gateway-1 openclaw cron list --all
   --json` for the id of `memory_vault_audit`, then `... openclaw cron edit <id> --disable`.
4. After the first **scheduled** run (the Sunday after install) the journal shows the same
   success and `systemctl list-timers` has moved NEXT a week on: remove the cron with
   `... openclaw cron rm <id>`. Leave `memory-vault-cleanup` alone; it only reads the report.

`deploy.sh` still rsyncs `scripts/memory-audit/` into the gateway workspace for the cron; that
copy is dead weight once the cron is removed and goes in a follow-up.

**Reading a failure.** `journalctl -u lifekit-vault-audit -n 40` has the `AUDIT FAIL:` line and
the audit's own stderr. `bin/audit.sh not found`: the vault clone is behind or missing. `no fresh
report written`: `audits/latest.md` is not writable by `lifekit` (it needs the `user:lifekit:rw-`
ACL on files another account created) or the date rolled over mid-run. A rerun is
`sudo systemctl start lifekit-vault-audit.service`; it is idempotent for a day.

**Rollback:**

```bash
sudo systemctl disable --now lifekit-vault-audit.timer
sudo rm /etc/systemd/system/lifekit-vault-audit.{service,timer} && sudo systemctl daemon-reload
```

If the cron is only parked, `openclaw cron edit <id> --enable` brings it back. If it was
removed, recreate it (checked against 2026.10.5; `OWNER_CHAT` is `LIFEKIT_TELEGRAM_CHAT` from the
host env file, never written into git), then add the failure alert as in "The career weekly cron":

```bash
docker exec openclaw-openclaw-gateway-1 openclaw cron add --name memory_vault_audit --agent kit \
  --cron "30 3 * * 0" --tz Europe/Dublin --session isolated \
  --command "sh /home/node/.openclaw/workspace/memory-audit/memory-vault-audit.sh" \
  --output-max-bytes 4000 --announce --channel telegram --to "$OWNER_CHAT" --json
```

### Agent memory in the vault

Kit (`kit`) and Ledger (`finance`) keep their workspace `memory/`
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
- **Only these two agents.** The other agents keep their memory in their
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
for id in kit finance; do
  src=/srv/openclaw/config/agents/$id/workspace/memory
  dst=/srv/memory/agents/$id/memory
  install -d -m 755 "$dst"
  [ -d "$src" ] || continue
  rsync -a --ignore-existing --exclude=/dreaming/ --exclude=/.dreams/ "$src/" "$dst/"
  rm -rf "$src/dreaming" "$src/.dreams"
done
cd /srv/memory
grep -q 'lifekit-stack#175' log.md ||
  sh bin/log.sh "agents/{kit,finance}/memory now hold those agents' workspace memory notes, bind-mounted into the gateway (lifekit-stack#175)"
SH
```

`bin/log.sh` commits only its own `log.md` line. The copied notes are
committed by the next `memory-sync.timer` run, never by hand. An exact
`YYYY-MM-DD.md` daily note older than 90 days is retired by the next Sunday
weekly vault-audit rotation, and git history keeps it; session-memory notes
with a suffix after the date are not matched by the rotate glob and stay. The
originals stay in the workspace, hidden under the mount, which is what makes
the rollback lossless.

**Rollback:** remove the two mount lines from both services and let the
merge redeploy. The agents go back to their workspace copies. Notes written
while the mounts were live stay in the vault, under `agents/<id>/memory/`.

**Verify** after the deploy:

- Send `/new` to one of the three agents. A dated note appears under
  `/srv/memory/agents/<id>/memory/`, named `YYYY-MM-DD-HHMM.md` or
  `YYYY-MM-DD-<slug>.md`.
- A `memory_search` from that agent finds the note.
- `docker exec openclaw-openclaw-gateway-1 openclaw doctor` is clean.
- `docker exec openclaw-openclaw-gateway-1 node -e "fetch('http://127.0.0.1:18789/healthz').then((r)=>r.text()).then(console.log)"` prints `{"ok":true,...}` (the host port exists only in token mode).

### Workspace memory files are pointers

`MEMORY.md` and `USER.md` are upstream OpenClaw bootstrap files: the gateway
loads them into each agent's startup context. The vault is the one memory
store, so in the kit, career, finance and learnclaw workspaces both files hold
a pointer to `~/memory/CORE.md` and no facts. `deploy.sh` runs
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
two files are confirmed in the vault.

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
`payload.toolsAllow`, a cap the gateway enforced when the agents ran on the `openclaw`
runtime and has not been re-verified under `claude-cli`, which they run on now. The job runs
as the `reader` agent ("Reader agent"), whose own tool cap is proved on `claude-cli` by
`scripts/reader-canary/run.sh`: the run has no relay, `ask_user`, `sessions_send`, `message`,
file-write or Gmail send/draft tool, and its one command is the document door script,
whatever the mail says. It delivers nothing; its run stays in its run history, whose
`summary` is all of the run's text, kept from the head and cut at 2000 characters. The
reader writes no text but its final reply, a mail section of under 1500 characters
between a `<<<MAIL-SECTION` line and a `MAIL-SECTION>>>` line.
The digest runs on `anthropic/claude-sonnet-5-5` (`NEEDS_YOU_DIGEST_MODEL` overrides; `ensure-needs-you-digest.sh` exits non-zero when the live row's `payload.model` differs) and does not read the ledger or the relay itself: its first step runs `skills/secondmate-relay/needs-you-digest.mjs`
(installed by `ensure-secondmate-relay.sh`), which prints the open ledger entries oldest first with lettered options, the live fleet decisions, and the already-counted Later, Fleet, partial-view and not-shown figures, or a `needs-you digest failed:` / `fleet status unavailable:` line. The model keeps the call-versus-to-do judgment, the mail section and the final message.
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
`agent-mail-sent` alert pings the owner's phone on any send (`severity: critical`). A lapsed Google sign-in, a failed reader
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
`scripts/ensure-needs-you-digest.sh` as `lifekit` (deploy does not converge it; it only runs its
`--check`, so a live row that differs turns the deploy red until the script is re-run, see
"Cron declarations check").

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
  (one reply per note) and then `drain --ack <id>`. Kit shows replies when asked, and
  the reply pusher (see "Reply pusher") tells Kit at once when a `kit-` note is answered.

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
the gateway (`docker compose -p openclaw --env-file /srv/lifekit-secrets/stack.env -f
docker-compose.yml up -d --force-recreate openclaw-gateway` in `compose/openclaw/`).

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

#### openclaw sender (fleet-gate stops)

The gateway's `fleet-gate` plugin files the P16 stops it blocks ("Fleet gate" below) as
`note oc-<day>-<hash>`. It is a fourth authorized key on the same root-owned forced
command, not a second script:

- **Sender:** the key's `command=` passes the sender as an argument,
  `command="/usr/local/libexec/kit-relay openclaw"`. The sender picks the request-id
  prefix (`oc-`), the rate bucket (30 new `oc-` notes per rolling hour, per home, counted
  apart from the other senders') and the `receipts` view (only `oc-` notes and their
  replies). Body limits are unchanged (1 B to 32 KiB of UTF-8 without NUL, exit 65).
  `oc-` notes are fleet-internal, not a person's: the reply pusher routes only `kit-`
  notes, so no reply is pushed to Telegram for one.
- **Key:** `/srv/lifekit-secrets/oc-relay/` (`id_ed25519`, pinned `known_hosts`,
  `ssh_config`, the same shape as `kit-relay/`), mounted read-only into the gateway at
  `/run/lifekit/oc-relay` by `compose/openclaw/docker-compose.yml`. The plugin runs
  `ssh -F /run/lifekit/oc-relay/ssh_config relay note oc-...` with the body on stdin.
- **Operator step** (once per host, after the PR that adds the sender and the mount has
  deployed; nothing is filed until it is done, and a stop is still blocked meanwhile, with
  the reason saying filing failed). Same as the finance-sentry steps above with these
  changes: step 1 first (the installed copy needs the `openclaw` arm, else exit 78); in
  step 2 use `oc-relay` for the directory and `-C oc-relay@openclaw-gateway`, `IdentityFile`
  and `UserKnownHostsFile` under `/run/lifekit/oc-relay/`, and hand the directory to the
  gateway's uid (`docker exec openclaw-openclaw-gateway-1 id -u`, 1000); authorize it with

  ```bash
  printf 'restrict,command="/usr/local/libexec/kit-relay openclaw",from="172.16.0.0/12" %s\n' \
    "$(sudo cat /srv/lifekit-secrets/oc-relay/id_ed25519.pub)" >> ~/.ssh/authorized_keys
  ```

  Verify with `docker exec "$G" ssh -F /run/lifekit/oc-relay/ssh_config relay 'note oc-smoke-<day>' <<< 'relay smoke test - ignore'`
  (outcome `created`; answer and ack it from the second mate's home afterwards), and
  `... relay 'note kit-x' </dev/null` is refused, exit 64.
- **Rollback:** delete that authorized_keys line and `sudo rm -r /srv/lifekit-secrets/oc-relay`.
  The other relays are untouched.

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
  `note` also takes the envelope flags and prints a receipt ("Envelope and receipt").
- **Rules** (`SKILL.md`): status questions run `relay.sh status` and answer in one
  format: "Your call" (numbered, options when the record names them, no ids), "Your
  to-dos" (verb first), then a "Later" count line (parked, dated and blocked, counted from
  `queued` and never listed) and a "Fleet" line (`counts.queued_open`, which leaves out
  held items); only a view older than 30 minutes adds "Fleet view is partial", and
  "+N not shown" appears only when `counts.decisions_open` really exceeds the list. Kit never calls `start_google_auth` (its link redirects to localhost and leaves
  a pending sign-in); on a Google sign-in failure it says so once and points at
  `docs/google-mcp-setup.md` steps 6-7. Tasks and proposals go to `firstmate` with the
  `task:`/`proposal:` prefix; an ask another agent made of Kit, an answer to a Needs-you
  card, or an edit to another agent's instructions is relayed only as his reply on his own
  turn: `task: <his words verbatim>` plus one `> context: <the pending ask or card question>
  (repo: <repo>)` line (the skill's "relayed ask"), and OpenClaw files no GitHub issue for
  any ask; when his words answer a firstmate reply Kit showed, the same one context line
  carries the thread anchor, `> context: re <note-id>: <the original ask, shortened>`
  (folded with a pending ask's context when both apply, never a second line, and only on
  his own turn): the new note goes to the home the shown reply came from (`firstmate` or
  `fm-vps`) and points at the original `kit-` note, so that home reads that note and its
  reply before answering, and its one-reply-per-note store is unchanged; an answer is
  `answer <id>: <his words>` sent only on his explicit words, only for an id in `decisions_open` at send time, to that item's
  `home_id`; Kit does not classify decisions or gate anything: under each firstmate
  home's standing answer-note rule an ordinary decision closes on the answer, while a
  decision that would authorize a merge or a destructive, irreversible or
  security-sensitive action gets firstmate's reply saying what it will do and runs only
  on a relayed `confirm <id>` (a records rule applied by firstmate, not enforced by the
  relay, the host forced command or this repo); an id missing from a capped
  `decisions_open` list is reported as "not in the shown list", not "not open"; a reply
  cursor is passed only with the home it came from; nothing is sent from a cron, a
  heartbeat or any turn but his own message (the PC handoff below is the one send he
  does not spell out); a refused call is reported verbatim and not
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
  prompt no longer has them; a prompt already carrying the step keeps its text (the script
  only ensures `--light-context`, see "Light context on `today-brief` and
  `ledger-opportunity`"), and a row carrying the previous version of the step is upgraded
  in place. The step's Google rule
  is evidence-based: the brief makes the real calendar and mail calls on every run, with the
  tool server's own argument names (`page_size`, `calendar_id`), and
  prints "Google sign-in expired - mail and calendar skipped" (with the re-sign-in to-do)
  only when the tool's own error text says the sign-in is expired or revoked; any other
  failure, or Google tools not available, prints "Google mail and calendar unavailable:
  <the error line, or tools not available>." and adds no to-do. **Operator step after a
  deploy that changes the step:** as `lifekit`, from `/srv/lifekit-stack`, run
  `scripts/ensure-today-brief-fleet.sh`, then check the row with `openclaw cron show`
  (inside the gateway). The step also prints the one decision outcome whose `brief_date` is
  today (`status_page.delivered`, fleet publisher README "Delivered decisions"). An empty
  fleet part reads "No fleet calls today." there, so it never contradicts the brief's
  calendar, mail or PR sections. The step also prints the one-line "Problems: ..." from
  `status_page.problems` (up to three oldest stalled items with owner and age in days, fleet
  publisher README "Problems"), and nothing when there are none.
- **Kit's `AGENTS.md`** is live host state (`/srv/openclaw/config/agents/kit/workspace/`),
  merged by hand like the "Needs you" section. Five lines move with this: the
  host-work line says host work goes to firstmate through the captain inbox, "the main
  home (`firstmate`)"; the Tools table's dashboard row reads `lifekit-dashboard:8080`
  (not the old loopback port `127.0.0.1:18790`); the plain-chat answer line also takes
  `<n> <letter>` for entry `<n>` of the numbered list Kit last sent; the digest
  line says it posts "Nothing needs you today." when nothing is open and no mail
  needs you, not `NO_REPLY`; and the Google Workspace section's line that Kit never calls
  `start_google_auth` and, on a sign-in failure, says so once and points at
  `docs/google-mcp-setup.md`. The relay-only door (no `gh issue create` for build asks)
  is three more lines in the live file: the "Autonomous dev work" bullet, the `ask_user`
  `answered` bullet and the "Never edit another agent's instructions" rule.
- **Handoff to the PC:** Kit's "Anything else" section hands a request that needs a
  logged-in site, a purchase or booking, or the desktop to firstmate, which runs it in
  the browser on the owner's PC while it is on; the VPS does only login-free work. Kit
  says it is handing off, sends `task: <his words verbatim>` to `firstmate` and replies
  with the note id. It does not hand off what it can do itself or what it is merely
  unsure about, and only the owner's own message triggers it: the relay's cron and
  heartbeat ban stays. `skills/lifekit-router` is not on Kit's path. **Operator step:**
  as `lifekit`, paste the "Anything else" section of
  `defaults/agents/kit/workspace/AGENTS.md` over the same section of the live
  `/srv/openclaw/config/agents/kit/workspace/AGENTS.md` (paste the section, not the
  whole file: the live file has drifted from the template; no restart, Kit reads it on
  its next session), then re-run the Deploy line below for the widened `SKILL.md`.
- **Deploy:** `sudo -u lifekit /srv/lifekit-stack/scripts/ensure-secondmate-relay.sh`
  (copies `SKILL.md` and `relay.sh`; no gateway restart). Verify from inside the gateway:
  `docker exec "$G" sh "$S" status | head -c 200`, and one `note <file> firstmate`
  shows `home=firstmate` in `journalctl -t kit-relay`.
- **Rollback:** revert the skill and re-run the ensure script. Notes already sent stay
  in the inbox they were sent to.

#### Development-class problems: file and carry on

The owner is never the messenger. Every OpenClaw agent carries the same persona block,
"Development-class problems": on a defect in a fleet app or tool, a data source or tool that
keeps failing, or a gap in a repo it does not own, the agent files the problem through the
devclaw intake tool (`file_intake`, over the `devclaw` MCP server), says so once in one line of
its own output, degrades and carries on, and never waits for the fix and never messages the
owner about a failure. It names the repo when the owner is obvious and leaves it out when not,
and firstmate routes it. A failed filing is stated in the same line, never relayed. This is
not a notification channel and adds no rule.

- **One source:** `defaults/agents/shared/dev-problems.md`, between its two marker lines.
  `scripts/ensure-dev-problem-block.sh [agent-id ...]` puts it into each workspace's
  `SOUL.md` (all of `agents/*/workspace` when no id is given, less the agents with no door,
  see "Agents with no door" below): it replaces the text between
  the markers, or appends the block when they are absent, and touches nothing else. Re-running
  converges; `DRY_RUN=1` reports without writing; a missing `SOUL.md` or a lone, repeated or
  reversed marker leaves that file alone and makes the exit code 1 after the others finish.
  Ledger's `AGENTS.md` is composed from finance-sentry and byte-compared, so the block goes in
  its host-state `SOUL.md`, like every other agent's. `ledger-persona.py check` exempts the
  block's devclaw mention and still flags a devclaw handoff anywhere else.
- **Learnclaw:** the deploy rewrites its `SOUL.md` from git (`ensure-learnclaw.sh`), which
  then re-applies the block, so nothing is needed by hand there. The other agents' templates
  in `defaults/agents/<id>/workspace/` stay hand-copied, as with the answer-shape section.
- **Externally blocked:** an item that waits on someone outside the fleet is a held record
  with owner `external` (firstmate sets the hold), which the fleet page shows as a held item
  (`records[].hold.owner: "external"`, counted in `projects[].held_external` and
  `queue.held_external`, see `scripts/fleet-publisher/README.md`), not a message.
- **Dependency:** the intake tool lives in devclaw, not here. It must exist, be idempotent
  by repo plus fingerprint (a repeat adds a count to the open issue), and be in the gateway's
  `tools.allow`; until it is, an agent that follows the block states that filing failed and
  carries on. Check by filing one test intake from the main agent and reading the
  issue or queue reference it returns.
- **Week sheet:** the line counting items routed and resolved without the owner against items
  that reached the owner is drafted by firstmate in the vault, not produced by this repo.

Operator steps, once per host after the PR deploys (as `lifekit`; the live workspaces and the
live `AGENTS.md` are host state):

```bash
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-dev-problem-block.sh   # three lines per agent with a door (this block, the a2a envelope, receipts)
```

Then paste the revised devclaw line of `defaults/agents/kit/workspace/AGENTS.md` into the live
kit `AGENTS.md`. Ledger's own persona still tells it to hand build work to Kit with
`sessions_send`; that line moves to this route by a finance-sentry `agent/ledger` change and a
`PIN` bump. **Verify:** `grep -c 'lifekit:dev-problems:begin' /srv/openclaw/config/agents/*/workspace/SOUL.md`
prints 1 for each agent with a door and 0 for the reader and the study panel. **Rollback:**
delete the text between and including the two marker lines in each `SOUL.md`.

#### Agent-to-agent envelope and ack

Agent-to-agent messages (`sessions_send`) are a door like any other: one envelope in, one receipt
back. The rule is the vault build standard "Envelope and receipt at every door" (2026-11-16,
decision 0016); this repo carries it into every agent's persona and does not restate it.

- **One source:** `defaults/agents/shared/a2a-envelope.md`, between its `lifekit:a2a-envelope`
  marker lines, spliced into each workspace's `SOUL.md` by the same
  `scripts/ensure-dev-problem-block.sh` as the block above (both blocks in one pass; a lone or
  reversed marker for either leaves the whole file alone). `ensure-learnclaw.sh` re-applies it.
  `scripts/tests/test_dev_problem_block.py` asserts the envelope fields and the ack text.
- **Ledger:** its persona (finance-sentry `agent/ledger`, pinned by `scripts/ledger-persona/PIN`)
  carries a pointer to this block, not a second envelope text; the pin is at the commit that
  made that change.

Operator steps after the PR deploys (as `lifekit`; live workspaces are host state):

```bash
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-dev-problem-block.sh   # three lines per agent with a door
```

**Verify:** `grep -c 'lifekit:a2a-envelope:begin' /srv/openclaw/config/agents/*/workspace/SOUL.md`
prints 1 for each agent with a door and 0 for the reader and the study panel. Then the exchange
check, from the main agent: have Kit `sessions_send` Ledger one envelope with `type: request`, a fresh `thread` and `reply_expected: true`; read Ledger's
session transcript and confirm its first message back is `type: ack` with that `thread`, a `state`
of `submitted` or `rejected` and where the item lives, before any reply. Send the same `thread`
again and confirm the ack's `state` is `working` and no second item exists. **Rollback:** delete
the text between and including the two marker lines in each `SOUL.md`.

#### Owner asks get a receipt in the same turn

Nothing the owner asks for, and nothing an agent promises, is held in chat or agent memory for
"later" or for a second go. Every agent carries the persona block "Owner asks and commitments: a
receipt in the same turn": the ask or commitment is filed in the intake store in the turn it is
said, with the decision 0016 envelope (`origin`, `owner`, `class`, `fingerprint`, `acceptance`,
`thread`), and the reply shows the owner the receipt id. Reversible work needs no second go
(vault decision 0018 row C1). The doors are the devclaw intake tool (`file_intake` on its intake
projects) and the agent-to-agent envelope to Kit; when a door fails the agent files the failure
itself in the intake store and never hands the item back to the owner. This is gap G1 of vault
decision 0019, at the persona level.

- **One source:** `defaults/agents/shared/receipts.md`, between its `lifekit:receipts` marker
  lines, spliced by the same `scripts/ensure-dev-problem-block.sh` as the two blocks above (all
  three in one pass; `ensure-learnclaw.sh` re-applies them). Ledger's `ledger-persona.py check`
  exempts this block's devclaw mention, as it does the dev-problems block.
- **Drift check:** `scripts/ensure-dev-problem-block.sh --check` writes nothing and exits 1 when
  any workspace (all of `agents/*/workspace`, so an agent defined later is covered) lacks a
  shared block, holds a different text between its markers, has no `SOUL.md` or has a marker
  problem; each failing block is named on stdout as `missing` or `differs`.
- **Agents with no door:** an agent whose `defaults/agents/<id>/agent.json` denies the devclaw
  intake tool and ships `tools.agentToAgent.send: []` (today the reader and the study panel) can
  reach neither door and answers in a parseable format, so the script gives it none of the three
  blocks and `--check` skips it (`no door reachable` on stdout). Its own ensure script rewrites
  its `SOUL.md` from git on each deploy, which leaves `--check` green. Any other agent, including
  one with no definition in the repo such as Ledger, gets the blocks.
- **Dependency:** the doors themselves are not here: the agent-to-agent allow list (#683) and
  the devclaw intake projects. Until they exist, an agent following the block files the door
  failure as the block says.

Operator steps after the PR deploys (as `lifekit`; live workspaces are host state):

```bash
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-dev-problem-block.sh           # three lines per agent with a door
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-dev-problem-block.sh --check   # exit 0, every block "unchanged"
```

**Verify:** `--check` exits 0, and `grep -c 'lifekit:receipts:begin'
/srv/openclaw/config/agents/*/workspace/SOUL.md` prints 1 for each agent with a door and 0 for
the reader and the study panel. Then ask Kit for something
reversible and confirm the same reply carries a receipt id and no "saved for later".
**Rollback:** delete the text between and including the two `lifekit:receipts` marker lines in
each `SOUL.md`.

### Kit's workout log and gym-day nudge

Kit logs a gym session from one typed or spoken line and confirms it in one line, answers
"what did I do last <lift>", and on gym days at 21:00 nudges the owner when nothing is
logged for the day.

- **CLI:** `workout-claw` is baked into the gateway image, pinned in
  `compose/openclaw-gateway/package.json` and bumped by Dependabot like the agent CLIs.
  Its data stays at `${LIFEKIT_STATE_DIR_HOST}/state/workout-claw` (mounted as
  `~/.workout-claw`). The host checkout `/srv/workout-claw` is no longer mounted or
  installed; it can be deleted once the image with the pin has deployed.
- **Skill:** `skills/workout-log` (`SKILL.md` + `nudge.sh`), installed into kit's
  workspace by `scripts/ensure-workout-log.sh`, which also reports kit's allowlist and the
  cron read-only. Voice notes reach kit already transcribed (OpenClaw's audio
  auto-detection, the local `whisper` CLI in the image).

  **Voice-note transcription model.** `tools.media.models` in
  `compose/openclaw-gateway/platform.patch.json` pins audio understanding to the `base`
  model, the only one the image bakes. Without the entry OpenClaw's auto-detected `whisper`
  run uses its own default (`turbo`, about 1.6 GB of weights), which on this CPU-only host
  needs several GiB on top of the gateway and shares the gateway container's memory limit;
  it once triggered an out-of-memory kill inside the container. Verify the live config
  (read-only): `docker exec openclaw-openclaw-gateway-1 openclaw config get tools.media.models`
  must show `--model base`. The operator can check a transcription by hand with
  `docker exec openclaw-openclaw-gateway-1 sh -c 'whisper <audio file> --model base --fp16 False --output_dir <scratch dir>'`.
  A cached `large-v3-turbo.pt` in the whisper cache is unused and safe for the operator
  to delete.
- **Nudge:** the `workout-nudge` cron, a command payload that runs `nudge.sh` (no model
  turn). Its day-of-week field is the gym-days setting: host state in the gateway's cron
  store, never in git. `nudge.sh` asks `workout-claw summary` for today (the gateway's
  local date): nothing logged prints one nudge line, which the cron announces to Telegram;
  a logged session prints `NO_REPLY`, which the scheduler suppresses; a workout-claw error
  exits 1, so the failure alert fires instead of a wrong nudge.

Operator steps, once per host, after the PR has deployed (the deploy rebuilt and
recreated the gateway with the baked CLI). Run as lifekit on the VPS:

```bash
G=$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway)
docker exec "$G" npm ls -g --depth=0 workout-claw   # the version pinned in package.json

# 1. The skill into kit's workspace.
/srv/lifekit-stack/scripts/ensure-workout-log.sh

# 2. Kit's skill allowlist, derived from the live list as in "Kit's second-mate relay"
#    step 5. Skip it when step 1 reported kit as unfiltered.
docker exec "$G" node -e 'const c=require(process.env.OPENCLAW_CONFIG_PATH);const s=c.agents.entries.kit.skills;if(!Array.isArray(s))process.exit(1);if(!s.includes("workout-log"))s.push("workout-log");process.stdout.write(JSON.stringify({agents:{entries:{kit:{skills:s}}}}))' > /tmp/kit-skills.patch.json
docker exec -i "$G" openclaw config patch --stdin --dry-run < /tmp/kit-skills.patch.json
docker exec -i "$G" openclaw config patch --stdin < /tmp/kit-skills.patch.json

# 3. The nudge. GYM_DAYS is the cron day-of-week list (0-6, Sunday is 0; e.g. 1,3,5);
#    the chat is the owner's, from the env file. `cron add` has no failure-alert
#    flags, so the edit sets them.
GYM_DAYS=<gym days>
CHAT=$(sed -nE 's/^[[:space:]]*LIFEKIT_TELEGRAM_CHAT=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\1/p' \
  /srv/lifekit-secrets/stack.env)
docker exec "$G" openclaw cron add \
  --name workout-nudge \
  --description "Gym days 21:00: nudge the owner when no workout is logged today. Declared in docs/runbook.md (source: lifekit-stack)." \
  --agent kit --session isolated \
  --cron "0 21 * * ${GYM_DAYS}" --tz "$(docker exec "$G" printenv TZ)" --exact \
  --command-argv '["sh","/home/node/.openclaw/agents/kit/workspace/skills/workout-log/nudge.sh"]' \
  --timeout-seconds 20 --no-output-timeout-seconds 10 --output-max-bytes 1000 \
  --announce --channel telegram --account default --to "$CHAT"
ID=$(docker exec "$G" openclaw cron list --json \
  | docker exec -i "$G" node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const j=JSON.parse(s);process.stdout.write((j.jobs||j).find(x=>x.name==="workout-nudge").id)})')
docker exec "$G" openclaw cron edit "$ID" \
  --failure-alert --failure-alert-after 1 \
  --failure-alert-mode announce --failure-alert-channel telegram \
  --failure-alert-account-id default --failure-alert-to "$CHAT"
```

- **Kit's `AGENTS.md`** is live host state, merged by hand: its workout routing points
  typed or spoken session lines and "what did I do last <lift>" at the `workout-log`
  skill. A planned-session flow the workspace already has (a "done" reply that logs the
  day's plan) stays; the skill defers to it.
- **One nudge:** any older gym nudge in kit's workspace (a sweep script or cron prompt
  that names fixed gym days) loses its gym part, so the owner gets one nudge, on the days
  this cron names.
- **Changing the gym days:** `docker exec "$G" openclaw cron edit "$ID" --cron "0 21 * * <days>"`.
- **Verify:** `docker exec "$G" sh /home/node/.openclaw/agents/kit/workspace/skills/workout-log/nudge.sh`
  prints the nudge line on a day with nothing logged and `NO_REPLY` once a session is
  logged; it reads only. Then send kit one session line and check the one-line confirm
  matches `workout-claw summary`.
- **Rollback:** `docker exec "$G" openclaw cron rm "$ID"`, re-apply kit's old skills list, and remove the
  skill folder from kit's workspace. The CLI pin rolls back with the image ("Rolling back
  OpenClaw").
- **Not declared in git.** No `ensure-*.sh` owns `workout-nudge`, so the deploy's cron
  declarations check does not cover it: the row above is an operator step and its live state
  is the operator's. Running step 3 twice leaves two rows of the same name; keep one with
  `cron rm <id>` (the 2026-11-26 check found one enabled and one disabled copy).

### Kit's evening day plan (command cron)

This row is an operator step, not declared by an `ensure-*.sh`, and the deploy's cron
declarations check does not cover it. It may be paused live (the 2026-11-26 check found it
disabled): `docker exec "$G" openclaw cron enable <id>` turns it back on, and nothing in git
expects either state.

The 21:00 `evening-day-plan` row is a command payload, not an agent turn: its message is a
fixed line, so a model only paid for reading two CLIs and copying numbers.
`skills/workout-log/day-check.sh <kcal target> <protein target g>` (installed with the
workout-log skill by `scripts/ensure-workout-log.sh`) runs `nutrition-claw summary` and
`workout-claw summary` for today and prints the whole delivery line:

```
Day check | nutrition 1840kcal/2278 160g -> 112g under | gym Wed: done (legs) | tomorrow's priorities?
```

kcal is eaten/target; the protein part is target -> eaten, then `under`, `over` or
`on target`; the gym part is `done (<muscle groups>)` or `no session`. The two targets are
the owner's numbers from the vault health page and live in the cron row (host state),
never in git. If either CLI fails or its summary lacks the numbers, the script prints
the reason on stderr and exits 1, so the failure alert fires instead of a wrong line.
`day-check.sh` passes `--date "$(date +%F)"` (the gateway's TZ) to both CLIs, reads `consumed` under
the `calories_kcal` and `protein_g` blocks, prefers the CLI's own `goal` over the arguments, and
names the distinct exercise `muscle` values (falling back to `muscle_group`). Rehearse read-only:
`docker exec -i "$G" sh -s -- <kcal> <protein-g> < skills/workout-log/day-check.sh`.

Operator steps, after the PR has deployed and `ensure-workout-log.sh` has run. Run as
lifekit on the VPS; read the old row first (`cron show`), and keep its delivery
destination if it differs from the owner's Telegram chat below:

```bash
G=$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway)
docker exec "$G" sh /home/node/.openclaw/agents/kit/workspace/skills/workout-log/day-check.sh <kcal> <protein-g>   # rehearse: prints the line, sends nothing
OLD=$(docker exec "$G" openclaw cron list --json \
  | docker exec -i "$G" node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const j=JSON.parse(s);process.stdout.write((j.jobs||j).find(x=>x.name==="evening-day-plan").id)})')
docker exec "$G" openclaw cron show "$OLD"   # note the schedule and delivery
CHAT=$(sed -nE 's/^[[:space:]]*LIFEKIT_TELEGRAM_CHAT=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\1/p' \
  /srv/lifekit-secrets/stack.env)
docker exec "$G" openclaw cron rm "$OLD"
docker exec "$G" openclaw cron add \
  --name evening-day-plan \
  --description "21:00 day check: nutrition and gym lines printed by day-check.sh, no model turn. Declared in docs/runbook.md (source: lifekit-stack)." \
  --agent kit --session isolated \
  --cron "0 21 * * *" --tz "$(docker exec "$G" printenv TZ)" --exact \
  --command-argv '["sh","/home/node/.openclaw/agents/kit/workspace/skills/workout-log/day-check.sh","<kcal>","<protein-g>"]' \
  --timeout-seconds 20 --no-output-timeout-seconds 10 --output-max-bytes 1000 \
  --announce --channel telegram --account default --to "$CHAT"
ID=$(docker exec "$G" openclaw cron list --json \
  | docker exec -i "$G" node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const j=JSON.parse(s);process.stdout.write((j.jobs||j).find(x=>x.name==="evening-day-plan").id)})')
docker exec "$G" openclaw cron edit "$ID" \
  --failure-alert --failure-alert-after 1 \
  --failure-alert-mode announce --failure-alert-channel telegram \
  --failure-alert-account-id default --failure-alert-to "$CHAT"
```

Rollback: `cron rm "$ID"` and re-add the old prompt row from the `cron show` output.

### Shared job queue

Work that needs an egress the server lacks waits on one shared, typed queue and runs on its
own when the egress answers. Today the egress is the owner's PC (the yt-tunnel relay, "YouTube
transcripts through the PC"), so the owner can send work while the PC is off and get each
answer once it is on. Video evaluations are its first kind ("Video evaluation").

- **Skill:** `skills/job-queue` (`jobq.py`, stdlib only, plus `drain-gate.js` and a short
  `SKILL.md`), installed by the deploy into the gateway's shared skills directory
  (`/srv/openclaw/config/skills/job-queue`, `~/.openclaw/skills/job-queue` in the container) by
  `scripts/ensure-job-queue.sh`, which also reports read-only whether the drain cron row exists
  and runs the shipped gate. A change to either triggers the OpenClaw phases.
- **Jobs:** one private JSON file per job, `<kind>-<key>.json` (mode 0600) in
  `~/.life-state/job-queue/` in the gateway, i.e. `/var/lib/lifekit/job-queue/` on the host
  (mode 0700). A job is keyed by its kind and key (a reel's shortcode, say), so the same reel
  sent twice is one job; it names the handler command the queue runs, the host its egress pre-check
  connects to (none for a file already here), its payload and a one-line label. Finished jobs are pruned after 14 days
  (`JOBQ_KEEP_DAYS`). Nothing about a job's result beyond its first line is kept here, except a
  finished run's output (`<job id>.out`, 0600) until a drain turn reads it. A run that dies is
  never silent: the job records the run's exit status (`run.exit`; a signal reads
  `killed by SIGKILL`) and its last stderr lines in `last_error`, a runner told to stop
  (SIGTERM/SIGHUP/SIGINT) records that and kills its handler, and the runner's own stderr is kept in
  `<job id>.err` (0600). When the queue gives up on a job whose runs never recorded an end, the
  job's `result` carries that last error, or says the runner was gone with its stderr.
- **Reading a killed run** (lifekit-stack#676): `run` in the job file is the runner's own
  record. `via` says who started it (`launcher`, or `gate` when no launcher was running);
  `stage` is how far it got (`runner started`, `handler running`, `handler ended`) and
  `alive_at` its last sign of life (once a minute), so `alive_at - runner_at` is how long it
  lived. A runner told to stop adds `signal`, `ran_for` (seconds) and a `last_error` of the form
  `the runner was stopped by SIGTERM at stage 'handler running' after 4m10s`. One with `stage`
  but no `ran_at` was killed outright (SIGKILL, the OOM killer, a gateway restart). One with no
  `stage` died before its first step: under `via: gate` that is the exec tool's cleanup below.
  A `run` holding only `requested_at` is waiting for the launcher; after 120 s it counts as a
  failed attempt, and the last attempt gives up as `no launcher started its runner`.
- **Egress, configured in one place:** `LIFEKIT_EGRESS_PROXY` in
  `/srv/lifekit-secrets/stack.env`, passed to the gateway by `compose/openclaw/docker-compose.yml`.
  Unset or empty is the PC relay, `socks5h://host.docker.internal:18081`; it takes any
  `socks5h://` or `http://` proxy URL, or `direct`. The queue's readiness pre-check and every
  handler's downloads use that one value, so a later server-side drainer (yt-dlp through a
  rented proxy, say) is a value change, not a code change. Changing it recreates the gateway on
  the next deploy.
- **Drain:** one cron row on kit, `job-queue-drain`, every 2 minutes. Its trigger script
  (`drain-gate.js`, stored in the cron row) runs `jobq.py next`. When a job can run, `next`
  claims it and asks the launcher to start it, and wakes no agent: the run belongs to the queue,
  not to a turn, so neither a tool's timeout nor a turn ending can kill it mid-run (a long fetch
  or transcription on a busy host once outlived the agent's exec timeout and never reported
  back). The launcher (`jobq.py launcher`) is a long-lived process the gateway's entrypoint
  starts outside every exec, in a loop that restarts it when it exits; it starts the runner
  (`jobq.py runner`) within a second and leaves when a deploy installs a new `jobq.py`, so the
  loop picks the new code up. `next` cannot start the runner itself: since OpenClaw 2026.11.9 the
  exec tool runs each command under a child-subreaper anchor that kills every process the
  command left behind, however detached, the moment it ends (lifekit-stack#676). With no
  launcher running (by hand on a host, in tests) `next` still starts it, as `via: gate`.
  `docker exec <gateway> python3 /home/node/.openclaw/skills/job-queue/jobq.py launcher-status`
  says whether one runs; its own errors go to `.launcher.err` in the queue directory. The
  runner keeps what the handler printed.
  Only a run that printed an answer wakes the agent: the next drain turn gets one appended
  instruction (`Run exactly: python3 .../jobq.py output <job id>`), which prints that output
  once, and the owning skill says how to answer. Nothing queued, a job already running, or no
  queued job whose egress answers a real CONNECT to its host all skip the turn with no run
  history. A job with no probe host (a file already on the server) needs no egress, so a link
  waiting for the PC never holds it back. Jobs run one at a time, the oldest one whose egress
  answers first; a run's output is handed to a turn before the next job starts.
- **Exactly one answer per job, bounded:** a run that fails while the egress answers counts an
  attempt and is retried quietly (`NO_REPLY`); the third failure (`JOBQ_MAX_ATTEMPTS`) is the
  one failure message. A run that fails because the egress went away mid-run gives its attempt
  back and waits. A run is bounded by `JOBQ_RUN_MINUTES` (90); one that ran over, crashed, or
  whose runner died (a gateway restart) is taken up again on the next drain, and given up
  with one message once those runs reach the attempt limit. A run's output that 3 drain turns
  in a row did not read is given up with one message too, and one read by a turn that then
  neither filed nor released the job is taken up again after 30 minutes
  (`JOBQ_LEASE_MINUTES`).

Operator step, once per host, after the PR has deployed (a deploy run shows `cron:
'job-queue-drain' is not set up` until it is done). Run as lifekit on the VPS:

```bash
G=$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway)
# The answers go to the owner's chat, from the env file. `cron add` has no failure-alert
# flags, so the edit sets them.
CHAT=$(sed -nE 's/^[[:space:]]*LIFEKIT_TELEGRAM_CHAT=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\1/p' \
  /srv/lifekit-secrets/stack.env)
docker exec "$G" openclaw cron add \
  --name job-queue-drain \
  --description "Every 2 min: run one ready job from the shared job queue when its egress answers. Declared in docs/runbook.md (source: lifekit-stack)." \
  --agent kit --session isolated --every 2m \
  --trigger-script /home/node/.openclaw/skills/job-queue/drain-gate.js \
  --message "Drain the shared job queue: follow the job instructions below exactly, one job, then answer as they say. With no job instructions below, reply exactly NO_REPLY." \
  --timeout-seconds 900 \
  --announce --channel telegram --account default --to "$CHAT"
ID=$(docker exec "$G" openclaw cron list --json \
  | docker exec -i "$G" node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const j=JSON.parse(s);process.stdout.write((j.jobs||j).find(x=>x.name==="job-queue-drain").id)})')
docker exec "$G" openclaw cron edit "$ID" \
  --failure-alert --failure-alert-after 1 \
  --failure-alert-mode announce --failure-alert-channel telegram \
  --failure-alert-account-id default --failure-alert-to "$CHAT"
```

- **A changed gate:** the row stores the script, so when a deploy reports the gate `DIFFERS`,
  re-apply it: `docker exec "$G" openclaw cron edit "$ID" --trigger-script /home/node/.openclaw/skills/job-queue/drain-gate.js`.
- **What is waiting:** `docker exec "$G" python3 /home/node/.openclaw/skills/job-queue/jobq.py list`
  (`depth` prints the counts only, as JSON: what the fleet status page shows; `ages` prints each
  job's state and times with no label or result: what its stalled check reads).
  Do not run `jobq.py next` by hand while the egress answers: it starts the next job, and each
  `next` that hands out a run's output counts as an offer, the third unread one giving it up.
- **Drop a job:** `docker exec "$G" python3 /home/node/.openclaw/skills/job-queue/jobq.py drop <job id>`
  (the id `list` prints, e.g. `video-ig-<shortcode>`). Kit does this only when the owner asks.
- **Verify:** `docker exec "$G" openclaw cron run "$ID" --due` with nothing queued, or with the
  PC off, runs nothing (`docker exec "$G" openclaw cron runs "$ID"` shows no new run). With a
  job queued and the PC on it runs the job and the answer reaches the owner's chat.
- **A new kind** (the YouTube transcript move is the first follow-up): the skill calls
  `jobq.add(kind, key, owner=..., handler=[...], probe=<host>, payload=..., label=...)` and,
  in its `run <job id>` handler, `running` (None unless the queue's runner started it), then
  `finish`, `retry` or `release`, downloading through `egress_proxy()`, and prints what the
  drain turn answers from (`NO_REPLY` for nothing); `skills/video-eval/video.py` is the
  worked example. A job that
  needs no egress (a file already on the host) passes `probe=""`, and one whose producer polls
  instead of hearing an answer passes `quiet=True`. The queue,
  the gate and the cron row stay as they are.
- **Rollback:** `docker exec "$G" openclaw cron rm "$ID"`, revert the PR, then on the host
  `rm -r /srv/openclaw/config/skills/job-queue /var/lib/lifekit/job-queue` (the second drops
  every waiting job).

### Video evaluation

One intake judge for videos the owner sends, asking adoption first: should we adopt what the
video shows (a tool, practice or pattern) into how we already work. It reports what the item
is, what it claims and how that holds, what in it we could adopt for the owner's standing
goals, what adopting it takes, and a verdict (`adopt`, `accept`, `park` or `drop`, with a
reason code for a drop) with a confidence line; a build is only a separately named follow-up,
considered when nothing adoptable exists. The owner sends Kit a YouTube or Instagram link (or a
video file) in Telegram; firstmate files the same typed job from the host with `scripts/video-eval.sh`; a scout can file it with origin `scout`.
Every item is one job on the shared job queue ("Shared job queue"), one Claude pass against one
versioned rubric, one report and one index row. Free: no API key, no cookies, no login, no paid
transcription. It replaced the Instagram reel claim check, which is now its `claim-check` mode.
A bare link or a video file the owner sends Kit is queued by the drop door before Kit's model
sees the message ("Video drop door"); a link inside prose still reaches Kit, which queues it.

- **Skill:** `skills/video-eval` (`SKILL.md`, `video.py`, stdlib only, and `rubric.md`; needs
  the `job-queue` skill and the youtube-transcript skill's yt-dlp and `media.py`, the media
  code both skills share: captions, `.vtt` to text, the yt-dlp downloads and whisper).
  `video.py <link>` keeps only the reel's shortcode or the YouTube video id and queues it (job
  `video-ig-<shortcode>` or `video-yt-<id>`; a video file is `video-file-<content hash>`). The
  URL is rebuilt from that id, so nothing else in a link reaches yt-dlp. `video.py run <job id>`
  is the queue's handler, run by the queue, never by kit:
  - **YouTube:** the captions first, with the youtube-transcript skill's own captions code.
    Without captions, the audio is downloaded and transcribed like a reel's. Videos over 15
    minutes (`VIDEO_MAX_SECONDS`) are not transcribed and answer `unavailable - too long ...`.
  - **Reel:** a share link is resolved through the egress, then the audio is downloaded with
    the pinned yt-dlp (`--proxy` set to the egress, 100 MB cap). `ffmpeg` and the image's
    `whisper` (model `base`) turn it into the transcript.
  - **A file already on the host:** no egress.

  It prints the material, the rubric's path and version, the mode and the owner's goals, and
  leaves the job running. The next drain turn reads that (`jobq.py output <job id>`), and the
  judge (kit's turn) writes the report and files it with `video.py report <job id>`.
- **The rubric:** `skills/video-eval/rubric.md`.
  - Its front matter is machine-read: the categories (`persona`, `channels`, `finance`,
    `dev-tooling`, `home`, `other`), the verdicts, the drop reason codes, the four tests, the
    section order and the length cap (40 lines, 3500 characters).
  - The core is the four tests every kind gets: evidence at a primary source the judge opened
    itself, cost stated and proportionate, a trace to a ranked goal, and no overengineering.
    `accept` needs all four passed. Since v8 they run on each adoptable part (`take` or `skip`
    under "Useful for the goals"), and the verdict is the best part worth taking; a drop needs
    that no part is.
  - The mode is `claim-check` (each factual claim checked against sources) for a reel, a file
    or `--check`, and `standard` for a YouTube video.
  - `report` refuses a report off the rubric with exit 3 and the reasons, and the job stays
    running for a fixed one. A report over the cap is cut, section by section, never the
    verdict, with a `[cut to fit ...]` marker. Every report and index row carries the rubric
    version.
  - A change to a list, the tests or the template ships with a `version:` bump, so a new
    version judges an item again.
- **Origin routes the reply only:** the judge never sees who sent an item. A `kit` item's
  answer is Kit's message to the owner, at most three lines: the title; `video · <category> ·
  <verdict (reason) | accept -> project | park until date>: <next step or why>`; and
  `report: <path>`. A `firstmate` or `scout` item says nothing (`NO_REPLY`, and its queue job
  is `quiet`, so even a give-up says nothing) and its producer polls `status`.
- **Dedupe across sources:** before a fresh judgment the index is searched for the same item.
  At enqueue the match is by normalized URL (tracking parameters, `www.` and case dropped);
  once fetched, by normalized title (generic titles such as "Video by <account>" never match).
  A repeat answers with the earlier verdict, date and reason and is not judged again; one
  matched by title from another source gets a `drop`/`duplicate` row pointing at the earlier
  report. A note (kind `note`) is matched by its source alone, never by title.
- **Private path and retention:** everything lives in `~/.life-state/intake/` in the gateway,
  i.e. `/var/lib/lifekit/intake/` on the host (mode 0700, files 0600):
  - `index.md` is the index: one markdown table row per item, columns `date`, `kind`,
    `origin`, `category`, `source`, `title`, `gist`, `verdict`, `reason`, `project`,
    `recheck`, `next step or why`, `rubric` and `report`. Rows are appended (one locked
    `O_APPEND` write per row; the intake shape step only adds its `status`/`ref` cells to a row,
    in place under the same lock) and the index is idempotent per source URL and rubric version. It is the
    shared store any producer reads.
  - `reports/<key>.v<rubric version>.md` holds the reports, kept for good.
  - `transcripts/<key>.txt` holds the transcripts, and `inbox/` the backfilled files waiting
    for their run. Both are deleted after 14 days (`VIDEO_RETENTION_DAYS`) by the next run,
    and an inbox file as soon as its run used it.
  - The owner's ranked standing goals are not kept here: the judge reads the `## ` headings of
    the vault's goals page (`~/memory/system/goals.md` in the gateway, the `/srv/memory` mount;
    `GOALS_PAGE` overrides), live at every run. Without the page the judge says so under
    Confidence.

  None of it is served, in git or in the openclaw-config mirror. The transcript is also kit's
  tool output, so it sits in kit's session history, which the encrypted off-box backup covers;
  Kit must not copy it elsewhere (the skill says so). The one copy out is software: an
  accepted item's report and transcript become a source page in the private memory vault
  ("Intake to vault").
- **Install:** the deploy. The OpenClaw post-up phase runs `scripts/ensure-job-queue.sh`, then
  `scripts/ensure-video-eval.sh`. The second copies the skill into kit's workspace (a box with
  no kit workspace skips) and removes the retired `reel-claim-check` skill there. It also
  reports, read-only, whether kit's allowlist names the skill, whether `ffmpeg`, `whisper`, the
  yt-dlp, `media.py` and `jobq.py` are present in the gateway, and whether the goals file is on file. A
  change to `skills/video-eval/` or to the script triggers the OpenClaw phases; a failed
  install turns the deploy red. `scripts/video-eval.sh` is the host side and runs from the
  checkout.

Operator steps, once per host, after the PR has deployed. Run as lifekit on the VPS:

1. Kit's skill allowlist (a deploy run shows `NOT on agent 'kit''s skills list` until it is
   done; skip it when the deploy reported kit as unfiltered). It swaps the retired skill for
   this one:

```bash
G=$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway)
docker exec "$G" node -e 'const c=require(process.env.OPENCLAW_CONFIG_PATH);let s=c.agents.entries.kit.skills;if(!Array.isArray(s))process.exit(1);s=s.filter(n=>n!=="reel-claim-check");for(const n of ["video-eval","job-queue"])if(!s.includes(n))s.push(n);process.stdout.write(JSON.stringify({agents:{entries:{kit:{skills:s}}}}))' > /tmp/kit-skills.patch.json
docker exec -i "$G" openclaw config patch --stdin --dry-run < /tmp/kit-skills.patch.json
docker exec -i "$G" openclaw config patch --stdin < /tmp/kit-skills.patch.json
rm /tmp/kit-skills.patch.json
```

2. The goals: nothing to write. The judge reads the headings of the vault's goals page through
   the gateway's `/srv/memory` mount; a goal changes for the judge when it changes in the vault.
3. **Kit's `AGENTS.md`** is live host state: replace the reel line with one that a YouTube or
   Instagram link the owner wants judged, or a video file, goes to the `video-eval` skill.
4. The old transcripts: `rm -r /var/lib/lifekit/reel-claim-check` on the host. Nothing prunes
   them any more.
5. Reels queued under the retired skill: each `reel-<shortcode>` job in the queue stores the
   old `reel.py` handler, which no longer exists, so nothing would run it and the drain would
   give up on it with a failure message. Right after the deploy list them, drop each, and
   send the links again through `scripts/video-eval.sh enqueue <link>`:
   `docker exec "$G" python3 /home/node/.openclaw/skills/job-queue/jobq.py list`, then
   `... jobq.py drop reel-<shortcode>` for each `reel-*` row that is not `done` or `failed`.

From the host (firstmate, or the operator; any account in the `docker` group). Origin is
`firstmate` unless `VIDEO_EVAL_ORIGIN` says otherwise (`idea` ignores it: always `scout`):

- **Enqueue:** `scripts/video-eval.sh enqueue <link> [--check]` prints `video queued - job
  <job id> ...`, `video already queued ...`, or, for an item judged before, its earlier
  verdict.
- **Backfill files already downloaded** (no download again; a `reel-<shortcode>.<ext>` is that
  reel, a `yt-<id>.<lang>.vtt` that YouTube video read from its captions, anything else a
  video file):
  `scripts/video-eval.sh backfill /path/reel-<shortcode>.mp4 /path/yt-<id>.en.vtt`.
  Each file's line says what was queued. The files are judged one per drain turn, with no
  egress needed: the gate skips a link job waiting for the PC and offers the oldest job whose
  egress answers.
- **Re-run a failed job:** send it again, the same way it came: `scripts/video-eval.sh
  backfill <file>` for a backfilled file, else `scripts/video-eval.sh enqueue <link>`. A
  `failed` job is queued afresh (attempt 0); a `queued` or `running` one is left as it is.
- **Completion signal:** `scripts/video-eval.sh status <job id>`. Its first line is `queued -
  attempt N` or `running - attempt N` while waiting. When finished it is `done` or `failed`,
  followed by the result. A judged job's result is the three-line answer, its last line
  `report: /var/lib/lifekit/intake/reports/<key>.v<N>.md`. An unavailable video is `done` with
  the `unavailable - ...` line.
- **Read:** `scripts/video-eval.sh report <job id>` prints the report;
  `scripts/video-eval.sh index` prints the index.
- **File a judged idea** (the ideas scout; always filed as origin `scout`):
  `scripts/video-eval.sh idea <report file>` (or the report on stdin) checks the report against
  the rubric, with the idea's `Claim:` and primary `Source:` lines, and files it at once: no
  job, no fetch. A `Source:` link over 160 characters is rejected, not cut. It prints the three
  lines, or the earlier verdict when that source or title was judged before; exit 3 lists what
  does not follow the rubric. The report is kept in
  `/var/lib/lifekit/intake/reports/ideas/`, which intake-to-vault does not file into the vault.
- **File a judged link or article** (a web page that is not a video, read and judged by its
  producer): `scripts/video-eval.sh link <report file> [--check]` (or the report on stdin)
  files it the same way as an idea, under `VIDEO_EVAL_ORIGIN`, with the item's own link as its
  `Source:` line. It is stamped `standard`, or `claim-check` with `--check`; a YouTube or
  Instagram video link is rejected (queue it with `enqueue`). The report is kept in
  `/var/lib/lifekit/intake/reports/links/`, which intake-to-vault does not file into the vault.
- **File a judged text note** (the owner's own note, read and judged by its producer):
  `scripts/video-eval.sh note <report file> [--check]` (or the report on stdin) files it the
  same way as a link, under `VIDEO_EVAL_ORIGIN`, with the note's text as its `Note:` line; it is
  recorded and matched under that text's digest (`note:<digest>`), so the same note sent again
  answers with its earlier verdict. A `Note:` over 4096 characters is rejected.
- **Queue a voice note** (an audio file: `.ogg`, `.oga`, `.opus`, `.m4a`, `.mp3`, `.wav`):
  `scripts/video-eval.sh voice <file> [--check]` queues it like a backfilled video file - no
  egress, the same local whisper, judged on the next drain turn - but it is judged and indexed
  as kind `note`. It prints `note queued - job video-file-<digest>`; follow it with `status`.
  Both kinds of note are stamped `standard`, or `claim-check` with `--check`, and their reports
  are kept in `/var/lib/lifekit/intake/reports/notes/`, which intake-to-vault does not file into
  the vault.
- **File a judged repo or tool** (a code repository or a tool's own page, read and judged by its
  producer): `scripts/video-eval.sh repo <report file> [--check]` (or the report on stdin)
  files it the same way as a link, under `VIDEO_EVAL_ORIGIN`, with the repo's link (else the
  tool's homepage) as its `Source:` line, so a repo sent once as a link answers with that
  verdict. It is stamped `standard`, or `claim-check` with `--check`; a YouTube or Instagram
  video link is rejected. The report is kept in `/var/lib/lifekit/intake/reports/repos/`, which
  intake-to-vault does not file into the vault.

Then:

- **Verify:**
  - Send Kit a public YouTube link. It answers `video queued` at once, and with the PC on the
    three-line answer arrives within a few minutes. Then `scripts/video-eval.sh index` shows
    its row, and its report has a `Rubric: video-eval v8 (standard)` line.
  - `scripts/video-eval.sh enqueue <the same link>` answers `already judged`.
  - A made-up reel link ends as an `unavailable` line, which proves the yt-dlp and the route
    to Instagram.
  - With the PC off a link job stays `queued - attempt 0`.
- **Delete an item's private files early:** `rm` its files under
  `/var/lib/lifekit/intake/transcripts/` and `reports/` on the host, and run
  `jobq.py drop video-<key>` for its queue entry. The index row stays (append-only), and so do
  the copy in kit's session history and its backup until those rotate. An accepted item is
  also a vault source page ("Intake to vault"): delete that page and its log line in the vault
  too.
- **yt-dlp stops working on Instagram or YouTube:** bump the pin in
  `scripts/ensure-youtube-transcript.sh`; the next deploy installs it and this skill picks it
  up.
- **Rollback:**
  1. Revert the PR. The deploy then reinstalls the reel claim check; check the reel skill's
     section in the reverted runbook for its steps.
  2. Remove `video-eval` from kit's workspace `skills/` on the host.
  3. Re-apply kit's old skills list (the patch above, with `reel-claim-check` in place of
     `video-eval`).
  4. Drop any waiting `video-*` jobs (`jobq.py drop`).

  Keep `/var/lib/lifekit/intake/` for the record, or delete it if the reports are not wanted.
- **Follow-up, out of scope here:** move the youtube-transcript skill's own fetches onto the
  queue, so a transcript asked while the PC is off waits instead of failing.

### Video drop door

The owner's Telegram chat with Kit has a door in front of the model, so queueing a video is
software, not a model turn. A message whose whole trimmed text is one YouTube or Instagram link,
or that carries one video file, is queued for video evaluation and answered at once with one
line: `video queued: youtube video <id> - position 2 of 3; the verdict comes here once judged`
(`video already queued: ...` when it waits already; the earlier verdict when it was judged
before). The verdict returns to the same chat by the existing path: the `job-queue-drain` turn
announces it on the `default` account to `LIFEKIT_TELEGRAM_CHAT`. Everything else, a link inside
prose included, reaches Kit's model unchanged.

- **Seam:** the `video-drop-door` gateway plugin
  (`compose/openclaw-gateway/plugins/video-drop-door`), a `before_dispatch` hook. A hook that
  answers `handled` sends its text as the reply and no model turn runs. `before_dispatch`
  carries no media, so the plugin's `message_received` hook, which the gateway runs for the same
  message just before, notes the one `video/*` file's saved path under
  `~/.openclaw/media/inbound/` for it. No parallel Telegram poller.
- **Who it opens for:** Telegram, account `default`, agent `kit`, a direct (non-group) chat
  whose id equals `LIFEKIT_TELEGRAM_CHAT` from the env file (passed into the gateway). The
  drain announces to that chat, which is what makes "same chat" hold. With the variable empty
  the door stays shut and Kit gets every message, as before.
- **Deterministic and untrusted-safe:** the plugin runs
  `python3 <kit workspace>/skills/video-eval/video.py door <text>` or `door-file <path>` with
  `execFile` (no shell, the message one argument, 45s cap). `door` takes only printable ASCII
  with no spaces, an http(s) scheme, no user or password, the default port, and a link
  `parse_link` reads to a reel shortcode or YouTube id, so the URL is rebuilt from that id.
  `door-file` takes only a regular file (not a symlink) with a media suffix, within the 200 MB
  ingest cap, and keeps a private copy like `ingest`. Exit 0 is the receipt, exit 2 "not for
  the door"; a `video failed: ...` line is sent as the answer, and any other failure, a timeout
  or a missing script lets the message through to Kit.
- **Position:** `jobq.py`'s `position()`: the running job first, then oldest first. A link
  waiting for the PC can be passed by a file, so it is the queue's order, not a promise.
- **Caveats:** a video file is queued whatever its caption says. A Telegram GIF arrives as an
  mp4 and is queued like a video. The receipt does not depend on any model, so it keeps working
  when the gateway's model credits end; the verdict does, because the drain hands the job to a
  Kit turn, until a later item moves the judge off the gateway.
- **Install:** the deploy. The configure phase runs `scripts/ensure-video-drop-door.sh`, which
  copies the plugin to `/srv/openclaw/config/extensions/video-drop-door` (0644, owned by the
  deploy account, which the gateway's loader requires) before `platform.patch.json` allows and
  enables `video-drop-door`. On first enable the patch's restart hint recreates the gateway;
  later changes to the plugin are applied with `openclaw plugins reload video-drop-door`. No
  operator step: `LIFEKIT_TELEGRAM_CHAT` is already in the env file, and the first deploy
  recreates the gateway for the new variable.
- **Verify:** send Kit a bare YouTube link: the one-line receipt arrives within seconds and
  `jobq.py list` shows the job; send the same link inside a sentence: Kit answers as before.
  `docker exec "$G" openclaw plugins inspect video-drop-door` shows it loaded with its two
  hooks.
- **Turn it off:** remove `video-drop-door` from `plugins.allow` and its entry in
  `platform.patch.json` (a PR), or, at once, `docker exec "$G" openclaw config patch` with
  `{"plugins":{"entries":{"video-drop-door":{"enabled":false}}}}`; Kit then takes every message
  again.

### Envelope and receipt

Every door that takes work carries one envelope and returns one receipt at once. The text
is a single build-standards entry in the vault, **"Envelope and receipt at every door"**
(`memory/system/build-standards.md`, 2026-11-16, decision 0016): the fields, the class
and state names, the fingerprint per door and the repeat rule are defined there and are not
restated here. This section is only where this repo enforces it.

| Door | Where | Envelope in | Receipt out | Fingerprint |
| --- | --- | --- | --- | --- |
| Job queue | `skills/job-queue/jobq.py enqueue` | flags `--origin --owner --class --acceptance [--fingerprint] [--thread]`, kept as `envelope` in the job file; the queue's own job owner is `--skill` | one JSON line `{"id","url","state"}`; `url` is `--url` or `jobq.py status <id>` | the job id (kind and key); a repeat of a waiting or running job adds `repeats` and `latest`, a finished one queued again carries `reopens` |
| Video intake, jobs | `scripts/video-eval.sh enqueue` (also `backfill`'s `ingest` and `voice` through `video.py`) | the same flags after the link; origin is `VIDEO_EVAL_ORIGIN` | the usual line, then the receipt line; `url` is `video-eval.sh status <id>`, or the report when the item was judged before | the item's normalized link |
| Video intake, direct | `video-eval.sh idea\|link\|note\|repo` | the same flags; an idea's origin is always `scout` | the usual three lines, then the receipt line; state `completed`, `url` the report | the item's recorded source; the envelope is kept in `<report>.envelope.json` with a count and the latest occurrence |
| Kit to firstmate | `skills/secondmate-relay/relay.sh note` | flags after the body file and optional home: `--origin --owner --class --acceptance [--thread]`; the note body starts with the envelope as a `---` block | the host's two JSON lines, then the receipt line; `url` is `relay.sh replies <home>` | the `thread`; the request id derives from it, so a note on the same thread is a replay of the first (receipt `working`, no second note) |

Rules this repo adds on top of the entry:

- **Optional as a whole, strict once started.** A door with no envelope flag behaves as it
  always did, and prints no receipt, so existing callers keep their output. With any envelope
  flag it refuses (exit 2 for video intake, 64 for the relay, 1 for `jobq.py`) an item missing
  `origin`, `owner`, `class` or `acceptance`, or one with a class outside `dev ops finance
  life unclear`, before anything is filed. The fingerprint and the thread default to the
  item's own (its link or id; for the relay the thread defaults to the body's digest and the
  fingerprint is always the thread).
- **The Telegram drop door fills its own.** `door` and `door-file` store `origin: kit` and
  `class: life` on the job and print the same one-line receipt as before.
- **State names** map from the queue's own: queued is `submitted`, running `working`, done
  `completed`, failed `failed`. The other four A2A names are not produced by these doors.
- **Reopening a relay thread.** The note body of a repeat is not stored: the host's
  request-id replay returns the first note's id. What the second mate does with a thread that
  was already answered is its own store's rule, not this repo's.

Verify: `jobq.py enqueue` with no envelope flag still queues (the drop door's shape), and with
all of them prints the receipt and `jobq.py list` shows the job; the same call again prints the
same receipt. `relay.sh note <file> --origin kit --owner firstmate --class dev --acceptance
"<done when>" --thread <id>` twice prints the same note id, the second time with
`"state":"working"`, and `relay.sh replies` shows one note.

### Fleet gate

Part of the P16 permission engine: an OpenClaw agent that reaches an action the owner has
ruled stop-and-file is stopped and the request is filed for him, instead of the agent
asking in chat. The agents run on the `claude-cli` runtime, and a tool call there still
passes OpenClaw's `before_tool_call` hook (checked inside the running gateway, 2026.10.7),
so a plugin can see and veto it.

Verified 2026-11-15 on the running gateway, OpenClaw 2026.10.9 (build 8b18b55):

- `openclaw plugins inspect fleet-gate` shows it enabled, version 0.1.0, origin global,
  source `~/.openclaw/extensions/fleet-gate/index.js`, shape non-capability. A diagnostics
  WARN says OpenClaw cannot verify the plugin's origin (record missing, `installSource`
  null): it loads because `plugins.allow` lists it, and it is not a trusted plugin.
- The hook fires: the plugin's own log (`~/.openclaw/logs/fleet-gate.jsonl`) holds 4674
  events from 2026-11-08T22:15Z to 2026-11-15T09:09Z, every one `table-missing` with effect
  `allowed`. The plugin runs on tool calls, so `before_tool_call` is invoked; this confirms
  the hook fires, not that it vetoes.
- **Current condition (2026-11-15, before the refuse-to-start change): the gate allows and
  logs everything.** The P16 table (`p16-openclaw.json`) was never installed: the extension
  directory holds `index.js`, `openclaw.plugin.json` and `package.json` only, and the source
  `ensure-fleet-gate.sh` reads (`/var/lib/lifekit/fleet-policy/p16-openclaw.json`) did not
  exist. fleet-policy now renders the table (`fleet-policy install`); a live veto has not
  been observed until it is copied to the host and installed. Since the plugin refuses to
  start without it, the host steps are, in order: `fleet-policy install`, copy the rendered
  `p16-openclaw.json` to `/var/lib/lifekit/fleet-policy/p16-openclaw.json`, then
  `scripts/ensure-fleet-gate.sh` (or the next deploy). Until the table is there the ensure
  script installs nothing and the deploy goes red, so the plugin already running stays as it
  is.

- **Seam:** the `fleet-gate` gateway plugin (`compose/openclaw-gateway/plugins/fleet-gate`),
  `before_tool_call` for every tool and `after_tool_call` for the outcome of the logged
  ones. Same shape and install path as the video drop door: `scripts/ensure-fleet-gate.sh`
  copies it to `/srv/openclaw/config/extensions/fleet-gate` in the deploy's configure phase,
  `platform.patch.json` allows and enables it, and a code change is applied with
  `openclaw plugins reload fleet-gate`.
- **The table:** `p16-openclaw.json` (schema `p16-openclaw.v1`) is rendered by fleet-policy
  from the P16 rows, so the rules live once; the plugin makes no network call and holds no
  second copy. `ensure-fleet-gate.sh` validates it and copies it next to the plugin, before
  the plugin code
  (source: `P16_OPENCLAW_TABLE`, default `/var/lib/lifekit/fleet-policy/p16-openclaw.json`);
  the plugin re-reads it when its mtime changes, so a new table needs no reload. Shape:
  `{"schema": "p16-openclaw.v1", "tools": {"<tool>" | "<prefix>*": {"class": "A"|"L"|"S",
  "id": "<resource.action>", "ruling": "<pointer>", "when": "non-owner"|"non-owner-to-main"}}}`.
  An exact name beats a prefix, the longest prefix beats a shorter one, and `mcp__x__y` is
  looked up as itself and as `x__y`. A row whose `when` does not hold for the call is
  skipped; an unknown `when` counts as holding, so a stop is never silently relaxed.
  `non-owner` is a run whose host-derived `requester.senderIsOwner` is not `true` (missing
  counts: it is unproven); `non-owner-to-main` adds that a `sessions_send` target is the
  main agent (`kit`): its `sessionKey` is `main` or `agent:kit:...`, its `agentId` is `kit`,
  or it names no agent (a bare `label`).
- **Classes:** `A` returns nothing and writes nothing. `L` returns nothing and writes one
  `taken` line, and `after_tool_call` one `outcome` line (`returned`, `error`, or `unknown`
  when the harness gave neither: no result is not proof of success). `S` blocks the call
  with `Stopped under P16 (<id>): filed for the owner as oc-<id>. Carry on with other
  work; do not ask him in chat.` and files the request through the relay; if filing fails
  (key not set up, rate limit, ssh down) it is still blocked, the reason says filing failed
  and why, and the `stopped` log line carries `filed: false` and the error. A tool the table
  does not list is allowed and logged as `unlisted` (P16: reversible and inside the fleet's
  space is taken, not asked).
- **The filed note** is the body of `note oc-<day>-<hash>` to the second mate's inbox
  (the `openclaw` sender, "openclaw sender" above): the class, ruling, agent, tool, session,
  run and the call's params (keys that look like tokens, passwords or cookies replaced with
  `[redacted]`, long strings and the whole cut). The id is `oc-`, the UTC day and a hash of
  agent, tool, class and params, so the same ask the same day is a replay of one note.
  Nothing the agent sent reaches a shell: `execFile` of `ssh`, the body on stdin.
- **Log:** `/srv/openclaw/config/logs/fleet-gate.jsonl` (in the gateway,
  `~/.openclaw/logs/fleet-gate.jsonl`), one JSON line per event (`taken`, `outcome`,
  `stopped`, `unlisted`, and the problems below), one older generation kept past 8 MiB. It
  is readable by the `lifekit` account; fleet-policy's audit reads it from there.
- **When it cannot decide:** the plugin refuses to start without its table. `register()`
  loads it once and throws on a missing or unreadable one (`table-missing`) or one that is not
  valid (`table-malformed`), registering no hook, so the gateway never runs a gate that decides
  on nothing (`FLEET_GATE_TABLE` overrides the path, for tests). `ensure-fleet-gate.sh` keeps
  the order safe: with no source table and none installed it exits non-zero before it touches
  the plugin (the one already running stays, the deploy goes red), and an invalid source
  fails it the same way without replacing the installed copy or the plugin code. After a
  start, a table swapped on disk that turns missing or malformed blocks nothing and logs the
  reason once a minute, with a gateway warning, and an unexpected error in the lookup does
  the same (`gate-error`): the last good decision is not kept. A single bad row is skipped and
  the rest of the table applies. The host fails `before_tool_call` closed after 15 s; the
  plugin leaves that default alone and keeps its own filing budget (9 s) inside it.
- **What it cannot see:** the gate judges the tool call, not what the tool then does. What a
  script, `gh` or `curl` does after `exec` starts; side effects inside an MCP server; and
  command-kind cron jobs (they run no agent tool call) are all outside it. Server-side
  scoping (the Google and finance-sentry MCP scopes, the repo and token permissions) stays
  the strongest boundary; the gate is the place a stop is filed, not the lock.
- **Per-agent `devclaw__*` deny: do not apply.** An earlier note here suggested denying the
  devclaw MCP tools for every agent except `kit`. Every agent now files a development-class
  problem through the devclaw intake tool ("Development-class problems: file and carry on"),
  so a blanket deny would stop the route. If per-agent enforcement is ever verified under
  `claude-cli` for that agent (not enforced when last tried, 2026-05-25; enforced on 2026.11.4
  for the `reader` agent's shape only, `scripts/reader-canary`; `docs/openclaw-agent-design.md`), scope it
  to the other devclaw tools: deny `devclaw__*` except the intake tool, and give a stop row to
  the rest.
- **Rehearse:** `scripts/rehearse-fleet-gate.sh` loads the repo's plugin in node with a
  scratch table and state dir and a recording `ssh`, plays one `A`, one `L` and one `S`
  call, and prints what each returned and what was filed (a class the given table has no
  row for, such as a table of only `S` rows, is played from its built-in sample row). It
  touches no container and no production state; run it as any user before and after a change.
- **Verify live:** `docker exec "$G" openclaw plugins inspect fleet-gate` shows it loaded
  with `before_tool_call` and `after_tool_call`; `tail /srv/openclaw/config/logs/fleet-gate.jsonl`
  as `lifekit` shows `taken`/`unlisted` lines as agents work. The first deploy that enables
  the plugin recreates the gateway through the platform patch's restart hint, as for any new
  plugin; later changes reload it in place. No other container restarts.
- **Turn it off:** remove `fleet-gate` from `plugins.allow` and its entry in
  `platform.patch.json` (a PR), or at once `docker exec "$G" openclaw config patch` with
  `{"plugins":{"entries":{"fleet-gate":{"enabled":false}}}}`; calls are no longer gated.

### Reader agent

The `reader` agent is the one agent that reads untrusted text: new mail and the documents
attached to it. Both reading crons run as it - `doc-door` and `needs-you-mail-reader` - so a
hostile message or document meets an agent that holds no write, send, web or delegation tool,
not Kit, which does (decided in #528, built in #556). It runs only from those two cron jobs: no
channel is bound to it, nothing chats with it, and no other agent can reach it.

- **Agent-to-agent rule**: every agent can message Kit and no other agent, Kit can answer
  each of them, and nothing can message the reader or the study panel. `tools.agentToAgent`
  stays on. Each agent's definition ships who it may message as
  `tools.agentToAgent.send` in `defaults/agents/<id>/agent.json`: Kit `["*"]`, Ledger
  (`finance`), learnclaw and career `["kit"]`, the reader and the study panel `[]`.
  `tools.agentToAgent.allow` is every agent the repo defines (each `defaults/agents/<id>` with a
  `workspace/`, plus Ledger's id from `scripts/ledger-persona/ledger-persona.py`'s default
  workspace) except the ones that ship `send: []`, and never the reader. So learnclaw and
  career cannot reach Ledger or the study panel, and only Kit reaches Ledger.
  `scripts/ensure-reader-agent.sh` derives the list and the sends on every deploy and converges
  them with `openclaw config patch` (the allow list, and only the `send` key of each agent that
  has a live entry; an agent not installed on the box gets no entry), so a new agent definition
  (a `workspace/` plus an `agent.json` with its `send`) joins without an edit to the script; a
  definition with no `send` list is refused, and `--check` fails when the live list or any live
  send differs. OpenClaw (2026.11.4 schema) has no one-way rule: the requester and the target
  must both match the global allow list, and an agent's own `send` only narrows its outbound
  sends inside it. So the reader stays out of the list, which also means it cannot send to Kit;
  a list that reaches the reader (`reader`, `*` or a trailing `*` pattern) is refused.

- **What it holds** (`defaults/agents/reader/agent.json`, the agent entry; the persona is
  `defaults/agents/reader/workspace/`): `tools.allow` is `exec` plus the two Gmail reads
  (`google-workspace__search_gmail_messages`, `google-workspace__get_gmail_messages_content_batch`);
  `tools.deny` lists every other tool group (fs, web, sessions, memory, ui, automation, messaging,
  nodes, agents, media, `process`, `code_execution`), the Gmail send and draft tools and the
  other MCP servers; no `tools.profile` (a profile hides MCP tools). `tools.exec.mode` is
  `allowlist` and the one allowlisted path is the door script
  (`/home/node/.openclaw/agents/reader/workspace/skills/doc-door/door.py`): any other command, a
  pipe, a second command, a heredoc or an interpreter one-liner is refused. No Docker sandbox, no
  docker socket. Model `anthropic/claude-sonnet-5-5`. It has no read tool, so `door.py scan` prints
  the skill's reporting guide itself and a report is `door.py report <id> --title ... --fact ...`
  (single-line arguments). The send path is Kit's and is unchanged: the reader has a read-only
  Workspace route only. The needs-you digest's `cron runs` marker pull stays Kit's.
- **The canary** (`scripts/reader-canary/run.sh`; `scripts/tests/test_reader_canary.py` runs it
  in the suite): one real `openclaw agent --local --agent reader` turn on the `claude-cli` runtime in a
  throwaway container from the gateway image (no network, no secrets, no live state), the real
  claude binary against a mock model API that issues 16 tool calls, and a stub Workspace MCP and
  door script that record what reaches them. It passes only when the model was offered nothing beyond the
  tools the allow-list grants (Claude Code's inert `DeferredToolPlaceholder` stub and its `ToolSearch` deferred-tool loader, each matched on its whole
  shape, are not counted; a lookalike still fails; the allowed tools may be deferred behind `ToolSearch`
  and so absent from the catalogue, which the five allowed calls running covers), the five allowed calls (both Gmail reads, `door.py scan`, `show` and
  `report`) ran, and each of the eleven denied calls (Gmail send and draft, any other command, a
  chained command, an interpreter one-liner, native file write and read, web fetch, and the message, cron and
  sessions tools) was refused and left no trace in the stub or the filesystem. A run that
  could not start (no model request) fails; it never reads as a pass. Exit 77 means no Docker or no
  image (a skip: the pytest wrapper skips loudly, or fails with `LIFEKIT_REQUIRE_CANARY=1`). Evidence stays in
  `CANARY_OUT=<dir>`. The canary can fail: `CANARY_AGENT_JSON=<entry>` swaps the entry, and an entry with
  no `tools` block gets the whole toolbox and fails on the Gmail send, draft and the three exec cases (checked when this
  shipped), and `test_reader_canary.py` feeds the verdict synthetic leaks. What it does not cover: the cron
  job's own `toolsAllow` (still unverified under `claude-cli`; step 6 checks it live) and the real Anthropic API
  (the model is mocked; the tool surface the gateway builds is what is under test).
- **Design A, not B.** The exec allowlist limited to `door.py` held in the canary with no leak, so the
  door needs no trigger-script gate for its exec cap (the alternative in #528; the gates above only save model turns). If a future gateway version leaks a denied call
  here, the canary turns red on the deploy host first; design B is then the answer.

**Operator steps** (after the merge deploys; until step 2 is done the deploy skips the reader and
leaves both crons on Kit, each with a note, so the merge changes nothing before then). On the VPS as `lifekit`, in
`/srv/lifekit-stack`, gateway container `openclaw-openclaw-gateway-1`:

1. Prove the cap on this box: `scripts/reader-canary/run.sh` must end with `reader canary passed`
   (about two minutes; needs Docker and the `lifekit-openclaw:local` image). Do not go on if it does not.
2. Apply the agent entry (host state, merged into the live config):
   `docker exec -i openclaw-openclaw-gateway-1 openclaw config patch --stdin < defaults/agents/reader/agent.json`.
   Read the apply hint it prints: restart the gateway only if it says the keys need it.
3. `scripts/ensure-reader-agent.sh` (the deploy runs it too): installs the contract into
   `/srv/openclaw/config/agents/reader/workspace`, allowlists the door script, and ends with
   `reader agent verified` only when the live entry equals the shipped one, nothing is bound to the
   reader, the reader is unreachable by other agents (`tools.agentToAgent` off, or on with a
   non-empty `tools.agentToAgent.allow` that excludes it; enabled with no allow list is refused, and
   the script sets the list and each agent's `send` to the repo's definitions when it is on, a
   re-run changes nothing) and the
   reader's allowlist is exactly the door script.
4. `scripts/ensure-doc-door.sh`: installs the skill into the reader's workspace and moves the
   `doc-door` job to the reader. Then `scripts/ensure-needs-you-digest.sh`: moves
   `needs-you-mail-reader` to the reader (the digest stays on Kit; the deploy runs only its `--check`).
   Each exits 1 if the live row's `agentId`, `toolsAllow` or delivery differs. The reader's runs use the
   `anthropic:api-key` auth profile: if `auth-mode.sh status` reports the reader without it, run the
   operator step in "OpenClaw auth mode (API key or subscription)" (`auth-mode.sh offline-profiles`,
   which stops the gateway) before this step, or the moved jobs fail on their first run.
5. Verify the move: `openclaw agents list --bindings` (reader, `bindings: 0`);
   `openclaw cron list --all --json` - `doc-door` and `needs-you-mail-reader` show `agentId: reader`
   and `payload.toolsAllow` `exec` / the two Gmail reads; `openclaw approvals get --json` - the
   reader's allowlist is the door path and nothing else.
6. Run both jobs once: `openclaw cron run <id> --expect-final --json`. `doc-door` ends `No new documents.`
   or a report; `needs-you-mail-reader` replies between its markers, and `openclaw cron runs <id>
   --limit 1 --sort desc --json` shows the reply and an `ok` status. A run that fails with a refused
   tool (not a Google error) means the prompt asked for a tool the reader does not hold: fix the prompt, not the cap.

**Rollback.** The deploy re-runs `ensure-doc-door.sh` on every OpenClaw-path merge, and it moves the job
to the reader whenever the reader entry is applied and verified, so `DOC_DOOR_AGENT=kit` alone does not
last: the next deploy undoes it. To keep the jobs on Kit, retire the reader first:

1. `openclaw agents delete reader`, then remove its allowlist rows with
   `openclaw approvals allowlist remove --agent reader <path>`. With no entry the deploy leaves both
   jobs wherever they are.
2. Move the jobs back: `DOC_DOOR_AGENT=kit scripts/ensure-doc-door.sh` and
   `NEEDS_YOU_READER_AGENT=kit scripts/ensure-needs-you-digest.sh` (Kit's copy of the door skill is
   installed by `ensure-doc-door.sh` on that run; both scripts also clear the reader-era `--trigger-script` from a
   row that now runs as Kit, since the gate reads the reader's state). `ensure-needs-you-digest.sh` is not converged by the deploy,
   so its move stays; the `doc-door` move stays only while the reader entry is absent.

The env overrides alone are a one-off for testing: the next deploy puts `doc-door` back on the reader, and its
`ensure-needs-you-digest.sh --check` reports the mail reader row on Kit as drift ("Cron declarations check").

**A refused `cron.update`.** When the gateway answers the job edit with `invalid cron.update params: MCP
authorization was disconnected or replaced` (the MCP authorization behind the job's stored creator
authority was retired by a gateway restart or reconnect), `ensure-doc-door.sh` leaves the row unchanged, prints
one `cron 'doc-door' (...) NOT updated` line and does not print `converged`; that step stays green, the
fail-closed check still runs on the row as it stands, and the next deploy retries. The deploy's cron
declarations check (below, "Cron declarations check") then runs `ensure-doc-door.sh --check` and reports the
unconverged row as drift, so the deploy as a whole goes red until an edit lands. Any other edit failure is
still red. If the line repeats across deploys, the authorization is genuinely gone: restore it as an operator
step (reconnect the Workspace MCP, `docs/google-mcp-setup.md`), then re-run `scripts/ensure-doc-door.sh`.

### Document door

New document attachments in the owner's mailbox become one short cited report each, with no step
for the owner. `skills/doc-door` (`door.py`, stdlib only) is run by the `doc-door` cron job
(`scripts/ensure-doc-door.sh`, declared on every deploy; 07:20, 13:20 and 19:20 Dublin): `scan`
speaks MCP to `google-workspace-mcp` (Kit's connector) itself for mail, and to
`google-workspace-mcp-fleet` (the fleet's own Google identity) for Drive, and calls only `search_gmail_messages`,
`get_gmail_message_content` and `get_gmail_attachment_content` (PDF, DOCX, XLSX and PPTX of
4 KiB to 15 MiB, mail from the last 3 days), converts each new attachment with MarkItDown, and
prints it with numbered lines for the reader agent to read (Kit's, until the reader is applied: "Reader agent"). The agent files the report with `door.py report`, which
accepts it only when it has a title, a Drive subject folder, an owner vault page, a one-line page
proposal and 3 to 5 facts, each with a quote that is on the cited line, and nothing
identifier-shaped (number runs, IBANs, e-mail addresses, PPS numbers). It then appends one
`bin/log.sh` line (pointer only: title, folder, page, report id) to the vault; a failed append is
retried by the next scan. The page edit stays a proposal in the report.

- **State is private and outside the vault:** `/var/lib/lifekit/doc-door/` on the host (mode
  700): `state.json` (one record per attachment content hash, so a re-sent file and a re-run are
  no-ops), `documents/` (the file and its converted text, a reading cache, never cited),
  `reports/`, and `venv/` (the job's own MarkItDown, pinned in `ensure-doc-door.sh`; bump the
  literal to upgrade).
- **Fail-closed:** the job's tool allow-list is exactly `exec`, it runs as the `reader` agent and it delivers nothing; the
  script exits 1 if the live row differs. A failed run alerts the owner through the `default`
  Telegram account, like the needs-you digest.
- **Filing into Drive.** After a report is accepted, `door.py` files the original: it finds the
  report's subject folder under the Drive root of the fleet account, or of the folder named by
  `DOC_DOOR_DRIVE_PARENT_ID` when the owner has shared one (`search_drive_files`), creates it if absent
  (`create_drive_folder`), lists the folder (`list_drive_items`) and uploads the bytes with
  `create_drive_file` (`base64_content` with the `content_mime_type` the server requires beside it,
  plus `base64_sha256`), under the original name, or suffixed with a short content hash when that name is taken, so nothing is overwritten. The Drive file id
  goes in the report's `Drive:` line and the private state, never in the vault log. One upload per
  content hash. Any Drive error (token without the Drive scope, size, server) leaves
  `Drive: pending (<short reason>)` and the next scan retries; it never fails the scan. All of these
  calls go to `google-workspace-mcp-fleet` (`DOC_DOOR_DRIVE_MCP_URL`); Kit's connector is
  read-only for Drive. These are the only Drive tools the door may call (`ALLOWED_TOOLS`, enforced
  in `Mcp.call`), and the fleet connector blocks every other Drive write by name
  (`docs/google-mcp-setup.md`, "The fleet connector"). A folder id the door cached from another
  Drive is never reused: the cache is keyed by connector and parent. Each create logs
  `[create_drive_file]` / `[create_drive_folder]`; the `agent-drive-created` rule
  (`rules-agent-drive.yml`) raises a warning for it, not a page (only critical routes to the
  phone), and `poll_alerts` does not read that file. Until the fleet account's one-time sign-in is in
  (an owner step, `docs/google-mcp-setup.md`, "The fleet connector") every report stays
  `Drive: pending (...)`; filing before 2026-11-08 went to the owner's Drive through Kit's
  connector and those originals stay where they are.
- **Report copies.** The accepted report itself is also copied into Drive, by rclone under a
  consent of its own that holds `drive.file` only ("Report copies in Drive (rclone)" below). Off
  until that consent is in; the fleet connector and Kit's connector are not involved.
- **Not a queue job:** a mailbox scan and a MarkItDown conversion fit one cron tick. If either
  proves slow, move conversion to the shared job queue then.
- **Files handed to an agent in chat** (the second source) and a Drive `Inbox` folder (the third)
  are not built; the video drop door's plugin shape is the model for the first.

### Report copies in Drive (rclone)

The document door files its cited report, not only the original, into Google Drive. A small
rclone-based uploader (`skills/doc-door/drive_upload.py`, stdlib Python around a pinned rclone) does
it under an OAuth consent of its own that holds the **`drive.file`** scope and nothing else. Under
that scope Google shows the app only files it created itself, so the uploader sees one folder,
`Document door reports` (made on first use, one subfolder per report `Folder:`), and nothing else in
the account. The Workspace connectors stay as they are: Kit's is read-only for Drive; the fleet
connector keeps filing originals (`docs/google-mcp-setup.md`, "The fleet connector"). Moving the
originals onto this narrower consent as well, and then taking the fleet connector down to
`drive:readonly`, is a separate change.

- **Wiring.** `door.py` calls the uploader after a report is accepted (`report`) and again on every
  scan for a report not yet copied (up to 5 per pass). The file is `<Title> [<8-char content
  hash>].md` under `Document door reports/<Folder>/`, the report as stored in
  `/var/lib/lifekit/doc-door/reports/`, so it carries the cited facts and the proposal and nothing
  else. `report` prints `Report copy: filed in <path>` or `Report copy: pending (<reason>)`; a
  failure never fails the scan. With no config the door prints no such line and attempts nothing.
  Reports filed before the consent are copied by the first scans after it.
- **rclone.** Pinned in `drive_upload.py` (the same version and sha256 as the off-box backup's;
  bump both installers together), downloaded and checked by `scripts/ensure-doc-door.sh` on every deploy
  into the job's state dir (`/var/lib/lifekit/doc-door/bin/`, not the image). Every call passes an
  explicit `--config` and `--drive-scope drive.file`, a minimal environment and no `-v`
  (debug logging prints credentials).
- **Where the secret lives.** `/srv/lifekit-secrets/doc-door-drive/rclone.conf` on the host, mode
  0600, in a 0700 folder owned by the gateway's uid, mounted read-write at
  `/run/lifekit/doc-door-drive` (read-write because rclone saves the refreshed access token
  beside the config). It holds the OAuth client id and secret and the refresh token; it is never
  in git, a log, a PR or a chat, and `configure` and `verify` print none of it. Any process in the
  gateway container can read it, which is why the scope is the guard: the token reaches only files
  this client created.

**One-time consent.** Run it right after the fleet account's sign-in
(`docs/google-mcp-setup.md`, "The fleet connector", step 6), in the same browser session, so there is
one sitting. Who takes the sign-in and the consent click, and where, is in that section; nothing here scripts either.

1. **OAuth client (Google Cloud console, the fleet's project).** *Google Auth Platform → Clients →
   Create client → Desktop app*, name `doc-door-uploader`. *Data access → Add or remove scopes*:
   only `.../auth/drive.file`. The consent screen is already *In production* (the fleet connector
   needs that too), so the refresh token does not expire after 7 days. Keep the client id and
   secret to hand for step 3.
2. **Host folder (`denys`, sudo), before or after the deploy that merged the mount:**

   ```bash
   sudo install -d -m 0700 -o lifekit -g lifekit /srv/lifekit-secrets/doc-door-drive
   ```

   If the deploy ran first, Docker made the folder root-owned; the same command fixes it in place
   and the running container sees the change, no recreate.
3. **Consent (the PC, in the browser signed in as the fleet account).** Needs rclone on that machine (any current release):

   ```bash
   RCLONE_DRIVE_SCOPE=drive.file rclone authorize drive "<client id>" "<client secret>" --auth-no-open-browser
   ```

   It prints one link, `http://127.0.0.1:53682/auth?state=...`. Open it in that browser. Google's
   consent page must ask for exactly one permission, "See, edit, create, and delete only the
   specific Google Drive files you use with this app"; if it lists anything wider, click Cancel
   and stop. After *Allow*, rclone prints the token between `--->` and `<---End paste`. Leave that
   terminal open for step 4; the output is a credential, so no screenshot and no chat.
4. **Install it (the VPS, as `lifekit`).** The paths below are the reader agent's workspace; while
   the job is still on Kit ("Reader agent"), use `agents/kit` instead.

   ```bash
   G=$(docker ps -q --filter label=com.docker.compose.project=openclaw --filter label=com.docker.compose.service=openclaw-gateway)
   docker exec -it "$G" python3 /home/node/.openclaw/agents/reader/workspace/skills/doc-door/drive_upload.py configure
   ```

   It asks for the client id, then the client secret (hidden), then the pasted token block
   (hidden), and writes `rclone.conf` (0600). It refuses to replace an existing config without
   `--force`.
5. **Verify.**

   ```bash
   docker exec "$G" python3 /home/node/.openclaw/agents/reader/workspace/skills/doc-door/drive_upload.py verify
   ```

   `VERIFY OK` means all of: the config is mode 0600 and asks for `drive.file`; **Google itself
   reports the live token's scope as exactly `drive.file`** (tokeninfo, and that it was issued to the
   config's client); the uploader created one test file in its own folder
   (`Document door reports/_verify/`), read it back byte for byte and removed it; the account root
   shows only `Document door reports` (a `drive.file` token sees nothing else); and Kit's
   connector offers no Drive write tool (below). Any `FAIL` line names the cause; the exit code is 1.
   `verify uploader` and `verify kit` run one half.
6. The next scan copies the pending reports; `docker exec "$G" python3 .../drive_upload.py status`
   shows the install, and a report's `Report copy:` line shows the result.

**Drive narrowing check for Kit's connector.** `verify kit` (also part of the bare `verify`) asks
Kit's connector, over MCP, which tools it offers: it fails if any Drive create, update, delete,
share, copy, move or import tool (the sharing-link tool `get_drive_shareable_link` included) is
among them, or if no Drive tool is offered at all (the level cannot then be shown). The server
still lists `get_drive_shareable_link` at `drive:readonly`, so Kit's compose service blocks it by
name in `WORKSPACE_MCP_DISABLED_TOOLS`. The compose file's `drive:readonly` literal is held by
`scripts/tests/test_doc_door_drive.py`; the container log check in `docs/google-mcp-setup.md`
("Verify Kit's connector is narrowed") is the third leg.

**Rehearsal without Google.** `DOC_DOOR_RCLONE_REMOTE` swaps Drive for any rclone remote, a local
folder for a rehearsal; the config is not needed and `verify` says plainly that it did not check
the token's scope.

```bash
docker exec -e DOC_DOOR_RCLONE_REMOTE=':local:/tmp/doc-door-rehearsal/' "$G" \
  python3 /home/node/.openclaw/agents/reader/workspace/skills/doc-door/drive_upload.py verify uploader
```

CI covers the door-to-uploader handoff the same way, with no Google and no secret
(`scripts/tests/test_doc_door_drive.py`): a stub rclone maps the Drive remote to a folder and
logs each call; the tests check the file lands in the right subfolder, that every call carries the
config and `drive.file`, that a failure leaves the copy pending and a later scan files it once,
and that no credential is printed. Where a real rclone is installed the same handoff also runs
against it on a local remote.

**Notes.**

- `drive.file` visibility is per OAuth client. If the client is ever replaced, the new one cannot
  see the old folder and makes a new `Document door reports`; the old files stay where they are.
- Revoke: remove the app on the fleet account's *Third-party apps with account access* page,
  `sudo rm /srv/lifekit-secrets/doc-door-drive/rclone.conf`, and delete the `doc-door-uploader`
  client in the Cloud project. Reports then stay `Report copy: pending` and no scan fails. Files
  already created stay in Drive until the owner deletes them.
- A token that stops working (`invalid_grant`, a revoked app) shows as `pending (...)` in the
  report line; redo steps 3 and 4 with `--force`.

### learnclaw (the learning loop)

learnclaw is the domain agent for the learning loop. It owns the artefact record on the vault's
`domains/learning.md`, a table with one row per artefact step. For each topic the loop is:
predict before reading, read, build one from-scratch artefact, apply it in a real project, post
about it, and get grilled. learnclaw reviews the artefact and grills the owner on it, but never
writes the artefact. "Where am I" is computed from the rows each time it is asked: the last
artefact, checkpoints met (an artefact row dated by the checkpoint), due or missed, and what
each open topic still lacks. Nothing stores that state separately. Other agents only read the page.

- **Contract:** `defaults/agents/learnclaw/workspace/` (`AGENTS.md`, `SOUL.md`, `IDENTITY.md`,
  `skills/README.md`). Change the persona here; the deploy overwrites the live files.
- **LearnHarness (adopted 2026-11-09):** the owner learns in the LearnHarness desktop app on the
  PC, which teaches and keeps the learning record; learnclaw reads that record and never teaches.
  - `scripts/ensure-learnclaw.sh` fetches LearnHarness's own `learnharness` skill from upstream at
    a pinned commit (`LH_SKILL_COMMIT`), checks each file against its sha256 manifest and installs
    it unedited into the workspace; a failed fetch or checksum keeps the installed copy.
  - `scripts/learnharness-sync/` pulls the record (`chats/`, `graph/`, `jobs/` only, never
    `settings.json` or keys) every 10 minutes into `/var/lib/lifekit/learnharness/data`, the
    gateway's `~/.life-state/learnharness/data`. The PC side is one `authorized_keys` line,
    `rrsync -ro` on the LearnHarness data folder, tailnet only, which the installer prints. The PC
    off is ssh exit 255, which the unit treats as success. After each pull it also refreshes a copy in
    learnclaw's workspace for the morning job (see "Learnclaw morning learning message"). Install once per host, as the admin
    account: `LH_SYNC_HOST=<PC tailnet name> LH_SYNC_USER=<PC account>
    scripts/learnharness-sync/install-learnharness-sync.sh`; check with
    `systemctl status learnharness-sync.service`.
  - learnclaw's Telegram door is the `reading` account, its own bot (token `LEARNING_BOT_TOKEN`).
    It was disabled by [OpenClaw audit bundle A](#openclaw-audit-bundle-a-2026-11-lifekit-stack530)
    and is enabled again for the daily problem (see "Daily LeetCode problem"); until an operator
    has applied that, it answers through Kit and the Control UI chat. Its allowlist `agents.entries.learnclaw.skills` is
    `["learning-record", "learnharness", "leetcode-daily"]` (host state).
- **Skill:** `skills/learning-record` (`record.py`, stdlib only).
  - `record.py status` prints the projections. A row whose date is not a real calendar date is
    reported on its own line, not counted, and does not stop the report.
  - `record.py append --topic ... --artefact ...` adds one row at the end of the table and bumps
    the page's `updatedAt`. It then runs the vault's own `bin/lint.sh --changed --tsv`. On any
    high or medium finding for the page it puts the page back byte for byte and exits 1.
  - A bad row (a pipe in a cell, a date that is not a real calendar date, `done` without the
    application, post and grilling)
    exits 2 and writes nothing. It never commits: memory-sync does.
  - Rows are append-only; a correction is a new row, and a topic's latest row is its state.
- **Install:** the deploy. The post-up phase runs `scripts/ensure-learnclaw.sh`. It copies the
  contract and the skill into the learnclaw workspace and reports, read-only, whether
  `learning-record` is on the agent's skill allowlist. A box without the workspace skips with a
  note. `install-workspace-pointers.sh` and the openclaw-config mirror include learnclaw too.
- **How it is invoked:**
  - The owner's own chat with learnclaw: the OpenClaw Control UI, or the `reading` Telegram bot once
    enabled. These are the only routes that record a row, since a row holds the owner's own words.
  - kit, as a subagent run, once `learnclaw` is in kit's `subagents.allowAgents`. kit relays the
    answer, and a subagent run never records. (The daily `evening-plan-note` learning-line run is
    superseded, see "Learning objective line from learnclaw".)
  - learnclaw's two crons are `leetcode-daily` (see "Daily LeetCode problem") and `learnclaw-morning` (see
    "Learnclaw morning learning message"); apart from those, nothing runs unless asked.
- **Cost:** a run is one Sonnet 4.6 conversation turn on the gateway's subscription. Measured on
  comparable career runs, that is about 50k tokens, mostly cache reads: roughly $0.10-0.20 at list
  prices. Only the `leetcode-daily` cron (four home days a week) and the `learnclaw-morning` cron (Monday to Saturday) schedule one.

**Operator steps (once, after the merge deploys).** The agent entry is host state.

```bash
G=$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway)
# 1. Create the agent (no --bind: it has no Telegram account).
docker exec "$G" openclaw agents add learnclaw \
  --workspace /home/node/.openclaw/agents/learnclaw/workspace \
  --agent-dir /home/node/.openclaw/agents/learnclaw/agent \
  --model anthropic/claude-sonnet-5-5 --non-interactive --json
# 2. As lifekit: install the contract and the skill, drop the bootstrap stub.
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-learnclaw.sh
sudo -u lifekit rm -f /srv/openclaw/config/agents/learnclaw/workspace/BOOTSTRAP.md
# 3. The entry: identity, description, the skill allowlist, the coding tool profile.
cat > /tmp/learnclaw.patch.json <<'JSON'
{"agents":{"entries":{"learnclaw":{"name":"Learnclaw","description":"The learning loop: the artefact record on the vault's learning page, predict-read-build-apply-post, the grilling.","identity":{"name":"Learnclaw","theme":"learning familiar - exacting, patient, brief","emoji":"📚"},"skills":["learning-record"],"tools":{"profile":"coding"}}}}}
JSON
docker exec -i "$G" openclaw config patch --stdin --dry-run < /tmp/learnclaw.patch.json
docker exec -i "$G" openclaw config patch --stdin < /tmp/learnclaw.patch.json
# 4. Let kit hand it learning questions (the live list plus learnclaw).
docker exec "$G" node -e 'const c=require(process.env.OPENCLAW_CONFIG_PATH);const a=((c.agents.entries.kit.subagents||{}).allowAgents||[]).slice();if(!a.includes("learnclaw"))a.push("learnclaw");process.stdout.write(JSON.stringify({agents:{entries:{kit:{subagents:{allowAgents:a}}}}}))' > /tmp/kit-allow.patch.json
docker exec -i "$G" openclaw config patch --stdin --dry-run < /tmp/kit-allow.patch.json
docker exec -i "$G" openclaw config patch --stdin < /tmp/kit-allow.patch.json
rm /tmp/learnclaw.patch.json /tmp/kit-allow.patch.json
```

5. Kit's live `AGENTS.md`: hand-merge the "Learning" section from
   `defaults/agents/kit/workspace/AGENTS.md` (kit's contract is copied by hand).
6. **Rehearsal:** one run that appends one row the vault lint accepts. Use a row the page already
   states as fact, for example the open topic with no artefact yet and status `in progress`:
   `docker exec "$G" openclaw agent --agent learnclaw --message "Record this row: <the owner's
   words>" --json`. The reply's usage block gives the run's real token cost.

**Verify:** `openclaw agents list` shows `learnclaw`. `openclaw skills --agent learnclaw list`
shows `learning-record` ready. The rehearsal printed `recorded: | <date> | ...`, and on the host
`sh bin/lint.sh --changed` in `/srv/memory` has no high or medium finding on
`domains/learning.md`. `ensure-learnclaw.sh` prints `allowlist: 'learning-record' is on agent
'learnclaw''s skills list`.

**Pause** (the agent and its record stay): take `learnclaw` out of kit's
`subagents.allowAgents`, and patch `{"agents":{"entries":{"learnclaw":{"skills":[]}}}}`. The
agent can still talk, but it can no longer read or write the record, and kit no longer reaches it.
The 21:00 plan run then writes "Learning objective unavailable (learnclaw)". Resume by re-applying steps 3 and 4.

**Retire / roll back:** `openclaw agents delete learnclaw` (it removes the entry and its
bindings). Then revert kit's `allowAgents` and the live kit "Learning" section,
and revert the PR so the deploy stops installing the contract. Rows already on the learning page
stay; they are the owner's record.

### Daily LeetCode problem (learnclaw, #594)

learnclaw sends the owner one LeetCode problem a day, chosen by pattern, in its own Telegram chat, so the
weekly by-hand problem count (the number behind the two interview loops by 2027-02-17) has a daily prompt.
Kit's Sunday 18:00 `goal-weekly-checkin` reads that count from learnclaw instead of asking.

- **What is sent.** Name and link, two lines, nothing else: no pattern name, no hint, no solution. At most
  one a day, on home days only: Monday, Wednesday, Friday and Saturday. Tuesday and Thursday are office
  days and Sunday is rest: no message. The slot is the home-day 17:30 interview-problem slot, Europe/Dublin.
- **The list.** `skills/leetcode-daily/problems.json`, curated and in git: patterns in Track A order of the
  vault's `domains/learning.md` (A1 Big-O, which has no problems to send, then hash maps, two pointers and
  sliding window, stacks and monotonic stack, binary search, linked lists, trees, heaps, graphs, intervals
  and greedy, backtracking, DP 1-D then 2-D, tries and union-find), problems in order inside a pattern. Pure
  basics and problems with no clear pattern are left out. Edit the list here, then
  `python3 skills/leetcode-daily/daily.py check`; the deploy installs it.
- **The pick.** `daily.py next --record` (`skills/leetcode-daily/daily.py`, stdlib only) takes the first
  problem with no row in the record, so a done, a skipped or an already offered problem never comes back: one
  pattern at a time, in order. It decides the day itself: on Tuesday, Thursday or Sunday it prints
  `SKIP office day (Tuesday)` (or the Thursday / rest-day line) and records nothing, whatever fired it. A
  second call on the same day prints `SKIP offered today`. The unattempted problem stays offered; the next
  send is the next problem.
  The offered row is recorded when the problem is picked, before delivery, so a send that fails (or an
  unechoed reply) skips that problem for the day and it does not come back.
- **The record: no second store.** Each step is one `leetcode: <slug>` row in the artefact record that
  `learning-record` writes (the vault's `domains/learning.md`), through `record.py append`, so its checks and
  the vault lint apply: `in progress` = offered, `done` = solved by hand, `dropped` = skipped; the latest row
  is the state. A `done` row needs only the grilled cell, which holds the pattern and the time and space
  complexity in the owner's words. These rows are left out of the artefact projections of `record.py status`
  (one summary line instead). LearnHarness's record is read-only here and is not touched.
- **After the attempt.** In chat (Control UI, or the `reading` bot), learnclaw helps the owner understand the
  problem, name the pattern and state the complexity, and records `daily.py done <slug> --pattern ... --time
  ... --space ...` or `daily.py skip <slug>` on the owner's own words. learnclaw never writes the solution.
- **The weekly count.** `daily.py week` prints `solved: <n> (Monday <date> to Sunday <date>, by hand)` and the
  titles: the problems whose latest row is `done` and dated Monday to Sunday of the week of the given date
  (default today, Dublin).
- **The cron.** `scripts/ensure-leetcode-daily.sh` declares `leetcode-daily`: agent `learnclaw`, `30 17 * * 1,3,5,6`
  Europe/Dublin, isolated, announce to Telegram account `reading` at the owner chat, failure alert through
  Kit's `default` account. It is a **command payload** (like `workout-nudge`): the row runs
  `python3 .../daily.py next --record --push` directly, with no model turn, and announces its stdout. The script
  refuses a schedule that can fire on Tuesday, Thursday or Sunday, replaces a row that is still the old agent
  turn (`cron rm`, then add as a command), and after converging re-reads the live row and exits 1 unless the
  agent, command argv, schedule and delivery (Telegram `reading`, owner chat) are the declared ones.
  **The guarantee:** a `daily.py` error (vault page missing, lint refusing or not running, unreadable problem
  list, any traceback) exits non-zero with nothing on stdout, so the scheduler records the run as failed, the
  failure alert fires through Kit's account after the first one, and no text reaches learnclaw's chat; only
  a success prints the two message lines (name, link) and a skip day prints exactly `NO_REPLY`, which the
  scheduler suppresses. It is no longer best-effort, and a silent day is no longer how an error shows. Tests:
  `scripts/tests/test_leetcode_daily.py` (the error-run tests drive the cron's own argv).
- **The check-in.** `scripts/ensure-goal-checkin-solved.sh` adds one marked block to Kit's `goal-weekly-checkin`
  row (host state; it carries the owner's goal, so it is edited in place, not copied into git): run
  `daily.py week`, take the `solved:` number, never ask the owner for it. It adds `exec` to the row's tool list
  when that list lacks it, and leaves the rest of the row alone. `DRY_RUN=1` prints the new message first.
- **Install.** The deploy copies the skill with the rest of learnclaw (`ensure-learnclaw.sh`); the cron and the
  check-in wiring are operator steps, and the Telegram account is a platform key.

**Operator steps (once).** The `reading` account carries a token ref again, so follow "Telegram bot tokens:
enabled accounts only", re-enabling: apply the patch first, then merge, or merge and apply straight after (a deploy
before the patch deletes the ref again). `LEARNING_BOT_TOKEN` is already in the gateway SOPS file.

```bash
G=$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway)
# 1. Enable the reading account on its own token (the same keys platform.patch.json carries).
cat > /tmp/reading.patch.json <<'JSON'
{"channels":{"telegram":{"accounts":{"reading":{"enabled":true,"botToken":{"source":"exec","provider":"sops","id":"learning-bot-token"}}}}}}
JSON
docker exec -i "$G" openclaw config patch --stdin --dry-run < /tmp/reading.patch.json
docker exec -i "$G" openclaw config patch --stdin < /tmp/reading.patch.json
rm /tmp/reading.patch.json
# 2. Bind the learning bot to learnclaw and let it see the new skill (add to its list; keep the others).
docker exec "$G" openclaw agents bind --agent learnclaw --bind telegram:reading
docker exec "$G" node -e 'const c=require(process.env.OPENCLAW_CONFIG_PATH);const s=((c.agents.entries.learnclaw||{}).skills||[]).slice();if(!s.includes("leetcode-daily"))s.push("leetcode-daily");process.stdout.write(JSON.stringify({agents:{entries:{learnclaw:{skills:s}}}}))' > /tmp/lc-skills.patch.json
docker exec -i "$G" openclaw config patch --stdin --dry-run < /tmp/lc-skills.patch.json
docker exec -i "$G" openclaw config patch --stdin < /tmp/lc-skills.patch.json
rm /tmp/lc-skills.patch.json
# 3. As lifekit, after the merge deploys: the cron, then the check-in wiring.
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-leetcode-daily.sh
sudo -u lifekit env DRY_RUN=1 /srv/lifekit-stack/scripts/ensure-goal-checkin-solved.sh
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-goal-checkin-solved.sh
```

**Verify.** `openclaw channels status` shows `reading` enabled; `openclaw skills --agent learnclaw list` shows
`leetcode-daily` ready; in the gateway, `python3 /home/node/.openclaw/agents/learnclaw/workspace/skills/leetcode-daily/daily.py next --date <a Tuesday>` prints
`SKIP office day (Tuesday)` and `status` prints the counts and the next problem; `daily.py week` prints `solved: <n>`.
Then one real run: `openclaw cron run <id> --expect-final --json` on a home day sends one message in the
learning bot's chat and appends one `in progress` row; on a Tuesday, Thursday or Sunday it prints
`NO_REPLY` and records nothing. To see the failure path, run the command with `LEARNING_VAULT` pointed at an empty directory: it exits non-zero.

**Pause** (the agent, its record and the list stay): `openclaw cron disable <id>` for `leetcode-daily`; the check-in then
reads the count of whatever the owner recorded in chat. **Retire:** `openclaw cron rm <id>`, delete the
marked block from `goal-weekly-checkin`'s message, and revert the PR; the rows stay, they are the owner's record.

### Learnclaw morning learning message (`learnclaw-morning`, #609)

Learning reaches the owner only from learnclaw (decision 2026-11-09). Kit's `today-brief` and
`evening-plan-note` carry no learning line any more, so learnclaw sends its own: one scheduled turn at 07:15
Europe/Dublin, Monday to Saturday, in learnclaw's own Telegram chat (account `reading`, never Kit's `default`).

- **What is sent.** At most four lines: (1) today's one LearnHarness session to open, or the one goal on the
  learning page, and the gap it closes; (2) one practical output; (3) `mirror newest session <date>`, so a
  stale mirror shows; (4) the weekly count of problems solved by hand, `<weekday> <n>/<target>` (`Mon 0/20`),
  quoted by the model from `weekly-count.json` and never counted by it (#620): `learnclaw-weekcount`, a command job
  at 07:10 (below), runs `daily.py weekfile`, the tested counter behind `daily.py week` (latest row per
  `leetcode: <slug>` topic, status `done`, dated Monday to Sunday, so it resets on Monday). The turn writes the
  file's `line` only when its `for_date` is today; otherwise, or if the file is unreadable, the line says
  `<weekday> count unreadable`, never 0. The target is `LEARNCLAW_WEEKLY_TARGET` (default 20, week of 2026-11-23),
  read by `scripts/ensure-learnclaw-weekcount.sh`; re-run that script after changing it. Sunday is excluded by the schedule alone: the script refuses a cron that can fire on Sunday.
- **Read-only.** The turn reads the LearnHarness mirror, the vault page `domains/learning.md` and `weekly-count.json`, and nothing
  else. Its `read` tool reaches the agent's workspace and the vault, not `~/.life-state` (the first test run,
  #611, said "mirror unreadable" for that reason), so it reads a copy inside the workspace,
  `/home/node/.openclaw/agents/learnclaw/workspace/learnharness-mirror`. `learnharness-sync.service` refreshes
  it after each pull (`ExecStartPost`, same files and modes, `--delete`); `ensure-learnclaw.sh` creates the
  directory. No path was added to the agent's file access. The row's tool
  allow-list is exactly `read`, which the script re-checks on the live row: no exec, no write, no
  `sessions_spawn`, no web, no send tool. It does not touch the `learning-record` skill.
- **The row.** `scripts/ensure-learnclaw-morning.sh` declares `learnclaw-morning`: agent `learnclaw`,
  `15 7 * * 1,2,3,4,5,6` Europe/Dublin, isolated, agent-turn payload on the repo's cron model
  (`scripts/lib/cron-model.sh`), `--light-context`, announce to Telegram `reading` at the owner chat, failure
  alert after one failure through Kit's `default` account. The owner chat id comes from `LIFEKIT_TELEGRAM_CHAT`
  in the host env file and is never written to git. It creates or edits the row, enables it, then re-reads the
  live row and exits 1 (naming the field) unless the agent, schedule and its timezone, message, tool list, model, isolated
  session and delivery (announce, Telegram, `reading`, owner chat) are the declared ones.
  Tests: `scripts/tests/test_learnclaw_morning.py`.
- **The count file.** `scripts/ensure-learnclaw-weekcount.sh` declares `learnclaw-weekcount`: agent `learnclaw`,
  `10 7 * * 1,2,3,4,5,6` Europe/Dublin, a command payload (no model turn) that runs
  `daily.py weekfile --out <workspace>/weekly-count.json --target <target>`, replacing the file atomically with
  `{"for_date", "line"}`. It prints `NO_REPLY`, so nothing is sent. A record that cannot be read writes
  `<weekday> count unreadable` and exits 1, so the failure alert fires. The script re-reads the live row and exits 1
  unless schedule, agent, argv and delivery are the declared ones. Tests: `scripts/tests/test_leetcode_daily.py`.
- **Cost.** One short model turn a day; the turn reads a page and a few session files.

**Apply** (once, after the PR merges and the deploy has run; the `reading` account must already be enabled and
bound to learnclaw, see "Daily LeetCode problem"). The sync unit is host state, so re-render it once (#611),
then pull and apply:

```bash
LH_SYNC_HOST=<PC tailnet name> LH_SYNC_USER=<PC account> \
  /srv/lifekit-stack/scripts/learnharness-sync/install-learnharness-sync.sh   # as the admin account
sudo systemctl start learnharness-sync.service                                 # fills the workspace copy now
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-learnclaw-weekcount.sh
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-learnclaw-morning.sh
```

**Blocked / allowed check.** Allowed: with the copy filled (`ls /srv/openclaw/config/agents/learnclaw/workspace/learnharness-mirror/chats`
lists files), the test run below delivers a line 3 `mirror newest session <date>`. Blocked: empty or remove that
directory's contents and run it again; line 3 is `mirror unreadable` and line 1 says so. Restart the sync unit
afterwards.

**Test run** (one real delivery, in learnclaw's chat; a manual run ignores the schedule, so it delivers on any day):

```bash
GW=$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)
ID=$(docker exec "$GW" openclaw cron list --all --json | python3 -c 'import json,sys; t=sys.stdin.read(); print(next(j["id"] for j in json.JSONDecoder().raw_decode(t, t.find("{"))[0]["jobs"] if j["name"]=="learnclaw-morning"))')
docker exec "$GW" openclaw cron run "$ID" --expect-final --json
```

**Undo.** Pause: `openclaw cron disable <id>` (the next run of the ensure script enables it again, so stop
running it). Retire: `openclaw cron rm <id>` and revert the PR. The mirror, the learning page and the record are
untouched either way.

### Weekly planning reminder (`planning-reminder`, #619)

A recurring reminder from Kit for the owner's weekly planning session: every Monday at 10:00 Europe/Dublin, one
fixed line in Kit's own Telegram chat (account `default`) pointing at the planning session and nothing else.

- **What the row is.** `scripts/ensure-planning-reminder.sh` declares `planning-reminder`: agent `kit`,
  `0 10 * * 1` Europe/Dublin, isolated, a command payload (`echo` of one fixed line), announce to Telegram
  `default` at the owner chat, failure alert after one failure. No model turn runs and nothing is read, so the
  message cannot carry vault, mail or calendar content. The owner chat id comes from `LIFEKIT_TELEGRAM_CHAT` in the
  host env file (or `PLANNING_REMINDER_CHAT`) and is never written to git.
- **Fail closed.** The script exits 1 with a plain reason when the owner chat id cannot be resolved (it never
  guesses a target) or when the cron is not Monday only, and exits 1 naming the field when the re-read live row
  differs from the declared one. A re-run on a converged row changes nothing (it only re-enables a paused row).
  Tests: `scripts/tests/test_planning_reminder.py`.

**Apply** (on the host, after the PR merges and the deploy has run; a separate gated step):

```bash
sudo -u lifekit /srv/lifekit-stack/scripts/ensure-planning-reminder.sh
```

**Remove.** Pause: `openclaw cron disable <id>` (a later run of the ensure script enables it again, so stop running
it). Retire: `openclaw cron rm <id>` inside the gateway (`docker exec <gateway> openclaw cron list --all --json`
shows the id) and revert the PR.

### Learning objective line from learnclaw (`evening-plan-note`, `today-brief`) - retired

> **Retired** by "Learnclaw morning learning message" above (decision 2026-11-09, #609): Kit's `today-brief`
> and `evening-plan-note` no longer carry a learning line. Its script, `scripts/ensure-learning-line.sh`
> (issue 525), was deleted in issue 678 so nothing can re-apply a superseded declaration; it is in git
> history if the text is ever wanted.

### Answer shape in the remaining personas

The 2026-11-15 "Answer shape (every reply)" rule (answer first, 75-word budget, plain short
sentences, one idea per sentence) is in Kit, Career, Learnclaw and Ledger. The same block, with Kit's
wording and numbered points, now also goes into the reader seed
(`defaults/agents/reader/workspace/SOUL.md`) and into the live `SOUL.md` of `health`, `learning` and
`social`, which live on the host only (mirrored to `lifekit-hq/openclaw-config` as
`agents/<name>/workspace/SOUL.md` by `openclaw-config-sync`, see "openclaw-config mirror").

`scripts/ensure-answer-shape.sh` reads the block from Kit's seed (so the wording cannot drift) and
inserts it into each persona's live `SOUL.md` only when the "## Answer shape (every reply)" heading is
absent, just before its "## Voice" section (at the end when there is none). A file that already has
the heading stays byte-identical; nothing else in any file is rewritten, so a second run adds
nothing. A persona with no workspace on the box (`health`, `learning` and `social` were retired
2026-10-07) is skipped with a note. `reader` is also rewritten from the seed by
`ensure-reader-agent.sh` on every deploy, so it carries the block either way.

**Operator step after the PR merges** (as `lifekit`, from `/srv/lifekit-stack`; the deploy does not
run it):

```bash
scripts/ensure-answer-shape.sh --dry-run   # per persona: would insert / already present / no SOUL.md; writes nothing
scripts/ensure-answer-shape.sh             # health learning social reader
```

**Verify:** a second run prints "already present" for every persona that exists. Then confirm the
sync mirrored it: start `openclaw-config-sync.service` (or wait up to 15 minutes for the timer), then
`git -C /srv/openclaw-config log -1 --stat` shows an `auto: lifekit-vps openclaw sync` commit touching
the changed `agents/<name>/workspace/SOUL.md` files, and
`grep -c 'Answer shape (every reply)' /srv/openclaw-config/agents/<name>/workspace/SOUL.md` prints 1
for each. **Undo:** delete the inserted section (heading through its tenth point) from the live
`SOUL.md`; the script puts it back on the next run, so stop running it.

### Light context on `today-brief` and `ledger-opportunity`

Both rows are host state (cron rows live in the gateway store, not in git) and both
prompts are self-contained: every file they read is an absolute workspace path, and
every tool call is named in the prompt. They do not need the workspace bootstrap
(`AGENTS.md`, persona and the other injected files), which each run carried on top
of its prompt (about 200k tokens a run in the token-diet audit). `--light-context`
(payload `lightContext: true`) drops that bootstrap for the run. The 2026-11-03
`jobs.py` figures are the baseline to beat: `today-brief` $0.74 a run,
`ledger-opportunity` $0.49 a run (list-price weighting).

**Operator step** (firstmate or the operator, once; the row hot-reloads, no gateway
restart, schedule, delivery and tools untouched). Ids differ per box, so read them
from the list first:

```bash
GW=$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)
docker exec "$GW" openclaw cron list --all        # ids for today-brief, ledger-opportunity
docker exec "$GW" openclaw cron edit <today-brief id> --light-context
docker exec "$GW" openclaw cron edit <ledger-opportunity id> --light-context
docker exec "$GW" openclaw cron show <id>         # payload shows lightContext: true
```

**Verify:** after both rows have run once on their schedule (`today-brief` fires 08:30 Dublin),
`python3 -I scripts/ops-report/jobs.py --days 1` should show `cron:today-brief` and
`cron:ledger-opportunity` per-run well below the baseline. Read the delivered brief
that day: it should still carry the calendar, the gym line, the deadlines and the
fleet section; `ledger-opportunity` should still end in `NO_REPLY` or one message.
Keep watching for a week (`--days 7`); a brief that loses a section or an opportunity
message that stops following its prompt means the prompt leaned on the bootstrap.

**Rollback:** `openclaw cron edit <id> --no-light-context`. Same hot reload.

`scripts/ensure-today-brief-fleet.sh` carries the flag for `today-brief`: it sets
`--light-context` with its message edit, and on a row that already has the fleet step
but not the flag it sets just the flag. So on the VPS host, as `lifekit`,
`/srv/lifekit-stack/scripts/ensure-today-brief-fleet.sh` applies the `today-brief` half
of the operator step above; `ledger-opportunity` has no ensure script, so it still
takes the `openclaw cron edit <id> --light-context` line.

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

## Cron declarations check

Cron rows live in the gateway's store, not in git. Each `scripts/ensure-*.sh` that adds or edits a row
is that row's declaration, and accepts `--check`: it writes nothing, compares the live row's agent,
schedule (expression and time zone), enabled flag, payload, trigger and delivery with what a converge sets
(the timeout, light-context and failure-alert settings a converge also sets are not all compared), and exits 0 when they
agree (`ok: cron '<name>' ... agrees with <script>`), 1 on drift (`drift: ...` or `refusing: ...`, naming
the field), 2 on bad usage, and 3 when it could not check (no gateway container, no owner chat id,
unreadable cron list). The shared part is `scripts/lib/cron-check.sh`. The gateway records the inference
account on live rows itself, so `--check` and the converges treat `<cron model>@anthropic:api-key` as the
declared model (that one form only).

`scripts/check-cron-declarations.sh` runs every declaration's `--check` and prints one line per script
(`ok`, `drift` for exit 1, `error` for any other non-zero, with the script's last line); an `error` line
means the check did not run (a gateway restart, a missing env value), not that git and live disagree. The deploy runs it on every deploy, path gate or
not, and fails the deploy at the end when any line is not `ok`. It never changes a row itself: a red
deploy here means git and live disagree, and the fix is a decision, not a retry.

- **Live is behind git** (the row is missing, paused, on an older prompt): run the named script without
  `--check`, as `lifekit` from `/srv/lifekit-stack`, then re-run the check.
- **Live is the intended design**: change the script in a PR so it declares what runs.
- **The job is retired**: `openclaw cron rm <id>`, and delete the script and its line in
  `DECLARATIONS` in the same PR.

Read-only, any time: `bash /srv/lifekit-stack/scripts/check-cron-declarations.sh` (a few minutes: each
script lists the crons itself). A new cron-installing `ensure-*.sh` must take `--check` and be listed in
`DECLARATIONS`; `scripts/tests/test_cron_check.py` fails otherwise. Rows no script declares (operator
steps in this runbook, such as `workout-nudge` and `evening-day-plan`, and rows agents add themselves)
are not checked.

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
docker exec "$GW" node -e "fetch('http://127.0.0.1:18789/healthz').then((r)=>r.text()).then(console.log)"   # {"ok":true,...}; the host port exists only in token mode
docker exec "$GW" openclaw channels status                  # all 6 Telegram accounts connected
docker exec "$GW" openclaw mcp status                       # configured servers; `mcp probe` needs the gateway stopped since 2026.11.4 (scripts/upgrade-smoke/openclaw.sh checks reachability)
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
under the home's `data/<decision id>/`; else the http(s) address in the
decision's own text; else `null`; running, queued and held tasks get `open_url` the same way), and copies finished
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
state and last-seen time (from a fleet home's cached secondmate summary),
per-item `started_at`, `landed_at`, `priority` and `ask` fields, and sanitised
`metrics/quota.jsonl` / `metrics/quota-snapshot.json` (contract in the README).
It also adds `status_page`, the living status page's sections (records,
projects, capacity, queue, intake, how-it-works, delivered decisions, problems; README
"Status page sections"), built by `fleet_status.py`, installed beside the script as
`/usr/local/bin/lifekit-fleet-status.py`. That helper reads each home's backlog
through its `bin/fm-tasks-axi.sh` and its `bin/fm-capacity.sh --json`, asks
`gh` for open PRs (cached ten minutes in the publisher's state directory), and
runs `jobq.py depth` in the gateway container (the admin account's `docker`
group); a source it cannot read leaves a line in `status_page.errors`, never a
failed run. The new output appears only once the
installed script is refreshed: re-run the install command below (it
re-installs the script, the helper and the units, idempotent). The unit's
environment gains `FLEET_PLAN_MAP` only when the installer is given one
(`FLEET_PLAN_MAP="<repo>=<project> ..."` beside `FM_HOMES`; README "One row per
project"): the alias list that folds a repo spelling naming no project, such as
the vault's, into its project's row. It is host config, so a re-install without
it drops the line. `FLEET_CLIP_STATS_FEED` works the same way and is what
turns the clip queue rows on: on the VPS pass
`FLEET_CLIP_STATS_FEED=/home/denys/clip-data/queue/stats.jsonl` beside
`FM_HOMES` (an absolute path of `[A-Za-z0-9._/-]`; only its parent directory is
read); unset, the unit carries none and those rows stay `null`.
Stalled items (`status_page.stalled`, README "Stalled items") are marked by one
mechanical age check over every queue; the thresholds are the one
`scripts/fleet-publisher/fleet-stalled.json` (installed beside the helper), so
changing a number is a PR to that file and a re-install, never a code change.
The A2A source is `FLEET_A2A_CMD`, unset until a relay or session store the
publisher can reach exists; the page says so. The queue items
(`status_page.items`, README "Queue items") read `FLEET_FINANCE_CMD` and
`FLEET_NEEDS_YOU_LEDGER`; neither the installer nor the unit sets them, so point
them with a systemd drop-in. Unset, `item_sources` says `read: false` and
`items` stays empty.
Clip and scout are two more stalled sources (`clip`, `scout` blocks of the same file:
`buffer_floor_days`, `slot_window_hours`, `idle_hours`), and
`status_page.clip_queue` publishes one row per clip channel (cosmos, mind, neural, yc,
vice-footage: waiting, oldest age, buffer days, egress, stalled; README "Clip queue"). Both
are read by the publisher's existing run, read-only, from the directory of
`FLEET_CLIP_STATS_FEED` (the clip queue's `daily-check.json`, `egress.json`,
`queue-run.json`; a missing input shows as unavailable, never zero) and, for the scout,
`FLEET_SCOUT_STATE` (its state dir). The installer sets the first (above); the unit never
sets `FLEET_SCOUT_STATE`, so point it with a systemd drop-in. Unset, the page says
`read: false`.

The same run writes `/var/lib/lifekit-fleet/taken-for-you.json`, the dashboard's
"Taken for you" feed: the last 200 actions the fleet took under a standing
ruling (P16) instead of asking, newest first, one line each with a stable `id`
(contract in the README "Taken for you feed"). Its sources are two host logs
read-only, in the admin account's `~/.local/state/fleet-policy/` (the unit
runs as that account; `FLEET_POLICY_DIR` overrides): `p16.jsonl`, the
permission engine's audit log, and `self-calls.jsonl`, the calls the fleet
answered itself. Only the subject line, ruling, outcome, time and id leave the
host - a self-answered call's answer text never does. Lines that do not parse
are skipped and counted in the file's `sources.<log>.skipped`, so a nonzero
count there is the place to look when the writer's format drifts; a log that
does not exist yet shows `read: false`, and with neither log present the
previous file stays. The new file and `lifekit-fleet-taken.py` appear only
after the install command below is re-run (no sudo is needed beyond that, and
no container is touched).

The same run writes `/var/lib/lifekit-fleet/screen-changes.json` and the images
under `screen-changes/`, the dashboard's "Screen changes" feed: each open pull
request, or one merged in the last 7 days, in `FLEET_SCREEN_REPOS` (default `lifekit-hq/lifekit-dashboard`) that
changes a `*-linux.png` baseline, with its before and after images (contract in
the README "Screen changes feed"). It reads GitHub through the admin account's
`gh`, read-only, so that account's `gh auth` must reach the repo. The helper
`lifekit-fleet-screens.py` appears only after the install command below is
re-run. To add an app, set `FLEET_SCREEN_REPOS` in a systemd drop-in for the unit
(space-separated `owner/name`); an empty value turns the feed off. A failed run
keeps the previous feed and turns *host systemd unit is down or failed* red.

**Cadence under load.** Every run reads the homes, rebuilds `status_page`, and
replaces the summary and `fleet.prom` (the freshness stamp) first; the daily
metrics, quota and taken feed follow. The two slow helpers run after that, on a
slower cadence: the screen changes feed (`gh`) every 5 minutes
(`FLEET_SCREENS_EVERY`, default 300 s) and the activity journal every 15 minutes
(`FLEET_JOURNAL_EVERY`, default 900 s), and neither starts once the run is two
minutes old (`FLEET_SLOW_BUDGET`, default 120 s; it stays due for the next run). A
section that is not due leaves its previous file in place; the cadence stamps are
`cadence-<section>` in the publisher's state directory. Each run puts its
working files in `/var/lib/lifekit-fleet/.work.*`, not `/tmp`, so a `/tmp` cleaner
cannot take them mid-run (the 2026-11-24 `bound.tsv: No such file` failure), and a
run that dies before replacing `fleet.prom` still rewrites its freshness gauges
from each home's own summary. Read how a run spent its time from the unit's
journal (`journalctl -u lifekit-fleet-publisher.service -n 5`): the last line of a
run is `fleet-publisher: timings_ms total=... homes=... status_page=... screens=...
journal=...` with `skipped=` naming sections the cadence or budget skipped. The
per-section table is in `scripts/fleet-publisher/README.md` "Run cadence and what a
loaded host does". The 15 minute alert threshold is unchanged.

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
set and skips it otherwise (it passes no `FLEET_PLAN_MAP` or `FLEET_CLIP_STATS_FEED`, so re-run the
installer by hand to restore one). Re-running the installer is how a home is
added or removed: it reinstalls the script and rewrites the unit's `FM_HOMES`.
The `lifekit-*.timer` glob in the unit gauge picks the timer up with no further
change. Merge deploys the Grafana rule and dashboard.

**Clip stats feed (operator step).** The same run appends the daily activity
journal under `/var/lib/lifekit-fleet/journal/`. Its `clip_stats` field reads
`FLEET_CLIP_STATS`, which the unit does not set, so the field says `unavailable`
until an operator points the variable (a systemd drop-in for the unit) at the
side-hustle feed. Feed format and how the retro board treats an unset variable:
README "Activity journal and retro board".

## Reply pusher

When firstmate answers one of the owner's notes sent through Kit (a `kit-` request id),
Kit says that reply in the owner's chat at once. `scripts/reply-pusher/` is the host
unit behind it: `lifekit-reply-pusher.path` watches each pinned home's reply drop
(`<home>/state/inbox/.replies`) and starts the oneshot `lifekit-reply-pusher.service`,
which runs `reply_pusher.py`. The pusher reads a home only through
`fm-inbox.sh receipts` (never inside the home), keeps its own cursor and an outbox under
`/var/lib/lifekit-reply-pusher/<home>/`, and POSTs `<hook path>/firstmate-reply` with
`{home, noteId, cursor}` - identifiers only, never a note or reply body - and
`Idempotency-Key: <home>:<cursor>`. Kit's turn (the "Firstmate reply" paragraph of the
`secondmate-relay` skill) reads the reply back with `relay.sh replies <home>` and says it.
Only `kit-` replies push; `dash-`, `fs-`, `oc-`, `fleet-`, `term-`, any other prefix and no prefix are
skipped (the cursor still moves past them).

A failed POST keeps its outbox entry; the unit exits 1 and systemd retries it with a
growing back-off (5 s up to 5 min, no start limit), so a gateway restart loses nothing and
a retry carries the same key (the gateway answers a duplicate from its cache, no second
turn). The gateway keeps no durable queue, which is why the pusher owns the outbox.
Exit 78 (no cursor, or the two secrets missing or malformed) is not retried: run the
installer, or fix `stack.env`.

**Route.** The pusher POSTs to `REPLY_PUSHER_GATEWAY`, default `http://127.0.0.1:18894`:
the edge's `openclaw-hook` entrypoint (`compose/edge/traefik/openclaw.yml`), bound to
loopback. In trusted-proxy mode the gateway's own `127.0.0.1:18789` is not published and the
edge's `:18892` puts every path behind the sign-in, so this route is the host's way in. It
passes exactly one request shape, `POST <OPENCLAW_HOOK_PATH>/firstmate-reply`; any other
method or path answers 404 from Traefik and never reaches the gateway. It strips the
identity headers and adds none, so the hook token (checked by the gateway) is the only
authentication. Traefik reads `OPENCLAW_HOOK_PATH` once at start from the rendered env
file (slashes around the value are trimmed, as the pusher does), so a rotation of the
path needs the edge recreated by the next deploy. To remove the route, delete the `openclaw-reply-hook` router, the
`openclaw-hook` entrypoint and the `127.0.0.1:18894` port; the pusher then fails with a
transport error and keeps its outbox. A quick check from the host (a 404 with no token,
never a gateway page): `curl -si -X POST http://127.0.0.1:18894/nothing | head -1`.

The pusher reads exactly two keys, `OPENCLAW_HOOK_TOKEN` and `OPENCLAW_HOOK_PATH`, from
`/srv/lifekit-secrets/stack.env` on each run, held in memory only (secrets inventory rows,
boundary rule). A rotation of the hook token or path therefore needs no pusher step; a push
that lands between the render and the gateway recreate answers 401 and retries.

**Install and cutover (operator step, never automatic).** The merge deploy lands the
`firstmate-reply` mapping and `allowedAgentIds` through the platform patch; it does not
install the units. On the VPS, as the admin account:

```bash
sudo FM_HOMES="<home> [<home> ...]" ADMIN_USER=<admin account> \
  bash /srv/lifekit-stack/scripts/reply-pusher/install-reply-pusher.sh
```

The installer copies the script, renders the units for the pinned homes, **baselines each
cursor at the home's newest existing reply (so no history replays)**, then enables the
service (boot catch-up) and `--now` the path unit. `bootstrap-vps.sh` runs it when `FM_HOMES`
(or `FM_HOME`) is set. Re-running is how a home is added or removed.

Rehearse before trusting it:

1. Send a synthetic note through Kit (the owner's own note, `kit-` id) and have the home
   answer it with `fm-inbox.sh reply <id>`.
2. `journalctl -u lifekit-reply-pusher -n 20` shows a clean run; no body, URL or token is
   ever logged. `ls /var/lib/lifekit-reply-pusher/<home>/outbox` is empty and `cursor` has
   advanced.
3. An identifiers-only hook turn runs in an isolated Kit session and the owner's Kit DM gets one
   message with the reply.
4. **Check the delivery route.** The hook mapping runs kit in an isolated session and
   delivers with an explicit `channel: "telegram"` and `to: ${OPENCLAW_KIT_CHAT}`, so it
   no longer depends on a last route. (The owner's Telegram DM runs in its own per-peer
   session, `session.dmScope = per-channel-peer`, which is why `agent:kit:main` never had
   one: the earlier `channel: "last"` mapping ended in `Delivering to Telegram requires
   target <chatId>`.) `OPENCLAW_KIT_CHAT` (a master setting) falls back to
   `LIFEKIT_TELEGRAM_CHAT`, the owner's personal Telegram id, like `OPENCLAW_FINANCE_CHAT`,
   so a host that already has that one needs no new setting; set `OPENCLAW_KIT_CHAT`
   through the secrets runbook only to deliver to a different chat. While both are unset
   or empty the deploy's dry run rejects the patch, the gateway keeps the old mapping and
   the deploy ends red. Do not widen the mapping by hand.
5. Retry check: with the gateway unreachable (a maintenance window, never a forced
   restart for this), answer another note: `outbox/` keeps one entry and the unit retries;
   once the gateway is back exactly one hook turn arrives.

A stuck outbox shows as `lifekit-reply-pusher.service` failed: `systemctl status
lifekit-reply-pusher` and the journal name the cause (`transport error`, an HTTP status, a
home's `fm-inbox receipts` exit). The host unit gauge tracks the `.path` unit (the oneshot is
inactive by design) and the service's result.

**Rollback:** `sudo systemctl disable --now lifekit-reply-pusher.path
lifekit-reply-pusher.service`. Nothing else depends on it; the mapping can stay (it only
answers an authorized caller) or be removed through the platform patch.

## Vault door

ChatGPT reads and writes the memory vault and nothing else (GitHub is the transport, no MCP
door, the captain inbox is the one door). `scripts/vault-door/` gives it a way to put proposals
on the Monday list through the vault, with no new credential or service. One host timer runs
`vault_door.py bridge` as the admin account:

- `lifekit-vault-door-bridge.timer` (every 5 minutes) reads the vault `inbox.md`. A line
  `- YYYY-MM-DD - chatgpt: <text>` (indented lines continue it) becomes one captain-inbox note
  `proposal: <text>` in the main firstmate home (`fm-inbox.sh note --request-id gpt-<hash>`),
  and the line gets ` [forwarded: <note id>]`. A held `proposal:` note is reviewed with the
  Monday list, never dispatched.

**Install (operator, `denys`):**

```bash
sudo FM_HOME=<main fleet home> bash /srv/lifekit-stack/scripts/vault-door/install-vault-door.sh
```

`bootstrap-vps.sh` runs the same installer when `FM_HOME` is set. `FM_HOME` is the one home
that receives the notes (the main firstmate home, not the other fleet homes). The `lifekit-*.timer`
glob in the unit gauge picks the timer up with no further change.

**Rehearsal (before trusting it):**

1. Add `- <today> - chatgpt: bridge rehearsal` to `/srv/memory/inbox.md`, then
   `systemctl start lifekit-vault-door-bridge.service`. The line gains `[forwarded: <id>]` and
   `FM_HOME=<home> <home>/bin/fm-inbox.sh list` shows the note as `proposal: bridge rehearsal`.
   Acknowledge it as usual and delete the vault line.
2. Run the bridge again: no second note (`journalctl -u lifekit-vault-door-bridge -n 5` says
   nothing was sent).

**Reading a failure.** The bridge unit fails (exit 1) when `fm-inbox.sh` fails or the vault
cannot be written; the affected lines stay unmarked and the next run retries under the same
request id, so a note is never lost or doubled. Exit 78 means `VAULT_DOOR_FM_HOME` is missing
from the unit: rerun the installer with `FM_HOME`. A marker edit to `inbox.md` can race a
ChatGPT append on the outbox branch; `memory-sync` then fails that tick on the rebase and the
next tick resolves it, and the marker is re-applied by the bridge (the request id replays) if
the merge dropped it.

**Retiring the old snapshot units.** Earlier installs also put a daily
`lifekit-vault-door-snapshot.timer` on the host, which wrote the vault page
`system/fleet-snapshot.md`. Nothing reads that page any more and the code is gone; on a host
that still has the units:

```bash
sudo systemctl disable --now lifekit-vault-door-snapshot.timer
sudo rm /etc/systemd/system/lifekit-vault-door-snapshot.{service,timer} && sudo systemctl daemon-reload
```

Rerun the installer afterwards so the installed `vault_door.py` is the bridge-only one. Then,
in the vault, delete `system/fleet-snapshot.md` (and its line in `index.md` on the next index
build).

**Undo:**

```bash
# stop the job (nothing else depends on it)
sudo systemctl disable --now lifekit-vault-door-bridge.timer
sudo rm /etc/systemd/system/lifekit-vault-door-bridge.{service,timer} && sudo systemctl daemon-reload
sudo rm -r /usr/local/libexec/lifekit-vault-door
```

Notes already forwarded stay in the captain inbox like any other note; acknowledge or delete
them there. `[forwarded: ...]` markers in `inbox.md` are inert and age out through the normal
30-day rotation.

## Intake to vault

The software half of "take the knowledge from a judged video as reference" (no model turn):
`scripts/intake-to-vault/` files each accepted video-eval report into the memory vault, in
the vault README's shape for raw evidence and through its `bin/` gates.
`lifekit-intake-to-vault.timer` (hourly, as `lifekit`) runs `intake_to_vault.py`, which for
each report under `/var/lib/lifekit/intake/reports/` whose `Verdict:` is `adopt` or `accept` writes:

- one immutable source page `sources/<judged date>-video-<key>-v<rubric>.md`: clipper
  frontmatter (`title`, `url`, `author`, `captured`, `type: other`, `summary`), then the report
  and the transcript, both verbatim in fences. `bin/lint.sh --changed` must report no high or
  medium finding on it, else the page is removed and the unit fails;
- its index line, by `bin/index.sh`;
- one line through `bin/log.sh` linking the page: on `projects/<p>/log.md` when the report's
  `Project:` line names that project (the whole value, or its start followed by a non-name
  character), else on the root `log.md` as kind `intake`. The link also keeps the page out of
  the unreferenced-source rotation.

A `park` or `drop` is not filed: the vault keeps only what the owner accepts (vault `CORE.md`),
and the intake index already records every judgment. Every step is checked first, so a rerun
writes nothing and a half-done run is finished by the next one. memory-sync commits the page
and the index; `bin/log.sh` commits its own line as `Kit (lifekit-vps)`. The run skips while
`/srv/memory/.model-pass` is under an hour old. Details: `scripts/intake-to-vault/README.md`.

**Install (operator, `denys`):**

```bash
sudo bash /srv/lifekit-stack/scripts/intake-to-vault/install-intake-to-vault.sh
```

`bootstrap-vps.sh` runs the same installer. The unit is skipped while `/srv/memory` has no
clone, and the `lifekit-*.timer` glob in the unit gauge picks the timer up.

**Rehearsal (before trusting it):**

1. `sudo systemctl start lifekit-intake-to-vault.service`, then
   `journalctl -u lifekit-intake-to-vault -n 20`: one `wrote sources/...` and one `logged on
   ...` line per accepted report, and a closing count with `0 failed`.
2. Read one new page under `/srv/memory/sources/` and its line at the end of the project's
   `log.md` (or the root `log.md`); `sh /srv/memory/bin/index.sh --check` says up to date.
3. Start the service again: the journal shows no `wrote` or `logged` line.

The next `memory-sync` tick commits the pages and the index; `/srv/memory/.lint-failed` must
not name them.

**Reading a failure.** Exit 1: a report could not be filed; the journal names it and why (an
unreadable report, a lint refusal with its findings, `bin/index.sh` or `bin/log.sh` refusing).
The other reports are still filed and the next run retries. Exit 78: the vault has no `bin/`
harness (`lint.sh`, `index.sh`, `log.sh`).

**Undo:**

```bash
sudo systemctl disable --now lifekit-intake-to-vault.timer
sudo rm /etc/systemd/system/lifekit-intake-to-vault.{service,timer} && sudo systemctl daemon-reload
sudo rm -r /usr/local/libexec/lifekit-intake-to-vault
```

Pages already filed stay: they are vault sources like any other and rotate under its rules.

## Intake shape step

The gate between an accepted idea and a build hand-off ("Intake to vault" files the evidence;
this decides what happens next). `scripts/intake-shape/intake_shape.py sweep`, run every 10
minutes by `lifekit-intake-shape.timer` (as the admin account, which owns the firstmate home,
has `gh` logged in and can write `/var/lib/lifekit/intake`), does two things with no model turn:

1. **Open.** Every intake row whose verdict is `adopt` or `accept` (both route the same way:
   `adopt` is "take it as it is", still shaped before anything is built) gets a **shaping
   item**: a captain-held task `shape-<key>` (`fm-captain-hold.sh hold`) shown on the Calls
   page. Its reason carries the proposed shape (owner repo from the report's `Project:`, class
   `dev`, done-when from its next step) and the options. One item per source: a repeat (the
   same source judged again) adds `(seen Nx)` to the open item's title; a repeat of an
   answered item opens `shape-<key>-2` whose reason names the earlier one. The row gets
   `shaping <item>` in two trailing cells, `status` and `ref`. Only rows dated on or after a
   cutoff open items, so the ideas accepted before the step existed are never asked about: the
   first run records its UTC date in `.shape-since` in the intake directory, and
   `intake_shape.py open --since YYYY-MM-DD` sets it (as the admin account). The next id and
   the "repeat of" reference come from the intake rows, since the hub archives closed items.
2. **Answer.** When the owner's answer is recorded on the item, the step files ONE issue in the
   owning repo (a registered project from `INTAKE_SHAPE_REPOS`) with a `## Done when` section,
   `Owner repo:`, `Class:` and `Decision: owner ruled` (or `matrix auto-answered` when the
   answer starts `matrix:`), then sets the `ready` label. **Only this step sets `ready`**, and
   only after reading the filed issue back and finding all four. The row becomes
   `handed-off <issue url>`; from then on the issue owns the item and the hub keeps the
   reference. Answer `approve` to file as proposed, `drop` to file nothing, or send
   `repo:` / `class:` / `done when:` lines to change the proposal. A closed item the decision
   matrix settled (its record opens "Captain answered this call through matrix settlement")
   is read from its `matrix:` line, else its `Answer:` line, by the same rules and counts as
   `matrix auto-answered`. An answer that cannot be
   filed (unreadable, no registered repo, a failed check) is logged once and does not end the
   item: the same item is reopened under its hold with the reason in its text, the rows stay
   `shaping`, and the step waits for the owner's next answer on it.

The same shape is the issue template `.github/ISSUE_TEMPLATE/shaped-work.md` for filing by
hand; other registered projects copy it. The devclaw side (refusing a goal on an issue without
`ready`) is separate.

**Install (root step under the host-change rule, run with `sudo` by `denys`).** The gateway's
`video.py` chmods the intake directory and its lock on every run, which clears the ACL mask,
so a one-off `setfacl` does not last. The installed unit carries a root
`ExecStartPre=+/usr/bin/setfacl -R -m u:<admin>:rwX,m::rwX,d:u:<admin>:rwX,d:m::rwX` that
re-applies the ACL (access and default) on `/var/lib/lifekit/intake` before every sweep, so
the install needs no manual `setfacl`; the installer renders the admin account into it
(`ADMIN_USER`, default `denys`):

```bash
sudo INTAKE_SHAPE_FM_HOME=/home/denys/fm-vps \
  INTAKE_SHAPE_REPOS="lifekit-stack=lifekit-hq/lifekit-stack" \
  bash /srv/lifekit-stack/scripts/intake-shape/install-intake-shape.sh
```

Optional `TASKS_AXI=<absolute path of tasks-axi>` (add it to the `sudo` line): the unit gets
`TASKS_AXI=` and a `PATH` that starts with that tool's directory. Unset, the installer looks
the tool up as the admin account (login shell, then an interactive one for a node version
manager) and refuses to install when it finds none. **Reinstall** after a change to the tool's
location, or to fix a unit installed without it: re-run the command above (it is idempotent;
it rewrites the unit and reloads systemd), then `sudo systemctl start lifekit-intake-shape.service`
and read `journalctl -u lifekit-intake-shape.service -n 20`.

> **Any root-installed unit that calls firstmate tools** (`tasks-axi`, `fm-captain-hold.sh`
> and the like) runs from systemd with a PATH that has no node version manager's bin
> directory, so the installer must write the tool's absolute path (`TASKS_AXI=`) and a `PATH=`
> starting with its directory into the unit, as the fleet publisher and intake shape installers
> do. A unit that skips this fails at run time, not at install.

Only rows dated from the install day on open items; to include earlier ones, run
`intake_shape.py open --since YYYY-MM-DD` once.

**Reading a failure.** Exit 1: an unreadable intake index, lock or `.shape-since` (a one-line
`intake-shape: <intake dir>: <error>` in the journal - never a silent empty run); otherwise the journal names the item and why (the hold or `gh` refused,
an issue failed the label check, an answer the step cannot read, a `shaping` row whose hub
item is gone - the answer was lost with it, so re-ask by hand and mark the row; an answer that cannot be filed is not a failure state, the item is reopened and waits). Every step is checked first,
so the next run completes a half-done one and files nothing twice: the issue carries a hidden
`intake-shape: <item>` marker the step searches before creating. Exit 2: no firstmate home, or `tasks-axi not found` (one journal line for the whole run: reinstall with `TASKS_AXI`, see above).

**Undo:**

```bash
sudo systemctl disable --now lifekit-intake-shape.timer
sudo rm /etc/systemd/system/lifekit-intake-shape.{service,timer} && sudo systemctl daemon-reload
sudo rm -r /usr/local/libexec/lifekit-intake-shape
```

Filed issues and index marks stay.

## Host unit gauge

`scripts/unit-gauge/unit-gauge.sh` writes `host_unit_active{unit}` and
`host_unit_failed{unit}` every 5 minutes from `unit-gauge.timer` into the
textfile directory (`host_unit.prom`). Units: every `actions.runner.*`
service, `tailscaled`, `docker`, `containerd`, `cron`, `unattended-upgrades`,
the service of every `lifekit-*.timer`, the services of three timers named
before that convention (`memory-sync`, `openclaw-config-sync` and
`memory-rotate`), and `lifekit-reply-pusher` (a `.path` unit, not a timer). For a
timer or the path unit, `active` tracks the
`.timer`/`.path` (its oneshot service is inactive between runs by design) and `failed`
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
fires warning per repo while the gauge reads 1 (muted off the phone; it reaches the fleet inbox through `alert-inbox`); the message names the repo, the release PR and the
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
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py role-users admin'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py ensure-app "<name>" \
  --redirect-uri <uri> --post-logout-uri <uri> --secret-file ~/<name>.secret'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py set-redirects <app> \
  --redirect-uri <uri> --post-logout-uri <uri>'
sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py set-sign-in-exp \
  --sign-in-identifiers email username --primary-color <#hex> --logo-url <url>'
```

`assign-role` takes a user id, primary email or username. `role-users` prints
the primary email of each user holding the role, one per line, and changes
nothing. `create-user` makes
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

### Fleet identity (`fleet-agent`)

One narrow Logto identity for the fleet's routine calls to the owner's own
apps, so none of them borrows `firstmate-ops` (Management API power, far too
broad). It is a machine-to-machine app `fleet-agent` that gets a token for one
API resource, `https://fleet.lifekit.internal/api` (override with
`FLEET_RESOURCE`; it is only an audience name, nothing resolves it), carrying
only the scopes its role holds. `scripts/identity/fleet-agent.sh` does it all
through `logto-admin.py` as `firstmate-ops`; every step is idempotent and
nothing restarts Logto or any container.

| Scope | Action class | Accepted by |
| --- | --- | --- |
| `fleet:read` | reads of fleet and health data | lifekit-dashboard edge, finance-sentry |
| `fleet:recategorise` | reversible recategorisation | finance-sentry |
| `basket:price-ingest` | price ingest | basket machine endpoints |
| `basket:catalog-read` | product catalog read | basket machine endpoints |
| `basket:catalog-add` | product catalog add | basket machine endpoints |

Writes that change money, accounts or ownership have no scope here, so no
token can carry them. An app accepts a token by checking issuer (Logto's
`/oidc`), audience (the resource indicator) and the scope its endpoint needs.
The apps themselves are separate work: lifekit-dashboard#217,
finance-sentry#1355 and basket#62.

**Issue** (admin account, a checkout, firstmate's age key; re-running changes
nothing):

```bash
bash scripts/identity/fleet-agent.sh issue     # resource, scopes, app, role, grant, secret store
bash scripts/identity/fleet-agent.sh status    # what Logto holds; whether the store matches
bash scripts/identity/fleet-agent.sh verify    # a token from the stored credentials; fails if any scope is missing
```

The client secret is created once by Logto and written only to the host
secret store, `~/.config/firstmate/fleet-agent.env.sops` (0600, a SOPS dotenv
with `FLEET_AGENT_CLIENT_ID`, `FLEET_AGENT_CLIENT_SECRET`,
`FLEET_AGENT_RESOURCE`, encrypted to the same recipients as
`logto-m2m.env.sops`, [docs/secrets.md](./secrets.md) "Not in these files").
It never reaches argv, a log, the ledger, the repo or a PR. The only
consumer is the fleet on this host: no app holds the secret, so no app file
gets a new secret. Unlike the `firstmate-ops` file, do not read it with
`sops exec-env`: sops 3.9.1 reads a store without a `.env` extension as JSON,
and `exec-env` has no `--input-type`. Decrypt with explicit dotenv types, as
`scripts/identity/fleet-agent.sh` does, and source the values in a subshell:

```bash
( set -a; . <(sops decrypt --input-type dotenv --output-type dotenv ~/.config/firstmate/fleet-agent.env.sops)
  curl -s -u "$FLEET_AGENT_CLIENT_ID:$FLEET_AGENT_CLIENT_SECRET" \
    -d grant_type=client_credentials --data-urlencode "resource=$FLEET_AGENT_RESOURCE" \
    -d scope=fleet:read http://127.0.0.1:3001/oidc/token )
```

Ask for the scopes the call needs; Logto grants the intersection with the
role. A scope the role lacks is not an error: the token simply comes back
without it, and a token with no scope is refused by every app.

A run that died after Logto created the app and before the store was written
recovers by running `issue` again (the secret is read back from Logto). A
store whose client id is not the live app's (the app was deleted and made
again) is replaced by the next `issue`.

**Rotate** (on exposure, or yearly with the secrets review): adds a new secret,
writes it to the store, then deletes the older ones in Logto. The old secret
answers HTTP 401 at once; a token it already minted lives out its hour. The
fleet is the only holder, so nothing else changes:

```bash
bash scripts/identity/fleet-agent.sh rotate && bash scripts/identity/fleet-agent.sh verify
```

**Revoke.** Two levels; both leave a token already issued valid until it
expires, at most an hour (the resource's token lifetime):

```bash
bash scripts/identity/fleet-agent.sh revoke            # take the role away
bash scripts/identity/fleet-agent.sh revoke --delete   # delete the app and the stored secret
```

`revoke` is the pause: Logto has no per-application disable, so the app
keeps its credentials but its role (and with it every scope) is gone. It
still gets a token, one that carries no scope, and a scope-checking app
refuses it (`verify` then fails, naming the missing scopes). `logto-admin.py undo` puts the role back. `revoke --delete` is
retirement: the app is gone, no token is issued at all, and `issue` later
makes a new app with a new client id and secret. For a leaked secret, run
`rotate` first (the leaked one stops at once), then decide.

**Per-agent identities (optional).** For an audit trail per agent, give each
its own app with the same or a narrower scope list, through the generic
commands (the role is named after the app, so each app's scopes are its own):

```bash
sops exec-env ~/.config/firstmate/logto-m2m.env.sops 'python3 scripts/identity/logto-admin.py \
  ensure-m2m-app oc-kit --resource https://fleet.lifekit.internal/api \
  --scope fleet:read --secret-file ~/oc-kit.secret'
```

`ensure-m2m-app` makes the app's role hold exactly the scopes given (extras
are taken away), `rotate-secret` and `revoke-app` take the app name, and
`app-status` prints an M2M app's roles and scopes (a sign-in app's redirect URIs
instead) and the live-secret count without a value.
Each is also in the ledger for `logto-admin.py undo`, except a secret
rotation and an app deletion, which cannot be taken back. The secret file
goes into its own SOPS file the same way as above, then is shredded.

The rehearsal covers all of it (resource, scopes, the app, idempotence, the
scope intersection, rotation, both revocations) against a throwaway Logto;
`scripts/tests/test_logto_admin.py` covers the request shapes and the
store handling.

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
     "group:basket": [],
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

### Fleet agent on the dashboard and basket

The fleet-agent identity (above) is accepted by two of the owner's apps, each
dormant until its issuer setting is present. The apps live in their own
repos and deploy from them; this repo owns the edge route and the host
settings. Owner decision 2026-11-08 is the authority; lifekit-stack#491 is
the tracking issue. What each switch is, in the order to turn them on (each
is harmless alone, because an app with no issuer refuses every bearer):

1. **Edge route (this repo).** `dashboard-machine` in
   `compose/edge/traefik/dynamic.yml`: a `GET` with an `Authorization: Bearer`
   header on exactly `/api/fleet`, `/api/fleet/metrics` and `/api/fleet/quota`
   skips the sign-in check and goes to the dashboard, with
   `X-Lifekit-Edge-Proof` and `X-Auth-Request-Email` removed. The edge
   checks nothing about the token. Any other request, a bearer on another
   path or a non-`GET` included, stays on the `dashboard` router and the
   sign-in check. Traefik watches the directory, so the merge deploy applies
   it live with no container restart. Before the dashboard is switched on, a
   bearer through this route is answered `401` by the dashboard.
2. **Dashboard.** `FLEET_AGENT_ISSUER=<IDENTITY_ENDPOINT>/oidc` in
   `/srv/dashboard/.env` (not a secret; the value `deploy.sh` prints on its
   `identity: issuer` line, the same as the edge's
   `OAUTH2_PROXY_OIDC_ISSUER_URL`), passed through by the dashboard repo's
   `deploy/docker-compose.dashboard.yml`, plus the container joining the
   external `identity-oidc` network so it reads Logto's keys at
   `http://logto:3001/oidc/jwks` (its defaults for audience and key URL
   stay). The dashboard repo's `docs/MACHINE-ACCESS.md` has the detail. The
   change recreates the dashboard container, which needs an explicit routed
   instruction naming the dashboard container before it is merged or deployed
   (a general merge word does not cover it).
3. **Basket.** In `~/.config/basket/env`:
   `FLEET_AGENT_AUTHORITY=<IDENTITY_ENDPOINT>/oidc` (the issuer it pins) and
   `FLEET_AGENT_BACKCHANNEL_AUTHORITY=http://logto:3001/oidc` (where it
   fetches discovery and keys), with the `basket-app` container joined to
   `identity-oidc` by basket's compose. Basket's `docs/decisions.md` "Fleet
   identity" has the detail. The change recreates `basket-app` only, not its
   database, and needs an explicit routed instruction naming the `basket-app`
   container before it is merged or deployed (a general merge word does not
   cover it). The Funnel that makes basket public is untouched: the machine
   endpoints are reachable as before, and a fleet token is only accepted on
   the three it names.
4. **finance-sentry.** The same issuer setting when its change lands (its
   own repo; not part of this activation until then).

No secret is set anywhere: the apps verify a signature against Logto's
published keys, and the fleet's client secret stays in
`~/.config/firstmate/fleet-agent.env.sops`.

**Verify** after the deploys (a token read from the store, never printed;
the dashboard through the edge's loopback entrypoint, basket on its
loopback port):

```bash
tok() { ( set -a; . <(sops decrypt --input-type dotenv --output-type dotenv ~/.config/firstmate/fleet-agent.env.sops)
  curl -s -u "$FLEET_AGENT_CLIENT_ID:$FLEET_AGENT_CLIENT_SECRET" \
   -d grant_type=client_credentials --data-urlencode "resource=$FLEET_AGENT_RESOURCE" \
   -d "scope=$1" http://127.0.0.1:3001/oidc/token ) \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'; }
T=$(tok fleet:read)
for p in /api/fleet /api/fleet/metrics /api/fleet/quota; do
  curl -s -o /dev/null -w "$p %{http_code}\n" -H "Authorization: Bearer $T" http://127.0.0.1:18890$p   # 200
  curl -s -o /dev/null -w "$p no token %{http_code}\n" http://127.0.0.1:18890$p                       # sign-in redirect, not 200
done
P=$(tok basket:catalog-read)
curl -s -o /dev/null -w "catalog read %{http_code}\n" -H "Authorization: Bearer $P" http://127.0.0.1:3310/api/products       # 200
curl -s -o /dev/null -w "catalog, wrong scope %{http_code}\n" -H "Authorization: Bearer $T" http://127.0.0.1:3310/api/products   # 403
```

A token with the wrong scope is `403`, a missing, malformed, expired or
wrong-issuer token is `401`. Sign-in is checked by opening the dashboard in a
browser: it must still send an unsigned-in visit to Logto and back.

**Rollback**, quickest first; each leaves people's sign-in as it is:

- **Stop the dashboard's machine reads at the edge:** remove the
  `dashboard-machine` router (and its `dashboard-machine-headers`
  middleware) from `compose/edge/traefik/dynamic.yml` and merge. Traefik
  reloads it live; no container restarts. A bearer then meets the sign-in
  check and is refused.
- **Stop an app accepting the token:** empty `FLEET_AGENT_ISSUER` in
  `/srv/dashboard/.env` and redeploy the dashboard; empty
  `FLEET_AGENT_AUTHORITY` (and the backchannel) in `~/.config/basket/env`
  and redeploy basket. Every bearer is then refused (`401`). Each redeploy
  recreates that one container.
- **Stop the identity itself:** Logto has no disable. `fleet-agent.sh revoke`
  makes tokens scopeless (every app answers `403`); `revoke --delete` stops
  issuing; both leave a token already issued valid for up to an hour (see
  "Fleet identity"). The `identity-oidc` membership can stay; it only lets
  the app read public keys.

## Sign-in page branding

The Logto sign-in page shows each application's own mark and primary color:
the lifekit mark in Indigo `#4f46e5` for the sign-in gate (dashboard and devclaw
console share that one Logto client), the fs mark in Petrol `#175a6d` for
finance-sentry's client, the bk mark in Violet `#7c3aed` for basket's client
(no dark primary picked yet), the tenant default (lifekit mark, Indigo) for
anything else. Logto's per-application sign-in experience carries the logo and color;
the tenant's Terms/Privacy links are cleared, because lifekit publishes no such
pages. `scripts/identity/branding/README.md` has the seeds and where the marks
come from. Operator step, with the M2M credentials; it changes the live
sign-in page, so run it at go time:

```bash
sops exec-env <file>.sops 'bash scripts/identity/apply-branding.sh'
```

Idempotent; an application it cannot find by name is reported and skipped
(override with `GATE_APP`, `FS_APP`, `BK_APP`, `GRAFANA_APP`). Reverse each change with
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
the lifekit dashboard and the devclaw console (and the OpenClaw dashboard
once [its cutover](#openclaw-behind-the-sign-in-gate) has run). Once signed
in on either, the other opens without asking again. Two services:

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
| Dashboard machine clients | one door: a `GET` with a bearer on `/api/fleet`, `/api/fleet/metrics` or `/api/fleet/quota` goes straight to the dashboard, which checks the fleet-agent token itself ([Fleet agent on the dashboard and basket](#fleet-agent-on-the-dashboard-and-basket)) |
| devclaw console | after sign-in, the gate adds devclaw's bearer (`DEVCLAW_MCP_TOKEN`), so no token is asked for |
| Dashboard relay proof | after sign-in, the dashboard route adds `X-Lifekit-Edge-Proof` (`RELAY_EDGE_PROOF`) and the signed-in `X-Auth-Request-Email`; the dashboard's relay endpoint requires both, so a container on `lifekit-shared` that calls the dashboard directly is refused. The dashboard's own env carries the same `RELAY_EDGE_PROOF` (`docs/secrets-runbook.md`) |
| Not behind it | finance-sentry (its own Logto client), Grafana, Logto itself. The OpenClaw gateway goes behind it with its own cutover: [OpenClaw behind the sign-in gate](#openclaw-behind-the-sign-in-gate) |

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

## OpenClaw behind the sign-in gate

The OpenClaw dashboard (Control UI) moves behind the same Logto sign-in as the
lifekit dashboard and the devclaw console, so nobody pastes the gateway token
into a browser. The gateway switches to `gateway.auth.mode` `trusted-proxy`:
it takes the signed-in email that the edge's Traefik sends and grants the
owner's email `operator.admin` (`gateway.auth.identityScopes`). The URL stays
`https://<name>:18789`. Its Serve port points at the edge's `openclaw`
entrypoint instead of the gateway, and the gateway's own `127.0.0.1:18789` is
no longer published.

The OpenClaw phone app keeps working through the cutover and after it. Its
pairing, device token and WebSocket skip the sign-in, and the gateway's own
device auth checks them exactly as it does today. A throwaway replica proved
this before the change merged.

**Routes** (`compose/edge/traefik/openclaw.yml`):

| Entrypoint | Request | Goes to | The gateway sees |
| --- | --- | --- | --- |
| `openclaw`, `127.0.0.1:18892` (Serve's `:18789`) | `/oauth2/*` | oauth2-proxy (sign-in callback, sign-out) | - |
| | the phone app: User-Agent `OpenClaw/...` (native) or an iPhone/iPad WKWebView without `Safari/` (its dashboard tab) | the gateway | no identity (any client-sent copy is removed); its device token or pairing code, as today |
| | a node host (`openclaw connect`): `GET /j/<22-char code>`, or a WebSocket upgrade with no `Origin` and no `Cookie` on `/` or on `/__openclaw__/worker` | the gateway | no identity (any client-sent copy is removed); the join code, then the node's bootstrap or device token, checked by the gateway |
| | anything else: every browser, Mobile Safari included | the `sso` check, then the gateway | `X-Auth-Request-Email`, the signed-in Logto email, on HTTP and the WebSocket upgrade |
| `openclaw-internal`, `openclaw-edge:18893` (`lifekit-shared` only, not published) | `/api/diagnostics/prometheus` with the gateway-token bearer | the gateway | `service:prometheus`, capped at `operator.read` |
| | `/v1/*` with the gateway-token bearer | the gateway | `service:finance-sentry`, capped at `operator.write` |
| | `/v1/*` with the study door's own bearer (`STUDY_DOOR_EDGE_SECRET`; the route matches nothing while that secret is empty) | the gateway | `service:study-door`, capped at `operator.write` |
| | anything else | 404 | - |
| `study-door`, `127.0.0.1:18895` (published on loopback only; Serve's `:8098`) | `POST` or `OPTIONS` `/v1/turn` | the study door (not the gateway) | - (the door checks its own bearer and Origin) |
| | any other method or path | 404 | - |
| `openclaw-hook`, `127.0.0.1:18894` (published on loopback only) | `POST <hook path>/firstmate-reply` | the gateway | no identity (any client-sent copy is removed); the hook token, checked by the gateway |
| | any other method or path | 404 | - |

The User-Agent match is not access control: requests on the app route carry
no identity, so the gateway's device auth is their only gate, as it is today.
Unchanged and direct: finance-sentry's companion hook trigger
(`openclaw-gateway:18789`, its `OPENCLAW_HOOK_TOKEN` on the hooks path, which
`gateway.auth` does not govern), and the in-container CLI over loopback.
Kit's channels and cron run inside the gateway and do not change.

**Who gets in.** `gateway.auth.trustedProxy.allowUsers` holds exactly the
owner's Logto email plus the service identities above (`service:prometheus`,
`service:finance-sentry` and, once the study door is turned on, `service:study-door`). The gate already
admits only Logto's `admin` role, and the gateway narrows that to one email.
A browser that signs in pairs as a device with OpenClaw's default scopes
(`deviceAutoApprove`), and the owner's email adds `operator.admin`.

**What reaches the gateway.** Every request through the edge comes from
`10.231.78.2`, Traefik's pinned address on `openclaw-ingress`, with forwarded
headers. The gateway answers such a request `403 proxy_attribution_required`
in either auth mode unless `gateway.trustedProxies` lists that address, so
every deploy keeps it listed (`scripts/deploy-trusted-proxies.sh`). In token
mode it sits next to the host's bridge addresses and only attributes the
client: the gateway still checks the bearer and ignores the identity header.
After the cutover, `gateway.trustedProxies` is exactly `10.231.78.2`.
`openclaw-ingress` is internal and holds only Traefik and the gateway (`scripts/lib/deploy-common.sh`
`OPENCLAW_EDGE_IP`). `127.0.0.1:18789` is no longer published (the
`compose/openclaw/trusted-proxy.yml` overlay). The gateway stays on
`lifekit-shared` for its own outbound calls (finance-sentry-mcp,
notify-relay, otel-collector), so a container there can still open a TCP
connection to it. That container holds no identity, though: the gateway takes
an identity only from `10.231.78.2` and no longer accepts the token. So "only
from the proxy" holds for authentication, not for TCP.

**Why the service clients go through the edge.** In trusted-proxy mode the
gateway accepts no bearer. Its documentation says the password configured
next to trusted-proxy mode is honoured only for local-direct callers
(loopback, no forwarded headers), so it cannot carry Prometheus' scrape or
finance-sentry's calls across the network. The replica confirmed this. Those
clients therefore keep the bearer they already hold, and the edge's internal
entrypoint swaps it for a service identity. `OPENCLAW_GATEWAY_PASSWORD`
serves only the in-container CLI (`docs/secrets.md`). That includes the
deploy's `exec` calls: running inside the gateway does not authenticate them,
and without the password a 2026.10.2 replica refused `health`, `cron list`
and `channels status` (`trusted_proxy_untrusted_source`). The internal routes
work in token mode too, once the deploy has listed the edge in
`gateway.trustedProxies`, so the clients move first, with no gap.

**Deploys after the cutover.** `openclaw_compose` reads the mode from
`openclaw.json` and adds the overlay in trusted-proxy mode, so every deploy,
one-shot run and rollback follows what the cutover left. Each deploy also
keeps `gateway.trustedProxies` at exactly the edge in trusted-proxy mode, and
in token mode lists the edge next to the host's bridge address
(`scripts/deploy-trusted-proxies.sh`). The auth keys are host state, never in
`platform.patch.json`.

**Rehearsal.** CI's tests job runs the whole cutover and rollback against
stubbed docker, tailscale and curl (`scripts/tests/test_openclaw_sso.py`):
the order of every step, the exact config before and after, a refused dry
run, a failed check rolling back by itself, a failing rollback that keeps its
saved state, and no secret value in any output. Its Prometheus stub reports
the scrape up only while `trustedProxies` lists the edge, as the gateway
does. `test_edge_routes.py` pins the routes and the User-Agent match.

### Node host enrolment through the gateway edge

A headless node host (`openclaw connect`, the desktop browser node "Kit gets
hands" needs) cannot pass the sign-in, and the gateway in trusted-proxy mode
takes no shared token, so the edge gives it three narrow routes with no
identity (`openclaw-node-join`, `openclaw-node-socket`, `openclaw-node-worker`
in `compose/edge/traefik/openclaw.yml`; the table above):

- **Join:** `GET /j/<code>` only, with exactly the gateway's 22-character
  URL-safe code. The gateway redeems it once (about 10 minutes, rate limited
  per client IP, an opaque 404 on any miss).
- **Main socket and worker socket:** a WebSocket upgrade on `/` and on
  `/__openclaw__/worker` that carries neither `Origin` nor `Cookie`. A browser
  always sends `Origin` on a WebSocket, so it can never take these routes and
  keeps signing in. The node authenticates inside the socket (bootstrap token,
  then its device token), so the gateway's own device auth is the only gate,
  exactly as on the phone app's route. The User-Agent is not used: the node's
  WebSocket client sends none.

The cloud-worker artifact routes (`/__openclaw__/worker-bootstrap`, `-bundle`,
`-transfer`) are not opened: a browser-only node host does not use them.

**The alternative, weighed.** Keeping trusted-proxy and using the gateway's
`gateway.auth.password` is not possible across the network: it is honoured
only for clean loopback/direct callers (see "Why the service clients go
through the edge"), and the node host is another machine. Switching to token
mode would undo the sign-in gate for the dashboard. So the node host gets
routes that bypass the identity check and lean on the gateway's own device
auth, as the phone app already does.

**The doctor finding stays.** `core/doctor/node-hosting-preconditions`
`machine-client-auth` at `gateway.auth` is a config-only check, so it cannot
clear while `gateway.auth` has no token/password and the mode is
trusted-proxy; `scripts/rehearse-openclaw-bump.sh` keeps tolerating exactly
that finding. A node host enrolling through these routes is the proof that the
path works.

**Enrolling a node (operator, after the routes are deployed).** The machine
that hosts the node is chosen separately.

1. On the gateway: `openclaw devices join-code` (admin credentials). Use the
   `https://<name>:18789/j/<code>` URL it prints (the Serve origin).
2. On the node machine: `openclaw connect <join-url> --service --commands
   browser.proxy`. `--commands` restricts the advertised surface to the
   browser proxy (no `system.run`); without it the node declares the full
   default surface and bootstrap enrolment approves it.
3. `openclaw nodes list` shows the node connected, advertising `browser.proxy`
   only. Later command or capability expansion still needs `openclaw nodes
   approve`.
4. Route browser calls to it: `gateway.nodes.browser.node` (the node's id from
   `nodes list`) in `compose/openclaw-gateway/platform.patch.json`, in the PR
   that follows once the machine exists.

**Rehearsal on a throwaway replica** (`docs/runbook.md` "OpenClaw behind the
sign-in gate" shape: a copy of live state, never the live gateway): mint a
join code on the replica; enrol a throwaway node through the edge routes with
`--commands browser.proxy`; check `nodes list`; then check refusals through the
same routes: a join code replayed (404), a wrong code (404), a WebSocket with no
token (refused by the gateway's connect), and a plain browser request to `/`
(sign-in redirect). `test_edge_routes.py` evaluates the real `openclaw.yml`
rules against sample requests (node join, both sockets, browser look-alikes
with `Origin` or `Cookie`, other methods and paths, the worker-artifact paths,
the phone app, the sign-in callback) and asserts which router wins each. It
evaluates the rules in Python, so it does not replace a throwaway Traefik with
the real file for a change to the rule grammar.

### Before the cutover

Run these from the deployed checkout (`/srv/lifekit-stack`) after this
change's merge deploy. That deploy creates `openclaw-ingress`, starts the
edge's two OpenClaw entrypoints, and moves Prometheus' scrape onto the edge
while the gateway is still in token mode.

1. **The scrape.** `curl -s --data-urlencode 'query=up{job="openclaw"}'
   http://127.0.0.1:9090/api/v1/query` shows value `"1"`. A `0` with the
   target's error `403 Forbidden` means `gateway.trustedProxies` does not
   list the edge (`10.231.78.2`): run a deploy, which adds it. The cutover's
   preflight refuses in that state.
2. **The password.** It is already in the master file. Render it into the
   env file; nothing restarts:

   ```bash
   sudo bash scripts/secrets/render-stack-env.sh
   ```

3. **Logto callback.** Give the gate's application the third port's callback
   and sign-out URIs. `set-redirects` replaces each list, so name all three
   ports:

   ```bash
   sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py set-redirects "lifekit sign-in gate" \
     --redirect-uri https://<name>:18790/oauth2/callback \
     --redirect-uri https://<name>:18791/oauth2/callback \
     --redirect-uri https://<name>:18789/oauth2/callback \
     --post-logout-uri https://<name>:18790/ --post-logout-uri https://<name>:18791/ \
     --post-logout-uri https://<name>:18789/'
   ```

4. **finance-sentry** (operator step in that repository, no code change). Its
   agent calls OpenClaw's OpenAI-compatible API, and that must now go
   through the edge. In finance-sentry's `docker/.env.sops`, set
   `OPENCLAW_GATEWAY_URL` to `http://openclaw-edge:18893`, keep its
   `OPENCLAW_GATEWAY_TOKEN` as it is, and redeploy finance-sentry. The
   cutover's preflight checks it: it reads only that one variable,
   `Agent__OpenClaw__BaseUrl`, from the `finance-sentry-api` container,
   and refuses while it points anywhere but `openclaw-edge:18893` (a box
   without finance-sentry passes). The companion hook
   trigger (`Companion__AgentTriggerUrl`, `http://openclaw-gateway:18789`)
   stays as it is.
5. **lifekit-dashboard.** Nothing: it does not call the gateway.
6. **The owner's email.** List the emails the gate admits and pick the
   owner's:

   ```bash
   sops exec-env <file>.sops 'python3 scripts/identity/logto-admin.py role-users admin'
   ```

### Cutover

One command. It checks everything above before it changes anything: the
password and token are in the env file, the edge answers a browser with the
sign-in redirect and the phone app's dashboard tab with the gateway,
`gateway.trustedProxies` lists the edge, Prometheus scrapes through the edge, finance-sentry calls through it, and
Serve publishes `:18789` without Funnel. Then it:

- saves `gateway.auth`, `gateway.trustedProxies`,
  `gateway.controlUi.allowedOrigins` and Serve's `:18789` target to
  `/srv/lifekit-secrets/openclaw-sso-rollback.json` (0600);
- dry-runs the config patch inside the running gateway;
- stops the gateway and patches `gateway.auth` to trusted-proxy (owner's
  email only), `trustedProxies` to exactly the edge, and adds
  `https://<name>:18789` to `allowedOrigins`;
- starts the gateway without its published port and points Serve's `:18789`
  at `127.0.0.1:18892`;
- checks the result (CLI health, the browser redirect, the app route, Serve,
  and the scrape) and rolls back on its own if anything is off.

The gateway restarts once, as on any deploy that recreates it.

```bash
cd /srv/lifekit-stack
sudo bash scripts/openclaw-sso.sh cutover --operator-email <owner email>
```

**Live check, phone first.** Open the OpenClaw app. It shows the gateway
connected, the dashboard tab loads, and a message to the agent is answered.
Then open `https://<name>:18789/` in a browser: Logto's sign-in (or none if
already signed in on another surface: the cookie is shared), then the
dashboard, with no token asked for. `sudo bash scripts/openclaw-sso.sh check`
reports the mode and Serve target any time. If the phone does not connect,
roll back.

### Rollback

```bash
sudo bash scripts/openclaw-sso.sh rollback
```

The rollback restores exactly what the cutover saved: `gateway.auth`,
`trustedProxies`, `allowedOrigins` and Serve's `:18789` target. The gateway
comes back in token mode with `127.0.0.1:18789` published, which is today's
routing. If one of its steps fails, it keeps the saved file and says so; fix
the cause and run it again. Without a saved file, it restores token mode with
the gateway token's env reference, re-derives `trustedProxies` and points
Serve at `127.0.0.1:18789`.

Leave Prometheus, finance-sentry and the extra Logto URIs as they are: the
internal routes work in token mode too. The restored `trustedProxies` holds
the edge (the deploy keeps it there in token mode); without a saved file the
rollback adds it back with the host's bridge address.

**Rotating.** The password: `sops set` plus render, then a deploy (it
recreates the gateway). The gateway token: also restart the edge's Traefik
(`docker restart edge-traefik-1`), because it reads the token
once at start (`docs/secrets.md`).

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

## Reminder bot

Compose project `reminder-bot` (`compose/reminder-bot/`): one Telegram
long-polling reminder bot (Python, python-telegram-bot, SQLite). It publishes
no port and takes no inbound traffic; the database is the `reminder_data` named
volume at `/data`, `TZ` is `Europe/Dublin`, and the container is
`reminder-bot-reminder-bot-1`. Its log ships to Loki like every container's
([Container console logs in Loki](#container-console-logs-in-loki)).

The bot's source repo is private and publishes no image, and the deploy account
cannot read it, so the image is built by an operator account that can (its
existing `gh` login, read-only), from the commit in
`scripts/reminder-bot/PIN`:

```bash
bash scripts/reminder-bot/build-image.sh            # builds reminder-bot:<short sha>; skips if it exists
bash scripts/reminder-bot/build-image.sh --check    # exit 1 if the tag is absent
```

**Build before the merge that adds or bumps the pin**, then deploy. A pin bump
is a PR that edits `PIN`; the new tag is a different image, so the old one stays
for rollback until a prune. `deploy.sh` runs the `--check` before it brings the
project up: with the image absent it prints a red failure naming the build
command, leaves the other projects as they were, and goes red at the end. It
skips the project with a yellow warning while `REMINDER_BOT_TOKEN` is not yet in
the rendered env file (`docs/secrets.md`).

First bring-up, after the token is in the master file: merge, `sudo bash
scripts/secrets/render-stack-env.sh`, then a deploy (CI on the next merge, or
`workflow_dispatch` on CI).

Verify, read-only:

```bash
docker logs --tail 20 reminder-bot-reminder-bot-1     # the polling log line, no error
docker exec reminder-bot-reminder-bot-1 python -c "import asyncio, os; from telegram import Bot; print(asyncio.run(Bot(os.environ['BOT_TOKEN']).get_me()).username)"   # getMe
```

The platform contract label is `none`: the bot has no HTTP surface to declare
health or metrics on, so liveness is the container's state and its log. The
database is on the volume only; there is no backup yet.

## Workout manager (wger)

Compose project `wger` (`compose/wger/`): [wger](https://github.com/wger-project/wger),
the self-hosted workout manager behind the owner's phone app (gym schedule,
exercise technique, sets checked off), with Kit setting the program through
its REST API. Five containers, all `lifekit.contract: none` (an upstream app):
`wger-nginx-1` (static, media and the proxy), `wger-web-1` (Django + gunicorn),
`wger-db-1` (Postgres), `wger-cache-1` (Redis, nothing to back up) and
`wger-powersync-1` (the app's offline-sync service, "PowerSync" below). Images
are pinned to exact versions; `scripts/check-wger-compose.py` fails the deploy
before `up` if one is not, if the database or media leave their named volumes
(`postgres_data`, `media`), if a port is not loopback-bound, if one of `web`'s four
secrets or the PowerSync storage password is empty once wger is meant to be up, if
`db` loses `wal_level=logical`, or if the network rules for Kit's route to wger or
for PowerSync's network break ("Kit and wger", "PowerSync" below).

**Setup.** The secrets (`WGER_SECRET_KEY`, `WGER_DB_PASSWORD`, the sign-in pair below and the PowerSync storage password, "PowerSync" below) are keys in the master
file with inventory rows in `docs/secrets.md`, so the next deploy renders them into the
env file and brings the project up; until `WGER_SECRET_KEY` and `WGER_DB_PASSWORD` are
present `deploy.sh` prints a warning and skips it. Values never in git. To rotate one, set a fresh value and redeploy (the
database password also needs the role changed, see the inventory row):

```bash
openssl rand -hex 32 | scripts/set-secret.sh master WGER_SECRET_KEY
openssl rand -hex 32 | scripts/set-secret.sh master WGER_DB_PASSWORD
```

**App sign-in keys.** The mobile app signs in with a token (allauth headless
JWT), signed with an RS256 keypair: `WGER_JWT_PRIVATE_KEY` and `WGER_JWT_PUBLIC_KEY`
in the master file (inventory rows in `docs/secrets.md`), passed to `web` as
`JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY`. Each is a single-line base64url JWK, so it
passes the env file render and compose interpolation unchanged. Without them the
app's login answers a server error (`Unable to load PEM file`) while the web
sign-in still works; with either empty, `check-wger-compose.py` fails the deploy.
The image's default keys are refused, so generate a fresh pair with wger's own
command and store both lines through `scripts/secrets/edit.sh master` (never
printed, never in git or a PR):

```bash
docker run --rm --entrypoint python3 -e DJANGO_DB_ENGINE=django.db.backends.sqlite3 \
  -e DJANGO_DB_DATABASE=/tmp/x -e SECRET_KEY=x -e DJANGO_DEBUG=True \
  wger/server:2.7.0 manage.py generate-jwt-keys    # JWT_PRIVATE_KEY= / JWT_PUBLIC_KEY= lines
```

**Rotating the pair signs the app out:** every issued token stops verifying, so
the app must log in again (the web session sign-in is unaffected). Always rotate
the two keys together, then render and redeploy.

The deploy derives `WGER_SITE_URL` (`https://<host>.<tailnet>.ts.net:8480`) from the
tailnet name; set `WGER_SITE_URL` in the env file to override. The database
password is set on the first boot of an empty volume only.

**Phone access.** nginx binds `127.0.0.1:8480` and nothing else; Tailscale Serve
publishes it on the tailnet name, tailnet-only, never Funnel (the same pattern
as the identity ports). The sign-in gate (`edge`) is not in front of it: the
wger mobile app cannot follow a browser sign-in, so wger's own login is the
gate and `ALLOW_REGISTRATION` is `False`.

```bash
sudo tailscale serve --bg --https=8480 http://127.0.0.1:8480
tailscale serve status        # 8480 -> 127.0.0.1:8480, "(tailnet only)"
```

In the wger mobile app, set the server address to
`https://<host>.<tailnet>.ts.net:8480` and log in with the owner account.

**Owner account and API token (operator; run by hand, values stay on the host).**

```bash
docker exec -it wger-web-1 python3 manage.py createsuperuser    # prompts for username and password
docker exec wger-web-1 python3 manage.py drf_create_token <username>    # prints the API token once
```

Store the token the way every other secret is stored (`docs/secrets.md`); it
never goes in git or a PR. Revoke by deleting the user's token in the Django
admin (`/admin/authtoken/tokenproxy/`) and run `drf_create_token` again.

**Exercise sync (first start, then whenever the catalogue should refresh).**
The exercise database, images and videos come from the upstream instance
(`https://wger.de`); the web container has egress for it. Wait until
`docker compose -p wger ps` shows `web` healthy (first start runs migrations
and takes minutes), then:

```bash
docker exec wger-web-1 python3 manage.py sync-exercises
docker exec wger-web-1 python3 manage.py download-exercise-images
docker exec wger-web-1 python3 manage.py download-exercise-videos
docker exec wger-web-1 python3 manage.py warmup-exercise-api-cache
```

The first run downloads a few GB into the `media` volume; it is safe to
re-run (it updates in place).

**REST API (what Kit calls).** Base path `https://<host>.<tailnet>.ts.net:8480/api/v2/`
(tailnet only), header `Authorization: Token <token>`. `GET /api/v2/version/`
needs no credentials and is the health probe.

**Update.** Change the pinned tags in `compose/wger/docker-compose.yml` in a
PR (wger first; read its release notes, minor bumps run migrations on start),
take a database dump first (below), merge; the deploy recreates the project.
Roll back by reverting the tag; a migration that already ran needs the dump
restored too.

**Backup and restore.** The Postgres database (accounts, routines, logged
sets) goes off-box nightly with the other databases: `wger.dump.age`
([Off-box backup (R2)](#off-box-backup-r2)). Media is not in that set: the
catalogue images and videos re-download with the exercise sync, and the
owner's uploads are not used. A manual dump and restore:

```bash
docker exec wger-db-1 sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > wger-$(date -u +%Y%m%d).dump
docker stop wger-nginx-1 wger-web-1
docker exec -i wger-db-1 sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < wger-<date>.dump
docker start wger-web-1 wger-nginx-1
```

From an off-box set, replace the redirect with `age -d -i $K set/wger.dump.age |`
as in the Off-box backup restore steps. Then run the exercise sync again if the
`media` volume is empty.

### PowerSync (the phone app's sync service)

The app signs in, then asks wger for a sync URL (`GET /api/v2/powersync-token`) and
syncs its offline copy through the separate PowerSync service. Without that service
the app stops on "Sync service unreachable". `powersync` is
`journeyapps/powersync-service` (pinned, `start -r unified`), configured with
upstream wger's own files in `compose/wger/powersync/` (`powersync.yaml`,
`sync_rules.yaml` unchanged; the service does not watch them, a change needs a
redeploy). How it hangs together:

- **Address.** wger returns `SITE_URL/ps/` (`POWERSYNC_URL_PATH=ps`, `POWERSYNC_URL`
  unset); nginx proxies `/ps/` to `powersync:8080`. So the app reaches it on the same
  tailnet-only address as wger (Tailscale Serve, never Funnel); no new published port.
- **Source.** wger's Postgres, over logical replication: `db` runs with
  `wal_level=logical` and the `powersync` publication comes from wger's own
  migration (core 0027). Sync buckets are stored in the `powersync` schema of the
  same database, owned by the role `powersync_storage` that `web` creates (wger's
  `setup-powersync-storage`, idempotent, run before the app starts on every boot)
  with the password `WGER_POWERSYNC_STORAGE_PASSWORD`.
- **Auth.** The service fetches wger's public JWKS (`web:8000/api/v2/powersync-keys`,
  the public half of `WGER_JWT_*`) to verify the app's tokens, so no extra key.
- **Networks.** `powersync` joins `backend` (db, web) and the internal `powersync`
  network it shares with nginx only; no `default`, so no egress. Read-only root,
  `cap_drop: ALL`, the same memory and pid limits as the rest.
- **Secret.** `WGER_POWERSYNC_STORAGE_PASSWORD` in the master file (inventory row in
  `docs/secrets.md`; `openssl rand -hex 32 | scripts/set-secret.sh master
  WGER_POWERSYNC_STORAGE_PASSWORD`). `check-wger-compose.py` fails the deploy while
  it is empty.

**Bring up.** Merge to `main`; the CI deploy does it all, with the secret in the
master file first (the deploy fails before `up` otherwise):

1. The deploy recreates `db` with `wal_level=logical` (a Postgres restart of a few
   seconds on the same named volume; the data is untouched; wger answers 502/503
   meanwhile), then `web` (runs `setup-powersync-storage`, then the usual start),
   then `powersync`, then `nginx`.
2. The deploy runs `scripts/check-wger-powersync.py` (below) and goes red if it fails.

Take the dump first ("Backup and restore") when you want a safety net; nothing in
this step changes existing tables.

**Verify.**

```bash
python3 scripts/check-wger-powersync.py http://127.0.0.1:8480 --wait 120
# ok (.../ps/ live and ready, keys served); checks GET /ps/probes/liveness,
# /ps/probes/readiness and /api/v2/powersync-keys through nginx
python3 scripts/check-wger-powersync.py https://<host>.<tailnet>.ts.net:8480    # the tailnet path the app uses
docker exec wger-db-1 psql -U wger -d wger -c 'show wal_level' -c 'select slot_name, active from pg_replication_slots'    # logical; a powersync_* slot, active
docker logs --tail 20 wger-powersync-1
```

Then open the wger app: it passes the "Sync service unreachable" screen and the
workout data loads. If the app still cannot reach it, run the check against the
tailnet address; a 502 there with the loopback check passing is Tailscale Serve
(`tailscale serve status` must show `8480 -> 127.0.0.1:8480`).

**Troubleshooting.** `/ps/probes/readiness` failing while liveness passes: read
`docker logs wger-powersync-1`; a storage-login error means the role and the env
password differ (`docker exec wger-web-1 python3 manage.py setup-powersync-storage`
resets the role to the password in the env file); a "publication does not exist"
error means wger's migrations have not run (wait for `web` healthy). After a
`WGER_SITE_URL` change the app needs to sign in again.

**Roll back.** A logical replication slot outlives its client, and an unread one
holds WAL on the volume without bound, so drop it before the revert lands:

```bash
docker stop wger-powersync-1    # an active slot cannot be dropped
docker exec wger-db-1 psql -U wger -d wger -c "select pg_drop_replication_slot(slot_name) from pg_replication_slots where slot_name like 'powersync%'"
```

Then revert the PR and let the deploy run: `powersync` disappears, nginx drops
`/ps/`, and `db` returns to the default WAL level (a few seconds of Postgres
restart). The `powersync_storage` role, the `powersync` schema and the
publication stay in the database and are harmless; to remove the first two, as
the database owner: `DROP SCHEMA powersync CASCADE; DROP ROLE powersync_storage;`
(the publication belongs to wger's migration; leave it). Nothing in the owner's workout data is
touched either way. Rotating `WGER_POWERSYNC_STORAGE_PASSWORD`: set the new value,
redeploy; `web` re-applies it to the role on start.

### Kit and wger

Kit's `workout-log` skill talks to wger with `wger.py` (`skills/workout-log/`): `routine`
writes Kit's `routines/gym.json` into wger as a routine, `logs` reads the workout logs back
(date range, exercise, sets, reps, weight), `nutrition`, `profile`, `log-write` and `log-delete`
fill the app and take a mirror back ("Fill the app" below), `check` proves the URL and the token. No new
service: the same skill, installed by `scripts/ensure-workout-log.sh`.

- **Base URL.** `WGER_BASE_URL` for the skill's run; the script's default is
  `http://wger-nginx-1:8080`, wger's nginx on its own network with the gateway. The owner's
  browser and phone use `https://<host>.<tailnet>.ts.net:8480`, the tailnet-only Tailscale
  Serve front (above); that URL is not reachable from inside the gateway container (checked
  2026-11-23: connection refused). So the external network `wger-gateway` (internal, no
  route off the box; `scripts/deploy.sh` creates it before either project's `up`) carries
  the call: `compose/wger/docker-compose.yml` puts nginx on it and
  `compose/openclaw/docker-compose.yml` puts the gateway on it, and nothing else joins it.
  No new published port, wger stays tailnet-only; `web`, `db` and `cache` stay on their own
  networks, and no wger service is on the wide `lifekit-shared` network. The deploy runs
  `scripts/check-wger-compose.py` before `up`: it fails the deploy if nginx is off
  `wger-gateway`, any other wger service is on it or on `lifekit-shared`, or the network
  stops being external. Docker's DNS answers the container name `wger-nginx-1` on every
  network the container is on (checked 2026-11-23 on a scratch internal network: the name
  resolved and `wger.py check` ran from a container that shared only that network). From the
  host, `WGER_BASE_URL=https://<host>.<tailnet>.ts.net:8480` also works and proves a token.
  Apply order after the PR deploys: the deploy creates the network, brings wger up (nginx
  joins), and recreates the gateway (it joins); until the gateway container is recreated the
  skill answers exit 4 "unreachable".
- **What was inferred about `routines/gym.json`.** The live file is host state this repo
  never held, so `wger.py routine` reads the shape from the notes about it, not from the
  file: `days` (a list, or an object keyed by weekday; `split`, `program`, `schedule` are
  accepted names) of `{day, name, exercises}`; each exercise `{name, sets, reps, weight}`
  with `reps` a number, `"8-12"` or `[8, 12]`, `weight` in kg and optional, and optional
  `aliases` and `wger_exercise` (a wger exercise id). Every other field (steps,
  progression and deload rules, cardio, notes) is ignored. If the real file differs in a
  way that matters (for instance exercises under another key), `routine` fails before
  writing anything and says what it did not find; adjust `load_program` in `wger.py`.
- **Where the token lives.** `WGER_API_TOKEN` is a key of the gateway secrets file
  `secrets/lifekit-gateway.env.sops` (inventory row in `docs/secrets.md`). The platform
  patch gives the skill an `apiKey` SecretRef to it (`skills.entries.workout-log.apiKey`,
  id `wger-api-token`, the `sops` provider); the skill's `primaryEnv` makes OpenClaw hand
  the value to the skill's run as `WGER_API_TOKEN`, and `wger.py` sends it only as
  `Authorization: Token <token>`. The deploy applies the patch and runs
  `openclaw secrets reload`; there is no env file and no plaintext copy on the host.
- **Apply on the host.** After the PR deploys, `scripts/ensure-workout-log.sh` (as `lifekit`)
  installs `wger.py` into kit's workspace and reports, read-only, whether the `apiKey`
  ref is wired. The token exists only inside a skill run, so verify by asking Kit to run
  `wger.py check` once (version, then an authenticated profile read).
- **Rotate** (on exposure, or after a revoke): mint a new token for the owner account and
  pipe it straight in; the value is never printed or kept in a file.

  ```bash
  docker exec wger-web-1 python3 manage.py drf_create_token --reset <owner username> \
    | grep -oE '[0-9a-f]{40}' | scripts/set-secret.sh gateway WGER_API_TOKEN
  ```

  Merge the PR it opens; the deploy's `openclaw secrets reload` picks the value up, with
  nothing to restart. Set the inventory row's "Last rotated" date in the same or the next PR.
- **Replacing a routine.** `wger.py routine --replace` builds the new routine in full first,
  and deletes the old one only after that succeeded; a failed build deletes the half-built
  routine and leaves the old one (exit 4). Deleting a routine in wger 2.7 also deletes the
  workout logs and sessions attached to it (`WorkoutLog.routine` and `WorkoutSession.routine`
  are `on_delete=CASCADE`), so `--replace` first asks wger whether the old routine has any
  and, if so, refuses with exit 1 and writes nothing: write the new one under another
  `--name`. A routine with no history is safe to replace.
- **How the writer was checked.** The payloads and query parameters in
  `scripts/tests/test_wger_skill.py` are validated against wger 2.7.0's own OpenAPI schema
  (`scripts/tests/fixtures/wger-openapi-2.7.0.json`, the part of the live instance's
  `/api/v2/schema` the client uses; regenerate it when wger is bumped), and one full write,
  a `--replace` over history, a rollback and the log read-back were rehearsed against a
  throwaway `wger/server:2.7.0` (own sqlite database, scratch network, no live volume,
  removed afterwards). wger drops unknown fields and query parameters silently, so the
  schema check is what showed that the `language` filter on exercise names did nothing
  (English is now matched on each row's `language`) and that wger's decimals are strings.
- **Fill the app: `nutrition`, `profile`, `log-write`.** The three commands that put the
  owner's numbers into wger so the app and Kit agree. All three take `--dry-run` (reads wger,
  writes nothing, says what it would do), all three are safe to run again, and none of them
  deletes anything (`log-delete`, below, is the one command that does).
  - `wger.py nutrition <plan.json>`: one nutrition plan, `{"name": "Cut 85 kg", "kcal": 2300,
    "protein": 190, "carbs": 220, "fat": 70}` (optional `fiber`, `start`, `end`; `start`
    defaults to today). The name is wger's plan `description` (80 characters at most) and is the
    identity: a plan with that name is created when absent, patched on only the goals that
    differ, and left alone when they match; two plans with the same name make it stop (exit 1)
    and ask for the extra to be deleted in the app. The goals live in the owner's health page;
    the file is built from them, never written into this repo.
  - `wger.py profile [--birthdate YYYY-MM-DD] [--height CM] [--weight KG] [--date YYYY-MM-DD]`:
    patches the profile fields wger 2.7 exposes (`birthdate`, `height`, 140-230 cm) when they
    differ, and adds a weight entry for `--date` (default today). A day has one entry: running it
    again with the same weight changes nothing, with another weight corrects that day's entry.
  - `wger.py log-write "<exercises>" [--date YYYY-MM-DD]`: the line Kit gave `workout-claw log`
    (`hack squat 3x10@60kg, bench press 8,8,6@60kg`; `@bw`, `lb` and uneven sets work) becomes
    one wger workout log per set. Each exercise is linked to the routine whose dates cover the
    day (the latest start wins) and its slot entry; a typed name resolves to the routine's own
    exercise when it matches that exercise's name or alias (so `biceps curl` logs to the
    routine's `Dumbbell Curl`, 1931, not wger's other curls) and falls back to the built-in
    alias map only when the routine has none; `--dry-run` prints the exercise id and whether it
    is a routine slot; an exercise that is not in the routine, or a
    day no routine covers, is still logged, without the link. It is idempotent per date and
    exercise: wger already holding identical sets prints `unchanged`; holding only the first sets of the
    line (a run cut short by a wger error) writes the rest, so sending the line again repairs it;
    holding anything else (other reps, weight or unit) exits 1 and writes nothing (correct those
    in the app). An exercise wger has no name for
    fails alone (exit 1, `wger_exercise` hint) while the others are written. The skill calls it
    only after `workout-claw summary` has shown the session; it never runs, reads or changes
    workout-claw, and a non-zero exit is reported in Kit's reply next to the "Logged" line.
  - `wger.py log-delete <YYYY-MM-DD> <exercise> [--dry-run]`: takes back a mirror (a wrong or
    test one): deletes that exercise's workout logs for the date, then each workout session that
    held one of those logs and is empty afterwards (a session still holding another exercise's
    logs, or one that held none of the deleted logs, such as an app-made session with notes,
    stays), and prints what it removed; run again it says `nothing to remove`. `--dry-run` only
    says what it would remove. `bicep(s) curl` is a built-in spelling of wger's `Dumbbell Curl`
    (1931, the plain curl; another curl needs `wger_exercise` or an alias in `gym.json`).
    The two DELETE endpoints are not in the OpenAPI fixture wger generated, so the fixture
    carries them by hand until it is regenerated.
  - Failures: a value wger refuses (HTTP 400, for instance a height outside its limits) is exit 4
    with wger's message; exit 2, 3 and 4 otherwise mean what they mean for every command (no
    token, token rejected, wger unreachable). The payloads are checked against the schema
    fixture, which has carried the nutrition plan, weight entry and profile PATCH endpoints
    since 2026-11-25, so a wger bump that renames a field fails the test, not the owner's
    session.
- **When the token is revoked** (deleted or reset in wger's admin): every `wger.py` call
  exits 3 with `wger rejected the token (HTTP <code>): it is revoked or wrong` (wger 2.7.0 sends 403, other versions 401); Kit says so,
  stops, and nothing is written. Rotate as above. Exit 2 (`WGER_API_TOKEN is not set`)
  means the value never reached the run: check the `apiKey` ref with
  `scripts/ensure-workout-log.sh` and that the deploy's `openclaw secrets reload` ran.
- **Not covered here:** the wger deploy, the owner account, exposure beyond the tailnet.

## Study panel and study door

The browser study panel sits beside a problem page (LeetCode and the like) and
asks the owner one small question at a time. It has two server-side pieces:

- **`study-panel`**, an OpenClaw agent with no tool at all
  (`defaults/agents/study-panel/`): `tools.allow` is empty and the deny list
  covers files, web, sessions, memory, messaging, nodes, agents, exec and the
  MCP servers, and ends in a `*` deny (an empty allow-list alone still leaves the
  gateway's own `openclaw` MCP tools on a local turn); it has no skills and no channel. It never writes the owner's
  code and offers no solution. It answers with exactly one JSON object as text.
- **The door** (`compose/study-door/`, compose project `study-door`, container
  `study-door-study-door-1`, port 8098): one `POST /v1/turn` in, one gated reply
  out. It asks the gateway's OpenAI-compatible API (`/v1/chat/completions`,
  model `openclaw/study-panel`, no tools, `tool_choice: "none"`), parses the
  answer as one JSON object (prose around it, a code fence or a second object is
  a reject), then checks it before the panel sees it: a closed
  schema (no code field, 280 characters of text, at most four tap options), a
  code gate (a fenced block, `<pre>`, a statement shape, or a 12-token run
  copied from the owner's code, or an answer that is not one JSON object,
  becomes a fixed fallback reply), and an HTML
  sanitiser for the small SVG/HTML visual (no script, link, image, form,
  frame, event handler or external `url()`). The door never logs request or
  reply bodies: a turn logs sizes, a hash, the outcome and the reject reason.
  Raw model replies (never the page or the owner's request) go to an audit
  spool (`study-door-spool` volume, mode 700/600) so a rejected answer can be
  read. A rejected reply (`copies_owner_code`, `fenced_code`) can itself hold
  owner code or a solution, which is why the spool stays private.

Path: extension → `https://<host>.<tailnet>.ts.net:8098` (Tailscale Serve,
tailnet only) → edge entrypoint `study-door`, `127.0.0.1:18895`
(`compose/edge/traefik/study-door.yml`, `POST`/`OPTIONS` on `/v1/turn` only,
every other path a 404) → the door → the edge's internal entrypoint
(`openclaw-edge:18893`) with `STUDY_DOOR_EDGE_SECRET` as the bearer → the
gateway, as the fixed identity `service:study-door` capped at `operator.write`.
The door checks its own bearer (`STUDY_DOOR_TOKEN`), the extension's Origin and
a per-minute rate limit (`STUDY_DOOR_RATE_PER_MIN`, default 20); the panel
cannot name an agent, a model, a tool or a session.

**Off until the owner turns it on.** Merging changes nothing for the door:
`deploy.sh` brings the project up only once `STUDY_DOOR_TOKEN` and
`STUDY_DOOR_EDGE_SECRET` are both in the rendered env file, and warns and
skips otherwise (the wger pattern). The agent entry is host state and the
deploy only reports it missing (`scripts/ensure-study-panel.sh`). The edge's
new route, entrypoint and published loopback port do go live on the merge
deploy (the edge is recreated); with the door off they answer 502 and nothing
else changes. Prometheus scrapes the door through a `file_sd` targets file the
deploy writes only while the door runs, so an absent door never fires
`target-down`.

**Operator steps (once; commands only, values never in git).** On the VPS as
`lifekit` in `/srv/lifekit-stack`, gateway container
`openclaw-openclaw-gateway-1`. **Prerequisite: the OpenClaw trusted-proxy
cutover has run** ("OpenClaw behind the sign-in gate",
`scripts/openclaw-sso.sh`). The door's bearer is `STUDY_DOOR_EDGE_SECRET`, not
the gateway token, so a gateway still in token mode answers 401 and the door
stays not ready; step 3 also needs `gateway.auth.trustedProxy.allowUsers` to
exist.

1. Prove the cap on this box. It must end with `study-panel canary passed`
   (needs Docker and the `lifekit-openclaw:local` image; exit 77 means neither,
   which is a skip, not a pass):

   ```bash
   CANARY_OUT=/tmp/study-panel-canary scripts/study-panel-canary/run.sh
   ```

   Stage one is a real local turn against a mock model that obeys an injected
   page and calls every tool it knows: the model must be offered no tool and
   every denied call must be refused with no effect. Stage two sends the door's
   exact request (no tools, `tool_choice: "none"`) to the gateway's
   `/v1/chat/completions` and requires `finish_reason: "stop"`, message content
   that parses to the mock's reply object, and an empty tool catalogue offered to
   the model. If it fails, read `$CANARY_OUT` and do not go on.

2. Apply the agent entry (host state, merged into the live config), then read
   the apply hint it prints; restart the gateway only if it says the keys need it:

   ```bash
   docker exec -i openclaw-openclaw-gateway-1 openclaw config patch --stdin < defaults/agents/study-panel/agent.json
   scripts/ensure-study-panel.sh        # installs the contract; ends "study-panel agent verified"
   ```

3. Let the gateway accept the door's service identity (`allowUsers` replaces as
   an array, so append to the live list):

   ```bash
   GW=openclaw-openclaw-gateway-1
   docker exec "$GW" openclaw config get gateway.auth.trustedProxy.allowUsers --json \
     | python3 -c 'import json,sys; t=sys.stdin.read(); a=json.loads(t[t.find("["):]); print(json.dumps({"gateway": {"auth": {"trustedProxy": {"allowUsers": a + ([] if "service:study-door" in a else ["service:study-door"])}}}}))' \
     | docker exec -i "$GW" openclaw config patch --stdin
   ```

   A fresh `scripts/openclaw-sso.sh` run writes the same list.

4. Mint the two secrets (add the two keys to the master file and their rows to
   `docs/secrets.md` first; `set-secret.sh` refuses a name that is not already a
   key), render, and redeploy so the edge reads `STUDY_DOOR_EDGE_SECRET` and the
   door comes up ([`docs/secrets-runbook.md`](./secrets-runbook.md)):

   ```bash
   scripts/secrets/edit.sh master
   openssl rand -hex 32 | scripts/set-secret.sh master STUDY_DOOR_TOKEN
   openssl rand -hex 32 | scripts/set-secret.sh master STUDY_DOOR_EDGE_SECRET
   ```

   `STUDY_DOOR_TOKEN` is the panel's bearer: paste it once into the extension's
   options (read it on the host with `sed -n 's/^STUDY_DOOR_TOKEN=//p'
   /srv/lifekit-secrets/stack.env`); it goes nowhere else. Set
   `STUDY_DOOR_ORIGIN=chrome-extension://<id>` in the env file once the
   extension is loaded; until then every browser origin is refused (a `curl` with
   the bearer still works). Rotate either secret by minting it again and
   redeploying; a rotated `STUDY_DOOR_TOKEN` needs the new value in the panel.

5. Publish it on the tailnet (tailnet only, never Funnel):

   ```bash
   sudo tailscale serve --bg --https=8098 http://127.0.0.1:18895
   tailscale serve status        # 8098 -> 127.0.0.1:18895, "(tailnet only)"
   ```

6. Check it end to end, from the host (the bearer comes from the env file; the
   body is a one-turn page):

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<host>.<tailnet>.ts.net:8098/v1/turn   # 401
   docker compose -p study-door ps                       # study-door healthy
   docker logs study-door-study-door-1 --tail 5          # one JSON line per turn, no bodies
   ```

**Take it away.** Unset the two secrets and redeploy (the deploy stops warning
only when both are present; remove the project with `docker compose -p
study-door down`), `sudo tailscale serve --https=8098 off`, and
`openclaw agents delete study-panel`.

**What it does not do.** It does not read the page itself: the extension sends
the page text, and the spike that proves it can read a problem page on the
owner's PC is a separate piece. The door's rejects are visible only as the
`study_door_events_total{kind="reject"}` counter and the audit spool; there is
no alert on them yet.

## Ledger persona from finance-sentry

finance-sentry's `agent/ledger/` is the source of truth for the finance agent's
persona (its README, "Composition model"): the OpenClaw persona is
`persona.core.md` + `adapters/openclaw.md`. `scripts/ledger-persona/ledger-persona.py`
fetches those two files at the commit in `scripts/ledger-persona/PIN` (read-only,
through `gh api` on the contents API with gh's existing login; no secret is
added), joins them with a `---` rule, and writes the result to the finance
agent's workspace `AGENTS.md`. Bump the pin by a PR that edits `PIN`.

Ledger runs the thesis loop itself: finance-sentry emits `ThesisReviewDue` and
`CandidateReviewDue` companion events, the pulse (`scripts/finance-pulse.md`, step 1)
hands them to the persona's review loop, and Ledger's service token writes theses
(audited) for those review-loop revisions. Ledger never promotes a candidate itself:
promotion is the owner's, filed as a proposal through P2. A thesis revision that
comes from a coverage memo or news (step 7) is proposed through P2 the same way; Ledger
does not ask the owner in a chat message to confirm either. Step 7 reads
`list_coverage` and new memos once a day, on the first run (the 09:20 slot, same
gate as the Sunday digest), and stays silent when nothing changed.

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
19,800 characters at the 2026-11-17 pin, it was about 23,300 before the trim). Only `AGENTS.md` is
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
of another agent's persona; tell the owner, and relay the ask to firstmate as a kit-relay
note if the owner wants the change made in finance-sentry `agent/ledger`), and this timer
is the backstop that notices when a live edit happens anyway. The file is not locked read-only: that would need a
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
  `AGENTS.md` and `SOUL.md` persona backups (`SOUL.md.bak-*`), OpenClaw's own
  state, inbound chat media, the vault daily notes `memory/YYYY-MM-DD.md`,
  installed `skills/`, and Ledger's MCP helper scripts. It prints one `<area>: <path>` line per hit and exits 1 (0
  clean, 2 when it cannot run). It never deletes or moves a file.
- **The report.** `lifekit-ledger-drift.timer` runs it every 5 minutes as the
  admin account with `--textfile`, which writes
  `ledger_finance_drift_file{area,path}` (1 per hit) and
  `ledger_finance_drift_files` into the textfile directory
  (`ledger_finance_drift.prom`). The *Ledger finance drift* rule
  (`ledger-finance-drift`, `rules.yml`) fires warning per file, which the
  notification policy mutes off the owner's phone, and reaches main through
  `alert-inbox` (`scripts/alert-inbox/poll_alerts.py`, one "alert firing" note
  per file). A run that cannot finish leaves the file untouched, so *host
  systemd unit is down or failed* and then *textfile metrics are stale* report
  it instead.

By hand, on the box as the admin account (read-only):

```bash
bash /srv/lifekit-stack/scripts/ledger-drift/lifekit-ledger-drift.sh; echo "exit=$?"
```

When it alerts: read the file. Anything still true goes into finance-sentry as
a proposal for Ledger to act on (a thesis or candidate is Ledger's own write, not an owner confirm); then retire the file by hand (the check never
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

## OpenClaw auth mode (API key or subscription)

OpenClaw reaches Claude in one of two modes, and `scripts/openclaw-auth/auth-mode.sh`
is the one switch between them:

| Mode | The `anthropic/*` models run on | Billed to | Auth |
| --- | --- | --- | --- |
| `api` | the direct runtime (`agentRuntime.id: openclaw`) | the prepaid Console credit | profile `anthropic:api-key`, a SecretRef to `ANTHROPIC_API_KEY` (`auth.order.anthropic` is exactly that profile) |
| `subscription` (the default) | the Claude Code runtime (`claude-cli`): OpenClaw launches the unmodified `claude` binary | the shared Claude account quota | the binary's own native login; `auth.order.anthropic` is empty |

The Console credit is not valid for Claude Code, so credit is only spent on
the direct runtime: `api` mode must change the runtime, not just the profile.
The `anthropic:setup-token` profile is **never** the fallback: OpenClaw's own
client presenting a subscription OAuth token is the use Anthropic's terms
exclude (2026-10-31 analysis), so the fallback from the API key is the switch
to the `claude-cli` runtime.

```bash
cd /srv/lifekit-stack/scripts/openclaw-auth
bash auth-mode.sh status            # mode, runtimes, auth.order, key profile per agent, live key probe, spend
bash auth-mode.sh api               # key -> credit (refuses an unusable key or spent credit)
bash auth-mode.sh subscription      # back to the Claude Code runtime
bash auth-mode.sh api --dry-run     # print and validate the config patch, change nothing
```

Run it as any account in the `docker` group (`offline-profiles`, below, is the exception); it drives the gateway through
`docker exec` and the `openclaw` CLI, never reading `openclaw.json` or a secret
file. Every OpenClaw CLI call takes ~20 s, so a status is under a minute and a
switch a few minutes.

**What a switch does.** One `openclaw config patch` that sets the runtime of
every `anthropic/*` model the agents use (`agents.defaults.models` and each
agent's own `models` map) and `auth.order.anthropic`; the gateway is restarted
only when the apply hint says the changed keys need it, otherwise it
hot-reloads. It then runs a real throwaway turn per agent in `AUTH_CHECK_AGENTS`
(default `kit`; set `AUTH_CHECK_AGENTS=kit,finance,career` for the lot; fresh session
`agent:<id>:auth-check-<time>` each run, nothing delivered) and requires it to be answered by
the new mode's provider (`anthropic` for `api`, `claude-cli` for
`subscription`). The check turns run with `--thinking low`: `off` is rejected by
some models on the direct runtime (`claude-opus-5-5`), and `low` is accepted by
every model in both modes. If the patch, the restart or a turn fails, the previous values
are patched back and the command exits 1. The rollback sets every key the switch
changed back to its exact old value and **deletes** (null) any key or whole
profile that did not exist before (for `api`, the `anthropic:api-key` profile),
so it validates against the schema; a rollback that still fails prints
`ROLLBACK FAILED`. Switching to the mode already active
changes nothing and only verifies. `api` also adds the `anthropic:api-key`
SecretRef profile (`secrets apply`, id `anthropic-api-key`, no value involved)
to every agent that lacks it.

**The key profile needs the gateway stopped (OpenClaw 2026.11.4).** OpenClaw
2026.11.4 lets a running gateway own its state database, and `openclaw secrets
apply` (the only way to store the SOPS exec SecretRef) has no gateway route, so
it answers "OpenClaw state database is busy" while the gateway is up (`secrets
store` and `models auth paste-api-key` do route through it, but they keep the
value in the database and cannot take our SecretRef). When `api` or
`ensure-profiles` meets that refusal it changes nothing, says so, and prints the
one command for the offline step. A **host operator** runs it (it stops the
gateway; an agent never does), as `lifekit` (or root): the compose helper reads
`openclaw.json`, which other accounts cannot, and the script stops with that
hint before touching the gateway when it cannot:

```bash
sudo -u lifekit bash /srv/lifekit-stack/scripts/openclaw-auth/auth-mode.sh offline-profiles
```

It stops `openclaw-cli` and `openclaw-gateway` through the deploy's
`openclaw_compose`, runs `openclaw secrets apply --allow-exec` (dry run first) as
a `run --rm --no-deps -T --entrypoint openclaw` one-shot with the plan, starts
the gateway again on every exit path (a failed apply included), waits for it to
be healthy and runs `secrets reload`, retrying it once after waiting for health
again if the gateway is not ready for it yet. Then re-run `auth-mode.sh api`, which finds
the profiles in place and goes on to the mode switch and the per-agent turns.
Expect the assistants to be down for the length of that one-shot.

**Putting a key in place (captain).** Create a Console key, then run one command
from your PC (the operator key lives there): `scripts/set-secret.sh gateway
ANTHROPIC_API_KEY`. It asks for the value with a hidden prompt (or reads it from
stdin), swaps it into the SOPS file without any plaintext touching disk or argv,
runs the secrets tests, opens the PR and enables merge on green. The OpenClaw
deploy after the merge runs `openclaw secrets reload`; then check it:

```bash
bash auth-mode.sh probe     # one JSON line: ok | rejected | exhausted | limited | unknown | no-key
```

`probe` resolves the SecretRef inside the gateway and sends a 1-token request
to the Messages API (`openclaw models status --probe` refuses to run while the
gateway is up); it prints the verdict only, never the key. `rejected` is an
invalid or revoked key (the state on 2026-11-14, when the 2026-10 key still in
the file answered 401, so the box stays in `subscription` mode until a new key
is placed), `exhausted` a key with no credit left.

### `auth-unknown` on every claude-cli turn is expected

In `subscription` mode the gateway log carries "cli session history refused
across auth boundary: reason=auth-unknown" on every `claude-cli` turn (89 of 89
turns in the #529 scout). The runtime has no account identity to bind the CLI
chat history to, so it never reuses that history; it is not a credential
change and not a tool-authorization failure, and the turn still answers.

- **Do nothing about it.** It is not an alert condition and not a reason to
  roll back an auth-mode switch.
- **Do not drop `sessions_history`** to silence it: the tool is unrelated to
  the refusal, and the line stays either way.
- The fresh per-run session ids in the deploy smoke and `auth-mode.sh` checks
  keep those turns out of the agents' real sessions; they do not avoid this
  line.

### Spend guard

OpenClaw's usage records carry tokens, not dollars, and only turns answered by
the direct runtime (provider `anthropic`) are billed to the credit. The guard
(`scripts/openclaw-auth/spend_guard.py`, run every 10 minutes by
`lifekit-openclaw-spend-guard.timer`) lists those turns from every agent's
transcript store inside the gateway, prices them, and keeps a host-side ledger
in `/var/lib/lifekit-openclaw-spend/` so the total survives OpenClaw pruning
old sessions.

- **Prices** live in `scripts/openclaw-auth/prices.json`: list USD per million
  tokens (Sonnet 3/15, Opus 5/25, Haiku 4.x 1/5; cache write 1.25x, cache read
  0.1x of the input price). A model with no row is priced at the dearest tier
  and shows in `openclaw_api_spend_unpriced_tokens`: add a row when that is
  above 0. The figure is an estimate (list prices, 5-minute cache assumed),
  not an invoice.
- **The credit** lives in `scripts/openclaw-auth/credit.json`: `credit_usd`,
  `since` (UTC) and `fallback_percent` (95). **A later top-up is a one-line
  edit**: add the amount to `credit_usd` and leave `since`; for a fresh credit
  period set both. Merge it like any change; the guard reads the deployed
  checkout.
- **Metrics** (`openclaw_api_spend.prom`): `openclaw_api_spend_usd{window}`
  (`since_topup`, `month`), `openclaw_api_credit_usd`,
  `openclaw_api_credit_used_percent`, `openclaw_api_auth_mode{mode}` (`mode="unreadable"`
  when the live config could not be read after `auth-mode.sh`'s bounded retries:
  the file stays fresh and the unit exits 2),
  `openclaw_api_key_billing_disabled` (absent then), `openclaw_api_key_probe_ok` (api mode
  only), `openclaw_api_fallback_active{reason}`,
  `openclaw_api_spend_unpriced_tokens`.
- **What fails the unit, and what does not.** An `auth` path OpenClaw answers as
  "valid but unset ... the runtime default applies" is a valid read: `auth-mode.sh`
  reads it as `{}`, resolves the mode from the agents' runtimes, says so in one stderr
  line (`status --json` carries `authSource: "default"`) and the guard logs "auth config
  unset, the runtime default applies; mode <m>" - exit 0. A gateway that is restarting
  (container missing, or `docker exec` refused with "Error response from daemon") makes
  `auth-mode.sh` exit 3; the guard then leaves `openclaw_api_spend.prom` untouched (its
  mtime stays honest for `textfile-collector-stale`), prints one journal line and exits
  0 while that file is younger than 30 minutes (`OPENCLAW_SPEND_STALE_SECONDS`, three
  passes). A gateway down longer than that, no metric file at all, or any real read
  error (a config read that still fails after the bounded retries) exits 2 and fails the unit.
- **Alerts** (Grafana group `openclaw`, existing Telegram contact point; only
  `critical` pages the phone, the rest reach the fleet inbox):
  `openclaw-api-credit-50` (warning), `openclaw-api-credit-75` and
  `openclaw-api-credit-90` (critical), exclusive bands so each crossing speaks
  once; and `openclaw-api-fallback-engaged` (critical), which names the reason.
- **Automatic fallback.** In `api` mode each pass runs the key probe and checks
  the per-agent profile state, then runs `auth-mode.sh subscription` itself
  when spend reached `fallback_percent`, when OpenClaw disabled
  `anthropic:api-key` for billing, or when the probe says the key is rejected
  or out of credit. It records why in `fallback.json` (the alert's `reason`:
  `spend`, `billing`, `key-refused`) until it next sees `api` mode, so the
  alert clears once the operator switches back. If the flip fails (rolled
  back), the pass exits 1 and `host-unit-down-or-failed` reports the timer.
  `auth-mode.sh api` refuses while the estimate is past the threshold unless
  `--force`.
- **Installing the timer** is a host step, like the other gauges (the deploy
  does not install units): `sudo bash /srv/lifekit-stack/scripts/host-gauge/install-host-gauges.sh`.
  Check it with `systemctl list-timers 'lifekit-openclaw-spend-guard*'` and
  `grep -v '^#' /var/lib/node_exporter/textfile/openclaw_api_spend.prom`.
  `bash auth-mode.sh status` prints the same estimate on demand.
- **Undo:** `sudo systemctl disable --now lifekit-openclaw-spend-guard.timer && sudo rm
  /etc/systemd/system/lifekit-openclaw-spend-guard.{service,timer}
  /usr/local/bin/lifekit-openclaw-spend-guard.sh
  /var/lib/node_exporter/textfile/openclaw_api_spend.prom`. Without the guard
  `api` mode has no automatic fallback; run `auth-mode.sh subscription` by hand.

Spend before the guard's first pass is only what OpenClaw still holds in its
transcript stores (the first pass backfills from `since`); install the timer
before the first `api` switch.

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
  and only for `youtube.com`, `googlevideo.com`, `ytimg.com` or, for the queued video evaluations
  ("Video evaluation"), `instagram.com`, `cdninstagram.com` and `fbcdn.net`, or a
  subdomain of one of them (label-boundary match, case-insensitive). Everything else (other
  names, lookalikes such as `evilyoutube.com` or `youtube.com.evil.com`, IPv4/IPv6 address
  types, IP literals sent as names) gets SOCKS reply `0x02` and a log line in `journalctl --user -u yt-relay`.
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
  exit 1 with that line on stderr, except a 429 or bot check from YouTube, which is
  `transcript unavailable - youtube is blocking this route` (exit 0; no retry, no
  cookies). Likewise only a relay-down or SOCKS `0x05`
  (upstream unreachable) failure reads as the PC-off line; an allow-list refusal
  (`0x02`) or other SOCKS error is exit 1 with the real reason.
- **Captions only:** human captions first (`YT_TRANSCRIPT_LANGS`, default `en.*`), then
  the video's original-language auto-generated track (`<lang>-orig`), VTT cleaned to
  plain text with a `Language:` line. A video with auto-dubbed audio lists one `-orig`
  track per audio language, so the auto pass asks only for the spoken language's (the
  audio language yt-dlp reports, `en-US` matching `en-orig`) and English's, and returns
  the spoken one, else English; when that request downloads nothing it asks once more for
  every `-orig` track and takes a lone one whatever its language, and with several and
  neither it fails rather than return an arbitrary one. YouTube's machine-translated auto tracks are never
  requested: they 429 on this route and `en.*` would match them. A video with no caption
  track answers `transcript unavailable - this video has no captions`; there is no audio
  fallback.
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
that changes it (its allow-list, say) needs `systemctl --user restart yt-relay.service`.
The shared job queue's default egress is this relay ("Shared job queue").

The deploy installs the skill: the OpenClaw post-up phase runs
`scripts/ensure-youtube-transcript.sh`, and a change to `skills/youtube-transcript/` or to
that script triggers the OpenClaw phases (`scripts/lib/openclaw-paths.sh`), so a merge
that changes either lands on the host with no manual step. A failed install turns the
deploy red and the next deploy retries it. To run it outside a deploy, as `lifekit` on the
host:

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
script (the sum is the release's `SHA2-256SUMS` line for `yt-dlp`) and merging it;
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
dumps go off-box nightly, encrypted, with the backstage, basket and wger databases:
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
cat /srv/memory/.sync-failed       # line 1: time, step=<lock|commit|lint|fetch|rebase|checkout|push>, exit code; then git's last 5 stderr lines
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
- `step=lock`: `bin/log.sh` (or another holder of `.git/vault.lock`) kept the vault
  lock for over 120 s; the tick takes the same flock for its whole body so the two
  never race for git's index lock. The next tick retries.
- `step=lint`: `bin/lint.sh` itself failed without naming a file; run
  `sh bin/lint.sh --changed` in the vault to see why.
- `step=fetch` / `step=push`: network or credentials; git's error is in the
  last lines of `.sync-failed` (credentials redacted) and in the journal.

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
vault audit, `lifekit-vault-audit.timer`); nothing here edits `/srv/memory`. The closed
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
copies the six stores that had no other copy to Cloudflare R2 each night,
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
| `wger.dump.age` | wger's Postgres (accounts, routines, logged sets) | `pg_dump -Fc` through `docker exec` into compose project `wger`'s `db`; skipped (and recorded) where wger is not deployed |
| `basket.dump.age` | basket's Postgres (the shared list, passkey accounts) | `pg_dump -Fc` through `docker exec` into compose project `basket`'s `db`; skipped (and recorded) where basket is not deployed |
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
`pg_restore --list` on the backstage, basket and wger dumps, each in a throwaway
`--network none` container, a tar listing of the state with `openclaw.json` in it, `PRAGMA
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

- Basket, into a running `basket-db` (stop the app first, start it after):

  ```bash
  docker stop basket-app
  age -d -i $K set/basket.dump.age | docker exec -i basket-db \
    sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner'
  docker start basket-app
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

Grafana's alerts route to Telegram only (the phone, for `severity: critical`), so nothing reaches an agent unless
something pulls. `scripts/alert-inbox/poll_alerts.py` (see
`scripts/alert-inbox/README.md`) reads the provisioned alert rules' own
queries from `rules.yml` (PromQL) and `rules-agent-mail.yml` (LogQL) and
evaluates them against Prometheus's and Loki's open, loopback-bound query
APIs — no Grafana admin credentials, no new listener. State kept for a rule
uid that is no longer provisioned is dropped on the next run.
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
Prometheus or Loki outage is its own transition so it is reported once, not every poll. Does not touch Grafana contact
points, policies, or credentials.

## Ops door

The ops queue is this repo's issues, and its door is the alert itself
(decision 0016, issue #543). Grafana's webhook contact point `ops-door`
(`compose/observability/grafana/provisioning/alerting/contact-points.yml.tmpl`)
POSTs every alert notice, any severity, to the `ops-door` compose service
(`compose/ops-door/`, `:8097`, internal-only, 128m), which files or updates a
GitHub issue in `lifekit-hq/lifekit-stack`. The envelope on each issue and the
receipt in each reply are the build-standards entry "Envelope and receipt at
every door" (vault, 2026-11-16); the receiver enforces it where the issue is
made and this section does not restate it. The route in `policies.yml.tmpl`
is `continue: true`, so Telegram delivery for incidents is unchanged, and it
sits after the external-heartbeat route, so the dead-man alert is never filed.

What it does, per alert fingerprint (alert name plus identifying labels;
`severity` and `grafana_folder` are left out so a re-grading does not fork an
incident):

- **First firing** opens one issue labelled `ops` and returns the receipt.
- **A repeat** on the open issue adds a count and the latest occurrence (the
  count and the other state live in a marker inside the issue body, so the
  receiver keeps none of its own) and one comment, with the same receipt.
- **Past the threshold** (`OPS_DOOR_REPEAT_THRESHOLD`, default 3 notices) the
  next repeat files exactly one Tier 2 call instead of another comment: the
  receiver spools `ops-repeat-<fingerprint>-<issue>.json`, the host timer
  `ops-door-escalate` runs `fleet-policy decide` for it, and the issue records
  the call id, so later repeats only raise the count.
- **A resolve** comments and leaves closing to the merged fix.
- **A firing after close** opens a new issue that links the closed one.

Because Grafana re-notifies a still-firing group every 4h, a fix that did not
close an alert reaches the threshold after about 12h without anyone resending.

**Host steps (once, in this order).** The deploy goes red at "ops door:
receiver live" until the first is done.

1. Mint the token: GitHub, Settings, Developer settings, fine-grained token,
   resource owner `lifekit-hq`, repository `lifekit-hq/lifekit-stack` only,
   permission Issues read/write and nothing else, the longest expiry offered.
   Store it without printing it or writing it to a file, as
   `OPS_DOOR_GITHUB_TOKEN` in the master file ("The four moves" in
   `docs/secrets-runbook.md`, the single-value `sops set` form), merge, render
   `stack.env`, redeploy. Add its row to the master table in `docs/secrets.md` in the
   same change that sets the value (`scripts/tests/test_secrets.py` fails when the
   inventory and the file disagree, so the row and the value land together, not
   before). The deploy recreates `ops-door` and its `/ready`
   proves GitHub accepts the token. Create the `ops` label once (`gh label
   create ops --repo lifekit-hq/lifekit-stack`); the receiver lists issues by it.
2. As root on the host: `sudo bash /srv/lifekit-stack/scripts/ops-door/install-ops-door.sh`
   creates the spool `/var/lib/lifekit/ops-door/escalations` (the container
   writes, the operator account owns it) and installs the `ops-door-escalate`
   timer. The deploy reports its state with `install-ops-door.sh --check`.
3. Grafana reads provisioning at startup; the deploy's provisioning reload
   picks up the new contact point and route on the next deploy.

Check it: `docker exec compose-ops-door-1 node -e "fetch('http://127.0.0.1:8097/ready').then(r=>console.log(r.status))"`
prints 200; `curl`-style test notices go to `POST /alerts` from inside the
compose network with an Alertmanager payload; `systemctl list-timers
ops-door-escalate.timer` and `journalctl -u ops-door-escalate` show the drain.
A token that expires or is revoked turns `/ready` red, which turns the next
deploy red and shows as a failing `ops-door` target; rotate it with the secrets
runbook. Logs are JSON with a trace id; `/metrics` carries
`ops_door_events_total{outcome=...}`.

## Alert remedies

What the agent that receives an `alert-inbox` note (above) may do about each
provisioned rule without asking, and when it reports up. Every row may always
run read-only diagnosis — the rule's own description commands, `docker
logs/inspect/stats`, `journalctl`, `systemctl status` — at no cost.

**Production test for containers.** A container is production only if its
compose project label is one of `compose openclaw identity edge reminder-bot finance-sentry
dashboard backstage basket devclaw xui` **and** its name starts with that project:

```bash
docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}} {{.Name}}' <container>
```

Everything else is scratch (a throwaway stack from a worker; two scratch
replicas carry `project=openclaw`, so only the name test catches them).

**The tiers.** *Unattended* is the remedy the agent applies itself, then runs
the verification named after it. *Routed instruction* is a remedy that waits
for an instruction naming it. `ISSUE` means file an issue (or a PR) in the
owning repo rather than act on the box.

**Report up** means a note to the agent or person that owns the fleet, with
the rule, labels, what was done and the verification result:

- **R1:** not cleared 30 minutes after the remedy.
- **R2:** the same rule and labels fired 3 or more times in 7 days.
- **R3:** any action taken on a production container.
- **R4:** an incident: a production service down past 15 minutes, money
  moving, a security event, or a dead credential stopping automation. Relay at once.

| Rule (severity) | Unattended (verify after) | Routed instruction | Report up |
| --- | --- | --- | --- |
| `target-down` (critical) | If a CI deploy run is in progress (`gh-axi run list -R lifekit-hq/lifekit-stack --status in_progress --limit 3`) wait for it to end; else `docker ps -a --filter name=<job>`, map the job to its container, read its logs. Verify `up{job}` = 1 | Any start, restart or recreate | Still down 15 min with no deploy running: R4 |
| `container-exited-abnormally` (critical) | Scratch: remove it (`docker rm -f <c>`, never `-v`) or have its owner remove it. Production: `docker logs --tail 100 <c>` into the report | Production: `docker start <c>` | Production: R3 at once. Scratch: R2 |
| `container-stopped-or-vanished` (critical) | Compare `git log --since=24h` of the owning repo: a container retired or renamed by a merge is log-only (the rule clears itself in about 30 min) | Bringing a production service back | Not explained by a merge: R3 |
| `container-restarting` (critical) | Scratch: `docker stop <c>`. Production: `docker logs --tail 50 <c>` | Any intervention on a production container | Production looping 15 min: R4 |
| `container-unhealthy` (critical) | Scratch: `docker stop <c>`. Production: `docker inspect --format '{{json .State.Health}}' <c>` | Production restart | Production unhealthy 15 min: R3, and R4 if it is down |
| `docker-exporter-scrape-errors` (critical) | `docker logs compose-container-exporter-1 --tail 50`; treat the container rules above and `container-oom-killed` / `container-near-memory-cap` as blind until it clears | Recreating the exporter outside a deploy | R1 |
| `container-oom-killed` (critical) | Scratch: find the owner, have it cap memory (`--memory 2g`, single-node builds) and remove the dead container. Production: `docker inspect -f '{{.State.OOMKilled}} {{.State.ExitCode}}' <c>` and `docker logs --tail 100 <c>`, then ISSUE raising `mem_limit` (the deploy recreates it) | Live `docker update --memory` | Production: R3 at once. Scratch: R2 |
| `root-filesystem-low-space` (warning) | `df -h /`, `docker system df`; delete backed-up leftovers (the prune policy and "Rollback images" above); have workers clear their worktree build output. Verify free space is back above 10% | Any prune outside the installed timers | Free space under 10%, or no improvement in 24 h |
| `tmp-filesystem-nearly-full` (warning) | As `root-filesystem-low-space`; verify `df -h /tmp` ("Moving /tmp off RAM") | Root-owned leftovers | As `root-filesystem-low-space` |
| `host-ram-nearly-exhausted` (critical) | `ps -eo pid,ppid,rss,etime,cmd --sort=-rss \| head -20`; `kill -TERM` a runaway host `ng test` and its headless-chrome children; pause new worker launches; have .NET workers use `-m:1`. Verify available RAM is above 10% within 15 min | Stopping anything else (a compose container, a daemon, a session) | R1 at 15 min: R4 (the box is at risk) |
| `host-swap-and-ram-pressure` (warning) | As `host-ram-nearly-exhausted` | As `host-ram-nearly-exhausted` | R1 |
| `textfile-collector-stale` (warning) | `journalctl -u <writer> -n 30`. Quota telemetry returning 429 is not exhaustion: log-only. Once the cause is fixed, start the writer unit once and verify the file's mtime advances | Unit file edits | Stale over 2 h for a non-quota file, or R2 |
| `textfile-collector-scrape-error` (warning) | `docker logs compose-node-exporter-1 --tail 50`; a malformed repo gauge: ISSUE | | Still firing after 1 h |
| `host-unit-down-or-failed` (warning) | `systemctl status <u>`, `journalctl -u <u> -n 50`. A failed one-shot unit may be reset (`reset-failed`) and started once after its cause is fixed; the weekly-prune red from an unlabelled `:prev` image is by design ("Rollback images"); repo bugs: ISSUE. Verify the unit is active | Runner units, docker, containerd, tailscaled and the prune units | Backup or verify unit red 2 nights; any unit red 24 h |
| `claude-quota-behind-pace` (warning) | Log-only: low usage is not a reason to change work | | Never |
| `claude-quota-nearly-exhausted` (critical) | Log-only; pausing and resuming at the reset belongs to the fleet owner | | One report per weekly window, keyed by the reset date |
| `claude-quota-runway-short` (warning) | Log-only: burning the quota down before the reset is intended | | Never |
| `host-memory-pressure` (warning) | As `host-ram-nearly-exhausted` at lower urgency; the annotation's top-3 groups say where to look | As `host-ram-nearly-exhausted` | Firing 60 min, or a group over its `docs/resource-budget.md` line on 3 days in 7 |
| `container-near-memory-cap` (warning) | `docker stats --no-stream <c>`; ISSUE: a PR raising `mem_limit` | Live limit change | R2, or it reaches `container-oom-killed` |
| `host-swap-high` (warning) | Log-only unless `host-ram-nearly-exhausted` or `host-memory-pressure` also fires (a full swap with free RAM is steady state) | | As `host-memory-pressure` |
| `devclaw-heartbeat-hung` (critical) | Read `/health` of the devclaw container | The restart the description prescribes (a production container) | At once: R3 |
| `devclaw-token-burn-high` (warning) | `docker logs devclaw-devclaw-mcp-1 --tail 100` and its `/metrics` | | 2 consecutive hours |
| `notify-relay-cannot-deliver` (critical) | `docker logs compose-notify-relay-1 --tail 100` | Repairing the Telegram bot setup | At once: R4 (the owner's incident path is down) |
| `openclaw-run-failed` (critical) | `docker exec openclaw-openclaw-gateway-1 openclaw cron list --all`, `cron runs <id> --limit 5`, `tasks list --status failed`. Timeouts and "receipt authority unavailable" are transient: log-only (the next scheduled run is the retry). A config or code cause: ISSUE | A gateway restart; a manual re-run of a job that announces to the owner | The same job fails 3 in a row, or one error class 3 or more times in 7 days: ISSUE and report. An auth-class error: report |
| `claude-oauth-token-expiring` (warning) | Prepare the branch with the date edits (`docs/secrets-runbook.md`, token rotation) | Renewing the token | First firing |
| `ledger-finance-drift` (warning) | Read the drifting file; with no finance facts in it, a PR adding it to `WORKSPACE_ALLOW` | Carrying the facts to finance-sentry and retiring the file | Finance facts present: one request for a decision |
| `openclaw-api-credit-50` (warning) | `bash /srv/lifekit-stack/scripts/openclaw-auth/auth-mode.sh status` for the estimate and key state ("OpenClaw auth mode") | Top-ups, key changes, switching modes | Log-only; the 75% rule is the page |
| `openclaw-api-credit-75` (critical) | As `openclaw-api-credit-50`; report the estimate and the days left at the current burn | Top-ups (captain: Console credit, then `credit_usd` in `scripts/openclaw-auth/credit.json` by PR) | One request for the captain: top up or let the guard fall back at 95% |
| `openclaw-api-credit-90` (critical) | As `openclaw-api-credit-50` | Top-ups and `auth-mode.sh` switches | One request for the captain: top up now, or accept the automatic fallback to the subscription |
| `openclaw-api-fallback-engaged` (critical) | Read the `reason` label; `auth-mode.sh status` confirms mode `subscription` and a verified turn; report which cause fired | `auth-mode.sh api` and key or credit changes | One request for the captain: new key or top-up, then `auth-mode.sh api` |
| `finance-backup-verify-stale` (warning) | `docker logs finance-sentry-api --since 2h \| grep -i restore`; ISSUE in finance-sentry | | Unverified over 14 days (data at risk) |
| `finance-retention-stale` (warning) | As `finance-backup-verify-stale` for the retention jobs; ISSUE | | 3 days |
| `finance-job-consecutive-failures` (warning) | Grep the job in the API logs; a provider outage or rate limit is log-only; a code cause: ISSUE | An expired bank consent or API key | A credential cause: report. Failing over 24 h |
| `fleet-summary-stale` (warning) | Read `fleet_home_summary_generated_timestamp_seconds{home}` in `/var/lib/node_exporter/textfile/fleet.prom`. For the agent's own home, run its session-start recovery; for any other home, log-only (its keeper restarts it; never touch another home) | | Another home stale over 60 min |
| `grafana-notification-delivery-failing` (critical) | `docker logs compose-grafana-1 --since 30m \| grep -i notif` | Repairing the Telegram bot setup | R1: R4 (the phone path is blind) |
| `grafana-rule-evaluation-failing` (critical) | Grafana's log names the rule uid; ISSUE: a fix PR to the rule file (the deploy reloads Grafana) | | R2, or over 2 h |
| `prometheus-config-reload-failing` (critical) | `docker logs compose-prometheus-1 --tail 50`; ISSUE | | R1 |
| `prometheus-tsdb-compaction-failing` (warning) | Logs and `df` | Anything on the `prometheus_data` volume | Second firing |
| `prometheus-notifications-dropped` (critical) | Logs | | R1 |
| `weekly-release-not-merged` (warning) | `gh-axi run view <run id from the alert's run_url> -R <repo> --log-failed`. A flaky run: `gh-axi workflow run weekly-release.yml --ref main -R <repo>`. A real failure: fix the release PR's failing check | | Unmerged after 2 re-runs or 7 days |
| `agent-mail-sent` (critical, Loki) | `docker logs --since 1h openclaw-google-workspace-mcp-1 \| grep send_gmail_message`; match each send to an explicit request | | Any send with no matching request: R4 (security) |
| `external-heartbeat-watchdog` | None: it fires by design (`heartbeat.yml.tmpl`) and carries no signal | | Never |

A remedy that is not in the table is not unattended: report up with the diagnosis.

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
4. **Anthropic auth failing** — `bash /srv/lifekit-stack/scripts/openclaw-auth/auth-mode.sh status` says which mode the box is in and whether the key probe is accepted ("OpenClaw auth mode" above). In `api` mode a `rejected`/`exhausted` probe or a profile disabled for billing means the spend guard flips to `subscription` on its next pass (or run `auth-mode.sh subscription` now); in `subscription` mode the Claude Code login inside the gateway's Claude home may have expired (`docker exec openclaw-openclaw-gateway-1 claude auth status`; re-login is the captain's interactive `claude auth login`, see the 2026-10-31 usage report). The key's rotation and the SecretRef refs are in `docs/secrets-runbook.md`, "Claude service token"; `openclaw secrets audit --allow-exec` must show no unresolved ref.

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
#    and the backstage, basket and wger databases from the newest R2 set ("Off-box backup (R2)"
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

## Claude Code OpenTelemetry

Claude Code's built-in OTLP metrics export replaces, once the numbers agree,
the hand-made `claude_quota_share_percent` path (`scripts/quota-share` +
`scripts/quota-gauge/claude-quota-share-gauge.sh`). **That script is not
retired yet**: keep it running until the "Claude Code usage" dashboard
(`compose/observability/grafana/provisioning/dashboards/lifekit/claude-code.json`)
shows the same weekly numbers as the quota-share panel on the Box dashboard.

Path: Claude Code (host) -> `127.0.0.1:4318` (otel-collector, loopback bind)
-> `metrics` pipeline (`deltatocumulative`) -> `prometheus` exporter `:8889`
-> Prometheus job `otel-collector-metrics` -> Grafana. The dashboard applies
quota-share's weights in PromQL (input + 5*output + 1.25*cacheCreation +
0.1*cacheRead, x5/3 Opus/Fable, x1/3 Haiku), so the first panel is the
measured share and the second is that share times the account's used percent.
The captain's PC is not measured, as in the script.

### Operator steps

1. Merge the PR; the `deploy` job recreates the collector (config is a
   directory mount, no hot reload), publishes `127.0.0.1:4318` and reloads
   Prometheus and Grafana provisioning. Check: `curl -s http://127.0.0.1:4318/`
   answers (404 is fine), and Prometheus target `otel-collector-metrics` is up.
2. Set the environment for each Claude Code consumer, in that consumer's own
   settings `env` block or launcher (not in the harness env files from git).
   Give each consumer its own `consumer` value, matching the quota-share names:

   ```bash
   CLAUDE_CODE_ENABLE_TELEMETRY=1
   OTEL_METRICS_EXPORTER=otlp
   OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
   OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
   OTEL_METRICS_INCLUDE_SESSION_ID=false
   OTEL_RESOURCE_ATTRIBUTES=consumer=firstmate   # firstmate | devclaw | openclaw
   ```

   Containers (OpenClaw's Claude sessions, devclaw) reach the collector as
   `http://otel-collector:4318` instead. Sessions started before the change
   keep running without telemetry.
3. After a day, confirm series exist (the exporter name derives from
   `claude_code.token.usage`, unit `tokens`; if the name differs, fix the
   expressions in `claude-code.json`):
   `curl -s 'http://127.0.0.1:9090/api/v1/label/__name__/values' | grep claude_code`.
4. Compare the dashboard with the Box dashboard's quota share panel over a
   full week. Only when they agree: retire the gauge timer and
   `scripts/quota-share` in a separate change.

Notes: `devclaw` also has its own `devclaw_tokens_total` input in the script,
which this export does not replace until devclaw's sessions carry the tag.
`metric_expiration` is 8d, so a consumer that stops reporting disappears from
the dashboard after a week.

OpenClaw vault search (`memory.search.extraPaths` over the vault mount) is
already in `compose/openclaw-gateway/platform.patch.json` (#192, see "Vault
search for every agent"); nothing further was needed for vault decision 0013
step 1.

## Idle-lane check (host timer)

`lifekit-idle-lanes.timer` runs `scripts/idle-lanes/idle_lanes.py check` every minute as the admin
account, once per fleet home in `IDLE_LANES_HOMES` (the main home and each secondmate home). It
only detects and escalates; it never changes how work is dispatched.

A home is **idle with work** when its `bin/fm-capacity.sh` reports `free_lanes > 0` and its
`bin/fm-tasks-axi.sh ready` lists at least one item (held items and items whose `--until` date has
not arrived are not in that list). It is **not** idle when `fm-capacity.sh` names a host reason
(`cpu`, `memory`, `disk`), or when the quota snapshot
(`/var/lib/lifekit-fleet/metrics/quota-snapshot.json`) projects the weekly window to run out before
it resets; with no snapshot, a stale one, or a projection that outlasts the reset, queued work
counts as ready.

| Time idle | What happens (once per episode) |
| --- | --- |
| first tick | the home's state file records `since` |
| 15 min | `bin/fm-inbox.sh note` to that home's supervisor: lanes idle with work queued, refill |
| 30 min | one `act` envelope to the owner through notify-relay, and the state file turns `red` |
| either side zero | the episode clears; the next idle spell is a new episode |

A probe that fails (capacity or queue unreadable) neither starts nor clears an episode and fails
the unit for that tick; a note or push that fails is retried next tick and never repeated once it
landed.

State is one file per home, `/var/lib/lifekit-fleet/idle-lanes/<home>.json` (`idle-lanes.v1`:
`state` `ok|idle|red`, `since`, `free_lanes`, `ready`, `note_sent_at`, `pushed_at`, `checked_at`,
`reason`, `error`), world-readable for the dashboard.

A home is only covered once it has a lane target (`config/lane-capacity`): without one,
`fm-capacity.sh` reports `free_lanes=unknown reason=no-target`, the check never starts an episode
for it, and `status` reports it as unmonitored.

Status: `python3 /usr/local/bin/lifekit-idle-lanes.py status` prints each home as `ok`,
`idle since T`, `red since T` or `unmonitored: no lane target` (exit 1 on red, unmonitored, an
error, or a state file older than 5 minutes).

Apply and check (root for apply; the deploy has no sudo and does not run it):

```bash
sudo IDLE_LANES_HOMES="<main fleet home> <secondmate home>" bash /srv/lifekit-stack/scripts/ensure-idle-lanes.sh
bash /srv/lifekit-stack/scripts/ensure-idle-lanes.sh --check   # exit 0 only when installed copies match and the timer is enabled
```

Undo: `sudo systemctl disable --now lifekit-idle-lanes.timer`, then remove
`/usr/local/bin/lifekit-idle-lanes.py` and the two units under `/etc/systemd/system/`.
