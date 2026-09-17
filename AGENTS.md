# lifekit-stack — Claude Context

Reference deployment for a personal-AI stack: bash bootstrap + Docker Compose that put
[OpenClaw](https://openclaw.ai/) + [lifekit](https://github.com/lifekit-hq/lifekit) + parameterized
workspace skills on a fresh VPS (reference: Hetzner CX22, Ubuntu 24.04, Tailscale-only, loopback
binds — no public ingress). Maintainer: Denys. Pre-release v0.x.

| Piece | What | Where it runs |
| --- | --- | --- |
| `compose/` | `docker-compose.yml` + Dockerfiles: openclaw-gateway, openclaw-cli (profile `cli`), lifekit-orchestrator, notify-relay, google-workspace-mcp, plus the box observability stack (prometheus, loki, grafana, node-exporter, otel-collector, tempo) and its `observability/` config — datasources, dashboard providers, and the **provisioned alert rules**. New observability services join both `default` and the external `lifekit-shared` network only if a cross-project caller needs them (otherwise `default` alone); Grafana provisioning (alerting, datasources) needs a `deploy.sh` reload after merge since Grafana reads it at startup only. | The VPS, as compose project |
| `scripts/` | `bootstrap-vps.sh` (host setup), `deploy.sh` (idempotent redeploy), `check-doc-drift.sh`, memory-audit cron wrapper (audit logic lives in the vault `bin/`), `quota-share/` (read-only report: shares quota-axi's account % across consumers by logged Claude Code session usage — see `scripts/quota-share/README.md`), sync tooling | The VPS |
| `skills/` | Jinja2-templated workspace skills (`{{ user.* }}` substitutions — never baked personal data) | OpenClaw workspace |
| `defaults/` | Agent workspace defaults (AGENTS.md contract, modules.yaml, openclaw config) | OpenClaw workspace |

## Commands

```bash
pre-commit run --all-files                 # THE local gate — exactly what CI's lint job runs
python3 -m venv .venv && .venv/bin/pip install --quiet pytest
.venv/bin/python -m pytest compose/container-exporter/tests scripts/quota-share/tests scripts/tests   # the CI tests job
bash scripts/check-doc-drift.sh            # README <-> compose service-count parity
```

Deploying is **not** a laptop command: every merge to `main` deploys via the CI `deploy` job on the
VPS self-hosted runner (`git reset --hard origin/main && bash scripts/deploy.sh` in
`/srv/lifekit-stack`, as the `lifekit` account). Manual path: `workflow_dispatch` on CI, or run
`scripts/deploy.sh` on the VPS directly — it is idempotent. Host-level changes use the `denys`
account (sudo); see README "VPS users".

The live OpenClaw config (`/srv/openclaw/config/openclaw.json`) has two halves. Platform keys
(logging, diagnostics, plugin enables, gateway auth rate limit, heartbeat) live in
`compose/openclaw-gateway/platform.patch.json`: `deploy.sh` compares it with the live file, applies
it with `openclaw config patch` only when a key differs, and recreates the gateway only when the
CLI's apply hint says the changed keys need it — a new platform key goes there, never in a PR
body. The personal half (`agents.entries`, `channels`, auth profiles, MCP tokens) stays host
state, hand-edited and only mirrored to a separate repo; a change that needs it ships as an
operator step (an `openclaw config patch` snippet) in the PR description. Check keys against the
installed schema (`openclaw config schema` inside the gateway), not just docs.openclaw.ai. Official
plugins that need core capabilities (e.g. diagnostics exporters) only get them as a trusted npm
install in the state dir (`openclaw plugins install @openclaw/<id>@<core version>`); a copy baked
into the image or loaded via `plugins.load.paths` loads untrusted and silently gets nothing.

## Mandatory gates (every PR, no soft-fail)

- **Lint** = `pre-commit run --all-files`: gitleaks (secrets), hygiene hooks, yamllint,
  shellcheck, hadolint (`--failure-threshold error`), ruff + ruff-format, doc-drift.
- **Gitleaks full-history scan** (OSS binary, pinned to the pre-commit rev).
- **Tests**: the container-exporter, quota-share and platform-contract pytest suites (the memory-audit logic moved to the vault `bin/` on 2026-09-14 - shellcheck covers its wrapper).
- **Platform contract** (deploy-time, not CI): every compose service carries `lifekit.contract` labels
  (`v1` or `none`), or `deploy.sh` stops before `up`; a `v1` service that fails an enforced item
  at runtime turns the deploy red. No waivers - fix the service, or add the item to `ENFORCED`
  only with the platform piece it needs. See [`docs/platform-contract.md`](./docs/platform-contract.md).
- **Doc drift** (separate workflow): README service table must match `compose/docker-compose.yml`.
- Privacy is a gate too: read [`docs/PRIVATE.md`](./docs/PRIVATE.md) before committing — gitleaks
  catches secrets, the human pass catches personal context. Skills must be `{{ user.* }}`
  templated; no names, IDs, schedules, or account details in the repo, ever.

## Conventions (ecosystem-standard)

- **Branch**: `<type>/<issue#>-<slug>` (e.g. `fix/94-deploy-abort-banner`); create via
  `gh issue develop <n>`.
- **Commits / PR titles**: conventional commits — release-please parses them into the CHANGELOG.
  Scope = area: `fix(deploy): …`, `feat(skills): …`, `feat(memory-audit): …`.
- **PRs**: body says what + why and carries a **Validation** section; squash-merge, CI green first.
- **Issues**: imperative title, no priority prefix — priority lives in the `P1`/`P2` label. P1
  issues carry traceability → acceptance criteria → shape; P2/P3 stay one-liners until promoted.
- **Milestones**: `M<n> — <outcome>`, named for the outcome, never a date.
- **Releases**: release-please maintains the release PR (version + CHANGELOG); the Weekly Release
  workflow merges it Mondays 08:00 UTC (or dispatch manually for "release now").
- Main is protected in spirit: all changes land via squash-merged PR, CI green first.
- Only `README.md` and `CLAUDE.md` belong at the repo root — durable docs go to `docs/`
  (tool-managed files are the exception: `CHANGELOG.md` + `version.txt` are release-please's,
  `LICENSE` stays, dotfiles/tool configs are fine).

### Gold-standard divergences

- **Deploys hang off push-to-main, not release-created.** This repo *deploys* (the maintainer's
  VPS tracks `main` continuously); releases exist for template consumers — version + CHANGELOG
  cadence — not as the deploy trigger. The Weekly Release still dispatches CI after merging the
  release PR, so the release commit deploys like any other.
- **No build gate in CI.** Docker images are not built per-PR (the runner is the 4 GB production
  VPS); hadolint lints every Dockerfile on every PR, and the images build at deploy.
- **CI runs on the VPS self-hosted runner** (deploy must; lint/tests follow it — the runner has no
  provisionable Python, hence the throwaway-venv pattern in `ci.yml`). The release workflows run
  on GitHub-hosted runners, matching finance-sentry's shape.
- **hadolint at `--failure-threshold error`**: accepted warning backlog (DL3006/DL4006/DL3016/
  DL3042) on working prod images — warnings print but don't block; revisit rather than loosen.
- **No repo-local CODE_OF_CONDUCT.md** — the org default in
  [lifekit-hq/.github](https://github.com/lifekit-hq/.github) applies. Extra house rule for this
  repo: personal-AI discussions touch personal data — treat details others share in issues with
  discretion.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
