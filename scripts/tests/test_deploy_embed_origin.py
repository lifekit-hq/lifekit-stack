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
if [ "$1" = serve ]; then printf '%s' "$TS_SERVE"; else printf '%s' "$TS_JSON"; fi
"""

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$DOCKER_PORTS"
"""


def serve_json(name, port, target):
    return json.dumps(
        {"Web": {f"{name}:{port}": {"Handlers": {"/": {"Proxy": f"http://{target}"}}}}}
    )


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "tailscale"
    stub.write_text(STUB)
    stub.chmod(0o755)
    dstub = bin_dir / "docker"
    dstub.write_text(DOCKER_STUB)
    dstub.chmod(0o755)

    def _run(
        dns=None,
        fail=False,
        env_file_line=None,
        shell_value=None,
        raw=None,
        serve=None,
        ports="127.0.0.1:19999->8080/tcp",
    ):
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        env["TS_JSON"] = (
            raw if raw is not None else json.dumps({"Self": {"DNSName": dns}})
        )
        env["TS_SERVE"] = (
            serve
            if serve is not None
            else (serve_json(dns.rstrip("."), 8443, "127.0.0.1:19999") if dns else "{}")
        )
        env["DOCKER_PORTS"] = ports
        if fail:
            env["TS_FAIL"] = "1"
        ef = tmp_path / "env"
        ef.write_text((env_file_line or "") + "\n")
        env["ENV_FILE"] = str(ef)
        if shell_value:
            env["GRAFANA_EMBED_ORIGIN"] = shell_value
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
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
    assert r.stdout == "https://box.example.ts.net:8443\n"


def test_default_port_443_is_omitted(run):
    r = run(
        dns="box.example.ts.net.",
        serve=serve_json("box.example.ts.net", 443, "127.0.0.1:19999"),
    )
    assert r.stdout == "https://box.example.ts.net\n"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"serve": serve_json("box.example.ts.net", 8443, "127.0.0.1:12345")},
        {"serve": "{}"},
        {"ports": ""},
    ],
)
def test_dashboard_port_not_found_stays_denied(run, kwargs):
    r = run(dns="box.example.ts.net.", **kwargs)
    assert r.returncode == 0
    assert r.stdout == ""
    assert "'none'" in r.stderr


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
