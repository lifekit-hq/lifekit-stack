"""Behaviour of scripts/unrouted (lifekit-stack#687): the daily check that every
owner ask and agent promise in the agents' chats has a receipt.

What this file pins: only the owner's own conversations are read (no cron run,
no deploy smoke turn, no other sender); a receipt counts when it exists where it
lives (a task note, a captain-held item, the intake spool, a receipt file) and
a cited id that does not exist does not; each unrouted item is filed through
the door once, with a thread that never carries the session key; the state
keeps the last seven days; `--check` is the 26-hour dead-man; and the replay
test catches the three asks the issue names. Every chat here is synthetic; the
real replay runs only on the host, against the private corpus at
UNROUTED_REPLAY (docs/runbook.md "Daily unrouted check").
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/unrouted/unrouted.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures/unrouted"
CHATS = FIXTURES / "chats.jsonl"
FAKE_MODEL = FIXTURES / "fake_model.py"

spec = importlib.util.spec_from_file_location("unrouted", SCRIPT)
unrouted = importlib.util.module_from_spec(spec)
sys.modules["unrouted"] = unrouted
spec.loader.exec_module(unrouted)

DAY_24 = 1795564800  # 2026-11-25T00:00:00Z: the run that reads 2026-11-24
DAY_25 = DAY_24 + 86400
# The three asks the issue names, by the UTC day and minute the owner said them.
TARGETS = ("2026-11-24 15:57", "2026-11-25 22:11", "2026-11-25 22:12")

HOLD = """#!/usr/bin/env bash
case "$2" in held-dentist-booking) exit 0;; held-broken) exit 9;; *) exit 3;; esac
"""
DOOR = f"""#!{sys.executable}
import json, os, sys
item = json.load(sys.stdin)
log = os.environ["FAKE_DOOR_LOG"]
n = sum(1 for _ in open(log)) if os.path.exists(log) else 0
open(log, "a").write(json.dumps(item) + "\\n")
print(json.dumps({{"id": f"spool:{{n:016x}}", "url": "", "state": "spooled"}}))
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "fm"
    (home / "state/inbox/handled").mkdir(parents=True)
    (home / "state/inbox/handled/1795000000-AbCdEf.note").write_text("note\n")
    (home / "bin").mkdir()
    hold = home / "bin/fm-captain-hold.sh"
    hold.write_text(HOLD)
    hold.chmod(0o755)
    door = tmp_path / "door.py"
    door.write_text(DOOR)
    door.chmod(0o755)
    monkeypatch.setenv("UNROUTED_MODEL_CMD", f"{sys.executable} {FAKE_MODEL}")
    monkeypatch.setenv("UNROUTED_DOOR_CMD", str(door))
    monkeypatch.setenv("FAKE_DOOR_LOG", str(tmp_path / "door.log"))
    monkeypatch.setenv("TASKS_AXI", "true")

    class Env:
        state = tmp_path / "state"
        log = tmp_path / "door.log"

        def run(self, now, *extra):
            return unrouted.main(
                [
                    "--chats", str(CHATS), "--state", str(self.state),
                    "--home", str(home), "--now", str(now), *extra,
                ]
            )  # fmt: skip

        def doc(self):
            return json.loads((self.state / "unrouted.json").read_text())

        def filed(self):
            if not self.log.exists():
                return []
            return [json.loads(line) for line in self.log.read_text().splitlines()]

    return Env()


def dry(env, now, capsys):
    assert env.run(now, "--no-file") == 0
    return json.loads(capsys.readouterr().out)


def minute(ts):
    return ts[:16].replace("T", " ")


def test_only_the_owners_own_conversations_are_read():
    convs = unrouted.conversations(
        unrouted.chats_from_file(CHATS, *unrouted.day_window("2026-11-24"))
    )
    keys = [c.session_key for c in convs]
    assert keys == ["agent:kit:telegram:direct:100000001"]
    convs = unrouted.conversations(
        unrouted.chats_from_file(CHATS, *unrouted.day_window("2026-11-25"))
    )
    assert sorted(c.agent for c in convs) == ["kit", "learnclaw"]
    assert not any("group" in c.session_key for c in convs)


def test_a_receipt_in_the_turn_routes_the_ask(env, capsys):
    out = dry(env, DAY_24, capsys)
    assert out["day"] == "2026-11-24" and out["conversations"] == 1
    (item,) = out["items"]
    assert minute(item["ask_at"]) == "2026-11-24 15:57"
    assert item["routed"] and item["receipt"] == "1795000000-AbCdEf"


def test_receipts_are_checked_where_they_live(env, capsys):
    out = dry(env, DAY_25, capsys)
    by_minute = {minute(i["ask_at"]): i for i in out["items"]}
    assert by_minute["2026-11-25 22:20"]["routed"]  # held item exists
    assert by_minute["2026-11-25 22:20"]["receipt"] == "held-dentist-booking"
    renew = by_minute["2026-11-25 22:30"]  # cited note does not exist
    assert not renew["routed"] and renew["receipt_cited"] == "1795999999-ZzZzZz"
    for m in ("2026-11-25 22:09", "2026-11-25 22:11", "2026-11-25 22:12"):
        assert not by_minute[m]["routed"] and by_minute[m]["kind"] == "promise"


def test_receipt_lookup_by_kind(tmp_path):
    home = tmp_path / "fm"
    (home / "state/inbox").mkdir(parents=True)
    (home / "state/inbox/1795000001-Open01.note").write_text("")
    (home / "bin").mkdir()
    (home / "bin/fm-captain-hold.sh").write_text(HOLD)
    (home / "bin/fm-captain-hold.sh").chmod(0o755)
    index = tmp_path / "index.md"
    index.write_text("| shape-tax-form | accepted |\n")
    errors: list[str] = []
    r = unrouted.Receipts([str(home)], [str(index)], errors)
    assert r.check("note 1795000001-Open01") == ("found", "1795000001-Open01")
    assert r.check("note 1795000002-Gone02") == ("missing", "1795000002-Gone02")
    assert r.check("spool:0123456789abcdef") == ("found", "spool:0123456789abcdef")
    assert r.check("see shape-tax-form") == ("found", "shape-tax-form")
    assert r.check("held-nothing-here") == ("missing", "held-nothing-here")
    assert r.check("no receipt at all") == (None, None)
    assert errors == []
    assert r.check("held-broken") == ("unchecked", "held-broken")
    assert errors and "held-broken" in errors[0]


def test_unrouted_items_are_filed_once_without_the_session_key(env):
    assert env.run(DAY_25) == 0
    filed = env.filed()
    assert len(filed) == 4
    for item in filed:
        assert item["project"] == "fleet"
        assert item["envelope"]["owner"] == "firstmate"
        assert item["envelope"]["origin"].startswith("unrouted-check/")
        assert item["fingerprint"].startswith("unrouted-")
        assert "100000001" not in json.dumps(item)
    doc = env.doc()
    assert doc["schema"] == "lifekit-unrouted.v1" and doc["day"] == "2026-11-25"
    assert doc["asks_and_promises"] == 5 and doc["routed"] == 1
    assert {i["filed"] for i in doc["items"]} == {f"spool:{n:016x}" for n in range(4)}
    assert all("routed" not in i for i in doc["items"])
    assert env.run(DAY_25 + 3600) == 0  # a rerun of the same day files nothing new
    assert len(env.filed()) == 4
    assert len(env.doc()["items"]) == 4


def test_the_door_item_carries_no_chat_words(env):
    """`fleet` files into a public repo: the filed item names the agent, the
    day and the thread, and none of the words the chat said."""
    assert env.run(DAY_25) == 0
    texts = [
        json.loads(line)["text"]
        for line in CHATS.read_text().splitlines()
        if json.loads(line)["ts"].startswith("2026-11-25")
    ]
    quotes = [i["quote"] for i in env.doc()["items"]]
    assert quotes and all(quotes)
    filed = env.filed()
    assert len(filed) == 4
    for item in filed:
        payload = json.dumps(item)
        for words in texts + quotes:
            assert words not in payload
            assert json.dumps(words)[1:-1] not in payload
        assert item["envelope"]["thread"].endswith("/telegram/2026-11-25")
        assert "2026-11-25" in item["title"] and "2026-11-25" in item["body"]


def chat(ts, role, text, owner=None):
    return {
        "agent": "kit", "session_key": "agent:kit:telegram:direct:100000002",
        "channel": "telegram", "chat_type": "direct", "ts": ts, "role": role,
        "owner": owner, "text": text,
    }  # fmt: skip


def test_two_asks_with_the_same_words_are_both_filed(env, tmp_path):
    chats = tmp_path / "same.jsonl"
    chats.write_text(
        "".join(
            json.dumps(m) + "\n"
            for m in (
                chat(
                    "2026-11-25T09:00:00.000Z", "assistant", "Plan A: rename the job."
                ),
                chat("2026-11-25T09:01:00.000Z", "user", "do this", owner=True),
                chat("2026-11-25T09:01:10.000Z", "assistant", "Noted."),
                chat(
                    "2026-11-25T17:00:00.000Z", "assistant", "Plan B: split the page."
                ),
                chat("2026-11-25T17:02:00.000Z", "user", "do this", owner=True),
                chat("2026-11-25T17:02:10.000Z", "assistant", "Noted."),
            )
        )
    )
    assert env.run(DAY_25, "--chats", str(chats)) == 0
    items = env.doc()["items"]
    assert [i["quote"] for i in items] == ["do this", "do this"]
    assert len({i["fingerprint"] for i in items}) == 2
    filed = env.filed()
    assert len(filed) == 2
    assert len({f["fingerprint"] for f in filed}) == 2


def test_a_rerun_that_quotes_other_words_files_nothing_twice(env, monkeypatch):
    assert env.run(DAY_25) == 0
    first = env.doc()["items"]
    assert len(env.filed()) == 4
    monkeypatch.setenv("FAKE_MODEL_QUOTE_WORDS", "2")
    assert env.run(DAY_25 + 3600) == 0
    again = env.doc()["items"]
    assert [i["quote"] for i in again] != [i["quote"] for i in first]
    assert [i["fingerprint"] for i in again] == [i["fingerprint"] for i in first]
    assert len(env.filed()) == 4


def test_a_rerun_that_links_a_promise_differently_files_nothing_twice(env, monkeypatch):
    assert env.run(DAY_25) == 0
    first = env.doc()["items"]
    assert len(env.filed()) == 4
    # The rerun's promises carry no `about`: anchored on the agent's message,
    # not on the owner ask they answer.
    monkeypatch.setenv("FAKE_MODEL_UNLINK", "1")
    assert env.run(DAY_25 + 3600) == 0
    again = env.doc()["items"]
    # Same turns, so the same owner asks the fixture holds with no receipt.
    assert [minute(i["ask_at"]) for i in again] == [
        "2026-11-25 22:09",
        "2026-11-25 22:11",
        "2026-11-25 22:12",
        "2026-11-25 22:30",
    ]
    assert [i["fingerprint"] for i in again] == [i["fingerprint"] for i in first]
    assert len(env.filed()) == 4


def test_a_receipt_in_the_turn_does_not_route_another_promise(
    env, tmp_path, monkeypatch
):
    """One owner turn, two pieces of work: the bug the agent filed (with an
    issue receipt) and a second promise with none. The receipt routes only
    its own item; the promise is listed and filed once, whether the model
    gives the filed bug as the owner's ask or, on a rerun, as a promise about it."""
    chats = tmp_path / "turn.jsonl"
    chats.write_text(
        "".join(
            json.dumps(m) + "\n"
            for m in (
                chat("2026-11-25T10:00:00.000Z", "user", "File the bug.", owner=True),
                chat("2026-11-25T10:00:20.000Z", "assistant", "Filed acme/x#12."),
                chat("2026-11-25T10:00:40.000Z", "assistant", "I'll fix the timer."),
            )
        )
    )
    gh = tmp_path / "bin/gh"
    gh.parent.mkdir()
    gh.write_text('#!/bin/sh\n[ "$2" = repos/acme/x/issues/12 ] && echo 12\n')
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ['PATH']}")
    model = tmp_path / "model.py"
    monkeypatch.setenv("UNROUTED_MODEL_CMD", f"{sys.executable} {model}")
    promise = {
        "kind": "promise",
        "msg": 2,
        "about": None,
        "quote": "fix",
        "receipt": None,
    }
    ask = {
        "kind": "ask",
        "msg": 0,
        "about": None,
        "quote": "File",
        "receipt": "acme/x#12",
    }
    linked = {
        "kind": "promise",
        "msg": 1,
        "about": 0,
        "quote": "Filed",
        "receipt": None,
    }
    runs = []
    for n, first in enumerate((ask, linked)):
        answer = {"type": "result", "is_error": False, "total_cost_usd": 0.001,
                  "result": json.dumps({"items": [first, promise]})}  # fmt: skip
        model.write_text(f"print({json.dumps(json.dumps(answer))})\n")
        assert env.run(DAY_25 + n * 3600, "--chats", str(chats)) == 0
        doc = env.doc()
        assert doc["asks_and_promises"] == 2 and doc["routed"] == 1
        (item,) = doc["items"]
        assert item["kind"] == "promise" and item["at"] == "2026-11-25T10:00:40.000Z"
        runs.append(item["fingerprint"])
    assert runs[0] == runs[1]
    (filed,) = env.filed()
    assert filed["fingerprint"] == runs[0]
    assert "timer" not in json.dumps(filed)


def test_routed_items_are_not_listed(env):
    assert env.run(DAY_24) == 0
    assert env.filed() == []
    assert env.doc()["items"] == [] and env.doc()["routed"] == 1


def test_the_state_keeps_seven_days(env):
    assert env.run(DAY_25) == 0
    assert len(env.doc()["items"]) == 4
    assert env.run(DAY_25 + 6 * 86400) == 0  # reads 2026-12-01: day 25 still kept
    assert len(env.doc()["items"]) == 4
    assert env.run(DAY_25 + 7 * 86400) == 0  # reads 2026-12-02: day 25 aged out
    assert env.doc()["items"] == []


def test_a_failed_filing_is_an_error_and_retried(env, monkeypatch):
    monkeypatch.setenv("UNROUTED_DOOR_CMD", "false")
    assert env.run(DAY_25) == 1
    doc = env.doc()
    assert len(doc["items"]) == 4 and not any("filed" in i for i in doc["items"])
    assert len(doc["errors"]) == 4
    monkeypatch.setenv("UNROUTED_DOOR_CMD", str(env.log.parent / "door.py"))
    assert env.run(DAY_25 + 60) == 0
    assert len(env.filed()) == 4


def test_unreadable_chats_record_no_run(env, monkeypatch):
    monkeypatch.setenv("UNROUTED_CHATS_CMD", "false")
    assert unrouted.main(["--state", str(env.state), "--now", str(DAY_25)]) == 1
    assert not (env.state / "unrouted.json").exists()


def test_check_is_the_dead_man(env, capsys):
    assert unrouted.main(["--check", "--state", str(env.state)]) == 1
    assert json.loads(capsys.readouterr().out)["stale"] is True
    assert env.run(DAY_25) == 0
    capsys.readouterr()
    fresh = ["--check", "--state", str(env.state), "--now", str(DAY_25 + 25 * 3600)]
    assert unrouted.main(fresh) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stale"] is False and out["unrouted"] == 4
    assert out["missed_day"] is None
    stale = ["--check", "--state", str(env.state), "--now", str(DAY_25 + 27 * 3600)]
    assert unrouted.main(stale) == 1
    assert json.loads(capsys.readouterr().out)["missed_day"] == "2026-11-26"


def test_a_backfill_of_an_older_day_keeps_the_missed_day(env, capsys):
    # The timer checks 2026-11-26; the operator then reruns 2026-11-24 by hand.
    assert env.run(DAY_25 + 86400) == 0
    assert env.run(DAY_25 + 86400 + 3600, "--day", "2026-11-24") == 0
    assert env.doc()["day"] == "2026-11-24"
    capsys.readouterr()
    stale = ["--check", "--state", str(env.state), "--now", str(DAY_25 + 4 * 86400)]
    assert unrouted.main(stale) == 1
    # The latest day checked is still 2026-11-26: the first missed one is the 27th.
    assert json.loads(capsys.readouterr().out)["missed_day"] == "2026-11-27"


def test_a_bad_day_is_usage(env):
    assert env.run(DAY_25, "--day", "yesterday") == 64


def test_the_model_runs_with_no_tools_and_no_settings(monkeypatch):
    monkeypatch.delenv("UNROUTED_MODEL_CMD", raising=False)
    argv = unrouted.model_argv("haiku")
    for flag in (
        "--tools",
        "--strict-mcp-config",
        "--setting-sources",
        "--no-session-persistence",
    ):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--tools") + 1] == ""


def test_the_model_answer_is_checked():
    answer = json.dumps(
        {
            "items": [
                {"kind": "ask", "msg": 1, "quote": "a"},
                {"kind": "promise", "msg": 9, "quote": "out of range"},
                {"kind": "note", "msg": 1, "quote": "not a kind"},
                {"kind": "promise", "msg": 2, "about": 1, "quote": "b", "receipt": 7},
            ]
        }
    )
    got = unrouted.parse_items(f"Here you go: {answer}", 0, 3)
    assert [(i["kind"], i["msg"], i["about"], i["receipt"]) for i in got] == [
        ("ask", 1, None, None),
        ("promise", 2, 1, None),
    ]
    with pytest.raises(ValueError):
        unrouted.parse_items("no json", 0, 3)


def test_replay_catches_the_named_asks_synthetic(env, capsys):
    caught = set()
    for now in (DAY_24, DAY_25):
        caught |= {minute(i["ask_at"]) for i in dry(env, now, capsys)["items"]}
    assert set(TARGETS) <= caught


REPLAY = Path(
    os.environ.get("UNROUTED_REPLAY")
    or Path.home() / ".local/share/lifekit-unrouted/replay.jsonl"
)


@pytest.mark.skipif(
    not REPLAY.is_file()
    or not shutil.which(os.environ.get("UNROUTED_CLAUDE") or "claude"),
    reason="the private replay corpus (UNROUTED_REPLAY) or Claude Code is not on this host",
)
def test_replay_catches_the_named_asks_real(tmp_path):
    """The real 2026-11-22..26 chats through the real model (the host only)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("UNROUTED_") or k == "UNROUTED_CLAUDE"
    }
    caught = set()
    for day in ("2026-11-24", "2026-11-25"):
        res = subprocess.run(
            [
                sys.executable, str(SCRIPT), "--chats", str(REPLAY), "--day", day,
                "--state", str(tmp_path), "--no-file",
                "--model", os.environ.get("UNROUTED_MODEL") or unrouted.DEFAULT_MODEL,
            ],
            env=env, capture_output=True, text=True, timeout=1200,
        )  # fmt: skip
        assert res.returncode in (0, 1), res.stderr
        out = json.loads(res.stdout)
        assert not any("extraction failed" in e for e in out["errors"]), out["errors"]
        caught |= {minute(i["ask_at"]) for i in out["items"]}
    missed = set(TARGETS) - caught
    assert not missed, f"the extractor missed {sorted(missed)}"


def run_installer(tmp_path, **extra):
    root = tmp_path / "root"
    res = subprocess.run(
        ["bash", str(REPO / "scripts/unrouted/install-unrouted.sh")],
        env={
            "PATH": "/usr/bin:/bin",
            "INSTALL_ROOT": str(root),
            "SYSTEMCTL": "true",
            "ADMIN_USER": "ops",
            "UNROUTED_FM_HOMES": "/home/ops/fm /home/ops/fm-two",
            "TASKS_AXI": "/home/ops/.nvm/bin/tasks-axi",
            "CLAUDE_BIN": "/home/ops/.local/bin/claude",
            **extra,
        },
        capture_output=True,
        text=True,
    )
    return root, res


def test_installer_renders_the_unit(tmp_path):
    root, res = run_installer(tmp_path)
    assert res.returncode == 0, res.stderr
    unit = (root / "etc/systemd/system/lifekit-unrouted.service").read_text()
    assert "__" not in unit
    assert "User=ops" in unit
    assert "--home /home/ops/fm --home /home/ops/fm-two" in unit
    assert '"UNROUTED_CLAUDE=/home/ops/.local/bin/claude"' in unit
    assert '"PATH=/home/ops/.nvm/bin:/home/ops/.local/bin:/usr/local/sbin' in unit
    assert "ProtectSystem=strict" in unit
    assert "/srv/lifekit-stack/scripts/unrouted/unrouted.py" in unit
    assert (root / "etc/systemd/system/lifekit-unrouted.timer").is_file()
    assert (root / "var/lib/lifekit-fleet/unrouted").is_dir()


def test_installer_skips_without_homes_and_needs_claude(tmp_path):
    root, res = run_installer(tmp_path, UNROUTED_FM_HOMES="")
    assert res.returncode == 0 and "skipping" in res.stderr
    assert not (root / "etc/systemd/system/lifekit-unrouted.service").exists()
    _, res = run_installer(tmp_path, CLAUDE_BIN="relative/claude")
    assert res.returncode == 1 and "Claude Code not found" in res.stderr
