# memory-audit - weekly vault audit (host timer)

Run by `lifekit-vault-audit.timer` (Sun 03:30 Europe/London, `Persistent=true`) as `lifekit` on
the host: `run-vault-audit.py` runs `memory-vault-audit.sh` with `MEMORY_VAULT=/srv/memory` and
posts the wrapper's one-line summary to notify-relay (`info`), or its `AUDIT FAIL:` line as an
`act` envelope and a failed unit. It replaced the OpenClaw cron `memory_vault_audit`, whose
gateway credits end 2026-11-09. Install, rehearsal, cutover and rollback are in `docs/runbook.md`
"Weekly vault audit (host timer)"; `install-vault-audit.sh` installs the two units (and
`bootstrap-vps.sh` calls it).

`deploy.sh` (its `prepare` OpenClaw phase, `deploy-openclaw.sh`) still rsyncs this directory
into the gateway workspace (`/srv/openclaw/workspace/memory-audit/`) for the cron until the cron
is removed; a change under this directory triggers an OpenClaw deploy (see `docs/runbook.md`
"Path-gated OpenClaw deploys").

**The audit logic is not here.** Since 2026-09-14 it lives in the vault itself -
`~/memory/bin/` (`lint.sh`, `index.sh`, `rotate.sh`, `contradictions.sh`, `audit.sh`;
POSIX sh + awk, `jq` optional) - so the same scripts run as Claude Code hooks on the PC,
on demand in any session, and here weekly. Vault policy is encoded once, next to the
pages it governs; see the vault `README.md` "Harness" section.

`memory-vault-audit.sh` only locates the vault (`$MEMORY_VAULT`; the unit sets `/srv/memory`,
the default `/home/node/memory` is the gateway's mount for the cron), runs
`bin/audit.sh --rotate --log`, checks that a fresh `audits/latest.md` was written
(delivery-required guard: a silent no-op is a failure), and prints the one-line summary. The
report and `log.md` line are committed + pushed by the host `memory-sync.timer`.

Retired with this change: `vault-lint.py`, `vault-autofix.py`, `vault-rotate.py`,
`gen-report.py`, their pytest suite, and `skills/memory-vault/scripts/vault_scan.py`
(git history keeps them). `openclaw wiki compile` is no longer part of the audit: the
typed layer it compiled (`claims[]`, entities/syntheses/reports) was retired from the vault.

## The model pass (`memory-vault-cleanup`)

This job only does the mechanical half. `memory-vault-cleanup`
(`../ensure-memory-vault-cleanup.sh`) is an agent-turn cron 20 minutes later
on the `kit` agent: it runs the vault's model jobs M1-M4 and M6 on the findings
this cron just wrote (`audits/findings.json` and two sections of
`audits/latest.md`), commits each job as `memory-model` with a `model:` subject,
and delivers its owner calls through notify-relay's envelope
(`docs/message-format.md`) only when they change. See `docs/runbook.md` "The
memory-vault-cleanup cron".
