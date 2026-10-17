#!/usr/bin/env python3
"""people serve: the people tool's operations as a small JSON API.

    serve.py [--bind ADDR] [--port PORT]

The lifekit-dashboard People page calls this over an internal Docker network
that only the dashboard joins (compose/docker-compose.yml, service
`people-serve`, profile `people`); the dashboard holds no Logto, Tailscale or
SMTP secret. The endpoint contract (paths, JSON, errors) is in docs/runbook.md
"People serve".

Every route but /healthz needs two headers, or the answer is 401 and nothing
runs:

    X-People-Secret       the shared secret (PEOPLE_SERVE_SECRET), compared in
                          constant time
    X-Auth-Request-Email  the signed-in owner's email, forwarded by the
                          dashboard; recorded in the Logto admin ledger as the
                          actor of each invite and resend. Only emails listed
                          in PEOPLE_SERVE_ACTORS (comma-separated) may act; any
                          other is 403 and nothing runs.

The service refuses to start without a secret of at least 32 characters or
without PEOPLE_SERVE_ACTORS, so a mis-set env never serves an open API. It runs the same functions as people.py
(one request at a time: they print their progress, which is captured and
returned as `output`), so Logto, tailnet and mail behave exactly as on the CLI.

Routes: GET /healthz, GET /v1/rights, GET /v1/people[?email=],
POST /v1/people/invite, POST /v1/people/resend. Revoke is not here yet: when
`people revoke` lands in people.py, add its entry to route_table and its handler
next to resend_route; nothing else in this file needs to change.

Tests: scripts/tests/test_people_serve.py, against fake Logto, Tailscale and
SMTP servers.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import io
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import mailer
import people
import tailnet

admin = people.admin
AdminError = people.AdminError

SECRET_HEADER = "X-People-Secret"
ACTOR_HEADER = "X-Auth-Request-Email"
MIN_SECRET = 32
MAX_BODY = 8192
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Everything people.py prints goes through one stdout, so requests that touch
# it take turns.
LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, output=None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.output = (
            status,
            code,
            message,
            output,
        )


class Service:
    """The configuration and the operations; the HTTP layer only routes."""

    def __init__(self, env=None) -> None:
        env = os.environ if env is None else env
        self.secret = env.get("PEOPLE_SERVE_SECRET", "")
        if len(self.secret) < MIN_SECRET:
            raise SystemExit(
                f"people serve: PEOPLE_SERVE_SECRET must be set, at least {MIN_SECRET} characters"
            )
        self.actors = {
            a.strip().lower()
            for a in env.get("PEOPLE_SERVE_ACTORS", "").split(",")
            if a.strip()
        }
        if not self.actors:
            raise SystemExit(
                "people serve: PEOPLE_SERVE_ACTORS must list at least one email"
            )
        self.host = (env.get("PEOPLE_HOST") or "").strip()
        app_id = env.get("LOGTO_M2M_APP_ID", "")
        app_secret = env.get("LOGTO_M2M_APP_SECRET", "")
        if not app_id or not app_secret:
            raise SystemExit(
                "people serve: LOGTO_M2M_APP_ID and LOGTO_M2M_APP_SECRET must be set"
            )
        self.logto = (
            env.get("LOGTO_ENDPOINT") or "http://127.0.0.1:3001",
            app_id,
            app_secret,
        )
        self.ledger = admin.Ledger(
            Path(
                env.get("LOGTO_ADMIN_LEDGER")
                or Path.home() / ".local/state/lifekit/logto-admin.ledger.jsonl"
            )
        )
        self.catalog_path = Path(env.get("PEOPLE_RIGHTS") or people.CATALOG)

    # ── auth ────────────────────────────────────────────────────────────────

    def actor(self, headers) -> str:
        """The owner's email, or ApiError 401. Checks the secret first."""
        given = (headers.get(SECRET_HEADER) or "").encode()
        if not hmac.compare_digest(given, self.secret.encode()):
            raise ApiError(401, "unauthorized", "missing or wrong credentials")
        email = (headers.get(ACTOR_HEADER) or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise ApiError(401, "unauthorized", f"{ACTOR_HEADER} is missing")
        if email not in self.actors:
            raise ApiError(403, "forbidden", "this account may not use people")
        return email

    # ── operations ──────────────────────────────────────────────────────────

    def catalog(self) -> dict:
        return people.load_catalog(self.catalog_path)

    def rights(self) -> dict:
        cat = self.catalog()
        return {
            "rights": [
                {"id": rid, "name": r["name"], "port": int(r["port"])}
                for rid, r in cat["rights"].items()
            ]
        }

    def new_api(self) -> admin.Api:
        return admin.Api(*self.logto)

    def status(self, email: str | None) -> dict:
        cat = self.catalog()
        api = self.new_api()
        if email:
            users = [admin.find_user(api, email)]
        else:
            users = api.list_all("/api/users")
        rows = [people.user_row(api, u, cat) for u in users]
        ts = tailnet.Tailscale()
        view = people.tailnet_view(ts, cat) if ts.configured else None
        out = []
        for row in rows:
            u = row["user"]
            addr = (u.get("primaryEmail") or "").lower()
            held = [rid for rid, r in cat["rights"].items() if r["role"] in row["roles"]]
            entry = {
                "email": addr or None,
                "name": u.get("name") or u.get("username"),
                "roles": row["roles"],
                "rights": held,
                "suspended": bool(u.get("isSuspended")),
                "tailnet": None,
                "drift": list(row["drift"]),
            }
            if view is not None and addr:
                entry["tailnet"] = people.tailnet_line(view, addr) or None
                entry["drift"] += people.tailnet_row_drift(view, row, cat)
            out.append(entry)
        extra = []
        notes = []
        if view is not None:
            notes = list(view["notes"])
            extra = people.tailnet_global_drift(view, cat, rows, not email)
        return {
            "people": out,
            "tailnet_configured": view is not None,
            "tailnet_notes": notes,
            "drift": extra,
        }

    def args_for(self, body: dict, cat: dict, resend: bool) -> argparse.Namespace:
        email = body.get("email")
        rights = body.get("rights")
        if not isinstance(email, str) or not EMAIL_RE.match(email.strip()):
            raise ApiError(400, "bad_request", "email must be an email address")
        if (
            not isinstance(rights, list)
            or not rights
            or not all(isinstance(r, str) for r in rights)
        ):
            raise ApiError(400, "bad_request", "rights must be a non-empty list of ids")
        unknown = [r for r in rights if r not in cat["rights"]]
        if unknown:
            raise ApiError(
                400, "unknown_right", f"unknown right {', '.join(unknown)}"
            )
        url = body.get("invite_url")
        if url is not None and not (
            isinstance(url, str)
            and re.match(r"^https://[^\s<>\"']+$", url)
            and len(url) <= 2048
        ):
            raise ApiError(400, "bad_request", "invite_url must be an https link")
        send = body.get("send", False)
        if not isinstance(send, bool):
            raise ApiError(400, "bad_request", "send must be true or false")
        if not self.host:
            raise ApiError(
                503, "not_configured", "PEOPLE_HOST is not set on the people service"
            )
        return argparse.Namespace(
            email=email.strip(),
            right=list(rights),
            host=self.host,
            invite_url=url,
            no_tailnet=False,
            send=send and not resend,
            resend=resend,
        )

    def run(self, actor: str, action: str, body: dict, resend: bool) -> dict:
        """invite or resend: run the CLI function, capture its output, record the actor."""
        cat = self.catalog()
        args = self.args_for(body, cat, resend)
        buf = io.StringIO()
        result, failure = "error", None
        try:
            with contextlib.redirect_stdout(buf):
                people.cmd_invite(self.new_api(), self.ledger, args, cat)
            result = "ok"
        except (AdminError, tailnet.TailnetError, mailer.MailError) as err:
            failure = str(err)
        finally:
            self.ledger.append(
                {
                    "action": "people-request",
                    "actor": actor,
                    "request": action,
                    "email": args.email.lower(),
                    "rights": args.right,
                    "send": bool(args.send or args.resend),
                    "result": result,
                }
            )
        output = buf.getvalue().splitlines()
        if failure is not None:
            raise ApiError(502, "failed", failure, output)
        return {"ok": True, "email": args.email.lower(), "output": output}


def route_table(svc: Service) -> dict:
    def get_people(actor, query, _body):
        return svc.status((query.get("email") or [None])[0])

    def get_rights(_actor, _query, _body):
        return svc.rights()

    def invite_route(actor, _query, body):
        return svc.run(actor, "invite", body, resend=False)

    def resend_route(actor, _query, body):
        return svc.run(actor, "resend", body, resend=True)

    return {
        ("GET", "/v1/rights"): get_rights,
        ("GET", "/v1/people"): get_people,
        ("POST", "/v1/people/invite"): invite_route,
        ("POST", "/v1/people/resend"): resend_route,
    }


def make_handler(svc: Service):
    routes = route_table(svc)

    class Handler(BaseHTTPRequestHandler):
        server_version = "people-serve"

        def log_message(self, fmt, *a):  # one JSON line per request, no query/body
            line = {"level": "info", "msg": "request", "line": fmt % a}
            print(json.dumps(line), file=sys.stderr, flush=True)

        def reply(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                raise ApiError(413, "too_large", f"body over {MAX_BODY} bytes")
            if n == 0:
                return {}
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                raise ApiError(400, "bad_request", "body is not JSON") from None
            if not isinstance(body, dict):
                raise ApiError(400, "bad_request", "body must be a JSON object")
            return body

        def handle_any(self, method: str) -> None:
            try:
                url = urlsplit(self.path)
                if method == "GET" and url.path == "/healthz":
                    return self.reply(200, {"ok": True})
                actor = svc.actor(self.headers)
                handler = routes.get((method, url.path))
                if handler is None:
                    if any(p == url.path for _, p in routes):
                        raise ApiError(405, "method_not_allowed", f"not for {method}")
                    raise ApiError(404, "not_found", "no such route")
                body = self.body() if method == "POST" else {}
                with LOCK:
                    return self.reply(200, handler(actor, parse_qs(url.query), body))
            except ApiError as err:
                err_body = {"code": err.code, "message": err.message}
                if err.output is not None:
                    err_body["output"] = err.output
                self.reply(err.status, {"ok": False, "error": err_body})
            except (AdminError, tailnet.TailnetError) as err:
                self.reply(
                    502, {"ok": False, "error": {"code": "failed", "message": str(err)}}
                )
            except Exception:  # never leak a traceback to the caller
                self.log_message("%s", "unhandled error")
                self.reply(
                    500,
                    {"ok": False, "error": {"code": "internal", "message": "internal error"}},
                )

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="serve.py", description="people serve")
    p.add_argument("--bind", default="0.0.0.0")  # noqa: S104 - container network only
    p.add_argument("--port", type=int, default=8095)
    args = p.parse_args(argv)
    svc = Service()
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(svc))
    print(
        json.dumps({"level": "info", "msg": f"people serve on {args.bind}:{args.port}"}),
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
