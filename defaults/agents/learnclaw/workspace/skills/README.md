# Learnclaw's workspace skills

The agent's allowlist is `agents.entries.learnclaw.skills` in the live `openclaw.json` (host state); a skill resolves when its `SKILL.md` is on a skill root the gateway reads (bundled, `<workspace>/skills/<name>/`, or `skills.load.extraDirs`).

## The declared set

| Skill | What learnclaw uses it for |
| --- | --- |
| `learning-record` | Reading the artefact record with its projections computed now, and appending one row per artefact step. The only vault write learnclaw has. |

`learning-record` is vendored in this repo (`skills/learning-record/`): `scripts/ensure-learnclaw.sh` copies it into this workspace on every deploy, together with `AGENTS.md`, `SOUL.md` and `IDENTITY.md`.

## Check

```bash
# Run inside the gateway container.
openclaw skills --agent learnclaw list       # learning-record should read ready
```
