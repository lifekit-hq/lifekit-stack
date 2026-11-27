"""The shared job queue, its drain gate and its ensure script (docs/runbook.md,
"Shared job queue").

The egress is a fake SOCKS5 or HTTP proxy on loopback, so nothing here leaves
the machine. The handler the queue's runner starts is a stand-in that sleeps
FAKE_SLEEP seconds, then does what FAKE_MODE says.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills/job-queue"
ENSURE = REPO / "scripts/ensure-job-queue.sh"

_SPEC = importlib.util.spec_from_file_location("jobq", SKILL / "jobq.py")
jobq = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(jobq)

FAKE_HANDLER = r"""
import os, signal, sys, time
sys.path.insert(0, os.environ["FAKE_LIB"])
import jobq
jid = sys.argv[-1]
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
mode = os.environ.get("FAKE_MODE", "material")
if jobq.running(jid) is None:
    print("NO_REPLY")
elif mode == "material":
    print("material for " + jid)
elif mode == "answer":
    jobq.finish(jid, "answered")
    print("the answer for " + jid)
elif mode == "retry":
    jobq.retry(jid, "boom")
    print("NO_REPLY")
elif mode == "crash":
    sys.exit("Traceback: boom")
elif mode == "noisy":
    sys.stderr.write("warming up\nTraceback (most recent call last):\nKeyError: 'x'\n")
    sys.exit(3)
elif mode == "oom":
    os.kill(os.getpid(), signal.SIGKILL)
elif mode == "silent":
    pass
"""
HANDLER = [sys.executable, "-c", FAKE_HANDLER, "run"]


@pytest.fixture
def q(tmp_path, monkeypatch):
    qdir = tmp_path / "queue"
    monkeypatch.setenv("JOBQ_DIR", str(qdir))
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", "direct")
    monkeypatch.setenv("FAKE_LIB", str(SKILL))
    for k in (
        "JOBQ_MAX_ATTEMPTS",
        "JOBQ_LEASE_MINUTES",
        "JOBQ_KEEP_DAYS",
        "JOBQ_RUN_MINUTES",
        "FAKE_SLEEP",
        "FAKE_MODE",
    ):
        monkeypatch.delenv(k, raising=False)
    yield qdir
    _kill_runs(qdir)


def _kill_runs(qdir: Path) -> None:
    """Every runner and handler a test left behind (they outlive their parent
    by design), found by the queue directory in their environment."""
    mark = f"JOBQ_DIR={qdir}".encode()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if mark in (proc / "environ").read_bytes().split(b"\0"):
                os.kill(int(proc.name), signal.SIGKILL)
        except OSError:
            pass


def _ran(jid: str, timeout: float = 30.0) -> dict:
    """The job once its runner has noted the handler's end."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobq.get(jid)
        if "ran_at" in (job.get("run") or {}):
            return job
        time.sleep(0.05)
    raise AssertionError(f"{jid} never ran: {jobq.get(jid)}")


def _add_file(key="AAA111", now=1000.0, **kw):
    """A job that needs no egress, like a file already on the host."""
    return jobq.add(
        "video",
        f"file-{key}",
        owner="video-eval",
        handler=HANDLER,
        probe="",
        now=now,
        **kw,
    )


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


def test_a_jobs_position_puts_the_running_one_first_then_the_oldest(q, monkeypatch):
    # The egress is down, so a file (no probe) runs ahead of an older link.
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", f"socks5h://127.0.0.1:{_closed_port()}")
    _add("AAA111", now=1000.0)
    jobq.add("reel", "BBB222", owner="o", handler=HANDLER, probe="", now=1001.0)
    _add("CCC333", now=1002.0)
    assert jobq.position("reel-AAA111") == (1, 3)
    assert jobq.position("reel-CCC333") == (3, 3)
    assert jobq.claim("reel-BBB222", now=1003.0) is not None
    assert jobq.position("reel-BBB222") == (1, 3)
    assert jobq.position("reel-AAA111") == (2, 3)
    jobq.finish("reel-BBB222", "Reel: x", now=1004.0)
    assert jobq.position("reel-BBB222") is None
    assert jobq.position("reel-CCC333") == (2, 2)
    assert jobq.position("reel-NOPE00") is None


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


def test_depth_counts_jobs_by_state_and_names_none(q):
    for key in ("AAA111", "BBB222", "CCC333", "DDD444"):
        _add(key, now=1000.0)
    jobq.claim("reel-AAA111", now=1001.0)
    jobq.claim("reel-BBB222", now=1001.0)
    jobq.finish("reel-BBB222", "Reel: https://example/BBB222", now=1002.0)
    jobq.claim("reel-CCC333", now=1001.0)
    jobq.finish("reel-CCC333", "Reel: old", now=1002.0 - 86400 - 1)
    got = jobq.depth(now=1003.0)
    assert got == {
        "schema": "jobq-depth.v1",
        "waiting": 1,
        "running": 1,
        "done_24h": 1,
        "failed_24h": 0,
    }
    # Counts only: no id, label or result leaves the queue.
    assert "example" not in json.dumps(got) and "reel-" not in json.dumps(got)
    out = subprocess.run(
        [sys.executable, str(SKILL / "jobq.py"), "depth"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out)["waiting"] == 1


def test_ages_gives_states_and_times_and_no_label_or_result(q):
    _add("AAA111", now=1000.0)
    _add("BBB222", now=1001.0)
    jobq.claim("reel-BBB222", now=1002.0)
    jobq.finish("reel-BBB222", "Reel: https://example/BBB222", now=1003.0)
    got = jobq.ages()
    assert got["schema"] == "jobq-ages.v1" and got["max_attempts"] == 3
    assert [(j["id"], j["status"]) for j in got["jobs"]] == [
        ("reel-AAA111", "queued"),
        ("reel-BBB222", "done"),
    ]
    assert got["jobs"][0]["attempts"] == 0
    assert got["jobs"][1]["finished_at"] == 1003.0 and got["jobs"][0]["class"] is None
    assert "example" not in json.dumps(got)
    out = subprocess.run(
        [sys.executable, str(SKILL / "jobq.py"), "ages"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert len(json.loads(out)["jobs"]) == 2


LIST_ENVELOPE = {
    "origin": "kit",
    "owner": "kit",
    "class": "life",
    "acceptance": "the reel is judged",
    "fingerprint": "https://example/AAA111",
    "thread": "reel-AAA111",
}


def test_list_json_gives_one_envelope_per_job_with_its_fields(q, monkeypatch):
    monkeypatch.delenv("LIFEKIT_EGRESS_PROXY")  # the default egress: the PC relay
    _add("AAA111", now=1000.0, envelope=LIST_ENVELOPE)
    _add_file("BBB222", now=1001.0)
    got = jobq.envelopes(now=1002.0)
    assert got["schema"] == "jobq-list.v1" and got["max_attempts"] == 3
    reel, file = got["jobs"]
    assert reel == {
        "id": "jobq:reel-AAA111",
        "store": "jobq",
        "ref": "reel-AAA111",
        "kind": "reel",
        "stage": "queued",
        "state": "submitted",
        "since": 1000.0,
        "queued_at": 1000.0,
        "finished_at": None,
        "attempts": 0,
        "origin": "kit",
        "owner": "kit",
        "class": "life",
        "fingerprint": "https://example/AAA111",
        "acceptance": "the reel is judged",
        "thread": "reel-AAA111",
        "waiting_on": "egress",
        "waiting": "waits for the PC relay to reach www.instagram.com",
    }
    # No envelope: the skill that answers it owns it, and no field is invented.
    assert file["id"] == "jobq:video-file-BBB222" and file["owner"] == "video-eval"
    assert not {"origin", "class", "fingerprint", "acceptance"} & set(file)
    assert (file["waiting_on"], file["waiting"]) == ("turn", "runs at the next drain")
    # Never the job's label, payload or handler.
    assert "label" not in reel and "payload" not in reel and "handler" not in reel


def test_list_json_of_an_empty_queue_is_an_empty_list(q):
    got = jobq.envelopes(now=1000.0)
    assert got == {"schema": "jobq-list.v1", "max_attempts": 3, "jobs": []}
    out = subprocess.run(
        [sys.executable, str(SKILL / "jobq.py"), "list", "--json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out)["jobs"] == []


def test_list_json_follows_a_job_through_its_attempts(q, monkeypatch):
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", "socks5h://relay.example:1080")
    _add("AAA111", now=1000.0, envelope=LIST_ENVELOPE)
    _add_file("BBB222", now=1001.0)
    jobq.claim("reel-AAA111", now=1002.0)
    running, other = jobq.envelopes(now=1003.0)["jobs"]
    assert (running["stage"], running["state"]) == ("running", "working")
    assert running["since"] == 1002.0 and running["attempts"] == 1
    assert "waiting_on" not in running
    assert other["waiting"] == "waits for the running job to end"
    # A failed run goes back to the queue: the attempt stays counted.
    assert jobq.retry("reel-AAA111", "the egress dropped") is False
    again = jobq.envelopes(now=1004.0)["jobs"][0]
    assert (again["stage"], again["state"], again["attempts"]) == (
        "queued",
        "submitted",
        1,
    )
    assert again["waiting"] == "waits for the egress to reach www.instagram.com"
    for t in (1005.0, 1007.0):
        jobq.claim("reel-AAA111", now=t)
        jobq.retry("reel-AAA111", "the egress dropped", now=t + 1)
    spent = jobq.envelopes(now=1009.0)["jobs"][0]
    assert (spent["stage"], spent["state"], spent["attempts"]) == (
        "failed",
        "failed",
        3,
    )
    assert spent["since"] == 1008.0 and spent["finished_at"] == 1008.0
    assert "waiting_on" not in spent
    # Read-only: listing claims, prunes and changes nothing.
    assert jobq.get("reel-AAA111")["status"] == "failed"


def test_drop_removes_one_job(q):
    _add()
    (q / "reel-AAA111.err").write_text("")
    assert jobq.drop("reel-AAA111") is True
    assert jobq.drop("reel-AAA111") is False
    assert jobq.get("reel-AAA111") is None
    assert not (q / "reel-AAA111.err").exists()


# --- next: what the gate sees ------------------------------------------------------------


def test_next_is_idle_with_nothing_queued_or_one_running(q):
    assert jobq.next_job(now=1000.0) == ("idle", "")
    _add()
    jobq.claim("reel-AAA111", now=1001.0)
    _add("BBB222", now=1002.0)
    assert jobq.next_job(now=1003.0) == ("idle", "")


def test_next_runs_the_handler_itself_and_wakes_no_turn_for_it(q):
    _add_file()
    assert jobq.next_job(now=1001.0) == ("started", "")
    job = jobq.get("video-file-AAA111")
    assert (job["status"], job["attempts"]) == ("running", 1)
    job = _ran("video-file-AAA111")
    assert job["status"] == "running"  # it waits for the turn that answers
    assert job["run"]["exit"] == 0 and job["run"]["unread"] is True
    assert stat.S_IMODE((q / "video-file-AAA111.out").stat().st_mode) == 0o600


def test_a_run_that_printed_something_is_handed_to_one_turn_to_answer(q):
    jid = "video-file-AAA111"
    _add_file()
    jobq.next_job(now=1001.0)
    _ran(jid)
    state, message = jobq.next_job(now=1002.0)
    assert state == "ready"
    me = shlex.join([sys.executable, str(SKILL.resolve() / "jobq.py")])
    assert message.splitlines() == [
        f"Job {jid} has run (attempt 1 of 3, queued 1970-01-01 00:16 UTC).",
        f"Run exactly: {me} output {jid}",
        "Then answer as the video-eval skill says for a queued job.",
    ]
    assert jobq.output(jid, now=1003.0) == f"material for {jid}"
    assert not (q / f"{jid}.out").exists()
    assert jobq.output(jid, now=1004.0) == "NO_REPLY"  # read once
    _add_file("BBB222", now=1005.0)
    assert jobq.next_job(now=1006.0) == ("idle", "")  # its turn is judging it
    jobq.finish(jid, "judged", now=1007.0)
    assert jobq.next_job(now=1008.0) == ("started", "")
    assert jobq.get("video-file-BBB222")["status"] == "running"


def test_a_job_the_handler_finished_is_answered_before_the_next_one_starts(
    q, monkeypatch
):
    monkeypatch.setenv("FAKE_MODE", "answer")
    _add_file("AAA111", now=1000.0)
    _add_file("BBB222", now=1001.0)
    jobq.next_job(now=1002.0)
    assert _ran("video-file-AAA111")["status"] == "done"
    state, message = jobq.next_job(now=1003.0)
    assert state == "ready" and "output video-file-AAA111" in message
    assert jobq.output("video-file-AAA111") == "the answer for video-file-AAA111"
    assert jobq.next_job(now=1004.0) == ("started", "")
    assert jobq.get("video-file-BBB222")["status"] == "running"


def test_a_run_that_says_no_reply_wakes_no_turn(q, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "retry")
    _add_file()
    jobq.next_job(now=1001.0)
    job = _ran("video-file-AAA111")
    assert (job["status"], job["attempts"]) == ("queued", 1)
    assert not (q / "video-file-AAA111.out").exists()
    monkeypatch.setenv("FAKE_MODE", "material")
    assert jobq.next_job(now=1002.0) == ("started", "")  # the retry, at once
    assert _ran("video-file-AAA111")["attempts"] == 2


def _descendants(pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        children.setdefault(int(fields[1]), []).append(int(proc.name))
    out, todo = [], [pid]
    while todo:
        kids = children.get(todo.pop(), [])
        out += kids
        todo += kids
    return out


def test_the_run_outlives_the_command_that_started_it(q, monkeypatch):
    # The bug this fixes: the agent ran the handler in its exec tool, which
    # killed it at the tool's timeout, mid-transcription, so it never reported
    # back. Now the gate's command starts it; killing that command the way the
    # tool does (its process group, then every process under it) leaves it be.
    monkeypatch.setenv("FAKE_SLEEP", "2")
    _add_file()
    gate = subprocess.Popen(
        [
            "sh",
            "-c",
            f"{shlex.join([sys.executable, str(SKILL / 'jobq.py'), 'next'])}"
            "; echo gate-done; sleep 60",
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert gate.stdout.readline() == "JOBQ idle\n"
    assert gate.stdout.readline() == "gate-done\n"
    for pid in _descendants(gate.pid):
        os.kill(pid, signal.SIGTERM)
    os.killpg(gate.pid, signal.SIGTERM)
    gate.wait(timeout=10)
    gate.stdout.close()
    job = _ran("video-file-AAA111")
    assert job["status"] == "running" and job["run"]["exit"] == 0
    assert jobq.next_job()[0] == "ready"
    assert jobq.output("video-file-AAA111") == "material for video-file-AAA111"


def test_a_runner_that_died_is_taken_up_again_at_once_then_given_up(q, monkeypatch):
    monkeypatch.setenv("JOBQ_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("FAKE_SLEEP", "60")
    _add_file()
    jid = "video-file-AAA111"
    jobq.next_job(now=1001.0)
    os.kill(jobq.get(jid)["run"]["pid"], signal.SIGKILL)  # a gateway restart, say
    _kill_runs(q)
    deadline = time.monotonic() + 10
    while jobq.next_job(now=1002.0) != ("started", "") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert jobq.get(jid)["attempts"] == 2
    os.kill(jobq.get(jid)["run"]["pid"], signal.SIGKILL)
    while (state := jobq.next_job(now=1003.0))[0] != "ready":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert "gave up: its last 2 runs never reported back" in state[1]
    assert jobq.get(jid)["status"] == "failed"


@pytest.mark.parametrize("mode", ["crash", "silent"])
def test_a_handler_that_ended_without_reporting_back_is_run_again_at_once(
    q, monkeypatch, mode
):
    monkeypatch.setenv("FAKE_MODE", mode)
    _add_file()
    jobq.next_job(now=1001.0)
    job = _ran("video-file-AAA111")
    assert job["status"] == "running" and "unread" not in job["run"]
    if mode == "crash":
        assert (
            job["run"]["exit"] == 1 and job["last_error"] == "exit 1: Traceback: boom"
        )
    assert jobq.next_job(now=1002.0) == ("started", "")
    assert jobq.get("video-file-AAA111")["attempts"] == 2


def test_a_read_output_nobody_finished_is_run_again_after_the_lease(q):
    jid = "video-file-AAA111"
    _add_file()
    jobq.next_job(now=1001.0)
    _ran(jid)
    jobq.next_job(now=1002.0)
    jobq.output(jid, now=1003.0)
    assert jobq.next_job(now=1003.0 + 29 * 60) == ("idle", "")
    assert jobq.next_job(now=1003.0 + 31 * 60) == ("started", "")
    assert jobq.get(jid)["attempts"] == 2


def test_only_the_queues_own_run_gets_the_job(q, monkeypatch):
    _add_file()
    jobq.claim("video-file-AAA111", now=1001.0)
    assert jobq.running("video-file-AAA111") is None  # a handler run by hand
    monkeypatch.setenv(jobq.RUN_ENV, "video-file-BBB222")
    assert jobq.running("video-file-AAA111") is None
    monkeypatch.setenv(jobq.RUN_ENV, "video-file-AAA111")
    assert jobq.running("video-file-AAA111")["attempts"] == 1


def test_next_waits_while_the_egress_does_not_answer(q, monkeypatch):
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", f"socks5h://127.0.0.1:{_closed_port()}")
    _add()
    assert jobq.next_job(now=1001.0) == ("wait", "")
    assert jobq.get("reel-AAA111").get("offers") is None  # a wait is no offer


def test_a_job_with_no_probe_needs_no_egress(q, monkeypatch):
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", f"socks5h://127.0.0.1:{_closed_port()}")
    jobq.add("video", "file-AAA111", owner="video-eval", handler=HANDLER, probe="")
    assert jobq.next_job(now=1001.0)[0] == "started"
    assert jobq.get("video-file-AAA111")["probe"] == ""


def test_a_file_behind_a_link_waiting_for_the_pc_is_offered_and_claimed(q, monkeypatch):
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", f"socks5h://127.0.0.1:{_closed_port()}")
    _add("AAA111", now=1000.0)
    jobq.add(
        "video",
        "file-BBB222",
        owner="video-eval",
        handler=HANDLER,
        probe="",
        now=1001.0,
    )
    assert jobq.next_job(now=1002.0) == ("started", "")
    assert jobq.get("video-file-BBB222")["status"] == "running"
    assert jobq.get("reel-AAA111")["status"] == "queued"
    _ran("video-file-BBB222")
    assert "video-file-BBB222" in jobq.next_job(now=1003.0)[1]
    jobq.output("video-file-BBB222", now=1004.0)
    assert jobq.next_job(now=1004.0) == ("idle", "")  # one is running
    jobq.finish("video-file-BBB222", "done", now=1005.0)
    assert jobq.next_job(now=1006.0) == ("wait", "")  # the link still waits


def test_files_keep_their_order_and_the_link_goes_first_once_the_egress_answers(q):
    _add("AAA111", now=1000.0)
    jobq.add(
        "video",
        "file-BBB222",
        owner="video-eval",
        handler=HANDLER,
        probe="",
        now=1001.0,
    )
    assert jobq.next_job(now=1002.0) == ("started", "")
    assert jobq.get("reel-AAA111")["status"] == "running"
    assert jobq.get("video-file-BBB222")["status"] == "queued"


def _offered(**kw) -> str:
    """A job that ran, its output handed to OFFER_LIMIT turns none read."""
    _add_file(**kw)
    jobq.next_job(now=1001.0)
    _ran("video-file-AAA111")
    for _ in range(jobq.OFFER_LIMIT):
        assert jobq.next_job(now=1001.0)[0] == "ready"
    return "video-file-AAA111"


def test_a_quiet_job_gives_up_without_a_reply(q):
    jid = _offered(quiet=True)
    state, message = jobq.next_job(now=1002.0)
    assert state == "ready" and message.splitlines()[1] == (
        "Run nothing. Reply exactly NO_REPLY: whoever queued it reads its status."
    )
    assert jobq.get(jid)["status"] == "failed"


def test_next_gives_up_after_offers_no_turn_read(q):
    jid = _offered()
    state, message = jobq.next_job(now=1002.0)
    assert state == "ready"
    assert message.startswith(
        f"Job {jid} gave up: 3 drain turns in a row did not read its result."
    )
    assert "Run nothing." in message and jid in message
    job = jobq.get(jid)
    assert job["status"] == "failed" and job["result"].startswith("gave up")
    assert not (jobq.root() / f"{jid}.out").exists()
    assert jobq.next_job(now=1003.0) == ("idle", "")


def test_each_run_starts_the_offer_count_again(q):
    jid = _offered()
    jobq.release(jid)
    assert jobq.output(jid) != "NO_REPLY"  # read at last; the run starts over
    assert jobq.next_job(now=1002.0) == ("started", "")
    _ran(jid)
    assert jobq.next_job(now=1003.0)[1].startswith(f"Job {jid} has run")


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
    _add_file()
    assert _cli("next", env=env).stdout == "JOBQ idle\n"  # started: no turn
    _ran("video-file-AAA111")
    out = _cli("next", env=env).stdout.splitlines()
    assert out[0] == "JOBQ ready" and out[2].startswith("Run exactly: ")
    run = shlex.split(out[2].removeprefix("Run exactly: "))
    r = subprocess.run(run, env=env, capture_output=True, text=True, check=True)
    assert r.stdout == "material for video-file-AAA111\n"
    jobq.finish("video-file-AAA111", "judged")
    _add()
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
  if (out === "REJECT") throw new Error("spawn failed");
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
        ("JOBQ ready\n", "THROW job-queue gate failed: jobq.py next: JOBQ ready"),
        (
            "jobq failed: boom\n",
            "THROW job-queue gate failed: jobq.py next: jobq failed: boom",
        ),
        ("", "THROW job-queue gate failed: jobq.py next: no output"),
        (
            "REJECT",
            "THROW job-queue gate failed: could not run jobq.py next: spawn failed",
        ),
    ],
)
def test_the_gate_fires_only_on_a_ready_job_and_throws_on_anything_odd(out, want):
    # Every fault names the gate, so an alert tells it from a slow gateway's
    # "code mode timeout exceeded" (docs/runbook.md, "Shared job queue").
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


# --- the envelope and the receipt ---------------------------------------------------------

ENVELOPE = {
    "origin": "firstmate",
    "owner": "kit",
    "class": "ops",
    "fingerprint": "https://example/AAA111",
    "acceptance": "the report is filed in the intake index",
    "thread": "intake-AAA111",
}


def _enqueue(*args, env=None, **flags):
    cmd = [
        "enqueue",
        "reel",
        "AAA111",
        "--skill",
        "video-eval",
        "--handler",
        json.dumps(HANDLER),
        "--probe",
        "",
        "--label",
        "https://example/AAA111",
        *args,
    ]
    for k, v in flags.items():
        cmd += [f"--{k.rstrip('_')}", v]
    return _cli(*cmd, env=env or {**os.environ})


def _envelope_flags(**over):
    return {k: v for k, v in {**ENVELOPE, **over}.items() if v is not None}


def test_enqueue_without_the_envelope_still_queues_and_prints_a_receipt(q):
    """The drop door's shape: no envelope given, the job still queues."""
    r = _enqueue()
    assert r.returncode == 0, r.stderr
    receipt = json.loads(r.stdout)
    assert set(receipt) == {"id", "url", "state"}
    assert receipt["id"] == "reel-AAA111" and receipt["state"] == "submitted"
    assert receipt["url"].endswith("jobq.py status reel-AAA111")
    job = jobq.get("reel-AAA111")
    assert job["status"] == "queued" and "envelope" not in job


def test_enqueue_with_the_envelope_stores_every_field_and_prints_the_receipt(q):
    r = _enqueue(**_envelope_flags(class_="ops"))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {
        "id": "reel-AAA111",
        "url": f"{sys.executable} {SKILL / 'jobq.py'} status reel-AAA111",
        "state": "submitted",
    }
    assert jobq.get("reel-AAA111")["envelope"] == ENVELOPE
    assert stat.S_IMODE((q / "reel-AAA111.json").stat().st_mode) == 0o600


def test_enqueue_takes_the_receipt_url_from_the_door(q):
    r = _enqueue("--url", "video-eval.sh status reel-AAA111")
    assert json.loads(r.stdout)["url"] == "video-eval.sh status reel-AAA111"


def test_the_fingerprint_and_thread_default_to_the_jobs_own(q):
    flags = _envelope_flags(fingerprint=None, thread=None)
    assert _enqueue(**flags).returncode == 0
    env = jobq.get("reel-AAA111")["envelope"]
    assert env["fingerprint"] == "https://example/AAA111"
    assert env["thread"] == "reel-AAA111"


@pytest.mark.parametrize("field", ["origin", "owner", "class", "acceptance"])
def test_enqueue_refuses_an_envelope_missing_a_field_and_queues_nothing(q, field):
    r = _enqueue(**_envelope_flags(**{field: None}))
    assert r.returncode == 1
    assert f"envelope missing: {field}" in r.stderr
    assert jobq.get("reel-AAA111") is None


def test_enqueue_refuses_an_unknown_class(q):
    r = _enqueue(**_envelope_flags(class_="weather"))
    assert r.returncode == 1 and "envelope class must be one of" in r.stderr
    assert jobq.get("reel-AAA111") is None


def test_enqueue_refuses_a_handler_that_is_not_a_json_list(q):
    cmd = ["enqueue", "reel", "AAA111", "--skill", "s", "--handler", '"run"']
    r = _cli(*cmd, env={**os.environ})
    assert r.returncode == 1 and "JSON list" in r.stderr


def test_a_repeat_while_it_waits_adds_a_count_and_the_latest_occurrence(q):
    first = json.loads(_enqueue(**_envelope_flags()).stdout)
    again = json.loads(
        _enqueue(**_envelope_flags(origin="kit", thread="intake-again")).stdout
    )
    assert again == first  # the same receipt: the filer never asks again
    job = jobq.get("reel-AAA111")
    assert job["repeats"] == 1
    assert (
        job["latest"]["origin"] == "kit" and job["latest"]["thread"] == "intake-again"
    )
    assert job["envelope"] == ENVELOPE  # the first occurrence's envelope stays
    assert len(list(q.glob("*.json"))) == 1


def test_a_repeat_of_a_running_job_reports_it_working(q):
    _add_file()
    jobq.next_job(now=1001.0)
    assert jobq.get("video-file-AAA111")["status"] == "running"
    _add_file()
    assert jobq.get("video-file-AAA111")["repeats"] == 1
    assert jobq.job_receipt("video-file-AAA111")["state"] == "working"


def test_a_finished_job_queued_again_is_a_new_one_naming_the_old(q):
    _enqueue(**_envelope_flags())
    finished = time.time()
    jobq.finish("reel-AAA111", "judged", now=finished)
    receipt = json.loads(_enqueue(**_envelope_flags()).stdout)
    assert receipt["state"] == "submitted"
    job = jobq.get("reel-AAA111")
    assert job["status"] == "queued" and "repeats" not in job
    assert job["reopens"] == {
        "id": "reel-AAA111",
        "status": "done",
        "finished_at": finished,
    }


def test_the_receipt_states_are_the_a2a_names(q):
    names = {
        jobq.QUEUED: "submitted",
        jobq.RUNNING: "working",
        jobq.DONE: "completed",
        jobq.FAILED: "failed",
    }
    for status, name in names.items():
        assert jobq.receipt("x", "y", status) == {"id": "x", "url": "y", "state": name}


def test_status_prints_the_receipt_as_it_stands_and_fails_for_no_job(q):
    _enqueue()
    jobq.finish("reel-AAA111", "judged")
    r = _cli("status", "reel-AAA111", env={**os.environ})
    assert json.loads(r.stdout)["state"] == "completed"
    assert _cli("status", "reel-NOPE", env={**os.environ}).returncode == 1


def test_a_handler_killed_by_a_signal_records_which_one(q, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "oom")
    _add_file()
    jobq.next_job(now=1001.0)
    job = _ran("video-file-AAA111")
    assert job["run"]["exit"] == -signal.SIGKILL
    assert job["last_error"] == "killed by SIGKILL"


def test_giving_up_names_the_last_exit_status_and_stderr_lines(q, monkeypatch):
    monkeypatch.setenv("JOBQ_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("FAKE_MODE", "noisy")
    _add_file()
    jid = "video-file-AAA111"
    jobq.next_job(now=1001.0)
    _ran(jid)
    state, message = jobq.next_job(now=1002.0)
    assert state == "ready"
    job = jobq.get(jid)
    assert job["status"] == "failed"
    assert "never reported back" in job["result"]
    assert (
        "exit 3: warming up | Traceback (most recent call last): | KeyError: 'x'"
        in (job["result"])
    )
    assert "KeyError: 'x'" in message


def test_a_read_output_whose_turn_never_finished_is_not_blamed_on_the_handler(
    q, monkeypatch
):
    monkeypatch.setenv("JOBQ_MAX_ATTEMPTS", "1")
    _add_file()
    jid = "video-file-AAA111"
    jobq.next_job(now=1001.0)
    _ran(jid)
    assert jobq.next_job(now=1002.0)[0] == "ready"
    assert jobq.output(jid, now=1003.0) != "NO_REPLY"
    state, message = jobq.next_job(now=1003.0 + 31 * 60)
    assert state == "ready"
    result = jobq.get(jid)["result"]
    assert "its output was read but the turn never finished the job" in result
    assert "printed nothing" not in result and "printed nothing" not in message


def test_a_final_silent_run_is_not_blamed_for_an_earlier_attempts_error(q, monkeypatch):
    monkeypatch.setenv("JOBQ_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("FAKE_MODE", "noisy")
    _add_file()
    jid = "video-file-AAA111"
    jobq.next_job(now=1001.0)
    assert _ran(jid)["last_error"].startswith("exit 3")
    monkeypatch.setenv("FAKE_MODE", "silent")
    assert jobq.next_job(now=1002.0) == ("started", "")
    assert _ran(jid)["attempts"] == 2
    state, message = jobq.next_job(now=1002.0 + 31 * 60)
    assert state == "ready"
    result = jobq.get(jid)["result"]
    assert "the handler exited 0 and printed nothing" in result
    assert "KeyError" not in result and "KeyError" not in message


def test_a_runner_told_to_stop_records_it_and_its_handler_is_killed(q, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "60")
    _add_file()
    jid = "video-file-AAA111"
    jobq.next_job(now=1001.0)
    runner = jobq.get(jid)["run"]["pid"]
    deadline = time.monotonic() + 10
    while jobq.get(jid)["run"].get("stage") != "handler running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    os.kill(runner, signal.SIGTERM)
    job = _ran(jid)
    assert job["run"]["exit"] is None
    assert job["run"]["signal"] == "SIGTERM"
    assert job["run"]["stage"] == "handler running"
    assert job["run"]["ran_for"] >= 0
    assert job["last_error"].startswith(
        "no exit status: the runner was stopped by SIGTERM at stage "
        "'handler running' after "
    )


def test_a_runner_killed_outright_is_named_when_the_queue_gives_up(q, monkeypatch):
    monkeypatch.setenv("JOBQ_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("FAKE_SLEEP", "60")
    _add_file()
    jid = "video-file-AAA111"
    jobq.next_job(now=1001.0)
    pid = jobq.get(jid)["run"]["pid"]
    os.kill(pid, signal.SIGKILL)
    _kill_runs(q)
    deadline = time.monotonic() + 10
    while (state := jobq.next_job(now=1002.0))[0] != "ready":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    result = jobq.get(jid)["result"]
    assert f"runner pid {pid} was gone before it recorded an exit" in result
    assert "never reported back" in state[1]


def test_a_runner_crash_leaves_its_traceback_in_the_give_up(q, monkeypatch):
    monkeypatch.setenv("JOBQ_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("FAKE_SLEEP", "60")
    _add_file()
    jid = "video-file-AAA111"
    jobq.next_job(now=1001.0)
    pid = jobq.get(jid)["run"]["pid"]
    os.kill(pid, signal.SIGKILL)
    _kill_runs(q)
    (q / f"{jid}.err").write_text("Traceback (most recent call last):\nMemoryError\n")
    deadline = time.monotonic() + 10
    while jobq.next_job(now=1002.0)[0] != "ready":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert (
        "its stderr: Traceback (most recent call last): | MemoryError"
        in (jobq.get(jid)["result"])
    )


# --- a killed runner leaves a cause (lifekit-stack#676) -------------------------


def test_a_runner_killed_outright_names_the_stage_it_reached_and_how_long_it_lived(q):
    # A mocked SIGKILL: the run's own record as the runner left it (its last
    # mark), and a pid that names no process any more.
    _add_file()
    jid = "video-file-AAA111"
    with jobq.locked() as d:
        p = jobq._path(d, jid)
        job = jobq._load(p)
        jobq._claim(job, 1001.0)
        job["run"] = {
            "pid": 2**22 + 7,
            "start": "1",
            "started_at": 1001.0,
            "via": "launcher",
            "runner_at": 1001.5,
            "stage": "handler running",
            "alive_at": 1001.5 + 185,
        }
        jobq._save(p, job)
    assert jobq._stale(jobq.get(jid), 1200.0)
    reason = jobq._never_reported(q, jobq.get(jid))
    assert f"runner pid {2**22 + 7} was gone before it recorded an exit" in reason
    assert "it reached stage 'handler running'" in reason
    assert "last seen alive 3m05s after it started" in reason
    assert "killed outright" in reason


def test_a_runner_the_gate_started_that_died_at_once_names_the_exec_cleanup(q):
    _add_file()
    jid = "video-file-AAA111"
    with jobq.locked() as d:
        p = jobq._path(d, jid)
        job = jobq._load(p)
        jobq._claim(job, 1001.0)
        job["run"] = {
            "pid": 2**22 + 7,
            "start": "1",
            "started_at": 1001.0,
            "via": "gate",
        }
        jobq._save(p, job)
    reason = jobq._never_reported(q, jobq.get(jid))
    assert "before it reached its first stage" in reason
    assert "exec tool's cleanup" in reason


def test_a_runner_marks_its_stage_while_it_runs(q, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "60")
    _add_file()
    jid = "video-file-AAA111"
    jobq.next_job(now=1001.0)
    deadline = time.monotonic() + 10
    while (run := jobq.get(jid)["run"]).get("stage") != "handler running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert run["via"] == "gate" and run["handler_pid"] > 0
    assert run["alive_at"] >= run["runner_at"]


@pytest.fixture
def this_runner(q):
    """A job claimed for this test process as its runner, so run_handler runs
    in-process; the signal handlers it installs are put back afterwards."""
    sigs = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    saved = {s: signal.getsignal(s) for s in sigs}
    _add_file()
    jid = "video-file-AAA111"
    with jobq.locked() as d:
        p = jobq._path(d, jid)
        job = jobq._load(p)
        jobq._claim(job, 1001.0)
        job["run"] = {"pid": os.getpid(), "start": "1", "started_at": 1001.0}
        jobq._save(p, job)
    yield jid
    for s, h in saved.items():
        signal.signal(s, h)


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


def test_a_breadcrumb_the_disk_refuses_does_not_end_the_run(this_runner, monkeypatch):
    # ENOSPC on every stage and sign-of-life write while the handler runs;
    # the run still goes to its end and records the handler's real exit.
    monkeypatch.setenv("FAKE_SLEEP", "1")
    monkeypatch.setattr(jobq, "BEAT", 0.1)
    real_save = jobq._save
    refused = []

    def full_disk(p, job):
        run = job.get("run") or {}
        if "ran_at" not in run and run.get("alive_at") != run.get("runner_at"):
            refused.append(run["stage"])
            raise OSError(28, "No space left on device")
        real_save(p, job)

    monkeypatch.setattr(jobq, "_save", full_disk)
    jobq.run_handler(this_runner)
    assert "handler running" in refused
    assert refused.count("runner started") > 1
    job = jobq.get(this_runner)
    assert job["run"]["exit"] == 0
    assert job["run"]["stage"] == "handler ended"
    assert "last_error" not in job
    assert jobq.output(this_runner) == f"material for {this_runner}"


@pytest.mark.parametrize(
    "boom, said",
    [
        (OSError(5, "Input/output error"), "Input/output error"),
        (RuntimeError("bug"), "runner crashed: RuntimeError: bug"),
    ],
)
def test_a_runner_that_fails_after_starting_its_handler_kills_it(
    this_runner, monkeypatch, boom, said
):
    monkeypatch.setenv("FAKE_SLEEP", "60")

    def broken_wait(proc, jid, deadline):
        raise boom

    monkeypatch.setattr(jobq, "_wait", broken_wait)
    jobq.run_handler(this_runner)
    job = jobq.get(this_runner)
    assert job["run"]["exit"] is None
    assert job["run"]["stage"] == "handler running"
    assert said in job["last_error"]
    assert _gone(job["run"]["handler_pid"])


ANCHOR = r"""
import ctypes, os, signal, subprocess, sys, time
# What OpenClaw's exec tool does around a command (2026.11.9,
# service-child-group-anchor): become the child subreaper, run the command,
# then kill every process it left behind, however detached, and reap them.
assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
print(subprocess.run(sys.argv[1:], capture_output=True, text=True).stdout, flush=True)

def children():
    kids = set()
    for t in os.listdir("/proc/self/task"):
        kids.update(int(x) for x in open(f"/proc/self/task/{t}/children").read().split())
    return kids

for sig in (signal.SIGTERM, signal.SIGKILL):
    for pid in children():
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
    time.sleep(1)
    while True:
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                break
        except ChildProcessError:
            break
"""


def _under_anchor(*argv):
    return subprocess.run(
        [sys.executable, "-c", ANCHOR, sys.executable, str(SKILL / "jobq.py"), *argv],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


def _launcher():
    p = subprocess.Popen(
        [sys.executable, str(SKILL / "jobq.py"), "launcher"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while not jobq.launcher_status().startswith("launcher pid"):
        assert time.monotonic() < deadline and p.poll() is None
        time.sleep(0.05)
    return p


def test_an_exec_anchor_kills_a_runner_the_gate_started_and_it_leaves_a_cause(
    q, monkeypatch
):
    # The bug: with no launcher the gate's command starts the runner, and the
    # exec tool's anchor kills it the moment the command ends. It must leave
    # the cause, never "it left no stderr" alone.
    monkeypatch.setenv("JOBQ_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("FAKE_SLEEP", "30")
    _add_file()
    jid = "video-file-AAA111"
    assert _under_anchor("next").startswith("JOBQ idle")
    deadline = time.monotonic() + 15
    while (state := jobq.next_job())[0] != "ready":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    result = jobq.get(jid)["result"]
    assert (
        "the runner was stopped by SIGTERM at stage" in result
        or "exec tool's cleanup" in result
    ), result
    assert "never reported back" in state[1]


def test_a_launched_runner_outlives_the_exec_anchor_and_reports(q, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "2")
    _add_file()
    jid = "video-file-AAA111"
    launcher = _launcher()
    assert _under_anchor("next").startswith("JOBQ idle")
    job = _ran(jid)
    assert job["run"]["via"] == "launcher" and job["run"]["exit"] == 0
    assert job["run"]["stage"] == "handler ended"
    assert jobq.next_job()[0] == "ready"
    assert jobq.output(jid) == f"material for {jid}"
    launcher.kill()
    launcher.wait()


def test_a_runner_no_launcher_started_is_given_up_with_that_reason(q, monkeypatch):
    monkeypatch.setenv("JOBQ_MAX_ATTEMPTS", "1")
    _add_file()
    jid = "video-file-AAA111"
    me = {"pid": os.getpid(), "start": jobq._proc_start(os.getpid())}
    jobq._save(q / jobq.LAUNCHER, me)  # a launcher that will never start it
    assert jobq.next_job(now=1001.0) == ("started", "")
    assert set(jobq.get(jid)["run"]) == {"requested_at"}
    assert jobq.next_job(now=1001.0 + 60)[0] == "idle"  # within the grace
    (q / jobq.LAUNCHER).unlink()
    state, message = jobq.next_job(now=1001.0 + jobq.LAUNCH_GRACE + 1)
    assert state == "ready"
    assert (
        f"no launcher started its runner within {jobq.LAUNCH_GRACE}s "
        "(no launcher is running)" in message
    )


def test_only_one_launcher_runs_and_it_leaves_when_its_code_changes(q, tmp_path):
    copy = tmp_path / "jobq.py"
    shutil.copy(SKILL / "jobq.py", copy)
    first = subprocess.Popen([sys.executable, str(copy), "launcher"])
    deadline = time.monotonic() + 10
    while not jobq.launcher_status().startswith("launcher pid"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    second = subprocess.run(
        [sys.executable, str(copy), "launcher"], timeout=10, check=False
    )
    assert second.returncode == 0 and first.poll() is None
    os.utime(copy, (time.time() + 5, time.time() + 5))  # a deploy installed new code
    assert first.wait(timeout=10) == 0
    assert "no launcher is running" in jobq.launcher_status()
