"""scripts/check-wger-powersync.py against a stand-in for wger's nginx.

The stub serves what nginx + PowerSync + wger answer on the three paths, so the
check's pass and each failure are held without Docker.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fakeserver import serve, stop

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_wger_powersync", REPO / "scripts/check-wger-powersync.py"
)
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)

JWKS = json.dumps({"keys": [{"kty": "RSA", "kid": "powersync"}]}).encode()


@pytest.fixture
def server():
    """A throwaway HTTP server; `routes` maps path -> (status, body) and is editable."""
    routes: dict[str, tuple[int, bytes]] = {
        "/ps/probes/liveness": (200, b"{}"),
        "/ps/probes/readiness": (200, b"{}"),
        "/api/v2/powersync-keys": (200, JWKS),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = routes.get(self.path, (404, b""))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    serve(httpd)
    base = f"http://127.0.0.1:{httpd.server_port}"
    yield base, routes
    stop(httpd)


def test_all_paths_answering_passes(server, capsys):
    base, _ = server
    assert chk.main(["x", base]) == 0
    assert "ok" in capsys.readouterr().out


@pytest.mark.parametrize("probe", ["liveness", "readiness"])
@pytest.mark.parametrize("status", [502, 503, 404])
def test_a_failing_probe_is_reported(server, capsys, probe, status):
    base, routes = server
    routes[f"/ps/probes/{probe}"] = (status, b"")
    assert chk.main(["x", base]) == 1
    assert f"/ps/probes/{probe} answered {status}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "answer", [(200, b'{"keys": []}'), (200, b"not json"), (500, b""), (404, b"")]
)
def test_no_signing_key_is_reported(server, capsys, answer):
    base, routes = server
    routes["/api/v2/powersync-keys"] = answer
    assert chk.main(["x", base]) == 1
    assert "/api/v2/powersync-keys" in capsys.readouterr().err


def test_nothing_listening_is_reported_per_check(capsys):
    assert chk.main(["x", "http://127.0.0.1:1"]) == 1
    err = capsys.readouterr().err
    assert err.count("answered nothing") == 3


def test_wait_retries_until_ready(server):
    base, routes = server
    routes["/ps/probes/readiness"] = (503, b"")
    seen = []

    def flip():
        seen.append(1)
        routes["/ps/probes/readiness"] = (200, b"{}")

    threading.Timer(0.5, flip).start()
    assert chk.main(["x", base, "--wait", "30"]) == 0
    assert seen


def test_wait_gives_up_at_the_deadline(server):
    base, routes = server
    routes["/ps/probes/readiness"] = (503, b"")
    assert chk.main(["x", base, "--wait", "1"]) == 1


def test_bad_arguments_exit_2(capsys):
    assert chk.main(["x", "--wait", "soon"]) == 2
    assert chk.main(["x", "a", "b"]) == 2
