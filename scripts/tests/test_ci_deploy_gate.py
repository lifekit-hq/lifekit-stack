"""The CI deploy job's `if:` must survive a skipped ancestor in its needs chain.

`changes` runs on pull requests only, so on push/dispatch it is skipped and
`secrets-gateway-decrypt` (always()) succeeds behind it. A `deploy.if` without
a status function is skipped by GitHub because of that skipped ancestor, which
silently stopped every deploy from 2026-09-30 (#252). This evaluates the real
expression against job-result scenarios, so that class stays caught.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest
import yaml

CI = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
JOBS = yaml.safe_load(CI.read_text())["jobs"]
DEPLOY = JOBS["deploy"]
NEEDED = DEPLOY["needs"]


def deploys(*, ref, event, results, cancelled=False):
    """Mirror GitHub's gate: a job with no status function in its `if:` is
    skipped when any needed job did not succeed; otherwise the expression
    decides."""
    expr = str(DEPLOY["if"]).strip().removeprefix("${{").removesuffix("}}").strip()
    has_status_fn = bool(re.search(r"\b(always|success|failure|cancelled)\(\)", expr))
    if not has_status_fn and any(r != "success" for r in results.values()):
        return False
    py = re.sub(r"needs\.([\w-]+)\.result", r"needs['\1']", expr)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = py.replace("!cancelled()", "(not cancelled)").replace("always()", "True")
    py = py.replace("github.ref", "ref").replace("github.event_name", "event")
    return bool(eval(py, {"__builtins__": {}}, {  # noqa: S307 - trusted workflow text
        "needs": results, "ref": ref, "event": event, "cancelled": cancelled,
    }))


def all_ok(**over):
    results = {job: "success" for job in NEEDED}
    results.update(over)
    return results


def test_deploy_needs_the_skipped_changes_chain():
    # Guards the premise: the chain really has a PR-only ancestor.
    assert "changes" in JOBS["secrets-gateway-decrypt"]["needs"]
    assert JOBS["changes"]["if"] == "github.event_name == 'pull_request'"
    assert "secrets-gateway-decrypt" in NEEDED


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_deploys_on_main_when_every_needed_job_succeeded(event):
    assert deploys(ref="refs/heads/main", event=event, results=all_ok())


@pytest.mark.parametrize("job", NEEDED)
@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
def test_no_deploy_when_a_needed_job_did_not_succeed(job, result):
    assert not deploys(
        ref="refs/heads/main", event="push", results=all_ok(**{job: result})
    )


def test_no_deploy_when_cancelled():
    assert not deploys(
        ref="refs/heads/main", event="push", results=all_ok(), cancelled=True
    )


@pytest.mark.parametrize(
    ("ref", "event"),
    [
        ("refs/heads/main", "pull_request"),
        ("refs/heads/main", "schedule"),
        ("refs/heads/release-please--branches--main", "workflow_dispatch"),
        ("refs/pull/1/merge", "pull_request"),
    ],
)
def test_never_deploys_off_main_push_or_dispatch(ref, event):
    assert not deploys(ref=ref, event=event, results=all_ok())


def test_the_old_expression_would_have_been_skipped():
    # The harness itself must catch the regression: a status-function-free
    # `if:` with a skipped ancestor never runs.
    old = DEPLOY["if"]
    try:
        DEPLOY["if"] = (
            "github.ref == 'refs/heads/main' && (github.event_name == 'push'"
            " || github.event_name == 'workflow_dispatch')"
        )
        assert not deploys(
            ref="refs/heads/main", event="push", results=all_ok(changes="skipped")
        )
    finally:
        DEPLOY["if"] = old
