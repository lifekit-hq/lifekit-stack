"""Behaviour of scripts/release-gauge/lifekit-release-gauge.sh against a fake gh."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/release-gauge/lifekit-release-gauge.sh"
SLUG = "lifekit-hq/finance-sentry"
RUN_URL = "https://github.com/lifekit-hq/finance-sentry/actions/runs/38209202428"
PR_URL = "https://github.com/lifekit-hq/finance-sentry/pull/851"
STARTED = "2026-10-12T05:43:38Z"

# Fake gh: answers `gh api .../runs` from STATE_DIR/<owner>_<name>.runs.json and
# `gh pr list` from STATE_DIR/<owner>_<name>.prs.json, applying --jq with the
# real jq; STATE_DIR/_broken fails every call like an expired login. Every
# call is logged to STATE_DIR/_calls.
FAKE = r"""#!/usr/bin/env bash
echo "$*" >> "$STATE_DIR/_calls"
[[ -f "$STATE_DIR/_broken" ]] && { echo "HTTP 401: Bad credentials" >&2; exit 1; }
jqexpr=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ "${args[i]}" == --jq ]] && jqexpr="${args[i+1]}"
  [[ "${args[i]}" == -R ]] && repo="${args[i+1]}"
done
if [[ "$1" == api ]]; then
  repo="$(sed -E 's#^repos/([^/]+/[^/]+)/.*#\1#' <<< "$2")"
  file="$STATE_DIR/${repo/\//_}.runs.json"
  default='{"workflow_runs": []}'
else
  [[ "$*" == *'--label autorelease: pending'* && "$*" == *'--state open'* ]] || exit 3
  file="$STATE_DIR/${repo/\//_}.prs.json"
  default='[]'
fi
if [[ -f "$file" ]]; then jq -r "$jqexpr" "$file"; else jq -r "$jqexpr" <<< "$default"; fi
"""


@pytest.fixture
def gauge(tmp_path):
    state, out, bin_dir = tmp_path / "state", tmp_path / "out", tmp_path / "bin"
    for d in (state, out, bin_dir):
        d.mkdir()
    fake = bin_dir / "gh"
    fake.write_text(FAKE)
    fake.chmod(0o755)

    def runs(repo, conclusion, started=STARTED, url=RUN_URL):
        rows = (
            []
            if conclusion is None
            else [
                {"conclusion": conclusion, "run_started_at": started, "html_url": url}
            ]
        )
        (state / f"{repo.replace('/', '_')}.runs.json").write_text(
            json.dumps({"workflow_runs": rows})
        )

    def prs(repo, *rows):
        (state / f"{repo.replace('/', '_')}.prs.json").write_text(
            json.dumps([{"url": u, "createdAt": c} for u, c in rows])
        )

    def run(repos=SLUG, expect=0):
        env = {k: v for k, v in os.environ.items() if k != "RELEASE_REPOS"}
        env |= {"PATH": f"{bin_dir}:{env['PATH']}", "STATE_DIR": str(state)}
        if repos is not None:
            env["RELEASE_REPOS"] = repos
        proc = subprocess.run(
            ["bash", str(SCRIPT), str(out)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == expect, proc.stderr
        if expect:
            return proc
        parsed = {}
        for line in (out / "weekly_release.prom").read_text().splitlines():
            if line and not line.startswith("#"):
                name, value = line.rsplit(" ", 1)
                parsed[name] = int(value)
        return parsed

    return runs, prs, run, state, out


def blocked(repo=SLUG, pr="", run=""):
    return f'weekly_release_blocked{{repo="{repo}",pr_url="{pr}",run_url="{run}"}}'


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_failed_run_with_its_release_pr_still_open_is_blocked(gauge, conclusion):
    runs, prs, run, *_ = gauge
    runs(SLUG, conclusion)
    prs(SLUG, (PR_URL, "2026-10-05T09:20:00Z"))
    assert run() == {blocked(pr=PR_URL, run=RUN_URL): 1}


def test_successful_run_is_not_blocked_and_never_reads_prs(gauge):
    # A merged release and the "no pending release PR" no-op both end green.
    runs, prs, run, state, _ = gauge
    runs(SLUG, "success")
    prs(SLUG, (PR_URL, "2026-10-05T09:20:00Z"))
    assert run() == {blocked(): 0}
    assert "pr list" not in (state / "_calls").read_text()


def test_failed_run_without_an_open_release_pr_is_not_blocked(gauge):
    runs, prs, run, *_ = gauge
    runs(SLUG, "failure")
    prs(SLUG)
    assert run() == {blocked(): 0}


def test_release_pr_opened_after_the_failed_run_is_not_blamed_on_it(gauge):
    runs, prs, run, *_ = gauge
    runs(SLUG, "failure")
    prs(SLUG, (PR_URL, "2026-10-13T10:00:00Z"))
    assert run() == {blocked(): 0}


def test_repo_without_any_completed_run_is_not_blocked(gauge):
    runs, _, run, *_ = gauge
    runs(SLUG, None)
    assert run() == {blocked(): 0}


def test_every_repo_gets_a_series(gauge):
    runs, prs, run, *_ = gauge
    other = "lifekit-hq/devclaw"
    runs(SLUG, "failure")
    prs(SLUG, (PR_URL, "2026-10-05T09:20:00Z"))
    runs(other, "success")
    assert run(repos=f"{SLUG} {other}") == {
        blocked(pr=PR_URL, run=RUN_URL): 1,
        blocked(repo=other): 0,
    }


def test_default_repos_are_the_four_weekly_release_repos(gauge):
    *_, run, _, _ = gauge
    assert {name.split('"')[1] for name in run(repos=None)} == {
        "lifekit-hq/finance-sentry",
        "lifekit-hq/lifekit-stack",
        "lifekit-hq/lifekit-dashboard",
        "lifekit-hq/devclaw",
    }


def test_read_failure_keeps_the_previous_file(gauge):
    runs, prs, run, state, out = gauge
    runs(SLUG, "failure")
    prs(SLUG, (PR_URL, "2026-10-05T09:20:00Z"))
    run()
    before = (out / "weekly_release.prom").read_text()
    (state / "_broken").write_text("")
    proc = run(expect=1)
    assert "Bad credentials" in proc.stderr
    assert (out / "weekly_release.prom").read_text() == before
    assert [p.name for p in out.iterdir()] == ["weekly_release.prom"]


def test_unexpected_url_fails_instead_of_writing_a_broken_label(gauge):
    runs, prs, run, _, out = gauge
    runs(SLUG, "failure", url='https://x/"} 1')
    prs(SLUG, (PR_URL, "2026-10-05T09:20:00Z"))
    run(expect=1)
    assert not (out / "weekly_release.prom").exists()


def test_installer_and_units_wire_the_gauge():
    installer = (REPO / "scripts/host-gauge/install-host-gauges.sh").read_text()
    assert "release-gauge/lifekit-release-gauge" in installer
    assert "lifekit-release-gauge.timer" in installer
    unit = (REPO / "scripts/release-gauge/lifekit-release-gauge.service").read_text()
    assert "User=__ADMIN_USER__" in unit
    assert (
        "ExecStart=/usr/local/bin/lifekit-release-gauge.sh /var/lib/node_exporter/textfile"
        in unit
    )
    timer = (REPO / "scripts/release-gauge/lifekit-release-gauge.timer").read_text()
    # textfile-collector-stale judges every 5-minute gauge against 15 minutes.
    assert "OnUnitActiveSec=5min" in timer
    assert os.access(SCRIPT, os.X_OK)
