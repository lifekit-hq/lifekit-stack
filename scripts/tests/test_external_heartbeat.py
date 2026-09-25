"""The external heartbeat is wired only when its URL variable is set."""

from __future__ import annotations

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
    assert "routes" not in doc["policies"][0]
    assert "__HEARTBEAT" not in (out / "policies.yml").read_text()


def test_set_wires_rule_contact_point_and_route(tmp_path):
    url = "https://example.invalid/ping/secret-token"
    out = render(tmp_path, url)
    hb = yaml.safe_load((out / "heartbeat.yml").read_text())
    (receiver,) = hb["contactPoints"][0]["receivers"]
    assert receiver["settings"]["url"] == "$HEARTBEAT_URL"
    (rule,) = hb["groups"][0]["rules"]
    assert rule["labels"] == {"heartbeat": "external"}
    (route,) = yaml.safe_load((out / "policies.yml").read_text())["policies"][0][
        "routes"
    ]
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
