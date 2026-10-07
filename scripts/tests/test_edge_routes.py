"""The edge's Traefik routes (compose/edge/traefik/dynamic.yml) and the
compose environment they read: the dashboard gets the sign-in identity and the
edge's proof, devclaw's route is unchanged.

dynamic.yml is a Go template, but its only actions sit inside quoted YAML
strings, so it parses as plain YAML.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
HTTP = yaml.safe_load((REPO / "compose/edge/traefik/dynamic.yml").read_text())["http"]
ROUTERS, MIDDLEWARES = HTTP["routers"], HTTP["middlewares"]
COMPOSE = yaml.safe_load((REPO / "compose/edge/docker-compose.yml").read_text())


def test_dashboard_router_runs_the_sign_in_check_then_adds_the_proof():
    assert ROUTERS["dashboard"]["middlewares"] == ["sso", "dashboard-edge-proof"]


def test_sso_forwards_only_the_signed_in_email():
    # authResponseHeaders copies these from oauth2-proxy's answer onto the
    # request, replacing any value the client sent; nothing else is forwarded.
    assert MIDDLEWARES["sso"]["forwardAuth"]["authResponseHeaders"] == [
        "X-Auth-Request-Email"
    ]


def test_sso_check_itself_is_unchanged():
    fa = MIDDLEWARES["sso"]["forwardAuth"]
    assert fa["address"] == "http://oauth2-proxy:4180/"
    assert fa["trustForwardHeader"] is True


def test_proof_middleware_sets_exactly_the_proof_header_from_the_environment():
    # A headers middleware overwrites a client-sent copy of the same name.
    assert MIDDLEWARES["dashboard-edge-proof"] == {
        "headers": {
            "customRequestHeaders": {
                "X-Lifekit-Edge-Proof": '{{ env "RELAY_EDGE_PROOF" }}'
            }
        }
    }


def test_proof_value_reaches_traefik_from_the_rendered_env():
    env = COMPOSE["services"]["traefik"]["environment"]
    assert env["RELAY_EDGE_PROOF"] == "${RELAY_EDGE_PROOF:-}"


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def test_every_template_env_var_is_passed_to_traefik():
    used = {
        name
        for value in _strings(HTTP)
        for name in re.findall(r"\{\{\s*env\s+\"(\w+)\"\s*\}\}", value)
    }
    assert {"DEVCLAW_MCP_TOKEN", "RELAY_EDGE_PROOF"} <= used
    assert used <= set(COMPOSE["services"]["traefik"]["environment"])


def test_oauth2_proxy_answers_the_check_with_the_email_header():
    env = COMPOSE["services"]["oauth2-proxy"]["environment"]
    assert env["OAUTH2_PROXY_SET_XAUTHREQUEST"] == "true"


def test_devclaw_routes_are_unchanged():
    assert ROUTERS["devclaw"]["middlewares"] == ["sso", "devclaw-bearer"]
    assert "middlewares" not in ROUTERS["devclaw-machine"]
    assert MIDDLEWARES["devclaw-bearer"] == {
        "headers": {
            "customRequestHeaders": {
                "Authorization": 'Bearer {{ env "DEVCLAW_MCP_TOKEN" }}'
            }
        }
    }


def test_the_proof_is_added_to_the_dashboard_only():
    users = {
        name
        for name, r in ROUTERS.items()
        if "dashboard-edge-proof" in r.get("middlewares", [])
    }
    assert users == {"dashboard"}


def test_the_sign_in_routes_stay_outside_the_gate():
    for name in ("dashboard-oauth2", "devclaw-oauth2"):
        assert "middlewares" not in ROUTERS[name]
