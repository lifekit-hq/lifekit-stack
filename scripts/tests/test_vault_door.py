"""Behaviour of scripts/vault-door against a fake vault and a fake firstmate home.

What this file pins: a ChatGPT note is only a `- DATE - chatgpt: ...` line in
inbox.md, it becomes exactly one `proposal:` captain-inbox note however often
it is written or the bridge runs, a failed send leaves the vault untouched so
the next run retries, and the snapshot page never carries a credential or a
host path and is never replaced by a half-built one.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/vault-door"
SCRIPT = DIR / "vault_door.py"
INSTALLER = DIR / "install-vault-door.sh"

spec = importlib.util.spec_from_file_location("vault_door", SCRIPT)
vd = importlib.util.module_from_spec(spec)
sys.modules["vault_door"] = vd
spec.loader.exec_module(vd)

NOW = 1793000000  # 2026-10-26T07:33:20Z
HEADER = "# Inbox\n\nItems older than 30 days rotate to inbox-archive.md.\n\n"

# Stands in for fm-inbox.sh: one file per request id, so a repeated id replays.
FAKE_INBOX = r"""#!/usr/bin/env python3
import json, os, sys
home = os.environ["FM_HOME"]
args = sys.argv[1:]
if os.path.exists(home + "/fail"):
    sys.stderr.write("boom\n")
    sys.exit(int(open(home + "/fail").read() or 1))
rid = args[args.index("--request-id") + 1]
body = sys.stdin.read()
with open(home + "/env", "w") as f:
    json.dump(sorted(os.environ), f)
req = home + "/requests"
os.makedirs(req, exist_ok=True)
path = req + "/" + rid
outcome = "replay" if os.path.exists(path) else "created"
if outcome == "created":
    with open(path, "w") as f:
        f.write(body)
print(json.dumps({"schema": "fm-inbox-note.v1", "outcome": outcome, "id": "note-" + rid[-6:],
                  "request_id": rid, "saved": True, "announced": True, "acknowledged": False, "path": path}))
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "system").mkdir(parents=True)
    (vault / "inbox.md").write_text(HEADER)
    home = tmp_path / "fm"
    (home / "bin").mkdir(parents=True)
    fake = home / "bin" / "fm-inbox.sh"
    fake.write_text(FAKE_INBOX)
    fake.chmod(0o755)
    monkeypatch.setenv("VAULT_DOOR_VAULT", str(vault))
    monkeypatch.setenv("VAULT_DOOR_FM_HOME", str(home))
    monkeypatch.setenv("VAULT_DOOR_NOW", str(NOW))
    return tmp_path


def notes(env: Path) -> list[str]:
    folder = env / "fm" / "requests"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def inbox(env: Path) -> str:
    return (env / "vault" / "inbox.md").read_text()


def write_inbox(env: Path, *items: str) -> None:
    (env / "vault" / "inbox.md").write_text(HEADER + "\n".join(items) + "\n")


# ---------------------------------------------------------------- parsing


def test_parses_only_chatgpt_lines_with_a_real_date():
    lines = [
        "- 2026-10-20 - kit: not chatgpt's",
        "- 2026-10-21 - chatgpt: first idea",
        "  continued here",
        "- 2026-02-31 - chatgpt: impossible date",
        "- 2026-10-22 — ChatGPT: em dash and any case",
        "",
        "  not part of it: a blank line ended the item",
    ]
    items = vd.parse_items(lines)
    assert [i.text for i in items] == [
        "first idea\ncontinued here",
        "em dash and any case",
    ]
    assert [(i.first, i.last) for i in items] == [(1, 2), (4, 4)]


def test_marked_lines_are_done_and_do_not_change_the_key():
    plain = vd.parse_items(["- 2026-10-21 - chatgpt: same idea"])[0]
    marked = vd.parse_items(["- 2026-10-21 - chatgpt: same idea [forwarded: n-1]"])[0]
    refused = vd.parse_items(
        ["- 2026-10-21 - chatgpt: same idea [not forwarded: empty]"]
    )[0]
    assert not plain.done and marked.done and refused.done
    assert plain.key == marked.key == refused.key


def test_key_ignores_date_and_whitespace_but_not_words():
    one = vd.parse_items(["- 2026-10-21 - chatgpt: a  b"])[0]
    two = vd.parse_items(["- 2026-10-25 - chatgpt:   a b  "])[0]
    other = vd.parse_items(["- 2026-10-21 - chatgpt: a c"])[0]
    assert one.key == two.key != other.key
    assert re.fullmatch(r"gpt-[0-9a-f]{24}", one.key)


def test_note_body_is_a_held_proposal_with_provenance():
    item = vd.parse_items(["- 2026-10-21 - chatgpt: try X", "  then Y"])[0]
    body = vd.note_body(item)
    assert body.startswith("proposal: try X\nthen Y\n")
    assert "untrusted" in body and item.key in body and "2026-10-21" in body


# ---------------------------------------------------------------- bridge


def test_bridge_forwards_once_and_marks(env):
    write_inbox(env, "- 2026-10-21 - chatgpt: an idea", "- 2026-10-21 - kit: leave me")
    assert vd.bridge() == 0
    assert len(notes(env)) == 1
    text = inbox(env)
    assert re.search(r"chatgpt: an idea \[forwarded: note-[0-9a-f]{6}\]\n", text)
    assert "kit: leave me\n" in text and "kit: leave me [" not in text
    assert vd.bridge() == 0
    assert len(notes(env)) == 1
    assert inbox(env) == text


def test_note_body_reaches_inbox_as_proposal(env):
    write_inbox(env, "- 2026-10-21 - chatgpt: an idea")
    vd.bridge()
    body = (env / "fm" / "requests" / notes(env)[0]).read_text()
    assert body.startswith("proposal: an idea\n")


def test_marker_removed_replays_without_a_second_note(env):
    write_inbox(env, "- 2026-10-21 - chatgpt: an idea")
    vd.bridge()
    marked = inbox(env)
    write_inbox(env, "- 2026-10-21 - chatgpt: an idea")  # a sync clobbered the marker
    assert vd.bridge() == 0
    assert len(notes(env)) == 1
    assert inbox(env) == marked


def test_identical_text_on_two_lines_is_one_note_both_marked(env):
    write_inbox(env, "- 2026-10-21 - chatgpt: same", "- 2026-10-22 - chatgpt: same")
    vd.bridge()
    assert len(notes(env)) == 1
    assert inbox(env).count("[forwarded: ") == 2


def test_send_failure_leaves_the_file_untouched_and_retries(env):
    write_inbox(env, "- 2026-10-21 - chatgpt: an idea")
    before = inbox(env)
    (env / "fm" / "fail").write_text("3")  # saved but not announced
    assert vd.bridge() == 1
    assert inbox(env) == before
    (env / "fm" / "fail").unlink()
    assert vd.bridge() == 0
    assert len(notes(env)) == 1 and "[forwarded: " in inbox(env)


def test_one_bad_note_does_not_block_the_others(env, monkeypatch):
    write_inbox(env, "- 2026-10-21 - chatgpt: one", "- 2026-10-21 - chatgpt: two")
    real = vd.send_note
    monkeypatch.setattr(
        vd, "send_note", lambda h, r, b: (None, "x") if "one" in b else real(h, r, b)
    )
    assert vd.bridge() == 1
    assert "two [forwarded: " in inbox(env) and "one [" not in inbox(env)


def test_oversize_and_empty_are_refused_not_sent(env):
    write_inbox(
        env,
        "- 2026-10-21 - chatgpt: " + "x" * (vd.NOTE_MAX_BYTES + 1),
        "- 2026-10-22 - chatgpt:",
    )
    assert vd.bridge() == 0
    assert notes(env) == []
    text = inbox(env)
    assert "[not forwarded: over 16 KiB]" in text and "[not forwarded: empty]" in text
    assert vd.bridge() == 0 and inbox(env) == text


def test_per_run_cap_defers_the_rest(env):
    write_inbox(
        env,
        *[f"- 2026-10-21 - chatgpt: idea {n}" for n in range(vd.BRIDGE_PER_RUN + 2)],
    )
    vd.bridge()
    assert len(notes(env)) == vd.BRIDGE_PER_RUN
    vd.bridge()
    assert len(notes(env)) == vd.BRIDGE_PER_RUN + 2
    assert inbox(env).count("[forwarded: ") == vd.BRIDGE_PER_RUN + 2


def test_model_pass_lock_skips_the_bridge(env):
    write_inbox(env, "- 2026-10-21 - chatgpt: an idea")
    lock = env / "vault" / ".model-pass"
    lock.write_text("")
    os.utime(lock, (NOW - 600, NOW - 600))
    assert vd.bridge() == 0 and notes(env) == []
    os.utime(lock, (NOW - 7200, NOW - 7200))  # stale lock
    assert vd.bridge() == 0 and len(notes(env)) == 1


def test_fm_inbox_runs_with_a_minimal_environment(env, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN_FOR_TEST", "s3cret")
    write_inbox(env, "- 2026-10-21 - chatgpt: an idea")
    vd.bridge()
    names = json.loads((env / "fm" / "env").read_text())
    assert "SECRET_TOKEN_FOR_TEST" not in names and "FM_HOME" in names


def test_bridge_needs_an_absolute_firstmate_home(env, monkeypatch):
    monkeypatch.setenv("VAULT_DOOR_FM_HOME", "")
    assert vd.bridge() == 78
    monkeypatch.setenv("VAULT_DOOR_FM_HOME", "relative/fm")
    assert vd.bridge() == 78


def test_marker_goes_on_the_fresh_file_not_the_one_read_earlier(env, monkeypatch):
    write_inbox(env, "- 2026-10-21 - chatgpt: an idea")
    real = vd.send_note

    def send_and_append(home, rid, body):  # ChatGPT's outbox lands during the send
        result = real(home, rid, body)
        with (env / "vault" / "inbox.md").open("a") as handle:
            handle.write("- 2026-10-26 - chatgpt: late idea\n")
        return result

    monkeypatch.setattr(vd, "send_note", send_and_append)
    vd.bridge()
    text = inbox(env)
    assert "an idea [forwarded: " in text and "late idea\n" in text


def test_in_place_write_keeps_the_inode(env):
    write_inbox(env, "- 2026-10-21 - chatgpt: an idea")
    before = (env / "vault" / "inbox.md").stat().st_ino
    vd.bridge()
    assert (env / "vault" / "inbox.md").stat().st_ino == before


# ---------------------------------------------------------------- program


def test_the_script_runs_as_a_program(env):
    write_inbox(env, "- 2026-10-21 - chatgpt: an idea")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "bridge"],
        capture_output=True,
        text=True,
        env={**os.environ},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(notes(env)) == 1
    for args in (["nope"], ["snapshot"]):
        assert (
            subprocess.run(
                [sys.executable, str(SCRIPT), *args], capture_output=True, check=False
            ).returncode
            == 64
        )


# ---------------------------------------------------------------- installer


def test_installer_installs_units_and_enables_the_timers(tmp_path):
    root = tmp_path / "root"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f"#!/bin/sh\necho \"$@\" >> '{tmp_path}/calls'\n")
    systemctl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "INSTALL_ROOT": str(root),
            "SYSTEMCTL": str(systemctl),
            "FM_HOME": "/srv/fm/main",
            "ADMIN_USER": "tester",
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr
    unit_dir = root / "etc/systemd/system"
    bridge = (unit_dir / "lifekit-vault-door-bridge.service").read_text()
    assert (
        "User=tester" in bridge
        and "VAULT_DOOR_FM_HOME=/srv/fm/main" in bridge
        and "__" not in bridge
    )
    assert (unit_dir / "lifekit-vault-door-bridge.timer").exists()
    assert not list(unit_dir.glob("lifekit-vault-door-snapshot.*"))
    assert os.access(
        root / "usr/local/libexec/lifekit-vault-door/vault_door.py", os.X_OK
    )
    calls = (tmp_path / "calls").read_text()
    assert "daemon-reload" in calls
    assert "enable --now lifekit-vault-door-bridge.timer" in calls
    assert "snapshot" not in calls


def test_installer_skips_without_a_firstmate_home_and_rejects_odd_paths(tmp_path):
    base = {**os.environ, "INSTALL_ROOT": str(tmp_path), "SYSTEMCTL": "true"}
    base.pop("FM_HOME", None)
    skip = subprocess.run(
        ["bash", str(INSTALLER)], capture_output=True, text=True, env=base, check=False
    )
    assert skip.returncode == 0 and "skipping" in skip.stderr
    bad = subprocess.run(
        ["bash", str(INSTALLER)],
        capture_output=True,
        text=True,
        env={**base, "FM_HOME": '/x"; rm -rf /'},
        check=False,
    )
    assert bad.returncode == 1
