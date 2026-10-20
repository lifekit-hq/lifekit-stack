"""The external heartbeat is wired only when its URL variable is set."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "compose/observability/grafana/provisioning/alerting"


def render(tmp_path: Path, url: str) -> Path:
    for name in ("policies.yml.tmpl", "heartbeat.yml.tmpl"):
        shutil.copy(SRC / name, tmp_path / name)
    subprocess.run(
        ["bash", str(REPO / "scripts/render-heartbeat.sh"), str(tmp_path), url],
        check=True,
    )
    return tmp_path


def test_unset_disables_cleanly(tmp_path):
    (tmp_path / "heartbeat.yml").write_text("stale")
    out = render(tmp_path, "")
    hb = yaml.safe_load((out / "heartbeat.yml").read_text())
    assert hb["deleteRules"] == [{"orgId": 1, "uid": "external-heartbeat-watchdog"}]
    assert "deleteContactPoints" not in hb
    assert "groups" not in hb and "contactPoints" not in hb
    doc = yaml.safe_load((out / "policies.yml").read_text())
    assert all("heartbeat" not in str(r) for r in doc["policies"][0]["routes"])
    assert "__HEARTBEAT" not in (out / "policies.yml").read_text()


def test_set_wires_rule_contact_point_and_route(tmp_path):
    url = "https://example.invalid/ping/secret-token"
    out = render(tmp_path, url)
    hb = yaml.safe_load((out / "heartbeat.yml").read_text())
    (receiver,) = hb["contactPoints"][0]["receivers"]
    assert receiver["settings"]["url"] == "$HEARTBEAT_URL"
    (rule,) = hb["groups"][0]["rules"]
    assert rule["labels"] == {"heartbeat": "external"}
    routes = yaml.safe_load((out / "policies.yml").read_text())["policies"][0]["routes"]
    route = routes[0]
    assert route["receiver"] == hb["contactPoints"][0]["name"]
    assert route["object_matchers"] == [["heartbeat", "=", "external"]]
    for f in ("heartbeat.yml", "policies.yml"):
        assert url not in (out / f).read_text()


def test_compose_maps_optional_variable_into_grafana():
    compose = yaml.safe_load((REPO / "compose/docker-compose.yml").read_text())
    env = compose["services"]["grafana"]["environment"]
    assert env["HEARTBEAT_URL"] == "${LIFEKIT_EXTERNAL_HEARTBEAT_URL:-}"


def test_root_policy_is_unchanged_when_disabled(tmp_path):
    doc = yaml.safe_load((render(tmp_path, "") / "policies.yml").read_text())
    assert doc["policies"][0]["receiver"] == "telegram-lifekit"


def test_phone_gets_critical_only_and_everything_else_is_muted(tmp_path):
    for url in ("", "https://example.invalid/ping/x"):
        doc = yaml.safe_load((render(tmp_path, url) / "policies.yml").read_text())
        routes = doc["policies"][0]["routes"]
        critical, rest = routes[-2:]
        assert critical["receiver"] == "telegram-lifekit"
        assert critical["object_matchers"] == [["severity", "=", "critical"]]
        assert rest["receiver"] == "telegram-lifekit"
        assert "object_matchers" not in rest
        assert rest["mute_time_intervals"] == ["always"]
        (mute,) = doc["muteTimes"]
        assert mute["name"] == "always"
        assert mute["time_intervals"] == [
            {"times": [{"start_time": "00:00", "end_time": "24:00"}]}
        ]


def test_every_alert_rule_has_a_routable_severity():
    rules = [
        (path.name, rule["uid"], rule.get("labels", {}).get("severity"))
        for path in sorted(SRC.glob("rules*.yml"))
        for group in yaml.safe_load(path.read_text())["groups"]
        for rule in group["rules"]
    ]
    assert rules
    assert [r for r in rules if r[2] not in ("critical", "warning")] == []


def test_rules_the_fleet_inbox_cannot_see_are_critical():
    # poll_alerts.py skips (with a warning) any rule it does not model, such as a
    # Loki rule; with the phone critical-only, such a rule reaches no one unless
    # it is critical.
    spec = importlib.util.spec_from_file_location(
        "poll_alerts", REPO / "scripts/alert-inbox/poll_alerts.py"
    )
    poll_alerts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(poll_alerts)
    for path in sorted(SRC.glob("rules*.yml")):
        doc = yaml.safe_load(path.read_text())
        modelled = {r.uid for r in poll_alerts.parse_rules(doc)[0]}
        unseen = [
            (path.name, rule["uid"], rule["labels"]["severity"])
            for group in doc["groups"]
            for rule in group["rules"]
            if rule["uid"] not in modelled and rule["labels"]["severity"] != "critical"
        ]
        assert unseen == []
