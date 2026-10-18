# `~/.claude/CLAUDE.md` - Claude Code harness pointer

**`~/memory/CORE.md`** is injected at session start by the `vault-core.sh` SessionStart hook (look for the `<vault-core>` block) - the ONE mandatory read (who the owner is, how to behave, hard rules, on-demand map). Read the file yourself only if that block is missing. Do NOT bulk-read the rest of the vault; pull pages only when the task needs them, per CORE's map.

Before writing anything to the vault, follow **`~/memory/README.md`** - the contract (schema, write policy, rotation policy). Key write rules: facts vs state, update-over-append, no session-exhaust files, deletion-by-rotation is normal.

When the owner tells you something durable (a decision, a goal, a preference), update the right vault page in the same session.

## Claude-Code-specific notes

- Older notes may say `~/.life/` or `/srv/life/` - translate to `~/memory/` (on the VPS host the vault is `/srv/memory/`). Frozen dated content keeps legacy paths verbatim as historical evidence.
- Claude-Code auto-memory (`~/.claude/projects/<x>/memory/`) is **off by policy** (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) - per-machine and invisible to other agents; the vault is the single source of truth.

<!-- Managed by lifekit-stack scripts/claude-home/install-claude-home.sh (source: defaults/claude-home/CLAUDE.md); a deploy rewrites hand edits. -->
