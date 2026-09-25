"""Grafana iframe embedding is only enabled together with a pinned frame-ancestors.

Runs the real consumer (`docker compose config`) and asserts on the resolved env.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "compose"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker compose unavailable"
)


def grafana_env(embed_origin: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "GRAFANA_EMBED_ORIGIN"}
    if embed_origin is not None:
        env["GRAFANA_EMBED_ORIGIN"] = embed_origin
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=COMPOSE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)["services"]["grafana"]["environment"]


def frame_ancestors(env: dict[str, str]) -> str:
    m = re.search(
        r"frame-ancestors ([^;]*);", env["GF_SECURITY_CONTENT_SECURITY_POLICY_TEMPLATE"]
    )
    assert m, "embedding enabled without a frame-ancestors directive"
    return m.group(1)


def test_unset_origin_denies_all_framing():
    env = grafana_env(None)
    assert env["GF_SECURITY_ALLOW_EMBEDDING"] == "true"
    assert env["GF_SECURITY_CONTENT_SECURITY_POLICY"] == "true"
    assert frame_ancestors(env) == "'none'"


def test_set_origin_is_the_only_frame_ancestor():
    env = grafana_env("https://dash.example.ts.net")
    assert env["GF_SECURITY_CONTENT_SECURITY_POLICY"] == "true"
    assert frame_ancestors(env) == "https://dash.example.ts.net"


def test_stock_csp_placeholders_reach_grafana_escaped():
    template = grafana_env(None)["GF_SECURITY_CONTENT_SECURITY_POLICY_TEMPLATE"]
    assert "$$NONCE" in template and "$$ROOT_PATH" in template


def test_anonymous_auth_not_enabled():
    env = grafana_env(None)
    assert env.get("GF_AUTH_ANONYMOUS_ENABLED", "false").lower() != "true"
