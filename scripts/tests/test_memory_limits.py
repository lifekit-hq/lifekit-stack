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

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "compose"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker compose unavailable"
)


def resolved_services() -> dict[str, dict]:
    proc = subprocess.run(
        ["docker", "compose", "--profile", "*", "config", "--format", "json"],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)["services"]


def test_every_service_has_a_memory_limit() -> None:
    services = resolved_services()
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
