"""Behaviour of scripts/quota-gauge/*.sh against a fake quota-axi / quota_share."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GAUGE = REPO / "scripts/quota-gauge/claude-quota-gauge.sh"
SHARE_GAUGE = REPO / "scripts/quota-gauge/claude-quota-share-gauge.sh"

WINDOW = {
    "id": "seven_day",
    "resetsAt": "2026-10-05T06:00:00.048397+00:00",
    "percentRemaining": 33,
    "pace": {"status": "ahead", "reservePercentPoints": -8.6, "burnMultiple": 1.14},
}


def payload(runway=None):
    effective = {"scope": "all_models", "status": "known"}
    if runway is not None:
        effective["runway"] = runway
    return {
        "providers": [
            {
                "provider": "claude",
                "windows": [WINDOW],
                "quotaSemantics": {"effectiveAvailability": [effective]},
            }
        ]
    }


@pytest.fixture
def env(tmp_path):
    bindir, out = tmp_path / "bin", tmp_path / "out"
    bindir.mkdir()
    out.mkdir()
    return (
        {
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "FAKE_DIR": str(tmp_path),
        },
        bindir,
        out,
    )


def fake(bindir, name, body):
    path = bindir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


def prom(path):
    return [ln for ln in path.read_text().splitlines() if ln]


def run_gauge(env, bindir, out, data):
    (Path(env["FAKE_DIR"]) / "quota.json").write_text(json.dumps(data))
    fake(bindir, "quota-axi", 'cat "$FAKE_DIR/quota.json"\n')
    return subprocess.run(
        ["bash", str(GAUGE), str(out)], env=env, capture_output=True, text=True
    )


def test_runway_metrics_exported(env):
    e, bindir, out = env
    runway = {
        "status": "projected_exhaustion",
        "usableRunwaySeconds": 4262,
        "projectedExhaustedAt": "2026-10-02T09:17:18.690Z",
        "limitingWindowId": "five_hour",
    }
    assert run_gauge(e, bindir, out, payload(runway)).returncode == 0
    lines = prom(out / "claude_quota.prom")
    assert "claude_quota_runway_seconds 4262" in lines
    assert "claude_quota_projected_exhausted_at_seconds 1790932638" in lines
    assert 'claude_quota_runway_status{status="projected_exhaustion"} 1' in lines
    assert 'claude_quota_limiting_window{window="five_hour"} 1' in lines
    assert 'claude_quota_percent_remaining{window="seven_day"} 33' in lines


def test_runway_lines_absent_without_projection(env):
    e, bindir, out = env
    runway = {"status": "no_projection"}
    assert run_gauge(e, bindir, out, payload(runway)).returncode == 0
    lines = prom(out / "claude_quota.prom")
    assert 'claude_quota_runway_status{status="no_projection"} 1' in lines
    assert not [ln for ln in lines if ln.startswith("claude_quota_runway_seconds")]
    assert not [ln for ln in lines if "limiting_window" in ln]


def test_runway_lines_absent_when_quota_axi_has_no_runway(env):
    e, bindir, out = env
    assert run_gauge(e, bindir, out, payload()).returncode == 0
    assert not [ln for ln in prom(out / "claude_quota.prom") if "runway" in ln]


REPORT = {
    "consumers": [
        {"name": "firstmate", "imputedUsedPct": 12.5},
        {"name": "captain", "imputedUsedPct": None, "residual": True},
        {"name": "devclaw", "imputedUsedPct": 0},
    ]
}


def run_share(env, bindir, out, exit_code):
    stub = Path(env["FAKE_DIR"]) / "quota_share.py"
    stub.write_text(
        f"import json, sys\nprint(json.dumps({REPORT!r}))\nsys.exit({exit_code})\n"
    )
    return subprocess.run(
        ["bash", str(SHARE_GAUGE), str(out), str(stub)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_share_metric_skips_residual_consumer(env):
    e, bindir, out = env
    assert run_share(e, bindir, out, 0).returncode == 0
    assert prom(out / "claude_quota_share.prom") == [
        'claude_quota_share_percent{consumer="firstmate"} 12.5',
        'claude_quota_share_percent{consumer="devclaw"} 0',
    ]


def test_share_over_budget_exit_still_writes(env):
    e, bindir, out = env
    assert run_share(e, bindir, out, 1).returncode == 0
    assert (out / "claude_quota_share.prom").exists()


def test_share_unreadable_quota_keeps_previous_file(env):
    e, bindir, out = env
    (out / "claude_quota_share.prom").write_text("old 1\n")
    assert run_share(e, bindir, out, 2).returncode == 2
    assert (out / "claude_quota_share.prom").read_text() == "old 1\n"
    assert not [p for p in out.iterdir() if p.name.startswith(".")]


def test_share_crash_with_exit_one_keeps_previous_file(env):
    e, bindir, out = env
    (out / "claude_quota_share.prom").write_text("old 1\n")
    stub = Path(e["FAKE_DIR"]) / "quota_share.py"
    stub.write_text("import sys\nsys.exit(1)\n")
    result = subprocess.run(
        ["bash", str(SHARE_GAUGE), str(out), str(stub)],
        env=e,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert (out / "claude_quota_share.prom").read_text() == "old 1\n"
    assert not [p for p in out.iterdir() if p.name.startswith(".")]
