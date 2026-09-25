"""Behaviour of scripts/deploy-embed-origin.sh against a stubbed `tailscale`."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/deploy-embed-origin.sh"

STUB = """#!/bin/sh
[ -n "$TS_FAIL" ] && exit 1
printf '%s' "$TS_JSON"
"""


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "tailscale"
    stub.write_text(STUB)
    stub.chmod(0o755)

    def _run(dns=None, fail=False, env_file_line=None, shell_value=None, raw=None):
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        env["TS_JSON"] = (
            raw if raw is not None else json.dumps({"Self": {"DNSName": dns}})
        )
        if fail:
            env["TS_FAIL"] = "1"
        ef = tmp_path / "env"
        ef.write_text((env_file_line or "") + "\n")
        env["ENV_FILE"] = str(ef)
        if shell_value:
            env["GRAFANA_EMBED_ORIGIN"] = shell_value
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
        )

    return _run


def test_explicit_env_file_value_wins(run):
    r = run(
        dns="box.example.ts.net.",
        env_file_line="GRAFANA_EMBED_ORIGIN=https://fake.example:8443",
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "https://fake.example:8443"


def test_explicit_shell_value_wins(run):
    r = run(dns="box.example.ts.net.", shell_value="https://fake.example")
    assert r.stdout.strip() == "https://fake.example"


def test_derived_exact_origin(run):
    r = run(dns="box.example.ts.net.")
    assert r.returncode == 0
    assert r.stdout == "https://box.example.ts.net\n"


@pytest.mark.parametrize(
    "kwargs", [{"fail": True}, {"raw": "not json"}, {"dns": ""}, {"raw": "{}"}]
)
def test_not_derivable_stays_denied_and_succeeds(run, kwargs):
    r = run(**kwargs)
    assert r.returncode == 0
    assert r.stdout == ""
    assert "'none'" in r.stderr


@pytest.mark.parametrize(
    "dns",
    [
        "*.example.ts.net.",
        "box.example.ts.net/x",
        "box.example.ts.net:443",
        "localhost",
    ],
)
def test_derived_origin_never_has_path_or_wildcard(run, dns):
    r = run(dns=dns)
    assert r.returncode == 0
    assert r.stdout == ""
