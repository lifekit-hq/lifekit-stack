#!/usr/bin/env python3
"""The shared job queue: work that waits for an egress to the outside world.

    jobq.py next        the drain gate's check (drain-gate.js): one job to run now?
    jobq.py list        what is waiting, running, and recently finished
    jobq.py drop <id>   remove one job

Some fetches cannot leave from this server (Instagram and YouTube refuse its
IP), so they go out through an egress: today the owner's PC, reached by the
yt-tunnel relay. A skill that needs one adds a job here and answers the owner
at once; a cron row on kit (job-queue-drain, docs/runbook.md "Shared job
queue") runs its gate every few minutes, and when the egress answers and
nothing else is running, the gate hands kit one job: the handler command the
producing skill stored, and the skill whose rules shape the reply. So a backlog
gathered while the PC was off drains, one job and one owner message at a
time, once it is on. The queue knows no kinds: a producer brings its own
handler and owner skill, and the handler reports back with claim / finish /
retry / release.

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
reports to the owner once. A running job whose handler never reported back is
taken up again after JOBQ_LEASE_MINUTES (default 30). The queue gives up on
its own, and the gate hands kit a one-line report for the owner instead of a
run, when a job used up its attempts that way, or when OFFER_LIMIT drain turns
in a row were handed it and none ran it, so no job loops or ends unreported.
Finished jobs stay JOBQ_KEEP_DAYS (default 14) for `list`, then are pruned.

`next` prints one line first, for the gate: "JOBQ ready" (the job's run
instructions follow), "JOBQ idle" (nothing to run, or one is running) or
"JOBQ wait" (jobs wait, the egress does not answer). Anything else is a
fault the gate surfaces.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_EGRESS = "socks5h://host.docker.internal:18081"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_MINUTES = 30
DEFAULT_KEEP_DAYS = 14
# Drain turns in a row that may be handed a job without running it.
OFFER_LIMIT = 3

KIND = re.compile(r"[a-z][a-z0-9-]{0,31}")
KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
LOCK = ".lock"


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


def _stale(job: dict, now: float) -> bool:
    lease = _int_env("JOBQ_LEASE_MINUTES", DEFAULT_LEASE_MINUTES) * 60
    return job["status"] == RUNNING and now - job.get("claimed_at", 0) > lease


def _prune(d: Path, now: float) -> None:
    keep = _int_env("JOBQ_KEEP_DAYS", DEFAULT_KEEP_DAYS) * 86400
    for p, job in _jobs(d):
        if job["status"] in (DONE, FAILED) and now - job.get("finished_at", 0) > keep:
            p.unlink(missing_ok=True)


def add(
    kind: str,
    key: str,
    *,
    owner: str,
    handler: list[str],
    probe: str,
    payload: dict | None = None,
    label: str = "",
    now: float | None = None,
) -> tuple[bool, int]:
    """Queue a job. Returns (added, jobs waiting): added is False when the
    same job is already queued or running. A finished one is queued again.
    The label (such as the link the owner sent) names the job to the owner."""
    now = time.time() if now is None else now
    if not HOST.fullmatch(probe):
        raise ValueError(f"bad probe host: {probe!r}")
    jid = job_id(kind, key)
    with locked() as d:
        _prune(d, now)
        p = _path(d, jid)
        old = _load(p)
        added = old is None or old["status"] in (DONE, FAILED)
        if added:
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


def _next(d: Path, now: float) -> dict | None:
    """The oldest runnable job, or None while another is running: one at a
    time, so a backlog never runs several downloads and transcriptions at once."""
    jobs = [j for _, j in _jobs(d)]
    if any(j["status"] == RUNNING and not _stale(j, now) for j in jobs):
        return None
    ready = [j for j in jobs if j["status"] == QUEUED or _stale(j, now)]
    return min(ready, key=lambda j: j["queued_at"], default=None)


def claim(jid: str, now: float | None = None) -> dict | None:
    """Mark the job running and count the attempt. None when it is not the
    job to run (gone, finished, or another one is running)."""
    now = time.time() if now is None else now
    with locked() as d:
        job = _next(d, now)
        if job is None or job["id"] != jid:
            return None
        job.update(
            status=RUNNING, claimed_at=now, attempts=job["attempts"] + 1, offers=0
        )
        _save(_path(d, jid), job)
        return job


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


def run_message(job: dict) -> str:
    since = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(job["queued_at"]))
    return (
        f"Job {job['id']} is ready (attempt {job['attempts'] + 1} of "
        f"{max_attempts()}, queued {since}).\n"
        f"Run exactly: {shlex.join([*job['handler'], job['id']])}\n"
        f"Then answer as the {job['owner']} skill says for a queued job."
    )


def give_up_message(job: dict, reason: str) -> str:
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


def next_job(now: float | None = None) -> tuple[str, str]:
    now = time.time() if now is None else now
    with locked() as d:
        _prune(d, now)
        job = _next(d, now)
        if job is not None and _stale(job, now) and job["attempts"] >= max_attempts():
            reason = f"its last {job['attempts']} runs never reported back"
            return "ready", _give_up(d, job, reason, now)
    if job is None:
        return "idle", ""
    if not egress_ready(job["probe"]):
        return "wait", ""
    with locked() as d:
        job = _next(d, now)
        if job is None:
            return "idle", ""
        offers = job.get("offers", 0) + 1
        if offers > OFFER_LIMIT:
            reason = f"{OFFER_LIMIT} drain turns in a row did not run it"
            return "ready", _give_up(d, job, reason, now)
        job["offers"] = offers
        _save(_path(d, job["id"]), job)
    return "ready", run_message(job)


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


def drop(jid: str) -> bool:
    with locked() as d:
        p = _path(d, jid)
        existed = p.exists()
        p.unlink(missing_ok=True)
    return existed


def main(argv: list[str]) -> int:
    os.umask(0o077)
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "next" and len(argv) == 2:
            state, message = next_job()
            print(f"JOBQ {state}" + (f"\n{message}" if message else ""))
            return 0
        if cmd == "list" and len(argv) == 2:
            print(listing())
            return 0
        if cmd == "drop" and len(argv) == 3:
            print(f"dropped {argv[2]}" if drop(argv[2]) else f"no job {argv[2]}")
            return 0
    except (OSError, ValueError) as e:
        print(f"jobq failed: {e}", file=sys.stderr)
        return 1
    print("usage: jobq.py next | list | drop <id>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
