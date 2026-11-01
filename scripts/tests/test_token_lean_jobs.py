"""The two scripts that keep model turns out of kit's recurring jobs:
skills/workout-log/day-check.sh (the evening-day-plan command cron) and
skills/secondmate-relay/needs-you-digest.mjs (the digest's precomputed reads),
and the digest's model pin in scripts/ensure-needs-you-digest.sh.

The claw CLIs and relay.sh are stubs; nothing touches live data or the gateway.
"""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DAY_CHECK = REPO / "skills/workout-log/day-check.sh"
DIGEST = REPO / "skills/secondmate-relay/needs-you-digest.mjs"
ENSURE_DIGEST = REPO / "scripts/ensure-needs-you-digest.sh"

NUTRITION = """date: 2026-11-01
totals:
  calories: 1840.4
  protein_g: 112
goal:
  calories: 2000
  protein_g: 150
meals: 3
"""
GYM_DONE = "date: 2026-11-01\nsessions:\n  - id: a\n    muscle_group: legs\n  - id: b\n    muscle_group: legs\n  - id: c\n    muscle_group: core\n"
GYM_NONE = "date: 2026-11-01\nsessions: 0\nmessage: no workouts logged\n"


def _stub(bindir: Path, name: str, text: str, rc: int = 0) -> None:
    (bindir / f"{name}.out").write_text(text)
    path = bindir / name
    path.write_text(f'#!/bin/sh\ncat "{bindir}/{name}.out"\nexit {rc}\n')
    path.chmod(0o755)


@pytest.fixture
def day_check(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def run(nut=NUTRITION, gym=GYM_DONE, nut_rc=0, gym_rc=0, args=("2278", "160")):
        _stub(bindir, "nutrition-claw", nut, nut_rc)
        _stub(bindir, "workout-claw", gym, gym_rc)
        return subprocess.run(
            ["sh", str(DAY_CHECK), *args],
            env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )

    return run


def test_day_check_prints_the_whole_message_with_the_clis_numbers(day_check):
    r = day_check()
    assert r.returncode == 0, r.stderr
    wd = datetime.datetime.now().strftime("%a")
    assert r.stdout == (
        f"Day check | nutrition 1840kcal/2278 160g -> 112g under | gym {wd}: done (legs, core)"
        " | tomorrow's priorities?\n"
    )


def test_day_check_ignores_the_clis_own_goal_block(day_check):
    r = day_check(nut=NUTRITION.replace("calories: 1840.4", "calories: 1500"))
    assert "1500kcal/2278" in r.stdout and "2000" not in r.stdout


@pytest.mark.parametrize(
    "protein,word", [(170, "over"), (160, "on target"), (112, "under")]
)
def test_day_check_protein_verdict(day_check, protein, word):
    r = day_check(nut=NUTRITION.replace("protein_g: 112", f"protein_g: {protein}"))
    assert f"160g -> {protein}g {word} |" in r.stdout


def test_day_check_no_session(day_check):
    r = day_check(gym=GYM_NONE)
    assert "| no session |" not in r.stdout and ": no session |" in r.stdout


@pytest.mark.parametrize(
    "kwargs",
    [
        {"nut_rc": 1, "nut": "error: corrupt JSON\n"},
        {"gym_rc": 1, "gym": "error: corrupt JSON\n"},
        {"nut": "date: 2026-11-01\nmeals: 0\n"},
        {"gym": "something else\n"},
    ],
)
def test_day_check_fails_closed_instead_of_printing_a_wrong_line(day_check, kwargs):
    r = day_check(**kwargs)
    assert r.returncode == 1 and r.stdout == ""
    assert r.stderr.startswith("Day check failed:")


def test_day_check_needs_numeric_targets(day_check):
    assert day_check(args=()).returncode == 64
    assert day_check(args=("x", "160")).returncode == 64


# --- needs-you-digest.mjs ---------------------------------------------------

pytestmark_node = pytest.mark.skipif(shutil.which("node") is None, reason="no node")


def fleet(**over):
    base = {
        "generated_epoch": int(time.time()),
        "counts": {"active_children": 2, "queued_open": 3, "decisions_open": 2},
        "decisions_open": [
            {"summary": "pick a thing", "reason": "A or B", "hold_bucket": "live", "board_url": "http://b/1"},
            {"summary": "later thing", "reason": "x", "hold_bucket": "dated"},
        ],
        "queued": [
            {"hold_bucket": "aged"},
            {"hold_kind": "waiting"},
            {"hold_bucket": "dated"},
            {"hold_bucket": "blocked"},
            {},
        ],
        "landed": [
            {"completion": {"date": datetime.date.today().isoformat()}},
            {"completion": {"date": "2020-01-01"}},
        ],
    }
    base.update(over)
    return base


def run_digest(tmp_path, ledger, status, rc=0):
    relay = tmp_path / "relay.sh"
    if rc == 0:
        (tmp_path / "status.json").write_text(status)
        relay.write_text(f'#!/bin/sh\ncat "{tmp_path}/status.json"\n')
    else:
        relay.write_text("#!/bin/sh\necho 'curl: (7) refused' >&2\nexit 7\n")
    path = tmp_path / "open.json"
    if ledger is not None:
        path.write_text(ledger if isinstance(ledger, str) else json.dumps(ledger))
    return subprocess.run(
        ["node", str(DIGEST), str(path)],
        env={
            "PATH": "/usr/bin:/bin:" + str(Path(shutil.which("node")).parent),
            "NEEDS_YOU_RELAY": str(relay),
            "TZ": "UTC",
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytestmark_node
def test_digest_precomputes_ledger_and_fleet(tmp_path):
    ledger = [
        {"ask": "newer", "options": ["x", "y"], "opened": "2026-10-30"},
        {"ask": "older", "options": ["p", "q", "r"], "context": "http://c", "opened": "2026-10-01"},
    ]
    r = run_digest(tmp_path, ledger, json.dumps(fleet()))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "ledger: 2 open, oldest first",
        "- older | context: http://c | options: A) p B) q C) r",
        "- newer | options: A) x B) y",
        "fleet decisions: 1 live",
        "- pick a thing | reason: A or B | board: http://b/1",
        "later: 2 parked · 1 dated · 1 blocked",
        "fleet: 2 working · 3 queued · 1 landed today",
    ]


@pytestmark_node
def test_digest_flags_partial_view_and_unlisted_decisions(tmp_path):
    f = fleet(generated_epoch=int(time.time()) - 7200)
    f["counts"]["decisions_open"] = 5
    out = run_digest(tmp_path, [], json.dumps(f)).stdout
    assert "fleet: partial, last updated 2h ago" in out
    assert "not shown: 3" in out
    assert out.startswith("ledger: 0 open")


@pytestmark_node
def test_digest_reports_a_missing_ledger_a_bad_ledger_and_a_failed_status(tmp_path):
    out = run_digest(tmp_path, None, json.dumps(fleet())).stdout
    assert out.splitlines()[0] == "ledger: none"
    out = run_digest(tmp_path, '{"not": "array"}', json.dumps(fleet())).stdout
    assert out.splitlines()[0] == "needs-you digest failed: not a JSON array"
    r = run_digest(tmp_path, [], "", rc=7)
    assert r.returncode == 0
    assert "fleet status unavailable: curl: (7) refused" in r.stdout
    assert "fleet decisions" not in r.stdout


# --- the digest's declaration ------------------------------------------------


def test_the_digest_job_is_pinned_to_sonnet_and_verified_live():
    text = ENSURE_DIGEST.read_text()
    assert 'DIGEST_MODEL="${NEEDS_YOU_DIGEST_MODEL:-anthropic/claude-sonnet-4-6}"' in text
    assert text.count('--model "$DIGEST_MODEL"') == 2  # cron add and cron edit
    assert "payload.model=" in text  # fail-closed check of the live row
    assert "needs-you-digest.mjs" in text


def test_the_digest_prompt_no_longer_gathers_the_ledger_by_hand():
    text = ENSURE_DIGEST.read_text()
    assert "Read needs-you/open.json" not in text
    assert "relay.sh status' and read the fleet JSON" not in text
