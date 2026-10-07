"""scripts/identity/logto-admin.py against a fake Logto Management API.

The fake keeps roles, users, applications and secrets in memory and checks
the client-credentials token, so these tests cover the request shapes, the
idempotency, the ledger and undo, and that no secret reaches the output.
scripts/identity/logto-m2m-bootstrap.sh is covered for its argument and
container checks with a stubbed `docker`; its SQL runs only against a real
Postgres, in scripts/identity/rehearse-logto-admin.sh.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ADMIN = REPO / "scripts/identity/logto-admin.py"
BOOTSTRAP = REPO / "scripts/identity/logto-m2m-bootstrap.sh"

APP_ID = "m2mappid"
APP_SECRET = "m2m-secret-value-0123456789abcdef"
TOKEN = "access-token-value-zyx"
GATE_SECRET = "gate-secret-value-abcdefghijklmnop"
SMTP_PASSWORD = "abcdefghijklmnop"
SMTP_PASSWORD_AS_SHOWN = "abcd efgh ijkl mnop"


class FakeLogto:
    def __init__(self) -> None:
        self.roles: list[dict] = []
        self.users = [
            {"id": "u1", "primaryEmail": "owner@example.com", "username": "owner"},
            {"id": "u2", "primaryEmail": "member@example.com", "username": "member"},
        ]
        self.user_roles: dict[str, list[str]] = {}
        self.apps: list[dict] = []
        self.secrets: dict[str, list[dict]] = {}
        self.sign_in_exp = {
            "id": "default",
            "color": {
                "primaryColor": "#6139F6",
                "isDarkModeEnabled": False,
                "darkPrimaryColor": "#9F7AFF",
            },
            "branding": {"logoUrl": "https://old/logo.png"},
            "signInMode": "SignInAndRegister",
            "signIn": {
                "methods": [
                    {
                        "identifier": "username",
                        "password": True,
                        "verificationCode": False,
                        "isPasswordPrimary": True,
                    }
                ]
            },
            "signUp": {"identifiers": ["username"], "password": True, "verify": False},
            "socialSignInConnectorTargets": ["google"],
            "socialSignIn": {"automaticAccountLinking": False},
        }
        self.app_sie: dict[str, dict] = {}
        self.connectors: list[dict] = []
        self.test_sends: list[dict] = []
        self.requests: list[tuple[str, str, object]] = []
        self.seq = 0

    def next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}{self.seq}"


def handler_for(fake: FakeLogto):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def reply(self, status: int, body=None, headers=None) -> None:
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def page(self, items: list, query: dict) -> None:
            page = int(query.get("page", ["1"])[0])
            size = int(query.get("page_size", ["20"])[0])
            chunk = items[(page - 1) * size : page * size]
            self.reply(200, chunk, {"Total-Number": str(len(items))})

        def handle_any(self, method: str) -> None:
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            raw = self.body()
            parts = [p for p in url.path.split("/") if p]
            if url.path == "/oidc/token":
                form = urllib.parse.parse_qs(raw.decode())
                want = base64.b64encode(f"{APP_ID}:{APP_SECRET}".encode()).decode()
                ok = self.headers.get("Authorization") == f"Basic {want}"
                good_form = form.get("grant_type") == [
                    "client_credentials"
                ] and form.get("resource") == ["https://default.logto.app/api"]
                if not (ok and good_form):
                    return self.reply(
                        400, {"error": "invalid_client", "error_description": "bad"}
                    )
                return self.reply(
                    200,
                    {
                        "access_token": TOKEN,
                        "expires_in": 3600,
                        "token_type": "Bearer",
                        "scope": "all",
                    },
                )
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self.reply(401, {"code": "auth.unauthorized", "message": "no"})
            data = json.loads(raw) if raw else None
            fake.requests.append((method, url.path, data))
            if parts == ["api", "sign-in-exp"]:
                if method == "GET":
                    return self.reply(200, fake.sign_in_exp)
                if method == "PATCH":
                    bad = set(data) - {
                        "color",
                        "branding",
                        "signIn",
                        "signUp",
                        "termsOfUseUrl",
                        "privacyPolicyUrl",
                    }
                    if bad:
                        return self.reply(
                            400, {"code": "guard.invalid_input", "message": str(bad)}
                        )
                    # Logto refuses a code sign-in with no connector for it.
                    coded = [
                        m["identifier"]
                        for m in data.get("signIn", {}).get("methods", [])
                        if m.get("verificationCode")
                    ]
                    if "email" in coded and not any(
                        c["type"] == "Email" for c in fake.connectors
                    ):
                        return self.reply(
                            400,
                            {
                                "code": "sign_in_experiences.enabled_connector_not_found",
                                "message": "no email connector",
                            },
                        )
                    fake.sign_in_exp.update(data)
                    return self.reply(200, fake.sign_in_exp)
            if parts[:2] == ["api", "connectors"]:
                if len(parts) == 2 and method == "GET":
                    return self.reply(200, fake.connectors)
                if len(parts) == 2 and method == "POST":
                    if data["connectorId"] != "simple-mail-transfer-protocol":
                        return self.reply(
                            422, {"code": "connector.not_found", "message": "x"}
                        )
                    if any(c["type"] == "Email" for c in fake.connectors):
                        return self.reply(
                            422, {"code": "connector.more_than_one", "message": "x"}
                        )
                    need = {"SignIn", "Register", "ForgotPassword", "Generic"}
                    got = {t["usageType"] for t in data["config"].get("templates", [])}
                    if not need <= got:
                        return self.reply(
                            400, {"code": "connector.invalid_config", "message": "t"}
                        )
                    conn = {
                        "id": fake.next_id("c"),
                        "type": "Email",
                        "connectorId": data["connectorId"],
                        "config": data["config"],
                    }
                    fake.connectors.append(conn)
                    return self.reply(200, conn)
                if len(parts) == 3 and parts[2] != "simple-mail-transfer-protocol":
                    conn = next(
                        (c for c in fake.connectors if c["id"] == parts[2]), None
                    )
                    if conn is None:
                        return self.reply(
                            404, {"code": "entity.not_found", "message": "gone"}
                        )
                    if method == "PATCH":
                        conn["config"] = data["config"]
                        return self.reply(200, conn)
                    if method == "DELETE":
                        fake.connectors.remove(conn)
                        return self.reply(204)
                if len(parts) == 4 and parts[3] == "test" and method == "POST":
                    fake.test_sends.append(data)
                    return self.reply(204)
            if parts[:2] == ["api", "roles"]:
                if method == "GET":
                    return self.page(fake.roles, query)
                if method == "POST":
                    if "description" not in data:
                        return self.reply(
                            400,
                            {"code": "guard.invalid_input", "message": "description"},
                        )
                    role = {"id": fake.next_id("r"), **data}
                    fake.roles.append(role)
                    return self.reply(200, role)
                if method == "DELETE":
                    fake.roles = [r for r in fake.roles if r["id"] != parts[2]]
                    for ids in fake.user_roles.values():
                        if parts[2] in ids:
                            ids.remove(parts[2])
                    return self.reply(204)
            if parts[:2] == ["api", "users"]:
                if len(parts) == 2:
                    return self.page(fake.users, query)
                uid = parts[2]
                have = fake.user_roles.setdefault(uid, [])
                if method == "GET":
                    return self.page([r for r in fake.roles if r["id"] in have], query)
                if method == "POST":
                    have.extend(i for i in data["roleIds"] if i not in have)
                    return self.reply(201, {"roleIds": data["roleIds"]})
                if method == "DELETE":
                    have.remove(parts[4])
                    return self.reply(204)
            if (
                parts[:2] == ["api", "applications"]
                and parts[3:] == ["sign-in-experience"]
                and parts[2] in {a["id"] for a in fake.apps}
            ):
                if method == "GET":
                    if parts[2] not in fake.app_sie:
                        return self.reply(404, {"message": "no sign-in experience"})
                    return self.reply(200, fake.app_sie[parts[2]])
                if method == "PUT":
                    bad = set(data) - {"color", "branding", "displayName"}
                    if bad:
                        return self.reply(
                            400, {"code": "guard.invalid_input", "message": str(bad)}
                        )
                    created = parts[2] not in fake.app_sie
                    fake.app_sie[parts[2]] = {**fake.app_sie.get(parts[2], {}), **data}
                    return self.reply(201 if created else 200, fake.app_sie[parts[2]])
            if parts[:2] == ["api", "applications"]:
                if len(parts) == 2 and method == "GET":
                    return self.page(fake.apps, query)
                if len(parts) == 2 and method == "POST":
                    app = {"id": fake.next_id("a"), **data}
                    fake.apps.append(app)
                    fake.secrets[app["id"]] = [
                        {
                            "name": "Default secret",
                            "value": GATE_SECRET,
                            "expiresAt": None,
                        }
                    ]
                    return self.reply(200, app)
                app = next((a for a in fake.apps if a["id"] == parts[2]), None)
                if app is None:
                    return self.reply(
                        404, {"code": "entity.not_found", "message": "gone"}
                    )
                if len(parts) == 4 and parts[3] == "secrets":
                    return self.reply(200, fake.secrets[app["id"]])
                if method == "PATCH":
                    meta = data["oidcClientMetadata"]
                    if (
                        "redirectUris" not in meta
                        or "postLogoutRedirectUris" not in meta
                    ):
                        return self.reply(
                            400, {"code": "guard.invalid_input", "message": "both"}
                        )
                    app["oidcClientMetadata"] = meta
                    return self.reply(200, app)
                if method == "DELETE":
                    fake.apps.remove(app)
                    return self.reply(204)
            return self.reply(404, {"code": "route", "message": f"{method} {url.path}"})

        def do_GET(self):  # noqa: N802
            self.handle_any("GET")

        def do_POST(self):  # noqa: N802
            self.handle_any("POST")

        def do_PATCH(self):  # noqa: N802
            self.handle_any("PATCH")

        def do_PUT(self):  # noqa: N802
            self.handle_any("PUT")

        def do_DELETE(self):  # noqa: N802
            self.handle_any("DELETE")

    return Handler


@pytest.fixture
def logto(tmp_path):
    fake = FakeLogto()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    outputs: list[str] = []
    ledger = tmp_path / "ledger.jsonl"

    def run(*args, secret=APP_SECRET, app_id=APP_ID, extra_env=None):
        env = {
            **(extra_env or {}),
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "LOGTO_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
            "LOGTO_ADMIN_LEDGER": str(ledger),
            "LOGTO_M2M_APP_ID": app_id,
            "LOGTO_M2M_APP_SECRET": secret,
        }
        proc = subprocess.run(
            ["python3", str(ADMIN), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        outputs.append(proc.stdout + proc.stderr)
        return proc

    run.fake = fake
    run.ledger = ledger
    run.outputs = outputs
    yield run
    server.shutdown()
    for out in outputs:
        for secret in (APP_SECRET, TOKEN, GATE_SECRET, SMTP_PASSWORD):
            assert secret not in out
    if ledger.exists():
        assert SMTP_PASSWORD not in ledger.read_text()


def ledger_entries(run) -> list[dict]:
    return [json.loads(line) for line in run.ledger.read_text().splitlines()]


def test_missing_credentials_is_usage_error(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    proc = subprocess.run(
        ["python3", str(ADMIN), "token-check"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "LOGTO_M2M_APP_ID" in proc.stderr


def test_token_check(logto):
    proc = logto("token-check")
    assert proc.returncode == 0, proc.stderr
    assert "scope all" in proc.stdout


def test_bad_secret_fails_without_echo(logto):
    proc = logto("token-check", secret="wrong-secret-value")
    assert proc.returncode == 1
    assert "token request failed: HTTP 400" in proc.stderr
    assert "wrong-secret-value" not in proc.stdout + proc.stderr


def test_ensure_role_is_get_or_create(logto):
    first = logto("ensure-role", "admin", "--description", "lifekit admin")
    assert first.returncode == 0, first.stderr
    assert "created" in first.stdout
    second = logto("ensure-role", "admin")
    assert second.returncode == 0
    assert "exists" in second.stdout
    assert [r["name"] for r in logto.fake.roles] == ["admin"]
    assert logto.fake.roles[0] == {
        "id": "r1",
        "name": "admin",
        "description": "lifekit admin",
        "type": "User",
    }
    assert [e["action"] for e in ledger_entries(logto)] == ["create-role"]


def test_roles_are_paged(logto):
    logto.fake.roles = [
        {"id": f"x{i}", "name": f"r{i}", "type": "User"} for i in range(250)
    ]
    proc = logto("ensure-role", "r240")
    assert proc.returncode == 0, proc.stderr
    assert "exists (x240)" in proc.stdout
    assert not logto.ledger.exists()


def test_assign_role_by_email_then_username(logto):
    logto("ensure-role", "admin")
    first = logto("assign-role", "owner@example.com", "admin")
    assert first.returncode == 0, first.stderr
    assert "assigned" in first.stdout
    second = logto("assign-role", "owner", "admin")
    assert "already has role admin" in second.stdout
    assert logto.fake.user_roles["u1"] == ["r1"]
    entry = ledger_entries(logto)[-1]
    assert entry["undo"] == {"method": "DELETE", "path": "/api/users/u1/roles/r1"}


def test_assign_role_unknown_user_or_role(logto):
    assert logto("assign-role", "nobody@example.com", "admin").returncode == 1
    proc = logto("assign-role", "owner", "admin")
    assert proc.returncode == 1
    assert "no role named 'admin'" in proc.stderr


def test_ensure_app_creates_with_uris_and_writes_secret(logto, tmp_path):
    secret_file = tmp_path / "gate.secret"
    args = (
        "ensure-app",
        "gate",
        "--redirect-uri",
        "https://h:18790/oauth2/callback",
        "--redirect-uri",
        "https://h:18791/oauth2/callback",
        "--post-logout-uri",
        "https://h:18790/",
    )
    proc = logto(*args, "--secret-file", str(secret_file))
    assert proc.returncode == 0, proc.stderr
    app = logto.fake.apps[0]
    assert app["type"] == "Traditional"
    assert app["oidcClientMetadata"] == {
        "redirectUris": [
            "https://h:18790/oauth2/callback",
            "https://h:18791/oauth2/callback",
        ],
        "postLogoutRedirectUris": ["https://h:18790/"],
    }
    assert secret_file.read_text() == GATE_SECRET
    assert (secret_file.stat().st_mode & 0o777) == 0o600

    again = logto(*args)
    assert again.returncode == 0
    assert "already set" in again.stdout
    assert len(logto.fake.apps) == 1
    assert [e["action"] for e in ledger_entries(logto)] == ["create-app"]

    refused = logto("ensure-app", "gate", "--secret-file", str(secret_file))
    assert refused.returncode == 1
    assert "not overwriting" in refused.stderr
    assert secret_file.read_text() == GATE_SECRET


def test_ensure_app_refuses_other_type(logto):
    logto.fake.apps.append(
        {
            "id": "m1",
            "name": "firstmate-ops",
            "type": "MachineToMachine",
            "oidcClientMetadata": {},
        }
    )
    proc = logto("ensure-app", "firstmate-ops")
    assert proc.returncode == 1
    assert "not Traditional" in proc.stderr


def test_set_redirects_patches_and_keeps_the_other_list(logto):
    logto.fake.apps.append(
        {
            "id": "g1",
            "name": "Grafana",
            "type": "Traditional",
            "oidcClientMetadata": {
                "redirectUris": [],
                "postLogoutRedirectUris": ["https://h:3000/login"],
                "backchannelLogoutSessionRequired": False,
            },
        }
    )
    proc = logto(
        "set-redirects",
        "Grafana",
        "--redirect-uri",
        "https://h:3000/login/generic_oauth",
    )
    assert proc.returncode == 0, proc.stderr
    patch = [r for r in logto.fake.requests if r[0] == "PATCH"]
    assert patch == [
        (
            "PATCH",
            "/api/applications/g1",
            {
                "oidcClientMetadata": {
                    "redirectUris": ["https://h:3000/login/generic_oauth"],
                    "postLogoutRedirectUris": ["https://h:3000/login"],
                    "backchannelLogoutSessionRequired": False,
                }
            },
        )
    ]
    entry = ledger_entries(logto)[-1]
    assert entry["before"]["redirectUris"] == []
    again = logto(
        "set-redirects", "g1", "--redirect-uri", "https://h:3000/login/generic_oauth"
    )
    assert "already set" in again.stdout
    assert len([r for r in logto.fake.requests if r[0] == "PATCH"]) == 1


def test_set_redirects_needs_a_list_and_an_app(logto):
    assert logto("set-redirects", "Grafana").returncode != 0
    proc = logto("set-redirects", "Grafana", "--redirect-uri", "https://h/x")
    assert proc.returncode == 1
    assert "no application" in proc.stderr


def test_undo_reverses_newest_first(logto):
    logto.fake.apps.append(
        {
            "id": "g1",
            "name": "Grafana",
            "type": "Traditional",
            "oidcClientMetadata": {"redirectUris": [], "postLogoutRedirectUris": []},
        }
    )
    logto("ensure-role", "admin")
    logto("assign-role", "owner", "admin")
    logto("set-redirects", "Grafana", "--redirect-uri", "https://h:3000/cb")
    logto("ensure-app", "gate")
    for _ in range(4):
        assert logto("undo").returncode == 0
    assert logto("undo").returncode == 1
    assert [a["name"] for a in logto.fake.apps] == ["Grafana"]
    assert logto.fake.apps[0]["oidcClientMetadata"]["redirectUris"] == []
    assert logto.fake.roles == []
    assert logto.fake.user_roles["u1"] == []
    undos = [e["of"] for e in ledger_entries(logto) if e["action"] == "undo"]
    assert undos == [4, 3, 2, 1]
    again = logto("undo", "2")
    assert again.returncode == 0
    assert "already undone" in again.stdout


SOCIAL_KEYS = ("signInMode", "socialSignInConnectorTargets", "socialSignIn")


def test_set_sign_in_exp_email_and_username(logto):
    fake = logto.fake.sign_in_exp
    untouched = {k: json.loads(json.dumps(fake[k])) for k in SOCIAL_KEYS}
    proc = logto(
        "set-sign-in-exp",
        "--sign-in-identifiers",
        "email",
        "username",
        "--sign-up-identifiers",
        "username",
    )
    assert proc.returncode == 0, proc.stderr
    assert [m["identifier"] for m in fake["signIn"]["methods"]] == ["email", "username"]
    assert all(
        m["password"] and m["isPasswordPrimary"] and not m["verificationCode"]
        for m in fake["signIn"]["methods"]
    )
    patch = [r for r in logto.fake.requests if r[0] == "PATCH"]
    assert [set(r[2]) for r in patch] == [{"signIn"}]
    assert {k: fake[k] for k in SOCIAL_KEYS} == untouched
    again = logto("set-sign-in-exp", "--sign-in-identifiers", "email", "username")
    assert "already set" in again.stdout
    assert len([r for r in logto.fake.requests if r[0] == "PATCH"]) == 1


def test_set_sign_in_exp_branding_color_merge_and_undo(logto):
    fake = logto.fake.sign_in_exp
    original = json.loads(json.dumps(fake))
    proc = logto(
        "set-sign-in-exp",
        "--logo-url",
        "https://h/logo.svg",
        "--dark-logo-url",
        "https://h/logo-dark.svg",
        "--favicon",
        "https://h/fav.ico",
        "--dark-favicon",
        "https://h/fav-dark.ico",
        "--primary-color",
        "#112233",
        "--dark-mode",
        "--sign-up-identifiers",
        "none",
    )
    assert proc.returncode == 0, proc.stderr
    assert fake["branding"] == {
        "logoUrl": "https://h/logo.svg",
        "darkLogoUrl": "https://h/logo-dark.svg",
        "favicon": "https://h/fav.ico",
        "darkFavicon": "https://h/fav-dark.ico",
    }
    # color is replaced whole by PATCH: unasked leaves must survive the merge
    assert fake["color"] == {
        "primaryColor": "#112233",
        "isDarkModeEnabled": True,
        "darkPrimaryColor": "#9F7AFF",
    }
    assert fake["signUp"] == {"identifiers": [], "password": True, "verify": False}
    entry = ledger_entries(logto)[-1]
    assert entry["before"]["branding"] == {
        "logoUrl": "https://old/logo.png",
        "darkLogoUrl": None,
        "favicon": None,
        "darkFavicon": None,
    }
    assert entry["before"]["color"] == {
        "primaryColor": "#6139F6",
        "isDarkModeEnabled": False,
    }
    assert logto("undo").returncode == 0
    assert fake == original


def test_set_sign_in_exp_accepts_svg_data_uri_and_undoes(logto):
    fake = logto.fake.sign_in_exp
    original = json.loads(json.dumps(fake))
    uri = "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode()
    proc = logto(
        "set-sign-in-exp",
        "--logo-url",
        uri,
        "--dark-logo-url",
        uri,
        "--favicon",
        uri,
        "--dark-favicon",
        uri,
    )
    assert proc.returncode == 0, proc.stderr
    assert fake["branding"] == {
        "logoUrl": uri,
        "darkLogoUrl": uri,
        "favicon": uri,
        "darkFavicon": uri,
    }
    assert logto("undo").returncode == 0
    assert fake == original


def test_clear_terms_links_empties_both_and_undoes(logto):
    fake = logto.fake.sign_in_exp
    fake["termsOfUseUrl"] = "https://media.tenor.com/x.gif"
    fake["privacyPolicyUrl"] = "https://media.tenor.com/y.gif"
    original = json.loads(json.dumps(fake))
    assert logto("set-sign-in-exp", "--clear-terms-links").returncode == 0
    assert fake["termsOfUseUrl"] is None
    assert fake["privacyPolicyUrl"] is None
    assert "already set" in logto("set-sign-in-exp", "--clear-terms-links").stdout
    assert logto("undo").returncode == 0
    assert fake == original


def test_set_app_sign_in_exp_creates_merges_and_undoes(logto):
    logto("ensure-app", "gate")
    app_id = logto.fake.apps[0]["id"]
    args = (
        "set-app-sign-in-exp",
        "gate",
        "--display-name",
        "lifekit",
        "--logo-url",
        "https://h/lk.svg",
        "--primary-color",
        "#4f46e5",
        "--dark-primary-color",
        "#8e9aff",
    )
    proc = logto(*args)
    assert proc.returncode == 0, proc.stderr
    assert logto.fake.app_sie[app_id] == {
        "displayName": "lifekit",
        "branding": {"logoUrl": "https://h/lk.svg"},
        "color": {"primaryColor": "#4f46e5", "darkPrimaryColor": "#8e9aff"},
    }
    assert "already set" in logto(*args).stdout
    # a later change keeps the leaves it did not ask for
    assert (
        logto("set-app-sign-in-exp", "gate", "--primary-color", "#000000").returncode
        == 0
    )
    assert logto.fake.app_sie[app_id]["color"] == {
        "primaryColor": "#000000",
        "darkPrimaryColor": "#8e9aff",
    }
    assert logto("undo").returncode == 0
    assert logto.fake.app_sie[app_id]["color"]["primaryColor"] == "#4f46e5"


def test_set_app_sign_in_exp_unknown_app(logto):
    assert logto("set-app-sign-in-exp", "nope", "--display-name", "x").returncode == 1
    proc = logto("set-app-sign-in-exp", "nope", "--display-name", "x", "--if-exists")
    assert proc.returncode == 0
    assert "skipped" in proc.stdout
    assert logto("set-app-sign-in-exp", "nope").returncode != 0
    assert (
        logto("set-app-sign-in-exp", "nope", "--primary-color", "red").returncode == 2
    )


def test_set_sign_in_exp_rejects_bad_input(logto):
    assert logto("set-sign-in-exp").returncode != 0
    assert logto("set-sign-in-exp", "--primary-color", "red").returncode == 2
    assert logto("set-sign-in-exp", "--logo-url", "javascript:x").returncode == 2
    for bad in (
        "data:text/html;base64,PHNjcmlwdD4=",
        "data:image/svg+xml,<svg/>",
        "data:image/png;base64,iVBORw0KGgo=",
        "data:image/svg+xml;base64,not base64!",
    ):
        assert logto("set-sign-in-exp", "--dark-favicon", bad).returncode == 2
    assert logto("set-sign-in-exp", "--sign-in-identifiers", "fax").returncode == 2
    assert not [r for r in logto.fake.requests if r[0] == "PATCH"]


def smtp(logto, *args, password=SMTP_PASSWORD_AS_SHOWN):
    return logto(
        "set-email-connector",
        "--user",
        "me@gmail.example",
        *args,
        extra_env={"LOGTO_SMTP_PASSWORD": password},
    )


def test_set_email_connector_creates_idempotently(logto):
    proc = smtp(logto)
    assert proc.returncode == 0, proc.stderr
    assert "created" in proc.stdout
    (conn,) = logto.fake.connectors
    cfg = conn["config"]
    assert (cfg["host"], cfg["port"], cfg["secure"]) == ("smtp.gmail.com", 465, True)
    assert cfg["auth"] == {
        "type": "login",
        "user": "me@gmail.example",
        "pass": SMTP_PASSWORD,
    }
    assert cfg["fromEmail"] == "me@gmail.example"
    assert "requireTLS" not in cfg
    assert {"SignIn", "Register", "ForgotPassword", "Generic"} <= {
        t["usageType"] for t in cfg["templates"]
    }
    assert all("{{code}}" in t["content"] for t in cfg["templates"])
    (entry,) = ledger_entries(logto)
    assert entry["action"] == "create-email-connector"
    assert entry["undo"] == {
        "method": "DELETE",
        "path": f"/api/connectors/{conn['id']}",
    }
    again = smtp(logto)
    assert "already set" in again.stdout
    assert [r[0] for r in logto.fake.requests if r[1] == "/api/connectors"] == [
        "GET",
        "POST",
        "GET",
    ]


def test_set_email_connector_updates_without_a_secret_in_the_ledger(logto):
    smtp(logto)
    proc = smtp(logto, "--from-name", "Sign in", "--from-email", "alias@gmail.example")
    assert proc.returncode == 0, proc.stderr
    assert "updated fromEmail" in proc.stdout
    assert (
        logto.fake.connectors[0]["config"]["fromEmail"]
        == "Sign in <alias@gmail.example>"
    )
    update = ledger_entries(logto)[-1]
    assert update["action"] == "update-email-connector"
    assert update["changed"] == ["fromEmail"]
    assert "undo" not in update
    # the password changes: reported by field name only
    rotated = smtp(
        logto,
        "--from-name",
        "Sign in",
        "--from-email",
        "alias@gmail.example",
        password="zzzzzzzzzzzzzzzz",
    )
    assert "updated auth" in rotated.stdout
    assert "zzzzzzzzzzzzzzzz" not in rotated.stdout + rotated.stderr
    assert "zzzzzzzzzzzzzzzz" not in logto.ledger.read_text()


def test_set_email_connector_takes_no_host_or_port(logto):
    for flag, value in (("--host", "smtp.resend.com"), ("--port", "587")):
        assert smtp(logto, flag, value).returncode == 2
    assert not [r for r in logto.fake.requests if r[1] == "/api/connectors"]


def test_code_sign_in_is_email_only(logto):
    proc = logto("set-sign-in-exp", "--code-sign-in-identifiers", "phone")
    assert proc.returncode == 2
    assert not [r for r in logto.fake.requests if r[0] == "PATCH"]


def test_set_email_connector_needs_a_password(logto):
    for password in ("", "   "):
        proc = smtp(logto, password=password)
        assert proc.returncode != 0
        assert "LOGTO_SMTP_PASSWORD" in proc.stderr
    assert not [r for r in logto.fake.requests if r[1] == "/api/connectors"]
    bare = logto("set-email-connector", "--user", "me@gmail.example")
    assert bare.returncode != 0 and "LOGTO_SMTP_PASSWORD" in bare.stderr


def test_set_email_connector_refuses_a_different_email_connector(logto):
    logto.fake.connectors.append(
        {"id": "c0", "type": "Email", "connectorId": "aliyun-dm", "config": {}}
    )
    proc = smtp(logto)
    assert proc.returncode == 1
    assert "another email connector" in proc.stderr
    assert [r for r in logto.fake.requests if r[0] != "GET"] == []


def test_email_code_sign_in_needs_the_connector_and_undoes(logto):
    fake = logto.fake.sign_in_exp
    args = (
        "set-sign-in-exp",
        "--sign-in-identifiers",
        "email",
        "username",
        "--code-sign-in-identifiers",
        "email",
    )
    refused = logto(*args)
    assert refused.returncode == 1
    assert "no email connector" in refused.stderr
    smtp(logto)
    proc = logto(*args)
    assert proc.returncode == 0, proc.stderr
    methods = {m["identifier"]: m for m in fake["signIn"]["methods"]}
    assert (methods["email"]["password"], methods["email"]["verificationCode"]) == (
        True,
        True,
    )
    assert (
        methods["username"]["password"],
        methods["username"]["verificationCode"],
    ) == (
        True,
        False,
    )
    assert "already set" in logto(*args).stdout
    code_only = logto("set-sign-in-exp", "--code-sign-in-identifiers", "email")
    assert code_only.returncode == 0
    (only,) = fake["signIn"]["methods"]
    assert (only["identifier"], only["password"], only["verificationCode"]) == (
        "email",
        False,
        True,
    )


def test_undo_deletes_the_created_connector(logto):
    smtp(logto)
    assert logto("undo").returncode == 0
    assert logto.fake.connectors == []


def test_send_test_email_uses_the_stored_config(logto):
    missing = logto("send-test-email", "you@example.com")
    assert missing.returncode == 1 and "no email connector" in missing.stderr
    smtp(logto)
    proc = logto("send-test-email", "you@example.com")
    assert proc.returncode == 0, proc.stderr
    (sent,) = logto.fake.test_sends
    assert sent["email"] == "you@example.com"
    assert sent["config"]["auth"]["pass"] == SMTP_PASSWORD
    assert "you@example.com" in proc.stdout


def test_ledger_needs_no_credentials(logto, tmp_path):
    logto("ensure-role", "admin")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "LOGTO_ADMIN_LEDGER": str(logto.ledger),
    }
    proc = subprocess.run(
        ["python3", str(ADMIN), "ledger"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert '"action": "create-role"' in proc.stdout
    assert (logto.ledger.stat().st_mode & 0o777) == 0o600


DOCKER_STUB = """#!/bin/sh
echo "docker $*" >>"$DOCKER_LOG"
[ "$1" = ps ] && [ -n "$PG_CTR" ] && echo "$PG_CTR"
exit 0
"""


@pytest.fixture
def bootstrap(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER_STUB)
    (bin_dir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"

    def run(*args, container="", project=None):
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOCKER_LOG": str(log),
            "PG_CTR": container,
        }
        if project:
            env["IDENTITY_PROJECT"] = project
        return subprocess.run(
            ["bash", str(BOOTSTRAP), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    run.log = log
    return run


@pytest.mark.parametrize(
    "args",
    [(), ("bogus",), ("apply", "--env-out"), ("apply", "extra"), ("remove", "x")],
)
def test_bootstrap_usage(bootstrap, args):
    assert bootstrap(*args).returncode == 2


def test_bootstrap_needs_the_postgres_container(bootstrap):
    proc = bootstrap("status")
    assert proc.returncode == 1
    assert "no running postgres container in compose project identity" in proc.stderr
    log = bootstrap.log.read_text()
    assert "label=com.docker.compose.project=identity" in log
    assert "label=com.docker.compose.service=postgres" in log


def test_bootstrap_project_override_and_env_out_guard(bootstrap, tmp_path):
    existing = tmp_path / "m2m.env"
    existing.write_text("keep\n")
    proc = bootstrap(
        "apply", "--env-out", str(existing), container="abc", project="logto-rehearsal"
    )
    assert proc.returncode == 1
    assert "not overwriting" in proc.stderr
    assert existing.read_text() == "keep\n"
    log = bootstrap.log.read_text()
    assert "label=com.docker.compose.project=logto-rehearsal" in log
    assert "exec" not in log
