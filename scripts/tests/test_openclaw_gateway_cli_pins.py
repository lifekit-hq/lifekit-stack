"""Invariants of compose/openclaw-gateway/package.json and its Dockerfile install.

lifekit-stack#146: the agent CLIs (claude-code, clawhub, summarize) are baked
into the gateway image at build time, pinned by package.json, instead of
installed unpinned at every container start. A `^`/`~` range there would let
the image float again, defeating the point.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATEWAY_DIR = REPO / "compose/openclaw-gateway"
PACKAGE_JSON = json.loads((GATEWAY_DIR / "package.json").read_text())
DEPENDENCIES = PACKAGE_JSON.get("dependencies", {})

EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def _install_run_commands() -> list[str]:
    """Shell commands (split on &&) of the Dockerfile RUN that does the npm install."""
    lines = (GATEWAY_DIR / "Dockerfile").read_text().splitlines()
    instructions: list[str] = []
    current = ""
    for line in lines:
        if not current and not line.strip().startswith("RUN "):
            continue
        stripped = line.rstrip()
        continued = stripped.endswith("\\")
        current += " " + stripped.removesuffix("\\").strip()
        if not continued:
            instructions.append(current.strip())
            current = ""
    (run,) = [i for i in instructions if "npm install" in i]
    return [c.strip() for c in run.split("&&")]


def test_dependencies_are_pinned_to_exact_versions():
    assert DEPENDENCIES, "expected pinned CLI dependencies in package.json"
    for name, version in DEPENDENCIES.items():
        assert EXACT_VERSION.match(version), (
            f"{name} is not exactly pinned: {version!r}"
        )


def test_expected_clis_are_present():
    assert set(DEPENDENCIES) == {
        "@anthropic-ai/claude-code",
        "clawhub",
        "@steipete/summarize",
    }


def test_install_reads_every_package_json_dependency_into_the_install_args():
    commands = _install_run_commands()
    variables: dict[str, str] = {}
    for cmd in commands:
        m = re.match(
            r"""(\w+)="\$\(node -p "require\('\./package\.json'\)\.dependencies\['([^']+)'\]"\)"$""",
            cmd,
        )
        if m:
            variables[m.group(2)] = m.group(1)
    install = next(c for c in commands if c.startswith("npm install"))
    for name in DEPENDENCIES:
        assert name in variables, f"{name} is not read from package.json"
        assert f'"{name}@${{{variables[name]}}}"' in install, (
            f"{name} is not installed as name@version from its extracted variable"
        )


def test_install_allows_claude_code_postinstall_script():
    install = next(c for c in _install_run_commands() if c.startswith("npm install"))
    assert "--allow-scripts=@anthropic-ai/claude-code" in install.split()
