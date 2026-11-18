"""The gates for the doc-door scan and the needs-you mail reader (#589).

`door.py check` / `door.py mail-check` answer one question without a model: does
this scheduled tick have anything to do? An idle tick makes no model call (the
gate script returns fire:false); a busy tick runs as before. The Workspace MCP is
a canned fake that records the tools it is asked for.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATES = REPO / "scripts/doc-door-gate"

spec = importlib.util.spec_from_file_location(
    "door_gates", REPO / "skills/doc-door/door.py"
)
door = importlib.util.module_from_spec(spec)
sys.modules["door_gates"] = door
spec.loader.exec_module(door)

NONE = "No messages found for query 'x'."
ONE = "Found 1 message\nMessage ID: M1\nThread ID: T1\n"


class FakeMcp:
    reply = NONE
    fail = False
    calls: list[tuple[str, dict]] = []

    def __init__(self, url=None, *a, **k):
        if FakeMcp.fail:
            raise RuntimeError("mail down")

    def call(self, tool, **args):
        FakeMcp.calls.append((tool, args))
        assert tool == "search_gmail_messages"  # a gate only ever searches
        return FakeMcp.reply


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(door, "HOME", tmp_path / "door")
    monkeypatch.setattr(door, "MAIL_IDLE", tmp_path / "door" / "mail-gate-idle.json")
    monkeypatch.setattr(door, "Mcp", FakeMcp)
    monkeypatch.setattr(door.drive_upload, "CONFIG", tmp_path / "none" / "rclone.conf")
    FakeMcp.reply, FakeMcp.fail, FakeMcp.calls = NONE, False, []
    return tmp_path


def run(capsys, *argv) -> tuple[int, str]:
    rc = door.main(["door.py", *argv])
    return rc, capsys.readouterr().out


def seed(rec: dict, doc="d1", skipped=()):
    base = {"message_id": "M1", "status": "reported", "drive": "filed", "logged": True}
    door.save_state({"docs": {doc: {**base, **rec}}, "skipped": list(skipped)})


# --- doc-door scan gate -------------------------------------------------------


def test_scan_gate_idle_when_every_message_is_known_and_nothing_is_pending(env, capsys):
    seed({}, skipped=["M2"])
    FakeMcp.reply = ONE + "Message ID: M2\n"
    assert run(capsys, "check") == (0, "DOOR idle\n")
    assert [t for t, _ in FakeMcp.calls] == ["search_gmail_messages"]


def test_scan_gate_idle_on_a_fresh_install_with_an_empty_window(env, capsys):
    assert run(capsys, "check") == (0, "DOOR idle\n")


@pytest.mark.parametrize(
    "rec,why",
    [
        ({"status": "converted"}, "waits for its report"),
        ({"drive": "pending"}, "Drive filing is pending"),
        ({"logged": False}, "vault log line is pending"),
    ],
)
def test_scan_gate_wakes_for_pending_work_without_searching(env, capsys, rec, why):
    seed(rec)
    rc, out = run(capsys, "check")
    assert rc == 0 and out.startswith("DOOR wake\n") and why in out
    assert FakeMcp.calls == []


def test_scan_gate_wakes_for_a_new_message(env, capsys):
    seed({})
    FakeMcp.reply = ONE + "Message ID: NEW\n"
    rc, out = run(capsys, "check")
    assert out == "DOOR wake\na message in the window is new to the scan\n"


def test_scan_gate_wakes_when_the_mailbox_cannot_be_searched(env, capsys):
    FakeMcp.fail = True
    rc, out = run(capsys, "check")
    assert rc == 0 and out.startswith("DOOR wake\nmail check failed: mail down")


# --- needs-you mail reader gate -----------------------------------------------


def test_mail_gate_idle_stamps_the_marker_and_reads_the_readers_window(env, capsys):
    assert run(capsys, "mail-check") == (0, "MAIL idle\n")
    ((tool, args),) = FakeMcp.calls
    assert args["query"] in ("in:inbox newer_than:16h", "in:inbox newer_than:8h")
    rc, out = run(capsys, "mail-idle")
    assert out.startswith("idle ") and int(out.split()[1]) > 0


def test_mail_idle_is_none_before_any_idle_check(env, capsys):
    assert run(capsys, "mail-idle") == (0, "none\n")


def test_mail_gate_wakes_when_mail_arrived_and_stamps_nothing(env, capsys):
    FakeMcp.reply = ONE
    assert run(capsys, "mail-check") == (0, "MAIL wake\nmail arrived in the window\n")
    assert run(capsys, "mail-idle")[1] == "none\n"


@pytest.mark.parametrize(
    "reply,why",
    [("something unexpected", "not recognised as empty"), ("", "not recognised")],
)
def test_mail_gate_wakes_on_a_reply_it_cannot_read(env, capsys, reply, why):
    FakeMcp.reply = reply
    rc, out = run(capsys, "mail-check")
    assert out.startswith("MAIL wake\n") and why in out
    assert run(capsys, "mail-idle")[1] == "none\n"


def test_mail_gate_wakes_when_the_mailbox_is_down(env, capsys):
    FakeMcp.fail = True
    assert run(capsys, "mail-check")[1].startswith("MAIL wake\nmail check failed")


def test_a_gate_never_files_converts_or_reports(env, capsys):
    run(capsys, "check")
    run(capsys, "mail-check")
    assert not (door.HOME / "documents").exists()
    assert door.load_state() == {"docs": {}}


# --- the stored trigger scripts -----------------------------------------------

RUNNER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const out = process.argv[2];
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const exec = async ({ command }) => {
  process.stderr.write(command + "\n");
  return { aggregated: out };
};
new AsyncFunction("exec", src)(exec).then(
  (r) => process.stdout.write(JSON.stringify(r)),
  (e) => { process.stdout.write("THROW " + e.message); },
);
"""
DOOR_PATH = "/home/node/.openclaw/agents/reader/workspace/skills/doc-door/door.py"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize(
    "script,sub,head",
    [("scan.js", "check", "DOOR"), ("mail.js", "mail-check", "MAIL")],
)
def test_gate_js_idle_fires_nothing_busy_fires_with_the_reason_and_odd_throws(
    script, sub, head
):
    def go(out):
        r = subprocess.run(
            ["node", "-e", RUNNER, str(GATES / script), out],
            capture_output=True,
            text=True,
            check=True,
        )
        assert r.stderr.strip() == f"{DOOR_PATH} {sub}"
        return json.loads(r.stdout) if r.stdout.startswith("{") else r.stdout

    assert go(f"{head} idle\n") == {"fire": False}
    assert go(f"{head} wake\nwhy one\nwhy two\n") == {
        "fire": True,
        "message": "Gate wake reason: why one; why two",
    }
    assert go(f"{head} wake\n") == f"THROW door.py {sub}: {head} wake"
    assert go("") == f"THROW door.py {sub}: no output"
