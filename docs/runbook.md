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
4. deploy.sh then runs one real agent turn per runtime (`fable` for
   claude-cli, `kit` for codex; `SMOKE_AGENTS` overrides) in a throwaway
   `deploy-smoke-<agent>` session. A runtime error fails the run; auth and quota
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

### Host-config patch: plugin enable and the 2026-09-16 audit warnings

`/srv/openclaw/config/openclaw.json` is host state: this repo does not apply
it, it only documents the patch. The patch below enables the plugin and
addresses the four `openclaw security audit` warnings from 2026-09-16
(`gateway.auth_no_rate_limit`, `tools.exec.security_full_configured`,
`tools.exec.agent_skill_mcp_boundary_drift`, `models.weak_tier`). It is
checked against the 2026.9.4 schema (`openclaw config schema`); objects
merge and arrays replace (`openclaw config patch --help`). Save it as
`openclaw.patch.json5`:

```json5
{
  gateway: {
    auth: {
      rateLimit: { maxAttempts: 10, windowMs: 60000, lockoutMs: 300000, exemptLoopback: true },
    },
  },
  plugins: {
    entries: { "diagnostics-prometheus": { enabled: true } },
  },
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

Apply it (dry run first), then recreate the gateway from the compose dir -
this interrupts Kit briefly. `gateway.reload` defaults to `hybrid`, so the
`plugins` change may restart the gateway on its own:

```bash
docker exec -i compose-openclaw-gateway-1 openclaw config patch --stdin --dry-run < openclaw.patch.json5
docker exec -i compose-openclaw-gateway-1 openclaw config patch --stdin < openclaw.patch.json5
cd /srv/lifekit-stack/compose
docker compose --env-file /srv/openclaw/config/.env -f docker-compose.yml \
  up -d --force-recreate openclaw-gateway
```

Exec policy per agent:

- career, learning, social: set to `ask` - 0 exec calls in 30 days and no domain cron.
- fable: `full` to `ask` - no sessions ever; its only automated turn is the deploy pong smoke.
- devclaw keeps `full` - its daily morning-brief cron sweeps repos with `gh` and writes briefs.
- kit keeps `full` - skills github, gh-issues and summarize need host binaries (145 exec calls in 30 days).
- health keeps `full` - its three claw CLIs are its only write path.
- finance keeps `full` - it writes state logs via bash (2160 exec calls in 30 days).

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

## Backups

`~/.life/` is your data. Back it up.

**Recommended:** push to a private git repo from the VPS, on a cron:

```bash
# /srv/life-backup-cron, runs every 6 hours
cd /srv/life
git add -A
git commit -m "snapshot $(date -Iseconds)" || true
git push origin main
```

This gives you point-in-time recovery and an off-machine copy.

**Volumes to back up if you want full disaster recovery:**

- `/srv/life/` — your knowledge data (most important)
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

Symptom: `/srv/life/queue.jsonl` keeps growing; domain files don't update.

```bash
ssh <your-vps-tailscale-name>
docker compose logs -f lifekit-curator --tail 100
```

Common causes:

1. **Curator crashed** — `docker compose restart lifekit-curator`. Check logs for the underlying error.
2. **Claude CLI auth in the curator container failed** — `docker compose exec lifekit-curator claude auth status`.
3. **`~/.life/domains/` not writable** — `docker compose exec lifekit-curator ls -la /srv/life/domains/`.

## SSHFS auto-mount

To auto-mount `/srv/life/` on your laptop at login:

```bash
# Add to /etc/fstab (Linux) or ~/Library/LaunchAgents (macOS)
<vps-tailscale-name>:/srv/life /home/<you>/.life fuse.sshfs \
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

# 3. Restore /srv/life/ from your private backup repo:
ssh <new-vps>
cd /srv/life
git clone <your-private-life-repo> .
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
