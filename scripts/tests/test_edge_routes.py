"""The edge's Traefik routes (compose/edge/traefik/dynamic.yml and
openclaw.yml) and the compose environment they read: the dashboard gets the
sign-in identity and the edge's proof, devclaw's route is unchanged, and the
OpenClaw gateway gets the identity from the sign-in, none from the phone app,
and a fixed service identity for each service client.

Both files are Go templates, but their only actions sit inside YAML strings,
so they parse as plain YAML. Traefik's file provider merges every file in the
directory, as the tests below do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
TRAEFIK = REPO / "compose/edge/traefik"
FILES = {
    p.name: yaml.safe_load(p.read_text())["http"] for p in sorted(TRAEFIK.glob("*.yml"))
}
HTTP = {
    kind: {k: v for f in FILES.values() for k, v in f.get(kind, {}).items()}
    for kind in ("routers", "middlewares", "services")
}
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
    assert {"DEVCLAW_MCP_TOKEN", "RELAY_EDGE_PROOF", "OPENCLAW_GATEWAY_TOKEN"} <= used
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


def test_no_name_is_defined_in_two_route_files():
    # The file provider would keep one silently.
    for kind in ("routers", "middlewares", "services"):
        names = [n for f in FILES.values() for n in f.get(kind, {})]
        assert len(names) == len(set(names)), kind


def _entrypoint_routers(entrypoint):
    return {n: r for n, r in ROUTERS.items() if r["entryPoints"] == [entrypoint]}


def test_openclaw_browsers_sign_in_and_reach_the_gateway_with_the_email():
    r = ROUTERS["openclaw"]
    assert r["rule"] == "PathPrefix(`/`)"
    # Strip any client-sent identity, then the check sets the real one.
    assert r["middlewares"] == ["openclaw-no-identity", "sso"]
    assert r["service"] == "openclaw"


def test_openclaw_sign_in_callback_stays_outside_the_gate():
    r = ROUTERS["openclaw-oauth2"]
    assert "middlewares" not in r
    assert r["service"] == "oauth2-proxy"
    assert (
        r["priority"]
        > ROUTERS["openclaw-app"]["priority"]
        > ROUTERS["openclaw"]["priority"]
    )


def test_openclaw_app_route_carries_no_identity():
    assert ROUTERS["openclaw-app"]["middlewares"] == ["openclaw-no-identity"]
    assert MIDDLEWARES["openclaw-no-identity"] == {
        "headers": {
            "customRequestHeaders": {
                "X-Auth-Request-Email": "",
                "X-Openclaw-Scopes": "",
            }
        }
    }


def _ua_patterns():
    return re.findall(
        r"HeaderRegexp\(`User-Agent`, `([^`]+)`\)", ROUTERS["openclaw-app"]["rule"]
    )


@pytest.mark.parametrize(
    ("ua", "app"),
    [
        ("OpenClaw/3 CFNetwork/3896.100.1.2.1 Darwin/27.0.0", True),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Mobile/15E148",
            True,
        ),
        (
            "Mozilla/5.0 (iPad; CPU OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Mobile/15E148",
            True,
        ),
        # Mobile Safari, desktop browsers and anything else sign in.
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.7 Mobile/15E148 Safari/604.1",
            False,
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.7 Safari/605.1.15",
            False,
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
            False,
        ),
        ("curl/8.14.1", False),
        ("", False),
    ],
)
def test_openclaw_app_route_matches_the_phone_app_only(ua, app):
    patterns = _ua_patterns()
    assert len(patterns) == 2
    # Go's regexp and Python's agree on this subset (anchors, classes, groups).
    assert any(re.search(p, ua) for p in patterns) is app


def test_openclaw_internal_routes_each_match_one_path_and_the_bearer():
    routes = _entrypoint_routers("openclaw-internal")
    assert set(routes) == {"openclaw-prometheus", "openclaw-finance-sentry"}
    bearer = 'Header(`Authorization`, `Bearer {{ env "OPENCLAW_GATEWAY_TOKEN" }}`)'
    assert routes["openclaw-prometheus"]["rule"] == (
        f"Path(`/api/diagnostics/prometheus`) && {bearer}"
    )
    assert (
        routes["openclaw-finance-sentry"]["rule"] == f"PathPrefix(`/v1/`) && {bearer}"
    )


@pytest.mark.parametrize(
    ("router", "identity", "scopes"),
    [
        ("openclaw-prometheus", "service:prometheus", "operator.read"),
        ("openclaw-finance-sentry", "service:finance-sentry", "operator.write"),
    ],
)
def test_openclaw_service_identities_carry_only_the_scope_they_need(
    router, identity, scopes
):
    (mw,) = ROUTERS[router]["middlewares"]
    assert MIDDLEWARES[mw] == {
        "headers": {
            "customRequestHeaders": {
                "X-Auth-Request-Email": identity,
                "X-Openclaw-Scopes": scopes,
            }
        }
    }


def test_every_openclaw_route_reaches_the_gateway_on_its_ingress_alias_only():
    for name, r in ROUTERS.items():
        if r["entryPoints"][0].startswith("openclaw") and name != "openclaw-oauth2":
            assert r["service"] == "openclaw"
    assert HTTP["services"]["openclaw"] == {
        "loadBalancer": {"servers": [{"url": "http://openclaw-gateway-edge:18789"}]}
    }


def test_openclaw_entrypoints_and_only_the_public_one_is_published():
    traefik = COMPOSE["services"]["traefik"]
    cmd = traefik["command"]
    assert "--entrypoints.openclaw.address=:18892" in cmd
    assert "--entrypoints.openclaw-internal.address=:18893" in cmd
    ports = [p for p in traefik["ports"] if "1889" in p]
    assert "127.0.0.1:18892:18892" in ports
    assert not any("18893" in p for p in traefik["ports"])


def test_reply_hook_route_passes_only_post_to_the_one_hook_path():
    r = ROUTERS["openclaw-reply-hook"]
    assert r["entryPoints"] == ["openclaw-hook"]
    assert r["rule"] == (
        'Method(`POST`) && Path(`/{{ env "OPENCLAW_HOOK_PATH" | trimAll "/" }}/firstmate-reply`)'
    )
    # The identity headers are stripped, no sign-in, no service identity: the
    # hook token the gateway checks is the only authentication.
    assert r["middlewares"] == ["openclaw-no-identity"]
    assert r["service"] == "openclaw"


def test_reply_hook_entrypoint_has_exactly_that_one_router():
    # Traefik answers 404 for anything no router on the entrypoint matches.
    assert set(_entrypoint_routers("openclaw-hook")) == {"openclaw-reply-hook"}


def test_reply_hook_entrypoint_is_loopback_published_and_trusts_no_forwarded_headers():
    traefik = COMPOSE["services"]["traefik"]
    cmd = traefik["command"]
    assert "--entrypoints.openclaw-hook.address=:18894" in cmd
    assert "--entrypoints.openclaw-hook.http.aliasHeadersStrategy=delete" in cmd
    assert not any("openclaw-hook.forwardedHeaders" in c for c in cmd)
    assert [p for p in traefik["ports"] if "18894" in p] == ["127.0.0.1:18894:18894"]


def test_hook_path_reaches_traefik_from_the_rendered_env():
    env = COMPOSE["services"]["traefik"]["environment"]
    assert env["OPENCLAW_HOOK_PATH"] == "${OPENCLAW_HOOK_PATH:-}"
