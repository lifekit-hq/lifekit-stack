# Kit's workspace skills

Skills are NOT vendored in this repo. They are installed at deploy time via the OpenClaw CLI, which puts each skill's `SKILL.md` into `<workspace>/skills/<skill-name>/` automatically.

## Install at deploy time

```bash
# Run inside the gateway environment (host or container, whichever runs Kit's
# OpenClaw daemon). After install, restart the gateway to pick up the new
# skill manifest — OpenClaw caches the skill list at startup.

openclaw skills install workout-claw
openclaw skills install life-state

# Restart gateway:
systemctl --user restart openclaw-gateway.service        # on host
# OR
docker compose -p openclaw -f compose/openclaw/docker-compose.yml --env-file ... \
  up -d --force-recreate openclaw-gateway                # in compose
```

## CLI binaries

The `SKILL.md` manifests assume the underlying CLIs are on the gateway's PATH. Install them globally (host or container):

```bash
npm install -g workout-claw life-state
```

## Adding more skills

Each new capability that Kit should be able to invoke as a discrete tool gets its own installable skill. Sources accepted by `openclaw skills install`:

- `<slug>` — ClawHub skill (e.g. `workout-claw`)
- `git:owner/repo@ref` — Git repo
- `./path/to/skill` — local directory containing `SKILL.md`

After install, the skill lives at `<workspace>/skills/<name>/SKILL.md` and Kit auto-discovers it on next gateway start.

## Naming

OpenClaw skill discovery is **flat** under `skills/` — no nested category directories. Use hyphen-prefix conventions to group related skills if needed (e.g. `health-workout-claw`, `health-life-state`).
