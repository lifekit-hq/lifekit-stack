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
    return {"v": 1, "ts": ts, "event": "task.status", "task": task, "state": state, "key": key, "text": text}


@pytest.fixture
def fleet(tmp_path):
    out, prom = tmp_path / "out", tmp_path / "prom"
    out.mkdir()
    prom.mkdir()
    (tmp_path / "data").mkdir()

    def write(source=None, ledger=(), lavish=None):
        src = tmp_path / "home-summary.json"
        src.write_text(source if isinstance(source, str) else json.dumps(source or summary()))
        (tmp_path / "ledger.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ledger))
        if lavish is not None:
            (tmp_path / "lavish.json").write_text(json.dumps({"sessions": dict(enumerate(lavish))}))

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

    return type("Fleet", (), {"write": staticmethod(write), "run": staticmethod(run), "metrics": staticmethod(metrics), "published": staticmethod(published), "out": out, "prom": prom, "tmp": tmp_path})


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
        summary(decisions_open=[decision("task-a"), decision("task-b"), decision("task-c"), decision("task")]),
        lavish=[
            session(f"{data}/task-a/old.html", "http://board/old", updated="2026-10-01T00:00:00Z"),
            session(f"{data}/task-a/plan.html", "http://board/new", updated="2026-10-02T00:00:00Z"),
            session(f"{data}/task-a/ended.html", "http://board/ended", status="ended", updated="2026-10-03T00:00:00Z"),
            session(f"{data}/task-b/plan.html", "http://board/b", status="ended"),
            session(f"{data}/task-c-extra/plan.html", "http://board/other"),
        ],
    )
    assert fleet.run().returncode == 0
    urls = {d["id"]: d["board_url"] for d in fleet.published()["decisions_open"]}
    assert urls == {"task-a": "http://board/new", "task-b": None, "task-c": None, "task": None}


@pytest.mark.parametrize("lavish", [None, "not json"])
def test_missing_or_broken_lavish_state_still_publishes(fleet, lavish):
    fleet.write(summary(decisions_open=[decision("x")]))
    if lavish:
        (fleet.tmp / "lavish.json").write_text(lavish)
    assert fleet.run().returncode == 0
    assert fleet.published()["decisions_open"][0]["board_url"] is None


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
        ledger=[event(NOW - 600, "a", "needs-decision", "k1"), event(NOW - 1000, "b", "needs-decision", "k2")],
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
            {"v": 1, "ts": NOW - 5, "event": "task.dispatched", "task": "t", "text": "usage limit"},
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
    ["{not json", json.dumps({"schema": "other", "generated_epoch": 1}), json.dumps({"schema": "fm-secondmate-home-summary.v1"})],
    ids=["malformed", "wrong-schema", "no-epoch"],
)
def test_bad_source_keeps_previous_files(fleet, source):
    fleet.write(summary())
    assert fleet.run().returncode == 0
    before = ((fleet.out / "home-summary.json").read_text(), (fleet.prom / "fleet.prom").read_text())
    fleet.write(source)
    assert fleet.run(FLEET_NOW=str(NOW + 60)).returncode != 0
    assert ((fleet.out / "home-summary.json").read_text(), (fleet.prom / "fleet.prom").read_text()) == before


def test_missing_source_keeps_previous_files(fleet):
    fleet.write(summary())
    fleet.run()
    (fleet.tmp / "home-summary.json").unlink()
    assert fleet.run().returncode != 0
    assert fleet.published()["generated_epoch"] == NOW - 30


@pytest.mark.parametrize(
    "secret",
    ["sk-" + "ant-" + "a" * 20, "ghp_" + "A" * 24, "xoxb-" + "1" * 12, "-----BEGIN " + "OPENSSH PRIVATE KEY-----", "tskey-" + "auth-abc123"],
)
def test_summary_carrying_a_credential_is_not_published(fleet, secret):
    fleet.write(summary(decisions_open=[decision("x", reason=f"leaked {secret}")]))
    result = fleet.run()
    assert result.returncode == 3
    assert not (fleet.out / "home-summary.json").exists()
    assert secret not in result.stderr


def test_ordinary_token_talk_is_not_a_credential(fleet):
    fleet.write(summary(decisions_open=[decision("x", summary="rotate the setup-token and the api key")]))
    assert fleet.run().returncode == 0


def test_needs_a_fleet_home(fleet):
    result = subprocess.run(
        ["bash", str(SCRIPT), str(fleet.out), str(fleet.prom)],
        env={k: v for k, v in os.environ.items() if k not in ("FM_HOME", "SUMMARY_FILE")},
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
    assert service["Service"]["ExecStart"].startswith("/usr/local/bin/lifekit-fleet-publisher.sh ")
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
