"""The shared job queue, its drain gate and its ensure script (docs/runbook.md,
"Shared job queue").

The egress is a fake SOCKS5 or HTTP proxy on loopback, so nothing here leaves
the machine.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills/job-queue"
ENSURE = REPO / "scripts/ensure-job-queue.sh"

_SPEC = importlib.util.spec_from_file_location("jobq", SKILL / "jobq.py")
jobq = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(jobq)

HANDLER = ["python3", "/skills/x/x.py", "run"]


@pytest.fixture
def q(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBQ_DIR", str(tmp_path / "queue"))
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", "direct")
    for k in ("JOBQ_MAX_ATTEMPTS", "JOBQ_LEASE_MINUTES", "JOBQ_KEEP_DAYS"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path / "queue"


def _add(key="AAA111", now=1000.0, **kw):
    return jobq.add(
        "reel",
        key,
        owner="video-eval",
        handler=HANDLER,
        probe="www.instagram.com",
        payload={"url": f"https://example/{key}"},
        label=f"https://example/{key}",
        now=now,
        **kw,
    )


# --- adding ---------------------------------------------------------------------


def test_add_keeps_one_private_file_per_job(q):
    assert _add() == (True, 1)
    assert stat.S_IMODE(q.stat().st_mode) == 0o700
    files = sorted(p.name for p in q.iterdir())
    assert files == [".lock", "reel-AAA111.json"]
    for name in files:
        assert stat.S_IMODE((q / name).stat().st_mode) == 0o600
    job = jobq.get("reel-AAA111")
    assert job["status"] == "queued" and job["attempts"] == 0
    assert job["handler"] == HANDLER and job["owner"] == "video-eval"


def test_a_second_add_while_it_waits_is_already_queued(q):
    _add()
    assert _add(now=1001.0) == (False, 1)
    assert _add("BBB222", now=1002.0) == (True, 2)


def test_a_finished_job_is_queued_again_on_a_new_add(q):
    _add()
    jobq.claim("reel-AAA111", now=1001.0)
    jobq.finish("reel-AAA111", "Reel: x", now=1002.0)
    assert _add(now=1003.0) == (True, 1)
    assert jobq.get("reel-AAA111")["status"] == "queued"


@pytest.mark.parametrize(
    "kind,key,probe",
    [
        ("Reel", "AAA111", "www.instagram.com"),
        ("reel", "../etc", "www.instagram.com"),
        ("reel", "a b", "www.instagram.com"),
        ("reel", "AAA111", "www.instagram.com:443"),
        ("reel", "AAA111", "evil.com/x"),
    ],
)
def test_add_refuses_bad_kinds_keys_and_probes(q, kind, key, probe):
    with pytest.raises(ValueError):
        jobq.add(kind, key, owner="o", handler=HANDLER, probe=probe)


def test_a_bad_job_id_never_names_a_path(q):
    for bad in ("../x", "reel-../../x", "reel-a/b", ""):
        with pytest.raises(ValueError):
            jobq.get(bad)


# --- running ----------------------------------------------------------------------


def test_jobs_run_oldest_first_and_one_at_a_time(q):
    _add("BBB222", now=1001.0)
    _add("AAA111", now=1000.0)
    assert jobq.claim("reel-BBB222", now=1002.0) is None  # not its turn
    job = jobq.claim("reel-AAA111", now=1002.0)
    assert job["status"] == "running" and job["attempts"] == 1
    assert jobq.claim("reel-BBB222", now=1003.0) is None  # one is running
    jobq.finish("reel-AAA111", "Reel: a\nmore", now=1004.0)
    assert jobq.claim("reel-BBB222", now=1005.0)["attempts"] == 1


def test_a_failed_run_is_retried_then_failed_after_the_last_attempt(q, monkeypatch):
    monkeypatch.setenv("JOBQ_MAX_ATTEMPTS", "2")
    _add()
    jobq.claim("reel-AAA111", now=1001.0)
    assert jobq.retry("reel-AAA111", "boom 1", now=1002.0) is False
    job = jobq.get("reel-AAA111")
    assert (job["status"], job["last_error"]) == ("queued", "boom 1")
    jobq.claim("reel-AAA111", now=1003.0)
    assert jobq.retry("reel-AAA111", "boom 2", now=1004.0) is True
    job = jobq.get("reel-AAA111")
    assert (job["status"], job["result"]) == ("failed", "boom 2")
    assert jobq.claim("reel-AAA111", now=1005.0) is None


def test_release_puts_it_back_without_counting_the_attempt(q):
    _add()
    jobq.claim("reel-AAA111", now=1001.0)
    jobq.release("reel-AAA111")
    job = jobq.get("reel-AAA111")
    assert (job["status"], job["attempts"]) == ("queued", 0)


def test_a_running_job_that_never_reported_back_is_taken_up_after_the_lease(q):
    _add("AAA111", now=1000.0)
    _add("BBB222", now=1001.0)
    jobq.claim("reel-AAA111", now=1002.0)
    assert jobq.claim("reel-AAA111", now=1002.0 + 29 * 60) is None
    job = jobq.claim("reel-AAA111", now=1002.0 + 31 * 60)
    assert job["attempts"] == 2


def test_finished_jobs_are_pruned_after_the_keep_days(q):
    _add()
    jobq.claim("reel-AAA111", now=1001.0)
    jobq.finish("reel-AAA111", "Reel: x", now=1002.0)
    assert "reel-AAA111" in jobq.listing(now=1002.0 + 13 * 86400)
    assert jobq.listing(now=1002.0 + 15 * 86400) == "the job queue is empty"


def test_listing_names_each_job_with_its_label_and_result(q):
    _add()
    jobq.claim("reel-AAA111", now=1001.0)
    jobq.finish("reel-AAA111", "Reel: https://example/AAA111\nAccount: a", now=1002.0)
    line = jobq.listing(now=1003.0)
    assert line.startswith("reel-AAA111  done  queued 1970-01-01 00:16 UTC  attempts 1")
    assert "https://example/AAA111" in line and "Account" not in line


def test_drop_removes_one_job(q):
    _add()
    assert jobq.drop("reel-AAA111") is True
    assert jobq.drop("reel-AAA111") is False
    assert jobq.get("reel-AAA111") is None


# --- next: what the gate sees ------------------------------------------------------------


def test_next_is_idle_with_nothing_queued_or_one_running(q):
    assert jobq.next_job(now=1000.0) == ("idle", "")
    _add()
    jobq.claim("reel-AAA111", now=1001.0)
    _add("BBB222", now=1002.0)
    assert jobq.next_job(now=1003.0) == ("idle", "")


def test_next_hands_over_the_exact_handler_command(q):
    _add()
    state, message = jobq.next_job(now=1001.0)
    assert state == "ready"
    assert message.splitlines() == [
        "Job reel-AAA111 is ready (attempt 1 of 3, queued 1970-01-01 00:16 UTC).",
        "Run exactly: python3 /skills/x/x.py run reel-AAA111",
        "Then answer as the video-eval skill says for a queued job.",
    ]


def test_next_waits_while_the_egress_does_not_answer(q, monkeypatch):
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", f"socks5h://127.0.0.1:{_closed_port()}")
    _add()
    assert jobq.next_job(now=1001.0) == ("wait", "")
    assert jobq.get("reel-AAA111").get("offers") is None  # a wait is no offer


def test_a_job_with_no_probe_needs_no_egress(q, monkeypatch):
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", f"socks5h://127.0.0.1:{_closed_port()}")
    jobq.add("video", "file-AAA111", owner="video-eval", handler=HANDLER, probe="")
    assert jobq.next_job(now=1001.0)[0] == "ready"
    assert jobq.get("video-file-AAA111")["probe"] == ""


def test_a_quiet_job_gives_up_without_a_reply(q):
    _add(quiet=True)
    for _ in range(jobq.OFFER_LIMIT):
        jobq.next_job(now=1001.0)
    state, message = jobq.next_job(now=1002.0)
    assert state == "ready" and message.splitlines()[1] == (
        "Run nothing. Reply exactly NO_REPLY: whoever queued it reads its status."
    )
    assert jobq.get("reel-AAA111")["status"] == "failed"


def test_next_gives_up_after_offers_no_turn_ran(q):
    _add()
    for _ in range(jobq.OFFER_LIMIT):
        assert jobq.next_job(now=1001.0)[0] == "ready"
    state, message = jobq.next_job(now=1002.0)
    assert state == "ready"
    assert message.startswith(
        "Job reel-AAA111 gave up: 3 drain turns in a row did not run it."
    )
    assert "Run nothing." in message and "https://example/AAA111" in message
    job = jobq.get("reel-AAA111")
    assert job["status"] == "failed" and job["result"].startswith("gave up")
    assert jobq.next_job(now=1003.0) == ("idle", "")


def test_a_claim_resets_the_offer_count(q):
    _add()
    for _ in range(jobq.OFFER_LIMIT):
        jobq.next_job(now=1001.0)
    jobq.claim("reel-AAA111", now=1002.0)
    jobq.release("reel-AAA111")
    assert jobq.next_job(now=1003.0)[1].startswith("Job reel-AAA111 is ready")


def test_next_gives_up_on_a_job_whose_last_runs_never_reported_back(q, monkeypatch):
    monkeypatch.setenv("JOBQ_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", f"socks5h://127.0.0.1:{_closed_port()}")
    _add()
    jobq.claim("reel-AAA111", now=1001.0)
    jobq.claim("reel-AAA111", now=1001.0 + 31 * 60)
    state, message = jobq.next_job(now=1001.0 + 62 * 60)  # needs no egress to report
    assert state == "ready"
    assert "gave up: its last 2 runs never reported back" in message
    assert jobq.get("reel-AAA111")["status"] == "failed"


def _cli(*args, env):
    return subprocess.run(
        [sys.executable, str(SKILL / "jobq.py"), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_cli_prints_the_gate_sentinel_first(q):
    env = {**os.environ}
    assert _cli("next", env=env).stdout == "JOBQ idle\n"
    _add()
    out = _cli("next", env=env).stdout.splitlines()
    assert out[0] == "JOBQ ready" and out[2].startswith("Run exactly: ")
    env["LIFEKIT_EGRESS_PROXY"] = f"socks5h://127.0.0.1:{_closed_port()}"
    assert _cli("next", env=env).stdout == "JOBQ wait\n"


def test_the_cli_fails_loudly(q):
    env = {**os.environ, "LIFEKIT_EGRESS_PROXY": "ftp://x:1"}
    _add()
    r = _cli("next", env=env)
    assert r.returncode == 1 and r.stderr.startswith(
        "jobq failed: LIFEKIT_EGRESS_PROXY"
    )
    assert _cli("drop", "../x", env=env).returncode == 1
    assert _cli("bogus", env=env).returncode == 2


# --- the egress -----------------------------------------------------------------------------


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeProxy:
    """One-connection-at-a-time SOCKS5 or HTTP CONNECT proxy on loopback. It
    answers `reply` and, on success, echoes what the client sends."""

    def __init__(self, kind: str, reply):
        self.kind, self.reply, self.asked = kind, reply, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return (
            f"{'socks5h' if self.kind == 'socks' else 'http'}://127.0.0.1:{self.port}"
        )

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                try:
                    ok = self._socks(c) if self.kind == "socks" else self._http(c)
                    while ok and (data := c.recv(64)):
                        c.sendall(data)
                except OSError:
                    pass

    def _socks(self, c) -> bool:
        assert c.recv(3) == b"\x05\x01\x00"
        c.sendall(b"\x05\x00")
        head = c.recv(5)
        name = c.recv(head[4])
        port = int.from_bytes(c.recv(2), "big")
        self.asked.append((head[:4], name.decode(), port))
        c.sendall(bytes([5, self.reply, 0, 1, 0, 0, 0, 0, 0, 0]))
        return self.reply == 0

    def _http(self, c) -> bool:
        head = b""
        while b"\r\n\r\n" not in head:
            head += c.recv(1)
        self.asked.append(head.split(b"\r\n")[0].decode())
        c.sendall(f"HTTP/1.1 {self.reply} X\r\nVia: fake\r\n\r\n".encode())
        return self.reply == 200

    def close(self):
        self.sock.close()


@pytest.fixture
def proxy():
    made = []

    def make(kind, reply):
        made.append(FakeProxy(kind, reply))
        return made[-1]

    yield make
    for p in made:
        p.close()


@pytest.mark.parametrize(
    "kind,reply,ready",
    [
        ("socks", 0x00, True),
        ("socks", 0x02, True),  # an allow-list refusal: run it and fail loudly
        ("socks", 0x05, False),  # the relay is up, what is behind it is not
        ("socks", 0x01, False),
        ("http", 200, True),
        ("http", 403, True),
        ("http", 502, False),
    ],
)
def test_egress_ready_is_a_real_connect_through_the_proxy(proxy, kind, reply, ready):
    p = proxy(kind, reply)
    assert jobq.egress_ready("www.instagram.com", p.url, timeout=5) is ready
    if kind == "socks":
        assert p.asked == [(b"\x05\x01\x00\x03", "www.instagram.com", 443)]
    else:
        assert p.asked == ["CONNECT www.instagram.com:443 HTTP/1.1"]


def test_egress_ready_is_false_with_no_proxy_listening():
    assert jobq.egress_ready("x.com", f"socks5h://127.0.0.1:{_closed_port()}") is False
    assert jobq.egress_ready("x.com", f"http://127.0.0.1:{_closed_port()}") is False


def test_direct_egress_is_always_ready_and_hands_no_proxy(monkeypatch):
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", "direct")
    assert jobq.egress_ready("x.com") is True and jobq.egress_proxy() is None


def test_the_default_egress_is_the_pc_relay(monkeypatch):
    monkeypatch.delenv("LIFEKIT_EGRESS_PROXY", raising=False)
    assert jobq.egress_proxy() == "socks5h://host.docker.internal:18081"
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", " ")
    assert jobq.egress_proxy() == "socks5h://host.docker.internal:18081"


@pytest.mark.parametrize("kind,reply", [("socks", 0), ("http", 200)])
def test_open_connection_carries_bytes_through_the_proxy(proxy, kind, reply):
    p = proxy(kind, reply)
    with jobq.open_connection("www.instagram.com", 443, p.url, 5) as s:
        s.sendall(b"hello")
        assert s.recv(5) == b"hello"


def test_open_connection_reports_a_refusal(proxy):
    p = proxy("socks", 0x02)
    with pytest.raises(jobq.EgressError) as e:
        jobq.open_connection("www.instagram.com", 443, p.url, 5)
    assert e.value.refused and "0x02" in str(e.value)


# --- the drain gate ----------------------------------------------------------------------------

GATE_RUNNER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const out = process.argv[2];
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const exec = async ({ command }) => {
  process.stderr.write(command + "\n");
  return { aggregated: out };
};
new AsyncFunction("exec", src)(exec).then(
  (r) => process.stdout.write(JSON.stringify(r)),
  (e) => { process.stdout.write("THROW " + e.message); },
);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize(
    "out,want",
    [
        ("JOBQ idle\n", {"fire": False}),
        ("JOBQ wait\n", {"fire": False}),
        (
            "JOBQ ready\nJob a\nRun exactly: x\n",
            {"fire": True, "message": "Job a\nRun exactly: x"},
        ),
        ("JOBQ ready\n", "THROW jobq.py next: JOBQ ready"),
        ("jobq failed: boom\n", "THROW jobq.py next: jobq failed: boom"),
        ("", "THROW jobq.py next: no output"),
    ],
)
def test_the_gate_fires_only_on_a_ready_job_and_throws_on_anything_odd(out, want):
    r = subprocess.run(
        ["node", "-e", GATE_RUNNER, str(SKILL / "drain-gate.js"), out],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (
        r.stderr.strip() == "python3 /home/node/.openclaw/skills/job-queue/jobq.py next"
    )
    assert (json.loads(r.stdout) if r.stdout.startswith("{") else r.stdout) == want


# --- the ensure script ---------------------------------------------------------------------------

DOCKER = """#!/bin/sh
case "$1" in
  ps) [ -n "$GATEWAY" ] && echo "$GATEWAY" ;;
  exec)
    case "$*" in
      *"cron list"*) echo '{"jobs":[]}' ;;
      *node*) cat >/dev/null; printf %s "$CRON_ROW" ;;
    esac ;;
esac
exit 0
"""


def _ensure(tmp_path, *, gateway="", row="missing"):
    state = tmp_path / "state"
    bindir = tmp_path / "dbin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(DOCKER)
    (bindir / "docker").chmod(0o755)
    r = subprocess.run(
        ["bash", str(ENSURE)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "OPENCLAW_CONFIG_DIR": str(state),
            "GATEWAY": gateway,
            "CRON_ROW": row,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return r, state / "skills/job-queue"


def test_ensure_installs_the_queue_into_the_shared_skills_dir(tmp_path):
    r, dest = _ensure(tmp_path)
    assert r.returncode == 0, r.stderr
    for name in ("SKILL.md", "jobq.py", "drain-gate.js"):
        assert (dest / name).read_bytes() == (SKILL / name).read_bytes()
    assert os.access(dest / "jobq.py", os.X_OK)
    assert "drain cron not checked" in r.stdout


def test_ensure_converges_on_a_rerun(tmp_path):
    _, dest = _ensure(tmp_path)
    (dest / "drain-gate.js").write_text("stale\n")
    r, dest = _ensure(tmp_path)
    assert r.returncode == 0
    assert (dest / "drain-gate.js").read_bytes() == (
        SKILL / "drain-gate.js"
    ).read_bytes()


@pytest.mark.parametrize(
    "row,said",
    [
        ("missing", "'job-queue-drain' is not set up - nothing drains the queue"),
        ("enabled same", "'job-queue-drain' is enabled and runs the shipped gate"),
        ("enabled differs", "runs a gate that DIFFERS from drain-gate.js"),
        ("disabled same", "'job-queue-drain' is disabled - nothing drains the queue"),
        ("", "could not read the gateway's cron list"),
    ],
)
def test_ensure_reports_the_drain_row_without_touching_it(tmp_path, row, said):
    r, _ = _ensure(tmp_path, gateway="abc123", row=row or "unknown")
    assert r.returncode == 0, r.stderr
    assert said in r.stdout


def test_the_skill_frontmatter_parses():
    yaml = pytest.importorskip("yaml")
    meta = yaml.safe_load((SKILL / "SKILL.md").read_text().split("---")[1])
    assert meta["name"] == "job-queue"
    assert meta["metadata"]["openclaw"]["requires"]["bins"] == ["python3"]
