# Post-upgrade smoke checks

One check per upgrade pillar, run on the VPS right after that pillar is
upgraded. Each is a script that prints exactly one summary line and exits
0 (pass), 1 (fail) or 2 (usage error). Correctness is decided by the script;
nothing here asks a model to judge. A later upgrade script calls the check
for the pillar it just upgraded. A pass is what lets a routine upgrade stay
silent; a fail starts the rollback. These scripts do not detect, apply or
roll back upgrades.

```bash
scripts/upgrade-smoke/openclaw.sh    [--expect-version 2026.9.8]
scripts/upgrade-smoke/claude-code.sh [--expect-version 2.1.303] [--no-call]
scripts/upgrade-smoke/firstmate.sh   [--expect-no-mistakes-version v1.84.0]
```

Summary line shape: `upgrade-smoke <pillar>: PASS|FAIL <key=value ...> [red=<reason,...>]`.
It holds versions, counts and check names, and never a config value, token or reply text.
`--expect-version` proves the upgraded version is the one actually running.
Without it, the check only proves that whatever is running is healthy.

All three are read-only against production. They never restart, recreate or
reconfigure a container or the gateway, never run a `--fix`, and never edit a
firstmate checkout. Only `claude-code.sh` calls a model, and it makes one call.

## openclaw.sh: OpenClaw

Run as any account in the `docker` group. Takes about 80 s, most of it `doctor --json`.
Every command runs inside the running gateway container (found by its compose
labels, as `scripts/deploy-openclaw.sh` does) with `docker exec`.

| Proves | How | Red reason |
| --- | --- | --- |
| The gateway container is up and its healthcheck passes | `docker inspect` state + health | `gateway-not-found`, `container-<state>` |
| The upgraded version is running | `openclaw --version` | `version-unreadable`, `version-not-<v>` |
| The gateway answers on its host port | `GET /healthz`, the endpoint the healthcheck and deploy use | `healthz` |
| Plugins loaded, model and child runtimes usable, every enabled channel account connected | `openclaw health --json` | `plugin-errors`, `plugins-unavailable`, `model-runtime-degraded`, `child-runtime-unavailable`, `channels-down` |
| No plugin-compat drift from the upgrade | `openclaw doctor --post-upgrade --json`: any finding fails | `post-upgrade-findings` |
| Every configured MCP server connects | `openclaw mcp probe --json` | `mcp-probe` |
| No error-class doctor finding | bare `openclaw doctor --json` (OpenClaw's read-only advisory set): `error`/`critical`/`high`/`fatal` fails | `doctor:<checkId,...>` |

The check makes no model call. Doctor warnings are tolerated, because the live
install has standing ones, such as the by-design `lan` bind. A single MCP server
being down also fails the check, since the probe cannot tell an upgrade fault
from an outage. Only a real agent turn proves that the agent runtimes answer,
and a turn writes a session, so this check does not run one. The deploy's smoke
turn (`openclaw_phase_smoke` in `scripts/deploy-openclaw.sh`) covers that on
every image bump. The fuller pre-merge proof is `scripts/rehearse-openclaw-bump.sh`
(the "Rehearse OpenClaw bump" workflow). It runs `doctor --fix` on a copy of the
live state before an image bump merges. This check is the one that runs after the bump.

## claude-code.sh: Claude Code

Run as the account whose Claude Code was upgraded. It uses that account's own login.
Takes about 4 s.

| Proves | How | Red reason |
| --- | --- | --- |
| The CLI runs and the upgraded version is the one on PATH | `claude --version` | `version-unreadable`, `version-not-<v>` |
| It still authenticates and completes a non-interactive turn | one `claude -p` asking for `pong`, JSON output: must be a non-error result whose reply is `pong` | `turn-exit-<rc>`, `turn-no-json`, `turn-bad-reply` |

Cost: one minimal turn per run. It uses the cheapest model (`haiku`, override
with `SMOKE_MODEL`) with no tools, no MCP servers and no settings sources, so
no hooks run. It runs from an empty directory, so no CLAUDE.md loads, and it
saves no session. Measured on 2026-10-19: about 3.7k input tokens (mostly cache),
4 output tokens, a reported 0.0006 USD, about 4 s. The summary carries the
reported cost. `--no-call` checks only the version.

## firstmate.sh: firstmate and its no-mistakes toolchain

Run as the firstmate account. Takes about 5 s. `FIRSTMATE_ROOT` (code root,
default `~/firstmate`) and `FIRSTMATE_HOME` (live home, default `~/fm-vps`) name
the two checkouts.

| Proves | How | Red reason |
| --- | --- | --- |
| no-mistakes runs at the expected version | `no-mistakes --version` | `no-mistakes-version-unreadable`, `no-mistakes-not-<v>` |
| Its dependencies, database, daemon and gate agent are healthy | `no-mistakes doctor`: exit 0, no ✗ line, `git`/`gh`/`database`/`daemon` all ✓ | `no-mistakes-doctor-exit-<rc>`, `no-mistakes-doctor:<names>` |
| The shared daemon is up | `no-mistakes daemon status` (read only, never start/stop) | `no-mistakes-daemon` |
| Both checkouts are git trees on a branch | `git --no-optional-locks symbolic-ref` | `root-not-on-branch`, `home-not-on-branch` |
| The home's scripts still parse | `bash -n` on every `bin/*.sh` | `home-bin-syntax-<n>` |
| firstmate's own tool floors and config still hold | the home's `bin/fm-bootstrap.sh` with `FM_BOOTSTRAP_DETECT_ONLY=1 FM_BOOTSTRAP_NETWORK=skip`, the read-only detect path a second concurrent session uses: a `MISSING`, `MISSING_MANUAL`, `BACKEND_INVALID`, `CREW_DISPATCH`, `STARTUP_MEMORY_BUDGET` or `TANGLE` line fails | `bootstrap-exit-<rc>`, `bootstrap:<prefixes>` |

Other bootstrap lines are routine and do not fail the check. These include
`BOOTSTRAP_INFO`, home-summary and backlog-reconcile notes. The summary
counts them as `bootstrap-notes`. The check makes no model call.

## Tests

`scripts/tests/test_upgrade_smoke.py` runs each script against stubbed
`docker`, `curl`, `claude` and `no-mistakes` on PATH, plus a throwaway
firstmate root and home. Each script passes on a healthy pillar and fails with
the named reason on each broken one. The OpenClaw tests also check that it
issues only `ps`, `inspect` and `exec`, and no fix, restart or agent turn.
The firstmate tests check that it leaves both checkouts untouched.
