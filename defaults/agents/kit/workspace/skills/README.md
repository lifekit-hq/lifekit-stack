# Kit's workspace skills

Skills are NOT vendored in this repo. They are installed at deploy time via the OpenClaw CLI, which puts each skill's `SKILL.md` into `<workspace>/skills/<skill-name>/` automatically.

## CLI tools

`workout-claw` and `life-state` are not skills: their skill entries are disabled and Kit runs them as CLIs (routing in `AGENTS.md`). They must be on the gateway's PATH. Install them globally (host or container):

```bash
npm install -g workout-claw life-state
```

## Adding more skills

Each new capability that Kit should be able to invoke as a discrete tool gets its own installable skill. Sources accepted by `openclaw skills install`:

- `<slug>` — ClawHub skill
- `git:owner/repo@ref` — Git repo
- `./path/to/skill` — local directory containing `SKILL.md`

After install, the skill lives at `<workspace>/skills/<name>/SKILL.md` and Kit auto-discovers it on next gateway start.

## Naming

OpenClaw skill discovery is **flat** under `skills/` — no nested category directories. Use hyphen-prefix conventions to group related skills if needed (e.g. `health-workout-claw`, `health-life-state`).
