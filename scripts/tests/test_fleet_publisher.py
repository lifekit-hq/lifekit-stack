"""Behaviour of scripts/fleet-publisher/fleet-publisher.sh against fixture state."""

from __future__ import annotations

import configparser
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/fleet-publisher"
SCRIPT = DIR / "fleet-publisher.sh"
NOW = 1_800_000_000


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

    def run(**env):
        return subprocess.run(
            ["bash", str(SCRIPT), str(out), str(prom)],
            env={
                **os.environ,
                "SUMMARY_FILE": str(tmp_path / "home-summary.json"),
                "LEDGER_FILE": str(tmp_path / "ledger.jsonl"),
                "DATA_DIR": str(tmp_path / "data"),
                "LAVISH_STATE": str(tmp_path / "lavish.json"),
                "FLEET_NOW": str(NOW),
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
    assert [{k: c[k] for k in ("id", "state")} for c in got["active_children"]] == [
        {"id": c["id"], "state": c["state"]} for c in summary()["active_children"]
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
    assert not [p for p in fleet.out.iterdir() if p.name != "home-summary.json"]
    assert not [p for p in fleet.prom.iterdir() if p.name != "fleet.prom"]


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
    urls = {d["id"]: d["board_url"] for d in fleet.published()["decisions_parked"]}
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
    assert fleet.published()["decisions_parked"][0]["board_url"] is None


def stub(path, body):
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return str(path)


def test_board_url_follows_the_captain_hold_binding_not_the_directory(fleet):
    data = fleet.tmp / "data"
    hold = stub(
        fleet.tmp / "hold.sh",
        'case "$1 $2" in "binding lavish-bound"|"binding lavish-wrong") echo "(any)";;'
        " *) exit 1;; esac",
    )
    tasks = stub(
        fleet.tmp / "tasks.sh",
        'echo "body: \\"Captain hold origin: scout-origin\\n\\nRouted by x\\""',
    )
    fleet.write(
        summary(decisions_open=[decision("held-call"), decision("other-call")]),
        lavish=[
            session(f"{data}/scout-origin/board.html", "http://board/bound"),
            session(f"{data}/elsewhere/board.html", "http://board/wrong"),
            session(f"{data}/scout-origin/unbound.html", "http://board/unbound"),
        ],
    )
    # The session key is the last url segment: only "bound" and "wrong" are bound.
    assert fleet.run(CAPTAIN_HOLD=hold, TASKS_AXI=tasks).returncode == 0
    urls = {d["id"]: d["board_url"] for d in fleet.published()["decisions_open"]}
    # Neither decision has an ask record, so both are parked: read them there.
    parked = {d["id"]: d["board_url"] for d in fleet.published()["decisions_parked"]}
    assert urls == {}
    assert parked == {
        "held-call": "http://board/bound",
        "other-call": "http://board/bound",
    }


def test_board_url_falls_back_to_the_directory_when_nothing_is_bound(fleet):
    data = fleet.tmp / "data"
    hold = stub(fleet.tmp / "hold.sh", "exit 1")
    fleet.write(
        summary(decisions_open=[decision("task-a")]),
        lavish=[session(f"{data}/task-a/plan.html", "http://board/dir")],
    )
    assert fleet.run(CAPTAIN_HOLD=hold).returncode == 0
    assert fleet.published()["decisions_parked"][0]["board_url"] == "http://board/dir"


def write_ask(fleet, id_, **record):
    d = fleet.tmp / "state" / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{id_}.json").write_text(json.dumps(record))


def test_ask_is_the_contract_shape_when_a_structured_record_exists(fleet):
    data = fleet.tmp / "data"
    write_ask(
        fleet,
        "pick-layout",
        ask="Which layout should ship?",
        options=[{"label": "Feed"}, {"label": "Cockpit", "recommended": True}, "Tabs"],
        free_text=True,
    )
    fleet.write(
        summary(
            decisions_open=[
                decision(
                    "pick-layout",
                    summary="lifekit-dashboard#9: Fleet layout",
                    hold_bucket="live",
                    reason="waits on the captain's word",
                )
            ]
        ),
        lavish=[session(f"{data}/pick-layout/board.html", "http://board/pick")],
    )
    assert fleet.run(STATE_DIR=str(fleet.tmp / "state")).returncode == 0
    got = fleet.published()
    (row,) = got["decisions_open"]
    assert row["ask"] == {
        "question": "Which layout should ship?",
        "options": [
            {"id": "2", "label": "Cockpit", "recommended": True},
            {"id": "1", "label": "Feed", "recommended": False},
            {"id": "3", "label": "Tabs", "recommended": False},
        ],
        "free_text_allowed": True,
        "link": "http://board/pick",
    }
    assert row["restart"]["kind"] == "captain_word"
    assert row["project"] == "lifekit-dashboard"
    assert got["decisions_parked"] == []


def test_no_structured_ask_means_no_ask(fleet):
    fleet.write(
        summary(
            decisions_open=[
                decision(
                    "parked-one",
                    reason="waits on the captain's word, parked",
                    hold_bucket="live",
                ),
                # A record exists but the hold is dated, so it is not a live ask.
                decision("dated-one", hold_until="2099-01-01", hold_bucket="dated"),
            ]
        )
    )
    write_ask(fleet, "dated-one", ask="Later?", options=["Yes"])
    assert fleet.run(STATE_DIR=str(fleet.tmp / "state")).returncode == 0
    got = fleet.published()
    assert got["decisions_open"] == []
    assert {d["id"] for d in got["decisions_parked"]} == {"parked-one", "dated-one"}
    assert all("ask" not in d for d in got["decisions_parked"])
    assert got["decisions_total"] == 2
    # Metrics keep counting the source's open decisions.
    assert fleet.metrics()["fleet_decisions_open"] == 2


def test_ask_link_is_the_decision_page_without_a_board(fleet):
    write_ask(fleet, "no-board", ask="Ship it?", options=[{"id": "go", "label": "Go"}])
    fleet.write(summary(decisions_open=[decision("no-board", hold_bucket="live")]))
    assert fleet.run(STATE_DIR=str(fleet.tmp / "state")).returncode == 0
    (row,) = fleet.published()["decisions_open"]
    assert row["ask"]["link"] == "/decisions/no-board"
    assert row["ask"]["options"] == [{"id": "go", "label": "Go", "recommended": False}]
    assert row["ask"]["free_text_allowed"] is False


@pytest.mark.parametrize(
    ("raw", "plain"),
    [
        (
            "chore(design): adopt impeccable project-scoped",
            "adopt impeccable project-scoped",
        ),
        ("fix(tokens)!: one font contract", "one font contract"),
        ("Scout: mobile foundation end to end", "mobile foundation end to end"),
        ("finance-sentry#574: Dashboard time filtering", "Dashboard time filtering"),
        ("feat(ui): finance-sentry#12: nested prefix", "nested prefix"),
        ("Working: the status word", "the status word"),
        # Cut at 70 characters by the snapshot: drop the partial word.
        (
            "chore(design): adopt impeccable project-scoped (impeccable plan step 1…",
            "adopt impeccable project-scoped (impeccable plan step…",
        ),
        ("a title that ends on a whole word …", "a title that ends on a whole word…"),
        ("Plain title", "Plain title"),
        ("", ""),
    ],
)
def test_title_plain_drops_prefixes_ids_and_cut_words(fleet, raw, plain):
    fleet.write(
        summary(
            active_children=[{"id": "a", "state": "working", "name": raw}],
            landed=[{"id": "l", "title": raw}],
            holds=[{"id": "h", "title": raw, "reason": "x"}],
        )
    )
    assert fleet.run().returncode == 0
    got = fleet.published()
    assert got["active_children"][0]["title_plain"] == plain
    assert got["landed"][0]["title_plain"] == plain
    assert got["holds"][0]["title_plain"] == plain


def test_children_carry_since_produces_open_url_and_project(fleet):
    state = fleet.tmp / "state"
    state.mkdir()
    data = fleet.tmp / "data"
    for id_ in ("shipper", "scouter", "issuer", "plain"):
        (state / f"{id_}.meta").write_text("x")
        os.utime(state / f"{id_}.meta", (NOW - 3600, NOW - 3600))
    (state / "shipper.status").write_text(
        "working: first https://github.com/acme/widgets/pull/1\n"
        "done: https://github.com/acme/widgets/pull/2 checks green\n"
    )
    fleet.write(
        summary(
            active_children=[
                {
                    "id": "shipper",
                    "state": "working",
                    "kind": "ship",
                    "name": "feat(x): a",
                    "repo": "widgets",
                },
                {
                    "id": "scouter",
                    "state": "working",
                    "kind": "scout",
                    "name": "Scout: b",
                },
                {
                    "id": "issuer",
                    "state": "working",
                    "kind": "ship",
                    "name": "widgets#7: c",
                },
                {"id": "plain", "state": "working", "kind": "ops", "name": "d"},
                {"id": "nometa", "state": "working", "kind": "ship", "name": "e"},
            ],
            landed=[{"id": "old", "pr_url": "https://github.com/acme/widgets/pull/3"}],
        ),
        lavish=[session(f"{data}/scouter/board.html", "http://board/scout")],
    )
    assert fleet.run(STATE_DIR=str(state)).returncode == 0
    kids = {c["id"]: c for c in fleet.published()["active_children"]}
    assert kids["shipper"]["since_epoch"] == NOW - 3600
    assert kids["shipper"]["produces"] == "pr"
    assert kids["shipper"]["open_url"] == "https://github.com/acme/widgets/pull/2"
    assert kids["shipper"]["project"] == "widgets"
    assert kids["scouter"]["produces"] == "report"
    assert kids["scouter"]["open_url"] == "http://board/scout"
    assert kids["issuer"]["open_url"] == "https://github.com/acme/widgets/issues/7"
    assert kids["issuer"]["project"] == "widgets"
    assert kids["plain"]["produces"] == "landing"
    assert kids["plain"]["open_url"] is None
    assert kids["nometa"]["since_epoch"] is None
    assert kids["nometa"]["project"] is None


def test_landed_items_get_since_produces_open_url_and_project(fleet):
    fleet.write(
        summary(
            landed=[
                {
                    "id": "merged",
                    "title": "widgets#3: x",
                    "pr_url": "https://github.com/acme/widgets/pull/3",
                    "completion": {"date": "2026-10-05"},
                },
                {
                    "id": "nothing",
                    "title": "ops thing",
                    "pr_url": None,
                    "report_path": None,
                    "completion": {},
                },
            ]
        )
    )
    assert fleet.run().returncode == 0
    merged, nothing = fleet.published()["landed"]
    assert merged["since_epoch"] == 1791158400
    assert merged["produces"] == "pr"
    assert merged["open_url"] == "https://github.com/acme/widgets/pull/3"
    assert merged["project"] == "widgets"
    assert (nothing["since_epoch"], nothing["produces"], nothing["open_url"]) == (
        None,
        "landing",
        None,
    )
    assert nothing["project"] is None


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
    assert item["open_url"] == item["report_url"]
    copy = fleet.out / "reports/scout-a/report.md"
    assert copy.read_text() == "# Report A\n"
    assert copy.stat().st_mode & 0o777 == 0o644
    assert (fleet.out / "reports").stat().st_mode & 0o777 == 0o755
    # Once no landed item names a report the stale copy goes away.
    fleet.write(summary())
    assert fleet.run().returncode == 0
    assert not (fleet.out / "reports").exists()
    assert not [p for p in fleet.out.iterdir() if p.name != "home-summary.json"]


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


def test_holds_get_restart_kind_project_and_full_total(fleet):
    fleet.write(
        summary(
            counts={"holds": 34},
            holds=[
                {
                    "id": "blocked",
                    "title": "x",
                    "reason": "waits on the domain",
                    "unresolved_blocker_ids": ["dep-1", "dep-2"],
                },
                {
                    "id": "props",
                    "title": "lifekit-stack#5: y",
                    "reason": "parked until his new proposals are evaluated",
                },
                {"id": "word", "title": "z", "reason": "waits on the captain's word"},
                {
                    "id": "dated",
                    "title": "w",
                    "reason": "restart 2026-10-07, else at the next audit",
                },
                {"id": "event", "title": "v", "reason": "after the next memory-audit"},
            ],
        )
    )
    assert fleet.run().returncode == 0
    got = fleet.published()
    assert got["holds_total"] == 34
    restart = {h["id"]: h["restart"] for h in got["holds"]}
    assert restart["blocked"] == {
        "kind": "after_work",
        "blocker_ids": ["dep-1", "dep-2"],
        "text": "waits on the domain",
    }
    assert restart["props"]["kind"] == "proposals"
    assert restart["word"]["kind"] == "captain_word"
    assert restart["dated"] == {
        "kind": "date",
        "until": "2026-10-07",
        "text": "restart 2026-10-07, else at the next audit",
    }
    assert restart["event"] == {"kind": "event", "text": "after the next memory-audit"}
    assert {h["id"]: h["project"] for h in got["holds"]}["props"] == "lifekit-stack"


def test_holds_total_defaults_to_the_list_length_and_output_is_versioned(fleet):
    fleet.write(summary(holds=[{"id": "h", "title": "t", "reason": "r"}]))
    assert fleet.run().returncode == 0
    got = fleet.published()
    assert got["holds_total"] == 1
    assert got["publisher_schema"] == "lifekit-fleet-publisher.v1"
    # The source summary's own fields still pass through.
    assert got["schema"] == "fm-secondmate-home-summary.v1"
    assert got["valid"] is True


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
            "FM_HOME": "/srv/fleet-home",
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
    assert service["Service"]["Environment"] == "FM_HOME=/srv/fleet-home"
    assert service["Service"]["Type"] == "oneshot"
    assert "__" not in (units / "lifekit-fleet-publisher.service").read_text()

    timer = configparser.ConfigParser(interpolation=None)
    timer.read_string((units / "lifekit-fleet-publisher.timer").read_text())
    assert timer["Timer"]["OnUnitActiveSec"] == "1min"

    script = root / "usr/local/bin/lifekit-fleet-publisher.sh"
    assert os.access(script, os.X_OK)
    assert script.read_text() == SCRIPT.read_text()
    assert service["Service"]["ExecStart"].startswith(
        "/usr/local/bin/lifekit-fleet-publisher.sh "
    )
    assert (root / "var/lib/lifekit-fleet").is_dir()
    assert (root / "var/lib/node_exporter/textfile").is_dir()
    assert calls.read_text().splitlines() == [
        "daemon-reload",
        "enable --now lifekit-fleet-publisher.timer",
    ]


def test_installer_skips_without_a_fleet_home():
    result = subprocess.run(
        ["bash", str(DIR / "install-fleet-publisher.sh")],
        env={k: v for k, v in os.environ.items() if k != "FM_HOME"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "skipping" in result.stderr
