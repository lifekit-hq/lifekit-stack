# skills/

Two kinds of skills live here:

**System skills** (committed to this repo) — wiring skills that are part of the stack itself, not personal content. They encode how OpenClaw interacts with the deployed modules.

**Personal skills** (not committed) — your personal vocabulary, routines, and preferences. These stay on disk and get rsynced to the VPS at deploy time.

## System skills

| Skill | Purpose |
|---|---|
| `lifekit-router` | Route every user intent to the right capability module using `~/.life/system/modules.yaml`. Not used by Kit, which routes by its own `AGENTS.md` and hands logged-in sites, purchases or bookings, and desktop work to firstmate |
| `memory-vault` | Operate the markdown memory vault: sync discipline, structure/link scanner (the vault `bin/lint.sh`), new-page checklist, log formats |
| `memory-defrag` | Manual-first `/defrag` pass over the vault: dedupe/merge candidates, splits, INDEX drift, orphan sources (`defrag_scan.py`); propose-don't-apply for merges. No cron without a separate graded proposal |
| `secondmate-relay` | Kit pipes the user's words, unchanged, to the captain inbox of the main firstmate (`firstmate`) or the VPS second mate (`fm-vps`, the default) and shows its replies unchanged, through the host's `kit-relay` forced command (`scripts/kit-relay/`); `relay.sh status` reads the fleet JSON the dashboard's fleet page uses (`lifekit-dashboard:8080/api/fleet`), read-only. Installed into kit's workspace by `scripts/ensure-secondmate-relay.sh`; kit's skill allowlist is an operator patch (`docs/runbook.md`, "Kit's second-mate relay") |
| `workout-log` | Kit logs a gym session from one typed or spoken line through the `workout-claw` CLI baked into the gateway image (spaces in names, kg/lb, uneven sets), confirms it in one line read back from `workout-claw summary`, and answers "what did I do last <lift>". `nudge.sh` is the gym-day 21:00 check behind the `workout-nudge` cron, whose day-of-week field (the gym days) is host state. Installed into kit's workspace by `scripts/ensure-workout-log.sh`; the allowlist and the cron are operator steps (`docs/runbook.md`, "Kit's workout log and gym-day nudge") |
| `video-eval` | One intake judge for videos: a YouTube or Instagram link (or a video file) the owner sends Kit, or one firstmate or a scout files from the host (`scripts/video-eval.sh`), is queued on the shared `job-queue`. When the owner's PC is on, the drain fetches it through the PC: YouTube captions first, else the audio, transcribed locally with the image's `whisper`. Kit then judges it in one pass against the versioned rubric (`rubric.md`). The rubric covers what it is, its claims and how they hold, its use for the owner's goals, the cost of adoption and an accept / park / drop verdict with a reason code; claim-check is a mode of it. A Kit item gets a three-line answer, other producers poll `status`. Reports and the append-only index stay in `~/.life-state/intake/` on the VPS, never published, and a repeat answers with its earlier verdict. Installed into kit's workspace by the deploy (`scripts/ensure-video-eval.sh`); kit's allowlist and the goals file are operator steps (`docs/runbook.md`, "Video evaluation") |
| `job-queue` | The shared, typed queue for work that waits for an egress the server lacks (today the owner's PC through the yt-tunnel relay; configured by `LIFEKIT_EGRESS_PROXY`): one private file per job, deduplicated per key, drained one job at a time by kit's `job-queue-drain` cron row whose trigger script skips the turn until the job's egress answers, with bounded attempts and exactly one answer per job. Installed into the gateway's shared skills dir by `scripts/ensure-job-queue.sh`; the cron row is an operator step (`docs/runbook.md`, "Shared job queue") |
| `youtube-transcript` | Plain-text captions of a YouTube video so an agent can summarize it, fetched with a pinned yt-dlp through an SSH SOCKS tunnel to the owner's PC (YouTube refuses the VPS IP); answers `transcript unavailable - the PC is off` when the PC is off. Tunnel unit: `scripts/yt-tunnel/`; installed into the gateway's shared skills dir by `scripts/ensure-youtube-transcript.sh` (`docs/runbook.md`, "YouTube transcripts through the PC") |

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
docker compose -p openclaw -f compose/openclaw/docker-compose.yml --env-file /srv/lifekit-secrets/stack.env \
  restart openclaw-gateway
```

## What's coming later

When the wizard (`lifekit init-stack`) ships, this directory will hold **parameterized neutral skills** — `{{ user.name }}` substitutions, defaults for everything personal. Users will pick which skills to enable via the wizard; the wizard renders the templates with their values, lands the result in `/srv/openclaw/workspace/skills/`.

See `docs/customizing-skills.md` for the eventual convention.

For now: skills stay on disk. Repo stays clean.
