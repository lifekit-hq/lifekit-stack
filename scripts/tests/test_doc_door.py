"""The doc-door skill (skills/doc-door/door.py) and its ensure script.

The Workspace MCP is replaced by a canned fake that records every tool it is
asked for, MarkItDown by a stub that prints a fixture, and the vault by a
directory whose bin/log.sh appends to a file. Nothing touches the network.
"""

from __future__ import annotations

import base64
import importlib.util
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills/doc-door"
ENSURE = REPO / "scripts/ensure-doc-door.sh"

spec = importlib.util.spec_from_file_location("door", SKILL / "door.py")
door = importlib.util.module_from_spec(spec)
sys.modules["door"] = door
spec.loader.exec_module(door)

PDF = b"%PDF-1.4 " + b"x" * 5000
TEXT = "# Offer letter\n\nStart date: 4 January 2027\nNotice period: two months\nProbation lasts six months\nSalary review each April\n"
MESSAGE = """Subject: Your offer
From: HR <hr@example.com>
Date: Mon, 26 Oct 2026 09:00:00 +0000

--- BODY ---
Please see attached.

--- ATTACHMENTS ---
1. offer.pdf (application/pdf, 5.0 KB)
   Attachment ID: ATT1
   Use get_gmail_attachment_content(message_id='M1', attachment_id='ATT1', attachment_index=0) to download
2. logo.png (image/png, 9.0 KB)
   Attachment ID: ATT2
"""
GOOD = """Title: Offer letter
Folder: Employment
Page: domains/career.md
Proposal: Add the start date and notice period to the employment section.
Facts:
- Employment starts early January | "Start date: 4 January 2027" [L3]
- Two months of notice are required | "Notice period: two months" [L4]
- Probation is six months | "Probation lasts six months" [L5]
"""


class FakeMcp:
    calls: list[str] = []
    fail = False

    def __init__(self, *a, **k):
        if FakeMcp.fail:
            raise RuntimeError("Google sign-in required")

    def call(self, tool, **args):
        FakeMcp.calls.append(tool)
        if tool == "search_gmail_messages":
            return "Found 1 message\nMessage ID: M1\nThread ID: T1\n"
        if tool == "get_gmail_message_content":
            return MESSAGE
        assert tool == "get_gmail_attachment_content" and args["return_base64"] is True
        return (
            "Attachment downloaded\n\nBase64 content (1 chars, standard base64):\n"
            + base64.b64encode(PDF).decode()
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "bin").mkdir(parents=True)
    (vault / "domains").mkdir()
    (vault / "domains/career.md").write_text("x")
    (vault / "bin/log.sh").write_text('echo "$@" >> "$VAULT/logged.txt"\n')
    conv = tmp_path / "markitdown"
    conv.write_text(f"#!/bin/sh\ncat <<'EOF'\n{TEXT}EOF\n")
    conv.chmod(0o755)
    monkeypatch.setattr(door, "HOME", tmp_path / "door")
    monkeypatch.setattr(door, "VAULT", vault)
    monkeypatch.setattr(door, "MARKITDOWN", str(conv))
    monkeypatch.setattr(door, "Mcp", FakeMcp)
    FakeMcp.calls, FakeMcp.fail = [], False
    return tmp_path


def run(argv, stdin=""):
    import io

    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        return door.main(["door.py", *argv])
    finally:
        sys.stdin = old


def doc_id():
    return next(iter(door.load_state()["docs"]))


def test_parsers():
    assert door.parse_search(
        "Message ID: A\nThread ID: B\nMessage ID: C\nMessage ID: A"
    ) == ["A", "C"]
    msg = door.parse_message(MESSAGE)
    assert msg["subject"] == "Your offer" and msg["date"].startswith("Mon, 26 Oct")
    assert [a["filename"] for a in msg["attachments"]] == ["offer.pdf", "logo.png"]
    assert (
        msg["attachments"][0]["index"] == 0
        and msg["attachments"][0]["att_id"] == "ATT1"
    )
    assert door.parse_attachment("x\nBase64 content (4 chars):\nQUJDRA==\n") == b"ABCD"
    assert door.kind_of("a.PDF") == "pdf" and door.kind_of("a.png") is None


def test_scan_converts_privately_and_is_idempotent(env, capsys):
    assert run(["scan"]) == 0
    out = capsys.readouterr().out
    assert "L3: Start date: 4 January 2027" in out and "=== DOC " in out
    assert "logo.png" not in out
    d = doc_id()
    assert stat.S_IMODE((door.HOME / "documents").stat().st_mode) == 0o700
    assert stat.S_IMODE((door.HOME / "documents" / f"{d}.md").stat().st_mode) == 0o600
    # Not yet reported: shown again, never fetched twice.
    FakeMcp.calls.clear()
    run(["scan"])
    assert "=== DOC " in capsys.readouterr().out
    assert "get_gmail_attachment_content" not in FakeMcp.calls
    assert run(["report", d], GOOD) == 0
    capsys.readouterr()
    run(["scan"])
    assert "No new documents." in capsys.readouterr().out


def test_only_read_tools_are_called(env):
    run(["scan"])
    assert set(FakeMcp.calls) <= {
        "search_gmail_messages",
        "get_gmail_message_content",
        "get_gmail_attachment_content",
    }


def test_mail_unavailable_is_one_line(env, capsys):
    FakeMcp.fail = True
    assert run(["scan"]) == 0
    assert (
        capsys.readouterr().out.strip() == "mail unavailable: Google sign-in required"
    )


def test_report_filed_and_logged_without_facts(env, capsys):
    run(["scan"])
    d = doc_id()
    assert run(["report", d], GOOD) == 0
    assert "Drive: pending" in (door.HOME / "reports" / f"{d}.md").read_text()
    rec = door.load_state()["docs"][d]
    assert rec["status"] == "reported" and rec["logged"] is True
    logged = (door.VAULT / "logged.txt").read_text()
    assert "Employment" in logged and "domains/career.md" in logged
    assert "January" not in logged and "two months" not in logged


def test_log_failure_is_retried(env):
    run(["scan"])
    d = doc_id()
    (door.VAULT / "bin/log.sh").write_text("exit 1\n")
    assert run(["report", d], GOOD) == 0
    assert door.load_state()["docs"][d].get("logged") is not True
    (door.VAULT / "bin/log.sh").write_text('echo "$@" >> "$VAULT/logged.txt"\n')
    run(["scan"])
    assert door.load_state()["docs"][d]["logged"] is True


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (
            lambda r: r.replace(
                '"Start date: 4 January 2027" [L3]', '"Start date: 9 March 2027" [L3]'
            ),
            "not on L3",
        ),
        (lambda r: r.replace("[L5]", "[L99]"), "outside the document"),
        (
            lambda r: r.replace(
                '- Probation is six months | "Probation lasts six months" [L5]\n', ""
            ),
            "2 facts",
        ),
        (
            lambda r: r.replace("Two months of notice", "Account 1234 5678 9012 holds"),
            "identifier",
        ),
        (
            lambda r: r.replace(
                "Two months of notice are required", "Write to a@b.example"
            ),
            "identifier",
        ),
        (lambda r: r.replace("domains/career.md", "domains/nope.md"), "does not exist"),
        (lambda r: r.replace("Employment", "../Employment"), "Folder"),
        (lambda r: r.replace("Proposal:", "Note:"), "Proposal"),
    ],
)
def test_report_rejections(env, mutate, needle, capsys):
    run(["scan"])
    d = doc_id()
    assert run(["report", d], mutate(GOOD)) == 3
    assert needle in capsys.readouterr().err
    assert door.load_state()["docs"][d]["status"] == "converted"


def test_unreadable_attachment_is_not_stored(env, monkeypatch, capsys):
    monkeypatch.setattr(door, "MARKITDOWN", "/bin/false")
    run(["scan"])
    assert "conversion failed" in capsys.readouterr().out
    assert door.load_state()["docs"] == {}


def test_ensure_script_pins_and_fails_closed():
    text = ENSURE.read_text()
    assert 'MARKITDOWN_VERSION="' in text and 'JOB_TOOLS="exec"' in text
    assert "--no-deliver" in text and "refusing:" in text
