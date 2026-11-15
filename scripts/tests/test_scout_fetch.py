"""The scout fetch glue (scripts/scout): lens pages -> feed2exec -> one note.

What this file pins: a first run over a lens sends exactly one note carrying the
count and the candidates path, and a second run sends none; a lens page that
cannot be fed to feed2exec safely is skipped and reported once (not every day);
`scout: off` and non-lens pages are ignored; `--catchup` records items as seen
and sends nothing; a failed note is retried instead of lost; the installer puts
down the units without enabling the timer. feed2exec and fm-inbox.sh are stubs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/scout"
FETCH = DIR / "scout-fetch.sh"
INSTALLER = DIR / "install-scout.sh"
SERVICE = DIR / "lifekit-scout.service"
TIMER = DIR / "lifekit-scout.timer"

# Prints one line per `args =` in the config, placeholders filled, the way the
# echo plugin would; a database file marks "seen". FAILME exits 1, TIMEOUTME
# logs the -v line feed2exec writes for a dead feed.
FAKE_FEED2EXEC = r"""#!/usr/bin/env bash
echo "$*" >> "$STATE_DIR/f2e-calls"
cfg="" db="" catchup=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) cfg="$2"; shift ;;
    --database) db="$2"; shift ;;
    --catchup) catchup=1 ;;
  esac
  shift
done
grep -q FAILME "$cfg" && { echo "KeyError: 'published'" >&2; exit 1; }
grep -q TIMEOUTME "$cfg" && echo "timeout while fetching feed gh:dead https://x.test/dead.atom" >&2
seen=0; [[ -e "$db" ]] && seen=1
: > "$db"
((seen || catchup)) && exit 0
grep -E '^args' "$cfg" | sed -E "s/^args *= *//; s|\{item.link\}|https://x.test/item|; s|\{item.title\}|'Title one'|"
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
        for name, body in (("feed2exec", FAKE_FEED2EXEC), ("fm-inbox.sh", FAKE_FM_INBOX)):
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


def test_first_run_sends_one_note_with_count_and_path_second_run_none(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    r = e.run()
    assert r.returncode == 0, r.stderr
    cand = e.state / "candidates/2026-11-16.txt"
    lines = cand.read_text().splitlines()
    assert lines == [
        "harness\tgh:a https://x.test/item 'Title one'",
        "harness\tweb:b https://x.test/item 'Title one'",
    ]
    notes = e.notes()
    assert len(notes) == 1
    assert notes[0].startswith("/fm/home|note --request-id scout-2026-11-16 -- ")
    assert "2 new items from 1 lens(es)" in notes[0] and str(cand) in notes[0]

    r = e.run()
    assert r.returncode == 0, r.stderr
    assert len(e.notes()) == 1  # quiet: nothing new, nothing sent
    assert cand.read_text().splitlines() == lines


def test_a_quiet_first_day_writes_nothing(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    e.run()
    e.run(date="2026-11-17")
    assert not (e.state / "candidates/2026-11-17.txt").exists()
    assert len(e.notes()) == 1


def test_broken_lens_is_skipped_and_reported_once(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    e.lens("bad", LENS.replace("{item.link}", "{item.published}", 1))
    e.lens("nosources", "---\nscout: on\n---\n\n# no sources here\n")
    e.lens("exec", LENS.replace("feed2exec.plugins.echo", "feed2exec.plugins.exec"))
    e.lens("crash", LENS.replace("url = https://x.test/b.atom", "url = FAILME"))
    r = e.run()
    assert r.returncode == 0, r.stderr
    notes = e.notes()
    broken = [n for n in notes if "scout-broken-" in n]
    assert len(broken) == 4, notes
    assert any("lens 'bad' skipped" in n and "{item.published}" in n for n in broken)
    assert any("lens 'nosources'" in n for n in broken)
    assert any("lens 'exec'" in n for n in broken)
    assert any("lens 'crash'" in n and "feed2exec failed" in n for n in broken)
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


def test_off_and_non_lens_pages_are_ignored(tmp_path):
    e = Env(tmp_path)
    e.lens("off", LENS.replace("scout: on", "scout: off"))
    e.lens("inventory", "---\nname: x\nstatus: active\n---\n\n# inventory\n")
    e.lens("plain", "# no frontmatter at all\n")
    r = e.run()
    assert r.returncode == 0, r.stderr
    assert e.notes() == []
    assert not (e.stub_state / "f2e-calls").exists()


def test_unterminated_frontmatter_is_reported(tmp_path):
    e = Env(tmp_path)
    e.lens("half", "---\nscout: on\n\n# never closed\n")
    e.run()
    assert any("lens 'half' skipped, frontmatter is not closed" in n for n in e.notes())


def test_catchup_records_items_and_sends_nothing(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    e.lens("bad", LENS.replace("{item.link}", "{item.published}", 1))
    r = e.run("--catchup")
    assert r.returncode == 0, r.stderr
    assert e.notes() == []
    assert not list((e.state / "candidates").glob("*"))
    assert "--catchup" in (e.stub_state / "f2e-calls").read_text()
    # what was recorded is not new on the next normal run
    e.run()
    assert e.notes() == [n for n in e.notes() if "scout-broken-" in n]
    assert not list((e.state / "candidates").glob("*.txt"))


def test_a_failed_note_is_retried_not_lost(tmp_path):
    e = Env(tmp_path)
    e.lens("harness")
    r = e.run(NOTE_FAIL="1")
    assert r.returncode == 1
    assert e.notes() == []
    assert (e.state / "candidates/2026-11-16.txt").exists()
    r = e.run(date="2026-11-17")  # feeds are quiet now; the old file is announced
    assert r.returncode == 0, r.stderr
    notes = e.notes()
    assert len(notes) == 1 and "--request-id scout-2026-11-16" in notes[0]
    e.run(date="2026-11-18")
    assert len(e.notes()) == 1


def test_dead_feeds_are_counted_per_day_and_reset(tmp_path):
    e = Env(tmp_path)
    e.lens("harness", LENS + "\n")
    dead = LENS.replace("[web:b]", "[gh:dead]\nurl = TIMEOUTME\nargs = gh:dead {item.link}\n\n[web:b]")
    e.lens("harness", dead)
    e.run()
    e.run(date="2026-11-17")
    f = e.state / "health/harness.gh_dead"
    assert f.read_text().strip() == "2"
    e.lens("harness")
    e.run(date="2026-11-18")
    assert not f.exists()


def test_needs_the_inbox_binary_and_feed2exec(tmp_path):
    e = Env(tmp_path)
    r = e.run(FM_INBOX_BIN="")
    assert r.returncode == 2 and "FM_INBOX_BIN" in r.stderr
    r = e.run(FEED2EXEC="/nonexistent/feed2exec")
    assert r.returncode == 2 and "not found" in r.stderr


def test_unknown_argument_is_a_usage_error(tmp_path):
    assert Env(tmp_path).run("--bogus").returncode == 2


def test_timer_is_daily_and_the_service_runs_the_script():
    t = TIMER.read_text()
    assert "OnCalendar=*-*-* 05:30:00 Europe/London" in t and "Persistent=true" in t
    s = SERVICE.read_text()
    assert "Type=oneshot" in s
    assert "scripts/scout/scout-fetch.sh" in s
    assert "EnvironmentFile=-/etc/lifekit/scout.env" in s


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
    env = (root / "etc/lifekit/scout.env").read_text()
    assert "FM_INBOX_BIN=/opt/fm/fm-inbox.sh" in env and "FM_INBOX_HOME=/home/x/fm" in env


def test_installer_needs_root_outside_a_test_root():
    r = subprocess.run(
        ["bash", str(INSTALLER)],
        env={k: v for k, v in os.environ.items() if k != "INSTALL_ROOT"},
        capture_output=True,
        text=True,
    )
    if os.geteuid() != 0:
        assert r.returncode == 1 and "root" in r.stderr
