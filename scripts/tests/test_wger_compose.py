"""compose/wger/docker-compose.yml and its static gate (scripts/check-wger-compose.py).

Blocked case: an unpinned image, a missing persistent volume, a public port.
Allowed case: the real compose file. Compose's own `config` resolves short
volume and port syntax to the long form the checker reads; `normalise` below
does the same for the YAML file so the tests need no Docker.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_wger_compose", REPO / "scripts/check-wger-compose.py"
)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

COMPOSE = REPO / "compose/wger/docker-compose.yml"


def normalise(doc: dict) -> dict:
    out = copy.deepcopy(doc)
    declared = out.get("volumes") or {}
    for svc in out["services"].values():
        vols = []
        for v in svc.get("volumes") or []:
            src, target = v.split(":")[:2]
            kind = "volume" if src in declared else "bind"
            vols.append({"type": kind, "source": src, "target": target})
        svc["volumes"] = vols
        ports = []
        for p in svc.get("ports") or []:
            parts = p.split(":")
            ports.append({"host_ip": parts[0] if len(parts) == 3 else None})
        svc["ports"] = ports
    return out


@pytest.fixture
def real():
    return normalise(yaml.safe_load(COMPOSE.read_text()))


def test_real_compose_passes(real):
    assert gate.check(real) == []


@pytest.mark.parametrize("image", ["postgres", "postgres:latest", "postgres:17"])
def test_unpinned_image_is_blocked(real, image):
    real["services"]["db"]["image"] = image
    problems = gate.check(real)
    assert any(p.startswith("db: image") and "not pinned" in p for p in problems)


def test_missing_database_volume_is_blocked(real):
    real["services"]["db"]["volumes"] = []
    assert "db: /var/lib/postgresql/data is not on a named volume" in gate.check(real)


def test_bind_mounted_media_is_blocked(real):
    real["services"]["web"]["volumes"] = [
        {"type": "bind", "source": "/srv/x", "target": "/home/wger/media"}
    ]
    assert "web: /home/wger/media is not on a named volume" in gate.check(real)


def test_undeclared_volume_is_blocked(real):
    del real["volumes"]["postgres_data"]
    assert "db: volume postgres_data is not declared" in gate.check(real)


def test_public_port_is_blocked(real):
    real["services"]["nginx"]["ports"] = [{"host_ip": None}]
    assert any("not bound to 127.0.0.1" in p for p in gate.check(real))


def test_every_service_declares_the_platform_contract():
    doc = yaml.safe_load(COMPOSE.read_text())
    for name, svc in doc["services"].items():
        assert svc["labels"]["lifekit.contract"] == "none", name


@pytest.mark.parametrize(
    "service, key, ref",
    [
        ("web", "SECRET_KEY", "${WGER_SECRET_KEY:-}"),
        ("web", "DJANGO_DB_PASSWORD", "${WGER_DB_PASSWORD:-}"),
        ("db", "POSTGRES_PASSWORD", "${WGER_DB_PASSWORD:-}"),
    ],
)
def test_secret_settings_are_env_references_only(service, key, ref):
    doc = yaml.safe_load(COMPOSE.read_text())
    assert doc["services"][service]["environment"][key] == ref


def test_web_trusts_the_proxy_scheme_header():
    doc = yaml.safe_load(COMPOSE.read_text())
    assert (
        doc["services"]["web"]["environment"]["X_FORWARDED_PROTO_HEADER_SET"] == "True"
    )
