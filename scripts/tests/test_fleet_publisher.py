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
    "FLEET_PROM_URL": "",
    "FLEET_CLIP_STATS": "",
    "FLEET_SCREEN_REPOS": "",
    "FLEET_INTAKE_CMD": "",
    "FLEET_VAULT": "/nonexistent",
    "FLEET_POLICY_DIR": "/nonexistent",
    "FLEET_SYSTEMCTL": "",
    "FLEET_CRON_CMD": "",
    "FLEET_INTENTS_CMD": "",
    "FLEET_SCHEDULED_MANIFEST": "/nonexistent",
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
    assert not [
        p for p in fleet.out.iterdir() if p.name not in ("home-summary.json", "journal")
    ]
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


LAVISH_HOST = "http://lifekit-vps.tail1cb676.ts.net:4387"
LAVISH_ADDR = f"{LAVISH_HOST}/session/12b5d64c38b4ae60"


def test_board_address_in_the_full_question_fills_board_url_and_ask_link(fleet):
    # The glossary call's shape: the address only in the full question; the
    # short title and the 150-character reason do not carry it.
    question = (
        "Which glossary terms stay? Review the board at "
        f"https://example.com/docs and then {LAVISH_ADDR}."
    )
    fleet.write(
        summary(
            decisions_open=[
                decision(
                    "glossary-board-review",
                    title="Glossary board review",
                    reason="r" * 150,
                    question=question,
                    ask={
                        "question": "Keep these terms?",
                        "options": [],
                        "free_text_allowed": True,
                        "link": None,
                    },
                ),
                decision("plain", reason="no address here"),
            ]
        )
    )
    assert fleet.run().returncode == 0
    items = {d["id"]: d for d in fleet.published()["decisions_open"]}
    glossary = items["glossary-board-review"]
    assert glossary["board_url"] == LAVISH_ADDR
    assert glossary["ask"]["link"] == LAVISH_ADDR
    assert items["plain"]["board_url"] is None
    assert items["plain"].get("ask") is None


def test_open_session_beats_a_text_address_and_a_session_url_beats_another_link(fleet):
    data = str(fleet.tmp / "data")
    fleet.write(
        summary(
            decisions_open=[
                decision("on-disk", question=f"see {LAVISH_ADDR}"),
                decision(
                    "text-only", summary=f"docs https://x.test/a then {LAVISH_ADDR})"
                ),
                decision("other-link", reason="read https://x.test/page, please"),
            ]
        ),
        lavish=[session(f"{data}/on-disk/plan.html", f"{LAVISH_HOST}/session/disk")],
    )
    assert fleet.run().returncode == 0
    urls = {d["id"]: d["board_url"] for d in fleet.published()["decisions_open"]}
    assert urls == {
        "on-disk": f"{LAVISH_HOST}/session/disk",
        "text-only": LAVISH_ADDR,
        "other-link": "https://x.test/page",
    }


def test_another_services_session_link_does_not_outrank_the_lavish_host(fleet):
    data = str(fleet.tmp / "data")
    fleet.write(
        summary(
            decisions_open=[
                decision(
                    "glossary-board-review",
                    question=f"see https://other.example/session/abc then {LAVISH_ADDR}",
                ),
            ]
        ),
        lavish=[
            session(
                f"{data}/elsewhere/plan.html",
                f"{LAVISH_HOST}/session/gone",
                status="ended",
            )
        ],
    )
    assert fleet.run().returncode == 0
    (item,) = fleet.published()["decisions_open"]
    assert item["board_url"] == LAVISH_ADDR


def test_task_rows_get_open_url_from_their_board_or_their_text(fleet):
    data = str(fleet.tmp / "data")
    fleet.write(
        summary(
            active_children=[
                {"id": "a", "state": "working", "status": f"board up at {LAVISH_ADDR}"},
                {"id": "b", "state": "working"},
                {"id": "c", "state": "working"},
            ],
            queued=[{"id": "q", "reason": f"waits on {LAVISH_ADDR}"}, {"id": "z"}],
        ),
        lavish=[session(f"{data}/c/plan.html", f"{LAVISH_HOST}/session/c")],
    )
    assert fleet.run().returncode == 0
    published = fleet.published()
    assert {t["id"]: t.get("open_url") for t in published["active_children"]} == {
        "a": LAVISH_ADDR,
        "b": None,
        "c": f"{LAVISH_HOST}/session/c",
    }
    assert {t["id"]: t.get("open_url") for t in published["queued"]} == {
        "q": LAVISH_ADDR,
        "z": None,
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
# `list` answers the ids in LISTED_IDS (default: both decisions) from anywhere;
# every `show` is recorded in SHOW_LOG when set, and answers only from the home.
TASKS_AXI_ONLY_FROM_FM_HOME = (
    'if [[ "$1" == list ]]; then\n'
    '  echo "count: 0"; echo "tasks[0]{id,state,kind,repo,title,priority}:"\n'
    '  for i in ${LISTED_IDS-held-call other-call}; do echo "  $i,queued,ship,r,\\"T\\",-"; done\n'
    "  exit 0\n"
    "fi\n"
    '[[ -z "${SHOW_LOG:-}" ]] || echo "$2" >> "$SHOW_LOG"\n'
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


def test_decisions_without_a_backlog_record_are_not_looked_up_or_logged(
    fleet, tmp_path
):
    hold = stub(fleet.tmp / "hold.sh", HOLD_BINDS_BOUND_AND_WRONG)
    tasks = stub(fleet.tmp / "tasks.sh", TASKS_AXI_ONLY_FROM_FM_HOME)
    shows = tmp_path / "shows.log"
    bound_board_fleet(fleet)
    env = {
        "CAPTAIN_HOLD": hold,
        "TASKS_AXI": tasks,
        "FM_HOME": str(fleet.tmp),
        "EXPECTED_PWD": str(fleet.tmp),
        "LISTED_IDS": "held-call",
        "SHOW_LOG": str(shows),
    }
    for _ in range(3):
        result = fleet.run(**env)
        assert result.returncode == 0, result.stderr
        assert "tasks-axi" not in result.stderr
    assert set(shows.read_text().split()) == {"held-call"}
    urls = {d["id"]: d["board_url"] for d in fleet.published()["decisions_open"]}
    # The decision with a record is still placed under its origin's board.
    assert urls["held-call"] == "http://board/bound"


def test_a_failing_show_for_a_listed_decision_is_still_logged(fleet):
    hold = stub(fleet.tmp / "hold.sh", HOLD_BINDS_BOUND_AND_WRONG)
    tasks = stub(fleet.tmp / "tasks.sh", TASKS_AXI_ONLY_FROM_FM_HOME)
    bound_board_fleet(fleet)
    result = fleet.run(
        CAPTAIN_HOLD=hold,
        TASKS_AXI=tasks,
        FM_HOME=str(fleet.tmp),
        EXPECTED_PWD=str(fleet.tmp / "not-the-home"),
        LISTED_IDS="held-call",
    )
    assert result.returncode == 0, result.stderr
    assert "tasks-axi show held-call failed: NOT_FOUND" in result.stderr
    assert "other-call" not in result.stderr


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
    assert not [
        p for p in fleet.out.iterdir() if p.name not in ("home-summary.json", "journal")
    ]


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
        "journal",
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


def test_the_summary_carries_the_scheduled_list(fleet):
    """A declared row with no live gateway rows cached yet is unknown; once the
    slow section has cached them, a declared row they lack is missing."""
    manifest = fleet.tmp / "scheduled.json"
    manifest.write_text(
        json.dumps(
            {
                "timers": [],
                "cron": [
                    {
                        "name": "ledger-pulse",
                        "script": "scripts/ensure-finance-pulse.sh",
                    },
                    {
                        "name": "planning-reminder",
                        "script": "scripts/ensure-planning-reminder.sh",
                    },
                ],
            }
        )
    )
    cron = fleet.tmp / "cron.json"
    cron.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "ledger-pulse",
                        "agentId": "finance",
                        "enabled": True,
                        "schedule": {"kind": "cron", "expr": "20 9 * * *"},
                        "delivery": {"to": "4223"},
                        "state": {"lastRunStatus": "ok"},
                    }
                ]
            }
        )
    )
    fleet.write(summary())
    env = {"FLEET_SCHEDULED_MANIFEST": str(manifest), "FLEET_SCHEDULED_EVERY": "300"}
    result = fleet.run(**env)
    assert result.returncode == 0, result.stderr
    # The first run reads the gateway after the summary is out: nothing cached yet.
    assert {e["name"]: e["state"] for e in fleet.published()["scheduled"]} == {
        "ledger-pulse": "unknown",
        "planning-reminder": "unknown",
    }
    result = fleet.run(
        **{**env, "FLEET_CRON_CMD": f"cat {cron}", "FLEET_SCHEDULED_EVERY": "0"}
    )
    assert result.returncode == 0, result.stderr
    result = fleet.run(**env)
    assert result.returncode == 0, result.stderr
    scheduled = fleet.published()["scheduled"]
    assert [(e["name"], e["owner"], e["state"]) for e in scheduled] == [
        ("ledger-pulse", "finance", "live"),
        ("planning-reminder", None, "missing"),
    ]
    assert "4223" not in (fleet.out / "home-summary.json").read_text()


def test_a_failed_gateway_read_keeps_its_old_stamp_and_goes_unknown(fleet):
    """Three cadences after the last good cron read, the cached rows are not live."""
    manifest = fleet.tmp / "scheduled.json"
    manifest.write_text(
        json.dumps(
            {
                "timers": [],
                "cron": [
                    {
                        "name": "ledger-pulse",
                        "script": "scripts/ensure-finance-pulse.sh",
                    },
                    {
                        "name": "planning-reminder",
                        "script": "scripts/ensure-planning-reminder.sh",
                    },
                ],
            }
        )
    )
    cron = fleet.tmp / "cron.json"
    cron.write_text(
        json.dumps(
            {"jobs": [{"name": "ledger-pulse", "agentId": "finance", "enabled": True}]}
        )
    )
    fleet.write(summary())
    env = {"FLEET_SCHEDULED_MANIFEST": str(manifest), "FLEET_SCHEDULED_EVERY": "300"}
    assert fleet.run(**env, FLEET_CRON_CMD=f"cat {cron}").returncode == 0
    # The gateway is gone from here on: every due read fails.
    down = {**env, "FLEET_CRON_CMD": "false"}
    assert fleet.run(**down, FLEET_NOW=str(NOW + 600)).returncode == 0
    states = {e["name"]: e["state"] for e in fleet.published()["scheduled"]}
    assert states == {"ledger-pulse": "live", "planning-reminder": "missing"}
    assert fleet.run(**down, FLEET_NOW=str(NOW + 1000)).returncode == 0
    scheduled = fleet.published()["scheduled"]
    assert [(e["name"], e["state"], e["stale_seconds"]) for e in scheduled] == [
        ("ledger-pulse", "unknown", 1000),
        ("planning-reminder", "unknown", 1000),
    ]


def test_the_summary_scheduled_list_is_empty_without_a_manifest(fleet):
    fleet.write(summary())
    result = fleet.run()
    assert result.returncode == 0, result.stderr
    assert fleet.published()["scheduled"] == []


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
    stalled = root / "usr/local/bin/lifekit-fleet-stalled.json"
    assert stalled.read_text() == (DIR / "fleet-stalled.json").read_text()
    taken = root / "usr/local/bin/lifekit-fleet-taken.py"
    assert os.access(taken, os.X_OK)
    assert taken.read_text() == (DIR / "taken_feed.py").read_text()
    screens = root / "usr/local/bin/lifekit-fleet-screens.py"
    assert os.access(screens, os.X_OK)
    assert screens.read_text() == (DIR / "screen_changes.py").read_text()
    scheduled = root / "usr/local/bin/lifekit-fleet-scheduled.py"
    assert os.access(scheduled, os.X_OK)
    assert scheduled.read_text() == (DIR / "scheduled.py").read_text()
    manifest = json.loads(
        (root / "usr/local/bin/lifekit-fleet-scheduled.json").read_text()
    )
    assert "lifekit-fleet-publisher.timer" in {t["unit"] for t in manifest["timers"]}
    assert "today-brief" in {c["name"] for c in manifest["cron"]}
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


def installed_environment(tmp_path):
    unit = tmp_path / "root/etc/systemd/system/lifekit-fleet-publisher.service"
    service = configparser.ConfigParser(interpolation=None)
    service.optionxform = str
    service.read_string(unit.read_text())
    return service["Service"]["Environment"]


def test_installer_carries_the_plan_map_into_the_unit(tmp_path):
    result = run_installer(
        tmp_path, FM_HOMES="/srv/fleet-home", FLEET_PLAN_MAP="alias=project other=thing"
    )
    assert result.returncode == 0, result.stderr
    assert installed_environment(tmp_path) == (
        '"FM_HOMES=/srv/fleet-home" "TASKS_AXI=/opt/tools/bin/tasks-axi"'
        ' "FLEET_PLAN_MAP=alias=project other=thing"'
    )


@pytest.mark.parametrize("plan_map", [None, ""])
def test_installer_leaves_the_plan_map_out_of_the_unit_when_unset(tmp_path, plan_map):
    env = {} if plan_map is None else {"FLEET_PLAN_MAP": plan_map}
    result = run_installer(tmp_path, FM_HOMES="/srv/fleet-home", **env)
    assert result.returncode == 0, result.stderr
    assert installed_environment(tmp_path) == (
        '"FM_HOMES=/srv/fleet-home" "TASKS_AXI=/opt/tools/bin/tasks-axi"'
    )


@pytest.mark.parametrize("plan_map", ['a="b', "a=b|c", "a=b&c"])
def test_installer_refuses_a_plan_map_it_cannot_render(tmp_path, plan_map):
    result = run_installer(
        tmp_path, FM_HOMES="/srv/fleet-home", FLEET_PLAN_MAP=plan_map
    )
    assert result.returncode == 1
    assert not (tmp_path / "root/etc/systemd").exists()


def test_installer_carries_the_clip_stats_feed_into_the_unit(tmp_path):
    feed = "/home/denys/clip-data/queue/stats.jsonl"
    result = run_installer(
        tmp_path, FM_HOMES="/srv/fleet-home", FLEET_CLIP_STATS_FEED=feed
    )
    assert result.returncode == 0, result.stderr
    assert installed_environment(tmp_path) == (
        '"FM_HOMES=/srv/fleet-home" "TASKS_AXI=/opt/tools/bin/tasks-axi"'
        f' "FLEET_CLIP_STATS_FEED={feed}"'
    )


@pytest.mark.parametrize("feed", [None, ""])
def test_installer_leaves_the_clip_stats_feed_out_of_the_unit_when_unset(
    tmp_path, feed
):
    env = {} if feed is None else {"FLEET_CLIP_STATS_FEED": feed}
    result = run_installer(tmp_path, FM_HOMES="/srv/fleet-home", **env)
    assert result.returncode == 0, result.stderr
    assert "FLEET_CLIP_STATS_FEED" not in installed_environment(tmp_path)


@pytest.mark.parametrize(
    "feed",
    ["relative/stats.jsonl", "/srv/a b/stats.jsonl", '/srv/a"b', "/srv/a|b", "/srv/$x"],
)
def test_installer_refuses_a_clip_stats_feed_it_cannot_render(tmp_path, feed):
    result = run_installer(
        tmp_path, FM_HOMES="/srv/fleet-home", FLEET_CLIP_STATS_FEED=feed
    )
    assert result.returncode == 1
    assert not (tmp_path / "root/etc/systemd").exists()


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
    "closed,links,blocked,created"
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
        "created": "2027-01-14",
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
        "held_external": 1,
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
        "held_external": 0,
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


def test_spellings_of_one_repo_are_one_row_with_summed_counts(homes):
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [
            task("s1", repo="sentry"),
            task("s2", repo="o/sentry", state="in_flight"),
            task("s3", repo="sentry", held="yes", hold_kind="external"),
            task("s4", repo="p/sentry"),
        ],
    )
    clone(vps, "sentry", "git@github.com:o/sentry.git")
    gh = fake_gh(homes.tmp, {"o/sentry": 4, "p/sentry": 1})
    assert homes.run(vps, FLEET_GH=gh).returncode == 0
    rows = {p["name"]: p for p in status(homes)["projects"]}
    assert sorted(rows) == ["o/sentry", "p/sentry"]
    row = rows["o/sentry"]
    assert (row["repo"], row["github"], row["open_prs"]) == ("o/sentry", "o/sentry", 4)
    assert (row["in_flight"], row["queued"], row["held"]) == (1, 1, 1)
    assert row["held_external"] == 1
    assert rows["p/sentry"]["queued"] == 1


def test_an_externally_blocked_item_is_a_counted_state_not_a_message(homes):
    """A dev problem an agent filed with `blocked-on: external` is held by
    firstmate as an external wait; the page counts it apart from the captain's
    and firstmate's own holds (docs/runbook.md, "Dev problems go to firstmate")."""
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [
            task("e1", repo="sentry", held="yes", hold_kind="external"),
            task("e2", repo="sentry", held="yes", hold_kind="external"),
            task("c1", repo="sentry", held="yes", hold_kind="captain"),
            task("p1", repo="sentry", held="yes", hold_kind="parked"),
            task("q1", repo="sentry"),
        ],
    )
    assert homes.run(vps).returncode == 0
    page = status(homes)
    by_id = {r["id"]: r for r in page["records"]}
    assert by_id["e1"]["state"] == "held"
    assert by_id["e1"]["hold"]["owner"] == "external"
    assert by_id["c1"]["hold"]["owner"] == "captain"
    assert by_id["p1"]["hold"]["owner"] == "firstmate"
    (row,) = [p for p in page["projects"] if p["name"] == "sentry"]
    assert (row["held"], row["held_external"], row["queued"]) == (4, 2, 1)
    assert (page["queue"]["held"], page["queue"]["held_external"]) == (4, 2)


def test_a_short_name_without_a_clone_joins_the_only_owner_spelling(homes):
    vps = status_home(
        homes.tmp, "fm-vps", [task("s1", repo="sentry"), task("s2", repo="o/sentry")]
    )
    assert homes.run(vps).returncode == 0
    (row,) = status(homes)["projects"]
    assert (row["name"], row["github"], row["queued"]) == ("o/sentry", "o/sentry", 2)


def test_plan_map_aliases_fold_vault_spellings_into_one_row(homes):
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [
            task("v1", repo="vault"),
            task("v2", repo="memory", state="in_flight"),
            task("v3", repo="o/memory"),
            task("x1", repo="o/other"),
        ],
    )
    plans = vault(homes.tmp, "memory")
    gh = fake_gh(homes.tmp, {"o/memory": 2, "o/other": 0})
    result = homes.run(
        vps, FLEET_GH=gh, FLEET_VAULT=str(plans), FLEET_PLAN_MAP="vault=memory"
    )
    assert result.returncode == 0, result.stderr
    rows = {p["name"]: p for p in status(homes)["projects"]}
    assert sorted(rows) == ["o/memory", "o/other"]
    row = rows["o/memory"]
    assert (row["in_flight"], row["queued"], row["open_prs"]) == (1, 2, 2)
    assert row["plan_url"].endswith("/projects/memory/plan.md")


def test_the_vault_spellings_are_one_row_linking_the_memory_plan(homes):
    records = [task(f"v{i}", repo=r) for i, r in enumerate(("vault", "memory"))]
    vps = status_home(homes.tmp, "fm-vps", records)
    plans = vault(homes.tmp, "memory")
    assert homes.run(vps, FLEET_VAULT=str(plans)).returncode == 0
    (row,) = status(homes)["projects"]
    assert (row["name"], row["queued"]) == ("memory", 2)
    assert row["plan_url"] == (
        "https://github.com/o/vault/blob/HEAD/projects/memory/plan.md"
    )


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
    # The PC is on but its summary carries no capacity: a reason, not nulls.
    assert caps["pc"]["running"] is None
    assert caps["pc"]["idle_reason"] == "capacity-unpublished"
    result = homes.run(vps, main, FLEET_NOW=str(NOW + 5000))
    caps = {c["home_id"]: c for c in status(homes)["capacity"]}
    assert caps["pc"]["idle_reason"] == "host-offline"


def lane_capacity(**over):
    return {
        "schema": "fm-lane-capacity.v1",
        "generated_epoch": NOW - 20,
        "machine": "pc-box",
        "lanes": {"count": 1, "ids": ["a"]},
        "cap": {"target": 3, "status": "ok"},
        "quota": {"runway": "ok"},
        **over,
    }


def pc_status_row(homes, **pc):
    vps = status_home(homes.tmp, "fm-vps", [task("q1")])
    pc_cache_in(vps).write_text(
        json.dumps(
            summary(generated_epoch=NOW - 60, counts={"active_children": 1}, **pc)
        )
    )
    result = homes.run(vps)
    assert result.returncode == 0, result.stderr
    page = status(homes)
    (row,) = [c for c in page["capacity"] if c["home_id"] == "pc"]
    return row, page


def test_pc_row_is_filled_from_the_capacity_its_summary_publishes(homes):
    row, _ = pc_status_row(
        homes,
        lane_capacity=lane_capacity(),
        queued=[{"id": "q"}, {"id": "h", "hold_kind": "captain"}],
    )
    assert row == {
        "home_id": "pc",
        "machine": "pc-box",
        "running": 1,
        "allowed": 3,
        "cap_status": "ok",
        "quota": "ok",
        "generated_epoch": NOW - 20,
        "idle_reason": None,
    }
    # The capacity feeds the row only; homes[] keeps state and last-seen.
    (entry,) = homes_pc_entry(homes)
    assert set(entry) == {
        "id",
        "path",
        "published",
        "remote",
        "valid",
        "reason",
        "last_seen_epoch",
        "state",
    }


@pytest.mark.parametrize(
    ("lanes", "cap", "queued", "reason"),
    [
        (3, {"target": 3, "status": "ok"}, [{"id": "q"}], "cap-reached"),
        (
            0,
            {"target": 3, "status": "ok"},
            [{"id": "h", "hold_bucket": "x"}],
            "all-held",
        ),
        (0, {"target": None, "status": "none"}, [], "queue-empty"),
        # No queued list: nothing is known about the queue, so no queue reason.
        (0, {"target": 3, "status": "ok"}, None, None),
    ],
)
def test_pc_row_idle_reason_uses_its_cap_and_its_queue(
    homes, lanes, cap, queued, reason
):
    extra = {} if queued is None else {"queued": queued}
    row, _ = pc_status_row(
        homes,
        lane_capacity=lane_capacity(lanes={"count": lanes}, cap=cap, quota=None),
        **extra,
    )
    assert row["idle_reason"] == reason


def test_pc_row_with_an_unreadable_capacity_is_unpublished_and_an_error(homes):
    row, page = pc_status_row(homes, lane_capacity={"schema": "other"})
    assert (row["running"], row["idle_reason"]) == (None, "capacity-unpublished")
    assert any("pc: published capacity unreadable" in e for e in page["errors"])


def test_an_off_pc_is_host_offline_even_with_a_published_capacity(homes):
    vps = status_home(homes.tmp, "fm-vps", [task("q1")])
    pc_cache_in(vps).write_text(
        json.dumps(summary(generated_epoch=NOW - 99_999, lane_capacity=lane_capacity()))
    )
    assert homes.run(vps).returncode == 0
    (row,) = [c for c in status(homes)["capacity"] if c["home_id"] == "pc"]
    assert (row["running"], row["idle_reason"]) == (None, "host-offline")
    assert row["generated_epoch"] == NOW - 99_999


def test_a_home_with_no_cap_declared_is_never_cap_reached(homes):
    vps = status_home(
        homes.tmp,
        "fm-vps",
        [task("q1")],
        capacity={"lanes": {"count": 9}, "cap": {"target": None, "status": "none"}},
    )
    assert homes.run(vps).returncode == 0
    (row,) = status(homes)["capacity"]
    assert (row["running"], row["allowed"], row["cap_status"]) == (9, None, "none")
    assert row["idle_reason"] is None


@pytest.mark.parametrize(
    ("running", "allowed", "quota", "queued", "held", "reason"),
    [
        (2, 2, None, 5, 0, "cap-reached"),
        (0, 2, "exhausted_now", 5, 0, "quota"),
        (0, 2, None, 0, 3, "all-held"),
        (0, None, None, 0, 0, "queue-empty"),
        (1, 2, None, 4, 0, None),
        (1, None, "projected_exhaustion", 4, 0, None),
        (5, None, None, 0, 0, "queue-empty"),
        (2, 2, None, None, 0, "cap-reached"),
        (0, 2, None, None, 0, None),
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
    assert (q["count"], q["held"], q["held_external"]) == (14, 1, 0)
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


def test_how_it_works_links_each_owner_page_the_vault_has(homes):
    vps = status_home(homes.tmp, "fm-vps", [])
    plans = vault(homes.tmp)
    (plans / "system" / "workflows").mkdir(parents=True)
    for wid in ("intake", "monday-loop"):
        (plans / "system" / "workflows" / f"{wid}.md").write_text("# owner\n")
    assert homes.run(vps, FLEET_VAULT=str(plans)).returncode == 0
    page = status(homes)
    blob = "https://github.com/o/vault/blob/HEAD/system/workflows"
    assert [(w["id"], w["url"]) for w in page["how_it_works"]] == [
        ("intake", f"{blob}/intake.md"),
        ("queue-to-merge", None),
        ("monday-loop", f"{blob}/monday-loop.md"),
    ]
    assert page["follow_ups"] == ["owner page for a task from queue to merge"]


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
    shutil.copy(DIR / "fleet-stalled.json", bindir / "lifekit-fleet-stalled.json")
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


# ── The "Taken for you" feed ────────────────────────────────────────────────


def p16_row(
    ts, subject="lifekit-dashboard#212", ruling="P8", outcome="merged", **extra
):
    return {
        "ts": ts,
        "actor": "firstmate",
        "home": "fm-vps",
        "class": "github.merge-green",
        "cell": "firstmate",
        "ruling": ruling,
        "subject": subject,
        "outcome": outcome,
        **extra,
    }


def self_call(ts, question="Merge the green PR 995?", **extra):
    return {
        "ts": ts,
        "id": "dc-979-merge",
        "home": "firstmate",
        "question": question,
        "answer": "merge, with a long free-text reason that must not be published",
        "record": "agent-behavior.md#P8 (merge green PRs) and #P2",
        **extra,
    }


def policy_dir(tmp, p16=None, self_calls=None):
    directory = tmp / "policy"
    directory.mkdir()
    for name, lines in (("p16.jsonl", p16), ("self-calls.jsonl", self_calls)):
        if lines is not None:
            (directory / name).write_text(
                "".join(
                    x if isinstance(x, str) else json.dumps(x) + "\n" for x in lines
                )
            )
    return directory


def taken(fleet):
    return json.loads((fleet.out / "taken-for-you.json").read_text())


def test_taken_feed_merges_both_logs_newest_first(fleet):
    fleet.write(summary())
    directory = policy_dir(
        fleet.tmp,
        p16=[
            p16_row("2026-11-08T10:00:00+00:00"),
            p16_row("2026-11-08T12:00:00Z", subject="skills#4", outcome="labelled"),
        ],
        self_calls=[self_call("2026-11-08T11:00:00+00:00")],
    )
    result = fleet.run(FLEET_POLICY_DIR=str(directory))
    assert result.returncode == 0, result.stderr
    assert (fleet.out / "taken-for-you.json").stat().st_mode & 0o777 == 0o644
    feed = taken(fleet)
    assert feed["schema"] == "fleet-taken-feed.v1"
    assert feed["generated_epoch"] == NOW
    assert feed["sources"] == {
        "p16": {"read": True, "lines": 2, "skipped": 0},
        "self_calls": {"read": True, "lines": 1, "skipped": 0},
    }
    assert [(i["source"], i["line"]) for i in feed["items"]] == [
        ("p16", "labelled skills#4 (P8)"),
        ("self_calls", "self-answered Merge the green PR 995? (P8)"),
        ("p16", "merged lifekit-dashboard#212 (P8)"),
    ]
    first = feed["items"][0]
    assert first == {
        "id": first["id"],
        "source": "p16",
        "ts": 1_794_139_200,
        "ruling": "P8",
        "subject": "skills#4",
        "outcome": "labelled",
        "line": "labelled skills#4 (P8)",
        "class": "github.merge-green",
        "actor": "firstmate",
        "home_id": "fm-vps",
    }
    assert (
        first["id"].startswith("p16-") and len(set(i["id"] for i in feed["items"])) == 3
    )
    # The self-answered call's answer and record text never leave the host.
    assert "free-text" not in (fleet.out / "taken-for-you.json").read_text()
    # Ids are stable across reruns.
    assert fleet.run(FLEET_POLICY_DIR=str(directory)).returncode == 0
    assert [i["id"] for i in taken(fleet)["items"]] == [i["id"] for i in feed["items"]]
    assert not [p for p in fleet.out.iterdir() if p.name.startswith(".taken.")]


def test_taken_feed_skips_and_counts_lines_it_cannot_use(fleet):
    fleet.write(summary())
    good = p16_row(1_794_139_000)
    no_ruling = {k: v for k, v in p16_row(1_794_139_001).items() if k != "ruling"}
    directory = policy_dir(
        fleet.tmp,
        p16=[
            json.dumps(good) + "\n",
            "{not json\n",
            "\n",
            "[1, 2]\n",
            json.dumps(no_ruling) + "\n",
            json.dumps(p16_row("yesterday")) + "\n",
            json.dumps(p16_row(1_794_139_002, subject="   ")) + "\n",
            json.dumps(p16_row(1_794_139_003, outcome=7)) + "\n",
            json.dumps(p16_row("PLACEHOLDER")).replace('"PLACEHOLDER"', "1e999") + "\n",
            json.dumps(p16_row("PLACEHOLDER")).replace('"PLACEHOLDER"', "9" * 400)
            + "\n",
            "[" * 60_000 + "\n",
        ],
        self_calls=["not json at all\n", json.dumps({"ts": 1_794_139_004}) + "\n"],
    )
    result = fleet.run(FLEET_POLICY_DIR=str(directory))
    assert result.returncode == 0, result.stderr
    feed = taken(fleet)
    assert [i["subject"] for i in feed["items"]] == ["lifekit-dashboard#212"]
    assert feed["sources"]["p16"] == {"read": True, "lines": 10, "skipped": 9}
    assert feed["sources"]["self_calls"] == {"read": True, "lines": 2, "skipped": 2}


def test_taken_feed_is_empty_for_empty_logs(fleet):
    fleet.write(summary())
    directory = policy_dir(fleet.tmp, p16=[], self_calls=[])
    assert fleet.run(FLEET_POLICY_DIR=str(directory)).returncode == 0
    feed = taken(fleet)
    assert feed["items"] == []
    assert feed["sources"]["p16"] == {"read": True, "lines": 0, "skipped": 0}


def test_taken_feed_reports_a_missing_log_and_publishes_the_other(fleet):
    fleet.write(summary())
    directory = policy_dir(fleet.tmp, self_calls=[self_call(1_794_139_000)])
    assert fleet.run(FLEET_POLICY_DIR=str(directory)).returncode == 0
    feed = taken(fleet)
    assert feed["sources"]["p16"] == {"read": False, "lines": 0, "skipped": 0}
    assert [i["source"] for i in feed["items"]] == ["self_calls"]


def test_taken_feed_with_no_log_leaves_the_previous_feed_alone(fleet):
    fleet.write(summary())
    (fleet.out / "taken-for-you.json").write_text('{"previous": true}')
    result = fleet.run(FLEET_POLICY_DIR=str(fleet.tmp / "absent"))
    assert result.returncode == 0, result.stderr
    assert json.loads((fleet.out / "taken-for-you.json").read_text()) == {
        "previous": True
    }
    assert not [p for p in fleet.out.iterdir() if p.name.startswith(".taken.")]


def test_taken_feed_cuts_a_very_long_subject_to_one_line(fleet):
    fleet.write(summary())
    long_subject = "line one\nline two\t" + "x" * 5000
    directory = policy_dir(
        fleet.tmp, p16=[p16_row(1_794_139_000, subject=long_subject)]
    )
    assert fleet.run(FLEET_POLICY_DIR=str(directory)).returncode == 0
    (item,) = taken(fleet)["items"]
    assert item["subject"].startswith("line one line two xxx")
    assert item["subject"].endswith("…") and len(item["subject"]) == 160
    assert "\n" not in item["line"]
    # A line past the size bound is skipped, not truncated.
    huge = json.dumps(p16_row(1_794_139_001, subject="y" * 70_000)) + "\n"
    directory = tmp_dir = fleet.tmp / "policy2"
    directory.mkdir()
    (tmp_dir / "p16.jsonl").write_text(huge)
    assert fleet.run(FLEET_POLICY_DIR=str(directory)).returncode == 0
    feed = taken(fleet)
    assert feed["items"] == [] and feed["sources"]["p16"]["skipped"] == 1


def test_taken_feed_is_bounded_to_the_newest_200(fleet):
    fleet.write(summary())
    rows = [p16_row(1_794_000_000 + n, subject=f"pr#{n}") for n in range(250)]
    directory = policy_dir(fleet.tmp, p16=rows)
    assert fleet.run(FLEET_POLICY_DIR=str(directory)).returncode == 0
    items = taken(fleet)["items"]
    assert len(items) == 200
    assert items[0]["subject"] == "pr#249" and items[-1]["subject"] == "pr#50"


def test_taken_feed_keeps_duplicate_lines_apart_and_withholds_credentials(fleet):
    fleet.write(summary())
    row = p16_row(1_794_139_000)
    leaked = p16_row(1_794_139_001, subject="tskey-auth-abcdef0123456789")
    directory = policy_dir(fleet.tmp, p16=[row, row, leaked])
    result = fleet.run(FLEET_POLICY_DIR=str(directory))
    assert result.returncode == 0, result.stderr
    feed = taken(fleet)
    assert len({i["id"] for i in feed["items"]}) == 2
    assert feed["sources"]["p16"]["skipped"] == 1
    assert "tskey-" not in (fleet.out / "taken-for-you.json").read_text()


def test_taken_feed_reads_only_the_tail_of_a_large_log(fleet):
    fleet.write(summary())
    directory = fleet.tmp / "policy"
    directory.mkdir()
    old = json.dumps(p16_row(1_794_000_000, subject="old " + "z" * 900)) + "\n"
    new = json.dumps(p16_row(1_794_139_000, subject="new")) + "\n"
    (directory / "p16.jsonl").write_text(old * 6000 + new)  # about 5.7 MiB
    assert fleet.run(FLEET_POLICY_DIR=str(directory)).returncode == 0
    feed = taken(fleet)
    assert feed["items"][0]["subject"] == "new"
    # The cut first line is dropped, not counted as a bad line.
    assert feed["sources"]["p16"]["skipped"] == 0


def test_a_failing_taken_helper_fails_the_run_but_not_the_summary(fleet):
    fleet.write(summary())
    broken = fleet.tmp / "broken.py"
    broken.write_text("raise SystemExit(1)\n")
    result = fleet.run(FLEET_TAKEN=str(broken))
    assert result.returncode == 1
    assert "the taken feed was not published" in result.stderr
    assert fleet.published()["published_epoch"] == NOW
    assert not (fleet.out / "taken-for-you.json").exists()
    assert not [p for p in fleet.out.iterdir() if p.name.startswith(".taken.")]


def test_an_installed_publisher_finds_the_taken_helper_beside_itself(fleet, tmp_path):
    bindir = tmp_path / "usr-local-bin"
    bindir.mkdir()
    shutil.copy(SCRIPT, bindir / "lifekit-fleet-publisher.sh")
    shutil.copy(DIR / "taken_feed.py", bindir / "lifekit-fleet-taken.py")
    fleet.write(summary())
    directory = policy_dir(fleet.tmp, p16=[p16_row(1_794_139_000)])
    result = subprocess.run(
        [
            "bash",
            str(bindir / "lifekit-fleet-publisher.sh"),
            str(fleet.out),
            str(fleet.prom),
        ],
        env={
            **os.environ,
            "FM_HOME": str(fleet.tmp),
            "SUMMARY_FILE": str(fleet.tmp / "home-summary.json"),
            "LEDGER_FILE": str(fleet.tmp / "ledger.jsonl"),
            "DATA_DIR": str(fleet.tmp / "data"),
            "LAVISH_STATE": str(fleet.tmp / "lavish.json"),
            "FLEET_NOW": str(NOW),
            "FLEET_STATE_DIR": str(fleet.tmp / "state"),
            **NO_LIVE_SOURCES,
            "FLEET_POLICY_DIR": str(directory),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert [i["subject"] for i in taken(fleet)["items"]] == ["lifekit-dashboard#212"]


# --- the screen changes feed ---------------------------------------------------

SCREEN_REPO = "lifekit-hq/lifekit-dashboard"
PNG = b"\x89PNG\r\n\x1a\n".decode("latin-1")
HEAD, BASE, MERGE_BASE = "a" * 40, "b" * 40, "c" * 40
COMMIT_TIME, ACTIVITY_TIME = 1_800_003_600, 1_800_007_200
BASELINE = "frontend/e2e/pixel.spec.ts-snapshots/fleet-phone-light-chromium-linux.png"
ADDED = "frontend/e2e/pixel.spec.ts-snapshots/pace-desktop-dark-chromium-linux.png"

FAKE_GH = r"""#!/usr/bin/env python3
import json, sys
root = __ROOT__
args = sys.argv[1:]
open(root + "/gh.log", "a").write(" ".join(args) + "\n")
world = json.load(open(root + "/gh-world.json"))
if world.get("fail"):
    sys.exit(1)
if args[:2] == ["pr", "list"]:
    kind = "merged" if args[args.index("--state") + 1] == "merged" else "pulls"
    print(json.dumps(world.get(kind, {}).get(args[args.index("-R") + 1], [])))
elif args[0] == "api" and "/compare/" in args[-1]:
    repo = args[-1].split("repos/")[1].split("/compare/")[0]
    head = args[-1].split("...")[1]
    print(json.dumps(world["compares"][repo][head]))
elif args[0] == "api":
    path, ref = args[-1].split("?ref=")
    key = path.split("/contents/", 1)[1] + "@" + ref
    sys.stdout.buffer.write(world["files"][key].encode("latin-1"))
else:
    sys.exit(2)
"""


def screens_world(
    fleet, pulls=None, compares=None, files=None, fail=False, merged=None
):
    (fleet.tmp / "gh-world.json").write_text(
        json.dumps(
            {
                "pulls": pulls or {},
                "merged": merged or {},
                "compares": compares or {},
                "files": files or {},
                "fail": fail,
            }
        )
    )
    gh = fleet.tmp / "gh"
    gh.write_text(FAKE_GH.replace("__ROOT__", repr(str(fleet.tmp))))
    gh.chmod(0o755)
    return str(gh)


def screen_pull(number, head=HEAD, title="Root every page on cmn-page"):
    return {
        "number": number,
        "title": title,
        "headRefOid": head,
        "baseRefOid": BASE,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ACTIVITY_TIME)),
    }


def screen_compare(*files, when=COMMIT_TIME):
    return {
        "merge_base_commit": {"sha": MERGE_BASE},
        "commits": [
            {
                "commit": {
                    "committer": {
                        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
                    }
                }
            }
        ],
        "files": [{"filename": path, "status": status} for path, status in files],
    }


def screen_feed(fleet):
    return json.loads((fleet.out / "screen-changes.json").read_text())


def run_screens(fleet, gh, **env):
    fleet.write(summary())
    # Every call is a fresh run of the feed: its cadence is covered on its own.
    env = {"FLEET_SCREEN_REPOS": SCREEN_REPO, "FLEET_SCREENS_EVERY": "0", **env}
    return fleet.run(FLEET_GH=gh, FLEET_SCREEN_HOME="fm-vps", **env)


def one_screen_world(fleet, **kwargs):
    return screens_world(
        fleet,
        pulls={SCREEN_REPO: [screen_pull(212)]},
        compares={
            SCREEN_REPO: {
                HEAD: screen_compare(
                    (BASELINE, "modified"),
                    (ADDED, "added"),
                    ("frontend/src/app/app.ts", "modified"),
                )
            }
        },
        files={
            f"{BASELINE}@{MERGE_BASE}": PNG + "before",
            f"{BASELINE}@{HEAD}": PNG + "after",
            f"{ADDED}@{HEAD}": PNG + "added",
        },
        **kwargs,
    )


def test_screen_changes_feed_has_the_contract_shape_and_copies_the_images(fleet):
    result = run_screens(fleet, one_screen_world(fleet))
    assert result.returncode == 0, result.stderr
    assert (fleet.out / "screen-changes.json").stat().st_mode & 0o777 == 0o644
    assert screen_feed(fleet) == {
        "schema": "fleet-screen-changes.v1",
        "generated_epoch": NOW,
        "items": [
            {
                "id": "lifekit-dashboard-212",
                "home_id": "fm-vps",
                "repo": "lifekit-dashboard",
                "pr": 212,
                "title": "Root every page on cmn-page",
                "url": f"https://github.com/{SCREEN_REPO}/pull/212",
                "state": "open",
                # The head commit's time, not the PR's last activity.
                "ts": COMMIT_TIME,
                "screens": [
                    {
                        "name": "fleet-phone-light",
                        "status": "changed",
                        "before": "before-fleet-phone-light.png",
                        "after": "after-fleet-phone-light.png",
                    },
                    {
                        "name": "pace-desktop-dark",
                        "status": "added",
                        "before": None,
                        "after": "after-pace-desktop-dark.png",
                    },
                ],
            }
        ],
    }
    images = fleet.out / "screen-changes/lifekit-dashboard-212"
    assert sorted(p.name for p in images.iterdir()) == [
        "after-fleet-phone-light.png",
        "after-pace-desktop-dark.png",
        "before-fleet-phone-light.png",
    ]
    assert (images / "before-fleet-phone-light.png").read_bytes() == (
        PNG + "before"
    ).encode("latin-1")
    assert (images / "after-fleet-phone-light.png").stat().st_mode & 0o777 == 0o644


def test_screen_changes_reads_only_allowlisted_repos_and_baseline_paths(fleet):
    gh = one_screen_world(fleet)
    other = "someone/else"
    world = json.loads((fleet.tmp / "gh-world.json").read_text())
    world["pulls"][other] = [screen_pull(7)]
    (fleet.tmp / "gh-world.json").write_text(json.dumps(world))
    assert run_screens(fleet, gh).returncode == 0
    log = (fleet.tmp / "gh.log").read_text()
    assert other not in log
    # The one non-baseline file in the diff is never fetched.
    assert "app.ts" not in log
    assert len(screen_feed(fleet)["items"]) == 1
    # An explicit allowlist replaces the default.
    (fleet.tmp / "gh.log").write_text("")
    assert run_screens(fleet, gh, FLEET_SCREEN_REPOS=other).returncode == 0
    assert SCREEN_REPO not in (fleet.tmp / "gh.log").read_text()


def test_screen_changes_leaves_out_a_pull_request_with_no_baseline_change(fleet):
    gh = screens_world(
        fleet,
        pulls={SCREEN_REPO: [screen_pull(5)]},
        compares={SCREEN_REPO: {HEAD: screen_compare(("README.md", "modified"))}},
    )
    assert run_screens(fleet, gh).returncode == 0
    assert screen_feed(fleet)["items"] == []
    assert not (fleet.out / "screen-changes/lifekit-dashboard-5").exists()


def test_screen_changes_skips_a_screen_missing_the_image_its_status_needs(fleet):
    gh = screens_world(
        fleet,
        pulls={SCREEN_REPO: [screen_pull(9)]},
        compares={
            SCREEN_REPO: {HEAD: screen_compare((BASELINE, "removed"), (ADDED, "added"))}
        },
        # Not a PNG: the removed screen has no usable before; the added one is fine.
        files={f"{BASELINE}@{MERGE_BASE}": "<html>", f"{ADDED}@{HEAD}": PNG + "x"},
    )
    assert run_screens(fleet, gh).returncode == 0
    (item,) = screen_feed(fleet)["items"]
    assert [s["name"] for s in item["screens"]] == ["pace-desktop-dark"]


def test_screen_changes_is_capped_at_20_items_newest_first(fleet):
    pulls, compares = [], {}
    for n in range(1, 26):
        head = f"{n:040x}"
        pulls.append(screen_pull(n, head=head))
        compares[head] = screen_compare((ADDED, "added"), when=COMMIT_TIME + n)
    gh = screens_world(
        fleet,
        pulls={SCREEN_REPO: pulls},
        compares={SCREEN_REPO: compares},
        files={f"{ADDED}@{n:040x}": PNG + "x" for n in range(1, 26)},
    )
    assert run_screens(fleet, gh).returncode == 0
    items = screen_feed(fleet)["items"]
    assert [i["pr"] for i in items] == list(range(25, 5, -1))
    # Only the listed items keep images.
    dirs = {p.name for p in (fleet.out / "screen-changes").iterdir()}
    assert dirs == {f"lifekit-dashboard-{n}" for n in range(6, 26)}


def test_screen_changes_caps_a_pull_requests_screens_at_40(fleet):
    paths = [f"snaps/screen{n}-chromium-linux.png" for n in range(45)]
    gh = screens_world(
        fleet,
        pulls={SCREEN_REPO: [screen_pull(3)]},
        compares={SCREEN_REPO: {HEAD: screen_compare(*[(p, "added") for p in paths])}},
        files={f"{p}@{HEAD}": PNG + "x" for p in paths},
    )
    assert run_screens(fleet, gh).returncode == 0
    (item,) = screen_feed(fleet)["items"]
    assert len(item["screens"]) == 40


def test_screen_changes_reuses_what_it_fetched_and_drops_a_closed_pull_request(fleet):
    gh = one_screen_world(fleet)
    assert run_screens(fleet, gh).returncode == 0
    fetched = (fleet.tmp / "gh.log").read_text().count("compare")
    assert fetched == 1
    assert run_screens(fleet, gh).returncode == 0
    assert (fleet.tmp / "gh.log").read_text().count("compare") == 1
    # A new head commit is read again.
    world = json.loads((fleet.tmp / "gh-world.json").read_text())
    new = "d" * 40
    world["pulls"][SCREEN_REPO] = [screen_pull(212, head=new)]
    world["compares"][SCREEN_REPO][new] = world["compares"][SCREEN_REPO][HEAD]
    world["files"][f"{BASELINE}@{new}"] = PNG + "newer"
    world["files"][f"{ADDED}@{new}"] = PNG + "added"
    (fleet.tmp / "gh-world.json").write_text(json.dumps(world))
    assert run_screens(fleet, gh).returncode == 0
    images = fleet.out / "screen-changes/lifekit-dashboard-212"
    assert (images / "after-fleet-phone-light.png").read_bytes().endswith(b"newer")
    # Closed unmerged: no longer open and not merged, so item and images go.
    world["pulls"][SCREEN_REPO] = []
    (fleet.tmp / "gh-world.json").write_text(json.dumps(world))
    assert run_screens(fleet, gh).returncode == 0
    assert screen_feed(fleet)["items"] == []
    assert not (fleet.out / "screen-changes/lifekit-dashboard-212").exists()


def merged_pull(number, merged_at, head=HEAD):
    return {
        **screen_pull(number, head=head),
        "mergedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(merged_at)),
    }


WINDOW = 7 * 86400


def test_screen_changes_keeps_a_merged_pull_request_for_seven_days(fleet):
    gh = one_screen_world(fleet)
    assert run_screens(fleet, gh).returncode == 0
    images = fleet.out / "screen-changes/lifekit-dashboard-212"
    assert screen_feed(fleet)["items"][0]["state"] == "open"
    # Merged inside the window: still listed, as merged, with its images.
    world = json.loads((fleet.tmp / "gh-world.json").read_text())
    world["pulls"][SCREEN_REPO] = []
    world["merged"][SCREEN_REPO] = [merged_pull(212, NOW - WINDOW + 60)]
    (fleet.tmp / "gh-world.json").write_text(json.dumps(world))
    assert run_screens(fleet, gh).returncode == 0
    (item,) = screen_feed(fleet)["items"]
    assert item["state"] == "merged"
    assert item["merged_epoch"] == NOW - WINDOW + 60
    assert item["ts"] == COMMIT_TIME
    assert (images / "before-fleet-phone-light.png").is_file()
    assert (fleet.tmp / "gh.log").read_text().count("compare") == 1
    # Merged outside the window: item and images go.
    world["merged"][SCREEN_REPO] = [merged_pull(212, NOW - WINDOW - 1)]
    (fleet.tmp / "gh-world.json").write_text(json.dumps(world))
    assert run_screens(fleet, gh).returncode == 0
    assert screen_feed(fleet)["items"] == []
    assert not images.exists()


def test_screen_changes_lists_a_pull_request_first_seen_merged(fleet):
    gh = one_screen_world(fleet)
    world = json.loads((fleet.tmp / "gh-world.json").read_text())
    world["pulls"][SCREEN_REPO] = []
    world["merged"][SCREEN_REPO] = [
        merged_pull(212, NOW - 86400),
        merged_pull(213, NOW - 30 * 86400, head="d" * 40),
    ]
    (fleet.tmp / "gh-world.json").write_text(json.dumps(world))
    assert run_screens(fleet, gh).returncode == 0
    (item,) = screen_feed(fleet)["items"]
    assert (item["pr"], item["state"]) == (212, "merged")
    assert "d" * 40 not in (fleet.tmp / "gh.log").read_text()


def test_screen_changes_keeps_the_previous_feed_when_gh_fails(fleet):
    assert run_screens(fleet, one_screen_world(fleet)).returncode == 0
    before = (fleet.out / "screen-changes.json").read_text()
    result = run_screens(fleet, one_screen_world(fleet, fail=True))
    assert result.returncode != 0
    assert "screen changes feed was not published" in result.stderr
    assert (fleet.out / "screen-changes.json").read_text() == before
    assert (fleet.out / "screen-changes/lifekit-dashboard-212").is_dir()


def test_screen_changes_is_off_without_gh(fleet):
    fleet.write(summary())
    assert fleet.run().returncode == 0
    assert not (fleet.out / "screen-changes.json").exists()


def test_screen_changes_withholds_an_item_that_looks_like_a_credential(fleet):
    gh = one_screen_world(fleet)
    world = json.loads((fleet.tmp / "gh-world.json").read_text())
    world["pulls"][SCREEN_REPO][0]["title"] = "key " + "ghp_" + "A" * 24
    (fleet.tmp / "gh-world.json").write_text(json.dumps(world))
    assert run_screens(fleet, gh).returncode == 0
    assert screen_feed(fleet)["items"] == []
    assert "ghp_" not in (fleet.out / "screen-changes.json").read_text()


# --- stalled: one mechanical check over every queue's native age -----------------

DAY = 86400


def stalled_world(homes, *, a2a=True, jobs=True, issues=True):
    """The four sources of the done-when, plus what must stay quiet: a fresh
    item per queue and a held one. Returns the run's extra environment."""
    tmp = homes.tmp
    old = "2027-01-01"  # two weeks before NOW
    vps = status_home(
        tmp,
        "fm-vps",
        [
            task("silent", state="in_flight", repo="o/alpha", created=old),
            task("fresh", state="in_flight", repo="o/alpha", created="2027-01-15"),
            task(
                "parked",
                held="yes",
                hold_kind="parked",
                repo="o/alpha",
                created=old,
            ),
            task("spoken", state="in_flight", repo="o/alpha", created=old),
        ],
    )
    (vps / "state").mkdir(exist_ok=True)
    (vps / "state/silent.status").write_text(f"working [at={NOW - 3 * DAY}]: x\n")
    (vps / "state/spoken.status").write_text(
        f"working [at={NOW - 3 * DAY}]: x\nworking [at={NOW - 60}]: y\n"
    )

    def iso(age):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age))

    listed = [
        {
            "number": 1,
            "title": "Aged",
            "url": "https://github.com/o/alpha/issues/1",
            "updatedAt": iso(8 * DAY),
            "labels": [{"name": "ops"}],
        },
        {
            "number": 2,
            "title": "Fresh",
            "url": "https://github.com/o/alpha/issues/2",
            "updatedAt": iso(DAY),
            "labels": [],
        },
        {
            "number": 3,
            "title": "Aged but held",
            "url": "https://github.com/o/alpha/issues/3",
            "updatedAt": iso(30 * DAY),
            "labels": [{"name": "Hold"}],
        },
    ]
    (tmp / "gh-issues.json").write_text(json.dumps(listed))
    gh = stub(
        tmp / "gh",
        'if [ "$1" = issue ]; then cat ' + str(tmp / "gh-issues.json") + "\n"
        "else echo '[]'; fi\n",
    )
    job = {"kind": "video", "attempts": 0, "queued_at": NOW - DAY // 2}
    (tmp / "jobs.json").write_text(
        json.dumps(
            {
                "schema": "jobq-ages.v1",
                "max_attempts": 3,
                "jobs": [
                    {
                        **job,
                        "id": "spent",
                        "status": "failed",
                        "attempts": 3,
                        "finished_at": NOW - 5000,
                        "class": "life",
                    },
                    {
                        **job,
                        "id": "waiting",
                        "status": "queued",
                        "queued_at": NOW - 2 * DAY,
                        "class": None,
                    },
                    {**job, "id": "young", "status": "queued", "class": "life"},
                    {**job, "id": "last-try", "status": "running", "attempts": 3},
                    {**job, "id": "finished", "status": "done", "attempts": 1},
                ],
            }
        )
    )
    (tmp / "threads.json").write_text(
        json.dumps(
            [
                {
                    "thread": "late",
                    "owner": "finance",
                    "deadline": NOW - 60,
                    "last_turn": NOW - 10,
                },
                {"thread": "quiet", "owner": "dev", "last_turn": NOW - 2 * DAY},
                {
                    "thread": "live",
                    "owner": "dev",
                    "deadline": NOW + DAY,
                    "last_turn": NOW - 60,
                },
                {
                    "thread": "over",
                    "owner": "dev",
                    "state": "completed",
                    "deadline": NOW - DAY,
                },
            ]
        )
    )
    env = {"FLEET_GH": str(gh), "FLEET_PR_TTL": "0"}
    env["FLEET_JOBS_CMD"] = f"cat {tmp / 'jobs.json'}"
    env["FLEET_A2A_CMD"] = f"cat {tmp / 'threads.json'}"
    return vps, env


def stalled_ids(page):
    return {g["owner"]: sorted(i["id"] for i in g["items"]) for g in page["stalled"]}


def test_every_queue_marks_its_aged_items_stalled_with_the_right_owner(homes):
    vps, env = stalled_world(homes)
    assert homes.run(vps, **env).returncode == 0
    page = status(homes)
    assert page["errors"] == []
    assert [g["owner"] for g in page["stalled"]] == [
        "dev",
        "ops",
        "finance",
        "life",
        "firstmate",
    ]
    assert stalled_ids(page) == {
        "dev": ["quiet"],
        "ops": ["o/alpha#1"],
        "finance": ["late"],
        "life": ["spent"],
        "firstmate": ["silent", "waiting"],
    }
    assert [g["count"] for g in page["stalled"]] == [1, 1, 1, 1, 2]
    # The held issue, the held record, the fresh and the finished never stall.
    flat = {i["id"]: i for g in page["stalled"] for i in g["items"]}
    assert flat["silent"]["reason"] == "no-status-line"
    assert flat["silent"]["since"] == NOW - 3 * DAY
    assert flat["silent"]["threshold"] == 48 * 3600
    assert flat["o/alpha#1"]["reason"] == "no-update"
    assert flat["o/alpha#1"]["threshold"] == 7 * DAY
    assert flat["spent"]["reason"] == "attempts-exhausted"
    assert flat["waiting"]["reason"] == "waiting"
    assert flat["late"]["reason"] == "past-deadline"
    assert flat["quiet"]["reason"] == "no-turn"
    assert page["stalled_sources"]["a2a"] == {"read": True, "reason": None}


def test_a_stalled_record_carries_its_owner_and_a_fresh_one_carries_null(homes):
    vps, env = stalled_world(homes)
    assert homes.run(vps, **env).returncode == 0
    got = {r["id"]: r for r in status(homes)["records"]}
    assert got["silent"]["stalled"] == {
        "since": NOW - 3 * DAY,
        "threshold": 48 * 3600,
        "owner": "firstmate",
    }
    for quiet in ("fresh", "parked", "spoken"):
        assert got[quiet]["stalled"] is None
    tops = {t["id"]: t for t in status(homes)["queue"]["top"]}
    assert tops["parked"]["stalled"] is None if "parked" in tops else True


def test_a_queued_record_stalls_on_its_creation_date(homes):
    vps = status_home(
        homes.tmp, "fm-vps", [task("old", created="2027-01-10"), task("new")]
    )
    assert homes.run(vps).returncode == 0
    page = status(homes)
    top = {t["id"]: t["stalled"] for t in page["queue"]["top"]}
    assert top["new"] is None
    assert top["old"]["owner"] == "firstmate"
    assert top["old"]["since"] == NOW - 5 * DAY - 8 * 3600
    assert stalled_ids(page)["firstmate"] == ["old"]


def test_a_threshold_in_config_changes_the_result_with_no_code_change(homes):
    vps, env = stalled_world(homes)
    cfg = json.loads((DIR / "fleet-stalled.json").read_text())
    cfg["github"]["days"] = 30
    cfg["backlog"]["hours"] = 24 * 30
    cfg["jobq"]["waiting_hours"] = 24 * 30
    cfg["a2a"]["idle_hours"] = 24 * 30
    path = homes.tmp / "stalled.json"
    path.write_text(json.dumps(cfg))
    assert homes.run(vps, FLEET_STALLED_CONFIG=str(path), **env).returncode == 0
    # Only what is stalled by its own rule remains: spent attempts, a passed deadline.
    assert stalled_ids(status(homes)) == {
        "dev": [],
        "ops": [],
        "finance": ["late"],
        "life": ["spent"],
        "firstmate": [],
    }


def test_a_missing_or_broken_threshold_file_marks_nothing_and_says_so(homes):
    vps, env = stalled_world(homes)
    path = homes.tmp / "stalled.json"
    path.write_text("{not json")
    assert homes.run(vps, FLEET_STALLED_CONFIG=str(path), **env).returncode == 0
    page = status(homes)
    assert page["stalled"] is None and page["stalled_sources"] is None
    assert all(r["stalled"] is None for r in page["records"])
    assert any("stalled thresholds" in e for e in page["errors"])


def test_an_unreachable_a2a_source_is_said_not_guessed(homes):
    vps, env = stalled_world(homes)
    del env["FLEET_A2A_CMD"]
    assert homes.run(vps, **env).returncode == 0
    page = status(homes)
    assert page["stalled_sources"]["a2a"] == {
        "read": False,
        "reason": "no relay or session records reachable",
    }
    assert page["errors"] == []
    assert stalled_ids(page)["finance"] == []


def test_a_failing_source_empties_only_its_own_rows(homes):
    vps, env = stalled_world(homes)
    env["FLEET_JOBS_CMD"] = "false"
    assert homes.run(vps, **env).returncode == 0
    page = status(homes)
    assert page["stalled_sources"]["jobq"]["read"] is False
    assert any("job ages unreadable" in e for e in page["errors"])
    assert stalled_ids(page)["life"] == []
    assert stalled_ids(page)["ops"] == ["o/alpha#1"]


def test_issues_are_read_inside_the_pr_budget_and_cached_with_the_count(homes):
    vps, env = stalled_world(homes)
    env["FLEET_PR_TTL"] = "600"
    assert homes.run(vps, **env).returncode == 0
    cache = json.loads((homes.tmp / "state/status-open-prs.json").read_text())
    assert [i["number"] for i in cache["o/alpha"]["issues"]] == [1, 2, 3]
    # A second run inside the TTL reads no gh at all and stalls the same.
    (homes.tmp / "gh-issues.json").write_text("[]")
    assert homes.run(vps, **env).returncode == 0
    assert stalled_ids(status(homes))["ops"] == ["o/alpha#1"]


# --- decision follow-through: a delivered decision posts one outcome -------


def decisions_vault(root, items, status="accepted", extra=""):
    folder = root / "vault/system/decisions"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "0042-demo-decision.md").write_text(
        "---\nname: decision-0042-demo\n"
        'summary: "Demo queues now have one owner each."\n'
        f"status: {status}\ndate: 2026-11-16\n"
        f"items: [{', '.join(items)}]\n{extra}---\n\n# 0042 - Demo decision\n"
    )
    return root / "vault"


def issues_gh(root, closed):
    """A gh stub: `issue view N -R repo` answers closed or open per the file."""
    (root / "closed.txt").write_text("\n".join(closed))
    return stub(
        root / "gh-issues",
        'n="$3"; state=OPEN; grep -qx "$n" "$(dirname "$0")/closed.txt" && state=CLOSED\n'
        'printf \'{"state":"%s","title":"Item %s","url":"https://github.com/o/r/issues/%s",'
        '"closedAt":"2026-11-17T00:00:00Z"}\' "$state" "$n" "$n"\n',
    )


def delivered_page(homes, tmp_path, closed, **env):
    vps = homes.tmp / "fm-vps"
    if not vps.exists():
        status_home(homes.tmp, "fm-vps", [])
    vault = decisions_vault(tmp_path, ["o/r#1", "o/r#2"])
    gh = issues_gh(tmp_path, closed)
    res = homes.run(
        vps,
        FLEET_GH=gh,
        FLEET_VAULT=str(vault),
        FLEET_DECISIONS_CONFIG=str(tmp_path / "none.json"),
        **env,
    )
    assert res.returncode == 0, res.stderr
    return status(homes)["delivered"]


def test_a_decision_with_an_open_item_posts_nothing(homes, tmp_path):
    assert delivered_page(homes, tmp_path, ["1"]) == []
    assert not (tmp_path / "state/decisions-delivered.json").exists()


def test_a_decision_with_every_item_closed_posts_one_outcome_once(homes, tmp_path):
    first = delivered_page(homes, tmp_path, ["1", "2"])
    assert len(first) == 1
    out = first[0]
    assert out["id"] == "0042" and out["date"] == "2027-01-15"
    assert [ln.split(" ")[0] for ln in out["lines"]] == ["Shipped", "Changed:", "Still"]
    assert "2 of 2 items closed" in out["lines"][0]
    assert "Demo queues now have one owner each." in out["lines"][1]
    assert out["brief_date"] == "2027-01-15"  # NOW is before 08:30 UTC that day
    # a later run, even with an item reopened, never posts it again
    second = delivered_page(homes, tmp_path, ["1"], FLEET_NOW=str(NOW + 7200))
    assert second == first


def test_one_brief_day_carries_one_outcome(homes, tmp_path):
    vps = status_home(homes.tmp, "fm-vps", [])
    vault = decisions_vault(tmp_path, ["o/r#1"])
    folder = vault / "system/decisions"
    (folder / "0043-second.md").write_text(
        (folder / "0042-demo-decision.md").read_text().replace("0042", "0043")
    )
    gh = issues_gh(tmp_path, ["1"])
    res = homes.run(
        vps,
        FLEET_GH=gh,
        FLEET_VAULT=str(vault),
        FLEET_DECISIONS_CONFIG=str(tmp_path / "none.json"),
    )
    assert res.returncode == 0, res.stderr
    days = [d["brief_date"] for d in status(homes)["delivered"]]
    assert len(days) == 2 and len(set(days)) == 2


def test_a_record_that_is_not_accepted_posts_nothing(homes, tmp_path):
    vps = status_home(homes.tmp, "fm-vps", [])
    vault = decisions_vault(tmp_path, ["o/r#1"], status="proposed")
    gh = issues_gh(tmp_path, ["1"])
    homes.run(vps, FLEET_GH=gh, FLEET_VAULT=str(vault))
    assert status(homes)["delivered"] == []


def test_back_fill_posts_0015_and_0016_on_the_first_run(homes, tmp_path):
    vps = status_home(homes.tmp, "fm-vps", [])
    folder = tmp_path / "vault/system/decisions"
    folder.mkdir(parents=True)
    for did, slug in (("0015", "ownership-routing"), ("0016", "queue-mechanics")):
        (folder / f"{did}-{slug}.md").write_text(
            f'---\nname: decision-{did}-{slug}\nsummary: "S {did}."\n'
            f"status: accepted\n---\n\n# {did} - {slug}\n"
        )
    every = [str(n) for n in range(1, 2000)]
    gh = issues_gh(tmp_path, every)
    res = homes.run(vps, FLEET_GH=gh, FLEET_VAULT=str(tmp_path / "vault"))
    assert res.returncode == 0, res.stderr
    page = status(homes)["delivered"]
    assert [d["id"] for d in page] == ["0015", "0016"]
    assert len(page[1]["items"]) == 8


def test_an_unreadable_mark_file_posts_nothing(homes, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state/decisions-delivered.json").write_text("{not json")
    assert delivered_page(homes, tmp_path, ["1", "2"]) is None


def test_publisher_writes_the_activity_journal_once_per_day(fleet):
    fleet.write(summary())
    # NOW is 2027-01-15 08:00 UTC: yesterday is a finished day, with no source to read.
    assert fleet.run().returncode == 0
    store = fleet.out / "journal/activity.jsonl"
    (row,) = [json.loads(x) for x in store.read_text().splitlines()]
    assert row["date"] == "2027-01-14" and row["openclaw_runs"] == "unavailable"
    again = fleet.run(FLEET_NOW=str(NOW + 60), FLEET_JOURNAL_EVERY="0")
    assert again.returncode == 0
    assert len(store.read_text().splitlines()) == 1


# --- the daily brief's Problems line ---------------------------------------


def test_four_problems_show_the_three_oldest_and_count_the_rest(homes):
    vps, env = stalled_world(homes)
    assert homes.run(vps, **env).returncode == 0
    seen = homes.tmp / "state/problems-seen.json"
    marks = json.loads(seen.read_text())
    assert len(marks) == 6
    # age the marks by hand: the first runs saw these days ago
    for key, days in (("quiet", 9), ("spent", 7), ("o/alpha#1", 6), ("late", 2)):
        (hit,) = [k for k in marks if k.endswith(f":{key}")]
        marks[hit]["first_seen"] = NOW - days * DAY
    seen.write_text(json.dumps(marks))
    assert homes.run(vps, **env).returncode == 0
    problems = status(homes)["problems"]
    assert problems["count"] == 6 and problems["more"] == 3
    assert [p["id"] for p in problems["shown"]] == ["quiet", "spent", "o/alpha#1"]
    assert [p["age_days"] for p in problems["shown"]] == [9, 7, 6]
    assert [p["owner"] for p in problems["shown"]] == ["dev", "life", "ops"]
    line = problems["line"]
    assert line.startswith("Problems: ") and "\n" not in line
    assert line.endswith("; +3 more") and line.count("(") == 3


def test_exactly_four_problems_say_one_more(homes):
    vps, env = stalled_world(homes, a2a=False)
    env = {k: v for k, v in env.items() if k != "FLEET_A2A_CMD"}
    # without threads the world holds spent, waiting, silent, o/alpha#1
    assert homes.run(vps, **env).returncode == 0
    problems = status(homes)["problems"]
    assert problems["count"] == 4
    assert len(problems["shown"]) == 3 and problems["more"] == 1
    assert problems["line"].endswith("; +1 more")


def test_no_problems_means_no_problems_line(homes):
    vps = status_home(homes.tmp, "fm-vps", [task("fresh", created="2027-01-15")])
    assert homes.run(vps).returncode == 0
    assert status(homes)["problems"] is None


def test_a_problem_ages_from_its_first_run_and_resets_only_when_it_clears(homes):
    vps, env = stalled_world(homes)

    def day(n):
        return {**env, "FLEET_NOW": str(NOW + n * DAY)}

    assert homes.run(vps, **day(0)).returncode == 0
    assert status(homes)["problems"]["count"] == 6
    assert homes.run(vps, **day(3)).returncode == 0
    page = status(homes)
    assert {p["age_days"] for p in page["problems"]["shown"]} == {3}
    # the job clears: its mark is dropped; it returns as a new problem, age 0
    (homes.tmp / "jobs.json").write_text(
        json.dumps({"schema": "jobq-ages.v1", "max_attempts": 3, "jobs": []})
    )
    assert homes.run(vps, **day(4)).returncode == 0
    assert "job:None:spent" not in json.loads(
        (homes.tmp / "state/problems-seen.json").read_text()
    )
    (homes.tmp / "jobs.json").write_text(
        json.dumps(
            {
                "schema": "jobq-ages.v1",
                "max_attempts": 3,
                "jobs": [
                    {
                        "id": "spent",
                        "kind": "video",
                        "status": "failed",
                        "attempts": 3,
                        "finished_at": NOW,
                        "queued_at": NOW,
                        "class": "life",
                    }
                ],
            }
        )
    )
    assert homes.run(vps, **day(5)).returncode == 0
    marks = json.loads((homes.tmp / "state/problems-seen.json").read_text())
    assert marks["job:None:spent"]["first_seen"] == NOW + 5 * DAY
    assert marks["thread:None:quiet"]["first_seen"] == NOW  # never cleared


def test_an_unreadable_source_keeps_its_problems_aging(homes):
    vps, env = stalled_world(homes)
    assert homes.run(vps, **env).returncode == 0
    bad = {**env, "FLEET_JOBS_CMD": "false", "FLEET_NOW": str(NOW + 2 * DAY)}
    assert homes.run(vps, **bad).returncode == 0
    marks = json.loads((homes.tmp / "state/problems-seen.json").read_text())
    assert marks["job:None:spent"]["first_seen"] == NOW


def test_a_broken_mark_file_posts_no_problems_line(homes):
    vps, env = stalled_world(homes)
    (homes.tmp / "state").mkdir(exist_ok=True)
    (homes.tmp / "state/problems-seen.json").write_text("{not json")
    assert homes.run(vps, **env).returncode == 0
    page = status(homes)
    assert page["problems"] is None
    assert any("problems-seen.json" in e for e in page["errors"])


# --- the Monday and Thursday side-hustle lines -----------------------------

MONDAY = NOW + 3 * DAY  # 2027-01-18 08:00 UTC
THURSDAY = NOW + 6 * DAY  # 2027-01-21 08:00 UTC


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


def feed_rows(name, at, **over):
    """One clip stats run for a channel: two Short rows and the channel row."""
    stamp = iso(at)
    videos = [
        {"at": stamp, "kind": "video", "channel": name, "clip": f"{name}_a.mp4",
         "video_id": "a", "found": True, "views": 120},
        {"at": stamp, "kind": "video", "channel": name, "clip": f"{name}_b.mp4",
         "video_id": "b", "found": True, "views": 900},
    ]  # fmt: skip
    channel = {
        "at": stamp, "kind": "channel", "channel": name, "videos_recorded": 5,
        "subscribers": 1234, "status": "ok",
        "days7": {"views": 4321, "subscribers_gained": 7},
    }  # fmt: skip
    return [*videos, {**channel, **over}]


def side_home(homes):
    """The one empty home of the test (made on first use)."""
    home = homes.tmp / "fm-vps"
    return home if home.exists() else status_home(homes.tmp, "fm-vps", [])


def side_world(homes, rows, now, name="stats.jsonl", raw=None):
    feed = homes.tmp / name
    feed.write_text(
        raw if raw is not None else "".join(json.dumps(r) + "\n" for r in rows)
    )
    vps = side_home(homes)
    res = homes.run(vps, FLEET_NOW=str(now), FLEET_CLIP_STATS_FEED=str(feed))
    assert res.returncode == 0, res.stderr
    return status(homes)["side_hustles"]


def week_before(name, now):
    """The same channel a week earlier, 3 uploads fewer."""
    return feed_rows(name, now - 7 * DAY - 3600, videos_recorded=2)


def test_monday_and_thursday_carry_one_line_per_channel_with_the_four_numbers(homes):
    for now in (MONDAY, THURSDAY):
        rows = [*week_before("alpha", now), *feed_rows("alpha", now - 3600)]
        rows += [*week_before("beta", now), *feed_rows("beta", now - 3600)]
        side = side_world(homes, rows, now)
        assert side["brief_day"] is True and side["credential"] is None
        assert side["lines"] == [
            f"{c}: 7d views 4,321 · subs 1,234 · uploads 3 · best Short {c}_b.mp4 (900 views)"
            for c in ("alpha", "beta")
        ]


def test_other_days_carry_no_brief_line(homes):
    rows = [*week_before("alpha", NOW), *feed_rows("alpha", NOW - 3600)]
    for now in (NOW, MONDAY + DAY, MONDAY + 2 * DAY, THURSDAY + DAY):
        side = side_world(homes, rows, now)
        assert side["brief_day"] is False and side["lines"] == []
        assert side["week_rows"] == []
        assert side["channels"][0]["status"] in ("ok", "no_data")


def test_the_monday_week_sheet_has_one_weekly_row_per_channel_and_thursday_none(homes):
    now = MONDAY
    rows = [*week_before("alpha", now), *feed_rows("alpha", now - 3600)]
    rows += feed_rows("beta", now - 3600)  # no row a week back: uploads unknown
    side = side_world(homes, rows, now)
    assert [r["channel"] for r in side["week_rows"]] == ["alpha", "beta"]
    alpha, beta = side["week_rows"]
    assert alpha == {
        "channel": "alpha", "week_of": "2027-01-18", "status": "ok",
        "views_7d": 4321, "subscribers": 1234, "uploads": 3,
        "best_short": {"clip": "alpha_b.mp4", "views": 900},
    }  # fmt: skip
    assert beta["uploads"] == "no data" and beta["views_7d"] == 4321
    thursday = side_world(homes, rows, THURSDAY)
    assert thursday["week_rows"] == [] and len(thursday["lines"]) == 2


def test_a_channel_with_no_data_in_the_window_says_no_data_not_zeros(homes):
    now = MONDAY
    stale = feed_rows("alpha", now - 9 * DAY)  # the last run is older than the window
    empty = feed_rows("beta", now - 3600, days7=None, subscribers=None)
    rows = [*stale, *empty, *feed_rows("gamma", now - 3600, days7={"views": 0})]
    side = side_world(homes, rows, now)
    assert side["lines"][0] == "alpha: no data"
    assert side["lines"][1] == "beta: no data"
    # a real zero is a number; what the feed does not hold is "no data"
    gamma = side["lines"][2]
    assert gamma.startswith("gamma: 7d views 0 · subs 1,234 · uploads no data")
    rows_by = {r["channel"]: r for r in side["week_rows"]}
    assert (
        rows_by["alpha"]["status"] == "no_data" and "views_7d" not in rows_by["alpha"]
    )
    assert (
        rows_by["gamma"]["views_7d"] == 0 and rows_by["gamma"]["uploads"] == "no data"
    )


def test_a_rejected_analytics_scope_is_one_credential_line_and_no_numbers(homes):
    now = THURSDAY
    consent = {"status": "needs_reconsent", "subscribers": None, "days7": None}
    rejected = {
        "status": "error",
        "reason": "Google rejected the stored credentials; run `clip auth --channel b`",
    }
    rows = [*week_before("alpha", now), *feed_rows("alpha", now - 3600)]
    rows += feed_rows("beta", now - 3600, **consent)
    rows += feed_rows("gamma", now - 3600, **rejected)
    side = side_world(homes, rows, now)
    credential = side["credential"]
    assert credential.startswith("Side hustles: YouTube Analytics needs consent again")
    assert "beta, gamma" in credential and "clip auth" in credential
    assert side["lines"] == [side["lines"][0], credential]
    assert side["lines"][0].startswith("alpha: 7d views 4,321")
    assert not any(c.isdigit() for c in credential)  # no number of any kind
    by = {c["channel"]: c["status"] for c in side["channels"]}
    assert by == {"alpha": "ok", "beta": "needs_consent", "gamma": "needs_consent"}


def test_an_unreadable_feed_shows_the_no_data_line_and_the_same_run_with_a_valid_feed_the_numbers(
    homes,
):
    now = MONDAY
    # blocked case: the feed path is a directory, so it cannot be read
    (homes.tmp / "gone").mkdir()
    vps = side_home(homes)
    res = homes.run(
        vps, FLEET_NOW=str(now), FLEET_CLIP_STATS_FEED=str(homes.tmp / "gone")
    )
    assert res.returncode == 0, res.stderr
    blocked = status(homes)
    side = blocked["side_hustles"]
    assert side["feed_read"] is False
    assert side["lines"] == ["Side hustles: no data (stats feed unreadable)"]
    assert side["week_rows"][0]["status"] == "no_data"
    assert any("clip stats feed unreadable" in e for e in blocked["errors"])
    # allowed case: the same run with a valid feed
    rows = [*week_before("alpha", now), *feed_rows("alpha", now - 3600)]
    allowed = side_world(homes, rows, now)
    assert allowed["feed_read"] is True
    assert allowed["lines"][0].startswith("alpha: 7d views 4,321 · subs 1,234")


def test_the_brief_keeps_its_cap_with_many_channels_and_a_credential(homes):
    now = MONDAY
    rows = []
    for name in ("a", "b", "c", "d"):
        rows += [*week_before(name, now), *feed_rows(name, now - 3600)]
    rows += feed_rows("e", now - 3600, status="needs_reconsent")
    side = side_world(homes, rows, now)
    assert len(side["lines"]) == fleet_status.SIDE_LINES == 3
    assert side["lines"][0].startswith("a: 7d views")
    assert side["lines"][1] == "+3 more channels not shown"
    assert side["lines"][2] == side["credential"]
    assert "needs consent again for e" in side["lines"][2]
    assert len(side["week_rows"]) == 5  # the sheet still has every channel


def test_no_feed_configured_or_an_empty_feed(homes):
    vps = side_home(homes)
    assert homes.run(vps, FLEET_NOW=str(MONDAY)).returncode == 0
    assert status(homes)["side_hustles"] is None
    side = side_world(homes, [], MONDAY, raw="not json\n")
    assert side["lines"] == [
        "Side hustles: no data (no channel rows in the stats feed)"
    ]


# --- clip queue rows and the clip and scout stalled sources -----------------------


def clip_iso(age):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - age))


def clip_channel(**over):
    base = {
        "buffer_days": 3.0,
        "buffer_waiting": 3,
        "checked_at": clip_iso(600),
        "last_post": clip_iso(6 * 3600),
        "slot": clip_iso(8 * 3600),
        "status": "ok",
    }
    return {**base, **over}


def clip_world(homes, channels, *, egress="up", files=True):
    """A clip queue directory beside the stats feed; returns the run's env."""
    queue = homes.tmp / "clipq"
    queue.mkdir(exist_ok=True)
    if files:
        (queue / "daily-check.json").write_text(json.dumps({"channels": channels}))
        (queue / "egress.json").write_text(json.dumps({"state": egress}))
        (queue / "queue-run.json").write_text(
            json.dumps({"at": clip_iso(900), "ran": [], "idle": list(channels)})
        )
    for name in channels:
        pending = queue / name / "pending"
        pending.mkdir(parents=True, exist_ok=True)
    return {
        "FLEET_NOW": str(NOW),
        "FLEET_CLIP_STATS_FEED": str(queue / "stats.jsonl"),
    }


def clip_rows(homes, env):
    assert homes.run(side_home(homes), **env).returncode == 0
    page = status(homes)
    return page, {r["channel"]: r for r in page["clip_queue"]["channels"]}


def test_a_healthy_clip_channel_is_published_not_stalled(homes):
    env = clip_world(homes, {"cosmos": clip_channel()})
    pend = homes.tmp / "clipq/cosmos/pending"
    (pend / "a").write_text(f"https://x/a\nadded: {clip_iso(7200)}\nby: seed\n")
    (pend / "b").write_text(f"https://x/b\nadded: {clip_iso(3600)}\nby: seed\n")
    page, rows = clip_rows(homes, env)
    assert rows["cosmos"] == {
        "channel": "cosmos",
        "status": "ok",
        "waiting": 3,
        "oldest_age_s": 7200,
        "buffer_days": 3.0,
        "egress": "up",
        "stalled": False,
        "stalled_reason": None,
        "unavailable": [],
        "owner": "clip",
        "class": "ops",
        "store": "clip-queue",
        "ref": "cosmos",
    }
    assert [
        i for g in page["stalled"] for i in g["items"] if i["source"] == "clip"
    ] == []
    assert page["stalled_sources"]["clip"] == {"read": True, "reason": None}


def test_every_live_channel_has_a_row_and_the_missing_ones_say_unavailable(homes):
    env = clip_world(homes, {"cosmos": clip_channel()})
    _, rows = clip_rows(homes, env)
    assert list(rows) == ["cosmos", "mind", "neural", "yc", "vice-footage"]
    gone = rows["vice-footage"]
    assert gone["status"] == "unavailable"
    assert gone["waiting"] is None and gone["buffer_days"] is None
    assert gone["stalled"] is None and "waiting" in gone["unavailable"]


def test_missing_clip_inputs_are_unavailable_never_zero(homes):
    env = clip_world(homes, {"cosmos": clip_channel()}, files=False)
    page, rows = clip_rows(homes, env)
    for row in rows.values():
        assert row["waiting"] is None and row["buffer_days"] is None
        assert row["egress"] is None and row["stalled"] is None
    assert page["clip_queue"]["unavailable"] == [
        "daily-check.json",
        "egress.json",
        "queue-run.json",
    ]
    assert page["stalled_sources"]["clip"]["read"] is False
    assert any("clip queue input unreadable" in e for e in page["errors"])


def test_an_empty_buffer_is_a_stalled_channel_with_a_real_zero(homes):
    chans = {"neural": clip_channel(buffer_days=0.0, buffer_waiting=0)}
    env = clip_world(homes, chans)
    page, rows = clip_rows(homes, env)
    assert rows["neural"]["waiting"] == 0 and rows["neural"]["buffer_days"] == 0.0
    assert rows["neural"]["oldest_age_s"] is None  # nothing waits: no age
    assert rows["neural"]["stalled"] is True
    assert rows["neural"]["stalled_reason"] == "buffer-short"
    items = {i["id"]: i for g in page["stalled"] for i in g["items"]}
    assert items["neural"]["source"] == "clip"
    assert items["neural"]["reason"] == "buffer-short"


def test_no_post_inside_the_slot_window_is_stalled_and_a_never_posted_one_too(homes):
    chans = {
        "mind": clip_channel(last_post=clip_iso(40 * 3600)),
        "vice-footage": clip_channel(last_post=None, buffer_days=0.0),
    }
    page, rows = clip_rows(homes, clip_world(homes, chans))
    assert rows["mind"]["stalled_reason"] == "no-post"
    assert rows["vice-footage"]["stalled_reason"] == "no-post"
    owners = {g["owner"]: [i["id"] for i in g["items"]] for g in page["stalled"]}
    assert sorted(owners["firstmate"]) == ["mind", "vice-footage"]


def test_a_down_egress_is_published_on_every_row(homes):
    env = clip_world(homes, {"yc": clip_channel()}, egress="pc-down")
    _, rows = clip_rows(homes, env)
    assert {r["egress"] for r in rows.values()} == {"pc-down"}
    assert rows["yc"]["stalled"] is False  # a down egress alone is not a stall


def test_the_clip_queue_is_read_only(homes):
    env = clip_world(homes, {"cosmos": clip_channel()})
    queue = homes.tmp / "clipq"
    (queue / "cosmos/pending/a").write_text(f"u\nadded: {clip_iso(60)}\n")

    def snapshot():
        return {
            p: (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
            for p in queue.rglob("*")
        }

    before = snapshot()
    clip_rows(homes, env)
    assert snapshot() == before


def test_the_scout_is_stalled_when_its_output_is_older_than_the_interval(homes):
    state = homes.tmp / "scout"
    (state / "candidates").mkdir(parents=True)
    out = state / "candidates/2027-01-01.txt"
    out.write_text("ideas\tx\n")
    env = {"FLEET_NOW": str(NOW), "FLEET_SCOUT_STATE": str(state)}
    os.utime(out, (NOW - 5 * DAY, NOW - 5 * DAY))
    assert homes.run(side_home(homes), **env).returncode == 0
    page = status(homes)
    item = next(
        i for g in page["stalled"] for i in g["items"] if i["source"] == "scout"
    )
    assert item["reason"] == "no-output" and item["threshold"] == 96 * 3600
    os.utime(out, (NOW - DAY, NOW - DAY))
    assert homes.run(side_home(homes), **env).returncode == 0
    page = status(homes)
    assert not [
        i for g in page["stalled"] for i in g["items"] if i["source"] == "scout"
    ]
    assert page["stalled_sources"]["scout"] == {"read": True, "reason": None}


def test_an_unset_scout_or_clip_feed_is_said_not_guessed(homes):
    assert homes.run(side_home(homes), FLEET_NOW=str(NOW)).returncode == 0
    page = status(homes)
    assert page["clip_queue"] is None
    assert page["stalled_sources"]["clip"]["read"] is False
    assert page["stalled_sources"]["scout"]["read"] is False


def test_a_held_fleet_task_left_unfiled_is_stalled_after_a_day(homes):
    held = homes.tmp / "held.json"
    held.write_text(
        json.dumps(
            [
                {
                    "id": "2026-11-25:aaa",
                    "note": "2026-11-25.md",
                    "title": "for firstmate: share the coach context",
                    "since": NOW - 25 * 3600,
                },
                {
                    "id": "2026-11-26:bbb",
                    "note": "2026-11-26.md",
                    "title": "for devclaw: fix the backup job",
                    "since": NOW - 3600,
                },
                {"id": "no-since"},
            ]
        )
    )
    env = {"FLEET_NOW": str(NOW), "FLEET_KIT_HELD_CMD": f"cat {held}"}
    assert homes.run(side_home(homes), **env).returncode == 0
    page = status(homes)
    rows = [i for g in page["stalled"] for i in g["items"] if i["source"] == "held"]
    assert [r["id"] for r in rows] == ["2026-11-25:aaa"]
    assert rows[0]["reason"] == "not-filed"
    assert rows[0]["threshold"] == 24 * 3600
    assert rows[0]["since"] == NOW - 25 * 3600
    group = next(g for g in page["stalled"] if g["owner"] == "firstmate")
    assert "2026-11-25:aaa" in [i["id"] for i in group["items"]]
    assert page["stalled_sources"]["kit_held"] == {
        "read": True,
        "reason": None,
        "open": 2,
    }


def test_unset_or_unreadable_held_items_are_said_not_guessed(homes):
    assert homes.run(side_home(homes), FLEET_NOW=str(NOW)).returncode == 0
    assert status(homes)["stalled_sources"]["kit_held"]["read"] is False
    bad = homes.tmp / "bad.json"
    bad.write_text("not json")
    assert (
        homes.run(
            side_home(homes), FLEET_NOW=str(NOW), FLEET_KIT_HELD_CMD=f"cat {bad}"
        ).returncode
        == 0
    )
    page = status(homes)
    assert page["stalled_sources"]["kit_held"]["read"] is False
    assert any("held items unreadable" in e for e in page["errors"])


# --- queue items: the 0019 envelope on finance, needs-you and clip records -------


def test_every_clip_channel_row_carries_the_envelope_and_no_acceptance(homes):
    env = clip_world(homes, {"cosmos": clip_channel()})
    _, rows = clip_rows(homes, env)
    for name, row in rows.items():
        assert row["owner"] == "clip" and row["class"] == "ops"
        assert row["store"] == "clip-queue" and row["ref"] == name
        assert "acceptance" not in row


def items_by_id(page):
    return {i["id"]: i for i in page["items"]}


def test_finance_items_carry_the_envelope_and_their_own_acceptance(homes):
    feed = homes.tmp / "finance.json"
    feed.write_text(
        json.dumps(
            [
                {
                    "kind": "thesis-revision",
                    "id": 41,
                    "title": "Revise  the thesis\nfor ACME",
                    "acceptance": "The revised thesis is confirmed or rejected",
                    "url": "https://finance.example/research/thesis-revisions/41",
                    "since": "2027-01-14T08:00:00Z",
                },
                {"kind": "alert", "id": "a-7", "title": "Balance below floor"},
                {"kind": "not-a-kind", "id": "x", "title": "skipped"},
                {"kind": "recommendation", "title": "no id: skipped"},
                "garbage",
            ]
        )
    )
    env = {"FLEET_NOW": str(NOW), "FLEET_FINANCE_CMD": f"cat {feed}"}
    assert homes.run(side_home(homes), **env).returncode == 0
    page = status(homes)
    assert page["item_sources"]["finance"] == {"read": True, "reason": None}
    got = items_by_id(page)
    assert set(got) == {
        "finance-db:thesis-revision/41",
        "finance-db:alert/a-7",
    }
    assert got["finance-db:thesis-revision/41"] == {
        "id": "finance-db:thesis-revision/41",
        "source": "finance",
        "kind": "thesis-revision",
        "title": "Revise the thesis for ACME",
        "owner": "finance",
        "class": "finance",
        "store": "finance-db",
        "ref": "https://finance.example/research/thesis-revisions/41",
        "since": NOW - DAY,  # NOW is 2027-01-15T08:00:00Z
        "acceptance": "The revised thesis is confirmed or rejected",
    }
    # No acceptance and no URL in the source: published without the key, ref the
    # native id, never a failure.
    alert = got["finance-db:alert/a-7"]
    assert "acceptance" not in alert
    assert alert["ref"] == "alert/a-7" and alert["since"] is None
    assert alert["owner"] == "finance" and alert["class"] == "finance"
    assert page["errors"] == []


def test_needs_you_decisions_are_kits_life_items_without_acceptance(homes):
    ledger = homes.tmp / "kit/needs-you/open.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            [
                {
                    "id": "dentist-slot",
                    "ask": "Book the Tuesday or the Friday dentist slot?",
                    "options": ["Tuesday", "Friday"],
                    "origin": "telegram",
                    "opened": "2027-01-10",
                },
                {"ask": "no id: skipped"},
            ]
        )
    )
    before = ledger.read_bytes()
    env = {"FLEET_NOW": str(NOW), "FLEET_NEEDS_YOU_LEDGER": str(ledger)}
    assert homes.run(side_home(homes), **env).returncode == 0
    page = status(homes)
    assert page["item_sources"]["needs_you"] == {"read": True, "reason": None}
    assert page["items"] == [
        {
            "id": "needs-you:dentist-slot",
            "source": "needs-you",
            "kind": "decision",
            "title": "Book the Tuesday or the Friday dentist slot?",
            "owner": "kit",
            "class": "life",
            "store": "needs-you",
            "ref": "dentist-slot",
            "since": NOW - 5 * DAY - 8 * 3600,  # 2027-01-10
            "origin": "telegram",
        }
    ]
    assert ledger.read_bytes() == before  # read-only


def test_unset_item_sources_say_so_and_the_existing_page_is_unchanged(homes):
    assert homes.run(side_home(homes), FLEET_NOW=str(NOW)).returncode == 0
    page = status(homes)
    assert page["schema"] == "fleet-status-page.v1"
    assert page["items"] == []
    assert page["item_sources"] == {
        "finance": {"read": False, "reason": "FLEET_FINANCE_CMD is not set"},
        "needs_you": {"read": False, "reason": "FLEET_NEEDS_YOU_LEDGER is not set"},
    }
    assert page["errors"] == []


def test_a_missing_ledger_is_empty_and_a_broken_source_is_an_error(homes):
    env = {
        "FLEET_NOW": str(NOW),
        "FLEET_NEEDS_YOU_LEDGER": str(homes.tmp / "nowhere/open.json"),
        "FLEET_FINANCE_CMD": "false",
    }
    assert homes.run(side_home(homes), **env).returncode == 0
    page = status(homes)
    assert page["items"] == []
    assert page["item_sources"]["needs_you"] == {"read": True, "reason": None}
    assert page["item_sources"]["finance"]["read"] is False
    assert any("finance items unreadable" in e for e in page["errors"])


# --- load: a busy host must not starve the stamp ------------------------------

SLOW_STUB = """\
import os, time
log = os.environ["SLOW_LOG"]
prom = open(os.environ["PROM_FILE"]).read()
fresh = f"fleet_publisher_last_success_timestamp_seconds {os.environ['FLEET_NOW']}" in prom
with open(log, "a") as f:
    f.write(f"{os.environ['SLOW_NAME']} {os.environ['FLEET_NOW']} stamp={'fresh' if fresh else 'old'}\\n")
time.sleep(float(os.environ.get("SLOW_SLEEP", "0")))
"""


def slow_helpers(fleet, sleep="0"):
    """Stand-ins for the two slow helpers: they log each start and whether the
    freshness stamp was already published when they began."""
    log = fleet.tmp / "slow.log"
    env = {
        "SLOW_LOG": str(log),
        "SLOW_SLEEP": sleep,
        "PROM_FILE": str(fleet.prom / "fleet.prom"),
    }
    for name, var in (("screens", "FLEET_SCREENS"), ("journal", "FLEET_JOURNAL")):
        path = fleet.tmp / f"{name}_stub.py"
        path.write_text(SLOW_STUB.replace("os.environ['SLOW_NAME']", repr(name)))
        env[var] = str(path)

    def starts():
        return log.read_text().splitlines() if log.exists() else []

    return env, starts


def timings(result):
    (line,) = [
        ln
        for ln in result.stderr.splitlines()
        if ln.startswith("fleet-publisher: timings_ms")
    ]
    return line


def test_slow_sections_follow_their_cadence_and_start_after_the_stamp(fleet):
    fleet.write(summary())
    env, starts = slow_helpers(fleet)

    def run(offset):
        result = fleet.run(FLEET_NOW=str(NOW + offset), **env)
        assert result.returncode == 0, result.stderr
        return result

    run(0)
    assert starts() == [f"screens {NOW} stamp=fresh", f"journal {NOW} stamp=fresh"]
    skipped = run(60)  # inside both cadences: neither helper starts
    assert len(starts()) == 2
    assert "skipped=screens:cadence,journal:cadence" in timings(skipped)
    run(300)  # the screens cadence (300 s) is due, the journal's (900 s) is not
    assert starts()[2:] == [f"screens {NOW + 300} stamp=fresh"]
    run(900)
    assert starts()[3:] == [
        f"screens {NOW + 900} stamp=fresh",
        f"journal {NOW + 900} stamp=fresh",
    ]
    # Every run, skipped sections or not, published its stamp.
    assert (
        fleet.metrics()["fleet_publisher_last_success_timestamp_seconds"] == NOW + 900
    )


def test_cadence_is_configurable_and_a_clock_step_back_runs_the_section(fleet):
    fleet.write(summary())
    env, starts = slow_helpers(fleet)
    env.update(FLEET_SCREENS_EVERY="30", FLEET_JOURNAL_EVERY="100000")
    for offset in (0, 30, 40):
        assert fleet.run(FLEET_NOW=str(NOW + offset), **env).returncode == 0
    names = [s.split()[0] for s in starts()]
    assert names == ["screens", "journal", "screens"]
    # A cadence stamp from the future (the clock stepped back) counts as due:
    # screens last started at NOW + 40, the journal's stamp is NOW (not due).
    assert fleet.run(FLEET_NOW=str(NOW), **env).returncode == 0
    assert [s.split()[0] for s in starts()][3:] == ["screens"]


def test_a_run_past_its_slow_budget_skips_the_slow_sections_and_keeps_them_due(fleet):
    fleet.write(summary())
    env, starts = slow_helpers(fleet)
    late = fleet.run(FLEET_SLOW_BUDGET="-1", **env)
    assert late.returncode == 0, late.stderr
    assert starts() == []
    assert "skipped=screens:budget,journal:budget" in timings(late)
    assert fleet.metrics()["fleet_publisher_last_success_timestamp_seconds"] == NOW
    # A budget skip leaves no cadence stamp, so the next run starts them.
    assert fleet.run(**env).returncode == 0
    assert [s.split()[0] for s in starts()] == ["screens", "journal"]


def test_cheap_path_stays_small_when_the_slow_sections_are_skipped(fleet):
    fleet.write(summary())
    env, starts = slow_helpers(fleet, sleep="60")  # a loaded host's slow helper
    state = fleet.tmp / "state"
    state.mkdir()
    for name in ("screens", "journal"):
        (state / f"cadence-{name}").write_text(f"{NOW}\n")
    began = time.monotonic()
    result = fleet.run(**env)
    elapsed = time.monotonic() - began
    assert result.returncode == 0, result.stderr
    assert starts() == []
    assert elapsed < 20, f"cheap path took {elapsed:.1f}s"
    total_ms = int(timings(result).split("total=")[1].split()[0])
    assert total_ms < 20_000
    assert fleet.metrics()["fleet_publisher_last_success_timestamp_seconds"] == NOW


def test_every_section_is_timed_on_the_timings_line(fleet):
    fleet.write(summary())
    line = timings(fleet.run())
    for section in (
        "homes",
        "status_page",
        "publish",
        "daily",
        "quota",
        "taken",
        "screens",
        "journal",
    ):
        assert f" {section}=" in line


# --- the run owns its working directory ----------------------------------------

SWEEPS_TMP = (
    'rm -rf "$SWEEP_DIR"/tmp.* "$SWEEP_DIR"/.[!.]* 2>/dev/null\n'
    'case "$1 $2" in "binding lavish-bound"|"binding lavish-wrong") echo "(any)";; *) exit 1;; esac'
)


def test_a_tmp_cleaner_cannot_take_the_working_directory_mid_run(fleet):
    # The failure seen on a busy host: something cleared /tmp/tmp.* while the run
    # was between two steps, and the next write into it died on "No such file".
    scratch = fleet.tmp / "scratch"
    scratch.mkdir()
    hold = stub(fleet.tmp / "hold.sh", SWEEPS_TMP)
    tasks = stub(fleet.tmp / "tasks.sh", TASKS_AXI_ONLY_FROM_FM_HOME)
    bound_board_fleet(fleet)
    result = fleet.run(
        CAPTAIN_HOLD=hold,
        TASKS_AXI=tasks,
        FM_HOME=str(fleet.tmp),
        EXPECTED_PWD=str(fleet.tmp),
        TMPDIR=str(scratch),
        SWEEP_DIR=str(scratch),
    )
    assert result.returncode == 0, result.stderr
    assert "bound.tsv" not in result.stderr
    urls = {d["id"]: d["board_url"] for d in fleet.published()["decisions_open"]}
    assert urls == {
        "held-call": "http://board/bound",
        "other-call": "http://board/bound",
    }
    # Nothing was made in the shared temp dir, and the run's own dir is gone.
    assert list(scratch.iterdir()) == []
    assert not [p for p in fleet.out.iterdir() if p.name.startswith(".")]


def test_a_working_directory_a_killed_run_left_behind_is_removed_after_a_day(fleet):
    fleet.write(summary())
    old, fresh = fleet.out / ".work.old123", fleet.out / ".work.new456"
    for d in (old, fresh):
        d.mkdir()
        (d / "bound.tsv").write_text("x")
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    assert fleet.run().returncode == 0
    assert not old.exists()
    assert fresh.exists()  # could belong to a run still going


# --- a failed run still moves the stamp ----------------------------------------


def write_a_summary_whose_publish_fails(fleet, **overrides):
    """The home's own file is fine, but the board address it joins in looks like a
    credential, so the run refuses the merged summary (exit 3) after reading
    everything and before it replaces fleet.prom."""
    data = fleet.tmp / "data"
    fleet.write(
        summary(decisions_open=[decision("d1")], **overrides),
        lavish=[session(f"{data}/d1/board.html", "http://board/ghp_" + "a" * 24)],
    )


def test_a_failed_run_does_not_leave_the_previous_stamp_to_age(fleet):
    fleet.write(summary(generated_epoch=NOW - 3000))
    assert fleet.run(FLEET_NOW=str(NOW - 600)).returncode == 0
    assert fleet.metrics()["fleet_summary_generated_timestamp_seconds"] == NOW - 3000
    # The home refreshes its own summary; the next publish then fails.
    write_a_summary_whose_publish_fails(fleet, generated_epoch=NOW - 20)
    result = fleet.run()
    assert result.returncode == 3
    assert "freshness gauges were refreshed" in result.stderr
    m = fleet.metrics()
    home = fleet.tmp.name
    assert m["fleet_summary_generated_timestamp_seconds"] == NOW - 20
    assert (
        m[f'fleet_home_summary_generated_timestamp_seconds{{home="{home}"}}']
        == NOW - 20
    )
    assert m["fleet_summary_valid"] == 1
    # Everything else is the last good run's, including its success time.
    assert m["fleet_publisher_last_success_timestamp_seconds"] == NOW - 600
    assert m['fleet_workers{state="working"}'] == 2
    assert not [p for p in fleet.prom.iterdir() if p.name != "fleet.prom"]


def test_a_failed_run_reports_an_invalid_home_invalid_not_fresh(fleet):
    fleet.write(summary())
    assert fleet.run(FLEET_NOW=str(NOW - 600)).returncode == 0
    write_a_summary_whose_publish_fails(
        fleet, generated_epoch=NOW - 20, valid=False, reason="stuck"
    )
    assert fleet.run().returncode == 3
    m = fleet.metrics()
    assert m["fleet_summary_valid"] == 0
    assert m["fleet_summary_generated_timestamp_seconds"] == NOW - 20


def test_a_failed_run_with_no_readable_home_leaves_fleet_prom_alone(fleet):
    fleet.write(summary())
    assert fleet.run().returncode == 0
    before = (fleet.prom / "fleet.prom").read_text()
    fleet.write("not json")
    assert fleet.run(FLEET_NOW=str(NOW + 600)).returncode != 0
    assert (fleet.prom / "fleet.prom").read_text() == before
