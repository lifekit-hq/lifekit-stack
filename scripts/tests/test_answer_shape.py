"""ensure-answer-shape.sh puts Kit's "Answer shape (every reply)" block into the
personas that lack it, once, and touches nothing else.

The live config dir is a temp dir (OPENCLAW_CONFIG_DIR); no docker is involved.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ensure-answer-shape.sh"
KIT_SOUL = REPO / "defaults/agents/kit/workspace/SOUL.md"
READER_SOUL = REPO / "defaults/agents/reader/workspace/SOUL.md"
HEADING = "## Answer shape (every reply)"

PERSONA = (
    "# SOUL.md - Test\n\nYou are **Test**.\n\n- **Vibe:** plain.\n\n"
    "## Voice\n\n- Direct.\n- Never use an em dash.\n\n"
    "## What you care about\n\n- One thing.\n"
)


def section(text: str) -> str:
    lines = text.splitlines(keepends=True)
    start = lines.index(HEADING + "\n")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return "".join(lines[start:end]).strip("\n") + "\n"


def run(config: Path, *args: str, env_extra: dict | None = None):
    env = {**os.environ, "OPENCLAW_CONFIG_DIR": str(config), **(env_extra or {})}
    env.pop("DRY_RUN", None)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )


def put(config: Path, agent: str, text: str) -> Path:
    path = config / "agents" / agent / "workspace" / "SOUL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_reader_seed_carries_kits_block_exactly():
    assert section(READER_SOUL.read_text(encoding="utf-8")) == section(
        KIT_SOUL.read_text(encoding="utf-8")
    )


def test_block_keeps_the_plain_hyphen_rule():
    block = section(KIT_SOUL.read_text(encoding="utf-8"))
    assert "—" not in block and "–" not in block


def test_persona_with_the_section_is_left_byte_identical(tmp_path):
    soul = put(
        tmp_path,
        "health",
        PERSONA.replace("## Voice", section(KIT_SOUL.read_text()) + "\n## Voice"),
    )
    before = soul.read_bytes()
    res = run(tmp_path, "health")
    assert res.returncode == 0, res.stderr
    assert "already present" in res.stdout
    assert soul.read_bytes() == before


def test_persona_without_it_gets_one_copy_and_a_second_run_adds_nothing(tmp_path):
    soul = put(tmp_path, "social", PERSONA)
    res = run(tmp_path, "social")
    assert res.returncode == 0, res.stderr
    assert "inserted" in res.stdout
    after = soul.read_text(encoding="utf-8")
    assert after.count(HEADING) == 1
    assert section(after) == section(KIT_SOUL.read_text(encoding="utf-8"))
    # only an addition: removing the block gives back the original text
    assert after.replace(section(after) + "\n", "", 1) == PERSONA
    assert after.index(HEADING) < after.index("## Voice")
    again = run(tmp_path, "social")
    assert again.returncode == 0 and "already present" in again.stdout
    assert soul.read_text(encoding="utf-8") == after


def test_persona_without_a_voice_section_gets_the_block_appended(tmp_path):
    original = "# SOUL.md - Bare\n\nYou are **Bare**."  # no trailing newline
    soul = put(tmp_path, "learning", original)
    assert run(tmp_path, "learning").returncode == 0
    after = soul.read_text(encoding="utf-8")
    assert after.startswith(original + "\n\n" + HEADING)
    assert after.count(HEADING) == 1


def test_dry_run_prints_per_persona_and_writes_nothing(tmp_path):
    souls = {a: put(tmp_path, a, PERSONA) for a in ("health", "reader")}
    before = {a: p.read_bytes() for a, p in souls.items()}
    for res in (run(tmp_path, "--dry-run"), run(tmp_path, env_extra={"DRY_RUN": "1"})):
        assert res.returncode == 0, res.stderr
        assert "agent 'health': would insert" in res.stdout
        assert "agent 'reader': would insert" in res.stdout
        assert "agent 'learning': no SOUL.md" in res.stdout
        assert {a: p.read_bytes() for a, p in souls.items()} == before
        assert not list(souls["health"].parent.glob(".soul-*"))


def test_default_covers_the_four_personas_and_skips_missing_ones(tmp_path):
    souls = {
        a: put(tmp_path, a, PERSONA) for a in ("health", "learning", "social", "reader")
    }
    other = put(tmp_path, "kit", PERSONA)
    res = run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert all(
        p.read_text(encoding="utf-8").count(HEADING) == 1 for p in souls.values()
    )
    assert other.read_text(encoding="utf-8") == PERSONA  # not a covered persona
    empty = tmp_path / "empty"
    assert run(empty).returncode == 0


def test_missing_source_block_fails_and_leaves_the_file(tmp_path):
    soul = put(tmp_path, "health", PERSONA)
    bad = tmp_path / "src.md"
    bad.write_text("# no block here\n", encoding="utf-8")
    res = run(tmp_path, "health", env_extra={"ANSWER_SHAPE_SOURCE": str(bad)})
    assert res.returncode == 1
    assert soul.read_text(encoding="utf-8") == PERSONA
