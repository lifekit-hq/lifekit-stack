# memory-audit - weekly vault audit (cron wrapper)

Run by the OpenClaw cron `memory_vault_audit` (Sun 03:30 Europe/Dublin) as a
deterministic `--command` job from the gateway workspace mount
(`/home/node/.openclaw/workspace/memory-audit/`, host `/srv/openclaw/workspace/memory-audit/`);
`deploy.sh` (its `prepare` OpenClaw phase, `deploy-openclaw.sh`) rsyncs this directory there on every OpenClaw deploy (a change under this directory triggers one; see `docs/runbook.md` "Path-gated OpenClaw deploys").

**The audit logic is not here.** Since 2026-09-14 it lives in the vault itself -
`~/memory/bin/` (`lint.sh`, `index.sh`, `rotate.sh`, `contradictions.sh`, `audit.sh`;
POSIX sh + awk, `jq` optional) - so the same scripts run as Claude Code hooks on the PC,
on demand in any session, and here weekly. Vault policy is encoded once, next to the
pages it governs; see the vault `README.md` "Harness" section.

`memory-vault-audit.sh` only locates the vault (`$MEMORY_VAULT`, default
`/home/node/memory`, the gateway's vault mount), runs `bin/audit.sh --rotate --log`, checks that a
fresh `audits/latest.md` was written (delivery-required guard: a silent no-op is a cron
failure), and prints the one-line Telegram summary. The report and `log.md` line are
committed + pushed by the host `memory-sync.timer`.

Retired with this change: `vault-lint.py`, `vault-autofix.py`, `vault-rotate.py`,
`gen-report.py`, their pytest suite, and `skills/memory-vault/scripts/vault_scan.py`
(git history keeps them). `openclaw wiki compile` is no longer part of the audit: the
typed layer it compiled (`claims[]`, entities/syntheses/reports) was retired from the vault.

## The model pass (`memory-vault-cleanup`)

This cron only does the mechanical half. `memory-vault-cleanup`
(`../ensure-memory-vault-cleanup.sh`) is a second, agent-turn cron 20 minutes later
on the same `kit` agent: it runs the vault's model jobs M1-M4 and M6 on the findings
this cron just wrote (`audits/findings.json` and two sections of
`audits/latest.md`), commits each job as `memory-model` with a `model:` subject,
and delivers its owner calls through notify-relay's envelope
(`docs/message-format.md`) only when they change. See `docs/runbook.md` "The
memory-vault-cleanup cron".
