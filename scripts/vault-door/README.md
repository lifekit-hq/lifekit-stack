# vault-door

ChatGPT's door to the fleet, through the one channel it already has: the memory vault.
No MCP server, no credential, no service. `vault_door.py bridge` runs every 5 minutes from a
systemd timer (installed by `install-vault-door.sh`, runbook "Vault door"): it turns each new
`- YYYY-MM-DD - chatgpt: <text>` line in the vault `inbox.md` into one held `proposal:` note in
the main firstmate captain inbox, and appends `[forwarded: <note id>]` to the line.

The only input is the vault. Writes: the markers in `inbox.md`, and notes through the main
home's own `fm-inbox.sh note`.

Behaviour worth knowing:

- **Dedupe.** The note's request id is `gpt-` plus a hash of the whitespace-normalised text, so
  the same text is one note however often it is written or the job runs (fm-inbox replays a
  request id). The marker is written last: a crash before it replays the same id next run.
- **Untrusted text.** The captain inbox gets `proposal: <text>` plus a provenance line. A
  `proposal:` note is a held backlog item for joint review, never dispatched work.
- **Limits.** 5 new notes per run, 16 KiB per note; an empty or oversize note is marked
  `[not forwarded: <reason>]`. Lines in `inbox.md` without `chatgpt:` are never touched.
- **Vault etiquette.** The job skips while `/srv/memory/.model-pass` is under an hour old (the
  rule `memory-sync.sh` follows) and writes `inbox.md` in place, keeping its inode and ACL.

Tests: `scripts/tests/test_vault_door.py`.
