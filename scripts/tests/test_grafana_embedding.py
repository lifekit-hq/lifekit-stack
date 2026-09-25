"""Grafana iframe embedding is only enabled together with a pinned frame-ancestors."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((REPO / "compose/docker-compose.yml").read_text())
ENV = {k: str(v) for k, v in COMPOSE["services"]["grafana"]["environment"].items()}


def csp() -> str:
    return ENV["GF_SECURITY_CONTENT_SECURITY_POLICY_TEMPLATE"]


def test_embedding_requires_csp_enabled():
    assert ENV["GF_SECURITY_ALLOW_EMBEDDING"] == "true"
    assert ENV["GF_SECURITY_CONTENT_SECURITY_POLICY"] == "true"


def test_frame_ancestors_pinned_to_variable_with_deny_default():
    m = re.search(r"frame-ancestors ([^;]*);", csp())
    assert m, "embedding enabled without a frame-ancestors directive"
    assert m.group(1) == "${GRAFANA_EMBED_ORIGIN:-'none'}"


def test_no_wildcard_frame_ancestors():
    value = re.search(r"frame-ancestors ([^;]*);", csp()).group(1)
    assert "*" not in value


def test_stock_csp_placeholders_survive_compose_escaping():
    assert "$$NONCE" in csp() and "$$ROOT_PATH" in csp()


def test_anonymous_auth_not_enabled():
    for key, value in ENV.items():
        if key.startswith("GF_AUTH_ANONYMOUS"):
            assert not (key.endswith("ENABLED") and value.lower() == "true")


def test_env_example_documents_variable_unset_by_default():
    text = (REPO / ".env.example").read_text()
    assert re.search(r"^# GRAFANA_EMBED_ORIGIN=", text, re.M)
    assert not re.search(r"^GRAFANA_EMBED_ORIGIN=", text, re.M)
