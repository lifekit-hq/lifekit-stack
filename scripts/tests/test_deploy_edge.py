"""Behaviour of scripts/deploy-edge.sh (the tailnet sign-in gate's redirect
domains and its Serve report) against a stubbed `tailscale`."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/deploy-edge.sh"

TS_STUB = """#!/bin/sh
[ -n "$TS_FAIL" ] && exit 1
if [ "$1" = serve ]; then printf '%s' "$TS_SERVE"; else printf '%s' "$TS_JSON"; fi
"""

NAME = "box.example.ts.net"
GATED = {18790: 18890, 18791: 18891}


def serve_json(targets=GATED, funnel=()):
    return json.dumps(
        {
            "TCP": {str(p): {"HTTPS": True} for p in targets},
            "Web": {
                f"{NAME}:{p}": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{t}"}}}
                for p, t in targets.items()
            },
            "AllowFunnel": {f"{NAME}:{p}": True for p in funnel},
        }
    )


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ts = bin_dir / "tailscale"
    ts.write_text(TS_STUB)
    ts.chmod(0o755)

    def _run(cmd, dns=NAME + ".", fail=False, env_lines=(), shell=None, serve=None):
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        env["TS_JSON"] = json.dumps({"Self": {"DNSName": dns}})
        env["TS_SERVE"] = serve if serve is not None else serve_json()
        if fail:
            env["TS_FAIL"] = "1"
        ef = tmp_path / "env"
        ef.write_text("\n".join(env_lines) + "\n")
        env["ENV_FILE"] = str(ef)
        env.update(shell or {})
        return subprocess.run(
            ["bash", str(SCRIPT), cmd],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    return _run


def test_domains_cover_every_port_of_the_tailnet_name(run):
    r = run("domains", shell={"IDENTITY_ENDPOINT": f"https://{NAME}:3001"})
    assert r.returncode == 0
    assert r.stdout == f"EDGE_REDIRECT_DOMAINS={NAME}:*\n"


def test_domains_add_an_issuer_on_another_host(run):
    r = run("domains", env_lines=['IDENTITY_ENDPOINT="https://id.example.org:8443"'])
    assert r.stdout == f"EDGE_REDIRECT_DOMAINS={NAME}:*,id.example.org:8443\n"
    r = run("domains", shell={"IDENTITY_ENDPOINT": "https://ID.example.org"})
    assert r.stdout == f"EDGE_REDIRECT_DOMAINS={NAME}:*,id.example.org\n"


def test_domains_without_an_issuer_still_cover_the_surfaces(run):
    r = run("domains")
    assert r.stdout == f"EDGE_REDIRECT_DOMAINS={NAME}:*\n"


@pytest.mark.parametrize("dns", [None, "", "bad name!.ts.net.", "localhost"])
def test_domains_without_a_tailnet_name_print_nothing(run, dns):
    r = run("domains", dns=dns)
    assert r.returncode == 0
    assert r.stdout == ""
    assert "not derivable" in r.stderr


def test_domains_with_tailscale_down_print_nothing(run):
    r = run("domains", fail=True)
    assert r.returncode == 0
    assert r.stdout == ""


def test_check_passes_when_both_surfaces_go_through_the_gate(run):
    r = run("check", env_lines=["DEVCLAW_MCP_TOKEN=t"])
    assert r.returncode == 0, r.stdout
    assert ":18790: https -> 127.0.0.1:18890 (sign-in gate), tailnet-only" in r.stdout
    assert ":18791: https -> 127.0.0.1:18891 (sign-in gate), tailnet-only" in r.stdout
    assert "DEVCLAW_MCP_TOKEN set" in r.stdout


def test_check_flags_a_surface_still_served_directly(run):
    # Before the operator's cutover, Serve still points at the apps' own ports.
    r = run(
        "check",
        serve=serve_json(targets={18790: 18790, 18791: 18891}),
        env_lines=["DEVCLAW_MCP_TOKEN=t"],
    )
    assert r.returncode == 1
    assert (
        ":18790: https -> http://127.0.0.1:18790, not the sign-in gate (127.0.0.1:18890)"
        in r.stdout
    )
    assert ":18791: https -> 127.0.0.1:18891 (sign-in gate)" in r.stdout


def test_check_flags_funnel_loudly(run):
    r = run(
        "check", serve=serve_json(funnel=(18791,)), env_lines=["DEVCLAW_MCP_TOKEN=t"]
    )
    assert r.returncode == 1
    assert ":18791: FUNNEL IS ON" in r.stdout


@pytest.mark.parametrize(
    "serve", [serve_json(targets={18790: 18890}), "{}", "not json"]
)
def test_check_flags_a_port_not_published(run, serve):
    r = run("check", serve=serve, env_lines=["DEVCLAW_MCP_TOKEN=t"])
    assert r.returncode == 1
    assert "not published" in r.stdout or "unreadable" in r.stdout


def test_check_flags_a_missing_devclaw_bearer(run):
    r = run("check")
    assert r.returncode == 1
    assert "DEVCLAW_MCP_TOKEN unset" in r.stdout


def test_usage(run):
    assert run("bogus").returncode == 2
