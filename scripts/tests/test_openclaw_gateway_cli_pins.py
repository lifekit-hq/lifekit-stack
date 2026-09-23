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


def test_dockerfile_installs_by_name_at_version_not_local_path():
    # claude-code's package.json carries a publish-guard `prepare` script that
    # npm runs for any from-source install (a local node_modules/ folder, or a
    # tarball packed from one) and fails outside Anthropic's release pipeline.
    # A registry install of the already-published tarball only runs
    # install/postinstall, so the Dockerfile must install `name@version`, not
    # a `./node_modules/...` path (verified against npm 10.9.8 / claude-code
    # 2.1.281: a local-path install/pack fails with "Direct publishing is not
    # allowed").
    assert "./node_modules/" not in DOCKERFILE
    for name in ("@anthropic-ai/claude-code", "clawhub", "@steipete/summarize"):
        assert re.search(
            rf'"{re.escape(name)}@\$\{{\w+\}}"', DOCKERFILE
        ), f"expected {name} installed as name@${{VERSION}} from package.json, not a hardcoded or local-path spec"


def test_dockerfile_reads_versions_from_package_json_not_hardcoded():
    # Single source of truth for pins: the Dockerfile must derive versions
    # from package.json at build time rather than duplicating a version
    # string, or a bump here could drift from the file Dependabot tracks.
    for version in PACKAGE_JSON["dependencies"].values():
        assert (
            version not in DOCKERFILE
        ), f"version {version!r} is hardcoded in the Dockerfile; read it from package.json instead"
