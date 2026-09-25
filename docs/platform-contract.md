# Platform contract (guardrail 1)

Every product on the box meets one contract: **health + readiness, metrics that
reach Prometheus, JSON logs with a trace id, OTLP traces, sits behind the edge,
owns its topics.** Guardrail 1 (adopted 2026-09-16) enforces it at deploy time,
and there are **no waivers**. The checker is
[`scripts/platform-contract.py`](../scripts/platform-contract.py) (stdlib Python,
read-only). Its docstring is the label reference.

## Who runs it

- **This repo's `deploy.sh`** runs it twice on its own compose project:
  - Before `up`, `--static` checks the `docker compose config` declaration. A
    service with a missing or inconsistent declaration stops the deploy before
    anything changes.
  - After the smoke turns, the runtime check probes the running containers. A
    failure turns the deploy red; the containers are already up, like every
    other post-deploy assertion. The rest of the box prints as a census that
    never fails this deploy. Undeclared containers of other projects (xui, for
    example) are reported only.
- **Each product's deploy** runs the same script on its own project. The call
  lands in the PR that makes that product conform, never before:

  ```bash
  CONTRACT=/srv/lifekit-stack/scripts/platform-contract.py
  # No `|| true` and no existence check that skips: a missing script fails the deploy.
  docker compose -f <compose file> config --format json | python3 "${CONTRACT}" --static -   # before up
  python3 "${CONTRACT}" --project "<compose project>"                                        # after the health wait
  ```

  Both runner users (`lifekit`, `denys`) can read `/srv/lifekit-stack`.

## Declaring a service

Put service `labels:` in the product's own compose file:

- `lifekit.contract: "v1"` opts a service in. Every container that serves traffic
  is `v1`, including static nginx (it gets `/healthz`, `/readyz` and a JSON
  `log_format`).
- `lifekit.contract: "none"` opts a service out. It is for datastores, platform
  pieces, upstream images we do not change, and one-shot CLIs.
- A `v1` service declares `port`, `health`, `ready`, `metrics` and `ingress`.
- A `v1` service may also declare:
  - `ready.via: edge`: readiness answers only an attributed proxy.
  - `metrics.auth: token`: only Prometheus holds the credential for the metrics
    path.
  - `service`: the OTLP `service.name`.

## What each item checks

| Item | Runtime check | Enforced |
| --- | --- | --- |
| health | Unauthenticated GET to the container IP returns 2xx and is not `text/html` (that rules out an SPA fallback page). | yes |
| ready | Same rules as health, on a different path. With `ready.via: edge`, the check is SKIP until the edge lands; it will then probe through Traefik (captain Q7). | yes |
| metrics | GET returns Prometheus text. With `metrics.auth: token`, the up target (next row) counts instead. | yes |
| scraped | A Prometheus target with this container's address and metrics path is `up`, and its last scrape returned samples. | yes |
| labels | The scraped target's series export no `job` or `instance` label (checked as `exported_job` / `exported_instance` in Prometheus). One documented per-service exception exists in `LABEL_EXCEPTIONS`, removed once that service renames its label. | yes |
| logs | At least 90% of the last 300 stdout/stderr lines are JSON, and at least one line has a trace id (`trace_id`, `traceId`, `@tr`, `trace.id`, …). | yes |
| traces | Reported only. The trace rule is decided when the collector work settles (Q8). | SKIP |
| edge | `internal` means no host port is published. `edge` means `traefik.enable` is set and no host port is published. | SKIP until Traefik |
| topics | Nothing to check yet. | SKIP until Redpanda |

**Why `labels`:** Prometheus reserves `job` and `instance` for the scrape
target. With `honor_labels` unset (correct here; leave it that way), a metric's
own `job` label is silently renamed `exported_job`, so any panel or query that
groups by `job` collapses to one row with no error. Name the label something
else (`kind`, `queue`, ...). Do not set `honor_labels: true` to work around it.

`ENFORCED` in the script grows by one item for each platform piece, in the PR
that ships that piece. SKIP is never set per product.

## This stack's own services

- **notify-relay** meets the contract from its own code:
  - `/ready` is a cached Telegram `getMe`. It fails when the bot token or
    Telegram does, so a Telegram outage during a deploy turns the deploy red.
  - Its `/metrics` is scraped as job `notify-relay`.
  - Its logs are JSON lines that carry the caller's `traceparent` trace id or a
    fresh one.
- **openclaw-gateway** meets the contract through one host step and one file
  in this repo:
  1. **Plugins, once per host.** The `diagnostics-prometheus` and
     `diagnostics-otel` plugins must be present in the gateway with `Trust`
     reading `reason=trusted-official` (`openclaw plugins inspect <id>`; the
     install is in `docs/runbook.md`, "The diagnostics-prometheus plugin").
     Prometheus job `openclaw` counts for `metrics` and `scraped` only when
     the last scrape returned samples: an untrusted plugin copy answers with
     an empty body.
  2. **Config keys, from git.** `compose/openclaw-gateway/platform.patch.json`
     carries `logging.consoleStyle: "json"`, `diagnostics.otel` (stdout log
     records, traces to `otel-collector`) and both plugin enables. `deploy.sh`
     compares it with the live `openclaw.json`, applies it with
     `openclaw config patch` only when a key differs, and recreates the
     gateway only when the CLI's apply hint says the changed keys need it.
     Nothing is applied by hand.

     **Why two log settings:**
     - `logging.consoleStyle: "json"` is the only switch for the gateway's
       console format. OpenClaw reads it from the config file; there is no
       environment variable for it.
     - `diagnostics.otel` with `logsExporter: "stdout"` (the bundled
       `diagnostics-otel` plugin) adds OTLP log records as JSON lines. Each
       record carries `trace_id` when one is active.
     - `metrics: false` because the collector has no metrics pipeline; metrics
       reach Prometheus through `diagnostics-prometheus`.
     - The entrypoint wraps its (fallback) npm install output as JSON, so the only
       non-JSON lines left are ones OpenClaw writes itself.

  3. Before the gate merges, check the result with
     `python3 scripts/platform-contract.py --project compose`.
