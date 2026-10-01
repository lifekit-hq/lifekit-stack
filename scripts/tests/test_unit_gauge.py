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
RULE = {r["uid"]: r for g in RULES["groups"] for r in g["rules"]}["host-unit-down-or-failed"]

FAKE = """#!/usr/bin/env bash
# Fake systemctl: STATE_DIR/<unit> holds "LoadState ActiveState Result";
# STATE_DIR/_files lists "<unit> enabled" rows for list-unit-files.
case "$1" in
  list-unit-files)
    glob="${@: -1}"
    while read -r unit rest; do
      # shellcheck disable=SC2254
      case "$unit" in $glob) echo "$unit $rest" ;; esac
    done < "$STATE_DIR/_files"
    ;;
  show)
    unit="${@: -1}"
    if [[ -f "$STATE_DIR/$unit" ]]; then
      read -r load active result < "$STATE_DIR/$unit"
    else
      load=not-found active=inactive result=success
    fi
    echo "LoadState=$load"; echo "ActiveState=$active"; echo "Result=$result"
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

    def unit(name, active="active", result="success", load="loaded", listed=False):
        (state / name).write_text(f"{load} {active} {result}\n")
        if listed:
            files.append(f"{name} enabled")
        (state / "_files").write_text("\n".join(files) + "\n")

    (state / "_files").write_text("")

    def run():
        proc = subprocess.run(
            ["bash", str(SCRIPT), str(out)],
            env={**os.environ, "SYSTEMCTL": str(fake), "STATE_DIR": str(state)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        parsed = {}
        for line in (out / "host_unit.prom").read_text().splitlines():
            if line and not line.startswith("#"):
                name, value = line.rsplit(" ", 1)
                parsed[name] = int(value)
        return parsed

    return unit, run


def test_allowlist_and_runners_reported_others_ignored(host):
    unit, run = host
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
    _, run = host
    assert run() == {}


def test_lifekit_timer_uses_timer_activity_and_service_result(host):
    unit, run = host
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
    unit, run = host
    unit("cron.service", result="signal")
    assert run()['host_unit_failed{unit="cron"}'] == 1


def test_alert_rule_reads_both_gauges_and_fires_above_zero():
    (expr,) = [
        d["model"]["expr"] for d in RULE["data"] if d["datasourceUid"] == "prometheus"
    ]
    assert "host_unit_failed" in expr and "host_unit_active" in expr
    (cond,) = [d for d in RULE["data"] if d["model"]["refId"] == RULE["condition"]]
    assert cond["model"]["conditions"][0]["evaluator"] == {"type": "gt", "params": [0]}
    assert RULE["for"] == "10m"
    assert RULE["labels"]["severity"] == "warning"
    assert "{{ $labels.unit }}" in RULE["annotations"]["summary"]


def test_installer_and_bootstrap_wire_the_gauge():
    installer = (REPO / "scripts/host-gauge/install-host-gauges.sh").read_text()
    assert "unit-gauge/unit-gauge" in installer
    assert "unit-gauge.timer" in installer
    for name in ("unit-gauge.sh", "unit-gauge.service", "unit-gauge.timer"):
        assert (REPO / "scripts/unit-gauge" / name).is_file()
