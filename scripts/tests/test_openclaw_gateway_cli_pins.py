"""Invariants of compose/openclaw-gateway/package.json (no Docker, no network).

lifekit-stack#146: the agent CLIs (claude-code, clawhub, summarize) are baked
into the gateway image at build time, pinned by this file, instead of
installed unpinned at every container start. A `^`/`~` range here would let
the image float again, defeating the point.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATEWAY_DIR = REPO / "compose/openclaw-gateway"
PACKAGE_JSON = json.loads((GATEWAY_DIR / "package.json").read_text())
DOCKERFILE = (GATEWAY_DIR / "Dockerfile").read_text()

EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def test_dependencies_are_pinned_to_exact_versions():
    deps = PACKAGE_JSON.get("dependencies", {})
    assert deps, "expected pinned CLI dependencies in package.json"
    for name, version in deps.items():
        assert EXACT_VERSION.match(
            version
        ), f"{name} is not exactly pinned: {version!r}"


def test_expected_clis_are_present():
    deps = set(PACKAGE_JSON.get("dependencies", {}))
    assert deps == {"@anthropic-ai/claude-code", "clawhub", "@steipete/summarize"}


def test_lockfile_matches_package_json():
    lock = json.loads((GATEWAY_DIR / "package-lock.json").read_text())
    root = lock["packages"][""]
    assert root["dependencies"] == PACKAGE_JSON["dependencies"]


def test_dockerfile_installs_from_the_pinned_package_json():
    assert "package.json" in DOCKERFILE
    assert "package-lock.json" in DOCKERFILE
    assert "npm ci" in DOCKERFILE
    assert "--allow-scripts=@anthropic-ai/claude-code" in DOCKERFILE
