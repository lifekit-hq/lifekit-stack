#!/usr/bin/env python3
"""Logto admin steps through the Management API, as the "firstmate-ops" M2M app.

Replaces the admin-console clicks in docs/runbook.md ("Identity provider
(Logto)", "Grafana sign-in through Logto", "Tailnet sign-in gate"). Every
command is idempotent: it reads first and changes only what differs.

    logto-admin.py token-check
    logto-admin.py ensure-role NAME [--description TEXT]
    logto-admin.py assign-role USER ROLE
    logto-admin.py ensure-app NAME [--redirect-uri URI]... [--post-logout-uri URI]...
                              [--secret-file PATH]
    logto-admin.py set-redirects APP [--redirect-uri URI]... [--post-logout-uri URI]...
    logto-admin.py set-sign-in-exp [--logo-url URL] [--dark-logo-url URL] [--favicon URL]
                              [--dark-favicon URL]
                              [--primary-color HEX] [--dark-primary-color HEX]
                              [--dark-mode | --no-dark-mode]
                              [--sign-in-identifiers {email,phone,username}...]
                              [--code-sign-in-identifiers {email}...]
                              [--sign-up-identifiers {email,phone,username,none}...]
    logto-admin.py set-email-connector --user ADDRESS
                              [--from-email ADDRESS] [--from-name NAME]
    logto-admin.py send-test-email ADDRESS
    logto-admin.py ledger
    logto-admin.py undo [N]

USER is a Logto user id, primary email or username. ROLE is a role name (a
User role). APP is an application id or its exact name. ensure-app creates a
Traditional web app; given URIs, it also brings an existing app's URIs to
exactly those. set-redirects replaces the given list and keeps the other one
(pass both to set both). --secret-file writes the app's default client
secret to a new 0600 file (never over an existing one), for `sops set` to
read; nothing prints a secret. set-sign-in-exp changes only the sign-in
experience fields it is given (PATCH /api/sign-in-exp; signInMode and the
social sign-in settings are never sent). --sign-in-identifiers email username
lets users sign in with either one: each is password verification, password
primary, no verification code. --code-sign-in-identifiers email adds sign-in
by emailed code for that identifier (alone, or together with its password when
it is also in --sign-in-identifiers); Logto accepts it only once an email
connector exists. The two lists together replace the whole methods list.
Logto replaces a PATCHed object whole, so the current color, branding, signIn
and signUp are read first and merged.

set-email-connector creates or updates Logto's SMTP email connector (always
smtp.gmail.com:465, TLS). The login is --user; the password is read from LOGTO_SMTP_PASSWORD
(a Google app password: whitespace is dropped), never argv, and is never
printed or written to the ledger. fromEmail defaults to --user, which is the
address Gmail sends as. Logto allows one email connector. send-test-email
sends one message through the stored connector config, to prove the path.

Credentials come from the environment, never argv:
  LOGTO_M2M_APP_ID, LOGTO_M2M_APP_SECRET  the M2M app (scripts/identity/logto-m2m-bootstrap.sh)
  LOGTO_ENDPOINT        default http://127.0.0.1:3001 (Logto's loopback port)
  LOGTO_ADMIN_LEDGER    default ~/.local/state/lifekit/logto-admin.ledger.jsonl

Every change appends one JSON line to the ledger: what changed, the ids, and
the inverse request. `undo N` sends entry N's inverse (default: the newest
entry not yet undone) and records that it did. The ledger holds ids and
redirect URIs only.

Exit codes: 0 done (or already so), 1 an API or credential error, 2 usage.
Tests: scripts/tests/test_logto_admin.py, against a fake Management API.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RESOURCE = "https://default.logto.app/api"
PAGE_SIZE = 100


class AdminError(Exception):
    pass


class Api:
    def __init__(self, endpoint: str, app_id: str, app_secret: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str | None = None

    def _send(self, req: urllib.request.Request) -> tuple[int, dict, object]:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                status, headers = resp.status, dict(resp.headers)
        except urllib.error.HTTPError as err:
            raw = err.read()
            status, headers = err.code, dict(err.headers or {})
        except urllib.error.URLError as err:
            raise AdminError(f"cannot reach {self.endpoint}: {err.reason}") from None
        body: object = None
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw.decode(errors="replace")[:200]
        return status, headers, body

    def token(self) -> dict:
        """POST /oidc/token, client_credentials. Returns the response minus the token."""
        basic = base64.b64encode(
            f"{urllib.parse.quote(self._app_id, safe='')}:"
            f"{urllib.parse.quote(self._app_secret, safe='')}".encode()
        ).decode()
        data = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "resource": RESOURCE, "scope": "all"}
        ).encode()
        req = urllib.request.Request(
            f"{self.endpoint}/oidc/token",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        status, _, body = self._send(req)
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            detail = (
                body.get("error_description") or body.get("error")
                if isinstance(body, dict)
                else body
            )
            raise AdminError(f"token request failed: HTTP {status}: {detail}")
        self._token = body["access_token"]
        return {k: v for k, v in body.items() if k != "access_token"}

    def call(self, method: str, path: str, body: object = None, ok=(200, 201, 204)):
        if self._token is None:
            self.token()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        status, headers, out = self._send(req)
        if status not in ok:
            msg = out.get("message") if isinstance(out, dict) else out
            code = (
                f" ({out['code']})" if isinstance(out, dict) and out.get("code") else ""
            )
            raise AdminError(f"{method} {path}: HTTP {status}{code}: {msg}")
        return status, headers, out

    def list_all(self, path: str) -> list:
        items: list = []
        page = 1
        sep = "&" if "?" in path else "?"
        while True:
            _, headers, out = self.call(
                "GET", f"{path}{sep}page={page}&page_size={PAGE_SIZE}"
            )
            if not isinstance(out, list):
                raise AdminError(f"GET {path}: expected a list")
            items.extend(out)
            total = next(
                (v for k, v in headers.items() if k.lower() == "total-number"), None
            )
            if len(out) < PAGE_SIZE or (total is not None and len(items) >= int(total)):
                return items
            page += 1


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text().splitlines()
            if line.strip()
        ]

    def append(self, entry: dict) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        n = len(self.entries()) + 1
        entry = {"n": n, "at": now(), **entry}
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return n


def now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def say(msg: str) -> None:
    print(msg, flush=True)


def one(items: list, what: str, key: str, value: str) -> dict | None:
    hits = [i for i in items if i.get(key) == value]
    if len(hits) > 1:
        raise AdminError(f"{len(hits)} {what}s named {value!r}; resolve by id")
    return hits[0] if hits else None


def find_role(api: Api, name: str) -> dict | None:
    return one(api.list_all("/api/roles"), "role", "name", name)


def find_user(api: Api, ref: str) -> dict:
    users = api.list_all("/api/users")
    for key in ("id", "primaryEmail", "username"):
        hits = [u for u in users if u.get(key) == ref]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise AdminError(f"{len(hits)} users match {key}={ref!r}; pass the user id")
    raise AdminError(f"no user with id, primary email or username {ref!r}")


def find_app(api: Api, ref: str) -> dict | None:
    apps = api.list_all("/api/applications")
    by_id = [a for a in apps if a.get("id") == ref]
    return by_id[0] if by_id else one(apps, "application", "name", ref)


def cmd_token_check(api: Api, _ledger: Ledger, _args) -> None:
    info = api.token()
    say(
        f"token ok: scope {info.get('scope', '?')}, expires in {info.get('expires_in', '?')}s"
    )


def cmd_ensure_role(api: Api, ledger: Ledger, args) -> None:
    role = find_role(api, args.name)
    if role:
        if role.get("type") != "User":
            raise AdminError(
                f"role {args.name!r} exists as type {role.get('type')}, not User"
            )
        say(f"role {args.name}: exists ({role['id']})")
        return
    _, _, role = api.call(
        "POST",
        "/api/roles",
        {
            "name": args.name,
            "description": args.description or args.name,
            "type": "User",
        },
    )
    n = ledger.append(
        {
            "action": "create-role",
            "role": {"id": role["id"], "name": args.name},
            "undo": {"method": "DELETE", "path": f"/api/roles/{role['id']}"},
        }
    )
    say(f"role {args.name}: created ({role['id']}), ledger #{n}")


def cmd_assign_role(api: Api, ledger: Ledger, args) -> None:
    user = find_user(api, args.user)
    role = find_role(api, args.role)
    if not role:
        raise AdminError(f"no role named {args.role!r} (ensure-role first)")
    uid, rid = user["id"], role["id"]
    if any(r.get("id") == rid for r in api.list_all(f"/api/users/{uid}/roles")):
        say(f"user {uid} already has role {args.role}")
        return
    api.call("POST", f"/api/users/{uid}/roles", {"roleIds": [rid]})
    n = ledger.append(
        {
            "action": "assign-role",
            "user": uid,
            "role": {"id": rid, "name": args.role},
            "undo": {"method": "DELETE", "path": f"/api/users/{uid}/roles/{rid}"},
        }
    )
    say(f"user {uid}: role {args.role} assigned, ledger #{n}")


def patch_redirects(api: Api, ledger: Ledger, app: dict, redirect, post_logout) -> None:
    meta = dict(app.get("oidcClientMetadata") or {})
    before = {
        "redirectUris": list(meta.get("redirectUris") or []),
        "postLogoutRedirectUris": list(meta.get("postLogoutRedirectUris") or []),
    }
    after = dict(before)
    if redirect is not None:
        after["redirectUris"] = list(redirect)
    if post_logout is not None:
        after["postLogoutRedirectUris"] = list(post_logout)
    if after == before:
        say(f"app {app['name']} ({app['id']}): redirect URIs already set")
        return
    # PATCH takes the whole oidcClientMetadata (both lists are required), so
    # send the current object with only the two lists replaced.
    api.call(
        "PATCH",
        f"/api/applications/{app['id']}",
        {"oidcClientMetadata": {**meta, **after}},
    )
    n = ledger.append(
        {
            "action": "set-redirects",
            "app": {"id": app["id"], "name": app["name"]},
            "before": before,
            "after": after,
            "undo": {
                "method": "PATCH",
                "path": f"/api/applications/{app['id']}",
                "body": {"oidcClientMetadata": {**meta, **before}},
            },
        }
    )
    say(
        f"app {app['name']} ({app['id']}): redirect URIs set "
        f"({len(after['redirectUris'])} sign-in, {len(after['postLogoutRedirectUris'])} sign-out), ledger #{n}"
    )


def write_secret(api: Api, app_id: str, path: Path) -> None:
    _, _, secrets = api.call("GET", f"/api/applications/{app_id}/secrets")
    live = [s for s in secrets or [] if not s.get("expiresAt")]
    secret = next(
        (s for s in live if s.get("name") == "Default secret"),
        live[0] if live else None,
    )
    if not secret:
        raise AdminError(f"app {app_id} has no unexpired secret")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise AdminError(f"{path} exists; not overwriting a secret file") from None
    with os.fdopen(fd, "w") as fh:
        fh.write(secret["value"])
    say(f"app {app_id}: client secret ({secret['name']!r}) written to {path} (0600)")


def cmd_ensure_app(api: Api, ledger: Ledger, args) -> None:
    app = find_app(api, args.name)
    if app:
        if app.get("type") != "Traditional":
            raise AdminError(
                f"application {args.name!r} exists as type {app.get('type')}, not Traditional"
            )
        say(f"app {args.name}: exists ({app['id']})")
        if args.redirect_uri is not None or args.post_logout_uri is not None:
            patch_redirects(api, ledger, app, args.redirect_uri, args.post_logout_uri)
    else:
        _, _, app = api.call(
            "POST",
            "/api/applications",
            {
                "name": args.name,
                "type": "Traditional",
                "oidcClientMetadata": {
                    "redirectUris": args.redirect_uri or [],
                    "postLogoutRedirectUris": args.post_logout_uri or [],
                },
            },
        )
        n = ledger.append(
            {
                "action": "create-app",
                "app": {"id": app["id"], "name": args.name, "type": "Traditional"},
                "undo": {"method": "DELETE", "path": f"/api/applications/{app['id']}"},
            }
        )
        say(f"app {args.name}: created ({app['id']}), ledger #{n}")
    if args.secret_file:
        write_secret(api, app["id"], Path(args.secret_file))


def cmd_set_redirects(api: Api, ledger: Ledger, args) -> None:
    if args.redirect_uri is None and args.post_logout_uri is None:
        raise SystemExit("set-redirects: pass --redirect-uri and/or --post-logout-uri")
    app = find_app(api, args.app)
    if not app:
        raise AdminError(f"no application with id or name {args.app!r}")
    patch_redirects(api, ledger, app, args.redirect_uri, args.post_logout_uri)


HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
SVG_DATA_URI = re.compile(r"^data:image/svg\+xml;base64,[A-Za-z0-9+/]+={0,2}$")
IDENTIFIERS = ("email", "phone", "username")


def hex_color(value: str) -> str:
    if not HEX_COLOR.match(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a #rgb or #rrggbb color")
    return value


def image_url(value: str) -> str:
    """An http(s) URL, or an inline base64 SVG data: URI (no hosting needed)."""
    if value.startswith("data:"):
        if not SVG_DATA_URI.match(value):
            raise argparse.ArgumentTypeError(
                f"{value[:40]!r} is not a data:image/svg+xml;base64,... URI"
            )
        return value
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an http(s) URL or data:image/svg+xml;base64 URI"
        )
    return value


def sign_in_methods(passwords: list[str], codes: list[str]) -> list[dict]:
    return [
        {
            "identifier": i,
            "password": i in passwords,
            "verificationCode": i in codes,
            "isPasswordPrimary": True,
        }
        for i in dict.fromkeys([*passwords, *codes])
    ]


def cmd_set_sign_in_exp(api: Api, ledger: Ledger, args) -> None:
    # (top-level key, leaf key, wanted value); a None value means "not asked for".
    methods = None
    if (
        args.sign_in_identifiers is not None
        or args.code_sign_in_identifiers is not None
    ):
        methods = sign_in_methods(
            args.sign_in_identifiers or [], args.code_sign_in_identifiers or []
        )
    wanted = [
        ("branding", "logoUrl", args.logo_url),
        ("branding", "darkLogoUrl", args.dark_logo_url),
        ("branding", "favicon", args.favicon),
        ("branding", "darkFavicon", args.dark_favicon),
        ("color", "primaryColor", args.primary_color),
        ("color", "isDarkModeEnabled", args.dark_mode),
        ("color", "darkPrimaryColor", args.dark_primary_color),
        ("signIn", "methods", methods),
        (
            "signUp",
            "identifiers",
            None
            if args.sign_up_identifiers is None
            else [i for i in dict.fromkeys(args.sign_up_identifiers) if i != "none"],
        ),
    ]
    wanted = [w for w in wanted if w[2] is not None]
    if not wanted:
        raise SystemExit("set-sign-in-exp: pass at least one field to set")
    _, _, current = api.call("GET", "/api/sign-in-exp")
    if not isinstance(current, dict):
        raise AdminError("GET /api/sign-in-exp: expected an object")
    before: dict[str, dict] = {}
    after: dict[str, dict] = {}
    for top, leaf, value in wanted:
        cur = current.get(top) or {}
        if cur.get(leaf) != value:
            before.setdefault(top, {})[leaf] = cur.get(leaf)
            after.setdefault(top, {})[leaf] = value
    if not after:
        say("sign-in experience: already set")
        return
    # PATCH replaces each top-level object, so send the current one with only
    # the changed leaves swapped in; undo sends back the prior whole object.
    body = {
        top: {**(current.get(top) or {}), **leaves} for top, leaves in after.items()
    }
    prior = {top: current.get(top) or {} for top in after}
    api.call("PATCH", "/api/sign-in-exp", body)
    n = ledger.append(
        {
            "action": "set-sign-in-exp",
            "before": before,
            "after": after,
            "undo": {"method": "PATCH", "path": "/api/sign-in-exp", "body": prior},
        }
    )
    changed = ", ".join(f"{t}.{k}" for t, leaves in after.items() for k in leaves)
    say(f"sign-in experience: set {changed}, ledger #{n}")


SMTP_CONNECTOR = "simple-mail-transfer-protocol"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
# (usageType, subject, text): Logto's SMTP connector requires the first four;
# the rest cover every other flow that can send a code. {{code}} is Logto's.
EMAIL_TEMPLATES = (
    ("SignIn", "Your sign-in code", "Your sign-in code is {{code}}."),
    ("Register", "Your sign-up code", "Your sign-up code is {{code}}."),
    (
        "ForgotPassword",
        "Your password reset code",
        "Your password reset code is {{code}}.",
    ),
    ("Generic", "Your verification code", "Your verification code is {{code}}."),
    (
        "OrganizationInvitation",
        "Your invitation code",
        "Your invitation code is {{code}}.",
    ),
    (
        "UserPermissionValidation",
        "Your verification code",
        "Your verification code is {{code}}.",
    ),
    (
        "BindNewIdentifier",
        "Your verification code",
        "Your verification code is {{code}}.",
    ),
    (
        "MfaVerification",
        "Your verification code",
        "Your verification code is {{code}}.",
    ),
    ("BindMfa", "Your verification code", "Your verification code is {{code}}."),
)


def email_templates() -> list[dict]:
    return [
        {
            "usageType": usage,
            "contentType": "text/plain",
            "subject": subject,
            "content": f"{text} It expires in 10 minutes. If you did not ask for it, ignore this message.",
        }
        for usage, subject, text in EMAIL_TEMPLATES
    ]


def smtp_config(args, password: str) -> dict:
    sender = args.from_email or args.user
    if args.from_name:
        sender = f"{args.from_name} <{sender}>"
    config = {
        "host": os.environ.get("LOGTO_ADMIN_TEST_ONLY_SMTP_HOST", SMTP_HOST),
        "port": SMTP_PORT,
        "secure": True,
        "auth": {"type": "login", "user": args.user, "pass": password},
        "fromEmail": sender,
        "templates": email_templates(),
    }
    return config


def email_connectors(api: Api) -> list[dict]:
    # Not paged: the endpoint returns every connector.
    _, _, out = api.call("GET", "/api/connectors")
    if not isinstance(out, list):
        raise AdminError("GET /api/connectors: expected a list")
    return [c for c in out if c.get("type") == "Email"]


def the_smtp_connector(api: Api) -> dict | None:
    found = email_connectors(api)
    other = [c for c in found if c.get("connectorId") != SMTP_CONNECTOR]
    if other:
        raise AdminError(
            f"another email connector is configured ({other[0].get('connectorId')}); "
            "Logto allows one, remove it first"
        )
    return found[0] if found else None


def cmd_set_email_connector(api: Api, ledger: Ledger, args) -> None:
    password = "".join(os.environ.get("LOGTO_SMTP_PASSWORD", "").split())
    if not password:
        raise SystemExit(
            "set-email-connector: LOGTO_SMTP_PASSWORD is empty or unset "
            "(the SMTP password, a Google app password)"
        )
    config = smtp_config(args, password)
    existing = the_smtp_connector(api)
    if existing is None:
        _, _, created = api.call(
            "POST", "/api/connectors", {"connectorId": SMTP_CONNECTOR, "config": config}
        )
        n = ledger.append(
            {
                "action": "create-email-connector",
                "connector": {"id": created["id"], "connectorId": SMTP_CONNECTOR},
                "undo": {
                    "method": "DELETE",
                    "path": f"/api/connectors/{created['id']}",
                },
            }
        )
        say(f"email connector: created ({created['id']}), ledger #{n}")
        return
    have = existing.get("config") or {}
    changed = [k for k in config if have.get(k) != config[k]]
    if not changed:
        say(f"email connector: already set ({existing['id']})")
        return
    api.call("PATCH", f"/api/connectors/{existing['id']}", {"config": config})
    # No undo: the previous config holds the previous password, and the
    # ledger never holds a secret. Run the command again with the old values.
    n = ledger.append(
        {
            "action": "update-email-connector",
            "connector": {"id": existing["id"], "connectorId": SMTP_CONNECTOR},
            "changed": changed,
        }
    )
    say(
        f"email connector: updated {', '.join(changed)} ({existing['id']}), ledger #{n}"
    )


def cmd_send_test_email(api: Api, _ledger: Ledger, args) -> None:
    existing = the_smtp_connector(api)
    if existing is None:
        raise AdminError("no email connector (set-email-connector first)")
    api.call(
        "POST",
        f"/api/connectors/{SMTP_CONNECTOR}/test",
        {"email": args.address, "config": existing.get("config") or {}},
    )
    say(f"test email: sent to {args.address}")


def cmd_ledger(_api, ledger: Ledger, _args) -> None:
    for e in ledger.entries():
        say(json.dumps(e, sort_keys=True))


def cmd_undo(api: Api, ledger: Ledger, args) -> None:
    entries = ledger.entries()
    undone = {e["of"] for e in entries if e.get("action") == "undo"}
    todo = [e for e in entries if "undo" in e and e["n"] not in undone]
    if args.n is None:
        if not todo:
            raise AdminError("nothing to undo")
        entry = todo[-1]
    else:
        entry = next((e for e in entries if e["n"] == args.n), None)
        if not entry or "undo" not in entry:
            raise AdminError(f"no undoable ledger entry #{args.n}")
        if args.n in undone:
            say(f"ledger #{args.n} already undone")
            return
    inv = entry["undo"]
    api.call(inv["method"], inv["path"], inv.get("body"), ok=(200, 201, 204, 404))
    n = ledger.append(
        {
            "action": "undo",
            "of": entry["n"],
            "request": f"{inv['method']} {inv['path']}",
        }
    )
    say(
        f"undid ledger #{entry['n']} ({entry['action']}): {inv['method']} {inv['path']}, ledger #{n}"
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="logto-admin.py",
        description="Idempotent Logto admin steps over the Management API (docs/runbook.md).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("token-check", help="get a token; prints its scope and lifetime")
    r = sub.add_parser("ensure-role", help="get-or-create a User role")
    r.add_argument("name")
    r.add_argument("--description")
    a = sub.add_parser("assign-role", help="give a user a role, if missing")
    a.add_argument("user", help="user id, primary email or username")
    a.add_argument("role", help="role name")
    for name, target, helptext in (
        ("ensure-app", "name", "get-or-create a Traditional web app"),
        ("set-redirects", "app", "replace an app's redirect URIs"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument(target)
        s.add_argument("--redirect-uri", action="append", metavar="URI")
        s.add_argument("--post-logout-uri", action="append", metavar="URI")
        if name == "ensure-app":
            s.add_argument("--secret-file", metavar="PATH")
    x = sub.add_parser(
        "set-sign-in-exp", help="set sign-in experience branding, color and methods"
    )
    x.add_argument("--logo-url", type=image_url, metavar="URL")
    x.add_argument("--dark-logo-url", type=image_url, metavar="URL")
    x.add_argument("--favicon", type=image_url, metavar="URL")
    x.add_argument("--dark-favicon", type=image_url, metavar="URL")
    x.add_argument("--primary-color", type=hex_color, metavar="HEX")
    x.add_argument("--dark-primary-color", type=hex_color, metavar="HEX")
    x.add_argument("--dark-mode", action=argparse.BooleanOptionalAction, default=None)
    x.add_argument(
        "--sign-in-identifiers",
        nargs="+",
        choices=IDENTIFIERS,
        help="password sign-in with each of these (e.g. email username)",
    )
    x.add_argument(
        "--code-sign-in-identifiers",
        nargs="+",
        choices=("email",),
        help="sign-in by emailed code for each of these; needs the connector",
    )
    x.add_argument(
        "--sign-up-identifiers",
        nargs="+",
        choices=(*IDENTIFIERS, "none"),
        help="sign-up identifiers; 'none' empties the list",
    )
    e = sub.add_parser(
        "set-email-connector",
        help="create or update the SMTP email connector (password in LOGTO_SMTP_PASSWORD)",
    )
    e.add_argument("--user", required=True, help="SMTP login, e.g. the Gmail address")
    e.add_argument("--from-email", help="sender address (default: --user)")
    e.add_argument("--from-name", help="sender display name")
    t = sub.add_parser(
        "send-test-email", help="send one message through the stored connector"
    )
    t.add_argument("address")
    sub.add_parser("ledger", help="print the change ledger")
    u = sub.add_parser(
        "undo", help="reverse a ledger entry (default: the newest not undone)"
    )
    u.add_argument("n", nargs="?", type=int)
    return p


COMMANDS = {
    "token-check": cmd_token_check,
    "ensure-role": cmd_ensure_role,
    "assign-role": cmd_assign_role,
    "ensure-app": cmd_ensure_app,
    "set-redirects": cmd_set_redirects,
    "set-sign-in-exp": cmd_set_sign_in_exp,
    "set-email-connector": cmd_set_email_connector,
    "send-test-email": cmd_send_test_email,
    "ledger": cmd_ledger,
    "undo": cmd_undo,
}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    ledger = Ledger(
        Path(
            os.environ.get("LOGTO_ADMIN_LEDGER")
            or Path.home() / ".local/state/lifekit/logto-admin.ledger.jsonl"
        )
    )
    api = None
    if args.cmd != "ledger":
        app_id = os.environ.get("LOGTO_M2M_APP_ID", "")
        app_secret = os.environ.get("LOGTO_M2M_APP_SECRET", "")
        if not app_id or not app_secret:
            print(
                "logto-admin: LOGTO_M2M_APP_ID and LOGTO_M2M_APP_SECRET must be set",
                file=sys.stderr,
            )
            return 2
        api = Api(
            os.environ.get("LOGTO_ENDPOINT") or "http://127.0.0.1:3001",
            app_id,
            app_secret,
        )
    try:
        COMMANDS[args.cmd](api, ledger, args)
    except AdminError as err:
        print(f"logto-admin: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
