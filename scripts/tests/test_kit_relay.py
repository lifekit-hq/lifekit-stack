"""The Kit relay: the kit-relay forced command, its installer, Kit's relay.sh
client and the skill installer (docs/runbook.md, "Kit's second-mate relay").

kit-relay runs rendered by install-kit-relay.sh --print against a scratch
FM_HOME whose bin/fm-inbox.sh is a stub that records each call. sshd is not
started: SSH_ORIGINAL_COMMAND is set the way sshd sets it for a forced
command, and the PTY/forwarding refusals belong to the key's `restrict`
option, not to this script. `logger` is stubbed so no test writes the journal.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
RELAY_DIR = REPO / "scripts/kit-relay"
INSTALLER = RELAY_DIR / "install-kit-relay.sh"
CLIENT = REPO / "skills/secondmate-relay/relay.sh"
SKILL_MD = REPO / "skills/secondmate-relay/SKILL.md"
ENSURE = REPO / "scripts/ensure-secondmate-relay.sh"
COMPOSE = REPO / "compose/openclaw/docker-compose.yml"
CONF = "/run/lifekit/kit-relay/ssh_config"

FAKE_INBOX = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
home = pathlib.Path(os.environ["FM_HOME"])
with (home / "calls.jsonl").open("a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}) + "\n")
verb = sys.argv[1]
if verb == "note":
    rid = sys.argv[sys.argv.index("--request-id") + 1]
    body = sys.stdin.buffer.read()
    req = home / "state/inbox/.requests"
    req.mkdir(parents=True, exist_ok=True)
    if (req / rid).exists():
        nid, outcome = (req / rid).read_text(), "replay"
    else:
        nid, outcome = "note-%d" % (len(list(req.iterdir())) + 1), "created"
        (req / rid).write_text(nid)
        (home / "state/inbox" / (nid + ".note")).write_bytes(body)
    code = int((home / "note-exit").read_text()) if (home / "note-exit").exists() else 0
    print(json.dumps({"schema": "fm-inbox-note.v1", "outcome": outcome, "id": nid,
                      "request_id": rid, "saved": True, "announced": code == 0}))
    sys.exit(code)
if verb == "receipts":
    sys.stdout.write((home / "receipts.json").read_text())
"""

FAKE_LOGGER = """#!/bin/sh
printf '%s\\n' "$*" >> "$KIT_RELAY_TEST_LOG"
"""

# sshd's forced-command shape: whatever the client asked for, joined by
# spaces, lands in SSH_ORIGINAL_COMMAND, and the pinned script runs instead.
FAKE_SSH_FORCED = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
assert args[:3] == ["-F", "/run/lifekit/kit-relay/ssh_config", "relay"], args
os.environ["SSH_ORIGINAL_COMMAND"] = " ".join(args[3:])
os.execv(os.environ["KIT_RELAY_SCRIPT"], [os.environ["KIT_RELAY_SCRIPT"]])
"""

FAKE_SSH_RECORD = """#!/bin/sh
printf '%s\\n' "$@" > "$SSH_TEST_DIR/argv"
cat > "$SSH_TEST_DIR/stdin"
"""


def write_exec(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def render(fm_home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALLER), "--print"],
        env={**os.environ, "FM_HOME": str(fm_home)},
        capture_output=True,
        text=True,
        check=False,
    )


class Relay:
    def __init__(self, tmp: Path):
        self.home = tmp / "home"
        (self.home / "bin").mkdir(parents=True)
        (self.home / "state/inbox/.requests").mkdir(parents=True)
        write_exec(self.home / "bin/fm-inbox.sh", FAKE_INBOX)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        write_exec(self.bin / "logger", FAKE_LOGGER)
        self.log = tmp / "logger.log"
        rendered = render(self.home)
        assert rendered.returncode == 0, rendered.stderr
        self.script = write_exec(tmp / "kit-relay", rendered.stdout)
        self.env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(tmp),
            "KIT_RELAY_TEST_LOG": str(self.log),
            "KIT_RELAY_SCRIPT": str(self.script),
        }

    def run(
        self, cmd: str | None, body: bytes = b"", **env
    ) -> subprocess.CompletedProcess:
        full = {**self.env, **env}
        if cmd is not None:
            full["SSH_ORIGINAL_COMMAND"] = cmd
        return subprocess.run(
            [str(self.script)], input=body, env=full, capture_output=True, check=False
        )

    def calls(self) -> list[dict]:
        path = self.home / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def requests(self) -> list[str]:
        return sorted(p.name for p in (self.home / "state/inbox/.requests").iterdir())

    def notes(self) -> list[Path]:
        return sorted((self.home / "state/inbox").glob("*.note"))

    def logged(self) -> str:
        return self.log.read_text() if self.log.exists() else ""


@pytest.fixture
def relay(tmp_path):
    return Relay(tmp_path)


def lines(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.decode().splitlines()]


# ─── the installer ───────────────────────────────────────────────────────────


def test_repo_copy_refuses_to_run_unrendered(tmp_path):
    result = subprocess.run(
        [str(RELAY_DIR / "kit-relay")],
        env={"PATH": os.environ["PATH"], "SSH_ORIGINAL_COMMAND": "receipts"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 78
    assert "not installed" in result.stderr


def test_installer_pins_fm_home(relay):
    text = relay.script.read_text()
    assert f"readonly FM_HOME='{relay.home}'\n" in text
    assert "__FM_HOME__" not in text


@pytest.mark.parametrize(
    "fm_home", ["", "relative/home", "/tmp/it's", "/tmp/a|b", "/tmp/$(id)"]
)
def test_installer_refuses_unsafe_fm_home(fm_home):
    result = subprocess.run(
        ["bash", str(INSTALLER), "--print"],
        env={**os.environ, "FM_HOME": fm_home},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""


def test_installer_needs_fm_inbox_in_the_home(tmp_path):
    result = render(tmp_path)
    assert result.returncode == 1
    assert "fm-inbox.sh is missing" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="checks the non-root refusal")
def test_installer_installs_only_as_root(relay):
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env={**os.environ, "FM_HOME": str(relay.home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "must run as root" in result.stderr


# ─── note ────────────────────────────────────────────────────────────────────


def test_note_body_is_stored_byte_identical(relay):
    body = "say \"hi\" and 'bye' $HOME `id` back\\slash \U0001f680\n--\nnot a header\n"
    body_bytes = body.encode()
    result = relay.run("note kit-20261002-abc", body_bytes)
    assert result.returncode == 0, result.stderr
    received, note = lines(result)
    assert received == {
        "schema": "kit-relay-received.v1",
        "bytes": len(body_bytes),
        "sha256": hashlib.sha256(body_bytes).hexdigest(),
    }
    assert note["outcome"] == "created"
    [stored] = relay.notes()
    assert stored.read_bytes() == body_bytes
    [call] = relay.calls()
    assert call["argv"] == ["note", "--request-id", "kit-20261002-abc", "--json", "-"]
    assert "rid=kit-20261002-abc" in relay.logged()
    assert "exit=0" in relay.logged()


def test_same_request_id_is_a_replay_and_drops_the_second_body(relay):
    first = lines(relay.run("note kit-1", b"first body\n"))[1]
    second = relay.run("note kit-1", b"a different body\n")
    assert second.returncode == 0
    replay = lines(second)[1]
    assert replay["outcome"] == "replay"
    assert replay["id"] == first["id"]
    [stored] = relay.notes()
    assert stored.read_bytes() == b"first body\n"


def test_body_at_the_limit_is_accepted(relay):
    result = relay.run("note kit-max", b"a" * 32768)
    assert result.returncode == 0, result.stderr
    assert lines(result)[0]["bytes"] == 32768


@pytest.mark.parametrize(
    "body, reason",
    [
        (b"a" * 32769, "body over 32768 bytes; nothing saved"),
        (b"a" * 40000, "body over 32768 bytes; nothing saved"),
        (b"", "empty body; nothing saved"),
        (b"\xff\xfe", "body is not valid UTF-8"),
        (b"text\x00more", "body contains NUL bytes"),
    ],
)
def test_bad_body_is_refused_whole(relay, body, reason):
    result = relay.run("note kit-bad", body)
    assert result.returncode == 65
    assert f"kit-relay: refused: {reason}" in result.stderr.decode()
    assert result.stdout == b""
    assert relay.calls() == []
    assert relay.requests() == []


# ─── refusals ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        "note kit-1;id",
        "receipts $(id)",
        "cat /etc/passwd",
        "status",
        "",
        None,
        "note other-1",
        "note",
        "note kit-1 extra",
        "note kit-",
        "receipts 123",
        "receipts 00000000000a",
        "receipts 000000000001 more",
        "note kit-" + "a" * 160,
    ],
)
def test_anything_but_the_two_verbs_is_refused(relay, cmd):
    result = relay.run(cmd, b"body\n")
    assert result.returncode == 64
    assert result.stderr.decode().startswith("kit-relay: refused: ")
    assert relay.calls() == []
    assert "refused: " in relay.logged()


# ─── rate limit ──────────────────────────────────────────────────────────────


def fill_requests(relay, count, prefix="kit-old-", age_s=0):
    req = relay.home / "state/inbox/.requests"
    for i in range(count):
        path = req / f"{prefix}{i}"
        path.write_text(f"note-old-{i}")
        if age_s:
            t = time.time() - age_s
            os.utime(path, (t, t))


def test_rate_limit_refuses_a_new_note_after_thirty_in_an_hour(relay):
    fill_requests(relay, 30)
    result = relay.run("note kit-new", b"body\n")
    assert result.returncode == 75
    assert "rate limit: 30 notes in the last hour" in result.stderr.decode()
    assert relay.calls() == []


def test_rate_limit_never_blocks_a_replay(relay):
    fill_requests(relay, 30)
    result = relay.run("note kit-old-3", b"body\n")
    assert result.returncode == 0, result.stderr
    note = lines(result)[1]
    assert (note["outcome"], note["id"]) == ("replay", "note-old-3")


def test_rate_limit_counts_only_recent_kit_notes(relay):
    fill_requests(relay, 29)
    fill_requests(relay, 10, prefix="kit-stale-", age_s=2 * 3600)
    fill_requests(relay, 10, prefix="alert-")
    assert relay.run("note kit-new", b"body\n").returncode == 0


# ─── environment and exit codes ──────────────────────────────────────────────


def test_fm_inbox_gets_only_the_pinned_environment(relay):
    result = relay.run(
        "note kit-env",
        b"body\n",
        FM_HOME="/tmp/evil",
        FM_INBOX_OVERRIDE="/tmp/evil",
        LC_ALL="C",
        AWS_SECRET_ACCESS_KEY="x",
    )
    assert result.returncode == 0, result.stderr
    [call] = relay.calls()
    assert set(call["env"]) == {"HOME", "PATH", "LANG", "FM_HOME"}
    assert call["env"]["FM_HOME"] == str(relay.home)
    assert call["env"]["LANG"] == "C.UTF-8"


def test_saved_but_not_announced_exit_passes_through(relay):
    (relay.home / "note-exit").write_text("3")
    result = relay.run("note kit-wake", b"body\n")
    assert result.returncode == 3
    assert lines(result)[1]["announced"] is False
    assert "exit=3" in relay.logged()


# ─── receipts ────────────────────────────────────────────────────────────────


def receipts_fixture() -> dict:
    def note(nid, rid):
        return {"id": nid, "request_id": rid, "body": f"body of {nid}"}

    return {
        "schema": "fm-inbox-receipts.v1",
        "home": "/srv/home",
        "generated": "2026-10-02T16:51:13Z",
        "pending": [note("p1", "kit-a"), note("p2", None), note("p3", "fs-x")],
        "handled": [note(f"h{i}", f"kit-h{i}") for i in range(25)]
        + [note("h-alert", "alert-1")],
        "replies": [
            {"id": "h0", "at": "2026-10-02T16:51:13Z", "body": 'Done: é "quoted"'},
            {"id": "h-alert", "at": "2026-10-02T16:52:00Z", "body": "not for kit"},
        ],
        "reply_cursor": "000000000007",
    }


def test_receipts_show_only_kit_notes_and_their_replies(relay):
    (relay.home / "receipts.json").write_text(json.dumps(receipts_fixture()))
    result = relay.run("receipts")
    assert result.returncode == 0, result.stderr
    [out] = lines(result)
    assert out["schema"] == "kit-relay-receipts.v1"
    assert out["source_schema"] == "fm-inbox-receipts.v1"
    assert [n["id"] for n in out["pending"]] == ["p1"]
    assert [n["id"] for n in out["handled"]] == [f"h{i}" for i in range(20)]
    assert out["replies"] == [receipts_fixture()["replies"][0]]
    assert out["reply_cursor"] == "000000000007"
    [call] = relay.calls()
    assert call["argv"] == [
        "receipts",
        "--all-pending",
        "--all-handled",
        "--all-replies",
    ]


def test_receipts_pass_the_cursor_through(relay):
    (relay.home / "receipts.json").write_text(json.dumps(receipts_fixture()))
    assert relay.run("receipts 000000000001").returncode == 0
    [call] = relay.calls()
    assert call["argv"][:3] == ["receipts", "--after", "000000000001"]


# ─── the client ──────────────────────────────────────────────────────────────


def utc_days() -> set[str]:
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        (now + datetime.timedelta(seconds=s)).strftime("%Y%m%d") for s in (-5, 0, 5)
    }


@pytest.fixture
def recording_ssh(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_exec(bindir / "ssh", FAKE_SSH_RECORD)
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "SSH_TEST_DIR": str(tmp_path)}

    def run(*args):
        result = subprocess.run(
            ["sh", str(CLIENT), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        argv = tmp_path / "argv"
        sent = tmp_path / "stdin"
        return (
            result,
            argv.read_text().splitlines() if argv.exists() else None,
            sent.read_bytes() if sent.exists() else None,
        )

    return run


def test_client_note_derives_the_request_id_and_sends_the_bytes(
    recording_ssh, tmp_path
):
    body = "tell them — exactly this\n".encode()
    (tmp_path / "body.txt").write_bytes(body)
    result, argv, sent = recording_ssh("note", str(tmp_path / "body.txt"))
    assert result.returncode == 0, result.stderr
    assert argv[:4] == ["-F", CONF, "relay", "note"]
    day, digest = argv[4].removeprefix("kit-").split("-")
    assert day in utc_days()
    assert digest == hashlib.sha256(body).hexdigest()[:16]
    assert len(argv) == 5
    assert sent == body


def test_client_replies_with_and_without_cursor(recording_ssh):
    result, argv, sent = recording_ssh("replies")
    assert result.returncode == 0
    assert argv == ["-F", CONF, "relay", "receipts"]
    assert sent == b""
    _, argv, _ = recording_ssh("replies", "000000000003")
    assert argv == ["-F", CONF, "relay", "receipts", "000000000003"]


@pytest.mark.parametrize("args", [[], ["status"], ["note"], ["note", "/nonexistent"]])
def test_client_refuses_bad_usage(recording_ssh, tmp_path, args):
    result, argv, _ = recording_ssh(*args)
    assert result.returncode == 64
    assert argv is None


def test_client_refuses_an_empty_body_file(recording_ssh, tmp_path):
    (tmp_path / "empty.txt").write_bytes(b"")
    result, argv, _ = recording_ssh("note", str(tmp_path / "empty.txt"))
    assert result.returncode == 64
    assert argv is None


def test_client_through_the_forced_command_sends_once(relay, tmp_path):
    write_exec(relay.bin / "ssh", FAKE_SSH_FORCED)
    body = tmp_path / "body.txt"
    body.write_bytes(b"Please check whether the nightly backup ran.\n")

    def send():
        return subprocess.run(
            ["sh", str(CLIENT), "note", str(body)],
            env=relay.env,
            capture_output=True,
            check=False,
        )

    first, second = send(), send()
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    created, replay = lines(first)[1], lines(second)[1]
    assert created["outcome"] == "created"
    assert replay["outcome"] == "replay"
    assert replay["id"] == created["id"]
    assert len(relay.notes()) == 1


# ─── the skill and its wiring ────────────────────────────────────────────────


FAKE_DOCKER = """#!/bin/sh
case "$1" in
  ps) [ -n "$FAKE_GATEWAY" ] && echo "$FAKE_GATEWAY" ;;
  exec) printf '%s' "$FAKE_LISTED" ;;
esac
exit 0
"""


def run_ensure(tmp_path, gateway="", listed=""):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    write_exec(bindir / "docker", FAKE_DOCKER)
    return subprocess.run(
        ["bash", str(ENSURE)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "OPENCLAW_CONFIG_DIR": str(tmp_path / "config"),
            "FAKE_GATEWAY": gateway,
            "FAKE_LISTED": listed,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_ensure_installs_the_skill_into_kits_workspace(tmp_path):
    workspace = tmp_path / "config/agents/kit/workspace"
    workspace.mkdir(parents=True)
    for _ in range(2):  # converges on re-run
        result = run_ensure(tmp_path)
        assert result.returncode == 0, result.stderr
    dest = workspace / "skills/secondmate-relay"
    assert (dest / "SKILL.md").read_bytes() == SKILL_MD.read_bytes()
    assert (dest / "relay.sh").read_bytes() == CLIENT.read_bytes()
    assert (dest / "SKILL.md").stat().st_mode & 0o777 == 0o644
    assert (dest / "relay.sh").stat().st_mode & 0o777 == 0o755
    assert "allowlist not checked" in result.stdout


def test_ensure_reports_a_missing_allowlist_entry(tmp_path):
    (tmp_path / "config/agents/kit/workspace").mkdir(parents=True)
    result = run_ensure(tmp_path, gateway="abc123", listed="no")
    assert result.returncode == 0, result.stderr
    assert "NOT on agent 'kit''s skills list" in result.stdout


def test_ensure_needs_the_agent_workspace(tmp_path):
    result = run_ensure(tmp_path)
    assert result.returncode == 1
    assert "no workspace for agent 'kit'" in result.stderr


def test_skill_frontmatter_names_its_folder_and_gates_on_ssh():
    _, front, _ = SKILL_MD.read_text().split("---\n", 2)
    meta = yaml.safe_load(front)
    assert meta["name"] == "secondmate-relay"
    assert meta["metadata"]["openclaw"]["requires"]["bins"] == ["ssh"]
    assert "{baseDir}/relay.sh" in SKILL_MD.read_text()


def test_gateway_mounts_the_relay_key_read_only_and_names_the_host():
    gateway = yaml.safe_load(COMPOSE.read_text())["services"]["openclaw-gateway"]
    assert (
        "${LIFEKIT_SECRETS_DIR:-/srv/lifekit-secrets}/kit-relay:/run/lifekit/kit-relay:ro"
        in gateway["volumes"]
    )
    assert "host.docker.internal:host-gateway" in gateway["extra_hosts"]
