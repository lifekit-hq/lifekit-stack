"""scripts/people/people.py against the fake Logto Management API.

The fake comes from test_logto_admin.py. These tests cover the invite flow
(user, roles, idempotency, ledger), the refusal of unknown rights before any
write, the rendered email, and status with its drift flags.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from test_logto_admin import (
    APP_ID,
    APP_SECRET,
    TOKEN,
    FakeLogto,
    handler_for,
    ledger_entries,
)

REPO = Path(__file__).resolve().parents[2]
PEOPLE = REPO / "scripts/people/people.py"
HOST = "box.example.ts.net"


@pytest.fixture
def people(tmp_path):
    fake = FakeLogto()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(fake))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    outputs: list[str] = []
    ledger = tmp_path / "ledger.jsonl"

    def run(*args, extra_env=None):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "LOGTO_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
            "LOGTO_ADMIN_LEDGER": str(ledger),
            "LOGTO_M2M_APP_ID": APP_ID,
            "LOGTO_M2M_APP_SECRET": APP_SECRET,
            **(extra_env or {}),
        }
        proc = subprocess.run(
            ["python3", str(PEOPLE), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        outputs.append(proc.stdout + proc.stderr)
        return proc

    run.fake = fake
    run.ledger = ledger
    yield run
    server.shutdown()
    for out in outputs:
        assert APP_SECRET not in out
        assert TOKEN not in out


def writes(fake: FakeLogto) -> list[tuple[str, str]]:
    return [(m, p) for m, p, _ in fake.requests if m != "GET"]


def test_catalog_has_the_agreed_shape():
    cat = json.loads((REPO / "scripts/people/rights.json").read_text())
    rights = cat["rights"]
    assert (rights["finance-sentry"]["port"], rights["finance-sentry"]["role"]) == (
        4200,
        "finance-sentry",
    )
    assert (rights["backstage"]["port"], rights["backstage"]["role"]) == (
        18810,
        "backstage",
    )
    assert cat["always_ports"] == [3001]
    assert cat["never_ports"] == [3002, 4387, 18789, 8303, 22]
    assert not {r["port"] for r in rights.values()} & set(cat["never_ports"])


def test_missing_credentials_is_usage_error(tmp_path):
    proc = subprocess.run(
        ["python3", str(PEOPLE), "status"],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "LOGTO_M2M_APP_ID" in proc.stderr


def test_invite_creates_user_roles_and_prints_email(people):
    proc = people(
        "invite", "Friend@Example.com", "--right", "backstage", "--host", HOST
    )
    assert proc.returncode == 0, proc.stderr
    user = people.fake.users[-1]
    assert user["primaryEmail"] == "friend@example.com"
    assert "password" not in user
    assert [r["name"] for r in people.fake.roles] == ["backstage"]
    assert people.fake.user_roles[user["id"]] == [people.fake.roles[0]["id"]]
    assert [e["action"] for e in ledger_entries(people)] == [
        "create-user",
        "create-role",
        "assign-role",
    ]
    out = proc.stdout
    assert "--- invite email" in out
    assert f"https://{HOST}:18810" in out
    assert "4200" not in out
    assert "friend@example.com" in out
    assert "[PASTE THE TAILSCALE INVITE LINK HERE]" in out
    assert "{{" not in out
    assert "Use Tailscale DNS" in out


def test_invite_is_idempotent(people):
    args = ("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    assert people(*args).returncode == 0
    before = writes(people.fake)
    again = people(*args)
    assert again.returncode == 0, again.stderr
    assert writes(people.fake) == before
    assert "already held" in again.stdout
    assert len(people.fake.users) == 3


def test_invite_two_rights_and_invite_url(people):
    proc = people(
        "invite",
        "friend@example.com",
        "--right",
        "finance-sentry",
        "--right",
        "backstage",
        "--host",
        HOST,
        "--invite-url",
        "https://login.tailscale.example/invite/abc",
    )
    assert proc.returncode == 0, proc.stderr
    assert {r["name"] for r in people.fake.roles} == {"finance-sentry", "backstage"}
    uid = people.fake.users[-1]["id"]
    assert len(people.fake.user_roles[uid]) == 2
    assert f"https://{HOST}:4200" in proc.stdout
    assert f"https://{HOST}:18810" in proc.stdout
    assert "https://login.tailscale.example/invite/abc" in proc.stdout
    assert "PASTE" not in proc.stdout
    assert "Finance Sentry and Backstage" in proc.stdout


def test_unknown_right_is_refused_before_any_write(people):
    proc = people(
        "invite",
        "friend@example.com",
        "--right",
        "backstage",
        "--right",
        "nope",
        "--host",
        HOST,
    )
    assert proc.returncode == 1
    assert "unknown right nope" in proc.stderr
    assert writes(people.fake) == []
    assert not people.ledger.exists()


def test_invite_needs_a_host(people):
    proc = people("invite", "friend@example.com", "--right", "backstage")
    assert proc.returncode == 1
    assert "PEOPLE_HOST" in proc.stderr
    assert writes(people.fake) == []
    ok = people(
        "invite",
        "friend@example.com",
        "--right",
        "backstage",
        extra_env={"PEOPLE_HOST": HOST},
    )
    assert ok.returncode == 0, ok.stderr


def test_invite_writes_undo_data_logto_admin_can_replay(people):
    people("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    user = people.fake.users[-1]
    entries = ledger_entries(people)
    assert entries[0]["undo"]["path"] == f"/api/users/{user['id']}"
    assert entries[2]["undo"]["path"].startswith(f"/api/users/{user['id']}/roles/")


def test_status_shows_roles_suspension_and_drift(people):
    people("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    people.fake.roles.append({"id": "radmin", "name": "admin", "type": "User"})
    people.fake.user_roles["u1"] = ["radmin"]  # owner: platform role only, no drift
    proc = people("status")
    assert proc.returncode == 0, proc.stderr
    assert "friend@example.com: roles [backstage]" in proc.stdout
    assert "owner@example.com: roles [admin]" in proc.stdout
    assert "member@example.com: roles [-]" in proc.stdout
    assert "member@example.com: no product role" in proc.stdout
    assert "owner@example.com: no product role" not in proc.stdout
    assert "friend@example.com: no product role" not in proc.stdout


def test_status_one_user_and_suspended(people):
    people("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    people.fake.users[-1]["isSuspended"] = True
    proc = people("status", "friend@example.com")
    assert proc.returncode == 0, proc.stderr
    assert "friend@example.com: roles [backstage], SUSPENDED" in proc.stdout
    assert "suspended but still holds a product role" in proc.stdout
    assert "owner@example.com" not in proc.stdout
    assert people("status", "ghost@example.com").returncode == 1


# --- mail: --send / --resend against a local fake SMTP server -------------

import socket  # noqa: E402
import ssl  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, str(REPO / "scripts/people"))
import mailer  # noqa: E402

PASSWORD = "abcdwxyzabcdwxyz"


class FakeSmtp:
    """A stdlib-socket SMTP server: EHLO, STARTTLS, AUTH PLAIN/LOGIN, one message."""

    def __init__(self, tls_ctx=None, fail_auth=False):
        self.tls_ctx = tls_ctx
        self.fail_auth = fail_auth
        self.messages: list[dict] = []
        self.logins: list[str] = []
        self.saw_starttls = False
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                self.session(conn)
            except (OSError, ssl.SSLError):
                pass
            finally:
                conn.close()

    def session(self, conn):
        f = conn.makefile("rwb", buffering=0)

        def out(line):
            f.write(line.encode() + b"\r\n")

        def read():
            return f.readline().decode().rstrip("\r\n")

        out("220 fake ESMTP")
        msg = {"to": [], "from": "", "data": ""}
        while True:
            line = read()
            if not line:
                return
            cmd = line.split(" ", 1)[0].upper()
            if cmd == "EHLO":
                out("250-fake")
                if self.tls_ctx and not self.saw_starttls:
                    out("250-STARTTLS")
                out("250 AUTH PLAIN LOGIN")
            elif cmd == "STARTTLS":
                out("220 go ahead")
                conn = self.tls_ctx.wrap_socket(conn, server_side=True)
                f = conn.makefile("rwb", buffering=0)
                self.saw_starttls = True
            elif cmd == "AUTH":
                self.logins.append(line)
                if self.fail_auth:
                    out("535 5.7.8 bad credentials")
                else:
                    out("235 ok")
            elif cmd == "MAIL":
                msg["from"] = line
                out("250 ok")
            elif cmd == "RCPT":
                msg["to"].append(line)
                out("250 ok")
            elif cmd == "DATA":
                out("354 end with .")
                lines = []
                while (row := read()) != ".":
                    lines.append(row)
                msg["data"] = "\n".join(lines)
                self.messages.append(msg)
                out("250 queued")
            elif cmd == "QUIT":
                out("221 bye")
                return
            else:
                out("250 ok")


@pytest.fixture(scope="module")
def tls_ctx(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem"),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "c.pem", d / "k.pem")
    client = ssl.create_default_context(cafile=str(d / "c.pem"))
    return ctx, client, str(d / "c.pem")


def smtp_env(srv, ca=None, **over):
    return {
        **({"PEOPLE_SMTP_CA_FILE": ca} if ca else {}),
        "PEOPLE_SMTP_USER": "owner@example.com",
        "PEOPLE_SMTP_PASSWORD": PASSWORD,
        "PEOPLE_SMTP_HOST": "127.0.0.1",
        "PEOPLE_SMTP_PORT": str(srv.port),
        **over,
    }


def test_mailer_starttls_auth_and_message(tls_ctx):
    server_ctx, client_ctx, _ = tls_ctx
    srv = FakeSmtp(tls_ctx=server_ctx)
    cfg = mailer.settings(
        {
            "PEOPLE_SMTP_USER": "owner@example.com",
            "PEOPLE_SMTP_PASSWORD": "abcd wxyz abcd wxyz",
            "PEOPLE_SMTP_HOST": "127.0.0.1",
            "PEOPLE_SMTP_PORT": str(srv.port),
            "PEOPLE_SMTP_FROM_NAME": "The Owner",
        }
    )
    assert cfg["password"] == PASSWORD
    mailer.send(cfg, "friend@example.com", "Hello", "Body line", context=client_ctx)
    assert srv.saw_starttls
    assert len(srv.logins) == 1
    (m,) = srv.messages
    assert "friend@example.com" in m["to"][0]
    assert "Subject: Hello" in m["data"]
    assert "The Owner" in m["data"]
    assert "Body line" in m["data"]


def test_mailer_without_password_is_unconfigured():
    assert mailer.settings({"PEOPLE_SMTP_USER": "a@example.com"}) is None
    assert mailer.settings({"PEOPLE_SMTP_PASSWORD": "x"}) is None
    assert (
        mailer.settings({"PEOPLE_SMTP_USER": "a@b", "PEOPLE_SMTP_PASSWORD": " "})
        is None
    )


def test_mailer_failure_hides_the_password():
    srv = FakeSmtp()  # no STARTTLS offered
    cfg = mailer.settings(smtp_env(srv))
    with pytest.raises(mailer.MailError) as err:
        mailer.send(cfg, "friend@example.com", "s", "b")
    assert PASSWORD not in str(err.value)


INVITE = ("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
LINK = "https://login.tailscale.example/invite/abc"


def test_send_without_app_password_prints_the_draft_and_exits_0(people):
    proc = people(*INVITE, "--send", "--invite-url", LINK)
    assert proc.returncode == 0, proc.stderr
    assert "mail not configured" in proc.stdout
    assert "--- invite email (draft" in proc.stdout
    assert LINK in proc.stdout
    assert people.fake.users[-1]["primaryEmail"] == "friend@example.com"


def test_plain_invite_never_sends(people, tls_ctx):
    srv = FakeSmtp(tls_ctx=tls_ctx[0])
    proc = people(*INVITE, "--invite-url", LINK, extra_env=smtp_env(srv))
    assert proc.returncode == 0, proc.stderr
    assert "draft; send it yourself" in proc.stdout
    assert srv.messages == [] and srv.logins == []


def test_send_without_a_link_does_not_mail_a_placeholder(people, tls_ctx):
    srv = FakeSmtp(tls_ctx=tls_ctx[0])
    proc = people(*INVITE, "--send", extra_env=smtp_env(srv))
    assert proc.returncode == 0, proc.stderr
    assert "not sent" in proc.stdout and "--invite-url" in proc.stdout
    assert srv.messages == []


def test_send_mails_one_email_for_the_chosen_rights(people, tls_ctx):
    srv = FakeSmtp(tls_ctx=tls_ctx[0])
    proc = people(
        *INVITE, "--send", "--invite-url", LINK, extra_env=smtp_env(srv, tls_ctx[2])
    )
    assert proc.returncode == 0, proc.stderr
    assert "invite email sent to friend@example.com" in proc.stdout
    assert "--- invite email" not in proc.stdout
    assert PASSWORD not in proc.stdout + proc.stderr
    (m,) = srv.messages
    assert "friend@example.com" in m["to"][0]
    assert "Subject: You're invited to Backstage" in m["data"]
    assert f"https://{HOST}:18810" in m["data"]
    assert "4200" not in m["data"]
    assert LINK in m["data"]
    assert "{{" not in m["data"] and "Use Tailscale DNS" in m["data"]


def test_send_failure_exits_1_after_the_invite(people, tls_ctx):
    srv = FakeSmtp(tls_ctx=tls_ctx[0], fail_auth=True)
    proc = people(
        *INVITE, "--send", "--invite-url", LINK, extra_env=smtp_env(srv, tls_ctx[2])
    )
    assert proc.returncode == 1
    assert "sending to friend@example.com failed" in proc.stderr
    assert PASSWORD not in proc.stdout + proc.stderr
    assert srv.messages == []
    assert people.fake.users[-1]["primaryEmail"] == "friend@example.com"


def test_resend_requires_an_existing_invite_and_writes_nothing(people):
    proc = people(*INVITE, "--resend", "--invite-url", LINK)
    assert proc.returncode == 1
    assert "no user with" in proc.stderr
    assert writes(people.fake) == []
    assert people(*INVITE).returncode == 0
    before = writes(people.fake)
    wrong = people(
        "invite", "friend@example.com", "--right", "finance-sentry",
        "--host", HOST, "--resend", "--invite-url", LINK,
    )  # fmt: skip
    assert wrong.returncode == 1
    assert "does not hold finance-sentry" in wrong.stderr
    assert writes(people.fake) == before


def test_resend_mails_again_and_writes_nothing(people, tls_ctx):
    assert people(*INVITE).returncode == 0
    before = writes(people.fake)
    srv = FakeSmtp(tls_ctx=tls_ctx[0])
    proc = people(
        *INVITE, "--resend", "--invite-url", LINK, extra_env=smtp_env(srv, tls_ctx[2])
    )
    assert proc.returncode == 0, proc.stderr
    assert len(srv.messages) == 1
    assert writes(people.fake) == before


def test_send_and_resend_are_exclusive(people):
    proc = people(*INVITE, "--send", "--resend")
    assert proc.returncode == 2
