"""Every skills/<dir> is installed by some script, and its vault paths exist.

Install = a scripts/ensure-*.sh or scripts/deploy-openclaw.sh names `skills/<dir>`.
Vault paths = `~/memory/<path>` mentions in a skill's files, resolved against the vault
copy on the host (`/srv/memory`, or `$LIFEKIT_VAULT_COPY`); skipped when no copy is there.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# Documented as not installed by the stack (skills/README.md, docs/runbook.md "not on Kit's path").
NOT_INSTALLED_OK = {"lifekit-router"}
VAULT_REF = re.compile(r"~/memory/([A-Za-z0-9_][A-Za-z0-9_./-]*[A-Za-z0-9_])")
TEXT = {".md", ".py", ".sh", ".js", ".mjs", ".yaml", ".yml", ".json"}


def uninstalled(skills: Path, scripts: Path) -> list[str]:
    text = "".join(
        p.read_text()
        for p in [*scripts.glob("ensure-*.sh"), scripts / "deploy-openclaw.sh"]
        if p.exists()
    )
    return sorted(
        d.name
        for d in skills.iterdir()
        if d.is_dir()
        and d.name not in NOT_INSTALLED_OK
        and f"skills/{d.name}" not in text
    )


def missing_vault_paths(skills: Path, vault: Path) -> list[str]:
    out = []
    for f in sorted(skills.rglob("*")):
        if not f.is_file() or f.suffix not in TEXT:
            continue
        for ref in sorted(set(VAULT_REF.findall(f.read_text()))):
            if not (vault / ref.rstrip("/.")).exists():
                out.append(f"{f.relative_to(skills)}: ~/memory/{ref}")
    return out


def test_blocked_case_uninstalled_skill_and_dangling_vault_path(tmp_path):
    skills, scripts, vault = (
        tmp_path / "skills",
        tmp_path / "scripts",
        tmp_path / "vault",
    )
    for d in (skills / "wired", skills / "orphan", scripts, vault / "system"):
        d.mkdir(parents=True)
    (scripts / "ensure-wired.sh").write_text("install skills/wired\n")
    (skills / "wired/SKILL.md").write_text(
        "reads ~/memory/system/gone.md and ~/memory/system/ok.md\n"
    )
    (vault / "system/ok.md").write_text("x")
    assert uninstalled(skills, scripts) == ["orphan"]
    assert missing_vault_paths(skills, vault) == [
        "wired/SKILL.md: ~/memory/system/gone.md"
    ]


def test_allowed_case_synthetic(tmp_path):
    skills, scripts, vault = (
        tmp_path / "skills",
        tmp_path / "scripts",
        tmp_path / "vault",
    )
    for d in (skills / "wired", scripts, vault / "system"):
        d.mkdir(parents=True)
    (scripts / "ensure-wired.sh").write_text("install skills/wired\n")
    (skills / "wired/SKILL.md").write_text("reads ~/memory/system/ok.md\n")
    (vault / "system/ok.md").write_text("x")
    assert uninstalled(skills, scripts) == []
    assert missing_vault_paths(skills, vault) == []


def test_every_skill_in_the_repo_is_installed():
    assert uninstalled(REPO / "skills", REPO / "scripts") == []


def test_skill_vault_paths_exist_in_the_vault_copy():
    vault = Path(os.environ.get("LIFEKIT_VAULT_COPY", "/srv/memory"))
    if not vault.is_dir():
        pytest.skip(f"no vault copy at {vault}")
    assert missing_vault_paths(REPO / "skills", vault) == []
