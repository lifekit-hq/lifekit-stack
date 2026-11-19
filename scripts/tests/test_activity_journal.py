"""Behaviour of scripts/fleet-publisher/activity_journal.py against fixture sources."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/fleet-publisher/activity_journal.py"
DAY = dt.date(2026, 11, 10)
START = int(dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc).timestamp())
NOW = START + 86400 + 3 * 3600  # 03:00 UTC the next day, past the 2h delay
OFF = {"FLEET_PROM_URL": "", "FLEET_GH": "", "FLEET_CLIP_STATS": ""}


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def box(tmp_path):
    out = tmp_path / "out"
    (out / "metrics").mkdir(parents=True)
    ledger = tmp_path / "ledger.jsonl"
    write_jsonl(
        ledger,
        [
            {
                "ts": START + 10,
                "event": "task.merged",
                "task": "a",
                "pr": "https://github.com/o/r1/pull/1",
            },
            {
                "ts": START + 20,
                "event": "task.merged",
                "task": "b",
                "pr": "https://github.com/o/r1/pull/2",
            },
            {
                "ts": START + 30,
                "event": "task.merged",
                "task": "c",
                "pr": "https://github.com/o/r2/pull/3",
            },
            {
                "ts": START - 5,
                "event": "task.merged",
                "task": "z",
                "pr": "https://github.com/o/r2/pull/9",
            },
            {
                "ts": START + 40,
                "event": "task.status",
                "task": "a",
                "state": "failed",
                "text": "secret words",
            },
            {
                "ts": START + 50,
                "event": "task.status",
                "task": "b",
                "state": "resolved",
                "key": "k1",
            },
        ],
    )

    def run(now=NOW, ledgers=(), **env):
        args = [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--now",
            str(now),
            "--out-dir",
            str(out),
            "--daily",
            str(out / "metrics/daily.jsonl"),
        ]
        for item in ledgers or [ledger]:
            args += ["--ledger", str(item)]
        return subprocess.run(
            args,
            env={**os.environ, **OFF, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def rows():
        path = out / "journal/activity.jsonl"
        return (
            [json.loads(x) for x in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    return type(
        "Box",
        (),
        {
            "out": out,
            "ledger": ledger,
            "run": staticmethod(run),
            "rows": staticmethod(rows),
            "tmp": tmp_path,
        },
    )


def test_appends_one_row_and_a_rerun_the_same_day_adds_nothing(box):
    assert box.run().returncode == 0
    rows = box.rows()
    assert [r["date"] for r in rows] == [DAY.isoformat()]
    assert rows[0]["merged_prs_by_repo"] == {"o/r1": 2, "o/r2": 1}
    assert rows[0]["failed_tasks"] == 1
    assert rows[0]["decisions_owner"] == 1
    before = (box.out / "journal/activity.jsonl").read_text()
    assert box.run(now=NOW + 600).returncode == 0
    assert (box.out / "journal/activity.jsonl").read_text() == before


def test_every_field_is_present_and_unreadable_sources_say_unavailable(box):
    # Blocked case: no taken feed, no daily metrics, Prometheus, gh and clip all off.
    result = box.run(ledgers=[box.tmp / "missing.jsonl"])
    assert result.returncode == 0, result.stderr
    (row,) = box.rows()
    fields = (
        "merged_prs_by_repo decisions_owner decisions_taken_for_owner failed_tasks red_ci_runs "
        "stalled_task_hours devclaw_sessions openclaw_runs openclaw_errors fleet_weighted_tokens "
        "clip_stats"
    ).split()
    assert all(row[f] == "unavailable" for f in fields)
    # Allowed case: the same day again appends nothing.
    box.run(now=NOW + 60, ledgers=[box.tmp / "missing.jsonl"])
    assert len(box.rows()) == 1


def test_one_failing_source_does_not_hide_the_others(box):
    box.run()
    (row,) = box.rows()
    assert row["merged_prs_by_repo"] == {"o/r1": 2, "o/r2": 1}
    assert row["red_ci_runs"] == "unavailable"
    assert row["openclaw_runs"] == "unavailable"


def test_taken_feed_and_daily_metrics_feed_the_row_without_free_text(box):
    (box.out / "taken-for-you.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "ts": START + 5,
                        "ruling": "agent-behavior.md#P16",
                        "subject": "private words",
                    },
                    {
                        "ts": START + 6,
                        "ruling": "agent-behavior.md#P16",
                        "subject": "more",
                    },
                    {
                        "ts": START - 9,
                        "ruling": "agent-behavior.md#P6",
                        "subject": "other day",
                    },
                ]
            }
        )
    )
    write_jsonl(
        box.out / "metrics/daily.jsonl",
        [
            {
                "date": DAY.isoformat(),
                "partial": False,
                "stall_task_hours": 1.5,
                "weighted_tokens": 1000,
            },
        ],
    )
    box.run()
    text = (box.out / "journal/activity.jsonl").read_text()
    (row,) = box.rows()
    assert row["decisions_taken_for_owner"] == {
        "total": 2,
        "by_ruling": {"agent-behavior.md#P16": 2},
    }
    assert row["stalled_task_hours"] == 1.5 and row["fleet_weighted_tokens"] == 1000
    assert "private words" not in text and "secret words" not in text


def test_survives_restart_and_catches_up_missed_days(box):
    box.run()
    box.run(now=NOW + 3 * 86400)
    assert [r["date"] for r in box.rows()] == [
        "2026-11-10",
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
    ]


def test_clip_stats_keep_numeric_fields_only(box, tmp_path):
    feed = tmp_path / "clip.json"
    feed.write_text(json.dumps({"views": 12, "note": "free text", "subs": 3.5}))
    box.run(FLEET_CLIP_STATS=str(feed))
    assert box.rows()[0]["clip_stats"] == {"subs": 3.5, "views": 12}


def test_retro_board_after_14_days_has_four_parts_and_three_changes(box):
    rows = []
    for n in range(14):
        day = DAY + dt.timedelta(days=n)
        t = int(
            dt.datetime(
                day.year, day.month, day.day, tzinfo=dt.timezone.utc
            ).timestamp()
        )
        rows.append(
            {
                "ts": t + 5,
                "event": "task.merged",
                "task": f"m{n}",
                "pr": "https://github.com/o/r1/pull/1",
            }
        )
        if n % 2 == 0:
            rows.append(
                {
                    "ts": t + 6,
                    "event": "task.status",
                    "task": f"f{n}",
                    "state": "failed",
                }
            )
    write_jsonl(box.ledger, rows)
    for n in range(14):  # the publisher's daily cadence, one finished day at a time
        assert box.run(now=NOW + n * 86400).returncode == 0
    assert len(box.rows()) == 14
    boards = sorted((box.out / "journal/retro").glob("*.json"))
    assert [b.name for b in boards] == ["2026-11-10_2026-11-23.json"]
    board = json.loads(boards[0].read_text())
    assert board["went_well"] and board["went_wrong"]
    assert any(
        r["problem"] == "failed-tasks" and r["days"] == 7
        for r in board["recurring_problems"]
    )
    changes = board["proposed_changes"]
    assert len(changes) == 3 and changes[0]["basis"] == "failed-tasks"
    assert "## Proposed changes" in boards[0].with_suffix(".md").read_text()
    before = boards[0].read_text()
    box.run(now=NOW + 13 * 86400 + 60)
    assert boards[0].read_text() == before
    assert len(list((box.out / "journal/retro").glob("*.json"))) == 1


def test_no_board_before_14_days(box):
    box.run()
    assert not (box.out / "journal/retro").exists()
