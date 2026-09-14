# memory-audit - weekly vault audit (cron wrapper)

Run by the OpenClaw cron `memory_vault_audit` (Sun 03:30 Europe/Dublin) as a
deterministic `--command` job from the gateway workspace mount
(`/home/node/.openclaw/workspace/memory-audit/`, host `/srv/openclaw/workspace/memory-audit/`);
`deploy.sh` rsyncs this directory there on every deploy.

**The audit logic is not here.** Since 2026-09-14 it lives in the vault itself -
`~/memory/bin/` (`lint.sh`, `index.sh`, `rotate.sh`, `contradictions.sh`, `audit.sh`;
POSIX sh + awk, `jq` optional) - so the same scripts run as Claude Code hooks on the PC,
on demand in any session, and here weekly. Vault policy is encoded once, next to the
pages it governs; see the vault `README.md` "Harness" section.

`memory-vault-audit.sh` only locates the vault (`$MEMORY_VAULT`, default
`/home/node/.openclaw/wiki/main`), runs `bin/audit.sh --rotate --log`, checks that a
fresh `audits/latest.md` was written (delivery-required guard: a silent no-op is a cron
failure), and prints the one-line Telegram summary. The report and `log.md` line are
committed + pushed by the host `memory-sync.timer`.

Retired with this change: `vault-lint.py`, `vault-autofix.py`, `vault-rotate.py`,
`gen-report.py`, their pytest suite, and `skills/memory-vault/scripts/vault_scan.py`
(git history keeps them). `openclaw wiki compile` is no longer part of the audit: the
typed layer it compiled (`claims[]`, entities/syntheses/reports) was retired from the vault.
