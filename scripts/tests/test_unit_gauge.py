"""Behaviour of scripts/unit-gauge/unit-gauge.sh against a fake systemctl."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/unit-gauge/unit-gauge.sh"
RULES = yaml.safe_load(
    (REPO / "compose/observability/grafana/provisioning/alerting/rules.yml").read_text()
)
RULE = {r["uid"]: r for g in RULES["groups"] for r in g["rules"]}[
    "host-unit-down-or-failed"
]

FAKE = """#!/usr/bin/env bash
# Fake systemctl: STATE_DIR/<unit> holds "LoadState ActiveState Result
# UnitFileState"; STATE_DIR/_files lists "<unit> enabled" rows for
# list-unit-files; STATE_DIR/_broken makes it fail like a dead bus.
if [[ -f "$STATE_DIR/_broken" ]]; then
  echo "Failed to connect to system scope bus" >&2
  exit 1
fi
case "$1" in
  list-unit-files)
    glob="${@: -1}"
    found=1
    while read -r unit rest; do
      # shellcheck disable=SC2254
      case "$unit" in $glob) echo "$unit $rest"; found=0 ;; esac
    done < "$STATE_DIR/_files"
    exit "$found"
    ;;
  show)
    unit="${@: -1}"
    if [[ -f "$STATE_DIR/$unit" ]]; then
      read -r load active result filestate < "$STATE_DIR/$unit"
    else
      load=not-found active=inactive result=success filestate=
    fi
    echo "LoadState=$load"; echo "ActiveState=$active"; echo "Result=$result"
    echo "UnitFileState=$filestate"
    ;;
esac
"""


@pytest.fixture
def host(tmp_path):
    state, out = tmp_path / "state", tmp_path / "out"
    state.mkdir()
    out.mkdir()
    fake = tmp_path / "systemctl"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    files = []

    def unit(
        name,
        active="active",
        result="success",
        load="loaded",
        enabled="enabled",
        listed=False,
    ):
        (state / name).write_text(f"{load} {active} {result} {enabled}\n")
        if listed:
            files.append(f"{name} enabled")
        (state / "_files").write_text("\n".join(files) + "\n")

    (state / "_files").write_text("")

    def run(expect=0):
        proc = subprocess.run(
            ["bash", str(SCRIPT), str(out)],
            env={**os.environ, "SYSTEMCTL": str(fake), "STATE_DIR": str(state)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == expect, proc.stderr
        if expect:
            return proc
        parsed = {}
        for line in (out / "host_unit.prom").read_text().splitlines():
            if line and not line.startswith("#"):
                name, value = line.rsplit(" ", 1)
                parsed[name] = int(value)
        return parsed

    return unit, run, state, out


def test_allowlist_and_runners_reported_others_ignored(host):
    unit, run, *_ = host
    unit("tailscaled.service")
    unit("docker.service", active="failed", result="exit-code")
    unit("actions.runner.o-r.host.service", listed=True)
    unit("sshd.service")  # not in the allowlist
    unit("actions.runnerless.service")  # no runner glob match
    got = run()
    assert got == {
        'host_unit_active{unit="tailscaled"}': 1,
        'host_unit_failed{unit="tailscaled"}': 0,
        'host_unit_active{unit="docker"}': 0,
        'host_unit_failed{unit="docker"}': 1,
        'host_unit_active{unit="actions.runner.o-r.host"}': 1,
        'host_unit_failed{unit="actions.runner.o-r.host"}': 0,
    }


def test_missing_units_are_skipped_not_down(host):
    _, run, *_ = host
    assert run() == {}


def test_lifekit_timer_uses_timer_activity_and_service_result(host):
    unit, run, *_ = host
    unit("lifekit-a.timer", listed=True)
    unit("lifekit-a.service", active="inactive")  # idle oneshot is healthy
    unit("lifekit-b.timer", listed=True)
    unit("lifekit-b.service", active="failed", result="exit-code")
    unit("lifekit-c.timer", active="inactive", listed=True)
    unit("lifekit-c.service", active="inactive")
    unit("lifekit-d.timer", listed=True)  # service unit missing
    got = run()
    assert got['host_unit_active{unit="lifekit-a"}'] == 1
    assert got['host_unit_failed{unit="lifekit-a"}'] == 0
    assert got['host_unit_failed{unit="lifekit-b"}'] == 1
    assert got['host_unit_active{unit="lifekit-c"}'] == 0
    assert got['host_unit_failed{unit="lifekit-d"}'] == 1


def test_result_other_than_success_counts_as_failed_even_when_active(host):
    unit, run, *_ = host
    unit("cron.service", result="signal")
    assert run()['host_unit_failed{unit="cron"}'] == 1


def test_disabled_inactive_unit_is_skipped_but_failed_or_running_one_is_reported(host):
    unit, run, *_ = host
    unit("cron.service", active="inactive", enabled="disabled")
    unit("docker.service", active="failed", result="exit-code", enabled="disabled")
    unit("containerd.service", active="active", enabled="disabled")
    unit("tailscaled.service", active="inactive", enabled="enabled")
    assert run() == {
        'host_unit_active{unit="docker"}': 0,
        'host_unit_failed{unit="docker"}': 1,
        'host_unit_active{unit="containerd"}': 1,
        'host_unit_failed{unit="containerd"}': 0,
        'host_unit_active{unit="tailscaled"}': 0,
        'host_unit_failed{unit="tailscaled"}': 0,
    }


def test_disabled_inactive_timer_is_skipped(host):
    unit, run, *_ = host
    unit("lifekit-a.timer", active="inactive", enabled="disabled", listed=True)
    unit("lifekit-a.service", active="inactive")
    assert run() == {}


def test_systemctl_failure_exits_nonzero_and_keeps_previous_file(host):
    unit, run, state, out = host
    unit("tailscaled.service")
    run()
    prom = out / "host_unit.prom"
    before = prom.read_text()
    (state / "_broken").write_text("")
    proc = run(expect=1)
    assert "systemctl" in proc.stderr
    assert prom.read_text() == before
    assert [p.name for p in out.iterdir()] == ["host_unit.prom"]


@pytest.mark.parametrize(
    ("active", "failed", "fires"),
    [(1, 0, False), (0, 0, True), (1, 1, True), (0, 1, True)],
)
def test_alert_rule_fires_when_a_unit_is_down_or_failed(active, failed, fires):
    (expr,) = [
        d["model"]["expr"] for d in RULE["data"] if d["datasourceUid"] == "prometheus"
    ]
    value = eval(
        expr,
        {"__builtins__": {}},
        {"host_unit_active": active, "host_unit_failed": failed},
    )
    (cond,) = [d for d in RULE["data"] if d["model"]["refId"] == RULE["condition"]]
    evaluator = cond["model"]["conditions"][0]["evaluator"]
    assert evaluator["type"] == "gt"
    assert (value > evaluator["params"][0]) is fires
    assert RULE["for"] == "10m"
    assert RULE["labels"]["severity"] == "warning"
