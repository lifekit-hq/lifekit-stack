"""Semantics of the provisioned Grafana alert rules, parsed as YAML.

Docker metric names are checked against what the exporter really emits for a
fake daemon, not against its source text.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
DOC = yaml.safe_load(
    (REPO / "compose/observability/grafana/provisioning/alerting/rules.yml").read_text()
)
RULES = {r["uid"]: r for g in DOC["groups"] for r in g["rules"]}

_SPEC = importlib.util.spec_from_file_location(
    "exporter", REPO / "compose/container-exporter/exporter.py"
)
exporter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(exporter)


def emitted_docker_metrics() -> set[str]:
    inspect = {
        "Id": "x",
        "Name": "/x",
        "RestartCount": 0,
        "State": {
            "Status": "running",
            "Running": True,
            "Restarting": False,
            "OOMKilled": False,
            "ExitCode": 0,
            "StartedAt": "2026-09-13T09:23:03.800309061Z",
            "FinishedAt": "2026-09-13T09:23:05.689913813Z",
            "Health": {"Status": "healthy"},
        },
        "Config": {"Labels": {}},
    }
    stats = {"memory_stats": {"usage": 2, "limit": 3, "stats": {"inactive_file": 1}}}
    samples = exporter.container_samples(inspect, stats)
    samples.append(("docker_exporter_scrape_errors", {}, 0.0))
    return {name for name, _, _ in samples}


def queries(rule: dict) -> list[str]:
    return [
        d["model"]["expr"] for d in rule["data"] if d["datasourceUid"] == "prometheus"
    ]


def threshold(rule: dict) -> tuple[str, float]:
    (cond,) = [d for d in rule["data"] if d["model"]["refId"] == rule["condition"]]
    evaluator = cond["model"]["conditions"][0]["evaluator"]
    return evaluator["type"], evaluator["params"][0]


def test_no_rule_is_paused():
    assert not [uid for uid, r in RULES.items() if r.get("isPaused")]


def test_retired_rule_is_deleted_not_provisioned():
    assert "container-memory-near-limit" not in RULES
    assert "container-memory-near-limit" in {r["uid"] for r in DOC["deleteRules"]}


def test_rules_only_reference_real_metrics():
    exprs = "\n".join(e for r in RULES.values() for e in queries(r))
    assert "openclaw_prometheus_series_dropped_total" not in exprs
    assert "docker_container_memory_limit_bytes" not in exprs
    real = emitted_docker_metrics()
    for uid, rule in RULES.items():
        for expr in queries(rule):
            for name in re.findall(r"\bdocker_[a-z_]+", expr):
                assert name in real, f"{uid}: {name} is not exported"


@pytest.mark.parametrize(
    ("uid", "metric", "severity", "op", "limit"),
    [
        ("container-oom-killed", "docker_container_oom_killed", "critical", "gt", 0),
        ("root-filesystem-readonly", "node_filesystem_readonly", "critical", "gt", 0),
        (
            "textfile-collector-stale",
            "node_textfile_mtime_seconds",
            "warning",
            "gt",
            900,
        ),
        (
            "textfile-collector-scrape-error",
            "node_textfile_scrape_error",
            "warning",
            "gt",
            0,
        ),
    ],
)
def test_new_rules(uid, metric, severity, op, limit):
    rule = RULES[uid]
    assert any(metric in e for e in queries(rule))
    assert rule["labels"]["severity"] == severity
    assert threshold(rule) == (op, limit)


def test_quota_pace_rule_active_and_no_five_hour_rule():
    rule = RULES["claude-quota-behind-pace"]
    assert rule["labels"]["severity"] == "warning"
    assert any("claude_quota_reserve_percent_points" in e for e in queries(rule))
    assert not [
        uid for uid, r in RULES.items() if any("five_hour" in e for e in queries(r))
    ]
