#!/usr/bin/env python3
"""The shared job queue: work that waits for an egress to the outside world.

    jobq.py next        the drain gate's check (drain-gate.js): start a job, or
                        hand an agent turn one that has run?
    jobq.py output <id> what the job's run printed, for the turn that answers it
    jobq.py list        what is waiting, running, and recently finished
    jobq.py depth       counts only, as JSON, for the fleet status page
    jobq.py drop <id>   remove one job
    jobq.py runner <id> the run itself, started by `next`, never by hand

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
reports to the owner once. The run is a detached runner process (its own
session, so it outlives the gate's command), bounded by JOBQ_RUN_MINUTES
(default 90); its output waits in <id>.out (0600) until a turn reads it. A
running job nobody will report back for is taken up again at once when its
runner died (a gateway restart) or its handler ended leaving nothing for a
turn, and JOBQ_LEASE_MINUTES (default 30) after a turn read its output without
finishing it. The queue gives up on its own, and the gate hands kit a
one-line report for the owner instead of the output, when a job used up its
attempts that way, or when OFFER_LIMIT drain turns in a row were handed its
output and none read it, so no job loops or ends unreported.
A quiet job (one filed by an agent that reads its status, not by the owner)
is given up with NO_REPLY instead: its producer polls the job.
Finished jobs stay JOBQ_KEEP_DAYS (default 14) for `list`, then are pruned.

`next` prints one line first, for the gate: "JOBQ ready" (a turn's
instructions follow), "JOBQ idle" (nothing to answer: nothing queued, one
running, or one just started) or "JOBQ wait" (jobs wait, the egress does not
answer). Anything else is a fault the gate surfaces.
"""

from __future__ import annotations

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
NO_REPLY = "NO_REPLY"
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
    now: float | None = None,
) -> tuple[bool, int]:
    """Queue a job. Returns (added, jobs waiting): added is False when the
    same job is already queued or running. A finished one is queued again.
    The label (such as the link the owner sent) names the job to the owner.
    An empty probe means the job needs no egress; a quiet job's give-up says
    nothing to the owner."""
    now = time.time() if now is None else now
    if probe and not HOST.fullmatch(probe):
        raise ValueError(f"bad probe host: {probe!r}")
    jid = job_id(kind, key)
    with locked() as d:
        _prune(d, now)
        p = _path(d, jid)
        old = _load(p)
        added = old is None or old["status"] in (DONE, FAILED)
        if added:
            _out(d, jid).unlink(missing_ok=True)
            _save(
                p,
                {
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
                },
            )
        waiting = sum(1 for _, j in _jobs(d) if j["status"] in (QUEUED, RUNNING))
    return added, waiting


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


def _spawn(jid: str, now: float) -> dict:
    """Start the job's runner in a session of its own with its stdio closed,
    so it outlives the gate's command and a tool that kills that command's
    process group or children."""
    p = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "runner", jid],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"pid": p.pid, "start": _proc_start(p.pid), "started_at": now}


def _mine(job: dict | None) -> bool:
    return job is not None and (job.get("run") or {}).get("pid") == os.getpid()


def run_handler(jid: str) -> None:
    """The runner: run the job's handler to its end (at most JOBQ_RUN_MINUTES),
    keep what it printed for the turn that answers it, and note that it ran.
    It records under the lock `next` holds while it starts it, so the job
    names this runner by then; a runner the job no longer names records
    nothing."""
    with locked() as d:
        job = _load(_path(d, jid))
    if not _mine(job):
        return
    out = _out(d, jid)
    limit = _int_env("JOBQ_RUN_MINUTES", DEFAULT_RUN_MINUTES) * 60
    try:
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
            try:
                err = proc.communicate(timeout=limit)[1]
                code = proc.returncode
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                code, err = None, f"ran over {limit // 60} minutes".encode()
    except OSError as e:
        code, err = None, str(e).encode()
    text = out.read_text(errors="replace").strip() if code == 0 else ""
    with locked() as d:
        p = _path(d, jid)
        job = _load(p)
        if not _mine(job):
            return
        job["run"].update(ran_at=time.time(), exit=code)
        job["offers"] = 0
        if text and text != NO_REPLY:
            job["run"]["unread"] = True
        else:
            out.unlink(missing_ok=True)
        if code != 0:
            lines = err.decode(errors="replace").strip().splitlines()
            job["last_error"] = (lines[-1] if lines else f"exit {code}")[:500]
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


def _give_up(d: Path, job: dict, reason: str, now: float) -> str:
    job.update(status=FAILED, result=f"gave up: {reason}", finished_at=now)
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
                reason = f"its last {job['attempts']} runs never reported back"
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
        run = _spawn(job["id"], now)
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


def drop(jid: str) -> bool:
    with locked() as d:
        p = _path(d, jid)
        existed = p.exists()
        p.unlink(missing_ok=True)
        _out(d, jid).unlink(missing_ok=True)
    return existed


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
        if cmd == "runner" and len(argv) == 3:
            run_handler(argv[2])
            return 0
        if cmd == "list" and len(argv) == 2:
            print(listing())
            return 0
        if cmd == "depth" and len(argv) == 2:
            print(json.dumps(depth()))
            return 0
        if cmd == "drop" and len(argv) == 3:
            print(f"dropped {argv[2]}" if drop(argv[2]) else f"no job {argv[2]}")
            return 0
    except (OSError, ValueError) as e:
        print(f"jobq failed: {e}", file=sys.stderr)
        return 1
    print(
        "usage: jobq.py next | output <id> | list | depth | drop <id>", file=sys.stderr
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
