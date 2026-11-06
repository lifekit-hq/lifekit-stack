# A worker container has no docker and no Prometheus

devclaw worktrees run in a container on the VPS, not on the VPS: there is no
`docker` binary, no host filesystem (`/srv` is empty, `/var/lib/node_exporter`
absent) and no route to the box's observability stack - Prometheus, Loki and
Grafana are loopback-bound, so `127.0.0.1:9090` is this container's own
loopback, not the box's. Outbound internet works; `gh` has no credentials.

Consequences, all handled in `.devclaw/verify`:

- The `shellcheck-precommit` and `hadolint-docker` pre-commit hooks are docker
  hooks. `verify` skips them, runs `shellcheck` from `shellcheck-py` pinned to
  the hook's version over the same file set (selected with pre-commit's own
  `identify`), and refuses to go green when the branch touched a Dockerfile it
  cannot lint.
- `scripts/tests/test_alert_rules.py` evaluates rules in promtool, reaching for a
  `promtool` on PATH before falling back to the `prom/prometheus` image - which
  it never pulls. Without either, 44 of its 78 tests skip, **and that is what CI
  does**: only `secrets-gateway-decrypt` and `deploy` are `self-hosted`, every
  other job including `pytest` runs on `ubuntu-latest` with neither a promtool
  nor that image (AGENTS.md's "lint/tests follow it onto the VPS runner" is
  stale on this point - check `ci.yml`, not the prose). So a green CI says
  nothing about whether the rule expressions parse. `verify` provisions the
  pinned static promtool from the Prometheus release tarball, reading the version
  from `PROMTOOL_IMAGE` in that test file so there is only one pin; it is the
  only place those 44 run, and a skip there now means something is wrong.
- CI's `notify-relay-tests` job runs `node --test` inside `node:22-trixie-slim`;
  locally the same suite runs against this image's node 22.
- The image has no `jq` and no `rsync`, and the tests in `scripts/tests` that
  drive repo shell scripts (and the stub `gh` they install) shell out to both:
  without them they fail with `command not found`, not with a repo bug. CI's
  `ubuntu-latest` runner ships both; `verify` provisions them into its venv
  from pinned, checksum-verified sources (jq's release binary, rsync built from
  the release tarball - upstream publishes no static rsync).

A promtool is downloadable, in other words, and a Prometheus is not: the rules
can be checked off-box, the box's own series cannot.

Anything that needs a **measurement from the live box** (a 7-day memory peak, a
container's footprint, `up{job=...}`) cannot be produced here. Do not invent a
number and do not quietly relabel it: ship the sized estimate with the date and
the condition that replaces it, say so in the commit, or hand back
`BLOCKED: env` when an estimate will not do.
