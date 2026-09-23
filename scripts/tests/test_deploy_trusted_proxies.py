"""Behaviour of scripts/deploy-trusted-proxies.sh, run as a subprocess against
a stubbed `docker` on PATH.

Since OpenClaw 2026.9.x the gateway rejects proxied requests with
proxy_attribution_required unless the proxy's source address is listed in
gateway.trustedProxies. The value is host-derived (Docker assigns the compose
project's default network gateway address at network creation), so it can't
live in compose/openclaw-gateway/platform.patch.json — it's set from
deploy.sh's onboard block by calling this script, on every deploy, and the
script itself skips once the key already holds a value.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/deploy-trusted-proxies.sh"

DOCKER_STUB = """#!/bin/sh
log="$DOCKER_CALL_LOG"
printf '%s\\n' "$*" >> "$log"
case "$1 $2" in
  "network inspect")
    printf '%s' "$NETWORK_INSPECT_OUTPUT"
    [ -n "$NETWORK_INSPECT_OUTPUT" ]
    exit $?
    ;;
  "compose --env-file")
    case " $* " in
      *" config get "*)
        printf '%s' "$CONFIG_GET_OUTPUT"
        ;;
    esac
    exit "${COMPOSE_RUN_EXIT:-0}"
    ;;
esac
exit 1
"""


@pytest.fixture
def docker_stub(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)
    call_log = tmp_path / "docker-calls.log"
    call_log.write_text("")
    return {"bin_dir": bin_dir, "call_log": call_log}


def write_openclaw_config(tmp_path, trusted_proxies=None):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    data = {"gateway": {}}
    if trusted_proxies is not None:
        data["gateway"]["trustedProxies"] = trusted_proxies
    (config_dir / "openclaw.json").write_text(json.dumps(data))
    return config_dir


def run(env_file, compose_file, config_dir, docker_stub, extra_env=None):
    env = {
        **os.environ,
        "PATH": f"{docker_stub['bin_dir']}:{os.environ['PATH']}",
        "DOCKER_CALL_LOG": str(docker_stub["call_log"]),
        "ENV_FILE": str(env_file),
        "COMPOSE_FILE": str(compose_file),
        "OPENCLAW_CONFIG_DIR": str(config_dir),
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def test_sets_trusted_proxies_from_the_derived_network_gateway(tmp_path, docker_stub):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    compose_file = tmp_path / "compose" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("")
    config_dir = write_openclaw_config(tmp_path)

    result = run(
        env_file,
        compose_file,
        config_dir,
        docker_stub,
        extra_env={
            "COMPOSE_PROJECT_NAME": "lifekit-stack",
            "NETWORK_INSPECT_OUTPUT": "172.30.0.1",
        },
    )

    assert result.returncode == 0, result.stderr
    calls = docker_stub["call_log"].read_text().splitlines()
    assert (
        calls[0]
        == "network inspect lifekit-stack_default --format {{(index .IPAM.Config 0).Gateway}}"
    )
    assert (
        calls[1]
        == f"compose --env-file {env_file} -f {compose_file} run --rm --no-deps "
        f"--entrypoint openclaw openclaw-gateway config set gateway.trustedProxies "
        f'["172.30.0.1"] --strict-json'
    )
    address = json.loads(
        calls[1].split("gateway.trustedProxies ", 1)[1].rsplit(" --strict-json", 1)[0]
    )
    assert address == ["172.30.0.1"]


def test_derives_the_project_name_from_the_compose_file_directory_when_unset(
    tmp_path, docker_stub
):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    compose_file = tmp_path / "myproject" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("")
    config_dir = write_openclaw_config(tmp_path)

    result = run(
        env_file,
        compose_file,
        config_dir,
        docker_stub,
        extra_env={"NETWORK_INSPECT_OUTPUT": "172.30.0.1"},
    )

    assert result.returncode == 0, result.stderr
    calls = docker_stub["call_log"].read_text().splitlines()
    assert calls[0].startswith("network inspect myproject_default ")


def test_aborts_and_does_not_set_when_the_network_lookup_is_empty(
    tmp_path, docker_stub
):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    compose_file = tmp_path / "compose" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("")
    config_dir = write_openclaw_config(tmp_path)

    result = run(
        env_file,
        compose_file,
        config_dir,
        docker_stub,
        extra_env={
            "COMPOSE_PROJECT_NAME": "lifekit-stack",
            "NETWORK_INSPECT_OUTPUT": "",
        },
    )

    assert result.returncode == 1
    assert "gateway.trustedProxies was not set" in result.stderr
    calls = docker_stub["call_log"].read_text().splitlines()
    assert len(calls) == 1
    assert calls[0].startswith("network inspect lifekit-stack_default ")


def test_aborts_when_the_config_set_call_fails(tmp_path, docker_stub):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    compose_file = tmp_path / "compose" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("")
    config_dir = write_openclaw_config(tmp_path)

    result = run(
        env_file,
        compose_file,
        config_dir,
        docker_stub,
        extra_env={
            "COMPOSE_PROJECT_NAME": "lifekit-stack",
            "NETWORK_INSPECT_OUTPUT": "172.30.0.1",
            "COMPOSE_RUN_EXIT": "1",
        },
    )

    assert result.returncode == 1


def test_skips_when_trusted_proxies_is_already_set(tmp_path, docker_stub):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    compose_file = tmp_path / "compose" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("")
    config_dir = write_openclaw_config(tmp_path, trusted_proxies=["10.0.0.1"])

    result = run(
        env_file,
        compose_file,
        config_dir,
        docker_stub,
        extra_env={"COMPOSE_PROJECT_NAME": "lifekit-stack"},
    )

    assert result.returncode == 0, result.stderr
    assert "already set" in result.stdout
    assert docker_stub["call_log"].read_text() == ""


def test_retries_when_trusted_proxies_is_present_but_empty(tmp_path, docker_stub):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    compose_file = tmp_path / "compose" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("")
    config_dir = write_openclaw_config(tmp_path, trusted_proxies=[])

    result = run(
        env_file,
        compose_file,
        config_dir,
        docker_stub,
        extra_env={
            "COMPOSE_PROJECT_NAME": "lifekit-stack",
            "NETWORK_INSPECT_OUTPUT": "172.30.0.1",
        },
    )

    assert result.returncode == 0, result.stderr
    calls = docker_stub["call_log"].read_text().splitlines()
    assert calls[0].startswith("network inspect lifekit-stack_default ")


def test_falls_back_to_config_get_when_the_config_file_is_missing(
    tmp_path, docker_stub
):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    compose_file = tmp_path / "compose" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("")
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    result = run(
        env_file,
        compose_file,
        config_dir,
        docker_stub,
        extra_env={
            "COMPOSE_PROJECT_NAME": "lifekit-stack",
            "NETWORK_INSPECT_OUTPUT": "172.30.0.1",
        },
    )

    assert result.returncode == 0, result.stderr
    calls = docker_stub["call_log"].read_text().splitlines()
    assert calls[0].startswith("compose --env-file")
    assert calls[0].endswith("config get gateway.trustedProxies")
    assert calls[1].startswith("network inspect lifekit-stack_default ")


def test_aborts_when_neither_the_config_file_nor_config_get_is_readable(
    tmp_path, docker_stub
):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    compose_file = tmp_path / "compose" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("")
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    result = run(
        env_file,
        compose_file,
        config_dir,
        docker_stub,
        extra_env={
            "COMPOSE_PROJECT_NAME": "lifekit-stack",
            "COMPOSE_RUN_EXIT": "1",
        },
    )

    assert result.returncode == 1
    assert "Could not read gateway.trustedProxies" in result.stderr
