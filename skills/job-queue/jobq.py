#!/usr/bin/env python3
"""The shared job queue: work that waits for an egress to the outside world.

    jobq.py next        the drain gate's check (drain-gate.js): start a job, or
                        hand an agent turn one that has run?
    jobq.py output <id> what the job's run printed, for the turn that answers it
    jobq.py list        what is waiting, running, and recently finished
    jobq.py list --json one item envelope per job (decision 0019), as JSON, for
                        the fleet publisher's items and stalled check
    jobq.py depth       counts only, as JSON, for the fleet status page
    jobq.py ages        each job's state and times (no label or result), as JSON:
                        the publisher's older read, before `list --json`
    jobq.py drop <id>   remove one job
    jobq.py enqueue <kind> <key> ...  queue a job from a script, print its receipt
    jobq.py status <id> the receipt of one job, as it stands now
    jobq.py runner <id> the run itself, started by the launcher (or `next`),
                        never by hand
    jobq.py launcher    the long-lived process that starts runs, run by the
                        gateway's entrypoint, never by hand
    jobq.py launcher-status  whether a launcher is running

Some fetches cannot leave from this server (Instagram and YouTube refuse its
IP), so they go out through an egress: today the owner's PC, reached by the
yt-tunnel relay. A skill that needs one adds a job here and answers the owner
at once; a cron row on kit (job-queue-drain, docs/runbook.md "Shared job
queue") runs its gate every few minutes, and when the egress answers and
nothing else is running, the gate starts one job: the queue runs the handler
command the producing skill stored, on its own, for as long as it takes. Once
the handler has ended, the gate hands kit one turn to answer from what it
printed, as the owning skill's rules say. So a backlog gathered while the PC
was off drains, one job and one owner message at a time, once it is on. The
queue knows no kinds: a producer brings its own handler and owner skill, and
the handler reports back with running / finish / retry / release. The agent
never runs a handler: an agent turn is bounded (its tool calls time out, the
turn ends), a download and a transcription on a busy host are not, and a
handler killed mid-run never reports back. A job whose input is already on
this host (a file to evaluate) names no probe host: it needs no egress, so the
gate starts the oldest job whose egress answers, and that file runs PC on or
off, even with a link waiting for the PC ahead of it.

The egress is one setting, LIFEKIT_EGRESS_PROXY (the gateway's environment,
compose/openclaw/docker-compose.yml): a socks5h:// or http:// proxy URL, or
"direct" for no proxy. Default: the PC relay. The queue only asks it whether
the job's probe host can be reached; the handlers send their downloads
through it. Nothing here or in a handler knows it is a PC.

Jobs are JSON files in a private directory (JOBQ_DIR, default
~/.life-state/job-queue, mode 0700, files 0600), one per <kind>-<key>, so a
second add of the same key while it waits is "already queued". A job is
queued -> running -> done | failed; a run counts an attempt, and after
JOBQ_MAX_ATTEMPTS (default 3) failures it is failed, which the handler
reports to the owner once. The run is a detached runner process, bounded by
JOBQ_RUN_MINUTES (default 90); its output waits in <id>.out (0600) until a
turn reads it. The gate's command cannot start it: the exec tool runs each
command under a child-subreaper anchor that, when the command ends, kills
every process the command left behind, however detached (OpenClaw 2026.11.9;
lifekit-stack#676). So `next` only claims the job and asks the launcher, a
long-lived `jobq.py launcher` the gateway's entrypoint starts outside every
exec, to start the runner; with no launcher running (a host, a test) `next`
starts it itself. A runner records how far it got as it goes (run.stage,
run.alive_at), and however it ends, its signal, stage and run time, so a run
killed outright still names the stage it reached and how long it lived. A
running job nobody will report back for is taken up again at once when its
runner died (a gateway restart) or its handler ended leaving nothing for a
turn, LAUNCH_GRACE seconds after `next` asked for a runner no launcher
started, and JOBQ_LEASE_MINUTES (default 30) after a turn read its output
without finishing it. The queue gives up on its own, and the gate hands kit a
one-line report for the owner instead of the output, when a job used up its
attempts that way, or when OFFER_LIMIT drain turns in a row were handed its
output and none read it, so no job loops or ends unreported.
A quiet job (one filed by an agent that reads its status, not by the owner)
is given up with NO_REPLY instead: its producer polls the job.
Finished jobs stay JOBQ_KEEP_DAYS (default 14) for `list`, then are pruned.

A door carries the envelope and returns a receipt (docs/runbook.md, "Envelope
and receipt"; the text is one build-standards entry in the vault, cited there,
never restated here). `enqueue` takes the envelope fields as flags and prints
the receipt `{id, url, state}` on stdout; the drop door (a producer that adds
jobs itself) may carry none, or only the fields it fills itself. A repeat of a
job that is still waiting or running adds a count and the latest occurrence to
that job; a finished job queued again names the finished one it reopens.

`next` prints one line first, for the gate: "JOBQ ready" (a turn's
instructions follow), "JOBQ idle" (nothing to answer: nothing queued, one
running, or one just started) or "JOBQ wait" (jobs wait, the egress does not
answer). Anything else is a fault the gate surfaces.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_EGRESS = "socks5h://host.docker.internal:18081"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_MINUTES = 30
DEFAULT_RUN_MINUTES = 90
DEFAULT_KEEP_DAYS = 14
# Drain turns in a row that may be handed a run's output without reading it.
OFFER_LIMIT = 3

KIND = re.compile(r"[a-z][a-z0-9-]{0,31}")
KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
LOCK = ".lock"
LAUNCHER = ".launcher"  # the running launcher's {pid, start}
LAUNCH_GRACE = 120  # seconds a launcher has to start a requested runner
BEAT = 60  # seconds between a runner's signs of life (run.alive_at)
LAUNCHER_POLL = 1.0
NO_REPLY = "NO_REPLY"
# The envelope every door accepts (build-standards, "Envelope and receipt at
# every door"): the class names and the A2A state names are that entry's.
CLASSES = ("dev", "ops", "finance", "life", "unclear")
ENVELOPE_REQUIRED = ("origin", "owner", "class", "acceptance")
ENVELOPE_FIELDS = (*ENVELOPE_REQUIRED, "fingerprint", "thread")
STATE_NAMES = {
    QUEUED: "submitted",
    RUNNING: "working",
    DONE: "completed",
    FAILED: "failed",
}
# Set for the handler by its runner: the job this process was started for.
RUN_ENV = "JOBQ_RUN_ID"


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def max_attempts() -> int:
    return _int_env("JOBQ_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)


def egress() -> str:
    return os.environ.get("LIFEKIT_EGRESS_PROXY", "").strip() or DEFAULT_EGRESS


def egress_proxy() -> str | None:
    """The proxy URL a handler hands its downloader, or None for direct."""
    e = egress()
    return None if e == "direct" else e


def root() -> Path:
    d = Path(os.environ.get("JOBQ_DIR") or Path.home() / ".life-state" / "job-queue")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def job_id(kind: str, key: str) -> str:
    if not KIND.fullmatch(kind) or not KEY.fullmatch(key):
        raise ValueError(f"bad job kind or key: {kind!r} {key!r}")
    return f"{kind}-{key}"


@contextlib.contextmanager
def locked():
    d = root()
    with open(d / LOCK, "a") as f:
        os.chmod(f.fileno(), 0o600)
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield d
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _path(d: Path, jid: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}-[A-Za-z0-9_-]{1,64}", jid):
        raise ValueError(f"bad job id: {jid!r}")
    return d / f"{jid}.json"


def _load(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _save(p: Path, job: dict) -> None:
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(job, f, indent=1, sort_keys=True)
    os.replace(tmp, p)


def _jobs(d: Path) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(d.glob("*.json")):
        job = _load(p)
        if job is not None:
            out.append((p, job))
    return out


def _out(d: Path, jid: str) -> Path:
    return d / f"{jid}.out"


def _err(d: Path, jid: str) -> Path:
    """The runner's own stderr (a crash's traceback), kept beside the job so a
    run that dies leaves its error somewhere a later turn can read."""
    return d / f"{jid}.err"


def _tail(text: str, lines: int = 5, limit: int = 400) -> str:
    """The last few non-empty lines of a stream, on one line, newest last."""
    kept = [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]
    return " | ".join(kept)[-limit:]


def _exit_text(code: int | None) -> str:
    if code is None:
        return "no exit status"
    if code < 0:
        try:
            return f"killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"killed by signal {-code}"
    return f"exit {code}"


def _proc_start(pid: int) -> str | None:
    """When the process started (clock ticks after boot), or None when it is
    gone or a zombie. With the pid it names one process, so a pid reused after
    a gateway restart is never taken for the runner."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[1].split()
    return None if fields[0] in ("Z", "X") else fields[19]


def _stale(job: dict, now: float) -> bool:
    """A running job nobody will report back for: its runner died before the
    handler ended (a gateway restart), the handler ended leaving nothing for a
    turn to answer, or the turn that read its output did not finish it within
    the lease. A job claimed without a runner keeps the lease from its claim."""
    if job["status"] != RUNNING:
        return False
    lease = _int_env("JOBQ_LEASE_MINUTES", DEFAULT_LEASE_MINUTES) * 60
    run = job.get("run")
    if run is None:
        return now - job.get("claimed_at", 0) > lease
    if "ran_at" not in run:
        if "pid" not in run:  # asked of the launcher, not started yet
            return now - run.get("requested_at", 0) > LAUNCH_GRACE
        start = _proc_start(run["pid"])
        return start is None or start != run["start"]
    if run.get("unread"):
        return False
    if "read_at" in run:
        return now - run["read_at"] > lease
    return True


def _prune(d: Path, now: float) -> None:
    keep = _int_env("JOBQ_KEEP_DAYS", DEFAULT_KEEP_DAYS) * 86400
    for p, job in _jobs(d):
        if job["status"] in (DONE, FAILED) and now - job.get("finished_at", 0) > keep:
            p.unlink(missing_ok=True)
            _out(d, job["id"]).unlink(missing_ok=True)
            _err(d, job["id"]).unlink(missing_ok=True)


def add(
    kind: str,
    key: str,
    *,
    owner: str,
    handler: list[str],
    probe: str,
    payload: dict | None = None,
    label: str = "",
    quiet: bool = False,
    envelope: dict | None = None,
    now: float | None = None,
) -> tuple[bool, int]:
    """Queue a job. Returns (added, jobs waiting): added is False when the
    same job is already queued or running. A finished one is queued again.
    The label (such as the link the owner sent) names the job to the owner.
    An empty probe means the job needs no egress; a quiet job's give-up says
    nothing to the owner. The envelope (see `make_envelope`) is kept as given.
    A repeat of a job still waiting or running adds to its count and records
    the latest occurrence; a finished job queued again names the one it
    reopens."""
    now = time.time() if now is None else now
    if probe and not HOST.fullmatch(probe):
        raise ValueError(f"bad probe host: {probe!r}")
    jid = job_id(kind, key)
    with locked() as d:
        _prune(d, now)
        p = _path(d, jid)
        old = _load(p)
        added = old is None or old["status"] in (DONE, FAILED)
        if not added:
            old["repeats"] = old.get("repeats", 0) + 1
            old["latest"] = {"at": now, **_latest(envelope)}
            _save(p, old)
        if added:
            _out(d, jid).unlink(missing_ok=True)
            job = {
                "id": jid,
                "kind": kind,
                "key": key,
                "owner": owner,
                "handler": list(handler),
                "probe": probe,
                "payload": payload or {},
                "label": " ".join(label.split())[:300],
                "quiet": quiet,
                "status": QUEUED,
                "queued_at": now,
                "attempts": 0,
            }
            if envelope:
                job["envelope"] = dict(envelope)
            if old is not None:
                job["reopens"] = {
                    "id": jid,
                    "status": old["status"],
                    "finished_at": old.get("finished_at", 0),
                }
            _save(p, job)
        waiting = sum(1 for _, j in _jobs(d) if j["status"] in (QUEUED, RUNNING))
    return added, waiting


def _latest(envelope: dict | None) -> dict:
    """What a repeat tells the open job about itself: who raised it again."""
    e = envelope or {}
    return {k: e[k] for k in ("origin", "thread") if e.get(k)}


def make_envelope(
    fields: dict, *, default_fingerprint: str = "", default_thread: str = ""
) -> dict:
    """The envelope from the fields a door was given, or ValueError naming
    what a door refuses: a missing required field, a class or an unknown field
    outside the list. The fingerprint and the thread, when a door gives none,
    are the job's own (an intake job deduplicates on its key, and a one-shot
    job is its own thread)."""
    unknown = sorted(set(fields) - set(ENVELOPE_FIELDS))
    if unknown:
        raise ValueError(f"unknown envelope field: {', '.join(unknown)}")
    env = {k: " ".join(str(fields.get(k) or "").split()) for k in ENVELOPE_FIELDS}
    env["fingerprint"] = env["fingerprint"] or default_fingerprint
    env["thread"] = env["thread"] or default_thread
    missing = [k for k in ENVELOPE_REQUIRED if not env[k]]
    if missing:
        raise ValueError(f"envelope missing: {', '.join(missing)}")
    if env["class"] not in CLASSES:
        raise ValueError(f"envelope class must be one of {', '.join(CLASSES)}")
    return env


def receipt(jid: str, url: str, state: str) -> dict:
    """The receipt every door returns at once, `{id, url, state}`. The state is
    a job's own status, in the A2A name."""
    return {"id": jid, "url": url, "state": STATE_NAMES.get(state, state)}


def status_command(jid: str) -> str:
    return shlex.join([sys.executable, str(Path(__file__).resolve()), "status", jid])


def job_receipt(jid: str, url: str = "") -> dict | None:
    """The receipt of a job as it stands now, or None when there is no such job."""
    job = get(jid)
    if job is None:
        return None
    return receipt(jid, url or status_command(jid), job["status"])


def get(jid: str) -> dict | None:
    with locked() as d:
        return _load(_path(d, jid))


def position(jid: str) -> tuple[int, int] | None:
    """Where a job stands among the jobs waiting or running: (its place, how
    many there are), a running job first, then oldest first. None when it is
    neither waiting nor running. A link that waits for the egress can be
    passed by a file that needs none, so the place is the queue's order, not
    a promise."""
    with locked() as d:
        live = [j for _, j in _jobs(d) if j["status"] in (QUEUED, RUNNING)]
    live.sort(key=lambda j: (j["status"] != RUNNING, j["queued_at"]))
    for n, j in enumerate(live, 1):
        if j["id"] == jid:
            return n, len(live)
    return None


def _runnable(d: Path, now: float) -> list[dict]:
    """The jobs that may run, oldest first; none while another is running: one
    at a time, so a backlog never runs several downloads and transcriptions at
    once."""
    jobs = [j for _, j in _jobs(d)]
    if any(j["status"] == RUNNING and not _stale(j, now) for j in jobs):
        return []
    ready = [j for j in jobs if j["status"] == QUEUED or _stale(j, now)]
    return sorted(ready, key=lambda j: j["queued_at"])


def _first_ready(jobs: list[dict]) -> dict | None:
    """The oldest job whose egress answers; a job with no probe host always
    does, so a job waiting for the PC never holds back a file already here."""
    answered: dict[str, bool] = {}
    for j in jobs:
        if not j["probe"]:
            return j
        if j["probe"] not in answered:
            answered[j["probe"]] = egress_ready(j["probe"])
        if answered[j["probe"]]:
            return j
    return None


def _claim(job: dict, now: float) -> None:
    job.update(status=RUNNING, claimed_at=now, attempts=job["attempts"] + 1, offers=0)
    job.pop("run", None)


def claim(jid: str, now: float | None = None) -> dict | None:
    """Mark the job running and count the attempt, with no runner: `next`
    claims and starts a job in one step. None when it is not the job to run
    (gone, finished, another one is running, or an older job whose egress
    answers goes first)."""
    now = time.time() if now is None else now
    with locked() as d:
        ready = _runnable(d, now)
    ids = [j["id"] for j in ready]
    if jid not in ids or _first_ready(ready[: ids.index(jid)]) is not None:
        return None
    with locked() as d:
        job = next((j for j in _runnable(d, now) if j["id"] == jid), None)
        if job is None:
            return None
        _claim(job, now)
        _save(_path(d, jid), job)
        return job


def running(jid: str) -> dict | None:
    """The job, for its handler: only when it is running and the queue's
    runner started this process for it, so a handler run by hand never runs a
    job beside the queue's own run."""
    if os.environ.get(RUN_ENV) != jid:
        return None
    job = get(jid)
    return job if job is not None and job["status"] == RUNNING else None


def _spawn(d: Path, jid: str, now: float) -> dict:
    """Start the job's runner in a session of its own with its stdio closed,
    so a kill of its starter's process group leaves it be. That is not enough
    under an exec tool's anchor, which kills what its command leaves behind:
    the launcher starts it there (`launcher`)."""
    fd = os.open(_err(d, jid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as err:  # the runner's crash, not a closed stream
        p = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "runner", jid],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
        )
    return {"pid": p.pid, "start": _proc_start(p.pid), "started_at": now}


def _launcher_alive(d: Path) -> bool:
    """A launcher is running: the one its file names, by pid and start time."""
    info = _load(d / LAUNCHER)
    if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
        return False
    return _proc_start(info["pid"]) == info.get("start")


def launcher_status() -> str:
    d = root()
    if _launcher_alive(d):
        return f"launcher pid {_load(d / LAUNCHER)['pid']} is running"
    return (
        "no launcher is running: `next` starts runs itself, and an exec tool kills them"
    )


def _launch_requested(d: Path, now: float) -> None:
    """Under the lock: start the runner of each job `next` asked for."""
    for p, job in _jobs(d):
        run = job.get("run") or {}
        if job["status"] == RUNNING and "requested_at" in run and "pid" not in run:
            run.update(_spawn(d, job["id"], now), via="launcher")
            job["run"] = run
            _save(p, job)


def _reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def launcher(poll: float = LAUNCHER_POLL) -> int:
    """Start each run `next` asks for, from outside the exec tool's process
    tree, so the run outlives the gate's command. One at a time: a second
    launcher leaves at once. It leaves when this file changes (a deploy), so
    the entrypoint's loop starts the new code."""
    me = Path(__file__).resolve()
    born = me.stat().st_mtime
    with locked() as d:
        if _launcher_alive(d):
            return 0
        _save(d / LAUNCHER, {"pid": os.getpid(), "start": _proc_start(os.getpid())})
    fd = os.open(d / f"{LAUNCHER}.err", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 2)
    os.close(fd)
    while True:
        try:
            with locked() as d:
                if (_load(d / LAUNCHER) or {}).get("pid") != os.getpid():
                    return 0
                _launch_requested(d, time.time())
        except (OSError, ValueError) as e:
            print(f"launcher: {e}", file=sys.stderr, flush=True)
        _reap()
        try:
            if me.stat().st_mtime != born:
                return 0
        except OSError:
            return 0
        time.sleep(poll)


def _mine(job: dict | None) -> bool:
    return job is not None and (job.get("run") or {}).get("pid") == os.getpid()


class _Killed(BaseException):
    """The runner was told to stop (a gateway restart, an exec tool's cleanup)."""

    def __init__(self, signum: int):
        self.signum = signum


def _on_signal(signum, _frame):
    raise _Killed(signum)


def _elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def _mark(jid: str, **fields) -> None:
    """Note how far this runner got (run.stage, run.alive_at), so a runner
    killed outright, with no chance to record anything, still names it."""
    with contextlib.suppress(OSError), locked() as d:
        p = _path(d, jid)
        job = _load(p)
        if _mine(job):
            job["run"].update(fields)
            _save(p, job)


def _wait(
    proc: subprocess.Popen, jid: str, deadline: float
) -> tuple[int | None, bytes]:
    """The handler's exit status and stderr, with a sign of life every BEAT
    seconds; killed at the deadline (JOBQ_RUN_MINUTES)."""
    while True:
        left = deadline - time.time()
        try:
            err = proc.communicate(timeout=max(0.0, min(BEAT, left)))[1]
            return proc.returncode, err
        except subprocess.TimeoutExpired:
            if time.time() >= deadline:
                os.killpg(proc.pid, signal.SIGKILL)
                return None, proc.communicate()[1] + b"\nran over the time limit"
            _mark(jid, alive_at=time.time())


def run_handler(jid: str) -> None:
    """The runner: run the job's handler to its end (at most JOBQ_RUN_MINUTES),
    keep what it printed for the turn that answers it, and note that it ran.
    It records under the lock its starter holds while it starts it, so the job
    names this runner by then; a runner the job no longer names records
    nothing. Its signal handlers come first, before it waits for the lock, so
    a stop at any point is caught. However the run ends (the handler's exit or
    death by a signal, this runner told to stop, or a crash of its own) the
    exit status, the last stderr lines, the stage it reached and how long it
    ran are recorded, so a run that dies is never silent; one killed outright
    leaves the stage and its last sign of life (run.stage, run.alive_at)."""
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _on_signal)
    began = time.time()
    stage = "runner started"
    limit = _int_env("JOBQ_RUN_MINUTES", DEFAULT_RUN_MINUTES) * 60
    d = root()
    out = _out(d, jid)
    code: int | None = None
    err = b""
    proc = None
    stopped: int | None = None
    try:
        with locked() as d:
            p = _path(d, jid)
            job = _load(p)
            if not _mine(job):
                return
            job["run"].update(stage=stage, runner_at=began, alive_at=began)
            _save(p, job)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            proc = subprocess.Popen(
                [*job["handler"], jid],
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.PIPE,
                env={**os.environ, RUN_ENV: jid},
                start_new_session=True,
            )
            stage = "handler running"
            _mark(jid, stage=stage, handler_pid=proc.pid, alive_at=time.time())
            code, err = _wait(proc, jid, began + limit)
            if code is None:
                err += f" of {limit // 60} minutes".encode()
            stage = "handler ended"
    except _Killed as k:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)  # let the record be written
        stopped = k.signum
        code, err = (
            None,
            f"the runner was stopped by {signal.Signals(k.signum).name} at "
            f"stage '{stage}' after {_elapsed(time.time() - began)}".encode(),
        )
    except OSError as e:
        code, err = None, str(e).encode()
    except Exception as e:  # the runner's own bug: still recorded, never silent
        code, err = None, f"runner crashed: {type(e).__name__}: {e}".encode()
    if proc is not None and stage != "handler ended":
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    text = ""
    if code == 0:
        with contextlib.suppress(OSError):
            text = out.read_text(errors="replace").strip()
    with locked() as d:
        p = _path(d, jid)
        job = _load(p)
        if not _mine(job):
            return
        ended = time.time()
        job["run"].update(
            ran_at=ended, exit=code, stage=stage, ran_for=round(ended - began, 1)
        )
        if stopped is not None:
            job["run"]["signal"] = signal.Signals(stopped).name
        job["offers"] = 0
        if text and text != NO_REPLY:
            job["run"]["unread"] = True
        else:
            out.unlink(missing_ok=True)
        if code != 0:
            tail = _tail(err.decode(errors="replace"))
            job["last_error"] = f"{_exit_text(code)}: {tail}"[:500].rstrip(": ")
        _save(p, job)


def output(jid: str, now: float | None = None) -> str:
    """What the job's run printed, read once by the turn that answers it."""
    now = time.time() if now is None else now
    with locked() as d:
        p = _path(d, jid)
        job = _load(p)
        if job is None or not (job.get("run") or {}).get("unread"):
            return NO_REPLY
        out = _out(d, jid)
        try:
            text = out.read_text(errors="replace").strip()
        except OSError:
            text = ""
        out.unlink(missing_ok=True)
        del job["run"]["unread"]
        job["run"]["read_at"] = now
        _save(p, job)
    return text or NO_REPLY


def _finish(jid: str, status: str, result: str, now: float | None) -> None:
    now = time.time() if now is None else now
    with locked() as d:
        p = _path(d, jid)
        job = _load(p)
        if job is not None:
            job.update(status=status, result=result[:500], finished_at=now)
            _save(p, job)


def finish(jid: str, result: str, now: float | None = None) -> None:
    """The job ran to an answer for the owner (a result, or a final 'cannot')."""
    _finish(jid, DONE, result, now)


def retry(jid: str, error: str, now: float | None = None) -> bool:
    """The run failed. Queue it again, or, after the last attempt, mark it
    failed. Returns True when that was the last attempt."""
    with locked() as d:
        p = _path(d, jid)
        job = _load(p)
        if job is None:
            return True
        final = job["attempts"] >= max_attempts()
        job.update(last_error=error[:500])
        if not final:
            job.update(status=QUEUED)
            _save(p, job)
    if final:
        _finish(jid, FAILED, error, now)
    return final


def release(jid: str) -> None:
    """The egress went away mid-run: back in the queue, the attempt uncounted."""
    with locked() as d:
        p = _path(d, jid)
        job = _load(p)
        if job is not None and job["status"] == RUNNING:
            job.update(status=QUEUED, attempts=max(0, job["attempts"] - 1))
            _save(p, job)


class EgressError(OSError):
    """The proxy answered and would not open the connection."""

    def __init__(self, message: str, refused: bool):
        super().__init__(message)
        self.refused = (
            refused  # it refused this host (an allow-list), not "cannot reach"
        )


def open_connection(
    host: str, port: int, proxy: str | None = None, timeout: float = 20.0
) -> socket.socket:
    """A TCP connection to host:port through the egress (proxy: a URL, "direct",
    or None for the configured egress). The name is resolved at the far end."""
    proxy = egress() if proxy is None else proxy
    if proxy == "direct":
        return socket.create_connection((host, port), timeout=timeout)
    u = urlparse(proxy)
    socks = u.scheme in ("socks5", "socks5h")
    if not socks and u.scheme != "http":
        raise ValueError(f"LIFEKIT_EGRESS_PROXY: unsupported scheme in {proxy!r}")
    s = socket.create_connection(
        (u.hostname, u.port or (1080 if socks else 8080)), timeout=timeout
    )
    try:
        s.settimeout(timeout)
        if socks:
            _socks5(s, host, port)
        else:
            _http_connect(s, host, port)
    except BaseException:
        s.close()
        raise
    return s


def _socks5(s: socket.socket, host: str, port: int) -> None:
    name = host.encode("idna")
    s.sendall(b"\x05\x01\x00")
    if _recv(s, 2) != b"\x05\x00":
        raise OSError("the SOCKS proxy wants authentication")
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    _, rep, _, atyp = _recv(s, 4)
    if rep != 0:
        raise EgressError(f"SOCKS reply 0x{rep:02x} for {host}", refused=rep == 2)
    size = {1: 4, 4: 16}.get(atyp) or _recv(s, 1)[0]
    _recv(s, size + 2)  # the bound address, unused


def _http_connect(s: socket.socket, host: str, port: int) -> None:
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += _recv(s, 1)
        if len(head) > 8192:
            raise OSError("the HTTP proxy sent an oversized reply")
    status = head.split(b" ", 2)[1] if head.count(b" ") else b""
    if status != b"200":
        code = status.decode(errors="replace")
        raise EgressError(f"HTTP proxy {code} for {host}", refused=code[:1] == "4")


def egress_ready(host: str, proxy: str | None = None, timeout: float = 8.0) -> bool:
    """Can the egress reach host:443 now? A real CONNECT through the proxy: a
    relay listens even when what is behind it is down, and answers a CONNECT
    it cannot carry out with a failure. A proxy that refuses the host itself
    (an allow-list) counts as ready, so the handler's run fails loudly and the
    owner hears about it, instead of the job waiting forever."""
    try:
        open_connection(host, 443, proxy, timeout).close()
    except EgressError as e:
        return e.refused
    except OSError:
        return False
    return True


def _recv(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("proxy closed the connection")
        buf += chunk
    return buf


def _name(job: dict) -> str:
    return job.get("label") or job["id"]


def ready_message(job: dict) -> str:
    since = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(job["queued_at"]))
    me = [sys.executable, str(Path(__file__).resolve())]
    return (
        f"Job {job['id']} has run (attempt {job['attempts']} of "
        f"{max_attempts()}, queued {since}).\n"
        f"Run exactly: {shlex.join([*me, 'output', job['id']])}\n"
        f"Then answer as the {job['owner']} skill says for a queued job."
    )


def give_up_message(job: dict, reason: str) -> str:
    if job.get("quiet"):
        return (
            f"Job {job['id']} gave up: {reason}.\n"
            "Run nothing. Reply exactly NO_REPLY: whoever queued it reads its status."
        )
    return (
        f"Job {job['id']} gave up: {reason}.\n"
        f"Run nothing. Tell the owner in one short message that the queued "
        f"{job['owner']} job for {_name(job)} failed and why; it does not say "
        f"anything about the {job['kind']} itself."
    )


def _never_reported(d: Path, job: dict) -> str:
    """Why a stale job's runs never reported back, from the final run's own
    evidence: its recorded exit, else that the runner itself died, with its
    stderr (a crash's traceback) when it left one. An older attempt's error
    is only ever labelled as earlier."""
    n = job["attempts"]
    reason = f"its last {n} runs never reported back"
    run = job.get("run") or {}
    if "read_at" in run:
        return f"{reason} (its output was read but the turn never finished the job)"
    if "ran_at" in run:
        if run.get("exit") == 0:
            return f"{reason} (the handler exited 0 and printed nothing)"
        return f"{reason} (last error: {job.get('last_error')})"
    try:
        tail = _tail(_err(d, job["id"]).read_text(errors="replace"))
    except OSError:
        tail = ""
    gone = _gone(run, d)
    earlier = f"; earlier error: {job['last_error']}" if job.get("last_error") else ""
    return (
        f"{reason} ({gone}"
        f"{'; its stderr: ' + tail if tail else '; it left no stderr'}{earlier})"
    )


def _gone(run: dict, d: Path) -> str:
    """What a dead runner's own record says of it: the stage it reached, how
    long it lived, and what can kill a runner with no chance to record."""
    if not run:
        return "no runner was started for it"
    if "pid" not in run:
        alive = "" if _launcher_alive(d) else " (no launcher is running)"
        return f"no launcher started its runner within {LAUNCH_GRACE}s{alive}"
    gone = f"runner pid {run['pid']} was gone before it recorded an exit"
    if "stage" not in run:
        return (
            f"{gone}, before it reached its first stage: killed within moments "
            "of its start"
            + (
                ""
                if run.get("via") == "launcher"
                else ", as an exec tool's cleanup kills a runner the gate's "
                "command started (no launcher was running)"
            )
        )
    lived = _elapsed(run.get("alive_at", 0) - run.get("runner_at", 0))
    return (
        f"{gone}; it reached stage '{run['stage']}' and was last seen alive "
        f"{lived} after it started; killed outright (SIGKILL, the OOM killer, or "
        "a gateway restart), with no signal it could catch"
    )


def _give_up(d: Path, job: dict, reason: str, now: float) -> str:
    job.update(status=FAILED, result=f"gave up: {reason}"[:500], finished_at=now)
    _save(_path(d, job["id"]), job)
    return give_up_message(job, reason)


def _deliver(d: Path, job: dict, now: float) -> str:
    """Hand a turn the job's unread output, or give up after OFFER_LIMIT
    turns in a row did not read it."""
    offers = job.get("offers", 0) + 1
    if offers <= OFFER_LIMIT:
        job["offers"] = offers
        _save(_path(d, job["id"]), job)
        return ready_message(job)
    del job["run"]["unread"]
    _out(d, job["id"]).unlink(missing_ok=True)
    reason = f"{OFFER_LIMIT} drain turns in a row did not read its result"
    if job["status"] == RUNNING:
        return _give_up(d, job, reason, now)
    _save(_path(d, job["id"]), job)
    return give_up_message(job, reason)


def next_job(now: float | None = None) -> tuple[str, str]:
    """("ready", a turn's instructions), ("started", "") when it started a
    job, ("idle", "") or ("wait", "")."""
    now = time.time() if now is None else now
    with locked() as d:
        _prune(d, now)
        for job in sorted((j for _, j in _jobs(d)), key=lambda j: j["queued_at"]):
            if (job.get("run") or {}).get("unread"):
                return "ready", _deliver(d, job, now)
        ready = _runnable(d, now)
        for job in ready:
            if _stale(job, now) and job["attempts"] >= max_attempts():
                reason = _never_reported(d, job)
                return "ready", _give_up(d, job, reason, now)
    if not ready:
        return "idle", ""
    picked = _first_ready(ready)
    if picked is None:
        return "wait", ""
    with locked() as d:
        job = next((j for j in _runnable(d, now) if j["id"] == picked["id"]), None)
        if job is None:
            return "idle", ""
        if _launcher_alive(d):
            run = {"requested_at": now}
        else:
            run = {**_spawn(d, job["id"], now), "via": "gate"}
        _claim(job, now)
        job["run"] = run
        _save(_path(d, job["id"]), job)
    return "started", ""


def listing(now: float | None = None) -> str:
    now = time.time() if now is None else now
    with locked() as d:
        _prune(d, now)
        jobs = [j for _, j in _jobs(d)]
    if not jobs:
        return "the job queue is empty"
    lines = []
    for j in sorted(jobs, key=lambda j: j["queued_at"]):
        at = time.strftime("%Y-%m-%d %H:%M", time.gmtime(j["queued_at"]))
        line = f"{j['id']}  {j['status']}  queued {at} UTC  attempts {j['attempts']}"
        if j.get("label"):
            line += f"  {j['label']}"
        if j.get("result"):
            line += f"  - {j['result'].splitlines()[0][:120]}"
        lines.append(line)
    return "\n".join(lines)


def depth(now: float | None = None) -> dict:
    """How many jobs wait, run, and finished in the last 24 h: counts only, no
    job id, label or result, so the fleet publisher can show them anywhere.
    Read-only: it takes no lock and prunes nothing (each job file is replaced
    whole, so a read never sees half of one)."""
    now = time.time() if now is None else now
    jobs = [j for _, j in _jobs(root())]

    def finished(status: str) -> int:
        return sum(
            j["status"] == status and now - j.get("finished_at", 0) <= 86400
            for j in jobs
        )

    return {
        "schema": "jobq-depth.v1",
        "waiting": sum(j["status"] == QUEUED for j in jobs),
        "running": sum(j["status"] == RUNNING for j in jobs),
        "done_24h": finished(DONE),
        "failed_24h": finished(FAILED),
    }


def ages() -> dict:
    """Each job's state and times, with no label, result or payload: the fleet
    publisher's stalled check read this before `list --json` (`envelopes`), which
    carries the same. Read-only, like `depth`."""
    jobs = [j for _, j in _jobs(root())]
    return {
        "schema": "jobq-ages.v1",
        "max_attempts": max_attempts(),
        "jobs": [
            {
                "id": j["id"],
                "kind": j["kind"],
                "status": j["status"],
                "queued_at": j["queued_at"],
                "finished_at": j.get("finished_at"),
                "attempts": j["attempts"],
                "class": (j.get("envelope") or {}).get("class"),
            }
            for j in sorted(jobs, key=lambda j: j["queued_at"])
        ],
    }


def _since(job: dict) -> float:
    """When the job last changed state: its finish, its claim, or its return to
    the queue after a run (a retry or a release), else when it was queued."""
    if job["status"] in (DONE, FAILED):
        return job.get("finished_at") or job["queued_at"]
    if job["status"] == RUNNING:
        return job.get("claimed_at") or job["queued_at"]
    return max(job["queued_at"], (job.get("run") or {}).get("ran_at") or 0)


def _waiting(job: dict, others_running: bool) -> dict:
    """Why a queued job has not run yet: `waiting_on` names the thing it waits
    for, `waiting` says it in one line. A job with a probe host waits for the
    egress (by default the PC relay); one without waits for its turn."""
    if job["probe"]:
        via = "the PC relay" if egress() == DEFAULT_EGRESS else "the egress"
        return {
            "waiting_on": "egress",
            "waiting": f"waits for {via} to reach {job['probe']}",
        }
    if others_running:
        return {"waiting_on": "turn", "waiting": "waits for the running job to end"}
    return {"waiting_on": "turn", "waiting": "runs at the next drain"}


def envelopes(now: float | None = None) -> dict:
    """One item envelope per job (decision 0019's item record): `id` is
    `jobq:<job id>`, `store` jobq and `ref` the job id; `stage` the queue's own
    state and `state` its A2A name; `since` the last state change; and the
    envelope fields the job carries (`origin`, `owner`, `class`, `fingerprint`,
    `acceptance`, `thread`). `owner` is the envelope's, else the skill that
    answers the job. A queued job says what it waits on. Never the job's label,
    result or payload. Read-only, like `depth`: the claim stays `next`'s move."""
    now = time.time() if now is None else now
    keep = _int_env("JOBQ_KEEP_DAYS", DEFAULT_KEEP_DAYS) * 86400
    jobs = [
        j
        for _, j in _jobs(root())
        if j["status"] not in (DONE, FAILED) or now - j.get("finished_at", 0) <= keep
    ]
    others_running = any(j["status"] == RUNNING for j in jobs)
    out = []
    for j in sorted(jobs, key=lambda j: j["queued_at"]):
        env = j.get("envelope") or {}
        item = {
            "id": f"jobq:{j['id']}",
            "store": "jobq",
            "ref": j["id"],
            "kind": j["kind"],
            "stage": j["status"],
            "state": STATE_NAMES.get(j["status"], j["status"]),
            "since": _since(j),
            "queued_at": j["queued_at"],
            "finished_at": j.get("finished_at"),
            "attempts": j["attempts"],
            "owner": env.get("owner") or j["owner"],
        }
        for k in ("origin", "class", "fingerprint", "acceptance", "thread"):
            if env.get(k):
                item[k] = env[k]
        if j["status"] == QUEUED:
            item.update(_waiting(j, others_running))
        out.append(item)
    return {"schema": "jobq-list.v1", "max_attempts": max_attempts(), "jobs": out}


def drop(jid: str) -> bool:
    with locked() as d:
        p = _path(d, jid)
        existed = p.exists()
        p.unlink(missing_ok=True)
        _out(d, jid).unlink(missing_ok=True)
        _err(d, jid).unlink(missing_ok=True)
    return existed


def _enqueue(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="jobq.py enqueue", exit_on_error=False)
    ap.add_argument("kind")
    ap.add_argument("key")
    ap.add_argument("--skill", required=True, help="the skill that answers the owner")
    ap.add_argument("--handler", required=True, help="the handler command, a JSON list")
    ap.add_argument("--probe", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--payload", default="{}", help="a JSON object")
    ap.add_argument("--url", default="", help="what the receipt's url names")
    for f in ENVELOPE_FIELDS:
        ap.add_argument(f"--{f}", default=None)
    try:
        a = ap.parse_args(argv)
        handler = json.loads(a.handler)
        payload = json.loads(a.payload)
    except (argparse.ArgumentError, ValueError) as e:
        raise ValueError(f"enqueue: {e}") from e
    if not (
        isinstance(handler, list)
        and handler
        and all(isinstance(x, str) for x in handler)
    ):
        raise ValueError("enqueue: --handler must be a JSON list of strings")
    if not isinstance(payload, dict):
        raise ValueError("enqueue: --payload must be a JSON object")  # noqa: TRY004
    jid = job_id(a.kind, a.key)
    given = {f: getattr(a, f) for f in ENVELOPE_FIELDS if getattr(a, f)}
    envelope = (
        make_envelope(given, default_fingerprint=a.label or jid, default_thread=jid)
        if given
        else None
    )
    add(
        a.kind,
        a.key,
        owner=a.skill,
        handler=handler,
        probe=a.probe,
        payload=payload,
        label=a.label,
        quiet=a.quiet,
        envelope=envelope,
    )
    print(json.dumps(job_receipt(jid, a.url), separators=(",", ":")))
    return 0


def main(argv: list[str]) -> int:
    os.umask(0o077)
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "next" and len(argv) == 2:
            state, message = next_job()
            state = "idle" if state == "started" else state
            print(f"JOBQ {state}" + (f"\n{message}" if message else ""))
            return 0
        if cmd == "output" and len(argv) == 3:
            print(output(argv[2]))
            return 0
        if cmd == "launcher" and len(argv) == 2:
            return launcher()
        if cmd == "launcher-status" and len(argv) == 2:
            print(launcher_status())
            return 0
        if cmd == "runner" and len(argv) == 3:
            run_handler(argv[2])
            return 0
        if cmd == "list" and len(argv) == 2:
            print(listing())
            return 0
        if cmd == "list" and argv[2:] == ["--json"]:
            print(json.dumps(envelopes()))
            return 0
        if cmd == "depth" and len(argv) == 2:
            print(json.dumps(depth()))
            return 0
        if cmd == "ages" and len(argv) == 2:
            print(json.dumps(ages()))
            return 0
        if cmd == "enqueue":
            return _enqueue(argv[2:])
        if cmd == "status" and len(argv) == 3:
            r = job_receipt(argv[2])
            print(json.dumps(r, separators=(",", ":")) if r else f"no job {argv[2]}")
            return 0 if r else 1
        if cmd == "drop" and len(argv) == 3:
            print(f"dropped {argv[2]}" if drop(argv[2]) else f"no job {argv[2]}")
            return 0
    except (OSError, ValueError) as e:
        print(f"jobq failed: {e}", file=sys.stderr)
        return 1
    print(
        "usage: jobq.py next | output <id> | list [--json] | depth | ages | drop <id> | status <id>\n"
        "       jobq.py enqueue <kind> <key> --skill S --handler JSON [--probe H] [--label L]\n"
        "         [--quiet] [--payload JSON] [--url U] [--origin O --owner W --class C\n"
        "         --acceptance A [--fingerprint F] [--thread T]]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
