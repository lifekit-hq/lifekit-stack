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


def test_box_dashboard_covers_rows():
    d = json.loads((PROV / "dashboards/lifekit/box.json").read_text())
    assert d["uid"] == "lifekit-box"
    assert sum(p["type"] == "row" for p in d["panels"]) == 7
    assert any(p["type"] == "alertlist" for p in d["panels"])
    exprs = " ".join(t["expr"] for p in d["panels"] for t in p.get("targets", []))
    for metric in (
        "fleet_workers",
        "fleet_decisions_open",
        "fleet_oldest_decision_age_seconds",
        "fleet_usage_limit_events_1h",
        "fleet_summary_generated_timestamp_seconds",
    ):
        assert metric in exprs


def test_node_exporter_collectors():
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((REPO / "compose/docker-compose.yml").read_text())
    command = compose["services"]["node-exporter"]["command"]
    for flag in (
        "--collector.disable-defaults",
        "--collector.filesystem",
        "--collector.meminfo",
        "--collector.loadavg",
    ):
        assert flag in command


def test_box_dashboard_has_runway_and_share_panels():
    d = json.loads((PROV / "dashboards/lifekit/box.json").read_text())
    exprs = {t["expr"]: p["title"] for p in d["panels"] for t in p.get("targets", [])}
    assert exprs["claude_quota_runway_seconds"] == "Quota runway"
    assert exprs["claude_quota_share_percent"] == "Quota share by consumer"
    # no panel overlaps another on the grid
    cells = set()
    for p in d["panels"]:
        g = p["gridPos"]
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                assert (x, y) not in cells, p["title"]
                cells.add((x, y))


def test_runway_status_and_window_are_unitless_and_unthresholded():
    d = json.loads((PROV / "dashboards/lifekit/box.json").read_text())
    (panel,) = [p for p in d["panels"] if p["title"] == "Quota runway"]
    overrides = {
        o["matcher"]["options"]: {p["id"]: p["value"] for p in o["properties"]}
        for o in panel["fieldConfig"]["overrides"]
    }
    assert overrides["A"]["unit"] == "s"
    assert "thresholds" in overrides["A"]
    assert panel["fieldConfig"]["defaults"]["unit"] == "none"
    (step,) = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert step["color"] == "text"


def test_quota_panels_show_only_current_values():
    d = json.loads((PROV / "dashboards/lifekit/box.json").read_text())
    for title in ("Quota runway", "Quota share by consumer"):
        (panel,) = [p for p in d["panels"] if p["title"] == title]
        assert all(t["instant"] and not t["range"] for t in panel["targets"]), title


def test_runway_panel_shows_projected_exhaustion_as_instant_datetime():
    d = json.loads((PROV / "dashboards/lifekit/box.json").read_text())
    (panel,) = [p for p in d["panels"] if p["title"] == "Quota runway"]
    (target,) = [
        t
        for t in panel["targets"]
        if t["expr"].startswith("claude_quota_projected_exhausted_at_seconds")
    ]
    assert target["instant"] and not target["range"]
    assert target["expr"].endswith("* 1000")
    (override,) = [
        o
        for o in panel["fieldConfig"]["overrides"]
        if o["matcher"]["options"] == target["refId"]
    ]
    assert {p["id"]: p["value"] for p in override["properties"]}[
        "unit"
    ] == "dateTimeAsIso"


def test_runway_status_and_window_value_mappings_are_not_empty_text():
    # Grafana 11 renders a lone series blank when its value maps to "" - in the
    # no_projection state only the status/window series exist, so they must map to
    # non-empty text to stay visible.
    d = json.loads((PROV / "dashboards/lifekit/box.json").read_text())
    (panel,) = [p for p in d["panels"] if p["title"] == "Quota runway"]
    overrides = {
        o["matcher"]["options"]: {p["id"]: p["value"] for p in o["properties"]}
        for o in panel["fieldConfig"]["overrides"]
    }
    for ref in ("B", "C"):
        for mapping in overrides[ref]["mappings"]:
            for result in mapping["options"].values():
                assert result["text"] != ""
