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
        svc["networks"] = {n: None for n in svc.get("networks") or []}
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
        ("web", "JWT_PRIVATE_KEY", "${WGER_JWT_PRIVATE_KEY:-}"),
        ("web", "JWT_PUBLIC_KEY", "${WGER_JWT_PUBLIC_KEY:-}"),
        (
            "web",
            "PS_STORAGE_PG_URI",
            "postgres://powersync_storage:${WGER_POWERSYNC_STORAGE_PASSWORD:-}@db:5432/wger",
        ),
        (
            "powersync",
            "PS_STORAGE_PG_URI",
            "postgres://powersync_storage:${WGER_POWERSYNC_STORAGE_PASSWORD:-}@db:5432/wger",
        ),
        (
            "powersync",
            "PS_DATABASE_URI",
            "postgres://wger:${WGER_DB_PASSWORD:-}@db:5432/wger",
        ),
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


def test_only_nginx_is_on_the_gateway_network(real):
    assert set(real["services"]["nginx"]["networks"]) == {
        "default",
        "wger-gateway",
        "powersync",
    }
    assert gate.check(real) == []
    for other in ("web", "db", "cache"):
        assert "wger-gateway" not in real["services"][other]["networks"]
    for svc in real["services"].values():
        assert "lifekit-shared" not in svc["networks"]


def test_the_gateway_is_the_only_openclaw_service_on_the_wger_network():
    doc = yaml.safe_load((REPO / "compose/openclaw/docker-compose.yml").read_text())
    on_it = [
        name
        for name, svc in doc["services"].items()
        if "wger-gateway" in (svc.get("networks") or {})
    ]
    assert on_it == ["openclaw-gateway"]
    assert doc["networks"]["wger-gateway"] == {"external": True}


@pytest.mark.parametrize("service", ["web", "db", "cache", "powersync"])
def test_another_service_on_the_gateway_network_is_blocked(real, service):
    real["services"][service]["networks"]["wger-gateway"] = None
    assert f"{service}: must not join wger-gateway (only nginx may)" in gate.check(real)


@pytest.mark.parametrize("service", ["nginx", "web", "db", "cache", "powersync"])
def test_any_service_on_the_wide_shared_network_is_blocked(real, service):
    real["services"][service]["networks"]["lifekit-shared"] = None
    assert f"{service}: must not join lifekit-shared" in gate.check(real)


def test_nginx_off_the_gateway_network_is_blocked(real):
    real["services"]["nginx"]["networks"].pop("wger-gateway")
    assert "nginx: must join wger-gateway" in gate.check(real)


@pytest.mark.parametrize("declared", [None, {}, {"external": False}])
def test_a_gateway_network_that_is_not_external_is_blocked(real, declared):
    if declared is None:
        real["networks"].pop("wger-gateway")
    else:
        real["networks"]["wger-gateway"] = declared
    assert "network wger-gateway must be declared external" in gate.check(real)


# --- the app sign-in keypair -------------------------------------------------

SECRETS = {
    "SECRET_KEY": "s",
    "DJANGO_DB_PASSWORD": "p",
    "JWT_PRIVATE_KEY": "a",
    "JWT_PUBLIC_KEY": "b",
    "PS_STORAGE_PG_URI": "postgres://powersync_storage:pw@db:5432/wger",
}


def test_secrets_set_passes_the_required_check(real):
    real["services"]["web"]["environment"].update(SECRETS)
    real["services"]["powersync"]["environment"].update(SECRETS)
    assert gate.check(real, require_secrets=True) == []


@pytest.mark.parametrize("key", ["JWT_PRIVATE_KEY", "JWT_PUBLIC_KEY"])
def test_missing_jwt_key_fails_the_required_check(real, key):
    env = real["services"]["web"]["environment"]
    env.update(SECRETS)
    env[key] = ""  # what compose resolves an unset ${WGER_JWT_...:-} to
    assert f"web: {key} is empty (docs/secrets.md, WGER_ rows)" in gate.check(
        real, require_secrets=True
    )


@pytest.mark.parametrize("service", ["web", "powersync"])
def test_empty_storage_password_fails_the_required_check(real, service):
    real["services"]["web"]["environment"].update(SECRETS)
    real["services"]["powersync"]["environment"].update(SECRETS)
    # what compose resolves an unset ${WGER_POWERSYNC_STORAGE_PASSWORD:-} to
    real["services"][service]["environment"]["PS_STORAGE_PG_URI"] = (
        "postgres://powersync_storage:@db:5432/wger"
    )
    assert (
        f"{service}: PS_STORAGE_PG_URI has no password "
        "(WGER_POWERSYNC_STORAGE_PASSWORD, docs/secrets.md)"
    ) in gate.check(real, require_secrets=True)


def test_secrets_are_not_required_by_default(real):
    assert gate.check(real) == []


# --- PowerSync (the app's offline sync service) -----------------------------


def test_powersync_service_is_pinned_and_on_its_own_network_only(real):
    svc = real["services"]["powersync"]
    assert set(svc["networks"]) == {"backend", "powersync"}
    assert set(real["services"]["nginx"]["networks"]) >= {"powersync"}
    assert "ports" not in svc or svc["ports"] == []
    assert gate.check(real) == []


def test_powersync_missing_is_blocked(real):
    del real["services"]["powersync"]
    assert "powersync: service is missing" in gate.check(real)


def test_powersync_on_default_network_is_blocked(real):
    real["services"]["powersync"]["networks"]["default"] = None
    assert "powersync: must not join default (no egress)" in gate.check(real)


@pytest.mark.parametrize("service", ["web", "db", "cache"])
def test_another_service_on_the_sync_network_is_blocked(real, service):
    real["services"][service]["networks"]["powersync"] = None
    assert (
        f"{service}: must not join powersync (only nginx and powersync may)"
        in gate.check(real)
    )


def test_nginx_off_the_sync_network_is_blocked(real):
    real["services"]["nginx"]["networks"].pop("powersync")
    assert "nginx: must join powersync (the /ps/ proxy)" in gate.check(real)


def test_sync_network_must_be_internal(real):
    real["networks"]["powersync"] = {}
    assert "network powersync must be declared internal" in gate.check(real)


def test_db_without_logical_wal_is_blocked(real):
    real["services"]["db"].pop("command")
    assert any("wal_level=logical" in p for p in gate.check(real))


def test_nginx_proxies_the_powersync_path():
    conf = (REPO / "compose/wger/nginx.conf").read_text()
    assert "location /ps/ {" in conf
    assert "http://powersync:8080" in conf
    assert "rewrite ^/ps/(.*)$ /$1 break;" in conf


def test_web_hands_the_app_the_nginx_path():
    env = yaml.safe_load(COMPOSE.read_text())["services"]["web"]["environment"]
    assert env["POWERSYNC_URL_PATH"] == "ps"
    assert "POWERSYNC_URL" not in env  # SITE_URL/ps/ is the nginx path


def _jwk_pair():
    """A keypair shaped like `manage.py generate-jwt-keys` prints (wger 2.7.0):
    each half a single-line base64url JSON JWK."""
    import json
    from base64 import urlsafe_b64encode

    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = json.loads(RSAAlgorithm.to_jwk(key))
    priv.update(alg="RS256", kid="wger")
    pub = {k: priv[k] for k in ("kty", "n", "e", "alg", "kid")}

    def enc(d):
        return urlsafe_b64encode(json.dumps(d).encode()).decode()

    return enc(priv), enc(pub)


def _jwk_b64_to_pem(value: str) -> bytes:
    """What wger's settings do with the env value (jwk_b64_to_pem)."""
    import json
    from base64 import urlsafe_b64decode

    from cryptography.hazmat.primitives import serialization
    from jwt.algorithms import RSAAlgorithm

    key = RSAAlgorithm.from_jwk(json.loads(urlsafe_b64decode(value)))
    if hasattr(key, "private_numbers"):
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def test_rendered_keypair_loads_as_pem_and_round_trips(tmp_path):
    pytest.importorskip("jwt")
    pytest.importorskip("cryptography")
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv, pub = _jwk_pair()
    # The render writes `NAME=value` lines (sops dotenv -> stack.env); compose
    # reads them back as one line each with no quoting.
    env_file = tmp_path / "stack.env"
    env_file.write_text(f"WGER_JWT_PRIVATE_KEY={priv}\nWGER_JWT_PUBLIC_KEY={pub}\n")
    rendered = dict(
        line.split("=", 1) for line in env_file.read_text().splitlines() if line
    )
    assert all("\n" not in v and v == v.strip() for v in rendered.values())

    private_pem = _jwk_b64_to_pem(rendered["WGER_JWT_PRIVATE_KEY"])
    public_pem = _jwk_b64_to_pem(rendered["WGER_JWT_PUBLIC_KEY"])
    assert isinstance(
        serialization.load_pem_private_key(private_pem, password=None),
        rsa.RSAPrivateKey,
    )
    assert isinstance(serialization.load_pem_public_key(public_pem), rsa.RSAPublicKey)

    token = jwt.encode({"sub": "owner"}, private_pem, algorithm="RS256")
    assert jwt.decode(token, public_pem, algorithms=["RS256"])["sub"] == "owner"
    other_pub = _jwk_b64_to_pem(_jwk_pair()[1])
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, other_pub, algorithms=["RS256"])
