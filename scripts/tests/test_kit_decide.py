"""kit-relay's decide verb (lifekit-stack#727): one JSON envelope on stdin, run
as `fleet-policy decide` with the actor the key pins, the verdict printed back.

kit-relay is rendered by install-kit-relay.sh --print with FLEET_POLICY and
TASKS_AXI pinned, and the pinned helper path is pointed at the repo's
kit_decide.py. fleet-policy is a stub that records its argv and environment and
answers as the test's scenario file says, appending the p16.jsonl line the real
one writes for that verdict. sshd is not started: SSH_ORIGINAL_COMMAND is set
the way sshd sets it for a forced command.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RELAY_DIR = REPO / "scripts/kit-relay"
INSTALLER = RELAY_DIR / "install-kit-relay.sh"
HELPER = RELAY_DIR / "kit_decide.py"
CLIENT = REPO / "skills/secondmate-relay/relay.sh"
PINNED_HELPER = "/usr/local/libexec/kit-decide/kit_decide.py"
CONF = "/run/lifekit/kit-relay/ssh_config"

FAKE_INBOX = "#!/bin/sh\nexit 0\n"

# fleet-policy decide as the relay runs it. The scenario file in FM_HOME picks
# the outcome; each verdict appends the p16.jsonl line the real CLI writes.
FAKE_FLEET_POLICY = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
home = pathlib.Path(os.environ["FM_HOME"])
argv = sys.argv[1:]
with (home / "fp-calls.jsonl").open("a") as f:
    f.write(json.dumps({"argv": argv, "env": dict(os.environ), "cwd": os.getcwd()}) + "\n")
flag = lambda name: next((a.split("=", 1)[1] for a in argv if a.startswith(name + "=")), None)
rid = argv[-1]
actor, subject = flag("--actor"), flag("--subject")
scenario = (home / "scenario").read_text().strip() if (home / "scenario").exists() else "auto"
if scenario == "auto":
    scenario = "self" if flag("--kind") == "self" else "filed"
state = pathlib.Path.home() / ".local/state/fleet-policy"
state.mkdir(parents=True, exist_ok=True)
def log(**e):
    # another actor's line lands between, as on a busy box
    with (state / "p16.jsonl").open("a") as f:
        f.write(json.dumps({"actor": "agent", "subject": subject, "outcome": "filed", "task": "other"}) + "\n")
        f.write(json.dumps({"ts": "2026-11-27T00:00:00+00:00", "actor": actor, "home": home.name, **e}) + "\n")
if scenario == "filed":
    log(**{"class": flag("--class"), "cell": "unlisted", "ruling": "agent-behavior.md#P16.1", "subject": subject,
           "outcome": "filed", "task": rid, "rows": (flag("--rows") or "").split(",")})
    print("held " + rid)
    sys.exit(0)
if scenario == "self":
    log(**{"class": "unclassified", "cell": "-", "ruling": flag("--record"), "subject": subject or "",
           "outcome": "self", "task": rid, "rows": (flag("--rows") or "").split(",")})
    print("recorded " + rid + ": taken for the captain")
    sys.exit(0)
if scenario == "ruled":
    log(**{"class": "unlisted", "cell": "allow-and-log", "ruling": "agent-behavior.md#P16.3", "subject": subject,
           "outcome": "refused-ruled", "ruled_by": "fleet.implementation-choice", "exception": "M17"})
    print("decide: refused: --step 'pick a library' reads as fleet.implementation-choice", file=sys.stderr)
    sys.exit(2)
if scenario == "repeat":
    log(**{"class": "github.merge-reserved", "cell": "stop-and-file", "ruling": "agent-behavior.md#P16",
           "subject": subject, "outcome": "refused-repeat"})
    print("decide: refused: he already answered github.merge-reserved", file=sys.stderr)
    sys.exit(2)
if scenario == "malformed":
    print("decide: refused: a call states the facts it rests on, each checked", file=sys.stderr)
    sys.exit(2)
print("Traceback: boom", file=sys.stderr)
sys.exit(1)
"""

FAKE_LOGGER = """#!/bin/sh
printf '%s\\n' "$*" >> "$KIT_RELAY_TEST_LOG"
"""

FAKE_SSH_FORCED = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
assert args[:3] == ["-F", "/run/lifekit/kit-relay/ssh_config", "relay"], args
os.environ["SSH_ORIGINAL_COMMAND"] = " ".join(args[3:])
os.execv(os.environ["KIT_RELAY_SCRIPT"], [os.environ["KIT_RELAY_SCRIPT"]])
"""

CALL = {
    "kind": "call",
    "rows": ["H2"],
    "boundary": "H2",
    "step": "top up the API credit by fifty dollars",
    "checked": [
        "the usage page shows 90 of 100 USD spent since 14 Nov",
        "auto-switch to subscription at 95 percent",
    ],
    "subject": "API credit top-up",
    "rec": "a",
    "options": {"a": "No top-up", "b": "Top up 50 USD"},
}

SELF = {
    "kind": "self",
    "rows": "M4",
    "question": "file the agreed relay work as an issue?",
    "answer": "filed it",
    "record": "system/decision-matrix.md M4",
}


def write_exec(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def render(home: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALLER), "--print"],
        env={**os.environ, "FM_HOME": str(home), **env},
        capture_output=True,
        text=True,
        check=False,
    )


class Box:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "fm-vps"
        (self.home / "bin").mkdir(parents=True)
        write_exec(self.home / "bin/fm-inbox.sh", FAKE_INBOX)
        write_exec(self.home / "bin/fm-captain-hold.sh", FAKE_INBOX)
        self.tools = tmp / "tools"
        self.tools.mkdir()
        self.tasks = write_exec(self.tools / "tasks-axi", "#!/bin/sh\nexit 0\n")
        self.fp = write_exec(self.tools / "fleet-policy", FAKE_FLEET_POLICY)
        out = render(self.home, TASKS_AXI=str(self.tasks), FLEET_POLICY=str(self.fp))
        assert out.returncode == 0, out.stderr
        self.rendered = out.stdout
        assert f"readonly DECIDE_BIN='{PINNED_HELPER}'" in out.stdout
        assert f"readonly FLEET_POLICY_BIN='{self.fp}'" in out.stdout
        self.script = write_exec(
            tmp / "kit-relay", out.stdout.replace(PINNED_HELPER, str(HELPER))
        )
        bindir = tmp / "bin"
        bindir.mkdir()
        write_exec(bindir / "logger", FAKE_LOGGER)
        write_exec(bindir / "ssh", FAKE_SSH_FORCED)
        self.log = tmp / "logger.log"
        self.env = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(tmp),
            "KIT_RELAY_TEST_LOG": str(self.log),
            "KIT_RELAY_SCRIPT": str(self.script),
        }
        self.state = tmp / ".local/state/lifekit-kit-relay/decide/fm-vps"

    def scenario(self, name: str) -> None:
        (self.home / "scenario").write_text(name)

    def run(
        self,
        cmd: str,
        envelope=CALL,
        sender: tuple[str, ...] = (),
        script: Path | None = None,
        **env,
    ):
        body = (
            envelope if isinstance(envelope, bytes) else json.dumps(envelope).encode()
        )
        return subprocess.run(
            [str(script or self.script), *sender],
            input=body,
            env={**self.env, "SSH_ORIGINAL_COMMAND": cmd, **env},
            capture_output=True,
            check=False,
        )

    def calls(self) -> list[dict]:
        path = self.home / "fp-calls.jsonl"
        return (
            [json.loads(x) for x in path.read_text().splitlines()]
            if path.exists()
            else []
        )


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def verdict(result) -> dict:
    [line] = result.stdout.decode().splitlines()
    return json.loads(line)


# ─── each verdict ────────────────────────────────────────────────────────────


def test_a_call_is_filed_with_the_keys_actor_and_every_field(box):
    r = box.run("decide kit-dec-1")
    assert r.returncode == 0, r.stderr
    v = verdict(r)
    assert v["schema"] == "kit-relay-decide.v1"
    assert (v["verdict"], v["id"], v["actor"], v["home"]) == (
        "filed",
        "kit-dec-1",
        "kit",
        "fm-vps",
    )
    assert v["rows"] == ["H2"] and v["class"] == "unlisted"
    [call] = box.calls()
    assert call["argv"] == [
        "decide",
        "--kind=call",
        "--actor=kit",
        f"--home={box.home}",
        "--class=unlisted",
        "--subject=API credit top-up",
        "--rec=a",
        "--boundary=H2",
        "--step=top up the API credit by fifty dollars",
        "--rows=H2",
        "--checked=the usage page shows 90 of 100 USD spent since 14 Nov",
        "--checked=auto-switch to subscription at 95 percent",
        "--option=a=No top-up",
        "--option=b=Top up 50 USD",
        "--",
        "kit-dec-1",
    ]
    assert call["cwd"] == str(box.home)
    assert "decide rid=kit-dec-1 actor=kit" in box.log.read_text()


def test_a_self_call_is_recorded(box):
    r = box.run("decide kit-dec-self", SELF)
    assert r.returncode == 0, r.stderr
    v = verdict(r)
    assert (v["verdict"], v["id"], v["rows"]) == ("recorded", "kit-dec-self", ["M4"])
    argv = box.calls()[0]["argv"]
    assert "--kind=self" in argv and "--rows=M4" in argv
    assert not any(a.startswith("--class=") for a in argv)


def test_a_ruled_call_is_refused_with_the_class_to_act_under(box):
    box.scenario("ruled")
    r = box.run("decide kit-dec-ruled")
    assert r.returncode == 2
    v = verdict(r)
    assert v["verdict"] == "refused-ruled"
    assert v["class"] == "fleet.implementation-choice"
    assert v["cell"] == "allow-and-log"
    assert "reads as fleet.implementation-choice" in v["message"]
    assert "id" not in v


def test_a_repeat_is_refused_as_answered(box):
    box.scenario("repeat")
    v = verdict(box.run("decide kit-dec-rep"))
    assert (v["verdict"], v["class"]) == ("refused-repeat", "github.merge-reserved")


def test_a_call_fleet_policy_refuses_as_malformed_says_why(box):
    box.scenario("malformed")
    r = box.run("decide kit-dec-bad")
    assert r.returncode == 2
    assert verdict(r) == {
        "schema": "kit-relay-decide.v1",
        "request_id": "kit-dec-bad",
        "actor": "kit",
        "home": "fm-vps",
        "verdict": "refused",
        "message": "a call states the facts it rests on, each checked",
    }


def test_a_conflict_or_no_row_call_without_a_class_is_the_unlisted_class(box):
    env = {k: v for k, v in CALL.items() if k not in ("boundary", "step")} | {
        "no_row": "no row covers it at all here"
    }
    box.run("decide kit-dec-nr", env)
    argv = box.calls()[0]["argv"]
    assert (
        "--class=unlisted" in argv and "--no-row=no row covers it at all here" in argv
    )


def test_a_named_class_passes_through(box):
    box.run("decide kit-dec-cls", CALL | {"class": "github.merge-reserved"})
    assert "--class=github.merge-reserved" in box.calls()[0]["argv"]


# ─── the actor is the key's ──────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["actor", "home", "id"])
def test_a_forged_actor_home_or_id_in_the_envelope_is_refused(box, key):
    r = box.run("decide kit-dec-forged", CALL | {key: "captain"})
    assert r.returncode == 65
    assert f"'{key}' is not the client's to set" in r.stderr.decode()
    assert box.calls() == []


def test_the_clients_environment_never_picks_the_actor(box):
    r = box.run(
        "decide kit-dec-env",
        FLEET_POLICY_ACTOR="captain",
        FLEET_POLICY_STATE="/tmp/x",
        KIT_DECIDE_ACTOR="captain",
    )
    assert r.returncode == 0, r.stderr
    env = box.calls()[0]["env"]
    assert "FLEET_POLICY_ACTOR" not in env and "FLEET_POLICY_STATE" not in env
    assert env["KIT_DECIDE_ACTOR"] == "kit"
    assert env["PATH"].split(":")[0] == str(box.tools)
    assert verdict(r)["actor"] == "kit"


@pytest.mark.parametrize(
    ("sender", "prefix", "actor"),
    [("openclaw", "oc-", "openclaw"), ("devclaw", "dc-", "devclaw")],
)
def test_each_deciding_key_files_as_its_own_actor(box, sender, prefix, actor):
    r = box.run(f"decide {prefix}dec-1", sender=(sender,))
    assert r.returncode == 0, r.stderr
    assert verdict(r)["actor"] == actor
    assert f"--actor={actor}" in box.calls()[0]["argv"]


@pytest.mark.parametrize("sender", ["dashboard", "finance-sentry"])
def test_keys_that_may_not_decide_are_refused(box, sender):
    prefix = {"dashboard": "dash-", "finance-sentry": "fs-"}[sender]
    r = box.run(f"decide {prefix}dec-1", sender=(sender,))
    assert r.returncode == 64
    assert b"decide is not this key's verb" in r.stderr
    assert box.calls() == []


@pytest.mark.parametrize(
    ("sender", "rid"),
    [
        ((), "oc-dec-1"),
        (("openclaw",), "kit-dec-1"),
        (("devclaw",), "kit-dec-1"),
        ((), "kit-a:b"),
        ((), "kit-a..b"),
    ],
)
def test_a_request_id_outside_the_keys_prefix_or_slug_is_refused(box, sender, rid):
    r = box.run(f"decide {rid}", sender=sender)
    assert r.returncode == 64
    assert box.calls() == []


# ─── envelope refusals ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"not json", "not JSON"),
        (b"[1, 2]", "one JSON object"),
        (
            json.dumps(CALL | {"kind": "ruling"}).encode(),
            "kind must be one of call, self",
        ),
        (json.dumps(CALL | {"tier": 1}).encode(), "unknown key 'tier'"),
        (json.dumps(CALL | {"checked": []}).encode(), "checked must be"),
        (
            json.dumps(CALL | {"options": ["a", "b"]}).encode(),
            "options must be an object",
        ),
        (json.dumps(CALL | {"options": {"a=b": "x", "c": "y"}}).encode(), "holds '='"),
        (
            json.dumps(CALL | {"subject": 3}).encode(),
            "subject must be a non-empty string",
        ),
        (b"", "empty body"),
    ],
)
def test_a_bad_envelope_is_refused_and_nothing_runs(box, body, reason):
    r = box.run("decide kit-dec-x", body)
    assert r.returncode == 65
    assert reason in r.stderr.decode()
    assert box.calls() == []


# ─── replay, retries and the rate limit ──────────────────────────────────────


def test_a_replay_returns_the_first_verdict_without_running_again(box):
    first = verdict(box.run("decide kit-dec-r"))
    box.scenario("ruled")
    r = box.run("decide kit-dec-r")
    assert r.returncode == 0
    assert verdict(r) == first | {"replay": True}
    assert len(box.calls()) == 1


def test_a_replay_of_a_refusal_keeps_its_exit(box):
    box.scenario("ruled")
    box.run("decide kit-dec-rr")
    r = box.run("decide kit-dec-rr")
    assert r.returncode == 2 and verdict(r)["verdict"] == "refused-ruled"
    assert len(box.calls()) == 1


def test_the_same_id_with_another_envelope_is_refused(box):
    box.run("decide kit-dec-s")
    r = box.run("decide kit-dec-s", CALL | {"subject": "something else"})
    assert r.returncode == 65
    assert b"used for a different envelope" in r.stderr
    assert len(box.calls()) == 1


def test_a_failed_run_is_not_recorded_so_a_retry_runs_again(box):
    box.scenario("crash")
    r = box.run("decide kit-dec-c")
    assert r.returncode == 1
    assert verdict(r)["verdict"] == "error"
    box.scenario("filed")
    assert verdict(box.run("decide kit-dec-c"))["verdict"] == "filed"
    assert len(box.calls()) == 2


def fill(box: Box, prefix: str, n: int, age: int = 0) -> None:
    box.state.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        p = box.state / f"{prefix}old-{i}.json"
        p.write_text("{}")
        if age:
            os.utime(p, (time.time() - age, time.time() - age))


def test_rate_limit_refuses_a_new_decision_after_thirty_in_an_hour(box):
    fill(box, "kit-", 30)
    r = box.run("decide kit-dec-31")
    assert r.returncode == 75
    assert box.calls() == []


def test_rate_limit_never_blocks_a_replay_and_counts_only_recent_own_records(box):
    box.run("decide kit-dec-early")
    fill(box, "kit-", 29)
    assert box.run("decide kit-dec-early").returncode == 0
    fill(box, "oc-", 30)
    (box.state / "kit-old-0.json").unlink()
    fill(box, "kit-stale-", 5, age=7200)
    assert box.run("decide kit-dec-new").returncode == 0


def test_decide_is_refused_when_the_install_pinned_no_fleet_policy(box):
    out = render(box.home, TASKS_AXI=str(box.tasks))
    assert out.returncode == 0, out.stderr
    script = write_exec(box.tmp / "kit-relay-nofp", out.stdout)
    r = box.run("decide kit-dec-1", script=script)
    assert r.returncode == 78
    assert b"decide is not installed" in r.stderr


def test_decide_on_the_main_home_uses_its_own_state_and_home(box, tmp_path):
    main = tmp_path / "firstmate"
    (main / "bin").mkdir(parents=True)
    write_exec(main / "bin/fm-inbox.sh", FAKE_INBOX)
    out = render(
        box.home,
        TASKS_AXI=str(box.tasks),
        FLEET_POLICY=str(box.fp),
        FM_HOME_ALT=str(main),
    )
    script = write_exec(
        tmp_path / "kit-relay-two", out.stdout.replace(PINNED_HELPER, str(HELPER))
    )
    r = box.run("decide kit-dec-m firstmate", script=script)
    assert r.returncode == 0, r.stderr
    assert verdict(r)["home"] == "firstmate"
    assert (
        tmp_path / ".local/state/lifekit-kit-relay/decide/firstmate/kit-dec-m.json"
    ).exists()


# ─── the installer ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        (
            {"FLEET_POLICY": "fleet-policy", "TASKS_AXI": "/bin/true"},
            "FLEET_POLICY must be an absolute path",
        ),
        (
            {"FLEET_POLICY": "/nonexistent/fleet-policy", "TASKS_AXI": "/bin/true"},
            "is missing or not executable",
        ),
        ({"FLEET_POLICY": "/bin/true"}, "FLEET_POLICY needs TASKS_AXI"),
    ],
)
def test_installer_refuses_a_bad_fleet_policy_pin(box, env, reason):
    out = render(box.home, **env)
    assert out.returncode != 0
    assert reason in out.stderr


def test_installer_without_fleet_policy_installs_no_decide(box):
    out = render(box.home)
    assert out.returncode == 0, out.stderr
    script = write_exec(box.tmp / "kit-relay-bare", out.stdout)
    r = box.run("decide kit-dec-1", script=script)
    assert r.returncode == 78
    assert b"decide is not installed" in r.stderr
    assert box.calls() == []


# ─── the client, end to end through the forced command ───────────────────────


def test_client_decide_derives_the_id_and_returns_the_verdict(box):
    f = box.tmp / "envelope.json"
    f.write_text(json.dumps(CALL))
    r = subprocess.run(
        ["sh", str(CLIENT), "decide", str(f)],
        env=box.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    v = json.loads(r.stdout)
    day, digest = v["request_id"].removeprefix("kit-dec-").split("-")
    assert digest == hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    assert v["verdict"] == "filed" and v["actor"] == "kit"
    # the same envelope again the same day is a replay
    r2 = subprocess.run(
        ["sh", str(CLIENT), "decide", str(f)],
        env=box.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert json.loads(r2.stdout)["replay"] is True
    assert len(box.calls()) == 1


@pytest.mark.parametrize(
    "args", [[], ["a", "b", "c", "d"], ["/nonexistent"], ["ENV", "nowhere"]]
)
def test_client_decide_refuses_bad_usage(box, args):
    f = box.tmp / "envelope.json"
    f.write_text(json.dumps(CALL))
    args = [str(f) if a == "ENV" else a for a in args]
    r = subprocess.run(
        ["sh", str(CLIENT), "decide", *args],
        env=box.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 64
    assert box.calls() == []
