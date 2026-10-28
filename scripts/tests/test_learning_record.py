"""learnclaw's learning-record skill and its ensure script (docs/runbook.md,
"learnclaw (the learning loop)").

The vault is a temporary git repo holding a learning page shaped like the real
one and a stub bin/lint.sh that speaks the real lint's --changed --tsv output:
it reports a [[link]] to a page that does not exist, the one rule a row can
trip. The live vault's own lint was run against record.py by hand (PR
Validation).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RECORD = REPO / "skills/learning-record/record.py"
ENSURE = REPO / "scripts/ensure-learnclaw.sh"
CONTRACT = REPO / "defaults/agents/learnclaw/workspace"

PAGE = """\
---
name: learning
updatedAt: 2026-10-01
status: active
---

# Learning

## Artefact record (v0 - one row per artefact)

Row = one from-scratch artefact.

| date | topic | artefact (repo/path) | reading | applied | post | grilled | status |
|---|---|---|---|---|---|---|---|
| due 2026-09-22 | caching | cache from scratch | a chapter | a boundary (planned) | - | - | in progress |

## In-progress
_(to fill)_
"""

# Speaks `bin/lint.sh --changed --tsv`: a medium broken-link for every [[x]]
# on a changed page with no x.md in the vault.
LINT = r"""#!/bin/sh
for f in $(git diff --name-only); do
  for l in $(grep -o '\[\[[^]]*\]\]' "$f" | tr -d '[]'); do
    [ -e "$l.md" ] || printf 'medium\tbroken-link\t%s\t[[%s]] resolves to nothing\n' "$f" "$l"
  done
done
printf 'low\tline-too-long\tdomains/learning.md\tfine\n'
"""


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "domains").mkdir(parents=True)
    (v / "bin").mkdir()
    (v / "domains/learning.md").write_text(PAGE)
    (v / "bin/lint.sh").write_text(LINT)
    (v / "career.md").write_text("career\n")
    git = ["git", "-C", str(v), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    return v


def record(vault, *args, today="2026-10-28"):
    return subprocess.run(
        [sys.executable, str(RECORD), *args],
        env={**os.environ, "LEARNING_VAULT": str(vault), "LEARNING_TODAY": today},
        capture_output=True,
        text=True,
        check=False,
    )


def table_rows(vault):
    lines = (vault / "domains/learning.md").read_text().splitlines()
    return [ln for ln in lines if ln.startswith("| ") and not ln.startswith("| date")]


def test_status_computes_the_projections_now(vault):
    r = record(vault, "status")
    assert r.returncode == 0, r.stderr
    assert "1 row(s), today 2026-10-28" in r.stdout
    assert "last artefact row: none yet" in r.stdout
    assert "checkpoint 2026-09-22 (caching): missed by 36 days" in r.stdout
    assert "in progress: caching - lacks applied, post, grilled" in r.stdout
    r = record(vault, "status", today="2026-09-20")
    assert "checkpoint 2026-09-22 (caching): due in 2 days" in r.stdout


def test_append_adds_one_row_at_the_end_of_the_table_and_bumps_updated_at(vault):
    r = record(vault, "append", "--topic", "caching", "--artefact", "repo/cache.py")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("recorded: | 2026-10-28 | caching | repo/cache.py |")
    rows = table_rows(vault)
    assert len(rows) == 2
    assert (
        rows[1]
        == "| 2026-10-28 | caching | repo/cache.py | - | - | - | - | in progress |"
    )
    text = (vault / "domains/learning.md").read_text()
    assert "updatedAt: 2026-10-28" in text
    assert text.index(rows[1]) < text.index("## In-progress")
    assert not list((vault / "domains").glob(".*tmp"))


def test_the_latest_row_is_the_topics_state(vault):
    r = record(
        vault,
        *("append", "--topic", "caching", "--artefact", "repo/cache.py"),
        *("--applied", "svc/x", "--post", "blog/1", "--grilled", "2026-10-28"),
        *("--status", "done"),
    )
    assert r.returncode == 0, r.stderr
    r = record(vault, "status")
    assert "last artefact row: 2026-10-28 (caching), 0 days ago" in r.stdout
    assert "checkpoint" not in r.stdout and "in progress:" not in r.stdout
    assert "done: 1 topic(s): caching" in r.stdout


def test_a_checkpoint_is_met_by_an_artefact_row_dated_by_it_whatever_its_status(vault):
    r = record(
        vault,
        *("append", "--topic", "caching", "--artefact", "repo/cache.py"),
        *("--date", "2026-09-20"),
    )
    assert r.returncode == 0, r.stderr
    r = record(vault, "status")
    assert "checkpoint 2026-09-22 (caching): met 2026-09-20" in r.stdout
    assert "missed" not in r.stdout
    assert "in progress: caching - lacks applied, post, grilled" in r.stdout


def test_a_checkpoint_is_missed_when_the_artefact_row_comes_after_it(vault):
    record(
        vault,
        *("append", "--topic", "caching", "--artefact", "repo/cache.py"),
        *("--date", "2026-09-23"),
    )
    record(
        vault,
        *("append", "--topic", "other", "--artefact", "repo/o.py"),
        *("--date", "2026-09-20"),
    )
    r = record(vault, "status")
    assert "checkpoint 2026-09-22 (caching): missed by 36 days" in r.stdout


def test_a_future_checkpoint_with_no_artefact_row_yet_is_due(vault):
    r = record(vault, "status", today="2026-09-20")
    assert "checkpoint 2026-09-22 (caching): due in 2 days" in r.stdout


@pytest.mark.parametrize("date", ["2026-02-30", "due 2026-13-01"])
def test_append_refuses_an_impossible_calendar_date_and_writes_nothing(vault, date):
    before = (vault / "domains/learning.md").read_bytes()
    r = record(vault, "append", "--topic", "caching", "--artefact", "a", "--date", date)
    assert r.returncode == 2 and "date: YYYY-MM-DD" in r.stderr
    assert (vault / "domains/learning.md").read_bytes() == before


def test_status_reports_a_row_with_an_impossible_date_instead_of_crashing(vault):
    page = vault / "domains/learning.md"
    bad = "| 2026-02-30 | tracing | t.py | - | - | - | - | in progress |"
    page.write_text(
        page.read_text().replace("\n\n## In-progress", f"\n{bad}\n\n## In-progress")
    )
    r = record(vault, "status")
    assert r.returncode == 0, r.stderr
    assert "unparseable date on row: '2026-02-30' (tracing)" in r.stdout
    assert "2 row(s), today 2026-10-28" in r.stdout
    assert "checkpoint 2026-09-22 (caching): missed by 36 days" in r.stdout
    assert "in progress: tracing" in r.stdout


@pytest.mark.parametrize(
    "args,said",
    [
        (["--status", "done", "--applied", "svc/x"], "status done needs post, grilled"),
        (["--status", "finished"], "status: one of"),
        (["--reading", "a | b"], "reading: a cell cannot hold a pipe"),
        (["--post", "line\nbreak"], "post: a cell cannot hold a pipe or a line break"),
        (["--date", "28/10/2026"], "date: YYYY-MM-DD"),
        (
            [
                "--applied",
                "x (planned)",
                "--post",
                "p",
                "--grilled",
                "g",
                "--status",
                "done",
            ],
            "status done needs applied",
        ),
    ],
)
def test_append_refuses_a_bad_row_and_writes_nothing(vault, args, said):
    before = (vault / "domains/learning.md").read_bytes()
    r = record(vault, "append", "--topic", "caching", "--artefact", "a", *args)
    assert r.returncode == 2 and said in r.stderr
    assert (vault / "domains/learning.md").read_bytes() == before


def test_a_lint_refusal_puts_the_page_back_byte_for_byte(vault):
    before = (vault / "domains/learning.md").read_bytes()
    r = record(vault, "append", "--topic", "see [[no-such-page]]", "--artefact", "a")
    assert r.returncode == 1
    assert "medium\tbroken-link\tdomains/learning.md\t[[no-such-page]]" in r.stderr
    assert (vault / "domains/learning.md").read_bytes() == before


def test_a_link_that_resolves_passes_the_lint(vault):
    r = record(vault, "append", "--topic", "see [[career]]", "--artefact", "a")
    assert r.returncode == 0, r.stderr


def test_a_lint_that_does_not_run_puts_the_page_back(vault):
    (vault / "bin/lint.sh").write_text("#!/bin/sh\necho boom >&2\nexit 2\n")
    before = (vault / "domains/learning.md").read_bytes()
    r = record(vault, "append", "--topic", "caching", "--artefact", "a")
    assert r.returncode == 1 and "lint exited 2: boom" in r.stderr
    assert (vault / "domains/learning.md").read_bytes() == before


def test_append_never_commits(vault):
    record(vault, "append", "--topic", "caching", "--artefact", "a")
    log = subprocess.run(
        ["git", "-C", str(vault), "log", "--oneline"], capture_output=True, text=True
    )
    assert len(log.stdout.splitlines()) == 1


def test_no_lint_means_nothing_is_written(vault):
    (vault / "bin/lint.sh").unlink()
    before = (vault / "domains/learning.md").read_bytes()
    r = record(vault, "append", "--topic", "caching", "--artefact", "a")
    assert r.returncode == 2 and "no vault lint" in r.stderr
    assert (vault / "domains/learning.md").read_bytes() == before


def test_a_page_without_the_record_table_is_refused(vault):
    (vault / "domains/learning.md").write_text("---\nupdatedAt: x\n---\n# Learning\n")
    r = record(vault, "status")
    assert r.returncode == 2 and "no '## Artefact record' section" in r.stderr


# --- the ensure script ---------------------------------------------------------------------


DOCKER = """#!/bin/sh
case "$1" in
  ps) [ -n "$GATEWAY" ] && echo "$GATEWAY" ;;
  exec) printf %s "$ALLOWLIST" ;;
esac
exit 0
"""


def ensure(tmp_path, *, workspace=True, gateway="", allowlist="yes"):
    state = tmp_path / "state"
    ws = state / "agents/learnclaw/workspace"
    if workspace:
        ws.mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "dbin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(DOCKER)
    (bindir / "docker").chmod(0o755)
    r = subprocess.run(
        ["bash", str(ENSURE)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "OPENCLAW_CONFIG_DIR": str(state),
            "GATEWAY": gateway,
            "ALLOWLIST": allowlist,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return r, ws


def test_ensure_writes_the_git_contract_over_the_live_one(tmp_path):
    ws = tmp_path / "state/agents/learnclaw/workspace"
    ws.mkdir(parents=True)
    (ws / "AGENTS.md").write_text("edited live\n")
    (ws / "memory").mkdir()
    (ws / "memory/2026-10-28.md").write_text("notes\n")
    r, ws = ensure(tmp_path)
    assert r.returncode == 0, r.stderr
    for f in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "skills/README.md"):
        assert (ws / f).read_bytes() == (CONTRACT / f).read_bytes()
    for f in ("SKILL.md", "record.py"):
        assert (ws / "skills/learning-record" / f).read_bytes() == (
            REPO / "skills/learning-record" / f
        ).read_bytes()
    assert os.access(ws / "skills/learning-record/record.py", os.X_OK)
    assert (ws / "memory/2026-10-28.md").read_text() == "notes\n"
    assert "no running openclaw-gateway container" in r.stdout


def test_ensure_skips_a_box_without_learnclaw_and_succeeds(tmp_path):
    r, ws = ensure(tmp_path, workspace=False)
    assert r.returncode == 0 and "no workspace for agent 'learnclaw'" in r.stdout
    assert not ws.exists()


@pytest.mark.parametrize(
    "allowlist,said",
    [
        ("yes", "is on agent 'learnclaw''s skills list"),
        ("unfiltered", "has no skills list; the skill is visible"),
        ("no", "NOT on agent 'learnclaw''s skills list - paused"),
        ("noentry", "no agent entry 'learnclaw'"),
    ],
)
def test_ensure_reports_the_allowlist_without_touching_it(tmp_path, allowlist, said):
    r, _ = ensure(tmp_path, gateway="abc123", allowlist=allowlist)
    assert r.returncode == 0, r.stderr
    assert said in r.stdout
