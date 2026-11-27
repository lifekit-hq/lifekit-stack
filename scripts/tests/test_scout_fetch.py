"""The scout fetch glue (scripts/scout): lens pages -> feed2exec -> one note.

What this file pins: a feed seen for the first time (a new lens, or a source
added to one) is caught up silently, so its back catalogue never reaches the
judge; after that a run with new items sends exactly one note carrying the
count and the candidates path, and a second run sends none; each feed's items
are written newest first even when the feed lists them oldest first, and a big
feed gets only its fair share of the per-lens cap beside small ones; reoffered
lines are offered once; a lens page that
cannot be fed to feed2exec safely is skipped and reported once (not every day);
`scout: off` and non-lens pages are ignored; `--catchup` records items as seen
and sends nothing; a failed note is retried instead of lost; the installer puts
down the units without enabling the timer. feed2exec and fm-inbox.sh are stubs;
one test runs the real feed2exec (with scout_echo) over a local feed file.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/scout"
FETCH = DIR / "scout-fetch.sh"
INSTALLER = DIR / "install-scout.sh"
TIMER = DIR / "lifekit-scout.timer"

# Plays feed2exec over the lens config: each section is a feed of `items` items
# (default 1) plus `grows` (default 1) more per F2E_GROW step; item i is dated
# 1000*i and listed newest first, or oldest first with `order = asc`. The seen
# database is a real feedcache table, as feed2exec keeps it. With the echo plugin
# an item prints as the echo line; with scout_echo as feed TAB date TAB line.
# FAILME exits 1; PARTIAL exits 1 and KILLME kills the caller after a fetch.
FAKE_FEED2EXEC = r"""#!/usr/bin/env python3
import configparser, os, signal, sqlite3, sys
args = sys.argv[1:]
cfg = args[args.index("--config") + 1]
db = args[args.index("--database") + 1]
catchup = "--catchup" in args
p = configparser.RawConfigParser()
p.read(cfg)
with open(os.environ["STATE_DIR"] + "/f2e-calls", "a") as f:
    f.write(" ".join(args) + " sections=" + ",".join(p.sections())
            + " output=" + p.defaults().get("output", "") + "\n")
text = open(cfg).read()
if "FAILME" in text:
    print("KeyError: 'published'", file=sys.stderr)
    sys.exit(1)
con = sqlite3.connect(db)
con.execute("CREATE TABLE IF NOT EXISTS feedcache (name text, guid text, PRIMARY KEY (name, guid))")
grow = int(os.environ.get("F2E_GROW", "0"))
for name in p.sections():
    n = p.getint(name, "items", fallback=1) + grow * p.getint(name, "grows", fallback=1)
    ids = list(range(1, n + 1))
    if p.get(name, "order", fallback="desc") != "asc":
        ids.reverse()
    for i in ids:
        guid = f"{name}-{i}"
        if con.execute("SELECT 1 FROM feedcache WHERE name=? AND guid=?", (name, guid)).fetchone():
            continue
        if not catchup:
            line = p.get(name, "args").replace("{item.link}", f"https://x.test/{i}")
            line = line.replace("{item.title}", f"'Title {i}'")
            if p.get(name, "output") == "scout_echo":
                line = f"{name}\t{1000 * i}\t{line}"
            print(line, flush=True)
        con.execute("INSERT INTO feedcache VALUES (?, ?)", (name, guid))
        con.commit()
if not catchup and "PARTIAL" in text:
    print("KeyError: 'published'", file=sys.stderr)
    sys.exit(1)
if not catchup and "KILLME" in text:
    os.kill(os.getppid(), signal.SIGKILL)
"""

# Records each call; fails when NOTE_FAIL is set.
FAKE_FM_INBOX = """#!/usr/bin/env bash
[[ -z "${NOTE_FAIL:-}" ]] || exit 1
echo "$FM_HOME|$*" >> "$STATE_DIR/notes"
"""

LENS = """---
name: scout-lens-test
status: active
scout: on
---

# Scout lens: test

## Question
Anything useful?

## Sources
```ini
[DEFAULT]
output = feed2exec.plugins.echo

[gh:a]
url = https://x.test/a.atom
args = gh:a {item.link} {item.title}

[web:b]
url = https://x.test/b.atom
args = web:b {item.link} {item.title}
```
"""


class Env:
    def __init__(self, tmp: Path):
        self.lenses = tmp / "lenses"
        self.state = tmp / "state"
        self.stub = tmp / "stub"
        for d in (self.lenses, self.stub, self.state):
            d.mkdir()
        self.stub_state = tmp / "stubstate"
        self.stub_state.mkdir()
        for name, body in (
            ("feed2exec", FAKE_FEED2EXEC),
            ("fm-inbox.sh", FAKE_FM_INBOX),
        ):
            p = self.stub / name
            p.write_text(body)
            p.chmod(0o755)

    def lens(self, name: str, text: str = LENS) -> None:
        (self.lenses / f"{name}.md").write_text(text)

    def run(self, *args: str, date: str = "2026-11-16", **extra: str):
        env = {
            **os.environ,
            "SCOUT_LENS_DIR": str(self.lenses),
            "SCOUT_STATE_DIR": str(self.state),
            "FEED2EXEC": str(self.stub / "feed2exec"),
            "FM_INBOX_BIN": str(self.stub / "fm-inbox.sh"),
            "FM_INBOX_HOME": "/fm/home",
            "STATE_DIR": str(self.stub_state),
            "SCOUT_DATE": date,
            **extra,
        }
        return subprocess.run(
            ["bash", str(FETCH), *args], env=env, capture_output=True, text=True
        )

    def notes(self) -> list[str]:
        f = self.stub_state / "notes"
        return f.read_text().splitlines() if f.exists() else []

    def candidates(self, date: str = "2026-11-16") -> list[str]:
        f = self.state / f"candidates/{date}.txt"
        return f.read_text().splitlines() if f.exists() else []

    def calls(self) -> list[str]:
        f = self.stub_state / "f2e-calls"
        return f.read_text().splitlines() if f.exists() else []


def tags(lines: list[str]) -> list[str]:
    """The source tag of each candidate line."""
    return [ln.split("\t", 1)[1].split(" ", 1)[0] for ln in lines]


def test_a_new_lens_is_caught_up_silently_then_new_items_sent_once(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    r = e.run()  # first sighting: the back catalogue is recorded, not offered
    assert r.returncode == 0, r.stderr
    assert e.notes() == [] and e.candidates() == []
    assert "--catchup sections=gh:a,web:b" in e.calls()[0]
    r = e.run(F2E_GROW="1")
    assert r.returncode == 0, r.stderr
    cand = e.state / "candidates/2026-11-16.txt"
    lines = e.candidates()
    assert lines == [
        "harness\tgh:a https://x.test/2 'Title 2'",
        "harness\tweb:b https://x.test/2 'Title 2'",
    ]
    # the normal fetch runs through scout_echo, not the bare echo plugin
    assert "output=scout_echo" in e.calls()[-1]
    notes = e.notes()
    assert len(notes) == 1
    assert notes[0].startswith("/fm/home|note --request-id scout-2026-11-16-2 -- ")
    assert "2 new items from 1 lens(es)" in notes[0] and str(cand) in notes[0]

    r = e.run(F2E_GROW="1")
    assert r.returncode == 0, r.stderr
    assert len(e.notes()) == 1  # quiet: nothing new, nothing sent
    assert e.candidates() == lines


def test_a_source_added_to_a_lens_is_caught_up_and_the_others_still_offered(
    tmp_path,
):
    e = Env(tmp_path)
    e.lens("harness")
    e.run()
    backlog = "[web:c]\nurl = https://x.test/c.atom\nargs = web:c {item.link} {item.title}\nitems = 40\n```\n"
    e.lens("harness", LENS.replace("```\n", backlog))
    r = e.run(F2E_GROW="1")
    assert r.returncode == 0, r.stderr
    # only web:c's 41 items were caught up; then every feed was fetched as
    # usual, and the seeded web:c had nothing new
    assert "--catchup sections=web:c " in e.calls()[-2]
    assert "fetch sections=gh:a,web:b,web:c " in e.calls()[-1]
    assert e.candidates() == [
        "harness\tgh:a https://x.test/2 'Title 2'",
        "harness\tweb:b https://x.test/2 'Title 2'",
    ]
    r = e.run(F2E_GROW="2", date="2026-11-17")
    assert r.returncode == 0, r.stderr
    assert tags(e.candidates("2026-11-17")) == ["gh:a", "web:b", "web:c"]
    assert "web:c https://x.test/42 'Title 42'" in e.candidates("2026-11-17")[2]


def test_a_feed_listing_oldest_first_is_written_newest_first(tmp_path):
    e = Env(tmp_path)
    e.lens("harness", LENS.replace("args = gh:a", "order = asc\nargs = gh:a"))
    e.run()
    e.run(F2E_GROW="3")
    assert e.candidates() == [
        "harness\tgh:a https://x.test/4 'Title 4'",
        "harness\tgh:a https://x.test/3 'Title 3'",
        "harness\tgh:a https://x.test/2 'Title 2'",
        "harness\tweb:b https://x.test/4 'Title 4'",
        "harness\tweb:b https://x.test/3 'Title 3'",
        "harness\tweb:b https://x.test/2 'Title 2'",
    ]
    assert "gh:a" in e.calls()[-1]  # the stub really listed gh:a oldest first


def test_a_large_backlog_feed_cannot_crowd_out_small_feeds(tmp_path):
    e = Env(tmp_path)
    big = LENS.replace(
        "args = gh:a {item.link} {item.title}",
        "args = gh:a {item.link} {item.title}\ngrows = 100",
    ).replace(
        "args = web:b {item.link} {item.title}",
        "args = web:b {item.link} {item.title}\ngrows = 5\n\n"
        "[web:claude]\nurl = https://x.test/c.atom\n"
        "args = web:claude {item.link} {item.title}\ngrows = 3",
    )
    e.lens("harness", big)
    e.run()
    r = e.run(F2E_GROW="1")
    assert r.returncode == 0, r.stderr
    lines = e.candidates()
    # 60 in all: both small feeds whole, the big one gets the rest, newest first
    assert len(lines) == 60
    assert tags(lines) == ["gh:a"] * 52 + ["web:b"] * 5 + ["web:claude"] * 3
    assert lines[0] == "harness\tgh:a https://x.test/101 'Title 101'"
    assert lines[51] == "harness\tgh:a https://x.test/50 'Title 50'"
    assert lines[52] == "harness\tweb:b https://x.test/6 'Title 6'"
    # what did not fit is kept beside the file, not dropped and not announced
    over = (e.state / "candidates/2026-11-16.overflow").read_text().splitlines()
    assert len(over) == 48 and over[0] == "harness\tgh:a https://x.test/49 'Title 49'"
    assert "60 new items" in e.notes()[0]
    # a later same-day run only has what is left of the lens's cap
    e.run(F2E_GROW="2")
    assert len(e.candidates()) == 60
    assert (
        len((e.state / "candidates/2026-11-16.overflow").read_text().splitlines())
        == 48 + 108
    )


def test_the_lens_cap_is_configurable(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    e.run()
    e.run(F2E_GROW="3", SCOUT_LENS_CAP="3")
    assert tags(e.candidates()) == ["gh:a", "gh:a", "web:b"]
    assert e.run(SCOUT_LENS_CAP="x").returncode == 2


def test_reoffered_lines_are_offered_once_after_fresh_items(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    e.run()
    queue = e.state / "reoffer/harness.txt"
    queue.write_text(
        "harness\tweb:b https://old.test/3 'Old 3'\n"
        "harness\tgh:k https://old.test/2 'Old 2'\n"
        "harness\tgh:k https://old.test/1 'Old 1'\n"
    )
    r = e.run(F2E_GROW="1", SCOUT_LENS_CAP="4")
    assert r.returncode == 0, r.stderr
    assert e.candidates() == [
        "harness\tgh:a https://x.test/2 'Title 2'",
        "harness\tweb:b https://x.test/2 'Title 2'",
        "harness\tweb:b https://old.test/3 'Old 3'",
        "harness\tgh:k https://old.test/2 'Old 2'",
    ]
    assert queue.read_text() == "harness\tgh:k https://old.test/1 'Old 1'\n"
    r = e.run(F2E_GROW="1", date="2026-11-17")  # feeds quiet: the queue alone
    assert e.candidates("2026-11-17") == ["harness\tgh:k https://old.test/1 'Old 1'"]
    assert not queue.exists()
    e.run(F2E_GROW="1", date="2026-11-18")
    assert e.candidates("2026-11-18") == []


def test_a_quiet_first_day_writes_nothing(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    e.run()
    e.run(F2E_GROW="1")
    e.run(F2E_GROW="1", date="2026-11-17")
    assert not (e.state / "candidates/2026-11-17.txt").exists()
    assert len(e.notes()) == 1


def test_broken_lens_is_skipped_and_reported_once(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    e.run()
    e.lens("bad", LENS.replace("{item.link}", "{item.published}", 1))
    e.lens("nosources", "---\nscout: on\n---\n\n# no sources here\n")
    e.lens("exec", LENS.replace("feed2exec.plugins.echo", "feed2exec.plugins.exec"))
    e.lens("crash", LENS.replace("url = https://x.test/b.atom", "url = FAILME"))
    r = e.run(F2E_GROW="1")
    assert r.returncode == 0, r.stderr
    notes = e.notes()
    broken = [n for n in notes if "scout-broken-" in n]
    assert len(broken) == 4, notes
    assert any("lens 'bad' skipped" in n and "{item.published}" in n for n in broken)
    assert any("lens 'nosources'" in n for n in broken)
    assert any("lens 'exec'" in n for n in broken)
    assert any("lens 'crash'" in n and "catch-up failed" in n for n in broken)
    # the healthy lens still ran, and is the only one in the count note
    count = [n for n in notes if "new items" in n]
    assert len(count) == 1 and "2 new items from 1 lens(es)" in count[0]

    e.run(date="2026-11-17")
    assert len([n for n in e.notes() if "scout-broken-" in n]) == 4  # not again

    # fixed, then broken again: that is a new transition and is reported
    e.lens("bad")
    e.run(date="2026-11-18")
    e.lens("bad", LENS.replace("{item.link}", "{item.published}", 1))
    e.run(date="2026-11-19")
    assert len([n for n in e.notes() if "lens 'bad'" in n]) == 2


def test_items_are_kept_even_when_the_args_carry_no_source_tag(tmp_path):
    e = Env(tmp_path)
    e.lens(
        "plain",
        LENS.replace("args = gh:a ", "args = gh ").replace(
            "args = web:b ", "args = web "
        ),
    )
    e.run()
    r = e.run(F2E_GROW="1")
    assert r.returncode == 0, r.stderr
    assert e.candidates() == [
        "plain\tgh https://x.test/2 'Title 2'",
        "plain\tweb https://x.test/2 'Title 2'",
    ]
    assert "2 new items" in e.notes()[0]


def test_items_printed_before_a_feed2exec_failure_are_kept(tmp_path):
    e = Env(tmp_path)
    e.lens("partial", LENS.replace("url = https://x.test/b.atom", "url = PARTIAL"))
    e.run()
    r = e.run(F2E_GROW="1")
    assert r.returncode == 0, r.stderr
    lines = e.candidates()
    assert len(lines) == 2 and all(line.startswith("partial\t") for line in lines)
    notes = e.notes()
    assert any("lens 'partial' skipped, feed2exec failed" in n for n in notes)
    assert any("2 new items from 1 lens(es)" in n for n in notes)


def test_items_of_earlier_lenses_survive_a_killed_run(tmp_path):
    e = Env(tmp_path)
    e.lens("a-first")
    e.lens("z-last", LENS.replace("url = https://x.test/b.atom", "url = KILLME"))
    e.run()  # the catch-up of both lenses
    r = e.run(F2E_GROW="1")
    assert r.returncode != 0  # the run was killed mid-way: no note went out
    assert e.notes() == []
    assert [line.split("\t")[0] for line in e.candidates()] == ["a-first", "a-first"]
    # the next run announces what the killed one had already recorded
    e.lens("z-last")
    r = e.run(F2E_GROW="1")
    assert r.returncode == 0, r.stderr
    notes = e.notes()
    assert len(notes) == 1 and "2 new items from 1 lens(es)" in notes[0]


def test_off_and_non_lens_pages_are_ignored(tmp_path):
    e = Env(tmp_path)
    e.lens("off", LENS.replace("scout: on", "scout: off"))
    e.lens("inventory", "---\nname: x\nstatus: active\n---\n\n# inventory\n")
    e.lens("plain", "# no frontmatter at all\n")
    r = e.run()
    assert r.returncode == 0, r.stderr
    assert e.notes() == []
    assert e.calls() == []


def test_unterminated_frontmatter_is_reported(tmp_path):
    e = Env(tmp_path)
    e.lens("half", "---\nscout: on\n\n# never closed\n")
    e.run()
    assert any("lens 'half' skipped, frontmatter is not closed" in n for n in e.notes())


def test_catchup_records_items_and_sends_nothing(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    e.lens("bad", LENS.replace("{item.link}", "{item.published}", 1))
    r = e.run("--catchup", F2E_GROW="1")
    assert r.returncode == 0, r.stderr
    assert e.notes() == []
    assert not list((e.state / "candidates").glob("*"))
    assert "--catchup" in e.calls()[0]
    # what was recorded is not new on the next normal run
    e.run(F2E_GROW="1")
    assert e.notes() == [n for n in e.notes() if "scout-broken-" in n]
    assert not list((e.state / "candidates").glob("*.txt"))


def test_a_failed_note_is_retried_not_lost(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    e.run()
    r = e.run(F2E_GROW="1", NOTE_FAIL="1")
    assert r.returncode == 1
    assert e.notes() == []
    assert (e.state / "candidates/2026-11-16.txt").exists()
    r = e.run(F2E_GROW="1", date="2026-11-17")  # feeds quiet; old file announced
    assert r.returncode == 0, r.stderr
    notes = e.notes()
    assert len(notes) == 1 and "--request-id scout-2026-11-16-2 " in notes[0]
    e.run(F2E_GROW="1", date="2026-11-18")
    assert len(e.notes()) == 1


def test_scout_echo_prints_feed_date_and_the_echo_line(capsys):
    import calendar
    import sys
    import time

    sys.path.insert(0, str(DIR))
    try:
        import scout_echo
    finally:
        sys.path.remove(str(DIR))
    feed = {"name": "web:b"}
    when = time.strptime("2026-11-26 10:00:00", "%Y-%m-%d %H:%M:%S")
    assert scout_echo.output(
        "web:b",
        "https://x.test/1",
        "A\ttab\nand newline",
        feed=feed,
        item={"updated_parsed": when},
    )
    scout_echo.output("web:b", "u", feed=feed, item={"updated_parsed": 1700000000.5})
    scout_echo.output("web:b", "u", feed=feed, item={})
    scout_echo.output("web:b", "u", feed={**feed, "catchup": True}, item={})
    assert capsys.readouterr().out.splitlines() == [
        f"web:b\t{calendar.timegm(when)}\tweb:b https://x.test/1 'A tab and newline'",
        "web:b\t1700000000\tweb:b u",
        "web:b\t0\tweb:b u",
    ]


ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title><id>urn:t</id>
<updated>2026-11-26T00:00:00Z</updated>{entries}</feed>
"""


def atom(days: list[int]) -> str:
    entry = (
        "<entry><title>Post {d}</title><id>urn:p{d}</id>"
        '<link href="https://x.test/p{d}"/>'
        "<updated>2026-11-{d:02d}T00:00:00Z</updated></entry>"
    )
    return ATOM.format(entries="".join(entry.format(d=d) for d in days))


@pytest.mark.skipif(shutil.which("feed2exec") is None, reason="needs feed2exec")
def test_with_the_real_feed2exec_a_new_source_is_seeded_and_items_come_newest_first(
    tmp_path,
):
    e = Env(tmp_path)
    feed = tmp_path / "feed.atom"
    feed.write_text(atom([1, 2, 3]))  # an old-items-first feed
    e.lens(
        "harness",
        LENS.replace("https://x.test/a.atom", feed.as_uri()).replace(
            "[web:b]\nurl = https://x.test/b.atom\nargs = web:b {item.link} {item.title}\n",
            "",
        ),
    )
    real = {"FEED2EXEC": shutil.which("feed2exec"), "HOME": str(tmp_path)}
    r = e.run(**real)
    assert r.returncode == 0, r.stderr
    assert e.candidates() == [] and e.notes() == []
    feed.write_text(atom([1, 2, 3, 4, 5]))
    r = e.run(**real)
    assert r.returncode == 0, r.stderr
    assert e.candidates() == [
        "harness\tgh:a https://x.test/p5 'Post 5'",
        "harness\tgh:a https://x.test/p4 'Post 4'",
    ]


def test_needs_the_inbox_binary_and_feed2exec(tmp_path):
    e = Env(tmp_path)
    r = e.run(FM_INBOX_BIN="")
    assert r.returncode == 2 and "FM_INBOX_BIN" in r.stderr
    r = e.run(FEED2EXEC="/nonexistent/feed2exec")
    assert r.returncode == 2 and "not found" in r.stderr


def test_unknown_argument_is_a_usage_error(tmp_path):
    assert Env(tmp_path).run("--bogus").returncode == 2


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="needs systemd-analyze"
)
def test_timer_fires_daily_at_0530_london_time():
    timer = configparser.ConfigParser(strict=False, interpolation=None)
    timer.optionxform = str
    timer.read(TIMER)
    spec = timer["Timer"]["OnCalendar"]
    assert timer["Timer"]["Persistent"] == "true"

    def elapses(base: str) -> list[str]:
        out = subprocess.run(
            [
                "systemd-analyze",
                "calendar",
                "--iterations=3",
                f"--base-time={base}",
                spec,
            ],
            capture_output=True,
            text=True,
            check=True,
            # TZ=UTC pins the output shape: with a non-UTC host zone systemd adds an
            # "(in UTC)" line, with a UTC host it does not.
            env={**os.environ, "TZ": "UTC"},
        ).stdout
        return re.findall(r"(?:Next elapse|Iteration #\d+): \w+ (\S+ \S+) UTC", out)

    # 05:30 in London is 05:30 UTC in winter and 04:30 UTC under BST, every day
    assert elapses("2026-11-16 00:00:00 UTC") == [
        "2026-11-16 05:30:00",
        "2026-11-17 05:30:00",
        "2026-11-18 05:30:00",
    ]
    assert elapses("2026-07-01 00:00:00 UTC") == [
        "2026-07-01 04:30:00",
        "2026-07-02 04:30:00",
        "2026-07-03 04:30:00",
    ]


def test_installer_installs_feed2exec_and_units_but_does_not_enable(tmp_path):
    root = tmp_path / "root"
    calls = tmp_path / "calls"
    for name in ("apt-get", "systemctl"):
        p = tmp_path / name
        p.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{calls}"\n')
        p.chmod(0o755)
    r = subprocess.run(
        ["bash", str(INSTALLER)],
        env={
            **os.environ,
            "INSTALL_ROOT": str(root),
            "APT_GET": str(tmp_path / "apt-get"),
            "SYSTEMCTL": str(tmp_path / "systemctl"),
            "FM_INBOX_BIN": "/opt/fm/fm-inbox.sh",
            "FM_INBOX_HOME": "/home/x/fm",
            "SCOUT_USER": "scoutuser",
        },
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    log = calls.read_text()
    assert "apt-get install -y feed2exec" in log
    assert "systemctl daemon-reload" in log
    assert "enable" not in log and "start" not in log
    unit = (root / "etc/systemd/system/lifekit-scout.service").read_text()
    assert "User=scoutuser" in unit and "__" not in unit
    assert (root / "etc/systemd/system/lifekit-scout.timer").is_file()
    if shutil.which("systemd-analyze"):
        v = subprocess.run(
            [
                "systemd-analyze",
                "verify",
                str(root / "etc/systemd/system/lifekit-scout.service"),
                str(root / "etc/systemd/system/lifekit-scout.timer"),
            ],
            capture_output=True,
            text=True,
        )
        # verify also lints the host's own units; only our units are ours to fix
        ours = [
            ln for ln in (v.stdout + v.stderr).splitlines() if "lifekit-scout" in ln
        ]
        assert v.returncode == 0 and ours == [], v.stdout + v.stderr
    env = (root / "etc/lifekit/scout.env").read_text()
    assert (
        "FM_INBOX_BIN=/opt/fm/fm-inbox.sh" in env and "FM_INBOX_HOME=/home/x/fm" in env
    )


def test_installer_needs_root_outside_a_test_root():
    r = subprocess.run(
        ["bash", str(INSTALLER)],
        env={k: v for k, v in os.environ.items() if k != "INSTALL_ROOT"},
        capture_output=True,
        text=True,
    )
    if os.geteuid() != 0:
        assert r.returncode == 1 and "root" in r.stderr
