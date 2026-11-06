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
- `scripts/tests/test_alert_rules.py`'s promtool cases skip without docker or a
  `promtool` binary - a run with dozens of skips there is the norm off-box, not a
  broken suite; the YAML-level rule tests still run.
- CI's `notify-relay-tests` job runs `node --test` inside `node:22-trixie-slim`;
  locally the same suite runs against this image's node 22.
- The image has no `jq` and no `rsync`, and the tests in `scripts/tests` that
  drive repo shell scripts (and the stub `gh` they install) shell out to both:
  without them they fail with `command not found`, not with a repo bug. CI's
  `ubuntu-latest` runner ships both; `verify` provisions them into its venv
  from pinned, checksum-verified sources (jq's release binary, rsync built from
  the release tarball - upstream publishes no static rsync).

Anything that needs a **measurement from the live box** (a 7-day memory peak, a
container's footprint, `up{job=...}`) cannot be produced here. Do not invent a
number and do not quietly relabel it: ship the sized estimate with the date and
the condition that replaces it, say so in the commit, or hand back
`BLOCKED: env` when an estimate will not do.
