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
4. deploy.sh then runs one real agent turn each for `fable` and `kit`
   (`SMOKE_AGENTS` overrides) in a throwaway `deploy-smoke-<agent>` session —
   both on claude-cli since the 2026-09-16 all-agents switch to Claude
   primary (see `scripts/deploy.sh` smoke-turn comment); OpenAI/codex is not
   primary for any agent right now. A runtime error fails the run; auth and quota
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

Before a jump across several months of releases, take a full verified state
backup first (the migrations are one-way; an older gateway cannot read the
migrated state, so the image tags alone do not roll back). Roughly 6 GB
compressed, 20+ minutes, the gateway can stay up:

```bash
ssh <your-vps-tailscale-name>
sudo docker run -d --name openclaw-backup --entrypoint openclaw \
  -e HOME=/home/node -e OPENCLAW_STATE_DIR=/home/node/.openclaw \
  -e OPENCLAW_CONFIG_PATH=/home/node/.openclaw/openclaw.json \
  --env-file /srv/openclaw/config/.env \
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

To rehearse a bump without touching live state: rsync `/srv/openclaw/config`
(minus `browser`, `tools`, `logs`) and the workspace to scratch dirs, run
the same `doctor --fix --non-interactive` with the new image against the
copies, read the doctor log and `doctor --json`, then delete the copies.

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
V="$(docker exec compose-openclaw-gateway-1 openclaw --version | awk '{print $2}')"
docker exec compose-openclaw-gateway-1 openclaw plugins install "@openclaw/diagnostics-prometheus@${V}"
docker exec compose-openclaw-gateway-1 openclaw plugins inspect diagnostics-prometheus | grep Trust
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
`diagnostics-otel` plugin enables, `gateway.auth.rateLimit`,
`agents.defaults.heartbeat.every`, the inbound `hooks` block and the finance
agent's heartbeat - live in
`compose/openclaw-gateway/platform.patch.json`. On every deploy `deploy.sh`
compares that file with the live config, applies it with
`openclaw config patch` only when a key differs, and force-recreates the
gateway only when the CLI's apply hint says the changed keys need it
(`plugins.entries` does; the rest hot-reloads under the default
`gateway.reload` hybrid mode). A deploy that finds nothing to change writes
nothing and leaves the gateway alone. A new platform key goes in that file,
never in a PR body; objects merge and scalars replace
(`openclaw config patch --help`), and the file is strict JSON so the deploy
can read it with stdlib Python. Check new keys against the installed schema
(`openclaw config schema`).

#### Inbound hooks and the finance pulse (in the platform file since 2026-09-18)

The same file turns on OpenClaw's inbound HTTP hooks for one caller and one
agent, and gives the finance agent a heartbeat:

- `hooks`: enabled, `allowedAgentIds: ["finance"]`, caller-supplied session
  keys off, and one mapping - `POST <hook path>/finance-sentry` runs the
  finance agent in an isolated session and delivers to its Telegram chat. The
  template interpolates `kind` and `eventId` only: the push carries
  identifiers, the agent reads the detail back through its MCP tools.
- `agents.entries.finance.heartbeat`: every 6h inside 07:00-23:00
  Europe/Dublin, sonnet, `lightContext`, `isolatedSession`. It is the one
  `agents.entries` key in the platform file; every other per-agent key stays
  host state. `agents.defaults.heartbeat.every` stays `0m`, so only this agent
  ticks.
- **No value is in git or in `openclaw.json`.** The file holds the literal
  references `${OPENCLAW_HOOK_TOKEN}`, `${OPENCLAW_HOOK_PATH}` and
  `${OPENCLAW_FINANCE_CHAT}`; OpenClaw resolves them from the container
  environment, which compose fills from `/srv/openclaw/config/.env` (see
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

The pulse's checklist is not config. It is the scratch of the
`heartbeat-finance` automation row (the workspace `HEARTBEAT.md` is a no-op in
2026.9.4), declared by `scripts/ensure-finance-pulse.sh` from
`scripts/finance-pulse.md`, the same way `scripts/ensure-morning-brief.sh`
declares its cron. Run it once on the host after the deploy that applied the
heartbeat. Until then the scratch is empty and every tick skips with
`reason=empty-heartbeat-file` and no model call.

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
   docker exec compose-openclaw-gateway-1 openclaw cron rm <ledger-scan job id>
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
     digest, no morning brief. The pulse (`heartbeat-finance` scratch) carries
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
      health: { model: { fallbacks: [] } },
      career: { model: { fallbacks: [] }, tools: { exec: { mode: "ask" } } },
      learning: { model: { fallbacks: [] }, tools: { exec: { mode: "ask" } } },
      social: { model: { fallbacks: [] }, tools: { exec: { mode: "ask" } } },
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
docker exec -i compose-openclaw-gateway-1 openclaw config patch --stdin --dry-run < openclaw.patch.json5
docker exec -i compose-openclaw-gateway-1 openclaw config patch --stdin < openclaw.patch.json5
```

Exec policy per agent:

- career, learning, social: set to `ask` - 0 exec calls in 30 days and no domain cron.
- fable: `full` to `ask` - no sessions ever; its only automated turn is the deploy pong smoke.
- devclaw keeps `full` - its daily morning-brief cron sweeps repos with `gh` and writes briefs.
- kit keeps `full` - skills github, gh-issues and summarize need host binaries (145 exec calls in 30 days).
- health keeps `full` - its three claw CLIs are its only write path.
- finance keeps `full` - it reaches its `state/` files (the learning journal)
  with bash (2160 exec calls in 30 days, counted before `ledger-scan` was
  retired).

Command-kind crons (`memory_vault_audit`, `weekly_log_summary`) bypass exec
policy.

Expected `openclaw security audit` after the patch: `gateway.auth_no_rate_limit`
and `models.weak_tier` cleared; `security_full_configured` (devclaw) and
`agent_skill_mcp_boundary_drift` (kit, health, finance) remain.

Follow-up, out of scope for this change: the boundary-drift remediation
(sandbox those agents, or split the sensitive MCP servers into a separate
gateway).

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

If the new version misbehaves after the deploy checks passed:

```bash
ssh <your-vps-tailscale-name>
cd /srv/lifekit-stack/compose
docker tag lifekit-openclaw:prev  lifekit-openclaw:local
docker compose --env-file /srv/openclaw/config/.env -f docker-compose.yml \
  up -d --no-build --force-recreate openclaw-gateway
```

Then revert the bump PR on `main` (below), or the next CI deploy rebuilds the
bad version over your rollback. This only works when the bump did not migrate
state. When it did (the deploy log shows "migrating state"), the old gateway
cannot read the migrated state dir: restore the full pre-upgrade backup
(`openclaw backup restore`, or untar the archive over `/srv/openclaw/config`
with the gateway stopped) before starting the `:prev` image.

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
back on their own; finance-sentry, devclaw, the dashboard, xui and closeloop
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

## Backups

`/srv/memory/` (the memory vault, mounted on your laptop as `~/memory/`) is your
data. Back it up.

**Set up your own mirror.** The simplest backup that works is a private git
repo the VPS pushes to on a timer:

```bash
# One-time: bootstrap-vps.sh leaves /srv/memory a plain directory, so make it a
# repo pointing at your private remote before the timer runs.
cd /srv/memory
git init -b main
git remote add origin <your-private-vault-repo>

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
- `/srv/openclaw/config/` — OpenClaw config + `.env`
- `/srv/openclaw/secret-key/` — OpenClaw OAuth encryption key (lose this and you re-pair every channel)
- `/srv/openclaw/workspace/` — workspace skills (recoverable from this repo, but having a local copy is faster)

Snapshot these via Hetzner Backups (built-in, ~20% extra/mo) or rsync to another box.

## When Telegram goes silent

Symptom: your bot stops responding, no errors visible.

```bash
ssh <your-vps-tailscale-name>
cd /srv/lifekit-stack
docker compose logs -f openclaw-gateway --tail 100
docker compose --profile cli run --rm -T openclaw-cli openclaw doctor
docker compose --profile cli run --rm -T openclaw-cli openclaw channels list
```

Common causes:

1. **Telegram token rotated** — check `/srv/openclaw/config/.env` against your BotFather token. Update + restart.
2. **Polling stalled** — `docker compose restart openclaw-gateway`.
3. **OpenClaw OOM** — `dmesg | grep -i oom`. If yes, scale up the VPS.
4. **Anthropic auth expired** — `docker compose --profile cli run --rm openclaw-cli claude auth status`. Re-login if needed.

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
bakes in `python3 / make / g++ / libvips-dev` so native modules (sharp,
better-sqlite3, etc.) can rebuild on-host. That covers the per-skill
`node_modules` case automatically — re-run `scripts/deploy.sh` and the
`sharp` error should clear.

Skill CLIs that live outside this repo (e.g. `life-state`, which is built
from `~/projects/life-state` on the maintainer's laptop) are NOT handled
automatically — they have to be installed once on the VPS after the first
rsync. Inside the gateway container:

```bash
docker compose -f compose/docker-compose.yml --env-file /srv/openclaw/config/.env \
  exec openclaw-gateway npm install -g /home/node/.openclaw/workspace/external/life-state
```

…and then restart the gateway. If `external/life-state` isn't there yet,
rsync it across alongside the skills directory.

## When `queue.jsonl` grows without draining

Symptom: `/srv/memory/queue.jsonl` keeps growing; domain files don't update.

```bash
ssh <your-vps-tailscale-name>
docker compose logs -f lifekit-curator --tail 100
```

Common causes:

1. **Curator crashed** — `docker compose restart lifekit-curator`. Check logs for the underlying error.
2. **Claude CLI auth in the curator container failed** — `docker compose exec lifekit-curator claude auth status`.
3. **`/srv/memory/domains/` not writable** — `docker compose exec lifekit-curator ls -la /srv/memory/domains/`.

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

You provisioned a Hetzner instance, it died, and you want to come back up on a fresh one.

```bash
# 1. New VPS, fresh Ubuntu 24.04. SSH in (over its temporary public address).
# 2. From your laptop:
cd lifekit-stack
lifekit init-stack --target <new-vps-ip>
# Wizard reuses your saved wizard.yaml (from your private backup, NOT this repo).

# 3. Restore /srv/memory/ from your private vault mirror (see "Backups" above -
#    it is plaintext, and only as fresh as its last push). Step 2 already
#    populated /srv/memory/ (system/modules.yaml), so a clone into it would
#    refuse - point the directory at the remote and reset onto it instead:
ssh <new-vps>
cd /srv/memory
git init -b main
git remote add origin <your-private-vault-repo>
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

Hetzner CX22 → CX32 is a live resize (no data loss). For larger jumps, snapshot first.

## When to ask for help

Open a [GitHub Issue](https://github.com/lifekit-hq/lifekit-stack/issues) with:

- `lifekit init-stack --version`
- `docker compose version`
- VPS provider + plan
- `docker compose logs --tail 200` (sanitized — never paste tokens)
- What you tried before opening the issue

Non-Hetzner setups are accepted, but no official SLA in v0.x.
