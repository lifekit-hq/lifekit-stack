"""The intake-door gateway plugin and its ensure script (docs/runbook.md,
"Intake door"): every agent files through `file_intake`, a hand-off that fails
lands in the spool, the retry files it once, and a repeat adds a count.

The plugin runs under node against a fake devclaw MCP server that answers like
devclaw's `file_intake`: one issue per repo + fingerprint, a repeat bumps its
count, an unknown project is rejected, and it can be taken down.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "compose/openclaw-gateway/plugins/intake-door"
ENSURE = REPO / "scripts/ensure-intake-door.sh"
PATCH = json.loads((REPO / "compose/openclaw-gateway/platform.patch.json").read_text())
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# The projects devclaw's intake knows: each repository owner, plus vault and fleet.
PROJECTS = {
    "devclaw": "lifekit-hq/devclaw",
    "lifekit-stack": "lifekit-hq/lifekit-stack",
    "lifekit-dashboard": "lifekit-hq/lifekit-dashboard",
    "finance-sentry": "lifekit-hq/finance-sentry",
    "lifekit-common": "lifekit-hq/lifekit-common",
    "vault": "example/memory",
    "fleet": "lifekit-hq/lifekit-stack",
}


class FakeDevclaw:
    """devclaw's file_intake over MCP streamable HTTP (SSE answers)."""

    def __init__(self):
        self.down = False
        self.calls: list[dict] = []
        self.issues: dict[tuple[str, str], dict] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["content-length"])))
                if fake.down:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"github unavailable")
                    return
                args = req["params"]["arguments"]
                fake.calls.append({"auth": self.headers.get("authorization"), **args})
                result = {
                    "content": [{"type": "text", "text": json.dumps(fake.file(args))}]
                }
                body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result})
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                self.wfile.write(f"event: message\ndata: {body}\n\n".encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def file(self, a):
        repo = PROJECTS.get(a["project_id"])
        if repo is None:
            reason = f"unknown project_id '{a['project_id']}'; valid projects: {', '.join(PROJECTS)}"
            return {
                "id": "",
                "url": "",
                "state": "rejected",
                "grade": {"ok": False, "reason": reason},
            }
        if a["owner"] != a["project_id"] or a["class_"] != "dev" or not a["acceptance"]:
            return {
                "id": "",
                "url": "",
                "state": "rejected",
                "grade": {"ok": False, "reason": "bad envelope"},
            }
        fp = a["fingerprint"] or a["title"].lower()
        key = (repo, fp)
        if key in self.issues:
            self.issues[key]["count"] += 1
            created = False
        else:
            self.issues[key] = {
                "number": len(self.issues) + 1,
                "count": 1,
                "body": a["body"],
            }
            created = True
        n = self.issues[key]["number"]
        return {
            "id": f"{repo}#{n}",
            "url": f"https://github.com/{repo}/issues/{n}",
            "state": "submitted" if created else "working",
            "grade": {"ok": True, "reason": ""},
            "number": n,
            "count": self.issues[key]["count"],
            "created": created,
            "fingerprint": fp,
        }

    def close(self):
        self.server.shutdown()


@pytest.fixture
def devclaw():
    fake = FakeDevclaw()
    yield fake
    fake.close()


# Runs one step against door.js / the plugin; stdin = {op, ...}, stdout = JSON.
RUNNER = r"""
import { readFileSync } from "node:fs";
const req = JSON.parse(readFileSync(0, "utf8"));
const door = await import(process.env.DOOR);
let out;
if (req.op === "file") out = await door.file(req.params, req.agent);
else if (req.op === "retry") out = await door.retry();
else if (req.op === "count") out = door.count();
else if (req.op === "plugin") {
  const plugin = (await import(process.env.INDEX)).default;
  const tools = [], services = [];
  plugin.register({
    registrationMode: "full",
    registerTool: (t, opts) => tools.push({ t, opts }),
    registerService: (s) => services.push(s),
    logger: { info() {}, warn() {} },
  });
  const tool = tools[0].t({ agentId: req.agent });
  const res = await tool.execute("call-1", req.params);
  const scheduled = [];
  services[0].start({ scheduler: { schedule: (j) => scheduled.push({ id: j.id, everyMs: j.everyMs }) } });
  out = {
    name: tool.name, opts: tools[0].opts, required: tool.parameters.required,
    props: Object.keys(tool.parameters.properties), result: res,
    service: services[0].id, scheduled,
  };
}
process.stdout.write(JSON.stringify(out));
"""


def step(tmp_path, devclaw_url, op, **req):
    env = {
        **os.environ,
        "DOOR": (PLUGIN / "door.js").as_uri(),
        "INDEX": (PLUGIN / "index.js").as_uri(),
        "INTAKE_DOOR_DEVCLAW_URL": devclaw_url,
        "INTAKE_DOOR_SPOOL": str(tmp_path / "spool"),
        "INTAKE_DOOR_TIMEOUT_MS": "3000",
        "DEVCLAW_MCP_TOKEN": "test-token",
    }
    r = subprocess.run(
        [NODE, "--input-type=module", "-e", RUNNER],
        input=json.dumps({"op": op, **req}),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def item(
    project, fp="deploy-red", title="Deploy went red on the gateway phase", **envelope
):
    return {
        "project": project,
        "title": title,
        "body": "error text and time",
        "fingerprint": fp,
        "envelope": {"class": "ops", "acceptance": "deploy green", **envelope},
    }


def spooled(tmp_path):
    d = tmp_path / "spool"
    return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []


@needs_node
@pytest.mark.parametrize("project", sorted(PROJECTS))
def test_an_agent_files_for_each_project(tmp_path, devclaw, project):
    got = step(tmp_path, devclaw.url, "file", params=item(project), agent="learnclaw")
    repo = PROJECTS[project]
    assert (
        got["id"] == f"{repo}#1"
        and got["state"] == "submitted"
        and got["spooled"] is False
    )
    call = devclaw.calls[0]
    assert call["auth"] == "Bearer test-token"
    assert (
        call["project_id"] == project
        and call["owner"] == project
        and call["class_"] == "dev"
    )
    assert call["origin"] == "learnclaw" and call["fingerprint"] == "deploy-red"
    # The filer's own envelope travels in the body.
    assert "class: ops" in call["body"] and "acceptance: deploy green" in call["body"]
    assert spooled(tmp_path) == []


@needs_node
def test_a_repeat_adds_a_count_not_a_second_item(tmp_path, devclaw):
    first = step(
        tmp_path, devclaw.url, "file", params=item("lifekit-stack"), agent="kit"
    )
    again = step(
        tmp_path, devclaw.url, "file", params=item("lifekit-stack"), agent="finance"
    )
    assert first["id"] == again["id"]
    assert again["state"] == "working" and again["count"] == 2
    assert len(devclaw.issues) == 1


@needs_node
def test_a_missing_envelope_is_filled_not_refused(tmp_path, devclaw):
    got = step(
        tmp_path,
        devclaw.url,
        "file",
        params={"project": "vault", "title": "x"},
        agent="career",
    )
    assert got["state"] == "submitted"
    call = devclaw.calls[0]
    assert call["acceptance"].startswith("unclear") and "class: unclear" in call["body"]
    assert call["origin"] == "career"


@needs_node
def test_a_refused_hand_off_lands_in_the_spool_and_the_retry_files_it_once(
    tmp_path, devclaw
):
    devclaw.down = True
    got = step(
        tmp_path, devclaw.url, "file", params=item("finance-sentry"), agent="finance"
    )
    assert got["spooled"] is True and got["id"].startswith("spool:")
    assert got["state"] == "submitted" and "HTTP 503" in got["reason"]
    assert "never hand it to the owner" in got["next"]
    assert len(spooled(tmp_path)) == 1
    # A repeat while down is a second occurrence of the same item: counted.
    again = step(
        tmp_path, devclaw.url, "file", params=item("finance-sentry"), agent="kit"
    )
    assert again["id"] == got["id"] and again["state"] == "working"
    assert step(tmp_path, devclaw.url, "count")["pending"] == 2

    # Still down: the retry keeps both, counting the attempt.
    out = step(tmp_path, devclaw.url, "retry")
    assert out["filed"] == 0 and out["failed"] == 2
    assert len(spooled(tmp_path)) == 2

    devclaw.down = False
    out = step(tmp_path, devclaw.url, "retry")
    assert out["filed"] == 2 and out["failed"] == 0
    assert spooled(tmp_path) == []
    # One issue, the repeat a count on it.
    assert list(devclaw.issues.values()) == [
        devclaw.issues[("lifekit-hq/finance-sentry", "deploy-red")]
    ]
    assert devclaw.issues[("lifekit-hq/finance-sentry", "deploy-red")]["count"] == 2
    # A later repeat after the retry still counts onto the same issue.
    later = step(
        tmp_path, devclaw.url, "file", params=item("finance-sentry"), agent="kit"
    )
    assert later["state"] == "working" and later["count"] == 3
    # Nothing left to file: a second retry is a no-op.
    assert step(tmp_path, devclaw.url, "retry")["filed"] == 0
    assert step(tmp_path, devclaw.url, "count") == {
        "schema": "intake-spool-count.v1",
        "pending": 0,
        "stuck": 0,
        "oldest_epoch": None,
    }


@needs_node
def test_an_unknown_project_is_spooled_then_filed_under_fleet(tmp_path, devclaw):
    got = step(
        tmp_path, devclaw.url, "file", params=item("no-such-repo"), agent="learnclaw"
    )
    assert got["spooled"] is True and "unknown project" in got["reason"]
    out = step(tmp_path, devclaw.url, "retry")
    assert out["filed"] == 1 and out["refiled_to_fleet"] == 1
    issue = devclaw.issues[("lifekit-hq/lifekit-stack", "deploy-red")]
    assert "no intake project `no-such-repo`" in issue["body"]
    assert spooled(tmp_path) == []


@needs_node
def test_an_unreachable_devclaw_is_spooled_and_counted_stuck(tmp_path):
    url = "http://127.0.0.1:9/mcp"  # nothing listens
    got = step(tmp_path, url, "file", params=item("devclaw"), agent="kit")
    assert got["spooled"] is True and "unreachable" in got["reason"]
    for _ in range(6):
        step(tmp_path, url, "retry")
    c = step(tmp_path, url, "count")
    assert c["pending"] == 1 and c["stuck"] == 1 and isinstance(c["oldest_epoch"], int)


@needs_node
def test_the_plugin_registers_the_tool_and_the_retry_service(tmp_path, devclaw):
    got = step(tmp_path, devclaw.url, "plugin", params=item("vault"), agent="career")
    assert got["name"] == "file_intake" and got["opts"] == {"name": "file_intake"}
    assert got["required"] == ["project", "title"]
    assert got["props"] == ["project", "title", "body", "fingerprint", "envelope"]
    receipt = json.loads(got["result"]["content"][0]["text"])
    assert (
        receipt["id"] == "example/memory#1"
        and got["result"]["details"]["receipt"] == receipt
    )
    assert devclaw.calls[0]["origin"] == "career"
    assert got["service"] == "intake-spool-retry"
    assert got["scheduled"] == [{"id": "retry", "everyMs": 600000}]


@needs_node
def test_the_spool_cli_prints_the_count(tmp_path, devclaw):
    devclaw.down = True
    step(tmp_path, devclaw.url, "file", params=item("lifekit-common"), agent="kit")
    env = {**os.environ, "INTAKE_DOOR_SPOOL": str(tmp_path / "spool")}
    r = subprocess.run(
        [NODE, str(PLUGIN / "spool.mjs"), "count"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    doc = json.loads(r.stdout)
    assert (
        doc["schema"] == "intake-spool-count.v1"
        and doc["pending"] == 1
        and doc["stuck"] == 0
    )


# --- platform patch, manifest, install --------------------------------------------


def test_the_platform_patch_allows_and_enables_the_plugin():
    assert "intake-door" in PATCH["plugins"]["allow"]
    assert PATCH["plugins"]["entries"]["intake-door"] == {"enabled": True}


def test_the_manifest_declares_the_tool():
    manifest = json.loads((PLUGIN / "openclaw.plugin.json").read_text())
    assert manifest["id"] == "intake-door"
    assert manifest["contracts"]["tools"] == ["file_intake"]
    pkg = json.loads((PLUGIN / "package.json").read_text())
    assert pkg["openclaw"]["extensions"] == ["./index.js"]
    assert set(pkg["files"]) == {
        "index.js",
        "door.js",
        "spool.mjs",
        "openclaw.plugin.json",
    }


LIVE = {
    "tools.allow": ["read", "devclaw__*"],
    "agents.entries": {
        "kit": {"tools": {"profile": "coding", "alsoAllow": ["message"]}},
        "career": {
            "tools": {"profile": "coding", "alsoAllow": ["message", "sessions_send"]}
        },
        "learnclaw": {"tools": {"profile": "coding"}},
        "ledger": {"tools": {"profile": "coding"}},
        "reader": {"tools": {"allow": ["read"], "deny": ["devclaw__*"]}},
        "study-panel": {"tools": {"allow": [], "deny": ["*"]}},
    },
}


def _ensure(tmp_path, live, *args):
    """Runs the ensure script against a docker stub that serves `live` to
    `config get` and applies `config patch --stdin` (a deep merge) to it."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    state = tmp_path / "live.json"
    state.write_text(json.dumps(live))
    docker = bindir / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "a = sys.argv[1:]\n"
        "open(os.environ['DOCKER_LOG'], 'a').write(' '.join(a) + '\\n')\n"
        "if a[0] == 'ps':\n"
        "    print(os.environ['GATEWAY']); sys.exit(0)\n"
        "live = json.load(open(os.environ['LIVE']))\n"
        "if a[-3:-1] == ['config', 'get'] or a[-4:-2] == ['config', 'get']:\n"
        "    key = a[a.index('get') + 1]\n"
        "    if key not in live: print('Config path not found: ' + key); sys.exit(1)\n"
        "    print(json.dumps(live[key])); sys.exit(0)\n"
        "if 'patch' in a:\n"
        "    p = json.load(sys.stdin)\n"
        "    open(os.environ['PATCHES'], 'a').write(json.dumps(p) + '\\n')\n"
        "    if 'allow' in p.get('tools', {}): live['tools.allow'] = p['tools']['allow']\n"
        "    for i, e in p.get('agents', {}).get('entries', {}).items():\n"
        "        live['agents.entries'].setdefault(i, {}).setdefault('tools', {}).update(e['tools'])\n"
        "    json.dump(live, open(os.environ['LIVE'], 'w'))\n"
        "sys.exit(0)\n"
    )
    docker.chmod(0o755)
    config = tmp_path / "config"
    (config / "extensions").mkdir(parents=True, exist_ok=True)
    for f in ("docker.log", "patches.jsonl"):
        (tmp_path / f).write_text("")
    r = subprocess.run(
        ["bash", str(ENSURE), *args],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "OPENCLAW_CONFIG_DIR": str(config),
            "DOCKER_LOG": str(tmp_path / "docker.log"),
            "PATCHES": str(tmp_path / "patches.jsonl"),
            "LIVE": str(state),
            "GATEWAY": "gw1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return r, json.loads(state.read_text()), (tmp_path / "docker.log").read_text()


def test_ensure_installs_the_plugin_and_gives_every_agent_with_a_door_the_tool(
    tmp_path,
):
    r, live, docker = _ensure(tmp_path, LIVE)
    assert r.returncode == 0, r.stderr + r.stdout
    dest = tmp_path / "config/extensions/intake-door"
    for f in (
        "index.js",
        "door.js",
        "spool.mjs",
        "openclaw.plugin.json",
        "package.json",
    ):
        assert (dest / f).read_bytes() == (PLUGIN / f).read_bytes()
        assert stat.S_IMODE((dest / f).stat().st_mode) == 0o644
    assert "exec gw1 openclaw plugins reload intake-door" in docker
    assert live["tools.allow"] == ["read", "devclaw__*", "file_intake"]
    e = live["agents.entries"]
    assert e["kit"]["tools"]["alsoAllow"] == ["message", "file_intake"]
    assert e["career"]["tools"]["alsoAllow"] == [
        "message",
        "sessions_send",
        "file_intake",
    ]
    assert e["learnclaw"]["tools"]["alsoAllow"] == ["file_intake"]
    assert e["ledger"]["tools"]["alsoAllow"] == ["file_intake"]
    # The doorless agents keep their caps exactly.
    assert e["reader"] == LIVE["agents.entries"]["reader"]
    assert e["study-panel"] == LIVE["agents.entries"]["study-panel"]

    # Converged: a second run patches nothing and reloads nothing.
    r, live2, docker = _ensure(tmp_path, live)
    assert r.returncode == 0, r.stderr
    assert live2 == live and "reload" not in docker and "patch" not in docker
    r, _, _ = _ensure(tmp_path, live, "--check")
    assert r.returncode == 0, r.stderr


def test_ensure_check_names_each_list_without_the_tool(tmp_path):
    _ensure(tmp_path, LIVE)  # install the files
    r, live, docker = _ensure(tmp_path, LIVE, "--check")
    assert r.returncode == 1
    assert (
        "tools.allow" in r.stderr
        and "agents.entries.learnclaw.tools.alsoAllow" in r.stderr
    )
    assert "reader" not in r.stderr and "patch" not in docker
    assert live == LIVE


def test_ensure_leaves_an_unset_global_allow_list_unset(tmp_path):
    live = {k: v for k, v in LIVE.items() if k != "tools.allow"}
    r, got, _ = _ensure(tmp_path, live)
    assert r.returncode == 0, r.stderr
    assert "tools.allow" not in got
    assert got["agents.entries"]["kit"]["tools"]["alsoAllow"][-1] == "file_intake"
