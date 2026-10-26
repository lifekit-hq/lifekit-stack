"""Behaviour of scripts/intake-to-vault against a fake vault harness and a fake intake dir.

What this file pins: only an accepted report is filed; it becomes exactly one
source page (report and transcript verbatim), one index regeneration and one
log line however often the job runs; the line goes to the project the report's
Project: line names, else to the cross-project log; a page the vault lint
refuses is removed again; and nothing is written while the model pass runs.
All fixtures are synthetic.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/intake-to-vault"
SCRIPT = DIR / "intake_to_vault.py"
INSTALLER = DIR / "install-intake-to-vault.sh"

spec = importlib.util.spec_from_file_location("intake_to_vault", SCRIPT)
itv = importlib.util.module_from_spec(spec)
sys.modules["intake_to_vault"] = itv
spec.loader.exec_module(itv)

# Stand-ins for the vault's bin/ gates. Each records its call in calls.log.
LINT = r"""#!/bin/sh
echo "lint $*" >> "$VAULT/calls.log"
[ -f "$VAULT/lint-out" ] || exit 0
cat "$VAULT/lint-out"; exit 1
"""
INDEX = r"""#!/bin/sh
echo "index $*" >> "$VAULT/calls.log"
{ echo "# Index"; for f in "$VAULT"/sources/*.md; do
  [ -e "$f" ] || continue; s=$(basename "$f" .md); echo "- [[sources/$s|$s]] - page"; done; } > "$VAULT/index.md"
echo "index.md regenerated"
"""
LOG = r"""#!/bin/sh
echo "log $*" >> "$VAULT/calls.log"
f="$VAULT/log.md"; pre=""
while getopts p:k: o; do case $o in
  p) f="$VAULT/projects/$OPTARG/log.md"; [ -f "$f" ] || { echo "no project $OPTARG" >&2; exit 1; } ;;
  k) pre="$OPTARG | " ;;
esac; done
shift $((OPTIND - 1))
echo "## [2026-10-26] $pre$1" >> "$f"
[ -f "$VAULT/log-fail" ] && { echo "commit failed" >&2; exit 1; }
exit 0
"""

TRANSCRIPT = """Video: https://example.com/reel/{key}/
Title: Synthetic clip {key}
Author: synthetic-author
Duration: 42
Language: en
Transcript: whisper base
Caption: none

Start every script with set -euo pipefail. ``` a fence inside ```.
"""


def report(
    key: str = "AbcSynth_1",
    verdict: str = "accept",
    project: str = "alpha (deploy scripts)",
    title: str | None = None,
    next_step: str = "add a lint rule.",
    url: str | None = None,
) -> str:
    tail = {
        "accept": f"Project: {project}",
        "park": "Recheck: 2026-12-01",
        "drop": "Reason: unsupported - no primary source",
    }[verdict]
    return f"""Title: {title or f"Synthetic clip {key}"}
Kind: video | Category: dev-tooling | Origin: firstmate
Source: {url or f"https://example.com/reel/{key}/"}
Evaluated: 2026-10-25 | Rubric: video-eval v1 (claim-check)
Gist: A synthetic claim about shell scripts.

## What it is
A synthetic fixture ([[not-a-page]]).

## Verdict
Tests: evidence pass, cost pass, goal-trace pass, no-overengineering pass
Verdict: {verdict}
{tail}
Next step: {next_step}

## Confidence
High.
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault, intake = tmp_path / "vault", tmp_path / "intake"
    for d in (
        "bin",
        "sources",
        "projects/alpha",
        "projects/alpha-beta",
        "projects/gamma",
    ):
        (vault / d).mkdir(parents=True)
    for p in ("alpha", "alpha-beta"):
        (vault / f"projects/{p}/log.md").write_text("# log\n")
    (vault / "log.md").write_text("# log\n")
    (vault / "index.md").write_text("# Index\n")
    for name, body in (("lint.sh", LINT), ("index.sh", INDEX), ("log.sh", LOG)):
        (vault / "bin" / name).write_text(body)
    (intake / "reports").mkdir(parents=True)
    (intake / "transcripts").mkdir()
    monkeypatch.setenv("INTAKE_TO_VAULT_VAULT", str(vault))
    monkeypatch.setenv("INTAKE_TO_VAULT_INTAKE", str(intake))
    return vault, intake


def add(intake: Path, key: str, text: str, transcript: bool = True) -> None:
    (intake / "reports" / f"ig-{key}.v1.md").write_text(text)
    if transcript:
        (intake / "transcripts" / f"ig-{key}.txt").write_text(
            TRANSCRIPT.format(key=key)
        )


def calls(vault: Path, kind: str) -> list[str]:
    path = vault / "calls.log"
    lines = path.read_text().splitlines() if path.exists() else []
    return [line for line in lines if line.startswith(kind + " ")]


def page(vault: Path, key: str = "abcsynth-1") -> Path:
    return vault / f"sources/2026-10-25-video-ig-{key}-v1.md"


def test_an_accepted_report_becomes_one_page_one_index_line_and_one_log_line(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report())
    add(intake, "ParkSynth", report("ParkSynth", "park"))
    add(intake, "DropSynth", report("DropSynth", "drop"))

    assert itv.main() == 0

    text = page(vault).read_text()
    head = text.split("\n---\n", 1)[0]
    assert 'title: "Synthetic clip AbcSynth_1"' in head
    assert "url: https://example.com/reel/AbcSynth_1/" in head
    assert 'author: "synthetic-author"' in head
    assert "captured: 2026-10-25" in head and "type: other" in head
    # Both verbatim, inside fences the content cannot close.
    assert report() in text and "Start every script with set -euo pipefail." in text
    assert "````text\nVideo: https://example.com/reel/AbcSynth_1/" in text
    assert sorted(p.name for p in (vault / "sources").iterdir()) == [page(vault).name]
    stem = "sources/2026-10-25-video-ig-abcsynth-1-v1"
    assert f"[[{stem}|" in (vault / "index.md").read_text()
    line = (vault / "projects/alpha/log.md").read_text().splitlines()[-1]
    assert line.startswith(
        "## [2026-10-26] video intake accepted: Synthetic clip AbcSynth_1"
    )
    assert line.endswith(f"[[{stem}|report]]") and "next: add a lint rule." in line
    assert (vault / "log.md").read_text() == "# log\n"
    assert calls(vault, "lint") == ["lint --changed --tsv"]


def test_a_second_run_writes_nothing(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report())
    assert itv.main() == 0
    before = page(vault).read_text(), (vault / "projects/alpha/log.md").read_text()
    (vault / "calls.log").unlink()

    assert itv.main() == 0

    assert not (vault / "calls.log").exists()
    assert (
        page(vault).read_text(),
        (vault / "projects/alpha/log.md").read_text(),
    ) == before


def test_a_run_that_stopped_after_the_page_completes_the_index_and_the_log(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report())
    (vault / "bin/index.sh").write_text("#!/bin/sh\nexit 2\n")
    assert itv.main() == 1
    assert (
        page(vault).exists()
        and "[[" not in (vault / "projects/alpha/log.md").read_text()
    )

    (vault / "bin/index.sh").write_text(INDEX)
    assert itv.main() == 0
    assert (vault / "projects/alpha/log.md").read_text().count("[[sources/") == 1


@pytest.mark.parametrize(
    ("value", "owner"),
    [
        ("alpha", "alpha"),
        ("alpha (deploy scripts)", "alpha"),
        ("Alpha-Beta: the dashboard", "alpha-beta"),
        ("alpha-beta", "alpha-beta"),
        ("alphabet", None),
        ("alpha-gamma", None),
        ("gamma", None),  # a project dir without log.md
        ("a home lab idea", None),
        ("", None),
    ],
)
def test_the_owning_project_is_only_a_named_vault_project(env, value, owner):
    vault, _ = env
    assert itv.owning_project(vault, value) == owner


def test_no_matching_project_goes_to_the_cross_project_log(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report(project="a home lab idea"))
    assert itv.main() == 0
    line = (vault / "log.md").read_text().splitlines()[-1]
    assert line.startswith(
        "## [2026-10-26] intake | video accepted for a home lab idea: "
    )
    assert calls(vault, "log")[0].startswith("log -k intake ")


def test_a_page_the_vault_lint_refuses_is_removed_and_the_rest_still_filed(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report())
    add(intake, "ZzzSynth", report("ZzzSynth"))
    bad = "sources/2026-10-25-video-ig-abcsynth-1-v1.md"
    (vault / "lint-out").write_text(
        f"high\tbroken\t{bad}\tbad frontmatter\nmedium\tother\tdomains/x.md\tnot ours\n"
    )

    assert itv.main() == 1

    assert not page(vault).exists()
    assert page(vault, "zzzsynth").exists()
    assert "abcsynth" not in (vault / "projects/alpha/log.md").read_text()
    assert "zzzsynth" in (vault / "projects/alpha/log.md").read_text()


def test_a_lint_that_cannot_run_refuses_the_page(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report())
    (vault / "bin/lint.sh").write_text("#!/bin/sh\nexit 2\n")
    assert itv.main() == 1
    assert not page(vault).exists()


def test_nothing_is_written_while_the_model_pass_runs(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report())
    (vault / ".model-pass").touch()
    assert itv.main() == 0
    assert not page(vault).exists() and not (vault / "calls.log").exists()

    old = time.time() - 2 * 3600
    os.utime(vault / ".model-pass", (old, old))
    assert itv.main() == 0
    assert page(vault).exists()


def test_a_vault_without_the_harness_is_not_configured(env):
    vault, intake = env
    (vault / "bin/log.sh").unlink()
    add(intake, "AbcSynth_1", report())
    assert itv.main() == 78
    assert not page(vault).exists()


def test_an_expired_transcript_is_said_not_invented(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report(), transcript=False)
    assert itv.main() == 0
    text = page(vault).read_text()
    assert "Not kept: the intake's retention removed the transcript" in text
    assert 'author: "unknown"' in text


def test_the_log_line_stays_under_the_vault_cap_with_its_link(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report(title="Ä" * 300, next_step="ß " * 400))
    assert itv.main() == 0
    line = (vault / "projects/alpha/log.md").read_text().splitlines()[-1]
    assert len(line.encode()) <= itv.LOG_LINE_MAX
    assert line.endswith("[[sources/2026-10-25-video-ig-abcsynth-1-v1|report]]")
    line.encode().decode("utf-8")


def test_a_log_commit_that_failed_after_the_append_is_not_repeated(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report())
    (vault / "log-fail").touch()
    assert itv.main() == 0
    assert itv.main() == 0
    assert (vault / "projects/alpha/log.md").read_text().count("[[sources/") == 1


def test_a_line_already_in_an_archived_log_is_not_written_again(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report())
    assert itv.main() == 0
    log_md = vault / "projects/alpha/log.md"
    (vault / "archive/projects/alpha").mkdir(parents=True)
    (vault / "archive/projects/alpha/log-2026.md").write_text(log_md.read_text())
    log_md.write_text("# log\n")

    assert itv.main() == 0
    assert "[[sources/" not in log_md.read_text()


def test_a_page_name_held_by_another_source_gets_its_own(env):
    vault, intake = env
    held = page(vault)
    held.write_text("---\ntitle: other\nurl: https://example.com/other/\n---\n")
    add(intake, "AbcSynth_1", report())

    assert itv.main() == 0

    assert held.read_text().startswith("---\ntitle: other\n")
    (mine,) = [p for p in (vault / "sources").iterdir() if p != held]
    assert mine.name.startswith("2026-10-25-video-ig-abcsynth-1-v1-")
    assert "url: https://example.com/reel/AbcSynth_1/" in mine.read_text()


def test_an_unreadable_report_fails_the_run_but_not_the_others(env):
    vault, intake = env
    (intake / "reports/ig-Broken.v1.md").write_text("Title: no verdict\n")
    add(intake, "AbcSynth_1", report())
    assert itv.main() == 1
    assert page(vault).exists()


def test_frontmatter_values_cannot_break_the_yaml(env):
    vault, intake = env
    add(
        intake,
        "AbcSynth_1",
        report(title='A "quoted" title: with\\ colon', url="not a url"),
    )
    assert itv.main() == 0
    head = page(vault).read_text().split("\n---\n", 1)[0]
    assert "title: \"A 'quoted' title: with/ colon\"" in head
    assert 'url: "not a url"' in head


def test_a_source_with_a_quote_or_backslash_is_filed_once_across_runs(env):
    vault, intake = env
    add(intake, "AbcSynth_1", report(url='https://example.com/a"b\\c'))
    assert itv.main() == 0
    assert itv.main() == 0
    assert len(list((vault / "sources").iterdir())) == 1
    assert len(calls(vault, "index")) == 1


def test_installer_installs_the_unit_and_enables_the_timer(tmp_path):
    root = tmp_path / "root"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f"#!/bin/sh\necho \"$@\" >> '{tmp_path}/calls'\n")
    systemctl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        capture_output=True,
        text=True,
        env={**os.environ, "INSTALL_ROOT": str(root), "SYSTEMCTL": str(systemctl)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    unit_dir = root / "etc/systemd/system"
    service = (unit_dir / "lifekit-intake-to-vault.service").read_text()
    assert "User=lifekit" in service and "INTAKE_TO_VAULT_VAULT=/srv/memory" in service
    assert (unit_dir / "lifekit-intake-to-vault.timer").exists()
    assert os.access(
        root / "usr/local/libexec/lifekit-intake-to-vault/intake_to_vault.py", os.X_OK
    )
    calls = (tmp_path / "calls").read_text()
    assert (
        "daemon-reload" in calls
        and "enable --now lifekit-intake-to-vault.timer" in calls
    )
