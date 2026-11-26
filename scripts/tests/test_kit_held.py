"""Behaviour of scripts/kit-held: a held item that names a fleet task is filed as
a shaping item when Kit holds it (lifekit-stack#684).

What this file pins: a held item opens exactly one shaping item (a captain-held
task, the Calls page) whose reason carries the 0016 envelope; a repeat of the
same fingerprint adds a count to the open item, and a repeat of an answered one
opens a new item naming it; a reminder to Kit itself is refused and nothing is
filed; a relay retry of the same request is a replay with no second count; the
check lists the open fleet items of Kit's daily notes that have no shaping
item; and the hold verb runs end to end from relay.sh through the kit-relay
forced command. The firstmate home and tasks-axi are stubs; all fixtures are
synthetic.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/kit-held/kit_held.py"
INSTALLER = REPO / "scripts/kit-relay/install-kit-relay.sh"
CLIENT = REPO / "skills/secondmate-relay/relay.sh"

HOLD = """#!{py}
import json, os, sys
s = {state!r} + "/hub.json"
d = json.load(open(s)) if os.path.exists(s) else {{}}
a = sys.argv[1:]
with open({state!r} + "/calls.jsonl", "a") as f:
    f.write(json.dumps(["hold"] + a) + "\\n")
if a[0] == "open":
    st = d.get(a[1], {{}}).get("state")
    sys.exit({{"open": 0, "closed": 1, None: 3}}[st])
if a[0] == "hold":
    opt = dict(zip(a[2::2], a[3::2]))
    old = d.get(a[1], {{}})
    d[a[1]] = {{
        "state": "open",
        "title": opt.get("--title", old.get("title")),
        "reason": opt["--reason"],
    }}
    json.dump(d, open(s, "w"))
"""
TASKS = """#!{py}
import json, os, sys
s = {state!r} + "/hub.json"
d = json.load(open(s))
a = sys.argv[1:]
with open({state!r} + "/calls.jsonl", "a") as f:
    f.write(json.dumps(["tasks"] + a) + "\\n")
if a[0] == "update":
    d[a[1]]["title"] = a[3]
    json.dump(d, open(s, "w"))
if a[0] == "show":
    print("task:")
    print("  id: " + a[1])
    print("  title: " + json.dumps(d[a[1]]["title"]))
"""

FAKE_SSH_FORCED = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
assert args[:3] == ["-F", "/run/lifekit/kit-relay/ssh_config", "relay"], args
os.environ["SSH_ORIGINAL_COMMAND"] = " ".join(args[3:])
os.execv(os.environ["KIT_RELAY_SCRIPT"], [os.environ["KIT_RELAY_SCRIPT"]])
"""
FAKE_LOGGER = "#!/bin/sh\nexit 0\n"
FAKE_INBOX = "#!/bin/sh\nexit 0\n"

ENVELOPE = {
    "origin": "telegram 2026-11-25",
    "owner": "firstmate",
    "class": "dev",
    "fingerprint": "coach-learnclaw-context",
    "acceptance": "the browser coach and learnclaw share one study context",
    "thread": "kit-main",
}
TEXT = "Share context between the browser coach and learnclaw."


def held(text: str = TEXT, **over: str) -> str:
    env = {**ENVELOPE, **over}
    head = "".join(f"{k}: {v}\n" for k, v in env.items() if v is not None)
    return f"---\n{head}---\n{text}\n"


def write_exec(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class Hub:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.state = tmp / "stub"
        self.state.mkdir()
        self.home = tmp / "fm-vps"
        (self.home / "bin").mkdir(parents=True)
        write_exec(
            self.home / "bin/fm-captain-hold.sh",
            HOLD.format(py=sys.executable, state=str(self.state)),
        )
        write_exec(self.home / "bin/fm-inbox.sh", FAKE_INBOX)
        self.bins = tmp / "stubs"
        self.bins.mkdir()
        self.tasks = write_exec(
            self.bins / "tasks-axi",
            TASKS.format(py=sys.executable, state=str(self.state)),
        )
        self.env = {
            **os.environ,
            "STUB_STATE": str(self.state),
            "FM_HOME": str(self.home),
            "TASKS_AXI": str(self.tasks),
            "KIT_HELD_STATE": str(tmp / "held-state"),
        }

    def file(self, body: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "file", *args],
            input=body,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def items(self) -> dict:
        path = self.state / "hub.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def close(self, item: str) -> None:
        d = self.items()
        d[item]["state"] = "closed"
        (self.state / "hub.json").write_text(json.dumps(d))

    def archive(self, item: str) -> None:
        """Done retention moves an answered item out of the backlog; the
        hub's `open` predicate then reads it as absent (exit 3)."""
        d = self.items()
        del d[item]
        (self.state / "hub.json").write_text(json.dumps(d))

    def calls(self) -> list[list[str]]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def need_tasks_axi_on_path(self) -> None:
        """fm-captain-hold.sh finds tasks-axi through PATH, as the real one does."""
        hold = self.home / "bin/fm-captain-hold.sh"
        hold.rename(self.home / "bin/hold-stub")
        write_exec(
            hold,
            "#!/bin/sh\ncommand -v tasks-axi >/dev/null || exit 127\n"
            f'exec "{self.home}/bin/hold-stub" "$@"\n',
        )


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path)


def receipt(result) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ─── filing ──────────────────────────────────────────────────────────────────


def test_a_held_fleet_task_opens_one_shaping_item_with_the_envelope(hub):
    r = receipt(hub.file(held()))
    assert r["id"] == "held-coach-learnclaw-context"
    assert r["state"] == "submitted" and r["count"] == 1
    assert r["url"] == "calls:fm-vps/held-coach-learnclaw-context"
    items = hub.items()
    assert list(items) == ["held-coach-learnclaw-context"]
    item = items["held-coach-learnclaw-context"]
    assert item["state"] == "open"
    assert item["title"].startswith("Held for firstmate: Share context")
    for field, value in ENVELOPE.items():
        assert f"{field}={value}" in item["reason"]
    assert TEXT in item["reason"]


def test_a_repeat_adds_a_count_to_the_open_item_and_files_nothing_new(hub):
    receipt(hub.file(held()))
    second = receipt(hub.file(held("Again: share the coach context.")))
    assert second == {
        "id": "held-coach-learnclaw-context",
        "url": "calls:fm-vps/held-coach-learnclaw-context",
        "state": "working",
        "count": 2,
    }
    third = receipt(hub.file(held()))
    assert third["count"] == 3
    items = hub.items()
    assert list(items) == ["held-coach-learnclaw-context"]
    assert items["held-coach-learnclaw-context"]["title"].endswith(" (seen 3x)")


def test_a_repeat_of_an_answered_item_opens_a_new_one_naming_it(hub):
    receipt(hub.file(held()))
    hub.close("held-coach-learnclaw-context")
    r = receipt(hub.file(held()))
    assert r["id"] == "held-coach-learnclaw-context-2"
    assert r["state"] == "submitted"
    assert r["repeat_of"] == "held-coach-learnclaw-context"
    new = hub.items()["held-coach-learnclaw-context-2"]
    assert "Repeat of held-coach-learnclaw-context" in new["reason"]


def test_a_repeat_of_an_answered_and_archived_item_still_names_it(hub):
    receipt(hub.file(held()))
    hub.close("held-coach-learnclaw-context")
    hub.archive("held-coach-learnclaw-context")
    r = receipt(hub.file(held()))
    assert r["id"] == "held-coach-learnclaw-context-2"
    assert r["state"] == "submitted"
    assert r["repeat_of"] == "held-coach-learnclaw-context"
    second = hub.items()["held-coach-learnclaw-context-2"]
    assert "Repeat of held-coach-learnclaw-context" in second["reason"]
    hub.close("held-coach-learnclaw-context-2")
    hub.archive("held-coach-learnclaw-context-2")
    third = receipt(hub.file(held()))
    assert third["id"] == "held-coach-learnclaw-context-3"
    assert third["repeat_of"] == "held-coach-learnclaw-context-2"


def test_filing_finds_tasks_axi_through_its_absolute_path(hub):
    hub.need_tasks_axi_on_path()
    env = {**hub.env, "PATH": "/usr/bin:/bin"}
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "file"],
        input=held(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert receipt(r)["id"] == "held-coach-learnclaw-context"


@pytest.mark.parametrize("owner", ["kit", "Kit", "me", "self"])
def test_a_reminder_to_kit_itself_is_refused_and_nothing_is_filed(hub, owner):
    r = hub.file(held(owner=owner))
    assert r.returncode == 65
    assert "reminder to Kit itself" in r.stderr
    assert hub.items() == {}
    assert not (hub.state / "calls.jsonl").exists()


@pytest.mark.parametrize("field", list(ENVELOPE))
def test_an_envelope_missing_a_field_is_refused(hub, field):
    r = hub.file(held(**{field: None}))
    assert r.returncode == 65
    assert f"envelope missing: {field}" in r.stderr
    assert hub.items() == {}


@pytest.mark.parametrize(
    "body",
    [
        TEXT,  # no envelope
        "---\norigin: x\n",  # never closed
        held(**{"class": "urgent"}),
        held(text=""),
    ],
)
def test_a_malformed_held_item_is_refused(hub, body):
    assert hub.file(body).returncode == 65
    assert hub.items() == {}


def test_a_replayed_request_returns_the_same_receipt_and_counts_nothing(hub):
    first = receipt(hub.file(held(), "--request-id", "kit-held-abc-20261125"))
    again = receipt(hub.file(held(), "--request-id", "kit-held-abc-20261125"))
    assert again["id"] == first["id"] and again["state"] == "working"
    assert again["replay"] is True
    item = hub.items()["held-coach-learnclaw-context"]
    assert "(seen" not in item["title"]


def test_no_home_is_exit_78(hub):
    env = {**hub.env, "FM_HOME": ""}
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "file"],
        input=held(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 78


# ─── the check ───────────────────────────────────────────────────────────────

NOW = int(dt.datetime(2026, 11, 26, 12, tzinfo=dt.timezone.utc).timestamp())


def check(hub, notes: Path, *extra: str, env: dict | None = None) -> list[dict]:
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "check",
            "--notes",
            str(notes),
            "--home",
            str(hub.home),
            "--seen",
            str(hub.tmp / "seen.json"),
            *extra,
        ],
        env=env or hub.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_check_lists_unfiled_fleet_items_and_skips_reminders_and_filed_ones(
    hub, tmp_path
):
    receipt(hub.file(held()))
    notes = tmp_path / "memory"
    notes.mkdir()
    (notes / "2026-11-25.md").write_text(
        "# 2026-11-25\n\n## Log\n- 22:11 he asked about the coach\n\n## Open\n"
        "- [ ] for firstmate: share the coach context (shaping: held-coach-learnclaw-context)\n"
        "- [ ] for devclaw: fix the backup job\n"
        "- [ ] for kit: ask him about the trip\n"
        "- [ ] for me: water the plants\n"
        "- [x] for firstmate: done already\n"
        "- [ ] for firstmate: a mark that names no item (shaping: held-nothing)\n"
        "Next: on 2026-11-26, raise it with Denys. On his go, send firstmate a task: note.\n"
        "Next: buy milk\n"
    )
    (notes / "notes.md").write_text("- [ ] for firstmate: not a daily note\n")
    found = check(hub, notes, "--now", str(NOW))
    titles = [f["title"] for f in found]
    assert titles == [
        "for devclaw: fix the backup job",
        "for firstmate: a mark that names no item",
        "on 2026-11-26, raise it with Denys. On his go, send firstmate a task: note.",
    ]
    assert all(f["note"] == "2026-11-25.md" for f in found)
    assert all(f["since"] == NOW for f in found)


def test_the_check_keeps_the_first_seen_time_and_drops_a_filed_item(hub, tmp_path):
    notes = tmp_path / "memory"
    notes.mkdir()
    note = notes / "2026-11-25.md"
    note.write_text("- [ ] for firstmate: share the coach context\n")
    first = check(hub, notes, "--now", str(NOW))
    later = check(hub, notes, "--now", str(NOW + 30 * 3600))
    assert [f["since"] for f in later] == [first[0]["since"]] == [NOW]
    receipt(hub.file(held()))
    note.write_text(
        "- [ ] for firstmate: share the coach context"
        " (shaping: held-coach-learnclaw-context)\n"
    )
    assert check(hub, notes, "--now", str(NOW + 31 * 3600)) == []
    assert json.loads((hub.tmp / "seen.json").read_text()) == {}


def test_the_check_does_not_list_a_line_whose_item_was_answered_and_archived(
    hub, tmp_path
):
    receipt(hub.file(held()))
    hub.close("held-coach-learnclaw-context")
    hub.archive("held-coach-learnclaw-context")
    notes = tmp_path / "memory"
    notes.mkdir()
    (notes / "2026-11-25.md").write_text(
        "- [ ] for firstmate: share the coach context"
        " (shaping: held-coach-learnclaw-context)\n"
    )
    before = len(hub.calls())
    assert check(hub, notes, "--now", str(NOW + 48 * 3600)) == []
    assert hub.calls()[before:] == []


def test_the_check_asks_the_hub_once_per_mark_with_tasks_axi_off_path(hub, tmp_path):
    hub.need_tasks_axi_on_path()
    notes = tmp_path / "memory"
    notes.mkdir()
    (notes / "2026-11-25.md").write_text(
        "- [ ] for firstmate: one (shaping: held-nothing)\n"
        "- [ ] for devclaw: two (shaping: held-nothing)\n"
    )
    found = check(
        hub, notes, "--now", str(NOW), env={**hub.env, "PATH": "/usr/bin:/bin"}
    )
    assert [f["title"] for f in found] == ["for firstmate: one", "for devclaw: two"]
    assert hub.calls() == [["hold", "open", "held-nothing", "--distinguish-absent"]]


def test_the_check_reads_only_recent_notes(hub, tmp_path):
    notes = tmp_path / "memory"
    notes.mkdir()
    (notes / "2026-10-01.md").write_text("- [ ] for firstmate: an old one\n")
    (notes / "2026-11-24.md").write_text("- [ ] for firstmate: a recent one\n")
    found = check(hub, notes, "--now", str(NOW))
    assert [f["title"] for f in found] == ["for firstmate: a recent one"]


# ─── end to end: relay.sh hold through the kit-relay forced command ─────────


def render(hub: Hub, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALLER), "--print"],
        env={**os.environ, "FM_HOME": str(hub.home), **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def relay(hub):
    out = render(hub, TASKS_AXI=str(hub.tasks))
    assert out.returncode == 0, out.stderr
    pinned = "/usr/local/libexec/kit-held/kit_held.py"
    assert f"readonly HELD_BIN='{pinned}'" in out.stdout
    assert f"readonly TASKS_AXI_BIN='{hub.tasks}'" in out.stdout
    script = write_exec(hub.tmp / "kit-relay", out.stdout.replace(pinned, str(SCRIPT)))
    bindir = hub.tmp / "bin"
    bindir.mkdir()
    write_exec(bindir / "ssh", FAKE_SSH_FORCED)
    write_exec(bindir / "logger", FAKE_LOGGER)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(hub.tmp),
        "STUB_STATE": str(hub.state),
        "KIT_RELAY_SCRIPT": str(script),
    }

    def run(*args: str, body: str = TEXT + "\n", script_path: Path | None = None):
        f = hub.tmp / "held.txt"
        f.write_text(body)
        e = {**env, "KIT_RELAY_SCRIPT": str(script_path or script)}
        return subprocess.run(
            ["sh", str(CLIENT), "hold", str(f), *args],
            env=e,
            capture_output=True,
            text=True,
            check=False,
        )

    return run


def flags(**over: str) -> list[str]:
    out = []
    for k, v in {**ENVELOPE, **over}.items():
        if v is not None:
            out += [f"--{k}", v]
    return out


def test_hold_files_the_item_through_the_relay(relay, hub):
    r = relay(*flags())
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["id"] == "held-coach-learnclaw-context" and got["state"] == "submitted"
    assert hub.items()["held-coach-learnclaw-context"]["state"] == "open"
    # The same text the same day is a replay: no second count.
    again = json.loads(relay(*flags()).stdout)
    assert again["state"] == "working" and again.get("replay") is True
    assert "(seen" not in hub.items()["held-coach-learnclaw-context"]["title"]
    # Other words, same fingerprint: one more count on the same item.
    more = json.loads(relay(*flags(), body="Still want the coach context.\n").stdout)
    assert more["count"] == 2 and more["id"] == got["id"]


def test_hold_with_a_named_home_reaches_it(relay, hub):
    r = relay("fm-vps", *flags())
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["url"] == "calls:fm-vps/held-coach-learnclaw-context"


def test_the_client_refuses_a_reminder_to_kit_before_any_send(relay, hub):
    r = relay(*flags(owner="kit"))
    assert r.returncode == 65
    assert "reminder to Kit itself" in r.stderr
    assert hub.items() == {}


@pytest.mark.parametrize("field", ["fingerprint", "thread", "owner"])
def test_the_client_needs_the_whole_envelope(relay, hub, field):
    r = relay(*flags(**{field: None}))
    assert r.returncode == 64
    assert f"envelope missing: {field}" in r.stderr
    assert hub.items() == {}


def test_hold_is_refused_when_the_install_pinned_no_tasks_axi(relay, hub):
    out = render(hub)
    assert out.returncode == 0, out.stderr
    plain = write_exec(hub.tmp / "kit-relay-plain", out.stdout)
    r = relay(*flags(), script_path=plain)
    assert r.returncode == 78
    assert "hold is not installed" in r.stderr
    assert hub.items() == {}


def test_hold_is_kits_verb_only(hub):
    out = render(hub, TASKS_AXI=str(hub.tasks))
    script = write_exec(hub.tmp / "kit-relay", out.stdout)
    r = subprocess.run(
        [str(script), "dashboard"],
        input=held(),
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(hub.tmp),
            "SSH_ORIGINAL_COMMAND": "hold kit-held-abc-20261125",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 64
    assert "hold is Kit's verb" in r.stderr


def test_the_installer_refuses_a_relative_tasks_axi(hub):
    out = render(hub, TASKS_AXI="tasks-axi")
    assert out.returncode != 0
    assert "TASKS_AXI must be an absolute path" in out.stderr


def test_a_hub_that_answers_every_id_as_existing_fails_instead_of_looping(hub):
    write_exec(hub.home / "bin/fm-captain-hold.sh", "#!/bin/sh\nexit 1\n")
    r = hub.file(held())
    assert r.returncode == 1
    assert "the hub looks wrong" in r.stderr
