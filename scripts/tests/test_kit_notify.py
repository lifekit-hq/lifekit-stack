"""kit-notify.sh: Kit's one-shot Telegram notice and its delivery check (#725).

`docker` is stubbed: `ps` names one gateway, `cron add` records the job it was
asked for, `cron list` lists it, `cron runs` replays a scripted sequence of run
histories (the last one repeats), and `cron rm` drops the job. The real route
was proven end to end on the VPS; the output of that run is in the PR.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/kit-notify.sh"
CHAT = "424242"

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FAKE_ROOT"])
args = sys.argv[1:]
if args[0] == "ps":
    print(os.environ.get("FAKE_PS", "gw123"))
    sys.exit(0)
cmd = args[args.index("openclaw") + 1:]
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps(cmd) + "\n")
jobs_file = root / "jobs.json"
jobs = json.loads(jobs_file.read_text()) if jobs_file.exists() else []
def flag(name):
    return cmd[cmd.index(name) + 1] if name in cmd else None
if cmd[:2] == ["cron", "add"]:
    if os.environ.get("FAKE_ADD_FAILS"):
        print("Error: gateway refused", file=sys.stderr)
        sys.exit(1)
    job = {"id": "job-1", "name": flag("--name"), "agentId": flag("--agent"),
           "state": {"nextRunAtMs": int(os.environ["FAKE_NEXT_MS"])}}
    jobs.append(job)
    jobs_file.write_text(json.dumps(jobs))
    print("[gateway] log line before the json")
    print(json.dumps(job))
elif cmd[:2] == ["cron", "list"]:
    print(json.dumps({"jobs": jobs}))
elif cmd[:2] == ["cron", "runs"]:
    seq = json.loads((root / "runs.json").read_text())
    n_file = root / "runs.n"
    n = int(n_file.read_text()) if n_file.exists() else 0
    n_file.write_text(str(n + 1))
    print(json.dumps({"entries": seq[min(n, len(seq) - 1)]}))
elif cmd[:2] == ["cron", "rm"]:
    jobs = [j for j in jobs if j["id"] != cmd[2]]
    jobs_file.write_text(json.dumps(jobs))
    print(json.dumps({"ok": True}))
else:
    sys.exit("unexpected: " + " ".join(cmd))
"""

DELIVERED = {
    "action": "finished",
    "ts": 2,
    "tsIso": "2026-11-27T13:32:09Z",
    "status": "ok",
    "delivered": True,
    "deliveryStatus": "delivered",
    "delivery": {
        "resolved": {
            "ok": True,
            "channel": "telegram",
            "to": CHAT,
            "accountId": "default",
        }
    },
}
FAILED = {
    "action": "finished",
    "ts": 2,
    "status": "error",
    "delivered": None,
    "deliveryStatus": "unknown",
    "error": "telegram: chat not found",
    "deliveryError": "telegram: chat not found",
}
SILENT = {
    "action": "finished",
    "ts": 2,
    "status": "ok",
    "delivered": False,
    "deliveryStatus": "not-delivered",
    "deliverySuppressionReason": "silent",
}


def box(tmp_path, runs=None, env_extra=None, jobs=None):
    (tmp_path / "runs.json").write_text(json.dumps(runs or [[]]))
    if jobs is not None:
        (tmp_path / "jobs.json").write_text(json.dumps(jobs))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_ROOT": str(tmp_path),
        "FAKE_NEXT_MS": str(int(time.time() * 1000)),
        "KIT_NOTIFY_POLL": "0",
        **(env_extra or {}),
    }


def run(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def calls(tmp_path):
    f = tmp_path / "calls.jsonl"
    return (
        [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []
    )


def listed(tmp_path):
    f = tmp_path / "jobs.json"
    return json.loads(f.read_text()) if f.exists() else []


def test_send_schedules_a_kept_one_shot_kit_job_to_the_owner(tmp_path):
    env = box(tmp_path)
    r = run(env, "send", "--at", "5m", "hello from Kit")
    assert r.returncode == 0, r.stderr
    assert "scheduled: job job-1" in r.stdout
    assert "check job-1" in r.stdout
    (add,) = [c for c in calls(tmp_path) if c[:2] == ["cron", "add"]]
    flag = lambda n: add[add.index(n) + 1]  # noqa: E731
    assert flag("--agent") == "kit"
    assert flag("--at") == "5m"
    assert "--announce" in add
    assert flag("--channel") == "telegram"
    assert flag("--delivery-target") == "owner"
    # Kept after the run, so the run record survives for the check.
    assert "--keep-after-run" in add and "--delete-after-run" not in add
    # No chat id anywhere: the gateway resolves the owner itself.
    assert "--to" not in add and "--account" not in add
    assert json.loads(flag("--command-argv")) == ["echo", "hello from Kit"]
    # Nothing ran a delivery check without --wait.
    assert not [c for c in calls(tmp_path) if c[:2] == ["cron", "runs"]]


def test_check_delivered_reports_and_deletes_the_job(tmp_path):
    env = box(tmp_path, runs=[[DELIVERED]], jobs=[{"id": "job-1", "name": "x"}])
    r = run(env, "check", "job-1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("delivered: ")
    assert "deleted: job job-1" in r.stdout
    assert ["cron", "runs", "job-1", "--json"] in calls(tmp_path)
    assert listed(tmp_path) == []
    # The resolved chat id never reaches the output.
    assert CHAT not in r.stdout + r.stderr


def test_check_failed_exits_nonzero_with_the_reason_and_keeps_the_job(tmp_path):
    env = box(tmp_path, runs=[[FAILED]], jobs=[{"id": "job-1", "name": "x"}])
    r = run(env, "check", "job-1")
    assert r.returncode == 1
    assert "failed:" in r.stderr and "telegram: chat not found" in r.stderr
    assert not [c for c in calls(tmp_path) if c[:2] == ["cron", "rm"]]
    assert listed(tmp_path) == [{"id": "job-1", "name": "x"}]


def test_check_failed_never_prints_the_chat_id_the_gateway_names(tmp_path):
    # The shape OpenClaw 2026.11.9 records when Telegram refuses the owner DM
    # (seen on a throwaway gateway against a fake Bot API): the error names
    # the resolved chat twice. The reason must survive, the chat id must not.
    named = {
        **FAILED,
        "status": "ok",
        "deliveryStatus": "not-delivered",
        "deliveryError": (
            f"Telegram send failed: chat not found (chat_id={CHAT}). "
            f'Input was: "telegram:{CHAT}". | Call to \'sendMessage\' failed! '
            "(400: Bad Request: chat not found) | group migrated (new -100… id)"
        ),
    }
    env = box(tmp_path, runs=[[named]], jobs=[{"id": "job-1", "name": "x"}])
    r = run(env, "check", "job-1")
    assert r.returncode == 1
    assert "chat not found" in r.stderr and "400: Bad Request" in r.stderr
    assert CHAT not in r.stdout + r.stderr
    assert listed(tmp_path) == [{"id": "job-1", "name": "x"}]


def test_check_suppressed_delivery_is_a_failure(tmp_path):
    env = box(tmp_path, runs=[[SILENT]])
    r = run(env, "check", "job-1")
    assert r.returncode == 1
    assert "delivery=not-delivered" in r.stderr and "silent" in r.stderr
    assert not [c for c in calls(tmp_path) if c[:2] == ["cron", "rm"]]


def test_check_before_the_run_is_not_yet_run(tmp_path):
    started = {"action": "started", "ts": 1}
    env = box(tmp_path, runs=[[started]])
    r = run(env, "check", "job-1")
    assert r.returncode == 3
    assert r.stdout.startswith("not-yet-run")
    assert not [c for c in calls(tmp_path) if c[:2] == ["cron", "rm"]]


def test_check_reads_the_latest_finished_run(tmp_path):
    older_failure = {**FAILED, "ts": 1}
    env = box(tmp_path, runs=[[older_failure, DELIVERED]])
    assert run(env, "check", "job-1").returncode == 0


def test_send_wait_polls_until_delivered(tmp_path):
    env = box(tmp_path, runs=[[], [], [DELIVERED]])
    r = run(env, "send", "--wait", "hello")
    assert r.returncode == 0, r.stderr
    assert "delivered: " in r.stdout and "deleted: job job-1" in r.stdout
    assert len([c for c in calls(tmp_path) if c[:2] == ["cron", "runs"]]) == 3
    assert listed(tmp_path) == []


def test_send_wait_reports_a_failed_delivery(tmp_path):
    env = box(tmp_path, runs=[[FAILED]])
    r = run(env, "send", "--wait", "hello")
    assert r.returncode == 1
    assert "telegram: chat not found" in r.stderr
    assert len(listed(tmp_path)) == 1


def test_send_wait_gives_up_as_not_yet_run(tmp_path):
    env = box(tmp_path, runs=[[]], env_extra={"KIT_NOTIFY_WAIT_MAX": "0"})
    r = run(env, "send", "--wait", "hello")
    assert r.returncode == 3
    assert "not-yet-run" in r.stderr


def test_a_failed_cron_add_exits_nonzero_with_the_reason(tmp_path):
    env = box(tmp_path, env_extra={"FAKE_ADD_FAILS": "1"})
    r = run(env, "send", "hello")
    assert r.returncode == 1
    assert "cron add failed" in r.stderr and "gateway refused" in r.stderr


def test_usage_errors_exit_2_before_touching_the_gateway(tmp_path):
    env = box(tmp_path)
    for args in (
        [],
        ["send"],
        ["send", "  "],
        ["check"],
        ["bogus"],
        ["send", "--nope", "x"],
    ):
        r = run(env, *args)
        assert r.returncode == 2, args
    assert calls(tmp_path) == []


def test_refuses_when_two_gateways_match(tmp_path):
    env = box(tmp_path, env_extra={"FAKE_PS": "gw1\ngw2"})
    r = run(env, "send", "hello")
    assert r.returncode == 1
    assert "refusing to act" in r.stderr
    assert calls(tmp_path) == []
