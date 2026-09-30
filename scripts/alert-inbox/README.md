# alert-inbox

Pulls firing Grafana alerts into an agent's inbox on transitions only, without
a new listener, a Grafana contact-point change, or Grafana admin credentials.

## Why pull, not push

A 2026-09-29 read-only monitoring check found every Grafana alert routes to
exactly one Telegram chat on the owner's phone, so nothing reaches an agent.
Grafana's HTTP API needs admin credentials this account cannot read and must
not use. Prometheus's query API (`http://127.0.0.1:9090`, loopback-bound,
unauthenticated) already scrapes Grafana's own alerting metrics, but those
are counts only — `grafana_alerting_alerts{state=...}` has no rule name
(confirmed live, 2026-09-29). So `poll_alerts.py` evaluates the provisioned
rules' own PromQL directly against Prometheus, reading the expression and
threshold from `compose/observability/grafana/provisioning/alerting/rules.yml`
rather than hand-copying thresholds into a second place they can drift from
it. Every current rule (checked 2026-09-29) uses the same shape — one raw
PromQL query, `reduce(last, dropNN)`, a single `lt`/`gt` threshold — and a
rule that uses anything wider (multi-step math, classic conditions) is
skipped with a stderr warning rather than silently misjudged.

`for:` (the rule's own pending duration before Grafana would actually fire)
is approximated as wall-clock elapsed since this poller first saw the
condition true, using its own state file. That is coarser than Grafana's
continuous evaluation — it is quantized to how often this script runs — but
it reuses each rule's own `for:` value instead of inventing a new one.

## What it does

Each run:

1. Reads `rules.yml`, evaluates every rule's PromQL expression as an instant
   query against Prometheus.
2. Advances a small per-series state machine (`normal` -> `pending` ->
   `alerting` -> `normal`) kept in a JSON file under
   `~/.local/state/lifekit-alert-inbox/state.json` (`--state-file` to
   override).
3. Calls `fm-inbox.sh note` exactly once per transition: a rule starting to
   fire ("alert firing: ..."), or the same rule resolving ("alert
   resolved: ..."). An already-firing rule is silent on every subsequent
   poll — no repeat notifications.
4. If Prometheus cannot be reached at all, that is its own transition
   (`prometheus-unreachable` starting/resolving), so an outage is reported
   once, not every poll.

Out of scope, by design: Grafana contact points or notification policies,
any new network listener or service, `/etc`, the RAM reaction automation,
and the external heartbeat (`docs/runbook.md` "External heartbeat
(dead-man)") — all unrelated to this pull.

## Run it

```bash
python3 scripts/alert-inbox/poll_alerts.py --dry-run   # prints instead of notifying
python3 scripts/alert-inbox/poll_alerts.py \
  --fm-inbox-bin /path/to/fm-inbox.sh \
  --fm-home /path/to/target/home
```

`--fm-inbox-bin` (or `FM_INBOX_BIN`) and `--fm-home` (or `FM_INBOX_HOME`) have
no default in this repo — nothing here hard-codes a path outside it. The
install step below is where an operator supplies both.

Flags: `--rules-file` (default: this repo's `rules.yml`), `--prometheus-url`
(default: `http://127.0.0.1:9090`), `--state-file`.

## Install (systemd timer)

`bootstrap-vps.sh` installs it; on a live box run the installer alone as root:

```bash
sudo FM_INBOX_BIN=/path/to/fm-inbox.sh FM_INBOX_HOME=/path/to/target/home \
  bash scripts/alert-inbox/install-alert-inbox.sh
```

It writes the two variables to `/etc/lifekit/alert-inbox.env`, installs
`alert-inbox.service` + `alert-inbox.timer` (every 5 minutes) running as
`ALERT_INBOX_USER` (default `ADMIN_USER`, then `denys`), and skips with a
message if neither the variables nor an existing env file are present. Logs
are in the journal (`journalctl -u alert-inbox`).

## Tests

```bash
.venv/bin/python -m pytest scripts/tests/test_poll_alerts.py
```

Pure-logic tests only (duration parsing, rule parsing against the real
`rules.yml`, threshold evaluation, the state machine, and an end-to-end
`poll()` run against a stubbed Prometheus and a stubbed `fm-inbox.sh`) — no
test touches a live Prometheus or sends a real notification.
