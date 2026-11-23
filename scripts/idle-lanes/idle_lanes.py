#!/usr/bin/env python3
"""Idle-lane check: free worker lanes sitting beside ready work, escalated by time.

Run once a minute by lifekit-idle-lanes.timer as the account that owns the fleet
homes, once per home in IDLE_LANES_HOMES (absolute paths, space separated). It
only detects and escalates; it never changes how work is dispatched.

    idle_lanes.py [check]   one tick over every home (the timer's job)
    idle_lanes.py status    doctor view of every home: ok / idle since T / red
                            (homes from IDLE_LANES_HOMES, else the state files)

A home is idle-with-work when its bin/fm-capacity.sh reports free_lanes > 0 and
its bin/fm-tasks-axi.sh ready lists at least one item. `ready` already leaves out
held items and items whose date gate (hold --until) has not arrived, and this
script reads only its own ready[N] group, never a held group. The condition is
false when:

  * fm-capacity.sh names a host reason (cpu, memory, disk) - the home is full
    for a host reason, not idle;
  * the weekly quota snapshot (QUOTA_SNAPSHOT) shows the weekly window will be
    exhausted before it resets. Absent that (no snapshot, a stale one, or a
    projection that outlasts the reset) queued work counts as ready.

The first tick the condition holds starts an episode: the home's state file
(STATE_DIR/<home id>.json) records `since`. The tick the condition is false
clears it. A probe that fails leaves the episode as it is (no start, no clear).
At NOTE_AFTER seconds the home's supervisor gets one inbox note
(bin/fm-inbox.sh note, idempotent per episode). At PUSH_AFTER seconds one `act`
envelope goes to the owner through notify-relay and the state file turns `red`,
which is what the dashboard reads. A note or push that fails is retried on the
next tick; one that landed is never repeated within the episode.

State file (schema idle-lanes.v1): home, state (ok|idle|red), since (epoch or
null), free_lanes, ready, note_sent_at, pushed_at, checked_at, reason, error.

Env: IDLE_LANES_HOMES, IDLE_LANES_STATE_DIR (default /var/lib/lifekit-fleet/idle-lanes),
QUOTA_SNAPSHOT (default /var/lib/lifekit-fleet/metrics/quota-snapshot.json),
DOCKER, IDLE_LANES_NOTE_AFTER (900), IDLE_LANES_PUSH_AFTER (1800),
IDLE_LANES_NOW (epoch; tests).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "idle-lanes.v1"
STATE_DIR = Path(
    os.environ.get("IDLE_LANES_STATE_DIR", "/var/lib/lifekit-fleet/idle-lanes")
)
QUOTA_SNAPSHOT = Path(
    os.environ.get(
        "QUOTA_SNAPSHOT", "/var/lib/lifekit-fleet/metrics/quota-snapshot.json"
    )
)
DOCKER = os.environ.get("DOCKER", "docker")
NOTE_AFTER = int(os.environ.get("IDLE_LANES_NOTE_AFTER", "900"))
PUSH_AFTER = int(os.environ.get("IDLE_LANES_PUSH_AFTER", "1800"))
PROBE_TIMEOUT = 60
QUOTA_MAX_AGE = 7200
STALE_AFTER = 300
HOST_REASONS = {"cpu", "memory", "disk"}
# Runs inside the notify-relay container (node, no curl): envelope on stdin.
POST_JS = (
    "let b='';"
    "process.stdin.on('data',d=>b+=d).on('end',async()=>{"
    "const r=await fetch('http://127.0.0.1:'+(process.env.NOTIFY_RELAY_PORT||8090)+'/notify',"
    "{method:'POST',headers:{'content-type':'application/json'},body:b});"
    "console.log(r.status+' '+await r.text());process.exit(r.ok?0:1)})"
)


class ProbeError(Exception):
    """A home's lanes or queue could not be read this tick."""


def now() -> int:
    return int(os.environ.get("IDLE_LANES_NOW") or time.time())


def iso(epoch: int | None) -> str:
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def home_ids(homes: list[str]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for home in homes:
        hid = Path(home).name
        if not re.fullmatch(r"[A-Za-z0-9._-]+", hid) or hid in ids:
            raise SystemExit(
                f"fleet home {home!r}: id {hid!r} is not a unique plain name"
            )
        ids[hid] = home
    return ids


def configured_homes() -> dict[str, str]:
    homes = os.environ.get("IDLE_LANES_HOMES", "").split()
    if not homes:
        raise SystemExit("IDLE_LANES_HOMES is not set (space-separated fleet homes)")
    return home_ids(homes)


def run_in_home(home: str, script: str, *args: str) -> subprocess.CompletedProcess:
    path = Path(home) / "bin" / script
    try:
        return subprocess.run(
            [str(path), *args],
            cwd=home,
            env={**os.environ, "FM_HOME": home},
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ProbeError(f"{script}: {err}") from err


def read_capacity(home: str) -> tuple[int | None, list[str]]:
    """free_lanes (None when the home has no target) and its reason list."""
    res = run_in_home(home, "fm-capacity.sh")
    line = next(
        (ln for ln in res.stdout.splitlines() if ln.startswith("capacity:")), ""
    )
    if res.returncode != 0 or not line:
        raise ProbeError(
            f"fm-capacity.sh exit {res.returncode}: {res.stderr.strip()[:200]}"
        )
    fields = dict(re.findall(r"(\w+)=(\S+)", line))
    free = fields.get("free_lanes", "")
    if not (free.isdigit() or free == "unknown"):
        raise ProbeError(f"fm-capacity.sh: unreadable free_lanes in {line!r}")
    return (int(free) if free.isdigit() else None), fields.get("reason", "").split(",")


def read_ready(home: str) -> int:
    """Dispatchable ready items: the ready[N] group only (held ones are not in it)."""
    res = run_in_home(home, "fm-tasks-axi.sh", "ready")
    if res.returncode != 0:
        raise ProbeError(
            f"fm-tasks-axi.sh ready exit {res.returncode}: {res.stderr.strip()[:200]}"
        )
    m = re.search(r"^ready\[(\d+)\]", res.stdout, re.M)
    if m:
        return int(m.group(1))
    m = re.search(r"^count:\s*(\d+)\s*$", res.stdout, re.M)
    if m and int(m.group(1)) == 0:
        return 0
    raise ProbeError("fm-tasks-axi.sh ready: no ready group in its output")


def quota_paused(at: int) -> bool:
    """True only when the weekly window is projected to run out before it resets."""
    try:
        snap = json.loads(QUOTA_SNAPSHOT.read_text())
        if at - int(snap["generated_epoch"]) > QUOTA_MAX_AGE:
            return False
        weekly = snap["windows"]["seven_day"]
        exhausted = int(snap["projected_exhausted_at"])
        return (
            snap.get("runway_status") == "projected_exhaustion"
            and snap.get("limiting_window") == "seven_day"
            and exhausted < int(weekly["resets_at"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def load_state(hid: str) -> dict:
    try:
        data = json.loads((STATE_DIR / f"{hid}.json").read_text())
        return data if data.get("schema") == SCHEMA else {}
    except (OSError, ValueError, AttributeError):
        return {}


def save_state(hid: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f".{hid}.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o644)
    tmp.replace(STATE_DIR / f"{hid}.json")


def send_note(hid: str, home: str, st: dict) -> str | None:
    text = (
        f"idle-lanes: {st['free_lanes']} free lane(s) and {st['ready']} ready item(s) "
        f"have sat idle together since {iso(st['since'])}; refill the lanes "
        "(bin/fm-tasks-axi.sh ready lists the queue)."
    )
    try:
        res = run_in_home(
            home,
            "fm-inbox.sh",
            "note",
            "--request-id",
            f"idle-lanes-{hid}-{st['since']}",
            "--",
            text,
        )
    except ProbeError as err:
        return str(err)
    return (
        None
        if res.returncode == 0
        else f"fm-inbox.sh note exit {res.returncode}: {res.stderr.strip()[:200]}"
    )


def push_owner(hid: str, st: dict, minutes: int) -> str | None:
    env = {
        "level": "act",
        "source": "fleet",
        "subject": hid,
        "headline": (
            f"{st['free_lanes']} free lane(s) idle with {st['ready']} item(s) queued "
            f"for {minutes} min"
        ),
    }
    try:
        ps = subprocess.run(
            [
                DOCKER,
                "ps",
                "-q",
                "--filter",
                "label=com.docker.compose.project=compose",
                "--filter",
                "label=com.docker.compose.service=notify-relay",
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
        cid = ps.stdout.split()[0] if ps.stdout.split() else ""
        if not cid:
            return "no running notify-relay container in compose project compose"
        res = subprocess.run(
            [DOCKER, "exec", "-i", cid, "node", "-e", POST_JS],
            input=json.dumps(env),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return f"cannot reach notify-relay: {err}"
    if res.returncode != 0:
        return f"notify-relay refused the envelope: {(res.stdout + res.stderr).strip()[:200]}"
    return None


def tick(hid: str, home: str, at: int, paused: bool) -> str | None:
    """One home, one tick. Returns a problem to report (exit 1), else None."""
    prev = load_state(hid)
    base = {"schema": SCHEMA, "home": hid, "checked_at": at}
    try:
        free, reasons = read_capacity(home)
        ready = read_ready(home)
    except ProbeError as err:
        # Neither start nor clear an episode on a failed read.
        save_state(hid, {**prev, **base, "error": str(err)})
        return f"{hid}: {err}"

    host_full = bool(HOST_REASONS & set(reasons))
    if paused:
        why = "quota"
    elif host_full:
        why = "host-full"
    elif not free:
        why = "no-free-lanes" if free == 0 else "no-lane-target"
    elif not ready:
        why = "no-ready-work"
    else:
        why = ""
    counts = {"free_lanes": free, "ready": ready}

    if why:
        save_state(
            hid,
            {
                **base,
                **counts,
                "state": "ok",
                "since": None,
                "note_sent_at": None,
                "pushed_at": None,
                "reason": why,
                "error": None,
            },
        )
        return None

    in_episode = prev.get("state") in ("idle", "red") and prev.get("since")
    st = {
        **base,
        **counts,
        "state": prev["state"] if in_episode else "idle",
        "since": prev["since"] if in_episode else at,
        "note_sent_at": prev.get("note_sent_at") if in_episode else None,
        "pushed_at": prev.get("pushed_at") if in_episode else None,
        "reason": "idle-with-work",
        "error": None,
    }
    elapsed = at - st["since"]
    problem = None
    if elapsed >= NOTE_AFTER and not st["note_sent_at"]:
        err = send_note(hid, home, st)
        if err is None:
            st["note_sent_at"] = at
        else:
            problem = f"{hid}: note failed: {err}"
    if elapsed >= PUSH_AFTER and not st["pushed_at"]:
        err = push_owner(hid, st, elapsed // 60)
        if err is None:
            st["pushed_at"] = at
        else:
            problem = f"{hid}: owner push failed: {err}"
    if st["pushed_at"]:
        st["state"] = "red"
    if problem:
        st["error"] = problem
    save_state(hid, st)
    return problem


def check() -> int:
    homes = configured_homes()
    at = now()
    paused = quota_paused(at)
    problems = [p for hid, home in homes.items() if (p := tick(hid, home, at, paused))]
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


def describe(hid: str, at: int) -> tuple[str, bool]:
    st = load_state(hid)
    if not st:
        return f"{hid}: no data (the check has not run)", True
    stale = at - int(st.get("checked_at", 0)) > STALE_AFTER
    counts = f"free {st.get('free_lanes')}, ready {st.get('ready')}"
    if st.get("state") == "red":
        line, bad = (
            f"{hid}: red since {iso(st['since'])} ({counts}; owner pushed {iso(st.get('pushed_at'))})",
            True,
        )
    elif st.get("state") == "idle":
        line, bad = f"{hid}: idle since {iso(st['since'])} ({counts})", False
    elif st.get("reason") == "no-lane-target":
        line, bad = f"{hid}: unmonitored: no lane target", True
    elif st.get("reason"):
        line, bad = f"{hid}: ok ({st['reason']})", False
    else:
        line, bad = f"{hid}: ok", False
    if st.get("error"):
        line += f" [last tick: {st['error']}]"
        bad = True
    if stale:
        line += f" [stale: last checked {iso(st.get('checked_at'))}]"
        bad = True
    return line, bad


def status() -> int:
    at = now()
    if os.environ.get("IDLE_LANES_HOMES", "").split():
        ids = list(configured_homes())
    else:  # on the host the unit owns the home list; read what it has written
        ids = sorted(p.stem for p in STATE_DIR.glob("*.json"))
    if not ids:
        print("no idle-lane state yet (IDLE_LANES_HOMES unset, no state files)")
        return 1
    rows = [describe(hid, at) for hid in ids]
    for line, _ in rows:
        print(line)
    return 1 if any(bad for _, bad in rows) else 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "check":
        return check()
    if cmd == "status":
        return status()
    print(
        __doc__.split("\n\n")[1] if __doc__ else "usage: idle_lanes.py [check|status]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
