# Career's workspace skills

Skills are NOT vendored in this repo. The agent's allowlist is `agents.entries.career.skills` in the live `openclaw.json` (host state); a skill resolves when its `SKILL.md` is on a skill root the gateway reads (bundled, `<workspace>/skills/<name>/`, or `skills.load.extraDirs`).

## The declared set

| Skill | What career uses it for |
| --- | --- |
| `github` | Reading the owner's public repos as portfolio evidence (needs `gh`, so exec - interactive only) |
| `gh-issues` | Same, for issues and PRs |
| `notion` | Notes the owner keeps in Notion, when they point you at one |
| `browser-automation` | Reading public job listings and company pages - never LinkedIn, never a logged-in session |
| `diagram-maker` | Career-path and skill-gap diagrams |

`summarize` is not declared: it is disabled globally in `skills.entries`, so declaring it only adds an unresolved entry.

## Check and install

```bash
# Run inside the gateway container. The allowlist takes the SKILL.md `name`,
# not the directory slug.
openclaw skills --agent career list          # every declared skill should read ready
openclaw skills info "<name>"                # where a skill resolves from
openclaw skills install <slug>               # ClawHub slug, git:owner/repo@ref, or ./path
```

After an install, recreate the gateway so it re-reads the skill manifest (a plain restart is not enough). Skill discovery is flat under `skills/`; use hyphen prefixes to group related skills.
