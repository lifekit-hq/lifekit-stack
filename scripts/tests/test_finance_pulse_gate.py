"""The ledger-pulse gate (scripts/finance-pulse-gate): pulse_gate.py against a
fake finance-sentry MCP server, and gate.js against canned pulse_gate.py output."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[2] / "scripts/finance-pulse-gate"
TOKEN = "t0k3n"
# 15:20 Europe/Dublin in November (UTC+0) - one of the two non-morning pulse runs.
AFTERNOON = "2026-11-17T15:20:00Z"
MORNING = "2026-11-17T09:20:00Z"
SUNDAY_MORNING = "2026-11-22T09:20:00Z"
NOW = datetime(2026, 11, 17, 15, 20, tzinfo=timezone.utc)


def ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


class FakeMcp(BaseHTTPRequestHandler):
    events: list = []
    health: list = []
    sse = False
    calls: list = []

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(401)
            self.end_headers()
            return
        method = body.get("method")
        if "id" not in body:  # a notification
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {}}
        else:
            name = body["params"]["name"]
            type(self).calls.append((name, body["params"]["arguments"]))
            value = (
                {"events": self.events, "mode": "scan"}
                if name == "get_pending_companion_events"
                else self.health
            )
            result = {"content": [{"type": "text", "text": json.dumps(value)}]}
        payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        if self.sse:
            out, ctype = f"event: message\ndata: {payload}\n\n", "text/event-stream"
        else:
            out, ctype = payload, "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(out.encode())


@pytest.fixture
def mcp():
    FakeMcp.events, FakeMcp.health, FakeMcp.sse, FakeMcp.calls = [], [], False, []
    server = HTTPServer(("127.0.0.1", 0), FakeMcp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


def check(url, now=AFTERNOON, token=TOKEN):
    env = {
        **os.environ,
        "FINANCE_SENTRY_MCP_URL": url,
        "FINANCE_SENTRY_MCP_TOKEN": token,
        "PULSE_GATE_NOW": now,
    }
    r = subprocess.run(
        [sys.executable, "-I", str(GATE / "pulse_gate.py"), "check"],
        env=env,
        capture_output=True,
        text=True,
    )
    return r.returncode, r.stdout.strip()


def provider(name, hours, **extra):
    return {"provider": name, "lastSyncAt": ago(hours), "status": "ok", **extra}


def test_empty_case_does_not_wake(mcp):
    FakeMcp.health = [provider("Monobank", 2), provider("IBKR", 5)]
    assert check(mcp) == (0, "PULSE idle")
    assert FakeMcp.calls[0] == (
        "get_pending_companion_events",
        {"includeHeldForDigest": True, "heldOverrideReason": "ledger-pulse gate check"},
    )


def test_pending_events_wake(mcp):
    FakeMcp.events = [{"id": "e1", "kind": "NewsCluster"}]
    FakeMcp.health = [provider("Monobank", 2)]
    code, out = check(mcp)
    assert code == 0
    assert out.splitlines() == ["PULSE wake", "1 pending companion event(s)"]


def test_a_stale_provider_wakes_but_a_data_source_or_never_synced_does_not(mcp):
    FakeMcp.health = [
        provider("sec-edgar", 200, kind="data_source"),
        {"provider": "Revolut X", "lastSyncAt": None, "status": "never_synced"},
        provider("Binance", 25),
    ]
    code, out = check(mcp)
    assert code == 0
    assert out.splitlines() == ["PULSE wake", "provider sync older than 24h: Binance"]
    FakeMcp.health = [provider("Binance", 23)]
    assert check(mcp) == (0, "PULSE idle")


def test_morning_slot_wakes_without_asking_finance_sentry(mcp):
    for now in (MORNING, SUNDAY_MORNING):
        code, out = check(mcp, now=now)
        assert code == 0 and out.startswith("PULSE wake")
    assert FakeMcp.calls == []
    # 09:23 is past the checklist's 09:22 cutoff.
    assert check(mcp, now="2026-11-17T09:23:00Z") == (0, "PULSE idle")


def test_morning_cutoff_follows_dublin_summer_time(mcp):
    # 08:20Z in July is 09:20 in Dublin (UTC+1); 09:20Z is 10:20 there.
    assert check(mcp, now="2026-07-14T08:20:00Z")[1].startswith("PULSE wake")
    assert check(mcp, now="2026-07-14T09:20:00Z") == (0, "PULSE idle")


def test_sse_answers_are_read(mcp):
    FakeMcp.sse = True
    FakeMcp.events = [{"id": "e1"}]
    assert check(mcp)[1].startswith("PULSE wake")


def test_faults_are_reported_not_silenced(mcp):
    code, out = check(mcp, token="wrong")
    assert code == 1 and out == "PULSE fault: finance-sentry MCP answered HTTP 401"
    code, out = check("http://127.0.0.1:1/")
    assert code == 1 and out.startswith("PULSE fault: finance-sentry MCP unreachable")
    assert check(mcp, token="")[1] == "PULSE fault: FINANCE_SENTRY_MCP_TOKEN is not set"


GATE_RUNNER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const out = process.argv[2];
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const exec = async ({ command }) => {
  process.stderr.write(command + "\n");
  return { aggregated: out };
};
new AsyncFunction("exec", src)(exec).then(
  (r) => process.stdout.write(JSON.stringify(r)),
  (e) => { process.stdout.write("THROW " + e.message); },
);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize(
    "out,want",
    [
        ("PULSE idle\n", {"fire": False}),
        (
            "PULSE wake\n2 pending companion event(s)\nprovider sync older than 24h: X\n",
            {
                "fire": True,
                "message": "Gate wake reason: 2 pending companion event(s); "
                "provider sync older than 24h: X",
            },
        ),
        ("PULSE wake\n", "THROW pulse_gate.py check: PULSE wake"),
        ("PULSE fault: boom\n", "THROW pulse_gate.py check: PULSE fault: boom"),
        ("", "THROW pulse_gate.py check: no output"),
    ],
)
def test_gate_js_fires_only_on_a_wake_and_throws_on_anything_odd(out, want):
    r = subprocess.run(
        ["node", "-e", GATE_RUNNER, str(GATE / "gate.js"), out],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stderr.strip() == (
        "python3 /home/node/.openclaw/gates/ledger-pulse/pulse_gate.py check"
    )
    assert (json.loads(r.stdout) if r.stdout.startswith("{") else r.stdout) == want
