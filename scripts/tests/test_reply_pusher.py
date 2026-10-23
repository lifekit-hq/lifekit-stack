"""Behaviour of scripts/reply-pusher against a fake firstmate home and a fake gateway.

The pusher tells Kit's inbound hook that firstmate replied to one of the owner's
notes. What this file pins: only `kit-` notes' replies are pushed (the route
table), the cursor starts at the newest existing reply, an outbox entry
survives a failed POST and is sent once on a later run under the same
Idempotency-Key, and no reply or note body ever reaches the payload, the
outbox, the log or any file in the state directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/reply-pusher"
SCRIPT = DIR / "reply_pusher.py"
INSTALLER = DIR / "install-reply-pusher.sh"

TOKEN = "test-token-" + "x" * 12
HOOK_PATH = "/hooks-c0ffee12deadbeef"
DECOY = "decoy-" + "y" * 12
REPLY_BODY = "REPLY-BODY-MARKER-4821 the owner's private answer"
NOTE_BODY = "NOTE-BODY-MARKER-7730 what the owner asked"

spec = importlib.util.spec_from_file_location("reply_pusher", SCRIPT)
reply_pusher = importlib.util.module_from_spec(spec)
sys.modules["reply_pusher"] = reply_pusher
spec.loader.exec_module(reply_pusher)

# Stands in for fm-inbox.sh: serves $FM_HOME/fixture.json as `receipts` output,
# applying --after the way the real one does. After its first call it serves
# late.json instead when that exists (a reply landing while a pass runs).
FAKE_INBOX = r"""#!/usr/bin/env python3
import json, os, sys
home = os.environ["FM_HOME"]
if os.path.exists(home + "/broken"):
    sys.stderr.write("NOTE-BODY-MARKER-7730 leaked on stderr\n")
    sys.exit(3)
calls = home + "/calls"
n = int(open(calls).read()) if os.path.exists(calls) else 0
open(calls, "w").write(str(n + 1))
if n == 0:
    open(home + "/argv", "w").write(json.dumps(sys.argv[1:]))
name = "late.json" if n >= 1 and os.path.exists(home + "/late.json") else "fixture.json"
data = json.load(open(home + "/" + name))
args = sys.argv[1:]
after = args[args.index("--after") + 1] if "--after" in args else ""
if after:
    data["replies"] = [r for r in data["replies"] if r["cursor"] > after]
data["reply_cursor"] = data["replies"][-1]["cursor"] if data["replies"] else after
json.dump(data, sys.stdout)
"""


def note(id_, request_id, handled=False):
    return {
        "id": id_,
        "request_id": request_id,
        "body": NOTE_BODY,
        "source": "relay",
        "acknowledged": handled,
    }


def reply(id_, cursor):
    return {
        "id": id_,
        "at": "2026-10-23T10:00:00Z",
        "body": REPLY_BODY,
        "cursor": cursor,
    }


def receipts(notes, replies):
    return {
        "schema": "fm-inbox-receipts.v1",
        "home": "x/y",
        "generated": "2026-10-23T10:00:00Z",
        "pending": [n for n in notes if not n["acknowledged"]],
        "handled": [n for n in notes if n["acknowledged"]],
        "replies": replies,
        "reply_cursor": replies[-1]["cursor"] if replies else "",
        "omitted": [],
    }


class Gateway:
    def __init__(self):
        outer = self
        self.requests = []
        self.statuses = []  # consumed one per request; empty means 200
        self.default = 200

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                status = outer.statuses.pop(0) if outer.statuses else outer.default
                self.send_response(status)
                if status in (301, 302, 307):
                    self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    gw = Gateway()
    yield gw
    gw.close()


@pytest.fixture
def box(tmp_path, gateway):
    state = tmp_path / "state"
    stack_env = tmp_path / "stack.env"
    stack_env.write_text(
        f"UNRELATED=1\nOTHER_SECRET={DECOY}\nOPENCLAW_HOOK_TOKEN={TOKEN}\n"
        f'export OPENCLAW_HOOK_PATH="{HOOK_PATH}"\n'
    )

    class Box:
        pass

    b = Box()
    b.tmp, b.state, b.stack_env, b.gateway = tmp_path, state, stack_env, gateway
    b.homes = []

    def add_home(name, notes, replies):
        home = tmp_path / name
        (home / "bin").mkdir(parents=True)
        inbox = home / "bin/fm-inbox.sh"
        inbox.write_text(FAKE_INBOX)
        inbox.chmod(0o755)
        (home / "fixture.json").write_text(json.dumps(receipts(notes, replies)))
        b.homes.append(home)
        return home

    def run(mode="run", expect=0, **env):
        full = {
            **os.environ,
            "REPLY_PUSHER_HOMES": " ".join(str(h) for h in b.homes),
            "REPLY_PUSHER_STATE": str(state),
            "REPLY_PUSHER_STACK_ENV": str(stack_env),
            "REPLY_PUSHER_GATEWAY": gateway.url,
            "REPLY_PUSHER_TIMEOUT": "5",
            **env,
        }
        full.pop("STATE_DIRECTORY", None)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), mode],
            env=full,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == expect, proc.stderr
        return proc

    def state_files():
        return [p for p in state.rglob("*") if p.is_file()] if state.exists() else []

    b.add_home, b.run, b.state_files = add_home, run, state_files
    return b


def assert_clean(box, proc):
    """No body, token, hook path or decoy anywhere the pusher wrote or logged."""
    haystacks = [proc.stdout, proc.stderr] + [p.read_text() for p in box.state_files()]
    haystacks += [r["body"] for r in box.gateway.requests]
    for text in haystacks:
        for secret in (
            REPLY_BODY,
            NOTE_BODY,
            "MARKER",
            TOKEN,
            HOOK_PATH.strip("/"),
            DECOY,
        ):
            assert secret not in text, f"{secret!r} leaked"


# ---------------------------------------------------------------- route table


@pytest.mark.parametrize(
    "request_id",
    [
        "dash-20261023-ab",
        "fs-1",
        "fleet-1",
        "term-1",
        "other-1",
        "KIT-1",
        "kit",
        "",
        None,
    ],
)
def test_only_kit_notes_are_routed(request_id):
    assert reply_pusher.route_for(request_id) is None


def test_kit_notes_route_to_the_firstmate_reply_hook():
    assert reply_pusher.route_for("kit-20261023-abc") == "firstmate-reply"
    assert reply_pusher.ROUTES == {"kit-": "firstmate-reply"}


def test_hook_url_normalises_the_leading_slash():
    for path in ("/hooks-x", "hooks-x", "/hooks-x/"):
        assert reply_pusher.hook_url("http://127.0.0.1:1", path, "firstmate-reply") == (
            "http://127.0.0.1:1/hooks-x/firstmate-reply"
        )


# ------------------------------------------------------------------ baseline


def test_baseline_starts_at_the_newest_reply_so_history_never_replays(box):
    box.add_home(
        "firstmate",
        [note("n1", "kit-a"), note("n2", "kit-b", handled=True)],
        [reply("n2", "000000000007"), reply("n1", "000000000009")],
    )
    proc = box.run("baseline")
    assert (box.state / "firstmate/cursor").read_text() == "000000000009\n"
    assert not box.gateway.requests
    box.run("run")
    assert not box.gateway.requests, "history replayed after the baseline"
    assert_clean(box, proc)


def test_baseline_with_no_replies_uses_the_zero_cursor_not_an_empty_one(box):
    # An empty --after would mean "no filter" and replay everything later.
    box.add_home("firstmate", [note("n1", "kit-a")], [])
    box.run("baseline")
    assert (box.state / "firstmate/cursor").read_text() == "000000000000\n"


def test_baseline_never_moves_an_existing_cursor(box):
    box.add_home("firstmate", [note("n1", "kit-a")], [reply("n1", "000000000009")])
    (box.state / "firstmate").mkdir(parents=True)
    (box.state / "firstmate/cursor").write_text("000000000003\n")
    box.run("baseline")
    assert (box.state / "firstmate/cursor").read_text() == "000000000003\n"


def test_run_without_a_baseline_stops_loudly_and_pushes_nothing(box):
    box.add_home("firstmate", [note("n1", "kit-a")], [reply("n1", "000000000009")])
    proc = box.run("run", expect=78)
    assert "baseline" in proc.stderr
    assert not box.gateway.requests


# ------------------------------------------------------------------ the push


def baselined(box, notes, replies_after):
    """A home whose cursor sits at 000000000001 with `replies_after` waiting."""
    home = box.add_home("firstmate", notes, replies_after)
    (box.state / "firstmate").mkdir(parents=True, exist_ok=True)
    (box.state / "firstmate/cursor").write_text("000000000001\n")
    return home


def test_kit_reply_is_pushed_as_ids_only_with_a_stable_idempotency_key(box):
    baselined(
        box,
        [note("1790-AbC", "kit-20261023-aaaa", handled=True)],
        [reply("1790-AbC", "000000000002")],
    )
    proc = box.run()
    [req] = box.gateway.requests
    assert req["path"] == f"{HOOK_PATH}/firstmate-reply"
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert req["headers"]["Idempotency-Key"] == "firstmate:000000000002"
    assert json.loads(req["body"]) == {
        "home": "firstmate",
        "noteId": "1790-AbC",
        "cursor": "000000000002",
    }
    assert list((box.state / "firstmate/outbox").glob("*")) == []
    assert (box.state / "firstmate/cursor").read_text() == "000000000002\n"
    assert_clean(box, proc)


def test_only_the_cursor_argument_reaches_fm_inbox_and_it_is_read_only(box):
    home = baselined(box, [note("n1", "kit-a")], [reply("n1", "000000000002")])
    box.run()
    argv = json.loads((home / "argv").read_text())
    assert argv == [
        "receipts",
        "--after",
        "000000000001",
        "--all-pending",
        "--all-handled",
        "--all-replies",
    ]


def test_replies_to_other_senders_notes_are_skipped_but_the_cursor_moves_past_them(box):
    baselined(
        box,
        [
            note("d1", "dash-20261023-a"),
            note("f1", "fs-20261023-a"),
            note("t1", "term-1"),
            note("o1", "fleet-9"),
            note("u1", None),
            note("k1", "kit-20261023-a"),
        ],
        [
            reply("d1", "000000000002"),
            reply("f1", "000000000003"),
            reply("t1", "000000000004"),
            reply("o1", "000000000005"),
            reply("u1", "000000000006"),
            reply("k1", "000000000007"),
        ],
    )
    proc = box.run()
    assert [json.loads(r["body"])["noteId"] for r in box.gateway.requests] == ["k1"]
    assert (box.state / "firstmate/cursor").read_text() == "000000000007\n"
    assert_clean(box, proc)


def test_skipped_only_replies_post_nothing_and_still_advance(box):
    baselined(box, [note("d1", "dash-a")], [reply("d1", "000000000002")])
    box.run()
    assert not box.gateway.requests
    assert (box.state / "firstmate/cursor").read_text() == "000000000002\n"


def test_replies_are_pushed_in_cursor_order(box):
    baselined(
        box,
        [note("a", "kit-a"), note("b", "kit-b", handled=True)],
        [reply("b", "000000000002"), reply("a", "000000000003")],
    )
    box.run()
    keys = [r["headers"]["Idempotency-Key"] for r in box.gateway.requests]
    assert keys == ["firstmate:000000000002", "firstmate:000000000003"]


def test_a_reply_that_lands_while_a_pass_runs_is_caught_in_the_same_run(box):
    home = baselined(box, [note("a", "kit-a")], [reply("a", "000000000002")])
    (home / "late.json").write_text(
        json.dumps(
            receipts(
                [note("a", "kit-a"), note("b", "kit-b")],
                [reply("a", "000000000002"), reply("b", "000000000003")],
            )
        )
    )
    box.run()
    keys = [r["headers"]["Idempotency-Key"] for r in box.gateway.requests]
    assert keys == ["firstmate:000000000002", "firstmate:000000000003"]


def test_nothing_new_makes_no_request_and_needs_no_secrets(box):
    box.add_home("firstmate", [note("a", "kit-a")], [reply("a", "000000000002")])
    (box.state / "firstmate").mkdir(parents=True)
    (box.state / "firstmate/cursor").write_text("000000000002\n")
    box.stack_env.write_text("")  # secrets absent: not needed with nothing to send
    box.run()
    assert not box.gateway.requests


# --------------------------------------------------------- retry, no duplicates


def test_failed_post_keeps_the_entry_and_a_later_run_sends_it_once(box):
    baselined(box, [note("a", "kit-a")], [reply("a", "000000000002")])
    box.gateway.statuses = [503]
    proc = box.run(expect=1)
    assert "503" in proc.stderr
    [entry] = list((box.state / "firstmate/outbox").glob("*.json"))
    assert json.loads(entry.read_text()) == {
        "home": "firstmate",
        "noteId": "a",
        "cursor": "000000000002",
        "hook": "firstmate-reply",
    }
    assert (box.state / "firstmate/cursor").read_text() == "000000000002\n"
    assert_clean(box, proc)

    proc = box.run()  # the gateway is back
    assert list((box.state / "firstmate/outbox").glob("*")) == []
    assert len(box.gateway.requests) == 2
    assert {r["headers"]["Idempotency-Key"] for r in box.gateway.requests} == {
        "firstmate:000000000002"
    }
    assert box.gateway.requests[0]["body"] == box.gateway.requests[1]["body"]
    assert_clean(box, proc)

    box.run()  # and now nothing is left to send
    assert len(box.gateway.requests) == 2


@pytest.mark.parametrize("status", [400, 401, 404, 500, 302])
def test_any_non_2xx_is_a_failure_that_keeps_the_entry(box, status):
    baselined(box, [note("a", "kit-a")], [reply("a", "000000000002")])
    box.gateway.statuses = [status]
    box.run(expect=1)
    assert len(box.gateway.requests) == 1, "a redirect or retry was followed"
    assert len(list((box.state / "firstmate/outbox").glob("*.json"))) == 1


def test_an_unreachable_gateway_retries_and_logs_neither_url_nor_token(box):
    baselined(box, [note("a", "kit-a")], [reply("a", "000000000002")])
    proc = box.run(expect=1, REPLY_PUSHER_GATEWAY="http://127.0.0.1:1")
    assert "transport error" in proc.stderr
    assert len(list((box.state / "firstmate/outbox").glob("*.json"))) == 1
    assert_clean(box, proc)


def test_a_failed_first_entry_blocks_later_ones_so_order_holds(box):
    baselined(
        box,
        [note("a", "kit-a"), note("b", "kit-b")],
        [reply("a", "000000000002"), reply("b", "000000000003")],
    )
    box.gateway.statuses = [500]
    box.run(expect=1)
    assert len(box.gateway.requests) == 1
    assert len(list((box.state / "firstmate/outbox").glob("*.json"))) == 2
    box.run()
    keys = [r["headers"]["Idempotency-Key"] for r in box.gateway.requests[1:]]
    assert keys == ["firstmate:000000000002", "firstmate:000000000003"]


def test_one_homes_failure_does_not_stop_the_others(box):
    broken = box.add_home("fm-vps", [note("x", "kit-x")], [reply("x", "000000000002")])
    (box.state / "fm-vps").mkdir(parents=True)
    (box.state / "fm-vps/cursor").write_text("000000000001\n")
    (broken / "broken").write_text("")
    baselined(box, [note("a", "kit-a")], [reply("a", "000000000002")])
    proc = box.run(expect=1)
    assert [json.loads(r["body"])["home"] for r in box.gateway.requests] == [
        "firstmate"
    ]
    assert "exit 3" in proc.stderr
    assert_clean(box, proc)  # the fm-inbox stderr, which quotes a note, is not logged


def test_unexpected_receipts_shape_pushes_nothing(box):
    home = baselined(box, [note("a", "kit-a")], [reply("a", "000000000002")])
    (home / "fixture.json").write_text(
        json.dumps({"schema": "something-else.v9", "replies": []})
    )
    proc = box.run(expect=1)
    assert "unexpected" in proc.stderr
    assert not box.gateway.requests


# ----------------------------------------------------------------- secrets


def test_read_env_keeps_only_the_two_hook_keys(box):
    got = reply_pusher.read_env(str(box.stack_env))
    assert got == {
        "OPENCLAW_HOOK_TOKEN": TOKEN,
        "OPENCLAW_HOOK_PATH": HOOK_PATH,
    }


@pytest.mark.parametrize(
    "text",
    [
        "OPENCLAW_HOOK_PATH=/hooks-x\n",
        "OPENCLAW_HOOK_TOKEN=abc\n",
        "OPENCLAW_HOOK_TOKEN=a b\n",
    ],
)
def test_missing_or_malformed_secrets_stop_without_a_retry_loop(box, text):
    baselined(box, [note("a", "kit-a")], [reply("a", "000000000002")])
    box.stack_env.write_text(text)
    proc = box.run(expect=78)
    assert "OPENCLAW_HOOK" in proc.stderr and "abc" not in proc.stderr
    assert not box.gateway.requests


def test_gateway_must_be_loopback(box):
    box.add_home("firstmate", [], [])
    box.run("baseline", expect=78, REPLY_PUSHER_GATEWAY="http://example.com")


# ------------------------------------------------------------------- state


def test_state_files_are_private(box):
    baselined(box, [note("a", "kit-a")], [reply("a", "000000000002")])
    box.gateway.statuses = [500]
    box.run(expect=1)
    for path in box.state_files():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_hostile_home_and_note_ids_are_refused():
    for bad in (
        "/tmp/Fm Home",
        "relative/path",
        "/tmp/UPPER",
        "/tmp/-lead",
        "/tmp/a;b",
    ):
        with pytest.raises(reply_pusher.NotConfigured):
            reply_pusher.Home(bad, Path("/tmp/state"))


def test_a_note_id_with_shell_or_markup_characters_is_not_pushed(box):
    baselined(box, [note("a;rm -rf", "kit-a")], [reply("a;rm -rf", "000000000002")])
    box.run()
    assert not box.gateway.requests


# --------------------------------------------------------------- installer


@pytest.fixture
def install(tmp_path):
    systemctl_log = tmp_path / "systemctl.log"
    su_log = tmp_path / "su.log"
    systemctl = tmp_path / "systemctl"
    calls = tmp_path / "calls.log"
    systemctl.write_text(
        f'#!/usr/bin/env bash\necho "$*" >> {systemctl_log}\necho systemctl >> {calls}\n'
    )
    su = tmp_path / "su"
    su.write_text(f'#!/usr/bin/env bash\necho "$*" >> {su_log}\necho su >> {calls}\n')
    systemctl.chmod(0o755)
    su.chmod(0o755)
    root = tmp_path / "root"

    def run(homes, expect=0, **env):
        proc = subprocess.run(
            ["bash", str(INSTALLER)],
            env={
                **os.environ,
                "INSTALL_ROOT": str(root),
                "SYSTEMCTL": str(systemctl),
                "SU": str(su),
                "ADMIN_USER": "opuser",
                "FM_HOMES": homes,
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == expect, proc.stderr
        return proc

    return run, root, systemctl_log, su_log, calls


def parse_unit(path):
    sections = {}
    current = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key, []).append(value)
    return sections


def test_installer_renders_one_watch_per_home_and_baselines_before_enabling(install):
    run, root, systemctl_log, su_log, calls = install
    run("/srv/fleet/firstmate /srv/fleet/fm-vps")
    unit_dir = root / "etc/systemd/system"
    path_unit = parse_unit(unit_dir / "lifekit-reply-pusher.path")
    assert path_unit["Path"]["PathChanged"] == [
        "/srv/fleet/firstmate/state/inbox/.replies",
        "/srv/fleet/fm-vps/state/inbox/.replies",
    ]
    assert path_unit["Path"]["Unit"] == ["lifekit-reply-pusher.service"]
    assert path_unit["Install"]["WantedBy"] == ["multi-user.target"]
    service = parse_unit(unit_dir / "lifekit-reply-pusher.service")
    svc = service["Service"]
    assert svc["Type"] == ["oneshot"]
    assert svc["User"] == ["opuser"]
    assert svc["Environment"] == [
        '"REPLY_PUSHER_HOMES=/srv/fleet/firstmate /srv/fleet/fm-vps"'
    ]
    # The whole rendered env is never handed to the unit: the pusher reads its
    # two hook keys itself (docs/secrets.md boundary rule).
    assert "EnvironmentFile" not in svc
    assert not any("OPENCLAW_HOOK" in v for vals in svc.values() for v in vals)
    # A failed push retries, but a missing operator step (exit 78) does not.
    assert svc["Restart"] == ["on-failure"]
    assert svc["RestartPreventExitStatus"] == ["78"]
    assert service["Unit"]["StartLimitIntervalSec"] == ["0"]
    assert svc["StateDirectory"] == ["lifekit-reply-pusher"]
    assert svc["StateDirectoryMode"] == ["0700"]
    assert svc["ProtectSystem"] == ["strict"]
    assert service["Install"]["WantedBy"] == ["multi-user.target"]
    for unit in (path_unit, service):
        assert "__" not in "".join(
            v for s in unit.values() for vs in s.values() for v in vs
        )
    script = root / "usr/local/libexec/lifekit-reply-pusher/reply_pusher.py"
    assert script.read_bytes() == SCRIPT.read_bytes()
    assert stat.S_IMODE((root / "var/lib/lifekit-reply-pusher").stat().st_mode) == 0o700
    [su_call] = su_log.read_text().splitlines()
    assert "opuser" in su_call and "reply_pusher.py' baseline" in su_call
    assert "REPLY_PUSHER_HOMES='/srv/fleet/firstmate /srv/fleet/fm-vps'" in su_call
    # Cursors are written before any unit is enabled, so nothing replays.
    assert calls.read_text().splitlines() == [
        "su",
        "systemctl",
        "systemctl",
        "systemctl",
    ]
    assert systemctl_log.read_text().splitlines() == [
        "daemon-reload",
        "enable lifekit-reply-pusher.service",
        "enable --now lifekit-reply-pusher.path",
    ]


def test_installer_skips_without_homes(install):
    run, root, systemctl_log, *_ = install
    proc = run("")
    assert "skipping" in proc.stderr
    assert not root.exists() and not systemctl_log.exists()


@pytest.mark.parametrize(
    "homes",
    [
        "relative/home",
        "/srv/fleet/Upper",
        "/srv/a b",
        "/srv/x/firstmate /srv/y/firstmate",
        "/srv/$(id)/fm",
    ],
)
def test_installer_refuses_unsafe_or_ambiguous_homes(install, homes):
    run, root, *_ = install
    run(homes, expect=1)
    assert not (root / "etc").exists()


def test_installer_needs_root_without_the_test_prefix():
    proc = subprocess.run(
        ["bash", str(INSTALLER)],
        env={**os.environ, "FM_HOMES": "/srv/fleet/firstmate", "INSTALL_ROOT": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    if os.geteuid() != 0:
        assert proc.returncode == 1 and "root" in proc.stderr
