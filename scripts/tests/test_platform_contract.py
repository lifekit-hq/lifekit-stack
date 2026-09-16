"""Tests for scripts/platform-contract.py (no Docker, no network)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "platform-contract.py"
_spec = importlib.util.spec_from_file_location("platform_contract", SCRIPT)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)

V1 = {
    "lifekit.contract": "v1",
    "lifekit.contract.port": "8090",
    "lifekit.contract.health": "/health",
    "lifekit.contract.ready": "/ready",
    "lifekit.contract.metrics": "/metrics",
    "lifekit.contract.ingress": "internal",
}
JSON_LINE = json.dumps({"level": "info", "msg": "request", "trace_id": "ab" * 16})


# ─── static gate ─────────────────────────────────────────────────────────────


def test_static_complete_declaration_passes():
    assert pc.static_findings("relay", {"labels": dict(V1)}) == []


def test_static_none_opts_out():
    assert pc.static_findings("db", {"labels": {"lifekit.contract": "none"}}) == []


def test_static_undeclared_fails():
    assert pc.static_findings("x", {}) == ["no lifekit.contract=v1|none label"]


@pytest.mark.parametrize("missing", ["port", "health", "ready", "metrics", "ingress"])
def test_static_missing_item_fails(missing):
    labels = dict(V1)
    del labels[f"lifekit.contract.{missing}"]
    assert f"no lifekit.contract.{missing} label" in pc.static_findings(
        "x", {"labels": labels}
    )


def test_static_internal_with_host_port_fails():
    spec = {"labels": dict(V1), "ports": [{"target": 8090, "published": "8090"}]}
    assert (
        "ingress=internal but the service publishes host ports"
        in pc.static_findings("x", spec)
    )


def test_static_ready_equal_to_health_fails():
    labels = dict(V1, **{"lifekit.contract.ready": "/health"})
    assert "readiness path is the liveness path" in pc.static_findings(
        "x", {"labels": labels}
    )


def test_static_has_no_waiver_label():
    # An unknown key is not a way out: the declaration is still checked in full.
    labels = {"lifekit.contract": "v1", "lifekit.contract.waive": "ready:2099-01-01:x"}
    assert len(pc.static_findings("x", {"labels": labels})) == 5


def test_run_static_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"name": "p", "services": {"a": {"labels": V1}}}))
    assert pc.run_static(str(good)) == 0
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"name": "p", "services": {"a": {"labels": V1}, "b": {}}})
    )
    assert pc.run_static(str(bad)) == 1
    assert "b: no lifekit.contract=v1|none label" in capsys.readouterr().out


# ─── logs ────────────────────────────────────────────────────────────────────


def test_logs_json_with_trace_passes():
    assert pc.log_verdict([JSON_LINE] * 10, "cd" * 16)[0] == "PASS"


def test_logs_text_fails():
    assert pc.log_verdict(["[15:19:37 INF] hello"] * 10, "x")[0] == "FAIL"


def test_logs_json_without_trace_fails():
    assert pc.log_verdict([json.dumps({"msg": "hi"})] * 10, "x")[0] == "FAIL"


def test_logs_share_threshold():
    lines = [JSON_LINE] * 9 + ["plain"]
    assert pc.log_verdict(lines, "x")[0] == "PASS"
    assert pc.log_verdict(lines + ["plain"], "x")[0] == "FAIL"


@pytest.mark.parametrize(
    "record",
    [{"@tr": "1"}, {"traceId": "1"}, {"trace_id": "1"}, {"trace": {"id": "1"}}],
)
def test_logs_trace_key_shapes(record):
    assert pc.log_verdict([json.dumps(record)], "x")[0] == "PASS"


def test_logs_empty_fails():
    assert pc.log_verdict(["", "  "], "x") == ("FAIL", "no log lines")


def test_logs_reports_probe_trace():
    assert "probe trace id logged" in pc.log_verdict([JSON_LINE], "ab" * 16)[1]


# ─── runtime check ───────────────────────────────────────────────────────────


def container(labels, published=False):
    return {
        "Id": "abc",
        "Name": "/compose-notify-relay-1",
        "Config": {
            "Labels": {
                "com.docker.compose.project": "compose",
                "com.docker.compose.service": "notify-relay",
                **labels,
            }
        },
        "NetworkSettings": {
            "Networks": {
                "compose_default": {
                    "IPAddress": "172.18.0.9",
                    "Aliases": ["compose-notify-relay-1", "notify-relay"],
                }
            }
        },
        "HostConfig": {"PortBindings": {"8090/tcp": [{}]} if published else {}},
    }


def target(address, path="/metrics", health="up", job="notify-relay"):
    return {
        "discoveredLabels": {"__address__": address, "__metrics_path__": path},
        "labels": {"job": job},
        "health": health,
    }


@pytest.fixture
def box(monkeypatch):
    """Fake HTTP answers per path and fake `docker logs` output."""
    state = {
        "http": {
            "/health": (200, "application/json"),
            "/ready": (200, "application/json"),
            "/metrics": (200, "text/plain; version=0.0.4"),
        },
        "logs": [JSON_LINE] * 5,
    }
    monkeypatch.setattr(
        pc,
        "get",
        lambda url, headers=None: state["http"].get(url.split(":8090")[1], (404, "")),
    )
    monkeypatch.setattr(
        pc.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "\n".join(state["logs"]), ""),
    )
    return state


def test_conforming_container_passes(box):
    r = pc.check(container(V1), [target("notify-relay:8090")])
    assert {k: v[0] for k, v in r.items()} == {
        "health": "PASS",
        "ready": "PASS",
        "metrics": "PASS",
        "scraped": "PASS",
        "logs": "PASS",
        "traces": "SKIP",
        "edge": "PASS",
        "topics": "SKIP",
    }


def test_spa_fallback_is_not_health(box):
    box["http"]["/health"] = (200, "text/html; charset=utf-8")
    assert pc.check(container(V1), [target("notify-relay:8090")])["health"][0] == "FAIL"


def test_scrape_target_must_match_path_and_be_up(box):
    assert (
        pc.check(container(V1), [target("notify-relay:8090", path="/other")])[
            "scraped"
        ][0]
        == "FAIL"
    )
    down = pc.check(container(V1), [target("notify-relay:8090", health="down")])
    assert down["scraped"] == ("FAIL", "job=notify-relay down")
    assert pc.check(container(V1), [])["scraped"][0] == "FAIL"


def test_token_metrics_follow_the_scrape_target(box):
    labels = dict(V1, **{"lifekit.contract.metrics.auth": "token"})
    box["http"]["/metrics"] = (401, "application/json")
    assert (
        pc.check(container(labels), [target("notify-relay:8090")])["metrics"][0]
        == "PASS"
    )
    assert pc.check(container(labels), [])["metrics"][0] == "FAIL"


def test_ready_via_edge_skips_until_edge_is_enforced(box, monkeypatch):
    labels = dict(V1, **{"lifekit.contract.ready.via": "edge"})
    box["http"]["/ready"] = (403, "application/json")
    assert (
        pc.check(container(labels), [target("notify-relay:8090")])["ready"][0] == "SKIP"
    )
    monkeypatch.setattr(pc, "ENFORCED", pc.ENFORCED | {"edge"})
    assert (
        pc.check(container(labels), [target("notify-relay:8090")])["ready"][0] == "FAIL"
    )


def test_unenforced_failures_are_skipped(box):
    labels = dict(V1, **{"lifekit.contract.ingress": "edge"})
    verdict, detail = pc.check(container(labels, published=True), [])["edge"]
    assert verdict == "SKIP" and "no edge proxy yet" in detail


def test_enforced_failure_is_not_skipped(box):
    box["logs"] = ["plain text"]
    assert pc.check(container(V1), [target("notify-relay:8090")])["logs"][0] == "FAIL"
