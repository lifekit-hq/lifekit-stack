"""Behaviour of scripts/ask-agent/ask-agent.sh against a fake gateway.

What this file pins: the POST targets the configured URL with the agent id,
no delivery and the request id in the message and the Idempotency-Key; the
token reaches the gateway as a bearer header but never appears in stdout,
stderr or the payload; a missing or malformed token, a missing URL, an unknown
agent and a refusing gateway each fail with a clear message.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ask-agent/ask-agent.sh"
TOKEN = "tok-" + "z" * 16


@pytest.fixture
def gateway():
    seen = []
    state = {"status": 200}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": json.loads(body),
                }
            )
            self.send_response(state["status"])
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/hooks-x/agent", seen, state
    srv.shutdown()


def run(args, env_extra, tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )


def token_file(tmp_path, value=TOKEN):
    p = tmp_path / "tok"
    p.write_text(value + "\n")
    return str(p)


def test_posts_the_question_and_prints_the_request_id(gateway, tmp_path):
    url, seen, _ = gateway
    r = run(
        ["finance", "what is the thing?"],
        {"ASK_AGENT_HOOK_URL": url, "ASK_AGENT_HOOK_TOKEN_FILE": token_file(tmp_path)},
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    req = r.stdout.split("request ")[1].strip()
    assert req.startswith("ask-")
    (call,) = seen
    assert call["path"] == "/hooks-x/agent"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["Idempotency-Key"] == req
    body = call["body"]
    assert body["agentId"] == "finance" and body["deliver"] is False
    assert req in body["message"] and "what is the thing?" in body["message"]
    assert "relay.sh note" in body["message"] and "firstmate" in body["message"]
    assert TOKEN not in json.dumps(body)
    assert TOKEN not in r.stdout + r.stderr


def test_token_env_works_and_kit_is_allowed(gateway, tmp_path):
    url, seen, _ = gateway
    r = run(
        ["kit", "q"],
        {"ASK_AGENT_HOOK_URL": url, "ASK_AGENT_HOOK_TOKEN": TOKEN},
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert seen[0]["body"]["agentId"] == "kit"


@pytest.mark.parametrize(
    "args,env,code,needle",
    [
        (["health", "q"], {"ASK_AGENT_HOOK_URL": "http://x"}, 64, "finance or kit"),
        (["kit", "  "], {"ASK_AGENT_HOOK_URL": "http://x"}, 64, "empty"),
        (["kit"], {}, 64, "usage"),
        (["kit", "q"], {}, 78, "ASK_AGENT_HOOK_URL"),
        (
            ["kit", "q"],
            {"ASK_AGENT_HOOK_URL": "http://x"},
            78,
            "no hook token configured",
        ),
        (
            ["kit", "q"],
            {
                "ASK_AGENT_HOOK_URL": "http://x",
                "ASK_AGENT_HOOK_TOKEN_FILE": "/nonexistent/tok",
            },
            78,
            "missing or unreadable",
        ),
        (
            ["kit", "q"],
            {"ASK_AGENT_HOOK_URL": "http://x", "ASK_AGENT_HOOK_TOKEN": "short"},
            78,
            "malformed",
        ),
    ],
)
def test_config_failures_are_clear(args, env, code, needle, tmp_path):
    r = run(args, env, tmp_path)
    assert r.returncode == code
    assert needle in r.stderr


def test_refused_token_and_gateway_errors_fail(gateway, tmp_path):
    url, _, state = gateway
    env = {"ASK_AGENT_HOOK_URL": url, "ASK_AGENT_HOOK_TOKEN": TOKEN}
    state["status"] = 401
    r = run(["kit", "q"], env, tmp_path)
    assert r.returncode == 77 and "refused" in r.stderr and TOKEN not in r.stderr
    state["status"] = 500
    r = run(["kit", "q"], env, tmp_path)
    assert r.returncode == 69 and "500" in r.stderr
