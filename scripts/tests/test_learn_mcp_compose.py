"""compose/learn-mcp/docker-compose.yml: the study coach's connector stays off
the host's ports, inside the platform contract, and free of secret values; its
image carries the repo's own record.py and daily.py.

Plain YAML, no Docker. The service's behaviour is compose/learn-mcp/tests.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
SERVICE_DIR = REPO / "compose/learn-mcp"
COMPOSE = yaml.safe_load((SERVICE_DIR / "docker-compose.yml").read_text())
SVC = COMPOSE["services"]["learn-mcp"]

_spec = importlib.util.spec_from_file_location(
    "platform_contract", REPO / "scripts/platform-contract.py"
)
contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract)


def test_the_project_is_its_own_and_has_exactly_one_service():
    assert COMPOSE["name"] == "learn-mcp"
    assert list(COMPOSE["services"]) == ["learn-mcp"]


def test_the_service_passes_the_static_contract_check():
    assert contract.static_findings("learn-mcp", SVC) == []


def test_it_is_routed_through_the_edge_and_publishes_no_host_port():
    assert SVC["labels"]["lifekit.contract.ingress"] == "edge"
    assert SVC["labels"]["traefik.enable"] == "true"
    assert "ports" not in SVC
    assert SVC["expose"] == ["8099"]
    assert set(SVC["networks"]) == {"default", "lifekit-shared"}
    assert COMPOSE["networks"]["lifekit-shared"] == {"external": True}


def test_it_is_locked_down_like_every_other_service():
    assert SVC["read_only"] is True
    assert SVC["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in SVC["security_opt"]
    assert SVC["deploy"]["resources"]["limits"]["memory"]


def test_the_token_comes_from_the_env_file_by_name_never_as_a_value():
    env = SVC["environment"]
    assert env["LEARN_MCP_TOKEN"] == "${LEARN_MCP_TOKEN:-}"
    for key, value in env.items():
        assert not re.search(r"[0-9a-f]{32}", str(value)), key


def test_the_default_ranges_are_anthropics_published_outbound_ranges():
    assert SVC["environment"]["LEARN_MCP_ALLOW_CIDRS"].endswith(
        ":-160.79.104.0/21,153.61.208.0/20}"
    )


def test_the_vault_is_the_only_writable_mount_and_the_mirror_is_read_only():
    mounts = SVC["volumes"]
    assert mounts[0].endswith(":/vault")
    assert mounts[1].endswith(":/mirror:ro")
    assert len(mounts) == 2


def test_the_image_copies_the_skills_from_the_repo():
    assert SVC["build"]["additional_contexts"] == {"skills": "../../skills"}
    dockerfile = (SERVICE_DIR / "Dockerfile").read_text()
    for path in (
        "learning-record/record.py",
        "leetcode-daily/daily.py",
        "leetcode-daily/problems.json",
    ):
        assert path in dockerfile
        assert (REPO / "skills" / path).is_file()
    assert "USER 1000:1000" in dockerfile
