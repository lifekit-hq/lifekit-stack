# skills/

Two kinds of skills live here:

**System skills** (committed to this repo) — wiring skills that are part of the stack itself, not personal content. They encode how OpenClaw interacts with the deployed modules.

**Personal skills** (not committed) — your personal vocabulary, routines, and preferences. These stay on disk and get rsynced to the VPS at deploy time.

## System skills

| Skill | Purpose |
|---|---|
| `lifekit-router` | Route every user intent to the right capability module using `~/.life/system/modules.yaml` |
| `memory-vault` | Operate the markdown memory vault: sync discipline, structure/link scanner (the vault `bin/lint.sh`), new-page checklist, log formats |
| `memory-defrag` | Manual-first `/defrag` pass over the vault: dedupe/merge candidates, splits, INDEX drift, orphan sources (`defrag_scan.py`); propose-don't-apply for merges. No cron without a separate graded proposal |
| `morning-brief` | Daily cross-project brief to Telegram (repo sweep via `gh` + devclaw live state) ending in numbered recommendations; a reply ("1 and 3") dispatches the selected items to devclaw via MCP. Owned by the `devclaw` waiter agent; installed + cron-ensured by `scripts/ensure-morning-brief.sh` (cron state lives in the gateway DB, not openclaw.json — the script is the git-side declaration) |

## Personal skills (v0.x)

## Why

`lifekit-stack` ships the *infrastructure layer* — Docker Compose for OpenClaw + lifekit-curator, the host bootstrap script, the deploy script. Workspace skills are *personal content*: they encode how you specifically want your agent to behave, your specific vocabulary, your scheduled routines, your domain interests. Bundling them in the public template would either:

- Bake one person's preferences into a "shared default" that doesn't fit anyone else, or
- Force every skill to be parameterized via Jinja2 templates (the wizard / parameterization work that's on the deferred-polish list — see `~/.life/system/lifekit-stack-execution-plan.md`).

So in v0.x, **your skills live at `~/.openclaw/workspace/skills/` on your laptop** and get `rsync`'d to the VPS at deploy time. They're never committed to git.

## How to get your skills onto the VPS

From your laptop, once after `scripts/bootstrap-vps.sh` finishes:

```bash
rsync -av --delete \
  ~/.openclaw/workspace/skills/ \
  <vps-tailscale-name>:/srv/openclaw/workspace/skills/
```

Then on the VPS, restart the gateway so it picks up the new skill manifest:

```bash
docker compose -f compose/docker-compose.yml --env-file /srv/openclaw/config/.env \
  restart openclaw-gateway
```

## What's coming later

When the wizard (`lifekit init-stack`) ships, this directory will hold **parameterized neutral skills** — `{{ user.name }}` substitutions, defaults for everything personal. Users will pick which skills to enable via the wizard; the wizard renders the templates with their values, lands the result in `/srv/openclaw/workspace/skills/`.

See `docs/customizing-skills.md` for the eventual convention.

For now: skills stay on disk. Repo stays clean.
