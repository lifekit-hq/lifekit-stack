# Reader's workspace skills

Reader has no skill allowlist to maintain and reads no skill file: it has no file tool. Its two jobs carry what they need.

| Job | What it carries |
| --- | --- |
| `needs-you-mail-reader` | Its whole procedure is the job message (`scripts/ensure-needs-you-digest.sh`). |
| `doc-door` | The job message names `skills/doc-door/door.py`; the script prints its own guide (the skill's `SKILL.md`, sections 2 and 3) after the documents it shows. |

`scripts/ensure-doc-door.sh` installs the door skill here; `scripts/ensure-reader-agent.sh` installs this contract. Edit them in the repo, never under `/srv/openclaw`.
