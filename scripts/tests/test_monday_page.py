"""Behaviour of scripts/monday-page against a fake merged summary, audit report and pace.

What this file pins: every open decision is either a call with its options and
recommendation preselected, or a refused row at the top of Charted Next naming
each missing piece; the weekly scope call always exists and says when the audit
or the pace is missing; outside text never reads as a wikilink; and the payload
satisfies the board's fm-bearings-board.v1 validator when one is available
(MONDAY_PAGE_BOARD_SH names a firstmate bin/fm-bearings-board.sh).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/monday-page/monday_page.py"

spec = importlib.util.spec_from_file_location("monday_page", SCRIPT)
mp = importlib.util.module_from_spec(spec)
sys.modules["monday_page"] = mp
spec.loader.exec_module(mp)

NOW = 1793606400  # 2026-11-02T08:00:00Z, a Monday

AUDIT = """# Policy audit 2026-11-02 (Monday run)

Window: 2026-10-26 to 2026-11-02. Violations: 1. Calls and merges before 2026-10-14 are listed as pre-adoption and never counted.

- merged window: 12 merged PRs scanned, 1 on a reserved surface

## P2

- note: release issued 2026-10-28T10:00 on hold x: abc until 2026-10-28T11:00

## P3

- none

## P6

- VIOLATION: https://github.com/o/r/pull/1 [secrets] merged 2026-10-30: reserved diff merged without a recorded release
"""

PACE = {
    "ruling": "agent-behavior.md#P7",
    "start_allowed": True,
    "quota": {
        "provider": "claude",
        "plan": "max",
        "percentRemaining": 80,
        "resetsAt": "2026-11-09T06:00Z",
    },
    "quota_error": None,
    "next_release": "2026-11-03T06:00+00:00",
    "burn": False,
    "burn_from": "2026-11-08T23:00+00:00",
}


def decision(ident, reason, **extra):
    return {
        "id": ident,
        "key": ident,
        "verb": "captain-hold",
        "summary": f"Decide {ident}",
        "reason": reason,
        "hold_until": "2026-11-02",
        "hold_age_days": 4,
        "source": "backlog",
        "home_id": "fm-vps",
        **extra,
    }


GOOD = "[Tier 2 reversible] [P1 monday=2026-11-02] rec=go opts: go=Go ahead; stop=Stop here - the captain asked whether to ship"


def summary(**overrides):
    base = {
        "published_epoch": NOW - 60,
        "counts": {"queued": 2},
        "decisions_open": [decision("ship-it", GOOD)],
        "active_children": [
            {
                "id": "w1",
                "kind": "ship",
                "state": "working",
                "repo": "lifekit-stack",
                "name": "Worker one",
                "doing": "writing tests",
                "home_id": "fm-vps",
            },
            {"id": "w2", "state": "working", "home_id": "firstmate"},
        ],
        "queued": [
            {
                "id": "ready-one",
                "title": "Ready work",
                "repo": "lifekit-stack",
                "kind": "ops",
                "since": "2026-10-30",
                "hold_kind": None,
                "unresolved_blocker_ids": [],
                "home_id": "fm-vps",
            },
            {
                "id": "held-one",
                "title": "Held work",
                "repo": None,
                "since": "not-a-date",
                "hold_kind": "external",
                "hold_reason": "waiting on a release",
                "unresolved_blocker_ids": [],
                "home_id": "fm-vps",
            },
        ],
        "landed": [
            {
                "id": "done-one",
                "title": "Shipped a thing",
                "home_id": "fm-vps",
                "pr_url": "https://github.com/lifekit-hq/lifekit-stack/pull/1",
            },
            {
                "id": "done-two",
                "title": "Local note",
                "home_id": "firstmate",
                "pr_url": None,
            },
        ],
        "homes": [{"id": "fm-vps"}, {"id": "firstmate"}],
    }
    return {**base, **overrides}


def payload(s=None, audit=("Audit line", ""), pace=("Pace line", "")):
    return mp.build(s or summary(), audit, pace, NOW)


def refused(p):
    return [row for row in p["charted"] if row.get("kind") == "warning"]


# --- mapping -------------------------------------------------------------------


def test_decision_becomes_a_call_with_its_recommendation_preselected():
    p = payload()
    call = p["captains_call"][1]
    assert call == {
        "key": "ship-it",
        "type": "decision",
        "repo": None,
        "title": "Decide ship-it",
        "options": [
            {"value": "go", "label": "Go ahead"},
            {"value": "stop", "label": "Stop here"},
        ],
        "recommend_value": "go",
        "about": "fm-vps: the captain asked whether to ship",
        "detail": GOOD + " · due 2026-11-02 · held 4 days",
    }
    assert refused(p) == []


def test_origin_leads_the_about_line_when_the_summary_carries_one():
    s = summary(decisions_open=[decision("ship-it", GOOD, origin="telegram")])
    assert (
        payload(s)["captains_call"][1]["about"]
        == "telegram (fm-vps): the captain asked whether to ship"
    )


def test_active_children_queued_and_landed_map_to_their_sections():
    p = payload()
    assert p["underway"] == [
        {
            "id": "w1",
            "repo": "lifekit-stack",
            "name": "Worker one",
            "state": "working",
            "doing": "writing tests",
            "kind": "ship",
        },
        {
            "id": "w2",
            "repo": None,
            "name": "w2",
            "state": "working",
            "doing": "working",
            "kind": "task",
        },
    ]
    assert p["charted"] == [
        {
            "id": "ready-one",
            "repo": "lifekit-stack",
            "title": "Ready work",
            "reason": "",
            "dispatchable": True,
            "kind": "queued",
            "filed": "2026-10-30",
        },
        {
            "id": "held-one",
            "repo": None,
            "title": "Held work",
            "reason": "waiting on a release",
            "dispatchable": False,
            "kind": "queued",
        },
    ]
    assert p["landed"] == [
        {
            "id": "done-one",
            "repo": "lifekit-stack",
            "what": "Shipped a thing",
            "owner": "fm-vps",
            "pr_url": "https://github.com/lifekit-hq/lifekit-stack/pull/1",
        },
        {"id": "done-two", "repo": None, "what": "Local note", "owner": "firstmate"},
    ]
    assert (p["schema"], p["home"], p["generated"], p["prs_live"]) == (
        "fm-bearings-board.v1",
        "fleet",
        "2026-11-02T08:00:00Z",
        False,
    )


def test_weekly_call_comes_first_and_carries_audit_pace_and_fleet_numbers():
    week = payload()["captains_call"][0]
    assert week["key"] == "weekly-scope-2026-11-02"
    assert week["title"] == "Accept this week's scope (week of 2026-11-02)"
    assert week["recommend_value"] == "accept"
    assert [o["value"] for o in week["options"]] == ["accept", "change"]
    assert (week["about"], week["decide"]) == ("Audit line", "Pace line")
    assert week["detail"] == (
        "Audit line | Pace line | Fleet: 1 calls, 0 refused, 2 underway, 2 queued, 2 landed; "
        "summary published 2026-11-02T07:59:00Z"
    )


def test_audit_report_reads_the_newest_dated_file_in_a_directory(tmp_path):
    (tmp_path / "2026-10-26.md").write_text(
        AUDIT.replace("2026-11-02 (", "2026-10-26 (")
    )
    (tmp_path / "2026-11-02.md").write_text(AUDIT)
    (tmp_path / "notes.md").write_text("# not a report\n")
    line, why = mp.read_audit(str(tmp_path))
    assert why == ""
    assert line == (
        "Audit 2026-11-02: 1 violations (2026-10-26 to 2026-11-02); entries: P2 1, P3 0, P6 1"
    )


def test_pace_line_from_a_file(tmp_path):
    f = tmp_path / "pace.json"
    f.write_text(json.dumps(PACE))
    assert mp.read_pace(str(f), "unused") == (
        "Pace: new starts allowed; weekly quota 80% left, resets 2026-11-09T06:00Z; "
        "next release 2026-11-03T06:00+00:00; burn from 2026-11-08T23:00+00:00",
        "",
    )


def test_live_pace_is_read_even_when_starts_are_held(tmp_path):
    fake = tmp_path / "fleet-policy"
    held = {
        **PACE,
        "start_allowed": False,
        "quota": None,
        "quota_error": "no quota-axi",
        "burn_from": None,
    }
    fake.write_text(f"#!/bin/sh\necho '{json.dumps(held)}'\nexit 1\n")
    fake.chmod(0o755)
    assert mp.read_pace(None, str(fake))[0] == (
        "Pace: new starts held; weekly quota unreadable (no quota-axi); next release 2026-11-03T06:00+00:00"
    )


# --- refusals --------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason, why",
    [
        ("[Tier 2 reversible] rec=go - the captain asked", ["no options"]),
        (
            "[Tier 2 reversible] opts: go=Go; stop=Stop - the captain asked",
            ["no recommendation"],
        ),
        ("[Tier 2 reversible] rec=go opts: go=Go; stop=Stop", ["no context"]),
        ("", ["no options", "no recommendation", "no context"]),
        (None, ["no options", "no recommendation", "no context"]),
        (
            "rec=maybe opts: go=Go; stop=Stop - asked",
            ["recommendation is not one of the options"],
        ),
        (
            "rec=go opts: go=Go; reconcile=Re - asked",
            ["option id reconcile is reserved"],
        ),
        (
            "rec=go opts: go=Go; stop=Stop; kee…",
            ["options cut off in the summary", "no context"],
        ),
    ],
)
def test_a_call_missing_a_piece_is_refused_with_every_reason(reason, why):
    p = payload(summary(decisions_open=[decision("bad-call", reason)]))
    assert [c["key"] for c in p["captains_call"]] == ["weekly-scope-2026-11-02"]
    assert refused(p) == [
        {
            "id": "refused-bad-call",
            "repo": None,
            "title": "Refused call: Decide bad-call",
            "reason": "refused: " + ", ".join(why),
            "dispatchable": False,
            "kind": "warning",
            "filed": "2026-11-02T08:00:00Z",
        }
    ]
    assert p["charted"][0]["kind"] == "warning"
    assert "1 refused" in p["captains_call"][0]["detail"]


def test_a_status_sourced_decision_with_no_reason_is_refused_not_smoothed_over():
    s = summary(
        decisions_open=[
            {
                "id": "x",
                "key": "k1",
                "verb": "needs-decision",
                "summary": "Pick one",
                "reason": None,
                "source": "status",
            }
        ]
    )
    assert (
        refused(payload(s))[0]["reason"]
        == "refused: no options, no recommendation, no context"
    )


# --- missing sources ---------------------------------------------------------------


def test_missing_audit_and_pace_still_emit_the_weekly_call(tmp_path):
    week = payload(
        audit=mp.read_audit(None), pace=mp.read_pace(str(tmp_path / "absent.json"), "x")
    )["captains_call"][0]
    assert week["about"] == "Audit unavailable: no audit report configured"
    assert week["decide"] == "Pace unavailable: pace unavailable (FileNotFoundError)"


@pytest.mark.parametrize(
    "setup, why",
    [
        (lambda d: None, "no audit report found"),
        (
            lambda d: (d / "2026-11-02.md").write_text("hello\n"),
            "audit report is not a fleet-policy audit",
        ),
    ],
)
def test_unusable_audit_sources_say_why(tmp_path, setup, why):
    setup(tmp_path)
    assert mp.read_audit(str(tmp_path)) == (None, why)


def test_garbage_pace_says_why(tmp_path):
    f = tmp_path / "pace.json"
    f.write_text("not json")
    assert mp.read_pace(str(f), "x") == (None, "pace output is not JSON")
    f.write_text("{}")
    assert mp.read_pace(str(f), "x") == (None, "pace output has no start_allowed")


def test_missing_summary_fails_and_prints_nothing(tmp_path):
    r = run_cli(tmp_path, summary_path=tmp_path / "absent.json")
    assert r.returncode == 1
    assert r.stdout == ""
    assert "fleet summary is unreadable" in r.stderr


# --- wikilinks ---------------------------------------------------------------------


def test_double_brackets_in_outside_text_never_read_as_wikilinks():
    s = summary(
        decisions_open=[
            {
                **decision("ship-it", GOOD.replace("whether to ship", "about [[x]]")),
                "summary": "Title [[x]]",
            },
            {**decision("bad", ""), "summary": "Refused [[y]]"},
        ],
        landed=[{"id": "l", "title": "Landed [[z]]", "home_id": "fm-vps"}],
    )
    out = json.dumps(payload(s), ensure_ascii=False)
    assert "[[" not in out and "]]" not in out
    call = payload(s)["captains_call"][1]
    assert call["title"] == "Title [\\[x]\\]"
    assert call["about"] == "fm-vps: the captain asked about [\\[x]\\]"


# --- CLI and the board's own validator --------------------------------------------


def run_cli(tmp_path, summary_path=None, audit=True, pace=True):
    if summary_path is None:
        summary_path = tmp_path / "summary.json"
        summary_path.write_text(json.dumps(summary()))
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONDAY_PAGE_")}
    env.update(MONDAY_PAGE_SUMMARY=str(summary_path), MONDAY_PAGE_NOW=str(NOW))
    if audit:
        (tmp_path / "audit").mkdir(exist_ok=True)
        (tmp_path / "audit/2026-11-02.md").write_text(AUDIT)
        env["MONDAY_PAGE_AUDIT"] = str(tmp_path / "audit")
    if pace:
        (tmp_path / "pace.json").write_text(json.dumps(PACE))
        env["MONDAY_PAGE_PACE"] = str(tmp_path / "pace.json")
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True
    )


def test_cli_prints_the_payload_and_names_refusals_on_stderr(tmp_path):
    s = summary(decisions_open=[decision("ship-it", GOOD), decision("bad", "")])
    (tmp_path / "summary.json").write_text(json.dumps(s))
    r = run_cli(tmp_path, summary_path=tmp_path / "summary.json")
    assert r.returncode == 0, r.stderr
    p = json.loads(r.stdout)
    assert [c["key"] for c in p["captains_call"]] == [
        "weekly-scope-2026-11-02",
        "ship-it",
    ]
    assert p["captains_call"][0]["about"].startswith("Audit 2026-11-02: 1 violations")
    assert "refused: no options, no recommendation, no context" in r.stderr


BOARD_SH = os.environ.get("MONDAY_PAGE_BOARD_SH")


@pytest.mark.skipif(
    not BOARD_SH, reason="MONDAY_PAGE_BOARD_SH names no fm-bearings-board.sh"
)
def test_payload_satisfies_the_board_validator(tmp_path):
    s = summary(
        decisions_open=[
            decision("ship-it", GOOD),
            decision("bad", ""),
            {**decision("w", GOOD), "summary": "T [[x]]"},
        ]
    )
    (tmp_path / "summary.json").write_text(json.dumps(s))
    r = run_cli(tmp_path, summary_path=tmp_path / "summary.json")
    data = tmp_path / "payload.json"
    data.write_text(r.stdout)
    script = Path(BOARD_SH).read_text()
    start = script.index("validate_payload() {")
    end = script.index("\n}\n", start) + 3
    check = subprocess.run(
        [
            "bash",
            "-c",
            f'BOARD_SCHEMA=fm-bearings-board.v1\n{script[start:end]}\nvalidate_payload "$1"',
            "_",
            str(data),
        ],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr


def test_an_open_decision_is_not_charted_again_and_a_repeated_key_is_refused():
    s = summary(
        decisions_open=[
            decision("ship-it", GOOD),
            {**decision("ship-it", GOOD), "home_id": "firstmate"},
        ],
        queued=[
            {
                "id": "ship-it",
                "title": "Decide ship-it",
                "hold_kind": "captain",
                "home_id": "fm-vps",
            }
        ],
    )
    p = payload(s)
    assert [c["key"] for c in p["captains_call"]] == [
        "weekly-scope-2026-11-02",
        "ship-it",
    ]
    assert [(r["id"], r["reason"]) for r in p["charted"]] == [
        ("refused-ship-it", "refused: duplicate key")
    ]
