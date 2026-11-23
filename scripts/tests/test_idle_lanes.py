"""The idle-lane check (scripts/idle-lanes) against fixture fleet homes.

Each home is a directory with fake bin/fm-capacity.sh, bin/fm-tasks-axi.sh and
bin/fm-inbox.sh that print what the test put in capacity.txt / ready.txt and log
inbox notes. A fake docker records the owner envelopes. Fixtures are synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/idle-lanes"
CHECK = DIR / "idle_lanes.py"
ENSURE = REPO / "scripts/ensure-idle-lanes.sh"

T0 = 1_800_000_000
MIN = 60

FAKE_CAPACITY = '#!/usr/bin/env bash\ncat "$(dirname "$0")/../capacity.txt"\n'
FAKE_TASKS = (
    '#!/usr/bin/env bash\n[[ "$1" == ready ]] || exit 2\n'
    'cat "$(dirname "$0")/../ready.txt"\n'
)
FAKE_INBOX = (
    '#!/usr/bin/env bash\necho "$*" >> "$(dirname "$0")/../inbox.log"\n'
    '[[ ! -e "$(dirname "$0")/../inbox.fail" ]]\n'
)
FAKE_DOCKER = """#!/usr/bin/env bash
case "$1" in
  ps) [[ -n "${NO_CONTAINER:-}" ]] || echo abc123 ;;
  exec) cat >> "$STATE_DIR/envelopes.jsonl"; echo >> "$STATE_DIR/envelopes.jsonl"
        exit "${POST_RC:-0}" ;;
esac
"""


def capacity(free=2, reason="ok"):
    return (
        f"capacity: free_lanes={free} reason={reason} lanes=3 target=5 "
        "secondmates=0 load1=1.0 cores=10 mem_free_pct=50 disk_free_mb=90000\n"
        "Lanes: 3 running of 5 target.\n"
    )


def ready(n, held=0):
    out = f"count: {n}\n"
    if n:
        out += f"ready[{n}]{{id,state,kind,repo,title}}:\n"
        out += "".join(f'  t{i},queued,ship,r,"title"\n' for i in range(n))
    else:
        out += "ready[0]:\n"
    if held:
        out += f"held[{held}]{{id,state}}:\n" + "".join(
            f"  h{i},queued\n" for i in range(held)
        )
    return out


class Fleet:
    def __init__(self, tmp: Path, names=("main",)):
        self.tmp = tmp
        self.state = tmp / "state"
        self.state.mkdir()
        self.homes = {}
        for name in names:
            home = tmp / name
            (home / "bin").mkdir(parents=True)
            for script, body in (
                ("fm-capacity.sh", FAKE_CAPACITY),
                ("fm-tasks-axi.sh", FAKE_TASKS),
                ("fm-inbox.sh", FAKE_INBOX),
            ):
                (home / "bin" / script).write_text(body)
                (home / "bin" / script).chmod(0o755)
            self.set(name, capacity(), ready(0))
            self.homes[name] = home
        docker = tmp / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.docker = docker
        self.quota = tmp / "quota.json"

    def set(self, name, cap, rdy):
        home = self.tmp / name
        (home / "capacity.txt").write_text(cap)
        (home / "ready.txt").write_text(rdy)

    def run(self, at, cmd="check", **extra):
        env = {
            **os.environ,
            "IDLE_LANES_HOMES": " ".join(str(h) for h in self.homes.values()),
            "IDLE_LANES_STATE_DIR": str(self.state / "idle"),
            "QUOTA_SNAPSHOT": str(self.quota),
            "DOCKER": str(self.docker),
            "STATE_DIR": str(self.state),
            "IDLE_LANES_NOW": str(at),
            **extra,
        }
        return subprocess.run(
            [sys.executable, str(CHECK), cmd],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def st(self, name):
        return json.loads((self.state / "idle" / f"{name}.json").read_text())

    def notes(self, name):
        log = self.tmp / name / "inbox.log"
        return log.read_text().splitlines() if log.exists() else []

    def pushes(self):
        f = self.state / "envelopes.jsonl"
        return (
            [json.loads(ln) for ln in f.read_text().split("\n") if ln]
            if f.exists()
            else []
        )


@pytest.fixture
def fleet(tmp_path):
    return Fleet(tmp_path)


def idle(fleet):
    fleet.set("main", capacity(2), ready(3))


def test_idle_begins_when_lanes_and_work_coexist(fleet):
    idle(fleet)
    assert fleet.run(T0).returncode == 0
    st = fleet.st("main")
    assert (st["state"], st["since"], st["free_lanes"], st["ready"]) == (
        "idle",
        T0,
        2,
        3,
    )
    assert fleet.notes("main") == [] and fleet.pushes() == []


def test_idle_clears_the_moment_either_side_is_zero(fleet):
    idle(fleet)
    fleet.run(T0)
    fleet.set("main", capacity(0, "full"), ready(3))
    fleet.run(T0 + MIN)
    assert fleet.st("main")["state"] == "ok" and fleet.st("main")["since"] is None
    idle(fleet)
    fleet.run(T0 + 2 * MIN)
    assert fleet.st("main")["since"] == T0 + 2 * MIN  # a new episode, a new clock
    fleet.set("main", capacity(2), ready(0))
    fleet.run(T0 + 3 * MIN)
    assert fleet.st("main")["state"] == "ok"


def test_note_at_15_minutes_once(fleet):
    idle(fleet)
    fleet.run(T0)
    fleet.run(T0 + 14 * MIN)
    assert fleet.notes("main") == []
    fleet.run(T0 + 15 * MIN)
    fleet.run(T0 + 16 * MIN)
    fleet.run(T0 + 20 * MIN)
    notes = fleet.notes("main")
    assert len(notes) == 1 and notes[0].startswith("note --request-id idle-lanes-main-")
    assert "idle" in notes[0] and fleet.pushes() == []
    assert fleet.st("main")["state"] == "idle"


def test_push_at_30_minutes_once_per_episode_and_red(fleet):
    idle(fleet)
    fleet.run(T0)
    fleet.run(T0 + 29 * MIN)
    assert fleet.pushes() == []
    fleet.run(T0 + 30 * MIN)
    fleet.run(T0 + 31 * MIN)
    fleet.run(T0 + 45 * MIN)
    pushes = fleet.pushes()
    assert len(pushes) == 1
    assert pushes[0]["level"] == "act" and pushes[0]["subject"] == "main"
    assert "\n" not in pushes[0]["headline"] and "body" not in pushes[0]
    st = fleet.st("main")
    assert (
        st["state"] == "red" and st["since"] == T0 and st["pushed_at"] == T0 + 30 * MIN
    )
    assert len(fleet.notes("main")) == 1
    # a second episode is a new push
    fleet.set("main", capacity(0, "full"), ready(3))
    fleet.run(T0 + 50 * MIN)
    idle(fleet)
    fleet.run(T0 + 51 * MIN)
    fleet.run(T0 + 82 * MIN)
    assert len(fleet.pushes()) == 2
    assert len(fleet.notes("main")) == 2


def test_host_full_is_not_idle_with_work(fleet):
    for reason in ("cpu", "memory", "disk", "pressure,disk"):
        fleet.set("main", capacity(0, reason), ready(5))
        fleet.run(T0)
        assert fleet.st("main")["state"] == "ok", reason
    # even if a probe claimed free lanes, a host reason wins
    fleet.set("main", capacity(1, "memory"), ready(5))
    fleet.run(T0)
    st = fleet.st("main")
    assert st["state"] == "ok" and st["reason"] == "host-full"


def test_held_and_time_gated_items_are_not_counted(fleet):
    # tasks-axi leaves held/date-gated work out of ready[N]; a held group in the
    # same output is never counted.
    fleet.set("main", capacity(2), ready(0, held=4))
    fleet.run(T0)
    assert fleet.st("main")["state"] == "ok" and fleet.st("main")["ready"] == 0
    fleet.set("main", capacity(2), ready(2, held=4))
    fleet.run(T0 + MIN)
    assert fleet.st("main")["ready"] == 2
    fleet.set("main", capacity(2), "count: 0\n")
    fleet.run(T0 + 2 * MIN)
    assert fleet.st("main")["state"] == "ok"


def test_no_lane_target_is_not_idle(fleet):
    fleet.set("main", capacity("unknown", "no-target"), ready(3))
    fleet.run(T0)
    assert fleet.st("main")["state"] == "ok"


def quota(fleet, exhausted, resets, status="projected_exhaustion", at=T0):
    fleet.quota.write_text(
        json.dumps(
            {
                "generated_epoch": at,
                "windows": {"seven_day": {"resets_at": resets}},
                "projected_exhausted_at": exhausted,
                "runway_status": status,
                "limiting_window": "seven_day",
            }
        )
    )


def test_quota_pauses_only_when_the_weekly_cap_is_hit_before_reset(fleet):
    idle(fleet)
    quota(fleet, exhausted=T0 + 1000, resets=T0 + 5000)
    fleet.run(T0)
    st = fleet.st("main")
    assert st["state"] == "ok" and st["reason"] == "quota"
    # exhaustion after the reset, no snapshot, or a stale one: work counts as ready
    quota(fleet, exhausted=T0 + 9000, resets=T0 + 5000)
    fleet.run(T0 + MIN)
    assert fleet.st("main")["state"] == "idle"
    fleet.quota.unlink()
    fleet.run(T0 + 2 * MIN)
    assert fleet.st("main")["state"] == "idle"
    quota(fleet, exhausted=T0 + 1000, resets=T0 + 5000, at=T0 - 3 * 3600)
    fleet.run(T0 + 3 * MIN)
    assert fleet.st("main")["state"] == "idle"


def test_a_failed_probe_neither_starts_nor_clears_an_episode(fleet):
    idle(fleet)
    fleet.run(T0)
    (fleet.tmp / "main/bin/fm-capacity.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    res = fleet.run(T0 + MIN)
    assert res.returncode == 1 and "fm-capacity.sh" in res.stderr
    st = fleet.st("main")
    assert st["state"] == "idle" and st["since"] == T0 and st["error"]


def test_a_failed_note_or_push_is_retried_not_marked_sent(fleet):
    idle(fleet)
    fleet.run(T0)
    (fleet.tmp / "main/inbox.fail").write_text("")
    res = fleet.run(T0 + 15 * MIN)
    assert res.returncode == 1 and fleet.st("main")["note_sent_at"] is None
    (fleet.tmp / "main/inbox.fail").unlink()
    fleet.run(T0 + 16 * MIN)
    assert fleet.st("main")["note_sent_at"] == T0 + 16 * MIN
    res = fleet.run(T0 + 30 * MIN, NO_CONTAINER="1")
    assert res.returncode == 1 and fleet.st("main")["pushed_at"] is None
    fleet.run(T0 + 31 * MIN)
    assert len(fleet.pushes()) == 1 and fleet.st("main")["state"] == "red"


def test_each_home_has_its_own_clock(tmp_path):
    fleet = Fleet(tmp_path, names=("main", "mate"))
    fleet.set("main", capacity(2), ready(3))
    fleet.run(T0)
    fleet.set("mate", capacity(1), ready(1))
    fleet.run(T0 + 20 * MIN)
    assert (
        fleet.st("main")["since"] == T0 and fleet.st("mate")["since"] == T0 + 20 * MIN
    )
    assert len(fleet.notes("main")) == 1 and fleet.notes("mate") == []


def test_status_prints_each_home_ok_idle_or_red(tmp_path):
    fleet = Fleet(tmp_path, names=("main", "mate"))
    fleet.set("main", capacity(2), ready(3))
    fleet.run(T0)
    out = fleet.run(T0 + MIN, "status")
    assert "main: idle since" in out.stdout and "mate: ok" in out.stdout
    assert out.returncode == 0
    fleet.run(T0 + 30 * MIN)
    out = fleet.run(T0 + 30 * MIN, "status")
    assert "main: red since" in out.stdout and out.returncode == 1
    stale = fleet.run(T0 + 2 * 3600, "status")
    assert "stale" in stale.stdout and stale.returncode == 1


def test_status_without_a_home_list_reads_the_state_files(fleet):
    idle(fleet)
    fleet.run(T0)
    env = {
        **os.environ,
        "IDLE_LANES_STATE_DIR": str(fleet.state / "idle"),
        "IDLE_LANES_NOW": str(T0 + MIN),
    }
    env.pop("IDLE_LANES_HOMES", None)
    res = subprocess.run(
        [sys.executable, str(CHECK), "status"], env=env, capture_output=True, text=True
    )
    assert res.stdout.startswith("main: idle since")


def test_unit_and_timer_wiring():
    unit = (DIR / "lifekit-idle-lanes.service").read_text()
    assert "Type=oneshot" in unit and "User=__ADMIN_USER__" in unit
    assert '"IDLE_LANES_HOMES=__IDLE_LANES_HOMES__"' in unit
    assert "/home/" not in unit
    timer = (DIR / "lifekit-idle-lanes.timer").read_text()
    assert "OnUnitActiveSec=1min" in timer and "WantedBy=timers.target" in timer


def run_ensure(tmp_path, *args, homes="/srv/a /srv/b", enabled="enabled"):
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        '#!/usr/bin/env bash\necho "$*" >> "' + str(tmp_path / "calls") + '"\n'
        f'[[ "$1" == is-enabled ]] && echo {enabled}\nexit 0\n'
    )
    systemctl.chmod(0o755)
    env = {
        **os.environ,
        "INSTALL_ROOT": str(tmp_path / "root"),
        "SYSTEMCTL": str(systemctl),
        "ADMIN_USER": "ops",
    }
    if homes:
        env["IDLE_LANES_HOMES"] = homes
    else:
        env.pop("IDLE_LANES_HOMES", None)
    return subprocess.run(
        ["bash", str(ENSURE), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_ensure_installs_units_and_enables_the_timer(tmp_path):
    res = run_ensure(tmp_path)
    assert res.returncode == 0, res.stderr
    root = tmp_path / "root"
    unit = (root / "etc/systemd/system/lifekit-idle-lanes.service").read_text()
    assert "User=ops" in unit and '"IDLE_LANES_HOMES=/srv/a /srv/b"' in unit
    assert "__" not in unit
    assert (root / "usr/local/bin/lifekit-idle-lanes.py").stat().st_mode & 0o111
    assert (root / "var/lib/lifekit-fleet/idle-lanes").is_dir()
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls == ["daemon-reload", "enable --now lifekit-idle-lanes.timer"]


def test_ensure_check_reports_drift_and_converges(tmp_path):
    assert (
        run_ensure(tmp_path, "--check", homes="").returncode == 1
    )  # nothing installed
    assert run_ensure(tmp_path).returncode == 0
    # --check reuses the installed home list when none is given
    ok = run_ensure(tmp_path, "--check", homes="")
    assert ok.returncode == 0, ok.stdout
    drift = run_ensure(tmp_path, "--check", homes="/srv/other")
    assert drift.returncode == 1 and "does not match" in drift.stdout
    off = run_ensure(tmp_path, "--check", homes="", enabled="disabled")
    assert off.returncode == 1


def test_ensure_refuses_bad_homes(tmp_path):
    assert run_ensure(tmp_path, homes="").returncode == 1
    assert run_ensure(tmp_path, homes='/srv/a"b').returncode == 1
    assert run_ensure(tmp_path, homes="relative").returncode == 1
