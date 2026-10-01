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
    deleted = {r["uid"] for r in DOC["deleteRules"]}
    for uid in ("container-memory-near-limit", "root-filesystem-readonly"):
        assert uid not in RULES
        assert uid in deleted


def test_rules_only_reference_real_metrics():
    exprs = "\n".join(e for r in RULES.values() for e in queries(r))
    assert "openclaw_prometheus_series_dropped_total" not in exprs
    real = emitted_docker_metrics()
    for uid, rule in RULES.items():
        for expr in queries(rule):
            for name in re.findall(r"\bdocker_[a-z_]+", expr):
                assert name in real, f"{uid}: {name} is not exported"


@pytest.mark.parametrize(
    ("uid", "metric", "severity", "op", "limit"),
    [
        ("container-oom-killed", "docker_container_oom_killed", "critical", "gt", 0),
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
        (
            "finance-backup-verify-stale",
            "finance_backup_last_verified_age_seconds",
            "warning",
            "gt",
            9 * 86400,
        ),
        (
            "finance-retention-stale",
            "finance_retention_last_run_age_seconds",
            "warning",
            "gt",
            2 * 86400,
        ),
    ],
)
def test_new_rules(uid, metric, severity, op, limit):
    rule = RULES[uid]
    assert any(metric in e for e in queries(rule))
    assert rule["labels"]["severity"] == severity
    assert threshold(rule) == (op, limit)


def test_host_ram_alert_uses_available_not_bare_free():
    rule = RULES["host-ram-nearly-exhausted"]
    assert any("node_memory_MemAvailable_bytes" in e for e in queries(rule))
    assert rule["labels"]["severity"] == "critical"
    assert threshold(rule) == ("lt", 0.10)


def test_tmp_filesystem_alert_uses_avail_over_size():
    rule = RULES["tmp-filesystem-nearly-full"]
    (expr,) = queries(rule)
    assert "tmp_filesystem_avail_bytes" in expr
    assert "tmp_filesystem_size_bytes" in expr
    assert rule["labels"]["severity"] == "warning"
    assert threshold(rule) == ("lt", 0.20)


def test_host_swap_alert_requires_ram_pressure_too():
    # Swap used alone sits near 100% permanently on this box (no swap-back-in),
    # so the rule must gate on available RAM as well, not swap in isolation.
    rule = RULES["host-swap-and-ram-pressure"]
    (expr,) = queries(rule)
    assert "node_memory_SwapTotal_bytes" in expr
    assert "node_memory_SwapFree_bytes" in expr
    assert "node_memory_MemAvailable_bytes" in expr
    assert " and " in expr
    assert rule["labels"]["severity"] == "warning"
    assert threshold(rule) == ("lt", 0.20)


def test_quota_pace_rule_active_and_no_five_hour_rule():
    rule = RULES["claude-quota-behind-pace"]
    assert rule["labels"]["severity"] == "warning"
    assert any("claude_quota_reserve_percent_points" in e for e in queries(rule))
    assert not [
        uid for uid, r in RULES.items() if any("five_hour" in e for e in queries(r))
    ]


def test_near_cap_rule_threshold():
    assert threshold(RULES["container-near-memory-cap"]) == ("gt", 90)


def test_memory_rules_ship_and_the_per_group_budget_rules_are_retired():
    assert set(RULES) >= {
        "host-memory-pressure",
        "container-near-memory-cap",
        "host-swap-high",
    }
    assert threshold(RULES["host-swap-high"]) == ("gt", 75)
    retired = {
        "project-memory-over-budget",
        "host-group-over-budget",
        "burst-pool-over-budget",
    }
    assert not retired & set(RULES)
    # Provisioning only adds and updates: a retired uid must be named to be deleted.
    assert retired <= {d["uid"] for d in DOC["deleteRules"]}


GIB = 1073741824
HOST_GAUGE_INTERVAL_S = 300
SCRAPE_INTERVAL_S = 15

_COMPARE = r"\(\s*(?:(?P<fn>rate)\((?P<metric>\w+)\[(?P<win>\d+)m\]\)|(?P<bare>\w+))\s*(?P<op>[<>])\s*(?P<rhs>[\d\s*()]+?)\s*\)"


def fires(expr: str, samples: dict[str, float]) -> bool:
    """Evaluate the pressure expression: `A or on() B` over scalar samples.

    A side with no sample yields no series, as in Prometheus, so it is false.
    """
    sides = re.split(r"\s+or\s+on\(\)\s+", " ".join(expr.split()))
    assert len(sides) == 2
    verdicts = []
    for side in sides:
        m = re.fullmatch(_COMPARE, side)
        assert m, side
        key = m["metric"] or m["bare"]
        if key not in samples:
            verdicts.append(False)
            continue
        rhs = eval(m["rhs"], {"__builtins__": {}})  # arithmetic literals only
        value = samples[key]
        verdicts.append(value < rhs if m["op"] == "<" else value > rhs)
    return any(verdicts)


def rate_window_seconds(expr: str) -> int:
    (win,) = re.findall(r"rate\(\w+\[(\d+)m\]\)", expr)
    return int(win) * 60


def rank_of(expr: str, groups: dict[str, float]) -> list[str]:
    """Evaluate `topk(n, m) [unless topk(k, m)]` over a group -> bytes map."""
    m = re.fullmatch(
        r"topk\((\d), [\w:]+\)(?: unless topk\((\d), [\w:]+\))?", expr.strip()
    )
    assert m, expr
    ordered = sorted(groups, key=groups.get, reverse=True)
    kept = set(ordered[: int(m[1])])
    if m[2]:
        kept -= set(ordered[: int(m[2])])
    return [g for g in ordered if g in kept]


def test_host_memory_pressure_rule_semantics():
    rule = RULES["host-memory-pressure"]
    assert threshold(rule) == ("gt", 0)
    assert rule["for"] == "15m"
    assert rule["labels"]["severity"] == "warning"
    expr = queries(rule)[0]
    healthy = {"node_memory_MemAvailable_bytes": 8 * GIB}
    assert not fires(expr, healthy)
    assert not fires(expr, healthy | {"host_vmstat_pswpin_pages_total": 68})
    assert fires(expr, {"node_memory_MemAvailable_bytes": 1.5 * GIB})
    assert not fires(expr, {"node_memory_MemAvailable_bytes": 2.5 * GIB})
    assert fires(expr, healthy | {"host_vmstat_pswpin_pages_total": 1500})
    # The swap-in counter is only rewritten every host-gauge interval; a rate
    # window must hold two writes even when one drifts by a scrape.
    assert rate_window_seconds(expr) >= 2 * (HOST_GAUGE_INTERVAL_S + SCRAPE_INTERVAL_S)


def test_host_memory_pressure_message_lists_the_top_three_groups():
    rule = RULES["host-memory-pressure"]
    groups = {
        "openclaw": 3.6 * GIB,
        "platform": 1.3 * GIB,
        "operator sessions": 4.3 * GIB,
        "CI runners": 0.4 * GIB,
        "OS and daemons": 0.75 * GIB,
    }
    by_ref = {d["refId"]: d["model"] for d in rule["data"]}
    names = {}
    for n in ("1", "2", "3"):
        assert by_ref[f"R{n}"]["expression"] == f"G{n}"
        (name,) = rank_of(by_ref[f"G{n}"]["expr"], groups)
        names[f"R{n}"] = name
    assert list(names.values()) == ["operator sessions", "openclaw", "platform"]
    rendered = re.sub(
        r"\{\{ \$values\.(R\d)\.Labels\.name \}\}",
        lambda m: names[m[1]],
        rule["annotations"]["description"],
    )
    ranks = re.findall(r"\d\. ([A-Za-z ]+?) \{\{", rendered)
    assert ranks == list(names.values())
    prom = yaml.safe_load(
        (REPO / "compose/observability/prometheus/rules.yml").read_text()
    )
    recorded = {r["record"] for g in prom["groups"] for r in g["rules"]}
    assert set(re.findall(r"topk\(\d, ([\w:]+)\)", by_ref["G1"]["expr"])) == recorded


def test_claude_token_expiry_rule_matches_the_documented_date():
    # The yearly rotation moves both; a date changed in one place only would
    # alert a year early or not at all.
    from datetime import datetime, timezone

    rule = RULES["claude-oauth-token-expiring"]
    (expr,) = queries(rule)
    epoch = int(re.fullmatch(r"\(vector\((\d+)\) - time\(\)\) / 86400", expr)[1])
    (row,) = [
        line
        for line in (REPO / "docs/secrets.md").read_text().splitlines()
        if line.startswith("| `CLAUDE_OAUTH_TOKEN` |")
    ]
    (date,) = re.findall(r"Expires (\d{4}-\d{2}-\d{2})", row)
    documented = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    assert epoch == int(documented.timestamp())
    assert threshold(rule) == ("lt", 30)
    assert rule["labels"]["severity"] == "warning"


def test_memory_group_selectors_match_weekly_report():
    """The recording rule and the weekly report must name the same members."""
    spec = importlib.util.spec_from_file_location(
        "weekly", REPO / "scripts/ops-report/weekly.py"
    )
    weekly = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(weekly)

    rules = yaml.safe_load(
        (REPO / "compose/observability/prometheus/rules.yml").read_text()
    )
    expr = rules["groups"][0]["rules"][0]["expr"]

    def squash(text):
        return re.sub(r"\s+", "", text)

    in_rule = {
        name: squash(selector)
        for selector, name in re.findall(
            r'docker_container_memory_usage_bytes\{([^}]*)\}\),\s*"name", "([^"]+)"',
            expr,
        )
    }
    in_report = {n: squash(sel) for n, sel in weekly.GROUP_SELECTORS.items()}
    assert in_rule == in_report


def test_finance_job_age_rules_wait_an_hour_and_stay_quiet_without_data():
    for uid in ("finance-backup-verify-stale", "finance-retention-stale"):
        rule = RULES[uid]
        assert rule["for"] == "1h"
        assert rule["noDataState"] == "OK"
        assert rule["labels"]["service"] == "finance-sentry"
