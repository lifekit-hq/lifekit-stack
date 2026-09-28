# Social's workspace skills

Skills are NOT vendored in this repo. The agent's allowlist is `agents.entries.social.skills` in the live `openclaw.json` (host state); a skill resolves when its `SKILL.md` is on a skill root the gateway reads (bundled, `<workspace>/skills/<name>/`, or `skills.load.extraDirs`).

## The declared set

| Skill | Source | What social uses it for |
| --- | --- | --- |
| `LinkedIn Writer` | ClawHub `linkedin-writer-1-0-0`, workspace | LinkedIn post drafts |
| `tiktok-growth` | ClawHub `tiktok-growth`, workspace | TikTok hooks and scripts |
| `Social Media Scheduler` | ClawHub `social-media-scheduler`, workspace | The posting calendar (a plan, not platform scheduling) |
| `github` | bundled | Public project activity as post material (needs `gh`, so exec - interactive only) |
| `notion` | host, see `skills info` | Notes the owner keeps in Notion, when they point you at one |
| `meme-maker` | host, see `skills info` | Image ideas for posts |

Not declared: `summarize` (disabled globally in `skills.entries`, so it only adds an unresolved entry) and `weather` (outside the domain).

## Check and install

```bash
# Run inside the gateway container. The allowlist takes the SKILL.md `name`
# (e.g. "LinkedIn Writer"), not the directory slug (linkedin-writer-1-0-0).
openclaw skills --agent social list          # every declared skill should read ready
openclaw skills info "<name>"                # where a skill resolves from
openclaw skills install <slug>               # ClawHub slug, git:owner/repo@ref, or ./path
```

After an install, recreate the gateway so it re-reads the skill manifest (a plain restart is not enough). Skill discovery is flat under `skills/`; use hyphen prefixes to group related skills.
