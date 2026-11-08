"""The fleet-gate gateway plugin and its ensure script (docs/runbook.md,
"Fleet gate").

The plugin is loaded in node with a fake plugin API, a P16 table written by
the test and a fake ssh that records what it is asked to file, so these tests
pin the table lookup (A, L, S, an unlisted tool, a malformed table, a missing
table), that a stop is blocked and filed through the relay with the body on
stdin and nothing through a shell, and that a gate that cannot decide blocks
nothing it should not and says so.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "compose/openclaw-gateway/plugins/fleet-gate"
ENSURE = REPO / "scripts/ensure-fleet-gate.sh"
PATCH = json.loads((REPO / "compose/openclaw-gateway/platform.patch.json").read_text())

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)

SCHEMA = "p16-openclaw.v1"
ASK = {
    "class": "S",
    "id": "openclaw.ask-user",
    "ruling": "agent-behavior.md#P16.2",
}


def table(**tools):
    return {"schema": SCHEMA, "tools": tools}


STANDARD = table(
    **{
        "read": {"class": "A", "id": "openclaw.own-workspace"},
        "write": {"class": "L", "id": "openclaw.own-workspace", "ruling": "P16.1"},
        "ask_user": ASK,
        "sessions_send": {**ASK, "when": "non-owner-to-main"},
        "devclaw__*": {"class": "L", "id": "app.read"},
        "devclaw__delete_*": {"class": "S", "id": "app.delete-record"},
        "google-workspace__send_*": {"class": "S", "id": "google.gmail-send-other"},
    }
)

# Plays steps against the plugin and prints what each hook returned. A step is
# [hook, event, ctx], or {"table": text} to rewrite the table file mid-run.
RUNNER = r"""
import fs from "node:fs";
const [plugin, steps] = process.argv.slice(1);
const mod = await import(plugin);
const hooks = {}; const opts = {};
mod.default.register({
  on: (name, fn, o) => { hooks[name] = fn; opts[name] = o; },
  logger: { warn: (m) => process.stderr.write(m + "\n") },
});
const results = [];
for (const step of JSON.parse(steps)) {
  if (!Array.isArray(step)) { fs.writeFileSync(process.env.FLEET_GATE_TABLE, step.table); results.push(null); continue; }
  const [hook, event, ctx] = step;
  results.push((await hooks[hook](event, ctx)) ?? null);
}
process.stdout.write(JSON.stringify({ results, opts, hooks: Object.keys(hooks) }));
"""

FAKE_SSH = r"""#!/bin/sh
dir="$SSH_TEST_DIR"
n=$(ls "$dir" | grep -c '^argv' || true)
printf '%s\n' "$@" > "$dir/argv.$n"
cat > "$dir/stdin.$n"
if [ -f "$dir/exit" ]; then
  echo "$(cat "$dir/said")" >&2
  exit "$(cat "$dir/exit")"
fi
printf '{"schema":"kit-relay-received.v1"}\n'
"""


@pytest.fixture
def box(tmp_path):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    ssh = tmp_path / "fake-ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    (tmp_path / "state/logs").mkdir(parents=True)
    return tmp_path


def write_table(box, doc):
    path = box / "p16-openclaw.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return path


def play(box, *steps, table_doc=STANDARD, **env):
    if table_doc is not None and not (box / "p16-openclaw.json").exists():
        write_table(box, table_doc)
    r = subprocess.run(
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            RUNNER,
            (PLUGIN / "index.js").as_uri(),
            json.dumps(steps),
        ],
        env={
            **os.environ,
            "OPENCLAW_STATE_DIR": str(box / "state"),
            "FLEET_GATE_TABLE": str(box / "p16-openclaw.json"),
            "FLEET_GATE_RELAY_CONF": "/relay/ssh_config",
            "FLEET_GATE_SSH": str(box / "fake-ssh"),
            "SSH_TEST_DIR": str(box / "ssh"),
            **env,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(r.stdout)
    out["stderr"] = r.stderr
    return out


def log_lines(box):
    path = box / "state/logs/fleet-gate.jsonl"
    return (
        [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    )


def filed(box):
    return sorted(
        (
            (
                (box / "ssh" / a).read_text().splitlines(),
                (box / "ssh" / a.replace("argv", "stdin")).read_text(),
            )
            for a in os.listdir(box / "ssh")
            if a.startswith("argv")
        ),
        key=lambda x: x[0],
    )


def before(tool, params=None, agent="finance", owner=False, requester=True, **event):
    ctx = {"agentId": agent, "sessionKey": f"agent:{agent}:cron:1", "runId": "run-1"}
    if requester:
        ctx["requester"] = {"senderIsOwner": owner}
    return [
        "before_tool_call",
        {
            "toolName": tool,
            "params": params or {},
            "runId": "run-1",
            "toolCallId": "tc-1",
            **event,
        },
        ctx,
    ]


def after(tool="write", **event):
    return [
        "after_tool_call",
        {"toolName": tool, "params": {}, "toolCallId": "tc-1", **event},
        {"agentId": "finance"},
    ]


# --- the table lookup ---------------------------------------------------------


@needs_node
def test_a_class_a_call_is_left_alone_and_leaves_no_trace(box):
    out = play(box, before("read"))
    assert out["results"] == [None]
    assert log_lines(box) == [] and filed(box) == []


@needs_node
def test_a_class_l_call_goes_through_and_is_logged_with_its_outcome(box):
    out = play(
        box,
        before("write", agent="kit"),
        after(result={"ok": True}, durationMs=12.4),
    )
    assert out["results"] == [None, None]
    taken, outcome = log_lines(box)
    assert taken["event"] == "taken" and taken["class"] == "L"
    assert taken["id"] == "openclaw.own-workspace" and taken["ruling"] == "P16.1"
    assert taken["agent"] == "kit" and taken["tool"] == "write"
    assert taken["toolCallId"] == "tc-1" and taken["runId"] == "run-1"
    assert outcome["event"] == "outcome" and outcome["outcome"] == "returned"
    assert outcome["durationMs"] == 12 and outcome["toolCallId"] == "tc-1"
    assert filed(box) == []


@needs_node
@pytest.mark.parametrize(
    ("done", "want"),
    [
        ({"error": "disk full"}, "error"),
        ({"result": "ok"}, "returned"),
        ({}, "unknown"),  # neither result nor error is not proof of success
    ],
)
def test_the_outcome_line_does_not_read_a_missing_error_as_success(box, done, want):
    play(box, before("write"), after(**done))
    assert log_lines(box)[-1]["outcome"] == want


@needs_node
def test_an_outcome_is_only_logged_for_a_call_the_gate_logged(box):
    play(
        box, before("read"), after("read", result="x"), after("write", toolCallId="zz")
    )
    assert log_lines(box) == []


@needs_node
def test_a_class_s_call_is_blocked_and_filed_through_the_relay(box):
    out = play(box, before("ask_user", {"question": "Shall I move 5 EUR to savings?"}))
    [res] = out["results"]
    assert res["block"] is True
    rid = res["blockReason"].split("filed for the owner as ")[1].split(". ")[0]
    assert rid.startswith("oc-") and len(rid) > 20
    assert res["blockReason"] == (
        f"Stopped under P16 (openclaw.ask-user): filed for the owner as {rid}. "
        "Carry on with other work; do not ask him in chat."
    )
    [(argv, body)] = filed(box)
    assert argv == [
        "-F", "/relay/ssh_config", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
        "relay", "note", rid,
    ]  # fmt: skip
    for want in (
        f"P16 stop {rid}",
        "class: openclaw.ask-user",
        "ruling: agent-behavior.md#P16.2",
        "agent: finance",
        "tool: ask_user",
        "session: agent:finance:cron:1",
        "run: run-1",
        "Shall I move 5 EUR to savings?",
    ):
        assert want in body, want
    [line] = log_lines(box)
    assert line["event"] == "stopped" and line["class"] == "S"
    assert line["filed"] is True and line["request"] == rid
    assert line["id"] == "openclaw.ask-user"


@needs_node
def test_the_same_stop_is_one_request_and_a_different_one_is_another(box):
    out = play(
        box,
        before("ask_user", {"question": "a"}, toolCallId="t1"),
        before("ask_user", {"question": "a"}, toolCallId="t2"),
        before("ask_user", {"question": "b"}, toolCallId="t3"),
    )
    rids = [r["blockReason"].split(" as ")[1].split(".")[0] for r in out["results"]]
    assert rids[0] == rids[1] != rids[2]


@needs_node
def test_nothing_the_agent_sent_reaches_a_shell_and_secrets_are_not_filed(box):
    evil = "x`touch pwned`$(touch pwned);touch pwned"
    play(
        box,
        before(
            "ask_user",
            {"question": evil, "api_key": "sk-secret", "Authorization": "Bearer t"},
        ),
    )
    [(argv, body)] = filed(box)
    assert evil not in " ".join(argv)
    assert evil in body.replace("\\", "") or "touch pwned" in body
    assert "sk-secret" not in body and "Bearer t" not in body
    assert "[redacted]" in body
    assert not (box / "pwned").exists()


@needs_node
def test_a_long_param_is_cut_before_it_is_filed(box):
    play(box, before("ask_user", {"question": "q" * 50_000}))
    [(_, body)] = filed(box)
    assert len(body) < 4_000


@needs_node
def test_an_unlisted_tool_is_allowed_and_logged_as_unlisted(box):
    out = play(box, before("brand_new_tool", agent="career"))
    assert out["results"] == [None]
    [line] = log_lines(box)
    assert line["event"] == "unlisted" and line["effect"] == "allowed"
    assert line["tool"] == "brand_new_tool" and line["agent"] == "career"
    assert filed(box) == []


@needs_node
def test_an_exact_row_beats_a_prefix_and_the_longest_prefix_beats_a_shorter(box):
    out = play(
        box,
        before("devclaw__list_prs"),  # devclaw__* -> L
        before("devclaw__delete_branch"),  # longer prefix -> S
        before("mcp__devclaw__delete_branch"),  # mcp__ is the same tool
        before("google-workspace__send_message"),
    )
    assert out["results"][0] is None
    assert [r["block"] for r in out["results"][1:]] == [True, True, True]
    assert [x["id"] for x in log_lines(box) if x["event"] == "stopped"] == [
        "app.delete-record",
        "app.delete-record",
        "google.gmail-send-other",
    ]


# --- who is asking ------------------------------------------------------------


@needs_node
@pytest.mark.parametrize(
    ("params", "kw", "stopped"),
    [
        ({"sessionKey": "agent:kit:main", "message": "x"}, {}, True),
        ({"sessionKey": "agent:kit:telegram:direct:1", "message": "x"}, {}, True),
        ({"sessionKey": "main", "message": "x"}, {}, True),
        ({"agentId": "kit", "message": "x"}, {}, True),
        ({"label": "something", "message": "x"}, {}, True),  # names no agent: counted
        ({"sessionKey": "agent:kit:main", "message": "x"}, {"requester": False}, True),
        ({"sessionKey": "agent:finance:main", "message": "x"}, {}, False),
        ({"agentId": "career", "message": "x"}, {}, False),
        ({"sessionKey": "agent:kit:main", "message": "x"}, {"owner": True}, False),
    ],
)
def test_sessions_send_to_the_main_agent_from_a_non_owner_run_is_an_ask(
    box, params, kw, stopped
):
    out = play(box, before("sessions_send", params, **kw))
    assert (out["results"][0] is not None) is stopped
    if stopped:
        assert out["results"][0]["block"] is True
        assert len(filed(box)) == 1
    else:
        assert filed(box) == []


@needs_node
def test_an_unknown_when_counts_as_holding_so_a_stop_is_never_relaxed(box):
    doc = table(sessions_send={**ASK, "when": "from-a-future-schema"})
    out = play(
        box, before("sessions_send", {"agentId": "career"}, owner=True), table_doc=doc
    )
    assert out["results"][0]["block"] is True


@needs_node
def test_the_table_can_make_ask_user_conditional_on_a_non_owner_run(box):
    doc = table(ask_user={**ASK, "when": "non-owner"})
    out = play(
        box,
        before("ask_user", {"question": "q"}, owner=True),
        before("ask_user", {"question": "q"}, owner=False),
        table_doc=doc,
    )
    assert out["results"][0] is None and out["results"][1]["block"] is True


# --- a gate that cannot decide ------------------------------------------------


@needs_node
def test_a_missing_table_blocks_nothing_and_logs_once_a_minute(box):
    out = play(
        box, before("ask_user"), before("write"), before("ask_user"), table_doc=None
    )
    assert out["results"] == [None, None, None]
    [line] = log_lines(box)
    assert line["event"] == "table-missing" and line["effect"] == "allowed"
    assert "p16-openclaw.json" in line["detail"]
    assert "not gated" in out["stderr"]
    assert filed(box) == []


@needs_node
@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("{not json", "not JSON"),
        ("[]", "schema"),
        (json.dumps({"schema": "p16-openclaw.v0", "tools": {"a": ASK}}), "schema"),
        (json.dumps({"schema": SCHEMA}), "tools"),
        (json.dumps({"schema": SCHEMA, "tools": []}), "tools"),
        (
            json.dumps({"schema": SCHEMA, "tools": {"a": {"class": "X"}}}),
            "no usable row",
        ),
        (json.dumps({"schema": SCHEMA, "tools": {}}), "no usable row"),
    ],
)
def test_a_malformed_table_blocks_nothing_and_logs(box, text, why):
    out = play(box, before("ask_user"), table_doc=text)
    assert out["results"] == [None]
    [line] = log_lines(box)
    assert line["event"] == "table-malformed" and why in line["detail"]
    assert filed(box) == []


@needs_node
def test_a_bad_row_is_skipped_and_the_rest_of_the_table_still_applies(box):
    doc = table(
        ask_user=ASK,
        broken={"class": "stop"},
        also_broken="S",
        write={"class": "L", "id": 7},
    )
    out = play(box, before("ask_user"), before("broken"), table_doc=doc)
    assert out["results"][0]["block"] is True and out["results"][1] is None


@needs_node
def test_a_table_swapped_on_disk_is_picked_up_without_a_reload(box):
    swapped = json.dumps(table(read={"class": "S", "id": "any.public"}))
    out = play(box, before("read"), {"table": swapped}, before("read"))
    assert out["results"][0] is None
    assert out["results"][2]["block"] is True


@needs_node
@pytest.mark.parametrize(
    ("exit_code", "said"),
    [
        (255, "Can't open user config file /relay/ssh_config"),
        (75, "kit-relay: refused: rate limit"),
    ],
)
def test_a_stop_is_still_blocked_when_filing_it_fails_and_says_so(box, exit_code, said):
    (box / "ssh/exit").write_text(str(exit_code))
    (box / "ssh/said").write_text(said)
    out = play(box, before("ask_user", {"question": "q"}))
    [res] = out["results"]
    assert res["block"] is True
    assert res["blockReason"].startswith(
        "Stopped under P16 (openclaw.ask-user): filing it for the owner failed ("
    )
    assert said[:40] in res["blockReason"]
    assert res["blockReason"].endswith(
        "Carry on with other work; do not ask him in chat."
    )
    assert "filed for the owner as" not in res["blockReason"]
    [line] = log_lines(box)
    assert line["filed"] is False and said[:40] in line["error"]


@needs_node
def test_a_missing_ssh_or_a_hung_one_still_ends_in_a_block(box):
    out = play(box, before("ask_user"), FLEET_GATE_SSH=str(box / "no-such-ssh"))
    assert out["results"][0]["block"] is True
    assert "failed" in out["results"][0]["blockReason"]
    hang = box / "hang-ssh"
    hang.write_text("#!/bin/sh\nexec sleep 30\n")
    hang.chmod(0o755)
    out = play(
        box, before("ask_user"), FLEET_GATE_SSH=str(hang), FLEET_GATE_FILE_MS="300"
    )
    assert out["results"][0]["block"] is True
    assert "failed" in out["results"][0]["blockReason"]


@needs_node
def test_the_hook_never_throws_on_an_odd_event(box):
    out = play(
        box,
        ["before_tool_call", {}, {}],
        ["before_tool_call", {"toolName": "ask_user", "params": None}, None],
        ["after_tool_call", {}, None],
    )
    assert out["results"][0] is None
    assert out["results"][1]["block"] is True  # the stop holds with nothing else known
    assert out["results"][2] is None


@needs_node
def test_the_log_survives_an_unwritable_path_and_the_stop_still_holds(box):
    (box / "a-file").write_text("not a directory")
    out = play(
        box,
        before("ask_user"),
        before("write"),
        FLEET_GATE_LOG=str(box / "a-file/logs/fleet-gate.jsonl"),
    )
    assert out["results"][0]["block"] is True and out["results"][1] is None


@needs_node
def test_the_hook_keeps_the_hosts_fifteen_second_fail_closed_timeout(box):
    out = play(box, before("read"))
    assert out["hooks"] == ["before_tool_call", "after_tool_call"]
    # No timeoutMs override and no matcher: the host default (15 s, fail closed)
    # applies to every tool.
    assert not out["opts"].get("before_tool_call")


# --- wiring -------------------------------------------------------------------


def test_the_platform_patch_allows_and_enables_the_gate():
    assert "fleet-gate" in PATCH["plugins"]["allow"]
    assert PATCH["plugins"]["entries"]["fleet-gate"] == {"enabled": True}
    manifest = json.loads((PLUGIN / "openclaw.plugin.json").read_text())
    package = json.loads((PLUGIN / "package.json").read_text())
    assert manifest["id"] == "fleet-gate"
    assert package["openclaw"]["extensions"] == ["./index.js"]


def test_the_gateway_mounts_the_oc_relay_key_read_only_in_its_own_directory():
    import yaml

    compose = yaml.safe_load((REPO / "compose/openclaw/docker-compose.yml").read_text())
    vols = compose["services"]["openclaw-gateway"]["volumes"]
    assert (
        "${LIFEKIT_SECRETS_DIR:-/srv/lifekit-secrets}/oc-relay:/run/lifekit/oc-relay:ro"
        in vols
    )


# --- the ensure script --------------------------------------------------------


def _ensure(tmp_path, live_config=None, table_doc=None, **env):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(
        '#!/bin/sh\necho "$*" >> "$DOCKER_LOG"\n'
        'case "$1" in ps) [ -n "$GATEWAY" ] && echo "$GATEWAY" ;; esac\nexit 0\n'
    )
    docker.chmod(0o755)
    config = tmp_path / "config"
    (config / "extensions").mkdir(parents=True, exist_ok=True)
    if live_config is not None:
        (config / "openclaw.json").write_text(json.dumps(live_config))
    src = tmp_path / "policy/p16-openclaw.json"
    if table_doc is not None:
        src.parent.mkdir(exist_ok=True)
        src.write_text(
            table_doc if isinstance(table_doc, str) else json.dumps(table_doc)
        )
    log = tmp_path / "docker.log"
    log.write_text("")
    r = subprocess.run(
        ["bash", str(ENSURE)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "OPENCLAW_CONFIG_DIR": str(config),
            "P16_OPENCLAW_TABLE": str(src),
            "DOCKER_LOG": str(log),
            "GATEWAY": "gw1",
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return r, config / "extensions/fleet-gate", log.read_text()


ENABLED = {
    "plugins": {
        "allow": ["fleet-gate"],
        "entries": {"fleet-gate": {"enabled": True}},
    }
}


def test_ensure_installs_the_plugin_and_the_table_readable_and_not_writable(tmp_path):
    r, dest, _ = _ensure(tmp_path, live_config={}, table_doc=STANDARD)
    assert r.returncode == 0, r.stderr
    for f in ("index.js", "openclaw.plugin.json", "package.json"):
        assert (dest / f).read_bytes() == (PLUGIN / f).read_bytes()
        assert stat.S_IMODE((dest / f).stat().st_mode) == 0o644
    assert json.loads((dest / "p16-openclaw.json").read_text()) == STANDARD
    assert stat.S_IMODE((dest / "p16-openclaw.json").stat().st_mode) == 0o644
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755
    assert "not enabled in the live config yet" in r.stdout


def test_ensure_reloads_an_enabled_plugin_only_when_its_code_changed(tmp_path):
    r, dest, docker = _ensure(tmp_path, live_config=ENABLED, table_doc=STANDARD)
    assert r.returncode == 0, r.stderr
    assert "exec gw1 openclaw plugins reload fleet-gate" in docker
    r, _, docker = _ensure(tmp_path, live_config=ENABLED, table_doc=STANDARD)
    assert "unchanged" in r.stdout and "reload" not in docker
    # A new table alone is picked up by the plugin's mtime check: no reload.
    newer = table(read={"class": "S", "id": "any.public"})
    r, dest, docker = _ensure(tmp_path, live_config=ENABLED, table_doc=newer)
    assert "table installed" in r.stdout and "reload" not in docker
    assert json.loads((dest / "p16-openclaw.json").read_text()) == newer


def test_ensure_without_a_table_warns_and_installs_the_plugin_anyway(tmp_path):
    r, dest, _ = _ensure(tmp_path, live_config={})
    assert r.returncode == 0, r.stderr
    assert "no P16 table" in r.stderr and "table-missing" in r.stderr
    assert (dest / "index.js").exists() and not (dest / "p16-openclaw.json").exists()


def test_ensure_keeps_the_installed_table_when_the_source_goes_missing(tmp_path):
    _ensure(tmp_path, live_config={}, table_doc=STANDARD)
    (tmp_path / "policy/p16-openclaw.json").unlink()
    r, dest, _ = _ensure(tmp_path, live_config={})
    assert r.returncode == 0 and "keeping the installed one" in r.stdout
    assert json.loads((dest / "p16-openclaw.json").read_text()) == STANDARD


@pytest.mark.parametrize(
    "bad",
    [
        "{nope",
        json.dumps({"schema": "other", "tools": {"a": ASK}}),
        json.dumps({"schema": SCHEMA, "tools": {}}),
        json.dumps({"schema": SCHEMA, "tools": {"a": {"class": "X"}}}),
    ],
)
def test_ensure_refuses_an_invalid_table_and_leaves_the_installed_one(tmp_path, bad):
    _ensure(tmp_path, live_config={}, table_doc=STANDARD)
    r, dest, _ = _ensure(tmp_path, live_config={}, table_doc=bad)
    assert r.returncode == 1 and "not a valid p16-openclaw.v1 table" in r.stderr
    assert json.loads((dest / "p16-openclaw.json").read_text()) == STANDARD


# --- the rehearsal ------------------------------------------------------------


@needs_node
def test_the_rehearsal_shows_an_a_call_untouched_and_an_s_call_blocked_and_filed(
    tmp_path,
):
    doc = tmp_path / "table.json"
    doc.write_text(json.dumps(STANDARD))
    for args in ([], [str(doc)]):
        r = subprocess.run(
            ["bash", str(REPO / "scripts/rehearse-fleet-gate.sh"), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "untouched" in r.stdout and "BLOCKED" in r.stdout
        assert "filed: relay note oc-" in r.stdout and "rehearsal: ok" in r.stdout
