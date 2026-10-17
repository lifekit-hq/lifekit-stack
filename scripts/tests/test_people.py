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
