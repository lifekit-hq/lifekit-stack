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
REAL_MCP = door.Mcp

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
    """Stubbed Workspace MCP: mail fixtures plus a tiny in-memory Drive."""

    calls: list[str] = []
    fail = False
    drive_error: str | None = None  # the text every Drive create refuses with
    folders: dict[str, str] = {}  # folder name -> id
    files: dict[str, list[str]] = {}  # folder id -> file names
    uploads: list[dict] = []

    def __init__(self, *a, **k):
        if FakeMcp.fail:
            raise RuntimeError("Google sign-in required")

    def drive(self, tool, args):
        if tool == "search_drive_files":
            hits = [
                f'- Name: "{n}" (ID: {i}, Type: application/vnd.google-apps.folder, Size: n/a)'
                for n, i in FakeMcp.folders.items()
                if f"name = '{n}'" in args["query"]
            ]
            return f"Found {len(hits)} files\n" + "\n".join(hits)
        if tool == "list_drive_items":
            rows = [
                f'- Name: "{n}" (ID: f-{k}, Type: application/pdf, Size: 1)'
                for k, n in enumerate(FakeMcp.files.get(args["folder_id"], []))
            ]
            return f"Found {len(rows)} items\n" + "\n".join(rows)
        if FakeMcp.drive_error:
            raise RuntimeError(FakeMcp.drive_error)
        if tool == "create_drive_folder":
            fid = f"fold-{len(FakeMcp.folders) + 1}"
            FakeMcp.folders[args["folder_name"]] = fid
            return f"Successfully created folder '{args['folder_name']}' (ID: {fid})"
        assert tool == "create_drive_file"
        FakeMcp.uploads.append(args)
        FakeMcp.files.setdefault(args["folder_id"], []).append(args["file_name"])
        return f"Successfully created file '{args['file_name']}' (ID: file-{len(FakeMcp.uploads)})"

    def call(self, tool, **args):
        FakeMcp.calls.append(tool)
        if tool in door.ALLOWED_TOOLS and "drive" in tool:
            return self.drive(tool, args)
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
    FakeMcp.calls, FakeMcp.fail, FakeMcp.drive_error = [], False, None
    FakeMcp.folders, FakeMcp.files, FakeMcp.uploads = {}, {}, []
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


def test_scan_alone_calls_only_mail_reads(env):
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
    assert (
        "Drive: filed in Employment" in (door.HOME / "reports" / f"{d}.md").read_text()
    )
    rec = door.load_state()["docs"][d]
    assert rec["status"] == "reported" and rec["logged"] is True
    logged = (door.VAULT / "logged.txt").read_text()
    assert "Employment" in logged and "domains/career.md" in logged
    assert "January" not in logged and "two months" not in logged
    assert "file-1" not in logged  # the Drive reference is never in the vault log


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


# --- Drive filing -----------------------------------------------------------------------


def reported(capsys):
    run(["scan"])
    d = doc_id()
    assert run(["report", d], GOOD) == 0
    capsys.readouterr()
    return d


def report_text(d):
    return (door.HOME / "reports" / f"{d}.md").read_text()


def test_filing_creates_the_folder_and_uploads_the_original_once(env, capsys):
    d = reported(capsys)
    assert FakeMcp.folders == {"Employment": "fold-1"}
    (up,) = FakeMcp.uploads
    assert up["file_name"] == "offer.pdf" and up["folder_id"] == "fold-1"
    assert base64.b64decode(up["base64_content"]) == PDF
    assert up["base64_sha256"] == door.hashlib.sha256(PDF).hexdigest()
    assert "Drive: filed in Employment (file-1)" in report_text(d)
    rec = door.load_state()["docs"][d]
    assert rec["drive"] == "filed" and rec["drive_ref"] == "file-1"
    # Later scans never upload it again.
    run(["scan"])
    run(["scan"])
    assert len(FakeMcp.uploads) == 1
    assert FakeMcp.calls.count("create_drive_file") == 1


def test_existing_folder_is_reused(env, capsys):
    FakeMcp.folders["Employment"] = "fold-existing"
    reported(capsys)
    assert "create_drive_folder" not in FakeMcp.calls
    assert FakeMcp.uploads[0]["folder_id"] == "fold-existing"


def test_name_collision_gets_a_hash_suffix_and_nothing_is_overwritten(env, capsys):
    FakeMcp.folders["Employment"] = "fold-existing"
    FakeMcp.files["fold-existing"] = ["offer.pdf"]
    d = reported(capsys)
    (up,) = FakeMcp.uploads
    assert up["file_name"] == f"offer-{d[:8]}.pdf"
    assert FakeMcp.files["fold-existing"][0] == "offer.pdf"


def test_suffixed_name_already_there_means_already_filed(env, capsys, monkeypatch):
    run(["scan"])
    d = doc_id()
    FakeMcp.folders["Employment"] = "fold-existing"
    FakeMcp.files["fold-existing"] = ["offer.pdf", f"offer-{d[:8]}.pdf"]
    assert run(["report", d], GOOD) == 0
    assert FakeMcp.uploads == []
    assert door.load_state()["docs"][d]["drive"] == "filed"


def test_refusal_leaves_pending_with_a_reason_and_the_scan_retries(env, capsys):
    FakeMcp.drive_error = "insufficient authentication scopes"
    d = reported(capsys)
    assert "Drive: pending (insufficient authentication scopes)" in report_text(d)
    assert door.load_state()["docs"][d]["status"] == "reported"
    # A scan during the refusal still succeeds and tries again.
    assert run(["scan"]) == 0
    assert "No new documents." in capsys.readouterr().out
    assert FakeMcp.calls.count("create_drive_file") == 0 and not FakeMcp.uploads
    # Consent lands: the next scan files it, exactly once.
    FakeMcp.drive_error = None
    run(["scan"])
    run(["scan"])
    assert len(FakeMcp.uploads) == 1
    assert "Drive: filed in Employment" in report_text(d)


def test_drive_outage_never_fails_report_or_scan(env, capsys, monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    run(["scan"])
    d = doc_id()
    monkeypatch.setattr(door, "Mcp", boom)
    assert run(["report", d], GOOD) == 0
    assert "Drive: pending (connection refused)" in report_text(d)
    assert door.load_state()["docs"][d]["status"] == "reported"


def test_reason_carries_no_identifier_shaped_text(env, capsys):
    FakeMcp.drive_error = "quota for user a@b.example exceeded 123456789012"
    d = reported(capsys)
    line = [ln for ln in report_text(d).splitlines() if ln.startswith("Drive:")][0]
    assert "a@b.example" not in line and "123456789012" not in line


def test_only_create_and_lookup_drive_tools_are_allowed(env, capsys):
    assert {t for t in door.ALLOWED_TOOLS if "drive" in t} == {
        "search_drive_files",
        "list_drive_items",
        "create_drive_folder",
        "create_drive_file",
    }
    reported(capsys)
    assert {t for t in FakeMcp.calls if "drive" in t} <= door.ALLOWED_TOOLS
    real = object.__new__(REAL_MCP)
    for tool in (
        "update_drive_file",
        "manage_drive_access",
        "set_drive_file_permissions",
        "get_drive_shareable_link",
        "copy_drive_file",
        "send_gmail_message",
    ):
        with pytest.raises(RuntimeError, match="not allowed"):
            REAL_MCP.call(real, tool)
