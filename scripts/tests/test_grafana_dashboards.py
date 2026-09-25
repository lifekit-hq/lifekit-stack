"""Provisioned Grafana dashboards: valid JSON, unique stable uids, the
provisioned Prometheus datasource by uid, and node-exporter collectors the
Box dashboard's host panels depend on (no Docker, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROV = REPO / "compose/observability/grafana/provisioning"
DASHBOARDS = sorted((PROV / "dashboards/lifekit").glob("*.json"))


def _datasource_refs(node):
    if isinstance(node, dict):
        if "datasource" in node:
            yield node["datasource"]
        for v in node.values():
            yield from _datasource_refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _datasource_refs(v)


def test_uids_unique_and_present():
    uids = [json.loads(p.read_text())["uid"] for p in DASHBOARDS]
    assert uids and len(uids) == len(set(uids))


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_dashboard_shape(path):
    d = json.loads(path.read_text())
    assert d["title"] and d["panels"]
    ids = [p["id"] for p in d["panels"]]
    assert len(ids) == len(set(ids))
    for ref in _datasource_refs(d):
        assert ref == {"type": "prometheus", "uid": "prometheus"}
    assert "http" not in path.read_text().replace("https://grafana.com", "")


def test_box_dashboard_covers_rows():
    d = json.loads((PROV / "dashboards/lifekit/box.json").read_text())
    assert d["uid"] == "lifekit-box"
    assert sum(p["type"] == "row" for p in d["panels"]) == 6
    assert any(p["type"] == "alertlist" for p in d["panels"])


def test_node_exporter_collectors():
    compose = (REPO / "compose/docker-compose.yml").read_text()
    for flag in (
        "--collector.disable-defaults",
        "--collector.filesystem",
        "--collector.meminfo",
        "--collector.loadavg",
    ):
        assert f"      - {flag}\n" in compose
