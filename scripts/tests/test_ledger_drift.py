"""Behaviour of scripts/ledger-drift/lifekit-ledger-drift.sh against a fake docker."""

from __future__ import annotations

import configparser
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ledger-drift/lifekit-ledger-drift.sh"
AUTO_MEMORY = (
    "/home/node/.claude/projects/-home-node--openclaw-agents-finance-workspace/memory"
)
WORKSPACE = "/home/node/.openclaw/agents/finance/workspace"

# Fake docker: `ps` prints a container id (none when STATE_DIR/_down exists);
# `exec <id> sh -c SCRIPT sh DIR REQUIRED` runs the script locally with DIR
# rewritten under STATE_DIR/root, so the real find runs against a fake tree.
# STATE_DIR/_broken fails every exec like a dead daemon. Every call is logged.
FAKE = r"""#!/usr/bin/env bash
echo "$*" >> "$STATE_DIR/_calls"
case "$1" in
  ps) [[ -f "$STATE_DIR/_down" ]] || echo 0d304cf83c34 ;;
  exec)
    [[ -f "$STATE_DIR/_broken" ]] && { echo "Error response from daemon" >&2; exit 1; }
    [[ "$2" == 0d304cf83c34 && "$3" == sh && "$4" == -c ]] || exit 3
    exec sh -c "$5" "$6" "$STATE_DIR/root$7" "$8"
    ;;
  *) exit 3 ;;
esac
"""

CLEAN_WORKSPACE = [
    "AGENTS.md",
    "AGENTS.md.bak.2026-07-03",
    "AGENTS.md.pre-persona-install",
    "SOUL.md",
    "IDENTITY.md",
    "USER.md",
    "MEMORY.md",
    "HEARTBEAT.md",
    ".openclaw/workspace-state.json.migrated.abc",
    "openclaw-workspace-state.json.migrated.abc",
    ".openclaw-cli-images/0cb4.jpg",
    "media/inbound/openclaw-staged-1/.gitignore",
    "media/inbound/openclaw-staged-1/input-1.jpg",
    "memory/.gitkeep",
    "memory/2026-10-19.md",
    "skills/README.md",
    "skills/some-skill/SKILL.md",
    "mcp/ibkr-flex-server.mjs",
    "mcp_call.js",
    "mcp_scan.js",
    "run_ledger_scan.js",
    ".git/HEAD",
]


@pytest.fixture
def check(tmp_path):
    state, out, bin_dir = tmp_path / "state", tmp_path / "out", tmp_path / "bin"
    for d in (state, out, bin_dir):
        d.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    root = state / "root"

    def put(base, rel, text="x"):
        path = root / base.lstrip("/") / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def workspace(*rels):
        (root / WORKSPACE.lstrip("/")).mkdir(parents=True, exist_ok=True)
        for rel in rels:
            put(WORKSPACE, rel)

    def run(*args, expect=0):
        env = dict(os.environ)
        env |= {"PATH": f"{bin_dir}:{env['PATH']}", "STATE_DIR": str(state)}
        proc = subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == expect, proc.stdout + proc.stderr
        return proc

    return SimpleNamespace(
        state=state, out=out, root=root, put=put, workspace=workspace, run=run
    )


def prom(out: Path) -> list[str]:
    text = (out / "ledger_finance_drift.prom").read_text()
    return [line for line in text.splitlines() if not line.startswith("#")]


def test_clean_workspace_and_no_auto_memory_dir_is_clean(check):
    check.workspace(*CLEAN_WORKSPACE)
    proc = check.run()
    assert proc.stdout == ""


def test_empty_auto_memory_dir_is_clean(check):
    check.workspace("AGENTS.md")
    (check.root / AUTO_MEMORY.lstrip("/")).mkdir(parents=True)
    assert check.run().stdout == ""


def test_any_auto_memory_file_is_drift_one_line_each(check):
    check.workspace("AGENTS.md")
    check.put(AUTO_MEMORY, "MEMORY.md")
    check.put(AUTO_MEMORY, "pltr-thesis.md")
    proc = check.run(expect=1)
    assert sorted(proc.stdout.splitlines()) == [
        "auto-memory: MEMORY.md",
        "auto-memory: pltr-thesis.md",
    ]


@pytest.mark.parametrize(
    "rel",
    [
        "ledger-own-theses.md",
        "notes/book-state.md",
        "memory/theses.md",
        "AGENTS.md.copy",
        ".claude/settings.json",
        "mcp/new-server.mjs",
    ],
)
def test_workspace_file_outside_the_allowlist_is_drift(check, rel):
    check.workspace(*CLEAN_WORKSPACE, rel)
    proc = check.run(expect=1)
    assert proc.stdout.splitlines() == [f"workspace: {rel}"]


def test_check_never_deletes_or_moves_anything(check):
    check.workspace(*CLEAN_WORKSPACE, "thesis.md")
    hit = check.put(AUTO_MEMORY, "thesis.md", "keep me")
    before = sorted(p for p in check.root.rglob("*"))
    check.run(expect=1)
    check.run("--textfile", str(check.out))
    assert sorted(p for p in check.root.rglob("*")) == before
    assert hit.read_text() == "keep me"


def test_textfile_mode_writes_one_series_per_file_and_exits_zero(check):
    check.workspace(*CLEAN_WORKSPACE, "thesis.md")
    check.put(AUTO_MEMORY, 'a "quoted" name.md')
    proc = check.run("--textfile", str(check.out))
    assert len(proc.stdout.splitlines()) == 2
    assert prom(check.out) == [
        'ledger_finance_drift_file{area="auto-memory",path="a \\"quoted\\" name.md"} 1',
        'ledger_finance_drift_file{area="workspace",path="thesis.md"} 1',
        "ledger_finance_drift_files 2",
    ]
    assert oct((check.out / "ledger_finance_drift.prom").stat().st_mode & 0o777) == (
        "0o644"
    )


def test_textfile_mode_clean_writes_only_the_count(check):
    check.workspace(*CLEAN_WORKSPACE)
    check.run("--textfile", str(check.out))
    assert prom(check.out) == ["ledger_finance_drift_files 0"]


@pytest.mark.parametrize("failure", ["_down", "_broken", "no-workspace"])
def test_a_check_that_cannot_run_exits_2_and_keeps_the_previous_file(check, failure):
    previous = check.out / "ledger_finance_drift.prom"
    previous.write_text("ledger_finance_drift_files 0\n")
    if failure == "no-workspace":
        check.put(AUTO_MEMORY, "x.md")
    else:
        check.workspace("AGENTS.md")
        (check.state / failure).touch()
    check.run("--textfile", str(check.out), expect=2)
    assert previous.read_text() == "ledger_finance_drift_files 0\n"
    assert [p.name for p in check.out.iterdir()] == ["ledger_finance_drift.prom"]


def test_lists_both_trees_inside_the_gateway_container(check):
    check.workspace("AGENTS.md")
    check.run()
    calls = (check.state / "_calls").read_text()
    assert "label=com.docker.compose.service=openclaw-gateway" in calls
    assert f"sh {AUTO_MEMORY} 0" in calls
    assert f"sh {WORKSPACE} 1" in calls


def _unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string((REPO / "scripts/ledger-drift" / name).read_text())
    return parser


def test_service_runs_the_check_as_the_admin_user_into_the_textfile_dir():
    service = _unit("lifekit-ledger-drift.service")
    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["User"] == "__ADMIN_USER__"
    assert service["Service"]["ExecStart"].split() == [
        "/usr/local/bin/lifekit-ledger-drift.sh",
        "--textfile",
        "/var/lib/node_exporter/textfile",
    ]
    assert os.access(SCRIPT, os.X_OK)


def test_timer_runs_every_five_minutes_and_is_installed_with_the_gauges():
    # textfile-collector-stale judges every 5-minute gauge against 15 minutes;
    # unit-gauge watches lifekit-*.timer, so a failed run alerts too.
    timer = _unit("lifekit-ledger-drift.timer")
    assert timer["Timer"]["OnUnitActiveSec"] == "5min"
    installer = (REPO / "scripts/host-gauge/install-host-gauges.sh").read_text()
    assert "ledger-drift/lifekit-ledger-drift" in installer
    assert "lifekit-ledger-drift.timer" in installer


def test_auto_memory_is_locked_off_by_managed_settings_in_the_gateway_image():
    settings = json.loads(
        (REPO / "compose/openclaw-gateway/managed-settings.json").read_text()
    )
    assert settings["autoMemoryEnabled"] is False
    assert settings["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    lines = (REPO / "compose/openclaw-gateway/Dockerfile").read_text().splitlines()
    copy = lines.index(
        "COPY --chmod=0644 managed-settings.json /etc/claude-code/managed-settings.json"
    )
    # Root-owned: copied before the image drops to the node user.
    assert copy < lines.index("USER node")
