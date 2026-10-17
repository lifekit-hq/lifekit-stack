"""scripts/people/serve.py (people serve) against fake Logto, Tailscale and SMTP.

The service is started as a subprocess on a free loopback port and driven with
plain HTTP. Covers the auth rejections (nothing runs, nothing is written), the
endpoints and their JSON, the actor recorded in the Logto admin ledger, and
the compose side: the service sits behind a profile, so the merge deploy's
`up` does not start it, and carries a lifekit.contract label.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from test_logto_admin import APP_ID, APP_SECRET, TOKEN, FakeLogto, handler_for
from test_people import (  # noqa: F401 - tls_ctx is a fixture
    PASSWORD,
    FakeSmtp,
    smtp_env,
    tls_ctx,
)
from test_people_tailnet import (
    CLIENT_ID,
    CLIENT_SECRET,
    INVITE_URL,
    USER_TOKEN,
    FakeTailscale,
    give_role,
    ts_handler,
)

REPO = Path(__file__).resolve().parents[2]
SERVE = REPO / "scripts/people/serve.py"
HOST = "box.example.ts.net"
SECRET = "s3cret-" + "x" * 40
OWNER = "owner@example.com"
LINK = "https://login.tailscale.example/uinv/zzz"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def serve(tmp_path):
    logto, ts = FakeLogto(), FakeTailscale()
    servers = []
    for handler in (handler_for(logto), ts_handler(ts)):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    ledger = tmp_path / "ledger.jsonl"
    procs: list[subprocess.Popen] = []

    def start(extra_env=None, tailnet=True, secret=SECRET):
        port = free_port()
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "LOGTO_ENDPOINT": f"http://127.0.0.1:{servers[0].server_port}",
            "LOGTO_ADMIN_LEDGER": str(ledger),
            "LOGTO_M2M_APP_ID": APP_ID,
            "LOGTO_M2M_APP_SECRET": APP_SECRET,
            "PEOPLE_HOST": HOST,
            "PEOPLE_SERVE_SECRET": secret,
        }
        if tailnet:
            env |= {
                "TAILSCALE_API_URL": f"http://127.0.0.1:{servers[1].server_port}",
                "TAILSCALE_OAUTH_CLIENT_ID": CLIENT_ID,
                "TAILSCALE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
                "TAILSCALE_USER_TOKEN": USER_TOKEN,
            }
        env |= extra_env or {}
        proc = subprocess.Popen(
            [sys.executable, str(SERVE), "--bind", "127.0.0.1", "--port", str(port)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1).read()
                break
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
        return base

    def call(base, method, path, body=None, headers=None):
        hdrs = {"X-People-Secret": SECRET, "X-Auth-Request-Email": OWNER}
        if headers is not None:
            hdrs = headers
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method)
        for k, v in hdrs.items():
            req.add_header(k, v)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    class S:
        pass

    s = S()
    s.logto, s.ts, s.ledger, s.start, s.call = logto, ts, ledger, start, call
    s.entries = lambda: (
        [json.loads(x) for x in ledger.read_text().splitlines()]
        if ledger.exists()
        else []
    )
    yield s
    outs = []
    for p in procs:
        p.terminate()
        out, err = p.communicate(timeout=10)
        outs.append(out + err)
    for srv in servers:
        srv.shutdown()
    for out in outs:
        for secret in (SECRET, CLIENT_SECRET, USER_TOKEN, APP_SECRET, TOKEN, PASSWORD):
            assert secret not in out


def writes(fake: FakeLogto):
    return [(m, p) for m, p, _ in fake.requests if m != "GET"]


# ─── start-up refusals ───────────────────────────────────────────────────────


@pytest.mark.parametrize("secret", ["", "short"])
def test_refuses_to_start_without_a_strong_secret(serve, secret):
    proc = subprocess.run(
        [sys.executable, str(SERVE), "--port", str(free_port())],
        env={
            "PATH": os.environ["PATH"],
            "HOME": "/nonexistent",
            "LOGTO_M2M_APP_ID": APP_ID,
            "LOGTO_M2M_APP_SECRET": APP_SECRET,
            "PEOPLE_SERVE_SECRET": secret,
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode != 0
    assert "PEOPLE_SERVE_SECRET" in proc.stderr


# ─── auth ────────────────────────────────────────────────────────────────────

ROUTES = [
    ("GET", "/v1/rights", None),
    ("GET", "/v1/people", None),
    ("POST", "/v1/people/invite", {"email": "a@b.co", "rights": ["backstage"]}),
    ("POST", "/v1/people/resend", {"email": "a@b.co", "rights": ["backstage"]}),
    ("GET", "/nope", None),
]
GOOD = {"X-People-Secret": SECRET, "X-Auth-Request-Email": OWNER}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Auth-Request-Email": OWNER},
        {"X-People-Secret": SECRET},
        {"X-People-Secret": "wrong" * 10, "X-Auth-Request-Email": OWNER},
        {"X-People-Secret": SECRET[:-1], "X-Auth-Request-Email": OWNER},
        {"X-People-Secret": SECRET + "x", "X-Auth-Request-Email": OWNER},
        {"X-People-Secret": SECRET, "X-Auth-Request-Email": "not-an-email"},
        {"X-People-Secret": SECRET, "X-Auth-Request-Email": ""},
    ],
)
@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_rejects_missing_or_wrong_credentials(
    serve, method, path, body, headers
):
    base = serve.start()
    status, out = serve.call(base, method, path, body, headers)
    assert status == 401
    assert out["error"]["code"] == "unauthorized"
    assert SECRET not in json.dumps(out)
    assert serve.logto.requests == []  # nothing ran, not even a token fetch
    assert serve.entries() == []


def test_healthz_needs_no_credentials_and_reveals_nothing(serve):
    base = serve.start()
    status, out = serve.call(base, "GET", "/healthz", None, {})
    assert (status, out) == (200, {"ok": True})
    assert serve.logto.requests == []


def test_actor_allowlist_limits_who_may_act(serve):
    base = serve.start({"PEOPLE_SERVE_ACTORS": "boss@example.com, Owner@Example.com"})
    status, _ = serve.call(base, "GET", "/v1/rights")
    assert status == 200
    other = {**GOOD, "X-Auth-Request-Email": "mallory@example.com"}
    status, out = serve.call(base, "GET", "/v1/rights", None, other)
    assert status == 403 and out["error"]["code"] == "forbidden"


def test_unknown_route_and_wrong_method(serve):
    base = serve.start()
    assert serve.call(base, "GET", "/nope")[0] == 404
    assert serve.call(base, "GET", "/v1/people/invite")[0] == 405
    assert serve.call(base, "POST", "/v1/rights", {})[0] == 405


# ─── endpoints ───────────────────────────────────────────────────────────────


def test_rights_lists_the_catalog(serve):
    base = serve.start()
    status, out = serve.call(base, "GET", "/v1/rights")
    assert status == 200
    ids = {r["id"]: r for r in out["rights"]}
    assert ids["finance-sentry"]["port"] == 4200
    assert set(ids["backstage"]) == {"id", "name", "port"}


def test_invite_creates_the_user_joins_the_group_and_records_the_actor(serve):
    base = serve.start()
    status, out = serve.call(
        base,
        "POST",
        "/v1/people/invite",
        {"email": "Friend@Example.com", "rights": ["backstage"], "invite_url": LINK},
    )
    assert status == 200, out
    assert out["ok"] and out["email"] == "friend@example.com"
    assert any("--- invite email (draft" in line for line in out["output"])
    assert any(LINK in line for line in out["output"])
    assert serve.logto.users[-1]["primaryEmail"] == "friend@example.com"
    assert serve.ts.acl_writes()
    ledger = serve.entries()
    (req,) = [e for e in ledger if e.get("action") == "people-request"]
    assert req["actor"] == OWNER
    assert req["request"] == "invite" and req["result"] == "ok"
    assert req["email"] == "friend@example.com" and req["rights"] == ["backstage"]
    # the invite's own writes landed in the same ledger, with their undo data
    assert any("undo" in e for e in ledger)


def test_invite_is_idempotent(serve):
    base = serve.start()
    body = {"email": "friend@example.com", "rights": ["backstage"], "invite_url": LINK}
    serve.call(base, "POST", "/v1/people/invite", body)
    before = len(writes(serve.logto))
    status, out = serve.call(base, "POST", "/v1/people/invite", body)
    assert status == 200 and "already held" in "\n".join(out["output"])
    assert len(writes(serve.logto)) == before


def test_invite_send_mails_once(serve, tls_ctx):
    smtp = FakeSmtp(tls_ctx=tls_ctx[0])
    base = serve.start(smtp_env(smtp, tls_ctx[2]))
    status, out = serve.call(
        base,
        "POST",
        "/v1/people/invite",
        {
            "email": "friend@example.com",
            "rights": ["backstage"],
            "invite_url": LINK,
            "send": True,
        },
    )
    assert status == 200, out
    assert any("invite email sent to friend@example.com" in x for x in out["output"])
    (m,) = smtp.messages
    assert LINK in m["data"] and f"https://{HOST}:18810" in m["data"]
    assert PASSWORD not in json.dumps(out)


def test_invite_without_send_never_mails(serve, tls_ctx):
    smtp = FakeSmtp(tls_ctx=tls_ctx[0])
    base = serve.start(smtp_env(smtp, tls_ctx[2]))
    serve.call(
        base,
        "POST",
        "/v1/people/invite",
        {"email": "friend@example.com", "rights": ["backstage"], "invite_url": LINK},
    )
    assert smtp.messages == [] and smtp.logins == []


def test_resend_writes_nothing_to_logto_and_mails(serve, tls_ctx):
    smtp = FakeSmtp(tls_ctx=tls_ctx[0])
    base = serve.start(smtp_env(smtp, tls_ctx[2]))
    body = {"email": "friend@example.com", "rights": ["backstage"], "invite_url": LINK}
    assert serve.call(base, "POST", "/v1/people/invite", body)[0] == 200
    before = len(writes(serve.logto))
    status, out = serve.call(base, "POST", "/v1/people/resend", body)
    assert status == 200, out
    assert len(writes(serve.logto)) == before
    assert len(smtp.messages) == 1
    actions = [e["request"] for e in serve.entries() if "actor" in e]
    assert actions == ["invite", "resend"]


def test_resend_for_someone_not_invited_is_a_failure_with_the_reason(serve):
    base = serve.start()
    serve.call(
        base,
        "POST",
        "/v1/people/invite",
        {"email": "friend@example.com", "rights": ["backstage"], "invite_url": LINK},
    )
    status, out = serve.call(
        base,
        "POST",
        "/v1/people/resend",
        {"email": "friend@example.com", "rights": ["finance-sentry"]},
    )
    assert status == 502
    assert out["error"]["code"] == "failed"
    assert "does not hold finance-sentry" in out["error"]["message"]
    last = serve.entries()[-1]
    assert last["actor"] == OWNER and last["result"] == "error"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "bad_request"),
        ({"email": "nope", "rights": ["backstage"]}, "bad_request"),
        ({"email": "a@b.co", "rights": []}, "bad_request"),
        ({"email": "a@b.co", "rights": "backstage"}, "bad_request"),
        ({"email": "a@b.co", "rights": ["root"]}, "unknown_right"),
        ({"email": "a@b.co", "rights": ["backstage"], "send": "yes"}, "bad_request"),
        (
            {"email": "a@b.co", "rights": ["backstage"], "invite_url": "http://x/y"},
            "bad_request",
        ),
        (
            {"email": "a@b.co", "rights": ["backstage"], "invite_url": "https://a b"},
            "bad_request",
        ),
    ],
)
def test_bad_requests_are_400_before_any_write(serve, body, code):
    base = serve.start()
    status, out = serve.call(base, "POST", "/v1/people/invite", body)
    assert status == 400 and out["error"]["code"] == code
    assert writes(serve.logto) == [] and serve.ts.acl_writes() == []
    assert serve.entries() == []


def test_body_that_is_not_json_or_too_large(serve):
    base = serve.start()
    req = urllib.request.Request(
        base + "/v1/people/invite", data=b"{not json", method="POST"
    )
    for k, v in GOOD.items():
        req.add_header(k, v)
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 400
    status, out = serve.call(
        base, "POST", "/v1/people/invite", {"email": "a@b.co", "pad": "x" * 9000}
    )
    assert status == 413


def test_the_host_comes_from_the_service_never_the_request(serve):
    base = serve.start()
    _, out = serve.call(
        base,
        "POST",
        "/v1/people/invite",
        {
            "email": "friend@example.com",
            "rights": ["backstage"],
            "invite_url": LINK,
            "host": "evil.example.org",
        },
    )
    text = "\n".join(out["output"])
    assert f"https://{HOST}:18810" in text and "evil.example.org" not in text


def test_invite_needs_the_host_configured(serve):
    base = serve.start({"PEOPLE_HOST": ""})
    status, out = serve.call(
        base,
        "POST",
        "/v1/people/invite",
        {"email": "friend@example.com", "rights": ["backstage"]},
    )
    assert status == 503 and out["error"]["code"] == "not_configured"
    assert writes(serve.logto) == []


def test_people_lists_rights_tailnet_and_drift(serve):
    base = serve.start()
    serve.call(
        base,
        "POST",
        "/v1/people/invite",
        {"email": "friend@example.com", "rights": ["backstage"], "invite_url": LINK},
    )
    status, out = serve.call(base, "GET", "/v1/people")
    assert status == 200 and out["tailnet_configured"] is True
    friend = next(p for p in out["people"] if p["email"] == "friend@example.com")
    assert friend["rights"] == ["backstage"] and friend["suspended"] is False
    assert "backstage" in " ".join(friend["roles"])
    assert "group:backstage" in (friend["tailnet"] or "")
    status, one = serve.call(base, "GET", "/v1/people?email=friend@example.com")
    assert [p["email"] for p in one["people"]] == ["friend@example.com"]


def test_people_flags_drift_and_works_without_a_tailnet(serve):
    base = serve.start(tailnet=False)
    user = serve.logto.users[0] if serve.logto.users else None
    if user is None:
        serve.call(
            base,
            "POST",
            "/v1/people/invite",
            {"email": "a@example.com", "rights": ["backstage"], "invite_url": LINK},
        )
        user = serve.logto.users[-1]
    user["isSuspended"] = True
    give_role(serve.logto, user["id"], "backstage")
    status, out = serve.call(base, "GET", "/v1/people")
    assert status == 200 and out["tailnet_configured"] is False
    row = next(p for p in out["people"] if p["email"] == user["primaryEmail"])
    assert "suspended but still holds a product role" in row["drift"]


def test_unknown_person_status_is_a_clean_error(serve):
    base = serve.start()
    status, out = serve.call(base, "GET", "/v1/people?email=ghost@example.com")
    assert status == 502 and out["error"]["code"] == "failed"
    assert "Traceback" not in json.dumps(out)


# ─── compose: not started by the merge deploy ────────────────────────────────

COMPOSE = REPO / "compose/docker-compose.yml"


def load_compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_people_serve_is_behind_a_profile_and_declares_a_contract():
    svc = load_compose()["services"]["people-serve"]
    assert svc["profiles"] == ["people"]
    assert svc["labels"]["lifekit.contract"] in {"v1", "none"}
    nets = load_compose()["networks"]
    assert nets["people-internal"]["internal"] is True
    assert set(svc["networks"]) == {"default", "people-internal"}
    # no host port: only the dashboard, on the internal network, can reach it
    assert "ports" not in svc


def test_deploy_never_activates_the_people_profile():
    for path in [REPO / "scripts/deploy.sh", *(REPO / "scripts/lib").glob("*.sh")]:
        text = path.read_text()
        assert "--profile people" not in text, path
        assert "COMPOSE_PROFILES" not in text, path
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    assert "profile people" not in ci and "COMPOSE_PROFILES" not in ci


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker compose")
def test_a_deploy_render_does_not_include_the_service():
    def services(*extra):
        proc = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE), *extra, "config", "--services"],
            env={**os.environ, "OPENCLAW_GATEWAY_TOKEN": "x"},
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip(f"compose config unavailable here: {proc.stderr[:200]}")
        return proc.stdout.split()

    assert "people-serve" not in services()
    assert "people-serve" in services("--profile", "people")
