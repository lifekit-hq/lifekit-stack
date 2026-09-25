"""Invariants of the provisioned Grafana alert rules (no Docker, no network, no YAML dep)."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RULES = (
    REPO / "compose/observability/grafana/provisioning/alerting/rules.yml"
).read_text()

# Metric names the stack really produces, from the exporters' own sources and
# node-exporter / Grafana / Prometheus built-ins.
EXPORTER = (REPO / "compose/container-exporter/exporter.py").read_text()
DOCKER_METRICS = set(re.findall(r'"(docker_[a-z_]+)"', EXPORTER))


def rule_blocks() -> dict[str, str]:
    parts = re.split(r"^      - uid: ([a-z0-9-]+)\n", RULES, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def exprs(block: str) -> str:
    return "\n".join(
        re.findall(r"^\s+expr: (?:>-\n)?((?:.+\n?)+?)(?=\s+instant:)", block, re.M)
    )


def test_no_rule_is_paused():
    assert "isPaused" not in RULES


def test_dropped_metric_and_retired_rule_gone():
    assert "openclaw_prometheus_series_dropped_total" not in RULES
    assert "container-memory-near-limit" not in rule_blocks()
    assert re.search(r"deleteRules:.*uid: container-memory-near-limit", RULES, re.S)
    assert "docker_container_memory_limit_bytes" not in RULES


def test_docker_metrics_referenced_exist():
    for uid, block in rule_blocks().items():
        for name in re.findall(r"\bdocker_[a-z_]+", exprs(block)):
            assert name in DOCKER_METRICS, f"{uid}: {name} is not exported"


def test_new_rules_present_with_severity():
    blocks = rule_blocks()
    want = {
        "container-oom-killed": ("docker_container_oom_killed", "critical"),
        "root-filesystem-readonly": ("node_filesystem_readonly", "critical"),
        "textfile-collector-stale": ("node_textfile_mtime_seconds", "warning"),
        "textfile-collector-scrape-error": ("node_textfile_scrape_error", "warning"),
        "claude-quota-behind-pace": ("claude_quota_reserve_percent_points", "warning"),
    }
    for uid, (metric, severity) in want.items():
        assert uid in blocks, uid
        assert metric in exprs(blocks[uid]), uid
        assert f"severity: {severity}" in blocks[uid], uid


def test_no_five_hour_quota_rule():
    assert "five_hour" not in RULES
