"""Invariants of the gateway.trustedProxies step in scripts/deploy.sh (static, no Docker).

Since OpenClaw 2026.9.x the gateway rejects proxied requests with
proxy_attribution_required unless the proxy's source address is listed in
gateway.trustedProxies. The value is host-derived (Docker assigns the compose
project's default network gateway address at network creation), so it can't
live in compose/openclaw-gateway/platform.patch.json — it's set once, in the
first-deploy onboard block, from a docker-network-inspect lookup. These tests
pin that shape rather than leaving it to review.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY_SH = (REPO / "scripts/deploy.sh").read_text()

ONBOARD_GUARD = re.compile(
    r'if \[\[ ! -f "\$\{OPENCLAW_CONFIG_DIR\}/openclaw\.json" \]\]; then\n'
    r"(.*?)\nfi\n",
    re.S,
)


def onboard_block() -> str:
    match = ONBOARD_GUARD.search(DEPLOY_SH)
    assert match, "no first-deploy onboard guard found in deploy.sh"
    return match.group(1)


def test_trusted_proxies_set_is_inside_the_first_deploy_guard():
    block = onboard_block()
    assert "gateway.trustedProxies" in block


def test_trusted_proxies_config_set_uses_strict_json():
    block = onboard_block()
    match = re.search(r"config set gateway\.trustedProxies.*", block)
    assert match, "no gateway.trustedProxies config set in the onboard block"
    assert "--strict-json" in match.group(0)


def test_trusted_proxies_config_set_is_not_outside_the_guard():
    # A regression that moves the config set below the closing `fi` would
    # re-run it (and re-fail on a since-changed network) on every deploy.
    tail = DEPLOY_SH[DEPLOY_SH.index(onboard_block()) + len(onboard_block()) :]
    assert "gateway.trustedProxies" not in tail.split("\n\n", 1)[0]


def test_network_name_is_derived_not_hardcoded():
    block = onboard_block()
    assert "compose_default" not in block, "network name must be derived, not hardcoded"
    assert "COMPOSE_PROJECT_NAME" in block
    assert "docker network inspect" in block
    assert re.search(r"_default", block), "expected a <project>_default network name"


def test_empty_address_aborts_the_deploy_without_a_fallback():
    block = onboard_block()
    match = re.search(
        r'if \[\[ -z "\$\{TRUSTED_PROXY_ADDR\}" \]\]; then\n(.*?)\n  fi\n', block, re.S
    )
    assert match, "no empty-address guard around TRUSTED_PROXY_ADDR"
    guard = match.group(1)
    assert "exit 1" in guard
    # No hardcoded IPv4/IPv6-looking literal fallback anywhere in the block.
    assert not re.search(r'trustedProxies\s+"\[\\"\d+\.\d+\.\d+\.\d+', block)
