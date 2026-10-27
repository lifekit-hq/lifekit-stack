# vault-door

ChatGPT's two-way door to the fleet, through the one channel it already has: the memory vault.
No MCP server, no credential, no service. `vault_door.py` has two modes, run by two systemd
timers (installed by `install-vault-door.sh`, runbook "Vault door"):

| Mode | Timer | What it does |
| --- | --- | --- |
| `snapshot` | daily 05:30 UTC | Overwrites the vault page `system/fleet-snapshot.md`: numbers, open captain calls, queued work, what merged in the last 7 days. |
| `bridge` | every 5 minutes | Turns each new `- YYYY-MM-DD - chatgpt: <text>` line in the vault `inbox.md` into one held `proposal:` note in the main firstmate captain inbox, and appends `[forwarded: <note id>]` to the line. |

Inputs are read-only: the fleet publisher's merged `home-summary.json` and `daily.jsonl`
(`scripts/fleet-publisher/`), `gh search prs`, and the vault. Writes: the snapshot page, the
markers in `inbox.md`, and notes through the main home's own `fm-inbox.sh note`.

Behaviour worth knowing:

- **Dedupe.** The note's request id is `gpt-` plus a hash of the whitespace-normalised text, so
  the same text is one note however often it is written or the job runs (fm-inbox replays a
  request id). The marker is written last: a crash before it replays the same id next run.
- **Untrusted text.** The captain inbox gets `proposal: <text>` plus a provenance line. A
  `proposal:` note is a held backlog item for joint review, never dispatched work.
- **Limits.** 5 new notes per run, 16 KiB per note; an empty or oversize note is marked
  `[not forwarded: <reason>]`. Lines in `inbox.md` without `chatgpt:` are never touched.
- **Snapshot hygiene.** A line that looks like a credential, or restates a subject another vault
  page owns (`bin/subjects.txt`), is withheld; host paths become `<path>`; a `[[`/`]]` in copied
  titles or summaries is backslash-escaped so it never reads as a wikilink. Without the fleet
  summary the page is left as it was and the unit fails.
- **Page budget.** The page stays within 280 lines and 22 KiB (the vault lint refuses a page
  past its own cap): at most 10 queue groups, and when still over budget the per-section item
  cap shrinks, each trimmed list ending in `… and N more`. Frontmatter and the proposal
  paragraph are kept.
- **Vault etiquette.** Both jobs skip while `/srv/memory/.model-pass` is under an hour old (the
  rule `memory-sync.sh` follows) and write files in place, keeping their inode and ACL.

Tests: `scripts/tests/test_vault_door.py`.
