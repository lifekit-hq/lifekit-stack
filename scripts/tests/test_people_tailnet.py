"""The tailnet half of scripts/people/people.py against fake Logto and Tailscale APIs.

The fake Tailscale API serves a HuJSON policy with an ETag, validates and
writes it with If-Match, lists users and creates user invites. These tests
cover the managed groups block (everything outside it stays byte for byte), an
idempotent sync, the 412 retry, the never-grant ports refusal, the invite with
and without the user token, the unconfigured skip, and status drift. No
credential reaches the output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_logto_admin import (
    APP_ID,
    APP_SECRET,
    TOKEN,
    FakeLogto,
    handler_for,
)

REPO = Path(__file__).resolve().parents[2]
PEOPLE = REPO / "scripts/people/people.py"
sys.path.insert(0, str(REPO / "scripts/people"))
import tailnet  # noqa: E402

HOST = "box.example.ts.net"
CLIENT_ID = "ts-client-id-0123"
CLIENT_SECRET = "tskey-client-secret-value-abcdef"
TS_TOKEN = "ts-access-token-value-qrstuv"
USER_TOKEN = "tskey-api-k123CNTRL-user-token-value-wxyz"
INVITE_URL = "https://login.tailscale.example/uinv/abc123"

POLICY = """\
// Tailnet policy (fixture).
{
  "hosts": { "box": "100.64.0.1" },
  "groups": {
    "group:ops": ["ops@example.com"],
    // BEGIN people-managed: written by `people sync`, hand edits are overwritten
    "group:finance-sentry": [],
    "group:backstage": [],
    // END people-managed
  },
  "grants": [
    /* static, written once by hand */
    { "src": ["group:finance-sentry"], "dst": ["box"], "ip": ["tcp:4200", "tcp:3001"] },
    { "src": ["group:backstage"], "dst": ["box"], "ip": ["tcp:18810", "tcp:3001"] },
    { "src": ["group:ops"], "dst": ["box"], "ip": ["*"] },
  ],
  "tests": [
    { "src": "ops@example.com", "accept": ["box:3002"] },
  ],
}
"""


class FakeTailscale:
    def __init__(self) -> None:
        self.policy = POLICY
        self.etag_n = 1
        self.users = [
            {
                "loginName": "owner@example.com",
                "role": "owner",
                "status": "active",
                "deviceCount": 2,
                "lastSeen": "2026-10-16T08:00:00Z",
            }
        ]
        self.validate_error: str | None = None
        self.conflicts = 0  # next POST /acl calls answer 412 after a rival edit
        self.no_etag = False
        self.requests: list[tuple[str, str, bytes]] = []
        self.user_token_ok = True
        self.users_status = 200
        # the user token's own key record (GET .../keys/<id>)
        self.key: dict = {"id": "k123", "expires": "2099-01-01T00:00:00Z"}
        self.key_status = 200
        self.suspended: list[str] = []

    @property
    def etag(self) -> str:
        return f'"etag-{self.etag_n}"'

    def acl_writes(self) -> list[bytes]:
        return [b for m, p, b in self.requests if m == "POST" and p.endswith("/acl")]

    def invite_bodies(self) -> list[bytes]:
        return [b for m, p, b in self.requests if p.endswith("/user-invites")]


def ts_handler(fake: FakeTailscale):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body="", headers=None, ctype="application/json"):
            raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def handle_any(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            path = urllib.parse.urlsplit(self.path).path
            fake.requests.append((method, path, raw))
            if path == "/api/v2/oauth/token":
                form = urllib.parse.parse_qs(raw.decode())
                if form.get("client_id") != [CLIENT_ID] or form.get(
                    "client_secret"
                ) != [CLIENT_SECRET]:
                    return self.reply(401, {"message": "bad client"})
                return self.reply(200, {"access_token": TS_TOKEN, "expires_in": 3600})
            auth = self.headers.get("Authorization")
            if path.endswith("/user-invites"):
                if auth != f"Bearer {USER_TOKEN}" or not fake.user_token_ok:
                    return self.reply(401, {"message": "token expired"})
                return self.reply(200, [{"id": "i1", "inviteUrl": INVITE_URL}])
            if "/keys/" in path:
                if auth != f"Bearer {USER_TOKEN}":
                    return self.reply(401, {"message": "bad token"})
                if path.rsplit("/", 1)[1] != "k123CNTRL":
                    return self.reply(404, {"message": "no such key"})
                return self.reply(fake.key_status, fake.key)
            if auth != f"Bearer {TS_TOKEN}":
                return self.reply(401, {"message": "unauthorized"})
            if path.endswith("/suspend") and method == "POST":
                uid = path.split("/")[-2]
                user = next((u for u in fake.users if u.get("id") == uid), None)
                if user is None:
                    return self.reply(404, {"message": "no such user"})
                user["status"] = "suspended"
                fake.suspended.append(uid)
                return self.reply(200, {})
            if path.endswith("/users"):
                if fake.users_status != 200:
                    return self.reply(fake.users_status, {"message": "scope"})
                return self.reply(200, {"users": fake.users})
            if path.endswith("/acl/validate"):
                if fake.validate_error:
                    return self.reply(
                        400, {"message": fake.validate_error, "data": [{"user": "x"}]}
                    )
                return self.reply(200, {})
            if path.endswith("/acl") and method == "GET":
                assert self.headers.get("Accept") == "application/hujson"
                headers = {} if fake.no_etag else {"ETag": fake.etag}
                return self.reply(200, fake.policy, headers, ctype="application/hujson")
            if path.endswith("/acl") and method == "POST":
                assert self.headers.get("Content-Type") == "application/hujson"
                if fake.conflicts:
                    fake.conflicts -= 1
                    fake.etag_n += 1  # a rival edit landed meanwhile
                if self.headers.get("If-Match") != fake.etag:
                    return self.reply(412, {"message": "precondition failed"})
                fake.policy = raw.decode()
                fake.etag_n += 1
                return self.reply(200, {}, ctype="application/hujson")
            return self.reply(404, {"message": "nope"})

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

    return Handler


@pytest.fixture
def env(tmp_path):
    logto = FakeLogto()
    ts = FakeTailscale()
    servers = []
    for handler in (handler_for(logto), ts_handler(ts)):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    outputs: list[str] = []

    def run(*args, tailnet_env=True, **extra):
        e = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "LOGTO_ENDPOINT": f"http://127.0.0.1:{servers[0].server_port}",
            "LOGTO_ADMIN_LEDGER": str(tmp_path / "ledger.jsonl"),
            "LOGTO_M2M_APP_ID": APP_ID,
            "LOGTO_M2M_APP_SECRET": APP_SECRET,
        }
        if tailnet_env:
            e |= {
                "TAILSCALE_API_URL": f"http://127.0.0.1:{servers[1].server_port}",
                "TAILSCALE_OAUTH_CLIENT_ID": CLIENT_ID,
                "TAILSCALE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
                "TAILSCALE_USER_TOKEN": USER_TOKEN,
            }
        e |= extra
        proc = subprocess.run(
            ["python3", str(PEOPLE), *args],
            env=e,
            capture_output=True,
            text=True,
            check=False,
        )
        outputs.append(proc.stdout + proc.stderr)
        return proc

    run.logto, run.ts = logto, ts
    yield run
    for s in servers:
        s.shutdown()
    for out in outputs:
        for secret in (CLIENT_SECRET, TS_TOKEN, USER_TOKEN, APP_SECRET, TOKEN):
            assert secret not in out


def logto_writes(logto: FakeLogto):
    return [(m, p) for m, p, _ in logto.requests if m != "GET"]


def block_groups(policy: str) -> dict[str, list[str]]:
    return tailnet.managed_groups(policy)


def outside_block(policy: str) -> tuple[str, str]:
    head, _, _, tail = tailnet.split_block(policy)
    return head, tail


def give_role(logto: FakeLogto, user_id: str, role: str) -> None:
    rid = next((r["id"] for r in logto.roles if r["name"] == role), None)
    if rid is None:
        rid = f"r-{role}"
        logto.roles.append({"id": rid, "name": role, "type": "User"})
    logto.user_roles.setdefault(user_id, []).append(rid)


# ─── the policy text ─────────────────────────────────────────────────────────


def test_strip_hujson_keeps_strings_drops_comments_and_trailing_commas():
    text = '{"a": "x // not a comment, ]", /* c */ "b": [1, 2,], // tail\n}'
    assert json.loads(tailnet.strip_hujson(text)) == {
        "a": "x // not a comment, ]",
        "b": [1, 2],
    }


def test_block_edit_leaves_every_other_byte_alone():
    new = tailnet.with_groups(
        POLICY,
        {"group:backstage": ["a@example.com", "A@example.com"], "group:x": []},
        ["group:finance-sentry", "group:backstage"],
    )
    assert outside_block(new) == outside_block(POLICY)
    assert block_groups(new) == {
        "group:backstage": ["A@example.com", "a@example.com"],
        "group:x": [],
    }
    assert tailnet.parse_policy(new)["groups"]["group:ops"] == ["ops@example.com"]


def test_markers_must_be_one_pair_and_block_lines_must_parse():
    with pytest.raises(tailnet.TailnetError, match="marker pair"):
        tailnet.split_block(POLICY.replace("BEGIN people-managed", "BEGIN other"))
    with pytest.raises(tailnet.TailnetError, match="marker pair"):
        tailnet.split_block(POLICY + POLICY)
    bad = POLICY.replace('"group:backstage": [],', "something odd")
    with pytest.raises(tailnet.TailnetError, match="line 2 of the people-managed"):
        tailnet.managed_groups(bad)


@pytest.mark.parametrize(
    "spec", ["tcp:3002", "3002", "tcp:3000-3100", "*", "tcp:*", "tcp:22,3002"]
)
def test_never_port_detection_covers_ranges_and_wildcards(spec):
    policy = {"grants": [{"src": ["group:backstage"], "ip": [spec]}]}
    with pytest.raises(tailnet.TailnetError, match="never grants"):
        tailnet.refuse_never_ports(policy, ["group:backstage"], [3002])


def test_a_grant_to_other_ports_or_other_groups_is_fine():
    policy = {
        "grants": [
            {"src": ["group:backstage"], "ip": ["tcp:18810", "udp:53"]},
            {"src": ["group:ops"], "ip": ["*"]},
        ]
    }
    tailnet.refuse_never_ports(policy, ["group:backstage"], [3002])


# ─── sync ────────────────────────────────────────────────────────────────────


def test_sync_rebuilds_groups_from_logto_roles_and_is_idempotent(env):
    env("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    give_role(env.logto, "u2", "finance-sentry")
    give_role(env.logto, "u2", "backstage")
    proc = env("sync")
    assert proc.returncode == 0, proc.stderr
    assert "group:backstage: +member@example.com" in proc.stdout
    assert "tailnet policy written" in proc.stdout
    assert block_groups(env.ts.policy) == {
        "group:finance-sentry": ["member@example.com"],
        "group:backstage": ["friend@example.com", "member@example.com"],
    }
    assert outside_block(env.ts.policy) == outside_block(POLICY)
    writes = len(env.ts.acl_writes())
    again = env("sync")
    assert again.returncode == 0, again.stderr
    assert "nothing written" in again.stdout
    assert len(env.ts.acl_writes()) == writes


def test_sync_removes_a_dropped_role_and_a_suspended_user(env):
    env("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    env("sync")
    assert "friend@example.com" in env.ts.policy
    env.logto.users[-1]["isSuspended"] = True
    proc = env("sync")
    assert "group:backstage: -friend@example.com" in proc.stdout
    assert block_groups(env.ts.policy)["group:backstage"] == []


def test_sync_dry_run_writes_nothing_and_unknown_groups_are_left_alone(env):
    env.ts.policy = POLICY.replace(
        '"group:backstage": [],',
        '"group:backstage": [],\n    "group:old": ["x@example.com"],',
    )
    give_role(env.logto, "u2", "backstage")
    before = env.ts.policy
    proc = env("sync", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "would change group:backstage: +member@example.com" in proc.stdout
    assert "group:old: in the managed block but not in the catalog" in proc.stdout
    assert env.ts.policy == before and env.ts.acl_writes() == []
    env("sync")
    assert block_groups(env.ts.policy)["group:old"] == ["x@example.com"]


def test_sync_creates_a_missing_group_line_even_when_empty(env):
    env.ts.policy = POLICY.replace('    "group:backstage": [],\n', "")
    proc = env("sync")
    assert proc.returncode == 0, proc.stderr
    assert "group:backstage: created (empty)" in proc.stdout
    assert block_groups(env.ts.policy)["group:backstage"] == []


def test_etag_conflict_rereads_and_retries_once(env):
    give_role(env.logto, "u2", "backstage")
    env.ts.conflicts = 1
    proc = env("sync")
    assert proc.returncode == 0, proc.stderr
    assert block_groups(env.ts.policy)["group:backstage"] == ["member@example.com"]
    gets = [r for r in env.ts.requests if r[0] == "GET" and r[1].endswith("/acl")]
    assert len(gets) == 2 and len(env.ts.acl_writes()) == 2


def test_a_second_conflict_gives_up(env):
    give_role(env.logto, "u2", "backstage")
    env.ts.conflicts = 2
    before = env.ts.policy
    proc = env("sync")
    assert proc.returncode == 1
    assert "changed twice" in proc.stderr
    assert env.ts.policy == before


def test_validation_failure_blocks_the_write(env):
    give_role(env.logto, "u2", "backstage")
    env.ts.validate_error = "test(s) failed"
    proc = env("sync")
    assert proc.returncode == 1
    assert "test(s) failed" in proc.stderr
    assert env.ts.policy == POLICY and env.ts.acl_writes() == []


def test_a_policy_without_an_etag_is_refused(env):
    env.ts.no_etag = True
    proc = env("sync")
    assert proc.returncode == 1
    assert "no ETag" in proc.stderr


@pytest.mark.parametrize("spec", ["tcp:3002", "tcp:3000-3100", "*"])
def test_a_grant_on_a_never_port_refuses_sync_and_invite(env, spec):
    env.ts.policy = POLICY.replace(
        '"ip": ["tcp:18810", "tcp:3001"]', f'"ip": ["tcp:18810", "{spec}"]'
    )
    give_role(env.logto, "u2", "backstage")
    sync = env("sync")
    assert sync.returncode == 1 and "never grants" in sync.stderr
    invite = env("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    assert invite.returncode == 1 and "never grants" in invite.stderr
    assert env.ts.acl_writes() == []
    assert logto_writes(env.logto) == []  # refused before any Logto write


def test_missing_markers_refuse_before_any_logto_write(env):
    env.ts.policy = POLICY.replace("BEGIN people-managed", "BEGIN nothing")
    proc = env("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    assert proc.returncode == 1
    assert "one-time operator step" in proc.stderr
    assert logto_writes(env.logto) == []


# ─── invite ──────────────────────────────────────────────────────────────────


def test_invite_adds_the_email_to_its_group_and_yields_the_invite_url(env):
    proc = env("invite", "Friend@Example.com", "--right", "backstage", "--host", HOST)
    assert proc.returncode == 0, proc.stderr
    assert block_groups(env.ts.policy) == {
        "group:finance-sentry": [],
        "group:backstage": ["friend@example.com"],
    }
    assert outside_block(env.ts.policy) == outside_block(POLICY)
    assert "tailnet group:backstage: added" in proc.stdout
    assert env.ts.invite_bodies() == [b'[{"role": "member"}]']  # no email field
    assert INVITE_URL in proc.stdout
    assert "PASTE" not in proc.stdout
    assert f"https://{HOST}:18810" in proc.stdout


def test_invite_is_idempotent_for_a_member(env):
    args = ("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    assert env(*args).returncode == 0
    env.ts.users.append(
        {"loginName": "friend@example.com", "role": "member", "status": "active"}
    )
    writes, invites = len(env.ts.acl_writes()), len(env.ts.invite_bodies())
    again = env(*args)
    assert again.returncode == 0, again.stderr
    assert "already a member" in again.stdout
    assert len(env.ts.acl_writes()) == writes
    assert len(env.ts.invite_bodies()) == invites
    assert "already on the tailnet as friend@example.com" in again.stdout
    assert "PASTE" not in again.stdout


def test_invite_without_the_user_token_prints_the_paste_instruction(env):
    proc = env(
        "invite",
        "friend@example.com",
        "--right",
        "backstage",
        "--host",
        HOST,
        TAILSCALE_USER_TOKEN="",
    )
    assert proc.returncode == 0, proc.stderr
    assert "friend@example.com" in env.ts.policy  # the group edit still happened
    assert "create an invite link in the admin console and paste it" in proc.stdout
    assert env.ts.invite_bodies() == []
    assert "[PASTE THE TAILSCALE INVITE LINK HERE]" in proc.stdout


def test_an_expired_user_token_falls_back_to_the_paste_instruction(env):
    env.ts.user_token_ok = False
    proc = env("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    assert proc.returncode == 0, proc.stderr
    assert "HTTP 401" in proc.stdout
    assert "create an invite link in the admin console and paste it" in proc.stdout
    assert "PASTE THE TAILSCALE INVITE LINK" in proc.stdout


def test_a_passed_invite_url_is_used_and_no_invite_is_created(env):
    proc = env(
        "invite",
        "friend@example.com",
        "--right",
        "backstage",
        "--host",
        HOST,
        "--invite-url",
        "https://login.tailscale.example/uinv/mine",
    )
    assert proc.returncode == 0, proc.stderr
    assert env.ts.invite_bodies() == []
    assert "https://login.tailscale.example/uinv/mine" in proc.stdout


def test_invite_two_rights_joins_both_groups(env):
    proc = env(
        "invite",
        "friend@example.com",
        "--right",
        "finance-sentry",
        "--right",
        "backstage",
        "--host",
        HOST,
    )
    assert proc.returncode == 0, proc.stderr
    groups = block_groups(env.ts.policy)
    assert groups["group:finance-sentry"] == ["friend@example.com"]
    assert groups["group:backstage"] == ["friend@example.com"]


def test_no_tailnet_flag_is_the_logto_only_invite(env):
    proc = env(
        "invite",
        "friend@example.com",
        "--right",
        "backstage",
        "--host",
        HOST,
        "--no-tailnet",
    )
    assert proc.returncode == 0, proc.stderr
    assert env.ts.requests == []


# ─── not configured ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        ("sync",),
        ("status",),
        ("invite", "friend@example.com", "--right", "backstage", "--host", HOST),
    ],
)
def test_without_the_oauth_client_tailnet_steps_skip_cleanly(env, args):
    proc = env(*args, tailnet_env=False)
    assert proc.returncode == 0, proc.stderr
    assert "tailnet: not configured" in proc.stdout
    assert env.ts.requests == []


def test_a_user_token_alone_is_not_enough_to_edit_the_policy(env):
    proc = env("sync", tailnet_env=False, TAILSCALE_USER_TOKEN=USER_TOKEN)
    assert proc.returncode == 0
    assert "not configured" in proc.stdout and env.ts.requests == []


# ─── status ──────────────────────────────────────────────────────────────────


def test_status_shows_tailnet_membership_groups_and_drift(env):
    env("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    env.ts.users += [
        {
            "loginName": "friend@example.com",
            "role": "member",
            "status": "idle",
            "deviceCount": 1,
            "lastSeen": "2026-10-15T10:00:00Z",
        },
        {"loginName": "stranger@gmail.example", "role": "member", "status": "active"},
    ]
    give_role(env.logto, "u2", "finance-sentry")  # a role with no group yet
    proc = env("status")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert (
        "  tailnet: member (idle, 1 devices, last seen 2026-10-15), "
        "groups [group:backstage]" in out
    )
    assert "tailnet: not a member" in out  # member@example.com
    assert "member@example.com: holds role finance-sentry but is not in" in out
    assert "tailnet member stranger@gmail.example matches no Logto user" in out
    assert "owner@example.com matches no Logto user" not in out  # the tailnet owner
    assert "no grant" not in out and "no tests block" not in out


def test_status_flags_group_without_role_missing_grants_and_missing_tests(env):
    env.ts.policy = (
        POLICY.replace(
            '"group:backstage": [],', '"group:backstage": ["member@example.com"],'
        )
        .replace(
            '{ "src": ["group:finance-sentry"], "dst": ["box"], "ip": ["tcp:4200", "tcp:3001"] },\n',
            "",
        )
        .replace('"tests": [', '"unused": [')
    )
    proc = env("status", "member@example.com")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "is in group:backstage without an active backstage role" in out
    assert "no grant lets group:finance-sentry reach tcp:4200" in out
    assert "no grant lets group:finance-sentry reach tcp:3001" in out
    assert "no grant lets group:backstage" not in out
    assert "the policy has no tests block" in out


def test_status_survives_a_missing_read_scope(env):
    env.ts.users_status = 403
    proc = env("status", "member@example.com")
    assert proc.returncode == 0, proc.stderr
    assert "tailnet: users unavailable: list tailnet users: HTTP 403" in proc.stdout
    assert "groups [-]" in proc.stdout
