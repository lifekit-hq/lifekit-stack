"""Behaviour of scripts/fleet-publisher/fleet-publisher.sh against fixture state."""

from __future__ import annotations

import configparser
import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/fleet-publisher"
SCRIPT = DIR / "fleet-publisher.sh"
NOW = 1_800_000_000
# The status page's live sources stay off unless a test stubs one: no gh, no
# docker exec into a gateway, no host vault.
NO_LIVE_SOURCES = {
    "FLEET_GH": "",
    "FLEET_INTAKE_CMD": "",
    "FLEET_VAULT": "/nonexistent",
}


def decision(id_, key=None, **extra):
    return {"id": id_, "key": key or id_, "verb": "captain-hold", **extra}


def summary(**overrides):
    base = {
        "schema": "fm-secondmate-home-summary.v1",
        "generated_epoch": NOW - 30,
        "valid": True,
        "active_children": [
            {"id": "a", "state": "working"},
            {"id": "b", "state": "working"},
            {"id": "c", "state": "blocked"},
        ],
        "decisions_open": [],
    }
    return {**base, **overrides}


def session(file, url, status="open", updated="2026-10-02T08:00:00Z"):
    return {"file": file, "url": url, "status": status, "updated_at": updated}


def event(ts, task="t", state="working", key=None, text=""):
    return {
        "v": 1,
        "ts": ts,
        "event": "task.status",
        "task": task,
        "state": state,
        "key": key,
        "text": text,
    }


@pytest.fixture
def fleet(tmp_path):
    out, prom = tmp_path / "out", tmp_path / "prom"
    out.mkdir()
    prom.mkdir()
    (tmp_path / "data").mkdir()

    def write(source=None, ledger=(), lavish=None):
        src = tmp_path / "home-summary.json"
        src.write_text(
            source if isinstance(source, str) else json.dumps(source or summary())
        )
        (tmp_path / "ledger.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in ledger)
        )
        if lavish is not None:
            (tmp_path / "lavish.json").write_text(
                json.dumps({"sessions": dict(enumerate(lavish))})
            )

    def run(cwd=None, **env):
        return subprocess.run(
            ["bash", str(SCRIPT), str(out), str(prom)],
            cwd=cwd,
            env={
                **os.environ,
                "FM_HOME": str(tmp_path),
                "SUMMARY_FILE": str(tmp_path / "home-summary.json"),
                "LEDGER_FILE": str(tmp_path / "ledger.jsonl"),
                "DATA_DIR": str(tmp_path / "data"),
                "LAVISH_STATE": str(tmp_path / "lavish.json"),
                "FLEET_NOW": str(NOW),
                "FLEET_STATE_DIR": str(tmp_path / "state"),
                **NO_LIVE_SOURCES,
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def metrics():
        parsed = {}
        for line in (prom / "fleet.prom").read_text().splitlines():
            if line and not line.startswith("#"):
                name, value = line.rsplit(" ", 1)
                parsed[name] = int(value)
        return parsed

    def published():
        return json.loads((out / "home-summary.json").read_text())

    return type(
        "Fleet",
        (),
        {
            "write": staticmethod(write),
            "run": staticmethod(run),
            "metrics": staticmethod(metrics),
            "published": staticmethod(published),
            "out": out,
            "prom": prom,
            "tmp": tmp_path,
        },
    )


def test_publishes_world_readable_copy_and_worker_counts(fleet):
    fleet.write(summary())
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    assert (fleet.out / "home-summary.json").stat().st_mode & 0o777 == 0o644
    assert (fleet.prom / "fleet.prom").stat().st_mode & 0o777 == 0o644
    got = fleet.published()
    assert got["published_epoch"] == NOW
    # One home: its summary unchanged, each item tagged with the home's id.
    home = fleet.tmp.name
    assert got["active_children"] == [
        {**c, "home_id": home} for c in summary()["active_children"]
    ]
    assert {k: got[k] for k in ("schema", "generated_epoch", "valid")} == {
        k: summary()[k] for k in ("schema", "generated_epoch", "valid")
    }
    assert got["homes"] == [
        {
            "id": home,
            "path": str(fleet.tmp),
            "published": True,
            "schema": "fm-secondmate-home-summary.v1",
            "generated_epoch": NOW - 30,
            "valid": True,
            "reason": None,
            "state": None,
            "counts": None,
            "invalidity": None,
        }
    ]
    m = fleet.metrics()
    assert m['fleet_workers{state="working"}'] == 2
    assert m['fleet_workers{state="blocked"}'] == 1
    assert m['fleet_workers{state="idle"}'] == 0
    assert m["fleet_decisions_open"] == 0
    assert m["fleet_oldest_decision_age_seconds"] == 0
    assert m["fleet_summary_valid"] == 1
    assert m["fleet_summary_generated_timestamp_seconds"] == NOW - 30
    assert m["fleet_publisher_last_success_timestamp_seconds"] == NOW
    assert m[f'fleet_home_summary_valid{{home="{home}"}}'] == 1
    assert m[f'fleet_home_summary_generated_timestamp_seconds{{home="{home}"}}'] == (
        NOW - 30
    )
    assert not [p for p in fleet.out.iterdir() if p.name != "home-summary.json"]
    assert not [p for p in fleet.prom.iterdir() if p.name != "fleet.prom"]


def test_home_invalidity_is_passed_through_as_is(fleet):
    invalidity = {"kind": "terminal_in_flight", "ids": ["done-task", "other"]}
    fleet.write(
        summary(valid=False, reason="finished work in flight", invalidity=invalidity)
    )
    fleet.run()
    got = fleet.published()
    assert got["homes"][0]["valid"] is False
    assert got["homes"][0]["invalidity"] == invalidity


def test_unknown_worker_state_gets_its_own_series(fleet):
    fleet.write(summary(active_children=[{"id": "a", "state": "stuck"}]))
    fleet.run()
    assert fleet.metrics()['fleet_workers{state="stuck"}'] == 1


def test_invalid_summary_is_reported_as_invalid(fleet):
    fleet.write(summary(valid=False))
    fleet.run()
    assert fleet.metrics()["fleet_summary_valid"] == 0


def test_decision_gets_the_newest_open_board_of_its_own_data_dir(fleet):
    data = fleet.tmp / "data"
    fleet.write(
        summary(
            decisions_open=[
                decision("task-a"),
                decision("task-b"),
                decision("task-c"),
                decision("task"),
            ]
        ),
        lavish=[
            session(
                f"{data}/task-a/old.html",
                "http://board/old",
                updated="2026-10-01T00:00:00Z",
            ),
            session(
                f"{data}/task-a/plan.html",
                "http://board/new",
                updated="2026-10-02T00:00:00Z",
            ),
            session(
                f"{data}/task-a/ended.html",
                "http://board/ended",
                status="ended",
                updated="2026-10-03T00:00:00Z",
            ),
            session(f"{data}/task-b/plan.html", "http://board/b", status="ended"),
            session(f"{data}/task-c-extra/plan.html", "http://board/other"),
        ],
    )
    assert fleet.run().returncode == 0
    urls = {d["id"]: d["board_url"] for d in fleet.published()["decisions_open"]}
    assert urls == {
        "task-a": "http://board/new",
        "task-b": None,
        "task-c": None,
        "task": None,
    }


@pytest.mark.parametrize("lavish", [None, "not json"])
def test_missing_or_broken_lavish_state_still_publishes(fleet, lavish):
    fleet.write(summary(decisions_open=[decision("x")]))
    if lavish:
        (fleet.tmp / "lavish.json").write_text(lavish)
    assert fleet.run().returncode == 0
    assert fleet.published()["decisions_open"][0]["board_url"] is None


def stub(path, body):
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return str(path)


HOLD_BINDS_BOUND_AND_WRONG = (
    'case "$1 $2" in "binding lavish-bound"|"binding lavish-wrong") echo "(any)";;'
    " *) exit 1;; esac"
)
TASKS_AXI_ONLY_FROM_FM_HOME = (
    '[[ "$PWD" == "$EXPECTED_PWD" ]] || { echo "NOT_FOUND" >&2; exit 1; }\n'
    'echo "body: \\"Captain hold origin: scout-origin\\n\\nRouted by x\\""'
)


def bound_board_fleet(fleet):
    data = fleet.tmp / "data"
    fleet.write(
        summary(decisions_open=[decision("held-call"), decision("other-call")]),
        lavish=[
            session(f"{data}/scout-origin/board.html", "http://board/bound"),
            session(f"{data}/elsewhere/board.html", "http://board/wrong"),
            session(f"{data}/scout-origin/unbound.html", "http://board/unbound"),
        ],
    )


def test_board_url_follows_the_captain_hold_binding_not_the_directory(fleet):
    hold = stub(fleet.tmp / "hold.sh", HOLD_BINDS_BOUND_AND_WRONG)
    tasks = stub(fleet.tmp / "tasks.sh", TASKS_AXI_ONLY_FROM_FM_HOME)
    bound_board_fleet(fleet)
    # The session key is the last url segment: only "bound" and "wrong" are bound.
    result = fleet.run(
        CAPTAIN_HOLD=hold,
        TASKS_AXI=tasks,
        FM_HOME=str(fleet.tmp),
        EXPECTED_PWD=str(fleet.tmp),
    )
    assert result.returncode == 0, result.stderr
    urls = {d["id"]: d["board_url"] for d in fleet.published()["decisions_open"]}
    assert urls == {
        "held-call": "http://board/bound",
        "other-call": "http://board/bound",
    }


def test_hold_origin_lookup_runs_from_the_fleet_home(fleet, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    hold = stub(fleet.tmp / "hold.sh", HOLD_BINDS_BOUND_AND_WRONG)
    tasks = stub(fleet.tmp / "tasks.sh", TASKS_AXI_ONLY_FROM_FM_HOME)
    bound_board_fleet(fleet)
    env = {"CAPTAIN_HOLD": hold, "TASKS_AXI": tasks, "EXPECTED_PWD": str(fleet.tmp)}
    # A lookup the backlog cannot answer is logged, and the board falls back.
    result = fleet.run(cwd=elsewhere, FM_HOME=str(elsewhere), **env)
    assert result.returncode == 0
    assert "tasks-axi show held-call failed: NOT_FOUND" in result.stderr
    assert {d["board_url"] for d in fleet.published()["decisions_open"]} == {None}
    # Run from FM_HOME it answers, whatever directory the publisher started in.
    result = fleet.run(cwd=elsewhere, FM_HOME=str(fleet.tmp), **env)
    assert result.returncode == 0, result.stderr
    assert {d["board_url"] for d in fleet.published()["decisions_open"]} == {
        "http://board/bound"
    }


def test_hold_origin_lookup_puts_the_tasks_axi_directory_on_path(fleet, tmp_path):
    bindir = tmp_path / "toolchain"
    bindir.mkdir()
    stub(bindir / "node", "exit 0\n")
    tasks = stub(
        bindir / "tasks-axi",
        'command -v node > /dev/null || { echo "node: not found" >&2; exit 127; }\n'
        + TASKS_AXI_ONLY_FROM_FM_HOME,
    )
    hold = stub(fleet.tmp / "hold.sh", HOLD_BINDS_BOUND_AND_WRONG)
    bound_board_fleet(fleet)
    result = fleet.run(
        CAPTAIN_HOLD=hold,
        TASKS_AXI=tasks,
        FM_HOME=str(fleet.tmp),
        EXPECTED_PWD=str(fleet.tmp),
        # Only the system dirs plus wherever jq lives: node must come from tasks-axi.
        PATH=f"/usr/bin:/bin:{Path(shutil.which('jq')).parent}",
    )
    assert result.returncode == 0, result.stderr
    assert "node: not found" not in result.stderr
    assert {d["board_url"] for d in fleet.published()["decisions_open"]} == {
        "http://board/bound"
    }


def test_board_url_falls_back_to_the_directory_when_nothing_is_bound(fleet):
    data = fleet.tmp / "data"
    hold = stub(fleet.tmp / "hold.sh", "exit 1")
    fleet.write(
        summary(decisions_open=[decision("task-a")]),
        lavish=[session(f"{data}/task-a/plan.html", "http://board/dir")],
    )
    assert fleet.run(CAPTAIN_HOLD=hold).returncode == 0
    assert fleet.published()["decisions_open"][0]["board_url"] == "http://board/dir"


def test_finished_reports_are_copied_and_served_by_url(fleet):
    data = fleet.tmp / "data"
    (data / "scout-a").mkdir()
    (data / "scout-a/report.md").write_text("# Report A\n")
    fleet.write(
        summary(
            landed=[
                {
                    "id": "scout-a",
                    "title": "Scout: a",
                    "report_path": "data/scout-a/report.md",
                }
            ]
        )
    )
    assert (
        fleet.run(
            REPORT_ROOT=str(fleet.tmp), REPORT_URL_BASE="/api/fleet/reports/"
        ).returncode
        == 0
    )
    (item,) = fleet.published()["landed"]
    assert item["report_url"] == "/api/fleet/reports/scout-a/report.md"
    assert item["report_path"] == "data/scout-a/report.md"
    copy = fleet.out / "reports/scout-a/report.md"
    assert copy.read_text() == "# Report A\n"
    assert copy.stat().st_mode & 0o777 == 0o644
    assert (fleet.out / "reports").stat().st_mode & 0o777 == 0o755
    # Once no landed item names a report the stale copy goes away.
    fleet.write(summary())
    assert fleet.run().returncode == 0
    assert not (fleet.out / "reports").exists()
    assert not [p for p in fleet.out.iterdir() if p.name != "home-summary.json"]


def test_republishing_reports_keeps_served_ones_in_place_and_drops_stale(fleet):
    data = fleet.tmp / "data"
    for id_ in ("keep", "drop"):
        (data / id_).mkdir()
        (data / id_ / "report.md").write_text(f"# {id_} v1\n")

    def landed(*ids):
        return summary(
            landed=[
                {"id": i, "title": i, "report_path": f"data/{i}/report.md"} for i in ids
            ]
        )

    fleet.write(landed("keep", "drop"))
    assert fleet.run(REPORT_ROOT=str(fleet.tmp)).returncode == 0
    served = fleet.out / "reports"
    served_inode = served.stat().st_ino
    (data / "keep/report.md").write_text("# keep v2\n")
    fleet.write(landed("keep"))
    assert fleet.run(REPORT_ROOT=str(fleet.tmp)).returncode == 0
    assert served.stat().st_ino == served_inode
    assert (served / "keep/report.md").read_text() == "# keep v2\n"
    assert not (served / "drop").exists()
    assert sorted(p.name for p in served.iterdir()) == ["keep"]
    assert sorted(p.name for p in fleet.out.iterdir()) == [
        "home-summary.json",
        "reports",
    ]


def test_reports_outside_the_data_dir_or_with_credentials_are_not_served(fleet):
    data = fleet.tmp / "data"
    (data / "ok").mkdir()
    (data / "ok/report.md").write_text("fine")
    (data / "secret").mkdir()
    (data / "secret/report.md").write_text("key " + "ghp_" + "A" * 24)
    (fleet.tmp / "outside.md").write_text("outside")
    (data / "link").mkdir()
    (data / "link/report.md").symlink_to(fleet.tmp / "outside.md")
    (data / "notes.txt").write_text("not markdown")
    paths = {
        "ok": "data/ok/report.md",
        "secret": "data/secret/report.md",
        "link": "data/link/report.md",
        "dotdot": "data/../outside.md",
        "abs": str(fleet.tmp / "outside.md"),
        "txt": "data/notes.txt",
        "missing": "data/missing/report.md",
    }
    fleet.write(
        summary(
            landed=[{"id": k, "title": k, "report_path": v} for k, v in paths.items()]
        )
    )
    assert fleet.run(REPORT_ROOT=str(fleet.tmp)).returncode == 0
    urls = {i["id"]: i["report_url"] for i in fleet.published()["landed"]}
    assert urls == {
        "ok": "reports/ok/report.md",
        **{k: None for k in paths if k != "ok"},
    }
    assert sorted(p.name for p in (fleet.out / "reports").iterdir()) == ["ok"]


def test_oldest_decision_age_uses_ledger_open_time_then_hold_days(fleet):
    fleet.write(
        summary(
            decisions_open=[
                decision("newer", "k1"),
                decision("older", "k2"),
                decision("held", hold_age_days=2),
                decision("unknown", "k9"),
            ]
        ),
        ledger=[
            event(NOW - 600, "newer", "needs-decision", "k1"),
            event(NOW - 1000, "older", "needs-decision", "k2"),
            event(NOW - 100, "older", "working", "k2"),
            event(NOW - 9000, "older", "needs-decision", "other-key"),
        ],
    )
    fleet.run()
    m = fleet.metrics()
    assert m["fleet_decisions_open"] == 4
    assert m["fleet_oldest_decision_age_seconds"] == 2 * 86400


def test_oldest_decision_age_from_ledger_alone(fleet):
    fleet.write(
        summary(decisions_open=[decision("a", "k1"), decision("b", "k2")]),
        ledger=[
            event(NOW - 600, "a", "needs-decision", "k1"),
            event(NOW - 1000, "b", "needs-decision", "k2"),
        ],
    )
    fleet.run()
    assert fleet.metrics()["fleet_oldest_decision_age_seconds"] == 1000


def test_usage_limit_events_counted_in_the_last_hour_only(fleet):
    fleet.write(
        summary(),
        ledger=[
            event(NOW - 10, text="blocked: Usage limit reached, needs rerun"),
            event(NOW - 3000, key="usage-limit-123"),
            event(NOW - 3500, text="usage-limit pause until reset"),
            event(NOW - 3700, text="usage limit reached"),
            event(NOW - 20, text="lint failed"),
            {
                "v": 1,
                "ts": NOW - 5,
                "event": "task.dispatched",
                "task": "t",
                "text": "usage limit",
            },
        ],
    )
    fleet.run()
    assert fleet.metrics()["fleet_usage_limit_events_1h"] == 3


def test_ledger_tolerates_garbage_lines_and_absence(fleet):
    fleet.write(summary())
    (fleet.tmp / "ledger.jsonl").write_text('not json\n{"ts": 1}\n')
    assert fleet.run().returncode == 0
    (fleet.tmp / "ledger.jsonl").unlink()
    assert fleet.run().returncode == 0
    assert fleet.metrics()["fleet_usage_limit_events_1h"] == 0


@pytest.mark.parametrize(
    "source",
    [
        "{not json",
        json.dumps({"schema": "other", "generated_epoch": 1}),
        json.dumps({"schema": "fm-secondmate-home-summary.v1"}),
    ],
    ids=["malformed", "wrong-schema", "no-epoch"],
)
def test_bad_source_keeps_previous_files(fleet, source):
    fleet.write(summary())
    assert fleet.run().returncode == 0
    before = (
        (fleet.out / "home-summary.json").read_text(),
        (fleet.prom / "fleet.prom").read_text(),
    )
    fleet.write(source)
    assert fleet.run(FLEET_NOW=str(NOW + 60)).returncode != 0
    assert (
        (fleet.out / "home-summary.json").read_text(),
        (fleet.prom / "fleet.prom").read_text(),
    ) == before


def test_missing_source_keeps_previous_files(fleet):
    fleet.write(summary())
    fleet.run()
    (fleet.tmp / "home-summary.json").unlink()
    assert fleet.run().returncode != 0
    assert fleet.published()["generated_epoch"] == NOW - 30


@pytest.mark.parametrize(
    "secret",
    [
        "sk-" + "ant-" + "a" * 20,
        "ghp_" + "A" * 24,
        "xoxb-" + "1" * 12,
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
        "tskey-" + "auth-abc123",
    ],
)
def test_summary_carrying_a_credential_is_not_published(fleet, secret):
    fleet.write(summary(decisions_open=[decision("x", reason=f"leaked {secret}")]))
    result = fleet.run()
    assert result.returncode == 3
    assert not (fleet.out / "home-summary.json").exists()
    assert secret not in result.stderr


def test_ordinary_token_talk_is_not_a_credential(fleet):
    fleet.write(
        summary(
            decisions_open=[
                decision("x", summary="rotate the setup-token and the api key")
            ]
        )
    )
    assert fleet.run().returncode == 0


def daily_row(date, home=None, **over):
    row = {
        "schema": "fleet-metrics-daily.v1",
        "date": date,
        "home": home,
        "computed_at": f"{date}T23:59:00Z",
        "partial": False,
        "backfilled": False,
        "runs": 10,
        "first_pass_runs": 4,
        "fix_rounds": 7,
        "merged_prs": 3,
        "weighted_tokens": 1000,
        "stall_task_hours": 1.5,
        "asks_new": {"t0": 0, "t1": 1, "t2": 2, "untiered": 3},
        "asks_avoidable": 1,
        "ask_answer_hours_median": None,
        "allowance_week_pct": 19,
        "five_hour_peak_pct": 8,
        "by_tier": {"sonnet": {"runs": 3, "first_pass": 1}},
        "peak_workers": 6,
        "mem_available_min": 6278980,
        "swap_used_max": 0,
        "psi_some_max": 0.18,
    }
    return {**row, **over}


@pytest.fixture
def daily(fleet):
    home = fleet.tmp.name
    src = fleet.tmp / "data" / "fleet-metrics"
    out = fleet.out / "metrics" / "daily.jsonl"

    def write(*rows):
        src.mkdir(exist_ok=True)
        (src / "daily.jsonl").write_text(
            "".join(
                r
                if isinstance(r, str)
                else json.dumps({**r, "home": r["home"] or home}) + "\n"
                for r in rows
            )
        )

    def read():
        return [json.loads(x) for x in out.read_text().splitlines()]

    fleet.write(summary())
    return type(
        "Daily",
        (),
        {
            "write": staticmethod(write),
            "read": staticmethod(read),
            "out": out,
            "home": home,
        },
    )


def test_daily_metrics_are_copied_sorted_by_date(fleet, daily):
    daily.write(daily_row("2026-10-12"), daily_row("2026-10-11"))
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    assert [r["date"] for r in daily.read()] == ["2026-10-11", "2026-10-12"]
    assert {r["home"] for r in daily.read()} == {daily.home}
    assert daily.out.stat().st_mode & 0o777 == 0o644


def test_daily_metrics_rerun_replaces_the_day_never_duplicates(fleet, daily):
    daily.write(daily_row("2026-10-13", partial=True, runs=2))
    assert fleet.run().returncode == 0
    assert [(r["runs"], r["partial"]) for r in daily.read()] == [(2, True)]
    # The capture appends: the later row for the date wins, in file order.
    daily.write(
        daily_row("2026-10-13", partial=True, runs=2),
        daily_row("2026-10-13", runs=9),
    )
    assert fleet.run().returncode == 0
    assert fleet.run().returncode == 0
    assert [(r["runs"], r["partial"]) for r in daily.read()] == [(9, False)]


def test_daily_metrics_history_outlives_the_source(fleet, daily):
    daily.write(daily_row("2026-10-11"))
    assert fleet.run().returncode == 0
    daily.write(daily_row("2026-10-12"))
    assert fleet.run().returncode == 0
    assert [r["date"] for r in daily.read()] == ["2026-10-11", "2026-10-12"]


def test_daily_metrics_skip_invalid_rows_and_count_them(fleet, daily):
    daily.write(
        daily_row("2026-10-10"),
        daily_row("2026-10-11", schema="fleet-metrics-daily.v2"),
        daily_row("2026-10-12", runs="ten"),
        {**daily_row("2026-10-13"), "date": "13/10/2026"},
        daily_row("2026-10-14", fix_rounds=-1),
        daily_row("2026-10-15", asks_new={"t0": 1}),
        daily_row("2026-10-16", home="someone-else"),
        "not json at all\n",
        daily_row("2026-10-17"),
    )
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    assert [r["date"] for r in daily.read()] == ["2026-10-10", "2026-10-17"]
    assert "skipped 7 invalid" in result.stderr
    assert fleet.published()["valid"] is True


def test_daily_metrics_keep_only_published_fields(fleet, daily):
    daily.write(
        daily_row(
            "2026-10-11",
            surprise="x",
            ask_answer_hours_median=0.5,
            asks_new={"t0": 0, "t1": 1, "t2": 2, "untiered": 3, "note": "x"},
            by_tier={"sonnet": {"runs": 3, "first_pass": 1, "note": "x"}},
        )
    )
    assert fleet.run().returncode == 0
    (row,) = daily.read()
    assert "surprise" not in row
    assert row["asks_new"] == {"t0": 0, "t1": 1, "t2": 2, "untiered": 3}
    assert row["ask_answer_hours_median"] == 0.5
    assert row["by_tier"] == {"sonnet": {"runs": 3, "first_pass": 1}}


def test_missing_daily_source_leaves_output_and_summary_alone(fleet, daily):
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    assert not daily.out.exists()
    assert fleet.published()["published_epoch"] == NOW
    daily.write(daily_row("2026-10-11"))
    assert fleet.run().returncode == 0
    before = daily.out.read_bytes()
    (fleet.tmp / "data" / "fleet-metrics" / "daily.jsonl").unlink()
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    assert daily.out.read_bytes() == before
    assert fleet.published()["valid"] is True


def test_unreadable_daily_source_does_not_break_the_summary(fleet, daily):
    daily.write(daily_row("2026-10-11"))
    assert fleet.run().returncode == 0
    before = daily.out.read_bytes()
    daily.write("garbage\n", '{"schema": "fleet-metrics-daily.v1"}\n')
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    assert daily.out.read_bytes() == before
    assert "skipped 2 invalid" in result.stderr


def test_daily_source_outside_the_home_data_is_not_read(fleet, daily, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "daily.jsonl"
    elsewhere.write_text(json.dumps(daily_row("2026-10-11", home=daily.home)) + "\n")
    src = fleet.tmp / "data" / "fleet-metrics"
    src.mkdir()
    (src / "daily.jsonl").symlink_to(elsewhere)
    assert fleet.run().returncode == 0
    assert not daily.out.exists()


def test_daily_source_with_a_credential_is_not_read(fleet, daily):
    daily.write(daily_row("2026-10-11", note="sk-ant-api03-abcdef"))
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    assert not daily.out.exists()
    assert "credential" in result.stderr


def test_daily_write_is_atomic_and_leaves_no_temp_files(fleet, daily):
    daily.write(daily_row("2026-10-11"))
    assert fleet.run().returncode == 0
    inode = daily.out.stat().st_ino
    assert fleet.run().returncode == 0
    assert daily.out.stat().st_ino == inode  # unchanged content is not rewritten
    daily.write(daily_row("2026-10-11"), daily_row("2026-10-12"))
    assert fleet.run().returncode == 0
    assert daily.out.stat().st_ino != inode  # replaced by rename, never edited in place
    assert [p.name for p in daily.out.parent.iterdir()] == ["daily.jsonl"]


def test_a_failed_daily_write_is_logged_and_never_blocks_the_summary(fleet, daily):
    # metrics/ cannot be created: something else holds its name.
    (fleet.out / "metrics").write_text("in the way")
    daily.write(daily_row("2026-10-11"))
    result = fleet.run()
    assert result.returncode != 0
    assert "daily metrics were not published" in result.stderr
    assert fleet.published()["published_epoch"] == NOW
    assert fleet.metrics()["fleet_decisions_open"] == 0


def test_needs_a_fleet_home(fleet):
    result = subprocess.run(
        ["bash", str(SCRIPT), str(fleet.out), str(fleet.prom)],
        env={
            k: v for k, v in os.environ.items() if k not in ("FM_HOME", "SUMMARY_FILE")
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


def make_home(root, name, source, ledger=(), hold=None):
    """A fleet home laid out as the publisher reads it: state/, data/, bin/."""
    home = root / name
    (home / "state").mkdir(parents=True)
    (home / "data").mkdir()
    (home / "state/home-summary.json").write_text(
        source if isinstance(source, str) else json.dumps(source)
    )
    (home / "state/fleet-ledger.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in ledger)
    )
    if hold is not None:
        (home / "bin").mkdir()
        stub(home / "bin/fm-captain-hold.sh", hold)
    return home


@pytest.fixture
def homes(tmp_path):
    out, prom = tmp_path / "out", tmp_path / "prom"
    out.mkdir()
    prom.mkdir()

    def run(*dirs, **env):
        clean = {
            k: v
            for k, v in os.environ.items()
            if k not in ("FM_HOME", "SUMMARY_FILE", "LEDGER_FILE", "DATA_DIR")
        }
        return subprocess.run(
            ["bash", str(SCRIPT), str(out), str(prom)],
            env={
                **clean,
                "FM_HOMES": " ".join(str(d) for d in dirs),
                "LAVISH_STATE": str(tmp_path / "lavish.json"),
                "FLEET_NOW": str(NOW),
                "FLEET_STATE_DIR": str(tmp_path / "state"),
                **NO_LIVE_SOURCES,
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def published():
        return json.loads((out / "home-summary.json").read_text())

    def metrics():
        parsed = {}
        for line in (prom / "fleet.prom").read_text().splitlines():
            if line and not line.startswith("#"):
                name, value = line.rsplit(" ", 1)
                parsed[name] = int(value)
        return parsed

    return type(
        "Homes",
        (),
        {
            "run": staticmethod(run),
            "published": staticmethod(published),
            "metrics": staticmethod(metrics),
            "out": out,
            "prom": prom,
            "tmp": tmp_path,
        },
    )


def two_homes(root):
    vps = make_home(
        root,
        "fm-vps",
        summary(
            active_children=[{"id": "v1", "state": "working"}],
            decisions_open=[decision("vps-call", "k1")],
            holds=[{"id": "vps-hold"}],
            landed=[{"id": "vps-done", "title": "done"}],
            counts={"active_children": 1, "holds": 4},
            state="ok",
        ),
        ledger=[event(NOW - 600, "vps-call", "needs-decision", "k1")],
    )
    main = make_home(
        root,
        "firstmate",
        summary(
            generated_epoch=NOW - 90,
            valid=False,
            reason="item x is stale",
            active_children=[
                {"id": "m1", "state": "working"},
                {"id": "m2", "state": "blocked"},
            ],
            decisions_open=[decision("main-call", "k2"), decision("main-two")],
            queued=[{"id": "main-next"}],
            counts={"active_children": 2, "holds": 0, "queued": 1},
        ),
        ledger=[
            event(NOW - 2000, "main-call", "needs-decision", "k2"),
            event(NOW - 60, text="usage limit reached"),
        ],
    )
    return vps, main


def test_every_home_is_published_with_each_item_tagged_by_its_home(homes):
    vps, main = two_homes(homes.tmp)
    result = homes.run(vps, main)
    assert result.returncode == 0, result.stderr
    got = homes.published()
    # Lists are concatenated in FM_HOMES order, every item naming its home.
    assert [(c["id"], c["home_id"]) for c in got["active_children"]] == [
        ("v1", "fm-vps"),
        ("m1", "firstmate"),
        ("m2", "firstmate"),
    ]
    assert [(d["id"], d["home_id"]) for d in got["decisions_open"]] == [
        ("vps-call", "fm-vps"),
        ("main-call", "firstmate"),
        ("main-two", "firstmate"),
    ]
    assert [(h["id"], h["home_id"]) for h in got["holds"]] == [("vps-hold", "fm-vps")]
    assert [(q["id"], q["home_id"]) for q in got["queued"]] == [
        ("main-next", "firstmate")
    ]
    assert got["landed"] == [
        {"id": "vps-done", "title": "done", "report_url": None, "home_id": "fm-vps"}
    ]
    # Whole-fleet fields: the oldest summary, valid only when every home is,
    # each home's reason named, counts summed; the rest is the first home's.
    assert got["generated_epoch"] == NOW - 90
    assert got["valid"] is False
    assert got["reason"] == "firstmate: item x is stale"
    assert got["counts"] == {
        "active_children": 3,
        "holds": 4,
        "queued": 1,
        "queued_open": 1,
    }
    assert got["state"] == "ok"
    assert got["published_epoch"] == NOW
    assert [
        (h["id"], h["path"], h["published"], h["generated_epoch"], h["valid"])
        for h in got["homes"]
    ] == [
        ("fm-vps", str(vps), True, NOW - 30, True),
        ("firstmate", str(main), True, NOW - 90, False),
    ]
    m = homes.metrics()
    assert m['fleet_workers{state="working"}'] == 2
    assert m['fleet_workers{state="blocked"}'] == 1
    assert m["fleet_decisions_open"] == 3
    assert m["fleet_oldest_decision_age_seconds"] == 2000
    assert m["fleet_usage_limit_events_1h"] == 1
    assert m["fleet_summary_valid"] == 0
    assert m["fleet_summary_generated_timestamp_seconds"] == NOW - 90
    assert m['fleet_home_summary_valid{home="fm-vps"}'] == 1
    assert m['fleet_home_summary_valid{home="firstmate"}'] == 0
    assert m['fleet_home_summary_generated_timestamp_seconds{home="fm-vps"}'] == (
        NOW - 30
    )
    assert m['fleet_home_summary_generated_timestamp_seconds{home="firstmate"}'] == (
        NOW - 90
    )


def test_a_child_homes_decisions_folded_into_its_parent_count_once(homes):
    vps, main = two_homes(homes.tmp)
    (main / "data/secondmates.md").write_text(
        f"- kid - Second mate on this host. (home: {vps}; scope: all)\n"
        "- away - Overflow mate. (host: pc; root: /elsewhere/r; home: /elsewhere/h; scope: x)\n"
    )
    summ = json.loads((main / "state/home-summary.json").read_text())
    summ["decisions_open"] = [
        decision("kid", "k1"),
        decision("kid", "k9"),
        decision("away", "k3"),
        decision("main-call", "k2"),
    ]
    (main / "state/home-summary.json").write_text(json.dumps(summ))
    result = homes.run(vps, main)
    assert result.returncode == 0, result.stderr
    # The child's own item stays; the parent's copies of it go; an item for a
    # secondmate nobody publishes, and the parent's own, are kept.
    assert [(d["id"], d["home_id"]) for d in homes.published()["decisions_open"]] == [
        ("vps-call", "fm-vps"),
        ("away", "firstmate"),
        ("main-call", "firstmate"),
    ]
    assert homes.metrics()["fleet_decisions_open"] == 3
    # Order of FM_HOMES does not matter: the parent first still drops them.
    assert homes.run(main, vps).returncode == 0
    assert homes.metrics()["fleet_decisions_open"] == 3


def test_a_parents_counts_drop_the_folded_child_items_it_no_longer_lists(homes):
    # The live case: 26 counted, 13 listed. The parent folds the child's 13
    # waits into its own list and its own count; the publisher drops the
    # copies from the list, so the count has to drop them too.
    vps = make_home(
        homes.tmp,
        "fm-vps",
        summary(
            decisions_open=[decision(f"v{n}") for n in range(7)],
            counts={"decisions_open": 7},
        ),
    )
    main = make_home(
        homes.tmp,
        "firstmate",
        summary(
            decisions_open=[decision("kid", f"k{n}") for n in range(13)]
            + [decision(f"m{n}") for n in range(6)],
            counts={"decisions_open": 19},
        ),
    )
    (main / "data/secondmates.md").write_text(
        f"- kid - Second mate on this host. (home: {vps}; scope: all)\n"
    )
    result = homes.run(vps, main)
    assert result.returncode == 0, result.stderr
    got = homes.published()
    assert len(got["decisions_open"]) == 13
    assert got["counts"]["decisions_open"] == 13
    assert [(h["id"], h["counts"]["decisions_open"]) for h in got["homes"]] == [
        ("fm-vps", 7),
        ("firstmate", 6),
    ]
    assert homes.metrics()["fleet_decisions_open"] == 13
    # Parent first changes nothing.
    assert homes.run(main, vps).returncode == 0
    assert homes.published()["counts"]["decisions_open"] == 13


def test_counts_that_cannot_go_below_zero_or_name_unlisted_items_stay_put(homes):
    vps = make_home(homes.tmp, "fm-vps", summary(counts={"decisions_open": 0}))
    main = make_home(
        homes.tmp,
        "firstmate",
        # counts beyond the capped list (30 counted, 2 listed): only the listed
        # folded item is subtracted. A count already below the listed folded
        # items bottoms out at 0 rather than going negative.
        summary(
            decisions_open=[decision("kid"), decision("kid", "k2"), decision("own")],
            counts={"decisions_open": 1},
        ),
    )
    (main / "data/secondmates.md").write_text(
        f"- kid - Mate. (home: {vps}; scope: x)\n"
    )
    assert homes.run(vps, main).returncode == 0
    assert homes.published()["counts"]["decisions_open"] == 0
    summ = json.loads((main / "state/home-summary.json").read_text())
    summ["counts"] = {"decisions_open": 30}
    (main / "state/home-summary.json").write_text(json.dumps(summ))
    assert homes.run(vps, main).returncode == 0
    assert homes.published()["counts"]["decisions_open"] == 28


def held(id_, bucket=None, kind="captain"):
    return {"id": id_, "hold_kind": kind, "hold_bucket": bucket}


def test_queued_open_counts_only_items_nobody_holds(homes):
    # The live case: 33 queued, 13 of them really queued, the other 20 held.
    vps = make_home(
        homes.tmp,
        "fm-vps",
        summary(
            queued=[
                {"id": f"q{n}", "hold_kind": None, "hold_bucket": None}
                for n in range(6)
            ]
            + [held(f"live{n}", "live") for n in range(7)]
            + [held("blk1", "blocked"), held("blk2", "blocked")]
            + [held("dat1", "dated"), held("dat2", "dated")]
            + [held(f"ext{n}", None, "external") for n in range(3)],
            counts={"queued": 20},
        ),
    )
    main = make_home(
        homes.tmp,
        "firstmate",
        summary(
            # items of an older summary carry no hold fields: not held
            queued=[{"id": f"n{n}"} for n in range(7)]
            + [held(f"mlive{n}", "live") for n in range(5)]
            + [held("mdat", "dated")],
            counts={"queued": 13},
        ),
    )
    assert homes.run(vps, main).returncode == 0
    got = homes.published()
    assert got["counts"]["queued"] == 33
    assert got["counts"]["queued_open"] == 13
    assert [(h["id"], h["counts"]["queued_open"]) for h in got["homes"]] == [
        ("fm-vps", 6),
        ("firstmate", 7),
    ]
    assert len(got["queued"]) == 33  # the list itself is not filtered


def test_queued_open_plus_the_later_buckets_accounts_for_every_queued_item(fleet):
    queued = (
        [{"id": f"q{n}", "hold_kind": None, "hold_bucket": None} for n in range(2)]
        + [held("aged1", "aged"), held("dat1", "dated"), held("blk1", "blocked")]
        + [held(f"ext{n}", None, "external") for n in range(2)]
    )
    fleet.write(summary(queued=queued, counts={"queued": 7, "decisions_open": 0}))
    assert fleet.run().returncode == 0
    got = fleet.published()
    counts = got["counts"]
    assert counts["queued_open"] == 2

    def bucket(item):
        if item["hold_bucket"] in ("dated", "blocked"):
            return item["hold_bucket"]
        if item["hold_bucket"] == "aged" or item["hold_kind"]:
            return "parked"
        return "open"

    tally = {}
    for item in got["queued"]:
        tally[bucket(item)] = tally.get(bucket(item), 0) + 1
    assert tally == {"open": 2, "parked": 3, "dated": 1, "blocked": 1}
    assert sum(tally.values()) == counts["queued"]


def test_queued_open_counts_the_listed_items_when_the_list_is_capped(fleet):
    # 25 queued, 20 listed (the held live ones sort last and are cut): the
    # listed unheld items are the count, not counts.queued minus listed holds.
    listed = [{"id": f"q{n}"} for n in range(3)] + [
        held(f"h{n}", "live") for n in range(17)
    ]
    fleet.write(summary(queued=listed, counts={"queued": 25, "decisions_open": 0}))
    assert fleet.run().returncode == 0
    counts = fleet.published()["counts"]
    assert counts["queued"] == 25
    assert counts["queued_open"] == 3


def test_a_non_object_queued_entry_does_not_abort_the_publish(fleet):
    listed = [{"id": "q1"}, "stray", None, held("h1", "live")]
    fleet.write(summary(queued=listed, counts={"queued": 4, "decisions_open": 0}))
    assert fleet.run().returncode == 0
    counts = fleet.published()["counts"]
    assert counts["queued"] == 4
    assert counts["queued_open"] == 1


def test_a_summary_without_counts_or_queued_gets_no_new_counts(fleet):
    fleet.write(summary(queued=[{"id": "q"}]))
    assert fleet.run().returncode == 0
    assert "counts" not in fleet.published()
    fleet.write(summary(counts={"decisions_open": 2}))
    assert fleet.run().returncode == 0
    assert fleet.published()["counts"] == {"decisions_open": 2}


def test_a_parents_items_for_an_unpublished_child_are_kept(homes):
    vps, main = two_homes(homes.tmp)
    (main / "data/secondmates.md").write_text(
        f"- kid - Second mate. (home: {vps}; scope: all)\n"
    )
    summ = json.loads((main / "state/home-summary.json").read_text())
    summ["decisions_open"] = [decision("kid", "k1")]
    (main / "state/home-summary.json").write_text(json.dumps(summ))
    (vps / "state/home-summary.json").write_text("not json")
    assert homes.run(vps, main).returncode == 1
    assert [d["id"] for d in homes.published()["decisions_open"]] == ["kid"]
    assert homes.metrics()["fleet_decisions_open"] == 1


def test_fm_homes_defaults_to_fm_home(homes):
    vps, _ = two_homes(homes.tmp)
    assert homes.run(FM_HOMES="", FM_HOME=str(vps)).returncode == 0
    got = homes.published()
    assert [h["id"] for h in got["homes"]] == ["fm-vps"]
    assert got["valid"] is True
    assert "reason" not in got


# Binds session "s-<home>" to the decision "held", only when called as that
# home: the CLI takes its home from FM_HOME, so a call that inherits another
# home's FM_HOME misses.
HOLD_BINDS_OWN_SESSION = (
    '[[ "$FM_HOME" == "$(cd "$(dirname "$0")/.." && pwd)" ]] || exit 1\n'
    '[[ "$2" == "lavish-s-$(basename "$FM_HOME")" ]] && echo held || exit 1\n'
)


def test_boards_are_found_through_each_homes_own_binding_and_data_dir(homes):
    vps = make_home(
        homes.tmp,
        "fm-vps",
        summary(decisions_open=[decision("held")]),
        hold=HOLD_BINDS_OWN_SESSION,
    )
    main = make_home(
        homes.tmp,
        "firstmate",
        summary(decisions_open=[decision("held"), decision("dir-call")]),
        hold=HOLD_BINDS_OWN_SESSION,
    )
    (homes.tmp / "lavish.json").write_text(
        json.dumps(
            {
                "sessions": dict(
                    enumerate(
                        [
                            session(
                                f"{vps}/data/elsewhere/b.html", "http://board/s-fm-vps"
                            ),
                            session(
                                f"{main}/data/elsewhere/b.html",
                                "http://board/s-firstmate",
                            ),
                            session(
                                f"{vps}/data/dir-call/b.html", "http://board/wrong-home"
                            ),
                            session(
                                f"{main}/data/dir-call/b.html", "http://board/main-dir"
                            ),
                        ]
                    )
                )
            }
        )
    )
    # The old unit's FM_HOME names the first home; it must not leak into the second.
    result = homes.run(vps, main, FM_HOME=str(vps), TASKS_AXI="/nonexistent")
    assert result.returncode == 0, result.stderr
    boards = [
        (d["home_id"], d["id"], d["board_url"])
        for d in homes.published()["decisions_open"]
    ]
    assert boards == [
        ("fm-vps", "held", "http://board/s-fm-vps"),
        ("firstmate", "held", "http://board/s-firstmate"),
        ("firstmate", "dir-call", "http://board/main-dir"),
    ]


def test_a_report_id_both_homes_land_is_served_from_the_first_home(homes):
    vps, main = (
        make_home(
            homes.tmp,
            name,
            summary(
                landed=[{"id": "same", "report_path": "data/same/report.md"}],
            ),
        )
        for name in ("fm-vps", "firstmate")
    )
    for home in (vps, main):
        (home / "data/same").mkdir()
        (home / "data/same/report.md").write_text(f"# from {home.name}\n")
    result = homes.run(vps, main)
    assert result.returncode == 0, result.stderr
    assert "firstmate: report same clashes" in result.stderr
    urls = [(i["home_id"], i["report_url"]) for i in homes.published()["landed"]]
    assert urls == [("fm-vps", "reports/same/report.md"), ("firstmate", None)]
    assert (homes.out / "reports/same/report.md").read_text() == "# from fm-vps\n"


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("{not json", 1),
        (json.dumps(summary(reason="leaked " + "ghp_" + "A" * 24)), 3),
    ],
    ids=["malformed", "credential"],
)
def test_an_unreadable_home_is_left_out_and_fails_the_run(homes, source, code):
    vps, _ = two_homes(homes.tmp)
    broken = make_home(homes.tmp, "broken", source)
    result = homes.run(vps, broken)
    assert result.returncode == code
    got = homes.published()
    assert {c["home_id"] for c in got["active_children"]} == {"fm-vps"}
    assert got["valid"] is False
    assert got["homes"][1] == {
        "id": "broken",
        "path": str(broken),
        "published": False,
        "valid": False,
        "reason": got["homes"][1]["reason"],
    }
    assert "ghp_" not in (homes.out / "home-summary.json").read_text()
    m = homes.metrics()
    assert m['fleet_home_summary_valid{home="broken"}'] == 0
    assert 'fleet_home_summary_generated_timestamp_seconds{home="broken"}' not in m
    assert m["fleet_summary_generated_timestamp_seconds"] == NOW - 30


def test_no_readable_home_keeps_previous_files(homes):
    vps, main = two_homes(homes.tmp)
    assert homes.run(vps, main).returncode == 0
    before = (homes.out / "home-summary.json").read_text()
    for home in (vps, main):
        (home / "state/home-summary.json").write_text("{not json")
    assert homes.run(vps, main).returncode == 1
    assert (homes.out / "home-summary.json").read_text() == before


@pytest.mark.parametrize(
    "dirs",
    [("/srv/a/fleet", "/srv/b/fleet"), ("relative/fleet",), ("/srv/a/-bad",)],
    ids=["duplicate-id", "relative", "bad-id"],
)
def test_home_list_needs_absolute_paths_with_unique_ids(homes, dirs):
    result = homes.run(*dirs)
    assert result.returncode == 2
    assert not (homes.out / "home-summary.json").exists()


def test_installer_renders_units_and_enables_the_timer(tmp_path):
    root, calls = tmp_path / "root", tmp_path / "calls"
    fake = tmp_path / "systemctl"
    fake.write_text(f'#!/usr/bin/env bash\necho "$*" >> {calls}\n')
    fake.chmod(0o755)
    result = subprocess.run(
        ["bash", str(DIR / "install-fleet-publisher.sh")],
        env={
            **os.environ,
            "ADMIN_USER": "fleetadmin",
            "FM_HOMES": " /srv/fleet-home   /srv/main-home ",
            "TASKS_AXI": "/opt/tools/bin/tasks-axi",
            "INSTALL_ROOT": str(root),
            "SYSTEMCTL": str(fake),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    units = root / "etc/systemd/system"
    service = configparser.ConfigParser(interpolation=None)
    service.optionxform = str
    service.read_string((units / "lifekit-fleet-publisher.service").read_text())
    assert service["Service"]["User"] == "fleetadmin"
    assert (
        service["Service"]["Environment"]
        == '"FM_HOMES=/srv/fleet-home /srv/main-home" "TASKS_AXI=/opt/tools/bin/tasks-axi"'
    )
    assert service["Service"]["Type"] == "oneshot"
    assert "__" not in (units / "lifekit-fleet-publisher.service").read_text()

    timer = configparser.ConfigParser(interpolation=None)
    timer.read_string((units / "lifekit-fleet-publisher.timer").read_text())
    assert timer["Timer"]["OnUnitActiveSec"] == "1min"

    script = root / "usr/local/bin/lifekit-fleet-publisher.sh"
    assert os.access(script, os.X_OK)
    assert script.read_text() == SCRIPT.read_text()
    helper = root / "usr/local/bin/lifekit-fleet-status.py"
    assert os.access(helper, os.X_OK)
    assert helper.read_text() == (DIR / "fleet_status.py").read_text()
    assert service["Service"]["ExecStart"].startswith(
        "/usr/local/bin/lifekit-fleet-publisher.sh "
    )
    assert (root / "var/lib/lifekit-fleet").is_dir()
    assert (root / "var/lib/node_exporter/textfile").is_dir()
    assert calls.read_text().splitlines() == [
        "daemon-reload",
        "enable --now lifekit-fleet-publisher.timer",
    ]


def test_installer_finds_tasks_axi_through_an_interactive_admin_shell(tmp_path):
    # A plain login shell lacks the node version manager set up in .bashrc: the
    # fake su answers only the interactive form.
    fake_su = tmp_path / "su"
    fake_su.write_text(
        "#!/usr/bin/env bash\n"
        'case "$4" in *"bash -ic"*) echo "noise"; echo /home/admin/.nvm/bin/tasks-axi;; esac\n'
    )
    fake_su.chmod(0o755)
    result = run_installer(
        tmp_path, SU=str(fake_su), TASKS_AXI="", FM_HOME="/srv/fleet-home"
    )
    assert result.returncode == 0, result.stderr
    unit = (
        tmp_path / "root/etc/systemd/system/lifekit-fleet-publisher.service"
    ).read_text()
    assert "TASKS_AXI=/home/admin/.nvm/bin/tasks-axi" in unit


def run_installer(tmp_path, **env):
    fake = tmp_path / "systemctl"
    fake.write_text("#!/usr/bin/env bash\n")
    fake.chmod(0o755)
    return subprocess.run(
        ["bash", str(DIR / "install-fleet-publisher.sh")],
        env={
            **{k: v for k, v in os.environ.items() if k not in ("FM_HOME", "FM_HOMES")},
            "TASKS_AXI": "/opt/tools/bin/tasks-axi",
            "INSTALL_ROOT": str(tmp_path / "root"),
            "SYSTEMCTL": str(fake),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_defaults_the_home_list_to_fm_home(tmp_path):
    result = run_installer(tmp_path, FM_HOME="/srv/fleet-home")
    assert result.returncode == 0, result.stderr
    unit = tmp_path / "root/etc/systemd/system/lifekit-fleet-publisher.service"
    assert '"FM_HOMES=/srv/fleet-home" ' in unit.read_text()


@pytest.mark.parametrize("home", ["relative/home", '/srv/a"b', "/srv/a|b"])
def test_installer_refuses_a_home_it_cannot_render(tmp_path, home):
    result = run_installer(tmp_path, FM_HOMES=f"/srv/ok {home}")
    assert result.returncode == 1
    assert not (tmp_path / "root/etc/systemd").exists()


def test_installer_skips_without_a_fleet_home():
    result = subprocess.run(
        ["bash", str(DIR / "install-fleet-publisher.sh")],
        env={k: v for k, v in os.environ.items() if k not in ("FM_HOME", "FM_HOMES")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "skipping" in result.stderr


# --- started_at / landed_at / priority / ask sidecar -------------------------


def test_items_gain_start_land_times_and_backlog_priority(fleet):
    fleet.write(
        summary(
            active_children=[{"id": "a", "state": "working"}, {"id": "z"}],
            landed=[{"id": "m"}, {"id": "c"}, {"id": "n"}],
            queued=[{"id": "q1"}, {"id": "q2"}],
        ),
        ledger=[
            {"ts": 100, "event": "task.dispatched", "task": "a"},
            {"ts": 150, "event": "task.dispatched", "task": "a"},
            {"ts": 120, "event": "task.dispatched", "task": "m"},
            {"ts": 300, "event": "task.merged", "task": "m"},
            {"ts": 400, "event": "task.cleaned_up", "task": "m"},
            {"ts": 500, "event": "task.cleaned_up", "task": "c"},
        ],
    )
    (fleet.tmp / "data" / "backlog.md").write_text(
        "# Backlog\n\n## In flight\n"
        "- [ ] q1 - First thing (repo: x) (kind: ship) (priority: 2) (since 2026-10-01)\n"
        "  (priority: 9) is not on an item line\n"
        "- [ ] q2 - No priority here (repo: x)\n"
    )
    assert fleet.run().returncode == 0
    got = fleet.published()
    assert [c.get("started_at") for c in got["active_children"]] == [150, None]
    assert [x.get("landed_at") for x in got["landed"]] == [300, 500, None]
    assert [q.get("priority") for q in got["queued"]] == [2, None]


def valid_ask(**extra):
    return {
        "question": "Ship it?",
        "options": [
            {"id": "yes", "label": "Yes", "recommended": True},
            {"id": "no", "label": "No"},
        ],
        "free_text_allowed": True,
        **extra,
    }


def ask_dir(fleet, id_):
    d = fleet.tmp / "data" / id_
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_ask_sidecar_fills_a_decision_without_an_ask(fleet):
    own = {"question": "From snapshot", "options": [], "free_text_allowed": True}
    fleet.write(
        summary(
            decisions_open=[decision("d1"), decision("d2", ask=own), decision("d3")]
        )
    )
    (ask_dir(fleet, "d1") / "ask.json").write_text(json.dumps(valid_ask()))
    (ask_dir(fleet, "d2") / "ask.json").write_text(json.dumps(valid_ask()))
    assert fleet.run().returncode == 0, fleet.run().stderr
    d1, d2, d3 = fleet.published()["decisions_open"]
    assert d1["ask"] == {**valid_ask(), "link": None} | {
        "options": [
            {"id": "yes", "label": "Yes", "recommended": True},
            {"id": "no", "label": "No", "recommended": False},
        ]
    }
    assert d2["ask"] == own  # the snapshot's own ask wins
    assert "ask" not in d3


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        json.dumps(valid_ask(options=[{"id": "a", "label": "A"}] * 7)),
        json.dumps(
            valid_ask(
                options=[
                    {"id": "a", "label": "A"},
                    {"id": "b", "label": "B", "recommended": True},
                ]
            )
        ),
        json.dumps(valid_ask(question="")),
        json.dumps(valid_ask(link="http://insecure.example")),
        json.dumps(valid_ask(note="sk-ant-" + "x" * 20)),
        json.dumps(valid_ask(free_text_allowed="yes")),
    ],
)
def test_invalid_ask_sidecar_is_skipped_not_fatal(fleet, bad):
    fleet.write(summary(decisions_open=[decision("d1")]))
    (ask_dir(fleet, "d1") / "ask.json").write_text(bad)
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    assert "ask" not in fleet.published()["decisions_open"][0]
    assert "ask.json" in result.stderr


def test_ask_sidecar_outside_the_data_dir_is_not_read(fleet, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "ask.json"
    outside.write_text(json.dumps(valid_ask()))
    fleet.write(summary(decisions_open=[decision("d1")]))
    (ask_dir(fleet, "d1") / "ask.json").symlink_to(outside)
    assert fleet.run().returncode == 0
    assert "ask" not in fleet.published()["decisions_open"][0]


# --- PC home -------------------------------------------------------------


def pc_cache(fleet, **overrides):
    cache = fleet.tmp / "state" / "secondmate-summary-cache"
    cache.mkdir(parents=True, exist_ok=True)
    body = summary(
        **{
            "generated_epoch": NOW - 60,
            "state": "no_active_work",
            "active_children": [],
            "counts": {"active_children": 0, "landed": 10},
            "landed": [{"id": f"l{i}"} for i in range(10)],
            **overrides,
        }
    )
    (cache / "61ab.json").write_text(json.dumps(body))
    return cache


def pc_entry(fleet):
    return [h for h in fleet.published()["homes"] if h["id"] == "pc"]


def test_pc_home_shows_state_and_last_seen_only(fleet):
    fleet.write(summary())
    pc_cache(fleet)
    result = fleet.run(FM_HOMES=str(fleet.tmp))
    assert result.returncode == 0, result.stderr
    got = fleet.published()
    assert pc_entry(fleet) == [
        {
            "id": "pc",
            "path": None,
            "published": True,
            "remote": True,
            "valid": True,
            "reason": None,
            "last_seen_epoch": NOW - 60,
            "state": "idle",
        }
    ]
    # No count, no list items, and the fleet's own fields are untouched.
    assert "counts" not in pc_entry(fleet)[0]
    assert all(
        x["home_id"] != "pc" for x in got["active_children"] + got.get("landed", [])
    )
    assert got["valid"] is True and got["generated_epoch"] == NOW - 30
    assert 'home="pc"' not in (fleet.prom / "fleet.prom").read_text()


def test_pc_home_is_working_when_it_has_active_children(fleet):
    fleet.write(summary())
    cache = pc_cache(fleet)
    f = cache / "61ab.json"
    body = json.loads(f.read_text())
    body["counts"]["active_children"] = 2
    f.write_text(json.dumps(body))
    assert fleet.run(FM_HOMES=str(fleet.tmp)).returncode == 0
    assert pc_entry(fleet)[0]["state"] == "working"


def test_stale_pc_summary_is_off_never_an_error(fleet):
    fleet.write(summary())
    pc_cache(fleet, generated_epoch=NOW - 99_999)
    result = fleet.run(FM_HOMES=str(fleet.tmp))
    assert result.returncode == 0, result.stderr
    got = fleet.published()
    assert pc_entry(fleet)[0]["state"] == "off"
    assert pc_entry(fleet)[0]["last_seen_epoch"] == NOW - 99_999
    assert got["valid"] is True and got.get("reason") is None


@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps({"schema": "other"}), "sk-ant-" + "x" * 30],
)
def test_unreadable_pc_summary_is_off_without_a_last_seen(fleet, content):
    fleet.write(summary())
    cache = fleet.tmp / "state" / "secondmate-summary-cache"
    cache.mkdir(parents=True)
    (cache / "x.json").write_text(content)
    result = fleet.run(FM_HOMES=str(fleet.tmp))
    assert result.returncode == 0, result.stderr
    assert pc_entry(fleet)[0]["state"] == "off"
    assert pc_entry(fleet)[0]["last_seen_epoch"] is None
    assert "sk-ant" not in (fleet.out / "home-summary.json").read_text()


def test_no_pc_cache_means_no_pc_entry(fleet):
    fleet.write(summary())
    assert fleet.run(FM_HOMES=str(fleet.tmp)).returncode == 0
    assert pc_entry(fleet) == []


def pc_cache_in(home, name="61ab.json"):
    cache = home / "state" / "secondmate-summary-cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / name).write_text(
        json.dumps(summary(generated_epoch=NOW - 60, counts={"active_children": 0}))
    )
    return cache / name


def homes_pc_entry(homes):
    return [h for h in homes.published()["homes"] if h["id"] == "pc"]


def test_pc_cache_in_a_later_home_is_published(homes):
    first = make_home(homes.tmp, "fm-vps", summary(home_id="vps"))
    main = make_home(homes.tmp, "firstmate", summary(home_id="main"))
    pc_cache_in(main)
    result = homes.run(first, main)
    assert result.returncode == 0, result.stderr
    pc = homes_pc_entry(homes)
    assert len(pc) == 1 and pc[0]["state"] == "idle"
    assert pc[0]["last_seen_epoch"] == NOW - 60


def test_first_home_with_exactly_one_cached_summary_wins(homes):
    first = make_home(homes.tmp, "fm-vps", summary(home_id="vps"))
    main = make_home(homes.tmp, "firstmate", summary(home_id="main"))
    pc_cache_in(first, "a.json")
    pc_cache_in(first, "b.json")
    pc_cache_in(main).write_text(
        json.dumps(summary(generated_epoch=NOW - 90, counts={"active_children": 1}))
    )
    assert homes.run(first, main).returncode == 0
    pc = homes_pc_entry(homes)
    assert pc[0]["last_seen_epoch"] == NOW - 90 and pc[0]["state"] == "working"


def test_pc_summary_file_overrides_every_home_cache(homes, tmp_path_factory):
    first = make_home(homes.tmp, "fm-vps", summary(home_id="vps"))
    main = make_home(homes.tmp, "firstmate", summary(home_id="main"))
    pc_cache_in(main)
    named = tmp_path_factory.mktemp("pc") / "pc.json"
    named.write_text(json.dumps(summary(generated_epoch=NOW - 120)))
    assert homes.run(first, main, PC_SUMMARY_FILE=str(named)).returncode == 0
    assert homes_pc_entry(homes)[0]["last_seen_epoch"] == NOW - 120


def test_no_pc_cache_in_any_home_means_no_pc_entry(homes):
    first = make_home(homes.tmp, "fm-vps", summary(home_id="vps"))
    main = make_home(homes.tmp, "firstmate", summary(home_id="main"))
    assert homes.run(first, main).returncode == 0
    assert homes_pc_entry(homes) == []


# --- quota history and snapshot ------------------------------------------


def quota_row(ts, key="e00c3368", used=10, **extra):
    return {
        "v": 1,
        "ts": ts,
        "at": "2026-10-16T18:07:02Z",
        "source_tool": "quota-axi --provider claude --full --json",
        "provider": "claude",
        "plan": "max",
        "account_key": key,
        "source": "oauth",
        "status": "fresh",
        "stale": False,
        "refreshed_at": "2026-10-16T18:07:04.106Z",
        "error": None,
        "five_hour": {"percent_used": used, "resets_at": "2026-10-16T23:10:00+00:00"},
        "seven_day": {"percent_used": 75, "resets_at": "2026-10-19T06:00:00+00:00"},
        "fable_week": {"percent_used": 3, "resets_at": "2026-10-19T06:00:00+00:00"},
        "ok": True,
        **extra,
    }


def write_quota(fleet, *rows):
    src = fleet.tmp / "data" / "fleet-metrics"
    src.mkdir(parents=True, exist_ok=True)
    (src / "quota.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def read_quota(fleet):
    f = fleet.out / "metrics" / "quota.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()]


def test_quota_history_is_sanitised_to_percentages_resets_and_slot(fleet):
    fleet.write(summary())
    write_quota(
        fleet,
        quota_row(2, key="bbbb2222", error="Claude quota endpoint rate limited"),
        quota_row(1),
        quota_row(3, key=""),
        {"ts": "bad"},
    )
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    rows = read_quota(fleet)
    # Numbered by first appearance in time order, whatever the file order.
    assert [(r["ts"], r["slot"]) for r in rows] == [(1, 1), (2, 2), (3, None)]
    assert set(rows[0]) == {"ts", "slot", "five_hour", "seven_day", "fable_week"}
    text = (fleet.out / "metrics" / "quota.jsonl").read_text()
    for leak in ("e00c3368", "bbbb2222", "account_key", "rate limited", "quota-axi"):
        assert leak not in text
    # The slot ledger is private, host-side, and holds hashes, never keys.
    ledger = fleet.tmp / "state" / "quota-slots"
    assert ledger.stat().st_mode & 0o777 == 0o600
    assert "e00c3368" not in ledger.read_text()
    assert not list(fleet.out.rglob("quota-slots"))
    assert (fleet.out / "metrics" / "quota.jsonl").stat().st_mode & 0o777 == 0o644


def test_quota_slot_map_pins_slot_numbers(fleet):
    fleet.write(summary())
    write_quota(fleet, quota_row(1, key="aaaa1111"), quota_row(2, key="bbbb2222"))
    assert fleet.run(QUOTA_SLOT_MAP="bbbb2222=1 aaaa1111=4").returncode == 0
    assert [(r["ts"], r["slot"]) for r in read_quota(fleet)] == [(1, 4), (2, 1)]


def test_quota_slots_stay_with_their_account_when_the_source_changes(fleet):
    fleet.write(summary())
    write_quota(fleet, quota_row(1, key="aaaa1111"), quota_row(2, key="bbbb2222"))
    assert fleet.run().returncode == 0
    # The first account's rows leave the source and a new account shows up first.
    write_quota(
        fleet,
        quota_row(3, key="cccc3333"),
        quota_row(4, key="bbbb2222"),
        quota_row(5, key="aaaa1111"),
    )
    assert fleet.run().returncode == 0
    assert [(r["ts"], r["slot"]) for r in read_quota(fleet)] == [
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 2),
        (5, 1),
    ]


def test_quota_slot_map_wins_over_the_ledger_and_is_not_recycled(fleet):
    fleet.write(summary())
    write_quota(fleet, quota_row(1, key="aaaa1111"))
    assert fleet.run().returncode == 0
    write_quota(fleet, quota_row(2, key="aaaa1111"), quota_row(3, key="bbbb2222"))
    assert fleet.run(QUOTA_SLOT_MAP="aaaa1111=7").returncode == 0
    assert [(r["ts"], r["slot"]) for r in read_quota(fleet)] == [(1, 1), (2, 7), (3, 8)]


def test_unreadable_quota_sample_is_a_null_percentage(fleet):
    fleet.write(summary())
    write_quota(fleet, quota_row(1, five_hour=None, seven_day=None, fable_week=None))
    assert fleet.run().returncode == 0
    (row,) = read_quota(fleet)
    for w in ("five_hour", "seven_day", "fable_week"):
        assert row[w] == {"percent_used": None, "resets_at": None}


def test_quota_history_survives_the_source_and_never_duplicates(fleet):
    fleet.write(summary())
    write_quota(fleet, quota_row(1), quota_row(2))
    assert fleet.run().returncode == 0
    write_quota(fleet, quota_row(2, used=50), quota_row(3))
    assert fleet.run().returncode == 0
    rows = read_quota(fleet)
    assert [r["ts"] for r in rows] == [1, 2, 3]
    assert rows[1]["five_hour"]["percent_used"] == 50


def test_quota_rows_with_out_of_range_percentages_are_dropped(fleet):
    fleet.write(summary())
    write_quota(fleet, quota_row(1, used=250), quota_row(2))
    assert fleet.run().returncode == 0
    assert [r["ts"] for r in read_quota(fleet)] == [2]


def test_missing_quota_source_publishes_no_quota_files(fleet):
    fleet.write(summary())
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    assert not (fleet.out / "metrics" / "quota.jsonl").exists()
    assert not (fleet.out / "metrics" / "quota-snapshot.json").exists()


GAUGE = """claude_quota_percent_remaining{window="five_hour"} 91
claude_quota_burn_multiple{window="five_hour"} 0.268
claude_quota_reserve_percent_points{window="five_hour"} 24.5802
claude_quota_resets_at_seconds{window="five_hour"} 1792192200
claude_quota_percent_remaining{window="seven_day"} 24
claude_quota_burn_multiple{window="seven_day"} 1.1624
claude_quota_reserve_percent_points{window="seven_day"} -10.6157
claude_quota_resets_at_seconds{window="seven_day"} 1792389600
claude_quota_percent_remaining{window="model:fable"} 97
claude_quota_runway_seconds 124877
claude_quota_projected_exhausted_at_seconds 1792305121
claude_quota_runway_status{status="projected_exhaustion"} 1
claude_quota_limiting_window{window="seven_day"} 1
"""


def test_quota_snapshot_comes_from_the_gauge_textfile(fleet):
    fleet.write(summary())
    (fleet.prom / "claude_quota.prom").write_text(GAUGE)
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    snap = json.loads((fleet.out / "metrics" / "quota-snapshot.json").read_text())
    assert snap["schema"] == "fleet-quota-snapshot.v1"
    assert snap["windows"]["five_hour"] == {
        "percent_remaining": 91,
        "burn_multiple": 0.268,
        "reserve_percent_points": 24.5802,
        "resets_at": 1792192200,
    }
    assert snap["windows"]["seven_day"]["reserve_percent_points"] == -10.6157
    assert snap["windows"]["model:fable"]["percent_remaining"] == 97
    assert snap["runway_seconds"] == 124877
    assert snap["projected_exhausted_at"] == 1792305121
    assert snap["runway_status"] == "projected_exhaustion"
    assert snap["limiting_window"] == "seven_day"
    assert isinstance(snap["generated_epoch"], int)


# --- status page sections -----------------------------------------------------

_SPEC = importlib.util.spec_from_file_location("fleet_status", DIR / "fleet_status.py")
fleet_status = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fleet_status)

LIST_COLUMNS = (
    "id,state,kind,repo,title,held,hold_kind,hold_until,hold_reason,priority,"
    "closed,links,blocked"
)


def task(id_, state="queued", repo="alpha", title=None, **extra):
    base = {
        "id": id_,
        "state": state,
        "kind": "ship",
        "repo": repo,
        "title": title or f"Task {id_}",
        "held": "no",
        "hold_kind": None,
        "hold_until": None,
        "hold_reason": None,
        "priority": None,
        "closed": None,
        "links": None,
        "blocked": "no",
    }
    return {**base, **extra}


def toon_cell(value):
    if value is None:
        return '"-"'
    text = str(value)
    return json.dumps(text) if any(c in text for c in ',"\n ') else text


def toon_list(tasks):
    cols = LIST_COLUMNS.split(",")
    rows = "".join(
        "  " + ",".join(toon_cell(t.get(c)) for c in cols) + "\n" for t in tasks
    )
    return f"count: {len(tasks)}\ntasks[{len(tasks)}]{{{LIST_COLUMNS}}}:\n{rows}"


def status_home(root, name, tasks, shows=None, capacity=None, ledger=()):
    """A fleet home with a backlog wrapper and a capacity script. `list` prints
    the tasks; `show <id> --full` prints shows[id]; every call is logged."""
    home = make_home(root, name, summary(), ledger=ledger)
    (home / "bin").mkdir(exist_ok=True)
    (home / "list.txt").write_text(toon_list(tasks))
    for id_, fields in (shows or {}).items():
        body = "".join(f"  {k}: {json.dumps(v)}\n" for k, v in fields.items())
        (home / f"show-{id_}.txt").write_text(f"task:\n  id: {id_}\n{body}")
    stub(
        home / "bin/fm-tasks-axi.sh",
        'echo "$*" >> "$FM_HOME/calls.log"\n'
        'case "$1" in list) cat "$FM_HOME/list.txt";; '
        'show) cat "$FM_HOME/show-$2.txt";; *) exit 2;; esac\n',
    )
    cap = {
        "schema": "fm-lane-capacity.v1",
        "generated_epoch": NOW - 5,
        "machine": "box",
        "lanes": {"count": 1, "ids": ["x"]},
        "cap": {"target": None, "status": "none"},
        "quota": None,
        **(capacity or {}),
    }
    (home / "capacity.json").write_text(json.dumps(cap))
    stub(home / "bin/fm-capacity.sh", 'cat "$FM_HOME/capacity.json"\n')
    return home


def clone(home, repo, origin):
    path = home / "projects" / repo
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin], check=True
    )


def vault(root, *plans):
    path = root / "vault"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "remote",
            "add",
            "origin",
            "https://github.com/o/vault.git",
        ],
        check=True,
    )
    for project in plans:
        (path / "projects" / project).mkdir(parents=True)
        (path / "projects" / project / "plan.md").write_text("# plan\n")
    return path


def fake_gh(root, counts):
    """gh that answers `pr list -R <slug>` with counts[slug] PRs, logging each call."""
    (root / "gh-counts.json").write_text(json.dumps(counts))
    return stub(
        root / "gh",
        f'echo "$*" >> {root}/gh.log\n'
        f'python3 -c \'import json,sys; n=json.load(open("{root}/gh-counts.json"))'
        '[sys.argv[1]]; print(json.dumps([{"number": i} for i in range(n)]))\' "$4"\n',
    )


def status(homes):
    return homes.published()["status_page"]


def test_records_come_whole_from_each_home_backlog_with_their_own_repo(homes):
    long = "A title long enough that the list cuts it " + "x" * 80
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [
            task("q1", priority=2),
            task(
                "cut",
                title=long[:60]
                + "\n... (truncated, 130 chars total - use show cut --full)",
            ),
            task(
                "h1",
                held="yes",
                hold_kind="captain",
                hold_until="2026-11-02",
                hold_reason="Pick a or b",
            ),
            task("p1", held="yes", hold_kind="parked", hold_reason="after the bump"),
            task("f1", state="in_flight", repo="beta"),
            task(
                "d1",
                state="done",
                closed="2027-01-14",
                links="pr:https://github.com/o/alpha/pull/7",
            ),
            task("old", state="done", closed="2027-01-07"),
            task("orphan", repo=None),
        ],
        shows={"cut": {"title": long}},
    )
    main = status_home(homes.tmp, "firstmate", [task("m1", repo="beta")])
    result = homes.run(vps, main)
    assert result.returncode == 0, result.stderr
    page = status(homes)
    assert page["schema"] == "fleet-status-page.v1"
    assert page["errors"] == []
    got = {(r["home_id"], r["id"]): r for r in page["records"]}
    assert set(got) == {
        ("fm-vps", i) for i in ("q1", "cut", "h1", "p1", "f1", "d1", "orphan")
    } | {("firstmate", "m1")}
    assert got["fm-vps", "cut"]["title"] == long
    assert got["fm-vps", "q1"]["priority"] == 2
    assert got["fm-vps", "h1"]["state"] == "held"
    assert got["fm-vps", "h1"]["hold"] == {
        "kind": "captain",
        "owner": "captain",
        "until": "2026-11-02",
        "reason": "Pick a or b",
    }
    assert got["fm-vps", "p1"]["hold"]["owner"] == "firstmate"
    assert got["fm-vps", "f1"]["state"] == "in_flight"
    assert got["fm-vps", "d1"]["pr_url"] == "https://github.com/o/alpha/pull/7"
    assert got["fm-vps", "orphan"]["repo"] is None
    assert ("fm-vps", "old") not in got
    # The backlog is read through the home's own wrapper, from the home.
    assert "list --limit 1000 --fields" in (vps / "calls.log").read_text()


def test_a_full_backlog_list_is_reported_as_possibly_incomplete(homes):
    vps = status_home(
        homes.tmp, "fm-vps", [task(f"t{i}") for i in range(fleet_status.LIST_LIMIT)]
    )
    assert homes.run(vps).returncode == 0
    page = status(homes)
    assert len(page["records"]) == fleet_status.LIST_LIMIT
    assert any("row limit" in e for e in page["errors"])


def test_a_stalled_gh_cannot_starve_the_other_sources(homes, monkeypatch):
    monkeypatch.setattr(fleet_status, "PR_BUDGET", 5)
    gh = stub(homes.tmp / "gh", "sleep 0.1\necho '[]'\n")
    errors: list[str] = []
    cache: dict = {}
    monkeypatch.setenv("FLEET_GH", gh)
    fleet_status.open_prs({"o/a", "o/b", "o/c"}, cache, NOW, errors)
    assert errors == []
    assert set(cache) == {"o/a", "o/b", "o/c"}
    monkeypatch.setattr(fleet_status, "PR_BUDGET", 2)
    stall = stub(homes.tmp / "gh2", "sleep 30\n")
    monkeypatch.setenv("FLEET_GH", stall)
    errors, cache = [], {"o/c": {"count": 4, "as_of": NOW - 3600}}
    started = time.monotonic()
    fleet_status.open_prs({"o/a", "o/b", "o/c"}, cache, NOW, errors)
    assert time.monotonic() - started < 3
    assert cache == {"o/c": {"count": 4, "as_of": NOW - 3600}}
    assert errors == [
        "open PRs of o/a unreadable (TimeoutExpired)",
        "open PRs not refreshed: the time budget ran out",
    ]


def test_a_cut_row_is_shown_once_and_then_served_from_the_cache(homes):
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [
            task(
                "cut",
                title="Short\n... (truncated, 90 chars total - use show cut --full)",
            )
        ],
        shows={"cut": {"title": "Short and then the rest"}},
    )
    for _ in range(2):
        assert homes.run(vps).returncode == 0
    assert (vps / "calls.log").read_text().count("show cut --full") == 1
    assert status(homes)["records"][0]["title"] == "Short and then the rest"


def test_cut_rows_past_the_show_budget_keep_the_list_cells(homes):
    cut = "Short\n... (truncated, 90 chars total - use show cut --full)"
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [task("cut", title=cut)],
        shows={"cut": {"title": "Short and then the rest"}},
    )
    cache: dict = {}
    errors: list[str] = []
    got, unresolved = fleet_status.read_records(
        "fm-vps", str(vps), "", cache, NOW, errors, time.monotonic() - 1
    )
    assert (got[0]["title"], unresolved, cache) == (cut, 1, {})
    assert not (vps / "calls.log").read_text().count("show")
    got, unresolved = fleet_status.read_records(
        "fm-vps", str(vps), "", cache, NOW, errors, time.monotonic() + 30
    )
    assert (got[0]["title"], unresolved) == ("Short and then the rest", 0)
    assert errors == []
    stub(
        vps / "bin/fm-tasks-axi.sh",
        'case "$1" in list) cat "$FM_HOME/list.txt";; show) sleep 30;; esac\n',
    )
    cache = {}
    started = time.monotonic()
    got, unresolved = fleet_status.read_records(
        "fm-vps", str(vps), "", cache, NOW, errors, time.monotonic() + 2
    )
    assert time.monotonic() - started < 3
    assert (got[0]["title"], unresolved, cache) == (cut, 1, {})


def test_a_failing_show_keeps_the_home_and_the_list_cells(homes):
    gone = "Gone\n... (truncated, 90 chars total - use show gone --full)"
    bare = "Bare\n... (truncated, 90 chars total - use show bare --full)"
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [task("q1"), task("gone", title=gone), task("bare", title=bare)],
        shows={"bare": {"kind": "ship"}},
    )
    assert homes.run(vps).returncode == 0
    page = status(homes)
    titles = {r["id"]: r["title"] for r in page["records"]}
    assert titles == {"q1": "Task q1", "gone": gone, "bare": bare}
    assert [e for e in page["errors"] if "not shown in full" in e] == [
        "1 cut record(s) not shown in full: show failed or ran out of time"
    ]
    assert homes.run(vps).returncode == 0
    assert (vps / "calls.log").read_text().count("show gone --full") == 2


def test_projects_roll_up_by_repo_with_no_project_last(homes):
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [
            task("a1"),
            task("a2", held="yes", hold_kind="external"),
            task("a3", state="in_flight"),
            task("a4", state="done", closed="2027-01-09"),
            task(
                "a5",
                state="done",
                closed="2027-01-14",
                links="pr:https://github.com/o/alpha/pull/9",
            ),
            task("b1", repo="o/beta"),
            task("n1", repo=None),
        ],
    )
    clone(vps, "alpha", "git@github.com:o/alpha.git")
    plans = vault(homes.tmp, "alpha")
    gh = fake_gh(homes.tmp, {"o/alpha": 3, "o/beta": 0})
    result = homes.run(vps, FLEET_GH=gh, FLEET_VAULT=str(plans))
    assert result.returncode == 0, result.stderr
    rows = {p["name"]: p for p in status(homes)["projects"]}
    assert [p["name"] for p in status(homes)["projects"]] == [
        "alpha",
        "o/beta",
        "no project",
    ]
    assert rows["alpha"] | {"last_landed": None} == {
        "repo": "alpha",
        "name": "alpha",
        "github": "o/alpha",
        "in_flight": 1,
        "queued": 1,
        "held": 1,
        "last_landed": None,
        "open_prs": 3,
        "open_prs_as_of": NOW,
        "plan_url": "https://github.com/o/vault/blob/HEAD/projects/alpha/plan.md",
    }
    assert rows["alpha"]["last_landed"] == {
        "id": "a5",
        "home_id": "fm-vps",
        "title": "Task a5",
        "closed": "2027-01-14",
        "pr_url": "https://github.com/o/alpha/pull/9",
    }
    assert (rows["o/beta"]["open_prs"], rows["o/beta"]["plan_url"]) == (0, None)
    assert rows["no project"] | {"queued": 0} == {
        "repo": None,
        "name": "no project",
        "github": None,
        "in_flight": 0,
        "queued": 0,
        "held": 0,
        "last_landed": None,
        "open_prs": None,
        "open_prs_as_of": None,
        "plan_url": None,
    }
    assert rows["no project"]["queued"] == 1


def test_open_prs_are_cached_and_a_failed_refresh_keeps_the_last_count(homes):
    vps = status_home(homes.tmp, "fm-vps", [task("b1", repo="o/beta")])
    gh = fake_gh(homes.tmp, {"o/beta": 2})
    assert homes.run(vps, FLEET_GH=gh).returncode == 0
    assert homes.run(vps, FLEET_GH=gh, FLEET_NOW=str(NOW + 60)).returncode == 0
    assert (homes.tmp / "gh.log").read_text().count("pr list") == 1
    # Past the TTL gh is asked again; when it fails the last count stays.
    broken = stub(homes.tmp / "gh-broken", "exit 1\n")
    result = homes.run(vps, FLEET_GH=broken, FLEET_NOW=str(NOW + 700))
    assert result.returncode == 0, result.stderr
    (row,) = status(homes)["projects"]
    assert (row["open_prs"], row["open_prs_as_of"]) == (2, NOW)
    assert status(homes)["errors"] == [
        "open PRs of o/beta unreadable (CalledProcessError)"
    ]


def test_plan_map_names_the_vault_project_when_it_differs(homes):
    vps = status_home(homes.tmp, "fm-vps", [task("a1", repo="alpha-repo")])
    plans = vault(homes.tmp, "alpha")
    result = homes.run(vps, FLEET_VAULT=str(plans), FLEET_PLAN_MAP="alpha-repo=alpha")
    assert result.returncode == 0, result.stderr
    (row,) = status(homes)["projects"]
    assert row["plan_url"].endswith("/projects/alpha/plan.md")


def test_capacity_is_published_per_home_with_an_idle_reason(homes):
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [task("q1")],
        capacity={"lanes": {"count": 2}, "cap": {"target": 2, "status": "ok"}},
    )
    main = status_home(
        homes.tmp, "firstmate", [task("h1", held="yes", hold_kind="captain")]
    )
    pc_cache_in(vps)
    result = homes.run(vps, main)
    assert result.returncode == 0, result.stderr
    caps = {c["home_id"]: c for c in status(homes)["capacity"]}
    assert caps["fm-vps"] == {
        "home_id": "fm-vps",
        "machine": "box",
        "running": 2,
        "allowed": 2,
        "cap_status": "ok",
        "quota": None,
        "generated_epoch": NOW - 5,
        "idle_reason": "cap-reached",
    }
    assert (caps["firstmate"]["allowed"], caps["firstmate"]["idle_reason"]) == (
        None,
        "all-held",
    )
    assert caps["pc"]["running"] is None
    assert caps["pc"]["idle_reason"] is None
    result = homes.run(vps, main, FLEET_NOW=str(NOW + 5000))
    caps = {c["home_id"]: c for c in status(homes)["capacity"]}
    assert caps["pc"]["idle_reason"] == "host-offline"


@pytest.mark.parametrize(
    ("running", "allowed", "quota", "queued", "held", "reason"),
    [
        (2, 2, None, 5, 0, "cap-reached"),
        (0, 2, "exhausted_now", 5, 0, "quota"),
        (0, 2, None, 0, 3, "all-held"),
        (0, None, None, 0, 0, "queue-empty"),
        (1, 2, None, 4, 0, None),
        (1, None, "projected_exhaustion", 4, 0, None),
    ],
)
def test_idle_reason_is_one_of_the_fixed_list(
    running, allowed, quota, queued, held, reason
):
    cap = {"running": running, "allowed": allowed, "quota": quota}
    assert fleet_status.idle_reason(cap, queued, held) == reason


def test_queue_has_its_count_top_ten_and_seven_day_throughput(homes):
    tasks = [task(f"q{i:02}", priority=(None if i % 3 else 1)) for i in range(14)]
    tasks.append(task("held", held="yes", hold_kind="captain"))
    daily = [
        daily_row(date, merged_prs=n, runs=n + 1)
        for date, n in [
            ("2027-01-07", 99),  # eight days back: outside the window
            ("2027-01-08", 2),
            ("2027-01-14", 3),
            ("2027-01-15", 50),  # today: not a whole day yet
        ]
    ]
    vps = status_home(homes.tmp, "fm-vps", tasks)
    (vps / "data/fleet-metrics").mkdir()
    (vps / "data/fleet-metrics/daily.jsonl").write_text(
        "".join(json.dumps(r | {"home": "fm-vps"}) + "\n" for r in daily)
    )
    result = homes.run(vps)
    assert result.returncode == 0, result.stderr
    q = status(homes)["queue"]
    assert (q["count"], q["held"]) == (14, 1)
    # Priority first, the backlog's order within it, unprioritised last.
    assert [t["id"] for t in q["top"]] == [
        "q00",
        "q03",
        "q06",
        "q09",
        "q12",
        "q01",
        "q02",
        "q04",
        "q05",
        "q07",
    ]
    assert q["throughput"] == {
        "days": 7,
        "from": "2027-01-08",
        "to": "2027-01-14",
        "days_with_data": 2,
        "merged_prs": 5,
        "runs": 7,
    }


def test_intake_is_the_job_queue_depth(homes):
    vps = status_home(homes.tmp, "fm-vps", [])
    depth = stub(
        homes.tmp / "depth",
        'echo \'{"schema":"jobq-depth.v1","waiting":3,"running":1,"done_24h":4,"failed_24h":0}\'\n',
    )
    assert homes.run(vps, FLEET_INTAKE_CMD=depth).returncode == 0
    assert status(homes)["intake"] == {
        "waiting": 3,
        "running": 1,
        "done_24h": 4,
        "failed_24h": 0,
    }
    junk = stub(homes.tmp / "junk", "echo '{\"waiting\": -1}'\n")
    assert homes.run(vps, FLEET_INTAKE_CMD=junk).returncode == 0
    assert status(homes)["intake"] is None
    assert status(homes)["errors"] == ["intake depth unreadable (ValueError)"]


def test_how_it_works_lists_each_workflow_and_its_missing_owner_pages(homes):
    vps = status_home(homes.tmp, "fm-vps", [])
    assert homes.run(vps).returncode == 0
    page = status(homes)
    assert [w["id"] for w in page["how_it_works"]] == [
        "intake",
        "queue-to-merge",
        "monday-loop",
    ]
    assert all(w["url"] is None for w in page["how_it_works"])
    assert len(page["follow_ups"]) == 3


def test_a_record_that_looks_like_a_credential_is_withheld(homes):
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [task("ok"), task("leak", hold_reason="token ghp_" + "a" * 30, held="yes")],
    )
    result = homes.run(vps)
    assert result.returncode == 0, result.stderr
    page = status(homes)
    assert [r["id"] for r in page["records"]] == ["ok"]
    assert page["errors"] == [
        "1 record(s) withheld: they look like they hold a credential"
    ]


def test_an_unreadable_backlog_is_an_error_line_not_a_failure(homes):
    vps = status_home(homes.tmp, "fm-vps", [task("a1")])
    stub(vps / "bin/fm-tasks-axi.sh", "echo boom >&2; exit 1\n")
    result = homes.run(vps)
    assert result.returncode == 0, result.stderr
    page = status(homes)
    assert page["records"] == []
    assert page["errors"][0].startswith(
        "fm-vps: backlog unreadable (CalledProcessError"
    )
    # Capacity still reads; with no records it names no idle reason.
    assert page["capacity"][0]["idle_reason"] is None


def test_a_failing_helper_publishes_the_summary_with_a_null_status_page(homes):
    vps = status_home(homes.tmp, "fm-vps", [])
    broken = homes.tmp / "broken.py"
    broken.write_text("raise SystemExit(1)\n")
    result = homes.run(vps, FLEET_STATUS=str(broken))
    assert result.returncode == 0, result.stderr
    assert "fleet_status.py failed" in result.stderr
    assert homes.published()["status_page"] is None
    assert homes.published()["homes"][0]["published"] is True


def test_an_installed_publisher_finds_the_helper_beside_itself(homes, tmp_path):
    bindir = tmp_path / "usr-local-bin"
    bindir.mkdir()
    shutil.copy(SCRIPT, bindir / "lifekit-fleet-publisher.sh")
    shutil.copy(DIR / "fleet_status.py", bindir / "lifekit-fleet-status.py")
    vps = status_home(homes.tmp, "fm-vps", [task("a1")])
    result = subprocess.run(
        [
            "bash",
            str(bindir / "lifekit-fleet-publisher.sh"),
            str(homes.out),
            str(homes.prom),
        ],
        env={
            **os.environ,
            "FM_HOMES": str(vps),
            "FLEET_NOW": str(NOW),
            "FLEET_STATE_DIR": str(tmp_path / "state"),
            "LAVISH_STATE": str(tmp_path / "lavish.json"),
            **NO_LIVE_SOURCES,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert [r["id"] for r in status(homes)["records"]] == ["a1"]
