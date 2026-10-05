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

    def run(cwd=None, **env):
        return subprocess.run(
            ["bash", str(SCRIPT), str(out), str(prom)],
            cwd=cwd,
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
    assert got["active_children"] == summary()["active_children"]
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
        PATH="/usr/bin:/bin",
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
        == '"FM_HOME=/srv/fleet-home" "TASKS_AXI=/opt/tools/bin/tasks-axi"'
    )
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
