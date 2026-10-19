"""Behaviour of scripts/openclaw-sso.sh, the operator's OpenClaw sign-in gate
cutover and its rollback (docs/runbook.md "OpenClaw behind the sign-in gate"),
run as a subprocess against stubbed `docker`, `tailscale`, `curl` and `id` on
PATH. This is the migration's CI rehearsal: the order of the steps, what each
direction writes to the gateway config and to Tailscale Serve, that the
preflight changes nothing when something it depends on is missing, and that a
failed result rolls itself back to exactly the state before.

The docker stub applies `openclaw config patch` the way the CLI documents it
(objects merge, arrays replace, null deletes, --replace-path replaces) to a
scratch openclaw.json, so the tests assert on the resulting config.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/openclaw-sso.sh"
TAILNET = "box.example-tailnet.ts.net"
EMAIL = "operator@example.com"
EDGE = "http://127.0.0.1:18892"
GATEWAY = "http://127.0.0.1:18789"
TOKEN_AUTH = {
    "mode": "token",
    "token": {"source": "env", "provider": "default", "id": "OPENCLAW_GATEWAY_TOKEN"},
}

DOCKER_STUB = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
line = " ".join(args)
with open(os.environ["DOCKER_CALL_LOG"], "a") as f:
    f.write(line + "\n")
config_path = os.environ["STUB_CONFIG"]


def load():
    with open(config_path) as f:
        return json.load(f)


def save(data):
    with open(config_path, "w") as f:
        json.dump(data, f)


def merge(base, patch):
    for k, v in patch.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base


if args[:1] == ["inspect"]:
    target = args[-1]
    if target == "finance-sentry-api":
        url = os.environ.get("FS_BASEURL")
        if url is None:
            sys.exit(1)
        print("Agent__OpenClaw__BaseUrl=" + url if url else "")
    elif target == "edgeid":
        print(os.environ.get("EDGE_INGRESS_IP", "10.231.78.2"))
    elif target == "gwid":
        print(os.environ.get("GW_INGRESS_IP", "10.231.78.9"))
    sys.exit()
if args[:2] == ["network", "inspect"]:
    print("172.30.0.1")
    sys.exit()
if args[:1] != ["compose"]:
    sys.exit(1)
if " ps -q " in f" {line} ":
    if line.endswith(" traefik"):
        print("edgeid")
    elif line.endswith(" oauth2-proxy"):
        print(os.environ.get("OAUTH2_PROXY_ID", "oauthid"))
    elif line.endswith(" openclaw-gateway"):
        if " --status running " not in line or os.environ.get("GW_RUNNING", "1"):
            print("gwid")
    sys.exit()
if " health --json" in line:
    sys.exit(int(os.environ.get("HEALTH_EXIT", "0")))
if " up -d " in f" {line} ":
    trusted = "trusted-proxy.yml" in line
    sys.exit(int(os.environ.get("UP_EXIT_TRUSTED" if trusted else "UP_EXIT", "0")))
if " config patch --stdin" in line:
    patch = json.loads(sys.stdin.read())
    if "--dry-run" in args:
        sys.exit(int(os.environ.get("DRYRUN_EXIT", "0")))
    data = load()
    if "--replace-path" in args:
        path = args[args.index("--replace-path") + 1].split(".")
        node, src = data, patch
        for key in path[:-1]:
            node = node.setdefault(key, {})
            src = src[key]
        node[path[-1]] = src.pop(path[-1])
    save(merge(data, patch))
    sys.exit()
if " config set gateway.trustedProxies " in line:
    data = load()
    data.setdefault("gateway", {})["trustedProxies"] = json.loads(args[args.index("gateway.trustedProxies") + 1])
    save(data)
    sys.exit()
sys.exit()
"""

TAILSCALE_STUB = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
state = os.environ["STUB_SERVE"]
with open(os.environ["DOCKER_CALL_LOG"], "a") as f:
    f.write("tailscale " + " ".join(args) + "\n")
if args[:2] == ["status", "--json"]:
    print(json.dumps({"Self": {"DNSName": os.environ["STUB_TAILNET"] + "."}}))
elif args[:3] == ["serve", "status", "--json"]:
    print(open(state).read())
elif args[:2] == ["serve", "--bg"]:
    port = args[2].split("=")[1]
    host = os.environ["STUB_TAILNET"] + ":" + port
    cfg = json.load(open(state))
    cfg.setdefault("TCP", {})[port] = {"HTTPS": True}
    cfg.setdefault("Web", {})[host] = {"Handlers": {"/": {"Proxy": args[3]}}}
    json.dump(cfg, open(state, "w"))
"""

# Browsers carry `Safari/`; the phone app's dashboard tab (WKWebView) does
# not. *_TRUSTED codes apply once the config is in trusted-proxy mode.
CURL_STUB = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["DOCKER_CALL_LOG"], "a") as f:
    f.write("curl " + " ".join(args) + "\n")
try:
    mode = json.load(open(os.environ["STUB_CONFIG"]))["gateway"]["auth"]["mode"]
except Exception:
    mode = "token"
suffix = "_TRUSTED" if mode == "trusted-proxy" else ""


def env(name, default):
    return os.environ.get(name + suffix, os.environ.get(name, default))


if any("api/v1/query" in a for a in args):
    up = env("PROM_UP", "1")
    print(json.dumps({"data": {"result": [{"value": [0, up]}] if up else []}}))
else:
    ua = args[args.index("-A") + 1]
    print(env("BROWSER_CODE", "302") if "Safari/" in ua else env("WEBVIEW_CODE", "200"), end="")
"""


def stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def serve_config(target: str, funnel: bool = False) -> dict:
    cfg = {
        "TCP": {"18789": {"HTTPS": True}},
        "Web": {f"{TAILNET}:18789": {"Handlers": {"/": {"Proxy": target}}}},
    }
    if funnel:
        cfg["AllowFunnel"] = {f"{TAILNET}:18789": True}
    return cfg


ORIGINAL_GATEWAY = {
    "bind": "lan",
    "auth": TOKEN_AUTH,
    "trustedProxies": ["172.30.0.1"],
    "controlUi": {"allowedOrigins": ["http://localhost:18789"], "enabled": True},
}


@pytest.fixture
def box(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir / "docker", DOCKER_STUB)
    stub(bin_dir / "tailscale", TAILSCALE_STUB)
    stub(bin_dir / "curl", CURL_STUB)
    stub(bin_dir / "id", '#!/bin/sh\necho "${STUB_UID:-0}"\n')
    stub(bin_dir / "sleep", "#!/bin/sh\nexit 0\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    env_file = secrets / "stack.env"
    env_file.write_text(
        "OPENCLAW_GATEWAY_TOKEN=tok-value-7f3a\nOPENCLAW_GATEWAY_PASSWORD='pw-value-9c1e'\nOTHER=1\n"
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "openclaw.json"
    config.write_text(json.dumps({"gateway": ORIGINAL_GATEWAY, "agents": {"x": 1}}))
    serve = tmp_path / "serve.json"
    serve.write_text(json.dumps(serve_config(GATEWAY)))
    log = tmp_path / "calls.log"
    log.write_text("")
    return {
        "tmp": tmp_path,
        "bin": bin_dir,
        "env_file": env_file,
        "config_dir": config_dir,
        "config": config,
        "serve": serve,
        "log": log,
        "rollback_file": secrets / "openclaw-sso-rollback.json",
        "env": {"FS_BASEURL": "http://openclaw-edge:18893"},
    }


def run(box, *args, **extra):
    env = {
        **os.environ,
        "PATH": f"{box['bin']}:{os.environ['PATH']}",
        "DOCKER_CALL_LOG": str(box["log"]),
        "ENV_FILE": str(box["env_file"]),
        "OPENCLAW_CONFIG_DIR": str(box["config_dir"]),
        "STUB_CONFIG": str(box["config"]),
        "STUB_SERVE": str(box["serve"]),
        "STUB_TAILNET": TAILNET,
        "OPENCLAW_SSO_POLL_TRIES": "2",
        "OPENCLAW_SSO_POLL_INTERVAL": "0",
        **box["env"],
        **extra,
    }
    env.pop("REPO_DIR", None)
    env.pop("OPENCLAW_COMPOSE_AUTH_MODE", None)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def gateway(box) -> dict:
    return json.loads(box["config"].read_text())["gateway"]


def serve_target(box) -> str:
    cfg = json.loads(box["serve"].read_text())
    return cfg["Web"][f"{TAILNET}:18789"]["Handlers"]["/"]["Proxy"]


def calls(box) -> list[str]:
    return box["log"].read_text().splitlines()


def index_of(lines: list[str], needle: str) -> int:
    return next(i for i, line in enumerate(lines) if needle in line)


def changing_calls(lines: list[str]) -> list[str]:
    return [
        line
        for line in lines
        if " stop openclaw-gateway" in line
        or " up -d " in line
        or ("config patch" in line and "--dry-run" not in line)
        or "config set" in line
        or line.startswith("tailscale serve --bg")
    ]


def test_cutover_moves_the_gateway_behind_the_gate_in_order(box):
    r = run(box, "cutover", "--operator-email", "Operator@Example.com")

    assert r.returncode == 0, r.stdout + r.stderr
    gw = gateway(box)
    assert gw["auth"] == {
        "mode": "trusted-proxy",
        "password": "${OPENCLAW_GATEWAY_PASSWORD}",
        "trustedProxy": {
            "userHeader": "x-auth-request-email",
            "allowUsers": [EMAIL, "service:prometheus", "service:finance-sentry"],
            "deviceAutoApprove": {"enabled": True},
        },
        "identityScopes": {EMAIL: ["operator.admin"]},
    }
    assert gw["trustedProxies"] == ["10.231.78.2"]
    assert gw["controlUi"] == {
        "allowedOrigins": ["http://localhost:18789", f"https://{TAILNET}:18789"],
        "enabled": True,
    }
    assert gw["bind"] == "lan"
    assert json.loads(box["config"].read_text())["agents"] == {"x": 1}
    assert serve_target(box) == EDGE

    lines = calls(box)
    dry = index_of(lines, "--dry-run")
    stop = index_of(lines, " stop openclaw-gateway")
    patch = next(
        i
        for i, line in enumerate(lines)
        if "config patch" in line and "--dry-run" not in line
    )
    up = index_of(lines, " up -d ")
    serve = index_of(lines, "tailscale serve --bg")
    assert dry < stop < patch < up < serve
    # The dry run reads the state the running gateway owns, so it runs inside
    # it; the write runs with the gateway stopped.
    assert " exec -T openclaw-gateway openclaw config patch" in lines[dry]
    assert " run --rm " in lines[patch]
    # The new mode's compose overlay: no published port, no token in env.
    assert "trusted-proxy.yml" in lines[patch]
    assert "trusted-proxy.yml" in lines[up]
    assert "trusted-proxy.yml" not in lines[stop]
    assert index_of(lines, " health --json") > serve
    assert "Live check on the phone now" in r.stdout


def test_cutover_saves_the_exact_previous_state_private(box):
    r = run(box, "cutover", "--operator-email", EMAIL)

    assert r.returncode == 0, r.stdout + r.stderr
    saved = json.loads(box["rollback_file"].read_text())
    assert saved == {
        "auth": TOKEN_AUTH,
        "trustedProxies": ["172.30.0.1"],
        "allowedOrigins": ["http://localhost:18789"],
        "serveTarget": GATEWAY,
    }
    assert stat.S_IMODE(box["rollback_file"].stat().st_mode) == 0o600


def test_cutover_never_puts_the_password_value_anywhere(box):
    r = run(box, "cutover", "--operator-email", EMAIL)

    assert r.returncode == 0, r.stdout + r.stderr
    for text in (r.stdout, r.stderr, box["log"].read_text(), box["config"].read_text()):
        assert "pw-value-9c1e" not in text
        assert "tok-value-7f3a" not in text


def test_preflight_lists_every_missing_dependency_and_changes_nothing(box):
    box["env"]["FS_BASEURL"] = "http://openclaw-gateway:18789"
    box["env_file"].write_text(
        "OPENCLAW_GATEWAY_TOKEN=tok\nOPENCLAW_GATEWAY_PASSWORD=\n"
    )
    r = run(box, "cutover", "--operator-email", EMAIL, PROM_UP="0", OAUTH2_PROXY_ID="")

    assert r.returncode != 0
    assert "not started, nothing changed" in r.stderr
    assert "OPENCLAW_GATEWAY_PASSWORD is not in" in r.stderr
    assert (
        "finance-sentry still calls the gateway at 'http://openclaw-gateway:18789'"
        in r.stderr
    )
    assert "Prometheus does not see" in r.stderr
    assert "oauth2-proxy is not running" in r.stderr
    assert changing_calls(calls(box)) == []
    assert gateway(box) == ORIGINAL_GATEWAY
    assert serve_target(box) == GATEWAY
    assert not box["rollback_file"].exists()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"BROWSER_CODE": "200"}, "not the 302 to sign-in"),
        ({"WEBVIEW_CODE": "302"}, "dashboard tab at http://127.0.0.1:18892/ got 302"),
        ({"EDGE_INGRESS_IP": "10.231.78.9"}, "not running at 10.231.78.2"),
        ({"GW_INGRESS_IP": ""}, "the gateway is not on openclaw-ingress"),
        ({"GW_RUNNING": ""}, "the gateway is not running"),
    ],
)
def test_preflight_checks_the_edge_routes_before_anything_changes(box, extra, message):
    r = run(box, "cutover", "--operator-email", EMAIL, **extra)

    assert r.returncode != 0
    assert message in r.stderr
    assert changing_calls(calls(box)) == []
    assert gateway(box) == ORIGINAL_GATEWAY


def test_preflight_refuses_a_public_funnel(box):
    box["serve"].write_text(json.dumps(serve_config(GATEWAY, funnel=True)))
    r = run(box, "cutover", "--operator-email", EMAIL)

    assert r.returncode != 0
    assert "Funnel is on for :18789" in r.stderr
    assert changing_calls(calls(box)) == []


def test_finance_sentry_absent_from_the_box_is_not_a_blocker(box):
    del box["env"]["FS_BASEURL"]
    r = run(box, "cutover", "--operator-email", EMAIL)

    assert r.returncode == 0, r.stdout + r.stderr


def test_a_rejected_dry_run_changes_nothing(box):
    r = run(box, "cutover", "--operator-email", EMAIL, DRYRUN_EXIT="1")

    assert r.returncode != 0
    assert "the patch was rejected; nothing changed" in r.stderr
    assert changing_calls(calls(box)) == []
    assert gateway(box) == ORIGINAL_GATEWAY
    assert not box["rollback_file"].exists()


@pytest.mark.parametrize(
    "extra",
    [
        {"WEBVIEW_CODE_TRUSTED": "401"},
        {"BROWSER_CODE_TRUSTED": "200"},
        {"HEALTH_EXIT": "1"},
        {"PROM_UP_TRUSTED": "0"},
        {"UP_EXIT_TRUSTED": "1"},
    ],
)
def test_a_failed_result_rolls_back_to_exactly_the_state_before(box, extra):
    before = box["config"].read_text()
    r = run(box, "cutover", "--operator-email", EMAIL, **extra)

    assert r.returncode != 0
    assert "rolling back" in r.stderr
    assert json.loads(box["config"].read_text()) == json.loads(before)
    assert serve_target(box) == GATEWAY
    assert not box["rollback_file"].exists()
    lines = calls(box)
    last_up = max(i for i, line in enumerate(lines) if " up -d " in line)
    assert "trusted-proxy.yml" not in lines[last_up]


def test_a_rollback_that_fails_keeps_the_saved_state_for_a_rerun(box):
    assert run(box, "cutover", "--operator-email", EMAIL).returncode == 0
    saved = box["rollback_file"].read_text()

    r = run(box, "rollback", UP_EXIT="1")

    assert r.returncode == 1
    assert "starting the gateway in token mode" in r.stderr
    assert "re-run: sudo bash scripts/openclaw-sso.sh rollback" in r.stderr
    assert box["rollback_file"].read_text() == saved

    r = run(box, "rollback")

    assert r.returncode == 0, r.stdout + r.stderr
    assert gateway(box) == ORIGINAL_GATEWAY
    assert serve_target(box) == GATEWAY


def test_rollback_restores_exactly_what_cutover_changed(box):
    before = json.loads(box["config"].read_text())
    assert run(box, "cutover", "--operator-email", EMAIL).returncode == 0
    box["log"].write_text("")

    r = run(box, "rollback")

    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(box["config"].read_text()) == before
    assert serve_target(box) == GATEWAY
    assert not box["rollback_file"].exists()
    lines = calls(box)
    stop = index_of(lines, " stop openclaw-gateway")
    patch = index_of(lines, "--replace-path gateway.auth")
    up = index_of(lines, " up -d ")
    serve = index_of(lines, "tailscale serve --bg")
    assert stop < patch < up < serve
    # Back on the base compose file: the gateway's port is published again.
    assert "trusted-proxy.yml" in lines[stop]
    assert "trusted-proxy.yml" not in lines[up]
    assert "token mode, Serve at the gateway" in r.stdout


def test_rollback_without_a_saved_state_restores_token_mode(box):
    assert run(box, "cutover", "--operator-email", EMAIL).returncode == 0
    box["rollback_file"].unlink()
    box["serve"].write_text(json.dumps(serve_config(EDGE)))

    r = run(box, "rollback")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "no " in r.stderr and "restoring token mode" in r.stderr
    gw = gateway(box)
    assert gw["auth"] == TOKEN_AUTH
    # The deploy's own derivation puts the host's proxy address back.
    assert "172.30.0.1" in gw["trustedProxies"]
    assert serve_target(box) == GATEWAY


def test_cutover_and_rollback_need_root(box):
    for args in (("cutover", "--operator-email", EMAIL), ("rollback",)):
        r = run(box, *args, STUB_UID="1000")
        assert r.returncode == 2
        assert "run as root" in r.stderr
    assert changing_calls(calls(box)) == []


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", 'x"@y.z'])
def test_cutover_needs_a_plain_operator_email(box, email):
    r = run(box, "cutover", "--operator-email", email)

    assert r.returncode == 2
    assert changing_calls(calls(box)) == []


def test_cutover_is_a_no_op_once_behind_the_gate(box):
    assert run(box, "cutover", "--operator-email", EMAIL).returncode == 0
    box["log"].write_text("")

    r = run(box, "cutover", "--operator-email", EMAIL)

    assert r.returncode == 0
    assert "already in trusted-proxy mode" in r.stdout
    assert changing_calls(calls(box)) == []


@pytest.mark.parametrize(
    ("mode", "target", "code", "message"),
    [
        ("token", GATEWAY, 0, "today's routing"),
        ("trusted-proxy", EDGE, 0, "behind the sign-in gate"),
        ("trusted-proxy", GATEWAY, 1, "the tailnet URL is down"),
        ("token", EDGE, 1, "does not point at the gateway"),
    ],
)
def test_check_reports_whether_auth_mode_and_serve_agree(
    box, mode, target, code, message
):
    data = json.loads(box["config"].read_text())
    data["gateway"]["auth"] = {"mode": mode}
    box["config"].write_text(json.dumps(data))
    box["serve"].write_text(json.dumps(serve_config(target)))

    r = run(box, "check", STUB_UID="1000")

    assert r.returncode == code
    assert message in r.stdout
    assert changing_calls(calls(box)) == []


# The compose side of the two modes: compose/openclaw/trusted-proxy.yml is
# what openclaw_compose adds in trusted-proxy mode.
yaml = pytest.importorskip("yaml")


class _ComposeLoader(yaml.SafeLoader):
    pass


_ComposeLoader.add_constructor(
    "!reset", lambda loader, node: {"!reset": loader.construct_sequence(node)}
)


def _compose(name):
    return yaml.load((REPO / "compose" / name).read_text(), Loader=_ComposeLoader)


def test_trusted_proxy_overlay_blanks_the_token_unpublishes_the_port_and_adds_the_password():
    overlay = _compose("openclaw/trusted-proxy.yml")["services"]
    gw = overlay["openclaw-gateway"]
    assert gw["ports"] == {"!reset": []}
    for service in ("openclaw-gateway", "openclaw-cli"):
        env = overlay[service]["environment"]
        assert env["OPENCLAW_GATEWAY_TOKEN"] == ""
        assert env["OPENCLAW_GATEWAY_PASSWORD"].startswith(
            "${OPENCLAW_GATEWAY_PASSWORD:?"
        )
    assert set(overlay) == {"openclaw-gateway", "openclaw-cli"}


def test_gateway_and_edge_share_only_the_pinned_ingress_network():
    gw = _compose("openclaw/docker-compose.yml")
    edge = _compose("edge/docker-compose.yml")
    assert gw["services"]["openclaw-gateway"]["networks"]["openclaw-ingress"] == {
        "aliases": ["openclaw-gateway-edge"]
    }
    assert edge["services"]["traefik"]["networks"]["openclaw-ingress"] == {
        "ipv4_address": "10.231.78.2"
    }
    assert gw["networks"]["openclaw-ingress"] == {"external": True}
    assert edge["networks"]["openclaw-ingress"] == {"external": True}
    lib = (REPO / "scripts/lib/deploy-common.sh").read_text()
    assert 'OPENCLAW_EDGE_IP="10.231.78.2"' in lib
