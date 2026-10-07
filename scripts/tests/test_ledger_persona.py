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
