"""Behaviour of scripts/deploy-grafana-oidc.sh against a stubbed `tailscale`."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/deploy-grafana-oidc.sh"

TS_STUB = """#!/bin/sh
[ -n "$TS_FAIL" ] && exit 1
printf '%s' "$TS_JSON"
"""

NAME = "box.example.ts.net"
FULL = {
    "GRAFANA_OIDC_ENABLED": "true",
    "GRAFANA_OIDC_CLIENT_ID": "grafana-app",
    "GRAFANA_OIDC_CLIENT_SECRET": "s3cret",
}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "tailscale"
    stub.write_text(TS_STUB)
    stub.chmod(0o755)

    def _run(env_file=None, shell=None, tailnet=True):
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "TS_JSON": json.dumps({"Self": {"DNSName": NAME + "."}}),
        }
        if not tailnet:
            env["TS_FAIL"] = "1"
        if env_file is not None:
            path = tmp_path / "stack.env"
            path.write_text("".join(f"{k}={v}\n" for k, v in env_file.items()))
            env["ENV_FILE"] = str(path)
        env.update(shell or {})
        proc = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
        )
        out = dict(line.split("=", 1) for line in proc.stdout.splitlines())
        return proc.returncode, out, proc.stderr

    return _run


def test_unset_is_off_and_silent(run):
    code, out, err = run(env_file={})
    assert code == 0
    assert out == {"GRAFANA_OIDC_ENABLED": "false", "GRAFANA_OIDC_ONLY": "false"}
    assert err == ""


def test_enabled_derives_the_tailnet_issuer(run):
    code, out, _ = run(env_file=FULL)
    assert code == 0
    assert out["GRAFANA_OIDC_ENABLED"] == "true"
    assert out["GRAFANA_OIDC_ONLY"] == "false"
    assert out["GRAFANA_OIDC_ISSUER"] == f"https://{NAME}:3001/oidc"


def test_explicit_identity_endpoint_wins(run):
    code, out, _ = run(
        env_file={**FULL, "IDENTITY_ENDPOINT": "https://auth.example.org/"},
        tailnet=False,
    )
    assert code == 0
    assert out["GRAFANA_OIDC_ISSUER"] == "https://auth.example.org/oidc"


def test_only_is_honoured_when_enabled(run):
    code, out, _ = run(env_file={**FULL, "GRAFANA_OIDC_ONLY": "true"})
    assert code == 0
    assert out["GRAFANA_OIDC_ONLY"] == "true"


@pytest.mark.parametrize(
    "missing", ["GRAFANA_OIDC_CLIENT_ID", "GRAFANA_OIDC_CLIENT_SECRET"]
)
def test_missing_client_setting_leaves_it_off(run, missing):
    env = {**FULL, "GRAFANA_OIDC_ONLY": "true"}
    del env[missing]
    code, out, err = run(env_file=env)
    assert code == 1
    assert out == {"GRAFANA_OIDC_ENABLED": "false", "GRAFANA_OIDC_ONLY": "false"}
    assert missing in err


def test_undecidable_issuer_leaves_it_off(run):
    code, out, err = run(env_file=FULL, tailnet=False)
    assert code == 1
    assert out["GRAFANA_OIDC_ENABLED"] == "false"
    assert "issuer" in err


def test_only_without_enabled_never_hides_the_password_form(run):
    code, out, err = run(env_file={"GRAFANA_OIDC_ONLY": "true"})
    assert code == 1
    assert out["GRAFANA_OIDC_ONLY"] == "false"
    assert "GRAFANA_OIDC_ONLY" in err


def test_shell_env_beats_env_file(run):
    code, out, _ = run(env_file=FULL, shell={"GRAFANA_OIDC_ENABLED": "false"})
    assert code == 0
    assert out["GRAFANA_OIDC_ENABLED"] == "false"


# ─── the compose side: off by default, never hides the form on its own ───────


def grafana_service(extra_env: dict[str, str]) -> dict:
    import shutil

    if shutil.which("docker") is None:
        pytest.skip("docker compose unavailable")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GRAFANA_OIDC_", "IDENTITY_"))
    }
    env.update(extra_env)
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=REPO / "compose",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)["services"]["grafana"]


def test_compose_default_is_password_only():
    svc = grafana_service({})
    env = svc["environment"]
    assert env["GF_AUTH_GENERIC_OAUTH_ENABLED"] == "false"
    assert env["GF_AUTH_DISABLE_LOGIN_FORM"] == "false"
    assert env["GF_AUTH_OAUTH_AUTO_LOGIN"] == "false"
    assert "identity-oidc" in svc["networks"]
    assert "lifekit-shared" not in svc["networks"]


def test_compose_maps_roles_and_server_side_urls():
    env = grafana_service(
        {
            "GRAFANA_OIDC_ENABLED": "true",
            "GRAFANA_OIDC_ISSUER": "https://box.example.ts.net:3001/oidc",
        }
    )["environment"]
    assert env["GF_AUTH_GENERIC_OAUTH_AUTH_URL"].endswith(":3001/oidc/auth")
    assert env["GF_AUTH_GENERIC_OAUTH_TOKEN_URL"] == "http://logto:3001/oidc/token"
    assert env["GF_AUTH_GENERIC_OAUTH_API_URL"] == "http://logto:3001/oidc/me"
    assert "roles" in env["GF_AUTH_GENERIC_OAUTH_SCOPES"].split()
    assert (
        "'admin') && 'Admin' || 'Viewer'"
        in (env["GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH"])
    )


def test_logto_and_grafana_share_identity_oidc_but_logto_is_off_lifekit_shared():
    import shutil

    if shutil.which("docker") is None:
        pytest.skip("docker compose unavailable")
    env = {
        **os.environ,
        "LOGTO_DB_PASSWORD": "x",
        "IDENTITY_ENDPOINT": "https://box.example.ts.net:3001",
        "IDENTITY_ADMIN_ENDPOINT": "https://box.example.ts.net:3002",
    }
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=REPO / "compose/identity",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {proc.stderr.strip()[:200]}")
    logto = json.loads(proc.stdout)["services"]["logto"]
    assert "identity-oidc" in logto["networks"]
    assert "lifekit-shared" not in logto["networks"]
