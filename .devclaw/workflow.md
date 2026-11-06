# devclaw workflow - lifekit-stack

How work is planned, built and shipped in this repository. Correct this page in
place when a change makes it wrong; never append to it.

## Planning

**No planning harness.** There is no `specs/`, no SpecKit, no plan directory: an
issue in `lifekit-hq/lifekit-stack` is the contract and the PR description is the
plan (`AGENTS.md` -> "Conventions"). One reviewable increment per session.

- Branch `<type>/<issue#>-<slug>`, created with `gh issue develop <n>` (devclaw
  goals arrive on `goal/<slug>-<issue#>` instead - keep that branch).
- A load-bearing decision goes in the `docs/` page that owns the subject
  (`docs/resource-budget.md`, `docs/runbook.md`, `docs/platform-contract.md`...),
  with the date and, when it suspends a review, what restarts it. Only
  `README.md` and `CLAUDE.md` live at the repo root; never add a root `PLAN.md`.
- The PR body says what + why and carries a **Validation** section.

## Build / test / verify

`.devclaw/verify` runs what CI's `lint`, `pytest`, `notify-relay-tests` and
`gitleaks-full` jobs run (see `.github/workflows/ci.yml`). Run it before handing
back. It provisions what it needs into `.devclaw/.venv` (gitignored): the venv,
pre-commit, the pinned jq and rsync the shell-script tests shell out to, and the
pinned promtool the alert-rule tests evaluate expressions in.

It refuses rather than noting when a required CI job is beyond it - that is the
rule the script is built on, since a green that covers an unrun job is worth
nothing. Expect a hard failure, and hand back `BLOCKED: env`, when the branch
touches a Dockerfile (hadolint is a docker hook), when it touches an input of
`secrets-gateway-decrypt`, when node is missing, or when the base revision will
not resolve.

What no worktree can run - do not try, and do not fake:

- **`secrets-gateway-decrypt`**: needs the on-box gateway age key, which never
  leaves the self-hosted runner. CI runs it only when one of its three inputs
  changes, and `verify` refuses when the branch is one of those.
- **`deploy`**: `main` only, on the VPS runner (`scripts/deploy.sh`). Deploying
  is never a laptop or worker command.
- **The box itself**: a worker container has no docker and no reachable
  Prometheus - see `.devclaw/memory/worker-has-no-docker-or-prometheus.md` for
  which checks degrade and what to hand back instead of guessing a measurement.

## Conventions

- Conventional commits; scope = area (`fix(deploy):`, `feat(skills):`).
  release-please parses them into the CHANGELOG, so the subject is the change.
- Every change lands via squash-merged PR with CI green; `main` deploys on merge.
- Gates are hard, no soft-fail: pre-commit, gitleaks full history, the pytest
  suites, the platform contract (deploy-time), doc drift, and the human privacy
  pass (`docs/PRIVATE.md` - no names, ids, schedules or chat ids in the repo;
  skills stay `{{ user.* }}`-templated).
- A new compose service carries `lifekit.contract` labels or the deploy stops
  (`docs/platform-contract.md`).

## Traps

`.devclaw/MEMORY.md` - one durable repo fact per file. Read a fact when its hook
bears on your task.
