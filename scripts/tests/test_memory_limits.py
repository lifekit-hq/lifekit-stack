"""Every service in this stack's compose resolves to an explicit memory limit.

Without one a container reports the host's total RAM as its limit, so any
"memory as a share of its limit" signal cannot work for it. Runs the real
consumer (`docker compose config`) with every profile enabled, so on-demand and
retired services are covered too.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[2] / "compose"
# The platform project and the openclaw project, each from its own directory.
COMPOSE_DIRS = [COMPOSE, COMPOSE / "openclaw"]

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker compose unavailable"
)


def resolved_services(compose_dir: Path) -> dict[str, dict]:
    proc = subprocess.run(
        ["docker", "compose", "--profile", "*", "config", "--format", "json"],
        cwd=compose_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"docker compose config failed: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout)["services"]


@pytest.mark.parametrize("compose_dir", COMPOSE_DIRS, ids=lambda d: d.name)
def test_every_service_has_a_memory_limit(compose_dir: Path) -> None:
    services = resolved_services(compose_dir)
    assert services, "compose rendered no services"
    missing = []
    for name, svc in sorted(services.items()):
        limits = svc.get("deploy", {}).get("resources", {}).get("limits", {})
        limit = limits.get("memory") or svc.get("mem_limit")
        if not limit or int(limit) <= 0:
            missing.append(name)
    assert not missing, (
        f"services without a memory limit: {missing} - merge `<<: *policy` "
        "or set deploy.resources.limits.memory"
    )


def test_gateway_stop_grace_covers_openclaw_drain() -> None:
    """Docker must give the gateway OpenClaw's own 330s stop time.

    The default 10s SIGKILLs a gateway that is still draining work, before it
    releases its owner lease; the next container (new hostname) then waits out
    the 300s lease.
    """
    gateway = resolved_services(COMPOSE / "openclaw")["openclaw-gateway"]
    assert gateway["stop_grace_period"] == "5m30s"
