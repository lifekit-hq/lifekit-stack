"""compose/study-door/docker-compose.yml: the study panel's door stays off the
host's ports, inside the platform contract, and free of secret values.

Plain YAML, no Docker: the file is small and its shape is what matters.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO / "compose/study-door/docker-compose.yml"
COMPOSE = yaml.safe_load(COMPOSE_FILE.read_text())
DOOR = COMPOSE["services"]["study-door"]

_spec = importlib.util.spec_from_file_location(
    "platform_contract", REPO / "scripts/platform-contract.py"
)
contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract)


def test_the_project_is_its_own_and_has_exactly_one_service():
    assert COMPOSE["name"] == "study-door"
    assert list(COMPOSE["services"]) == ["study-door"]


def test_the_door_passes_the_static_contract_check():
    assert contract.static_findings("study-door", DOOR) == []


def test_the_door_is_routed_through_the_edge_and_publishes_no_host_port():
    labels = DOOR["labels"]
    assert labels["lifekit.contract.ingress"] == "edge"
    assert labels["traefik.enable"] == "true"
    assert "ports" not in DOOR
    assert DOOR["expose"] == ["8098"]


def test_the_door_reaches_the_edge_and_the_gateway_over_lifekit_shared_only():
    assert set(DOOR["networks"]) == {"default", "lifekit-shared"}
    assert COMPOSE["networks"]["lifekit-shared"] == {"external": True}
    assert DOOR["environment"]["STUDY_DOOR_GATEWAY_URL"].endswith(
        ":-http://openclaw-edge:18893}"
    )


def test_the_door_is_locked_down_like_every_other_service():
    assert DOOR["read_only"] is True
    assert DOOR["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in DOOR["security_opt"]
    assert DOOR["deploy"]["resources"]["limits"]["memory"]
    assert DOOR["healthcheck"]["test"][0] == "CMD-SHELL"


def test_secrets_come_from_the_env_file_by_name_never_as_values():
    env = DOOR["environment"]
    for name in ("STUDY_DOOR_TOKEN", "STUDY_DOOR_EDGE_SECRET"):
        assert env[name] == f"${{{name}:-}}"
    for key, value in env.items():
        assert not re.search(r"[0-9a-f]{32}", str(value)), key


def test_the_audit_spool_is_a_named_volume_and_holds_no_page():
    assert DOOR["volumes"] == ["study-door-spool:/spool"]
    assert "study-door-spool" in COMPOSE["volumes"]
