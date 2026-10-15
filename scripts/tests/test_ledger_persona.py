"""ledger-persona.py composes finance-sentry's core + openclaw adapter, installs
it into a workspace, and reports drift. Everything runs against a scratch
workspace and a local --source dir; nothing is fetched."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[1] / "ledger-persona"
SCRIPT = DIR / "ledger-persona.py"
spec = importlib.util.spec_from_file_location("ledger_persona", SCRIPT)
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)

PIN = "a" * 40


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "adapters").mkdir(parents=True)
    (src / "persona.core.md").write_text("# Core\n\nlaw one\n")
    (src / "adapters/openclaw.md").write_text("# OpenClaw\n\ntelegram\n")
    return src


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = tmp_path / "ws"
    w.mkdir()
    return w


def run(*args: str):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--pin", PIN],
        capture_output=True,
        text=True,
    )


def test_pin_file_is_a_commit_sha():
    assert lp.SHA_RE.match(lp.read_pin())


def test_compose_is_core_then_adapter(source):
    out = lp.compose(lp.load_parts(PIN, source))
    assert out == "# Core\n\nlaw one\n\n---\n\n# OpenClaw\n\ntelegram\n"


def test_compose_cli_prints_composition(source, ws):
    r = run("compose", "--source", str(source))
    assert r.returncode == 0 and r.stdout.startswith("# Core")


def test_check_reports_missing_file(source, ws):
    r = run("check", "--source", str(source), "--workspace", str(ws))
    assert r.returncode == 1 and "missing" in r.stdout


def test_install_then_check_in_sync_and_idempotent(source, ws):
    args = ("--source", str(source), "--workspace", str(ws))
    assert run("install", *args).returncode == 0
    assert (ws / "AGENTS.md").read_text().endswith("telegram\n")
    assert "in sync" in run("check", *args).stdout
    assert "unchanged" in run("install", *args).stdout
    assert not (ws / lp.BACKUP).exists()


def test_check_reports_drift_and_never_writes(source, ws):
    (ws / "AGENTS.md").write_text("hand edited\n")
    r = run("check", "--source", str(source), "--workspace", str(ws))
    assert r.returncode == 1 and "DRIFT" in r.stdout and "+law one" in r.stdout
    assert (ws / "AGENTS.md").read_text() == "hand edited\n"


def test_install_keeps_previous_file(source, ws):
    (ws / "AGENTS.md").write_text("hand edited\n")
    assert (
        run("install", "--source", str(source), "--workspace", str(ws)).returncode == 0
    )
    assert (ws / lp.BACKUP).read_text() == "hand edited\n"
    assert "law one" in (ws / "AGENTS.md").read_text()


def test_install_refuses_oversize_unless_allowed(source, ws):
    (source / "persona.core.md").write_text("x" * (lp.BOOTSTRAP_MAX_CHARS + 1))
    args = ("--source", str(source), "--workspace", str(ws))
    r = run("install", *args)
    assert r.returncode != 0 and "bootstrapMaxChars" in r.stderr
    assert not (ws / "AGENTS.md").exists()
    assert run("install", *args, "--allow-oversize").returncode == 0


def test_bad_pin_rejected(source, ws):
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "compose",
            "--source",
            str(source),
            "--pin",
            "main",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and "40-char" in r.stderr


def installed(source: Path, ws: Path) -> tuple[str, ...]:
    args = ("--source", str(source), "--workspace", str(ws))
    assert run("install", *args).returncode == 0
    return args


@pytest.mark.parametrize("rel", sorted(lp.RETIRED_PATHS))
def test_check_flags_a_retired_path(source, ws, rel):
    args = installed(source, ws)
    (ws / rel).mkdir(parents=True)
    r = run("check", *args)
    assert r.returncode == 1 and f"stale: {rel} exists" in r.stdout
    assert "DRIFT" not in r.stdout and "retired big-picture copy" in r.stdout


@pytest.mark.parametrize(
    "text",
    [
        "- ASP.NET Core 9 backend\n",
        "Stack: Angular 21\n",
        "BankSync (Plaid + Monobank)\n",
        "hand build work to DevClaw\n",
        "Check the wiki (`wiki_get`)\n",
        "your reply goes back to Kit\n",
    ],
)
@pytest.mark.parametrize("name", lp.SCANNED_FILES)
def test_check_flags_stale_text(source, ws, name, text):
    args = installed(source, ws)
    (ws / name).write_text("# Notes\n\n" + text)
    r = run("check", *args)
    assert r.returncode == 1 and f"stale: {name}:3 " in r.stdout


def test_owner_facts_and_pointers_pass(source, ws):
    args = installed(source, ws)
    (ws / "MEMORY.md").write_text(
        "# MEMORY.md\n\n## Investment focus\n- energy for AI data centres\n"
        "- relayed by Kit 2026-10-09\n\nBig picture: the finance-sentry primer.\n"
    )
    (ws / "USER.md").write_text("# USER.md\n\n- Timezone: Europe/Dublin\n")
    (ws / "memory").mkdir()
    (ws / "memory/2026-10-15.md").write_text("Plaid came up today\n")
    r = run("check", *args)
    assert r.returncode == 0 and "in sync" in r.stdout


def test_stale_lines_come_before_the_diff(source, ws):
    (ws / "AGENTS.md").write_text("hand edited\n")
    (ws / "skills/project-context").mkdir(parents=True)
    r = run("check", "--source", str(source), "--workspace", str(ws))
    lines = r.stdout.splitlines()
    assert r.returncode == 1 and "DRIFT" in lines[0] and "1 hit)" in lines[0]
    assert lines[1].startswith("stale: skills/project-context")
    assert "+law one" in r.stdout
