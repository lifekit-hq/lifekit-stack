# quota-share

Divides quota-axi's account-wide "percent used" across the consumers of the
shared Claude Max account (captain, firstmate, devclaw, OpenClaw), using
evidence that already exists — Claude Code's own session JSONL, plus the
`devclaw_tokens_total` Prometheus metric — with no new instrumentation.

Background: `guard-45-agent-registry-quota/report.md` Part B (the scout that
proposed this tool) — lives in the firstmate operator home, not this repo.

## Run it

```bash
python3 scripts/quota-share/quota_share.py            # table
python3 scripts/quota-share/quota_share.py --json      # machine-readable
```

Runs on the VPS as the `lifekit` (or `denys`) account. Needs `quota-axi` on
`PATH`; reads the OpenClaw gateway's session logs directly if the account can
(`/home/lifekit/.claude/projects`), else falls back to `docker exec` into
`compose-openclaw-gateway-1`; reads `devclaw_tokens_total` from the local
Prometheus at `127.0.0.1:9090` if reachable. Any of those being unavailable
degrades that one consumer's numbers (a `note:`/`warnings` line says so) —
it never crashes the whole report.

It prints only percentages: no raw token counts, no credentials.

Exit code: 1 when any non-residual consumer's imputed used % is over its
configured `share` while the account's 7-day pace is `ahead`; 0 otherwise;
2 if `quota-axi` itself could not be read. **This is a report signal only —
nothing here disables a cron, holds an agent, or otherwise enforces
anything.**

## Config: `quota-shares.json`

One entry per consumer: a `share` (% of the account, captain's call — see
below) and `match` globs that identify that consumer's sessions.

- a plain glob matches this host's own `~/.claude/projects` session `cwd`
  (`~` expands to the running user's home), e.g. `"~/.treehouse/*"`.
- `lifekit:<glob>` matches a `cwd` from the OpenClaw gateway's session logs.
- `metric:<name>` adds a Prometheus counter's 7-day increase (only
  `devclaw_tokens_total` today) for a consumer that has no Claude Code
  session logs of its own.
- exactly one consumer may set `"match": "residual"` — normally the
  captain, whose PC-side usage this host cannot see at all. Its `imputed
  used %` always prints `not measured` and it can never trip the exit code.

The `share` numbers currently in the file are the placeholders from the
report's Part B proposal (captain 35 / firstmate 30 / devclaw 20 / openclaw
15). Per the captain's intent (2026-09-16): run this as a report only for a
week before setting real numbers — this tool enforces nothing regardless of
what `share` says, so changing the numbers only changes what the table and
exit code call "over".

## Tests

`tests/test_quota_share.py` covers the pure logic (usage weighting, JSONL
dedup, consumer matching, share math) with no external calls. Run with the
project's pytest command (see the repo root `AGENTS.md`), or directly:

```bash
python3 -m pytest -q scripts/quota-share/tests
```
