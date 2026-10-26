"""The video-drop-door gateway plugin and its ensure script (docs/runbook.md,
"Video drop door").

The plugin is loaded in node with a fake plugin API and drives a fake video.py
that logs its argv, so these tests pin which messages reach the door, that the
message travels as one argument with no shell, and that every doubt falls
through to the model. The door's matcher and receipt are video.py's, tested in
test_video_eval.py.
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
PLUGIN = REPO / "compose/openclaw-gateway/plugins/video-drop-door"
ENSURE = REPO / "scripts/ensure-video-drop-door.sh"
PATCH = json.loads((REPO / "compose/openclaw-gateway/platform.patch.json").read_text())

CHAT = "424242"
LINK = "https://youtu.be/bQPU6UJ4iCw"

# Answers like video.py's door: a receipt for a link it takes, exit 2 for
# anything else, "video failed:" or a crash on the links named so.
FAKE_VIDEO_PY = r"""
import json, os, sys
open(os.environ["DOOR_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\n")
cmd, arg = sys.argv[1], sys.argv[2]
if cmd == "door-file":
    print(f"video queued: video file {os.path.basename(arg)} - position 1 of 1")
elif arg.endswith("/failing"):
    sys.stderr.write("video failed: the queue is full\n")
    sys.exit(1)
elif arg.endswith("/crashing"):
    sys.stderr.write("Traceback (most recent call last):\n")
    sys.exit(1)
elif arg.startswith("https://youtu.be/"):
    print("video queued: youtube video x - position 1 of 1\n")
else:
    sys.exit(2)
"""

# Loads the plugin against a fake API and plays the scenarios: each is a list
# of steps, a step an [env, hook, event, ctx] whose result is printed.
RUNNER = r"""
const [plugin, scenarios] = process.argv.slice(1);
const mod = await import(plugin);
const hooks = {};
const opts = {};
mod.default.register({ on: (name, fn, o) => { hooks[name] = fn; opts[name] = o; } });
const out = { timeouts: opts.before_dispatch, results: [] };
for (const steps of JSON.parse(scenarios)) {
  const got = [];
  for (const [env, hook, event, ctx] of steps) {
    if (env === null) delete process.env.LIFEKIT_TELEGRAM_CHAT;
    else process.env.LIFEKIT_TELEGRAM_CHAT = env;
    got.push((await hooks[hook](event, ctx)) ?? null);
  }
  out.results.push(got);
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture
def state(tmp_path):
    skill = tmp_path / "agents/kit/workspace/skills/video-eval"
    skill.mkdir(parents=True)
    (skill / "video.py").write_text(FAKE_VIDEO_PY)
    (tmp_path / "media/inbound").mkdir(parents=True)
    return tmp_path


def _ctx(**kw):
    return {
        "channelId": "telegram",
        "accountId": "default",
        "conversationId": CHAT,
        "sessionKey": "agent:kit:telegram:direct:" + CHAT,
        "messageId": "7",
        "senderId": CHAT,
        **kw,
    }


def _text(text, env=CHAT, event=None, **ctx):
    return [
        env,
        "before_dispatch",
        {"content": text, "isGroup": False, **(event or {})},
        _ctx(**ctx),
    ]


def _media(path, content_type="video/mp4", env=CHAT, n=1, **ctx):
    media = [{"path": str(path), "contentType": content_type, "kind": "video"}] * n
    return [env, "message_received", {"media": media, "messageId": "7"}, _ctx(**ctx)]


def _play(state, *scenarios):
    node = shutil.which("node")
    log = state / "door.log"
    log.write_text("")
    r = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            RUNNER,
            (PLUGIN / "index.js").as_uri(),
            json.dumps(scenarios),
        ],
        env={**os.environ, "OPENCLAW_STATE_DIR": str(state), "DOOR_LOG": str(log)},
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(r.stdout)
    calls = [json.loads(ln) for ln in log.read_text().splitlines()]
    return out, calls


needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


@needs_node
def test_a_bare_link_from_the_owner_to_kit_gets_the_scripts_receipt(state):
    out, calls = _play(
        state, [_text(f"  {LINK}\n")], [_text(LINK, conversationId=f"telegram:{CHAT}")]
    )
    want = {"handled": True, "text": "video queued: youtube video x - position 1 of 1"}
    assert out["results"] == [[want], [want]]
    assert calls == [["door", LINK], ["door", LINK]]
    assert (
        out["timeouts"]["timeoutMs"] > 45_000
    )  # the hook outlives the script's own cap


@needs_node
def test_the_message_reaches_the_script_as_one_argument_never_a_shell(state):
    text = f"{LINK};touch${{IFS}}pwned`id`$(id)"
    out, calls = _play(state, [_text(text)])
    assert calls == [["door", text]]
    assert not (state / "pwned").exists()


@needs_node
@pytest.mark.parametrize(
    "step",
    [
        _text(f"{LINK} is this true?"),
        _text(f"is this true? {LINK}"),
        _text("hello"),
        _text(""),
        _text(LINK, env=""),
        _text(LINK, env=None),
        _text(LINK, conversationId="999"),
        _text(LINK, accountId="finance"),
        _text(LINK, channelId="discord"),
        _text(LINK, sessionKey="agent:finance:telegram:direct:" + CHAT),
        _text(LINK, event={"isGroup": True}),
    ],
)
def test_anything_else_goes_to_the_model_unchanged(state, step):
    out, calls = _play(state, [step])
    assert out["results"] == [[None]]
    # Prose never spawns the script; the JS look ahead is only a prefilter.
    assert calls == []


@needs_node
def test_a_failure_the_script_explains_is_answered_and_a_crash_falls_through(state):
    out, _ = _play(
        state, [_text("https://youtu.be/failing")], [_text("https://youtu.be/crashing")]
    )
    assert out["results"] == [
        [{"handled": True, "text": "video failed: the queue is full"}],
        [None],
    ]


@needs_node
def test_a_link_the_script_refuses_goes_to_the_model(state):
    out, calls = _play(state, [_text("https://www.instagram.com/stories/x/")])
    assert out["results"] == [[None]] and calls == [
        ["door", "https://www.instagram.com/stories/x/"]
    ]


@needs_node
def test_a_video_file_from_the_owner_is_queued_whatever_the_caption(state):
    saved = state / "media/inbound/abc.mp4"
    out, calls = _play(
        state,
        [_media(saved), _text("")],
        [_media(saved), _text("what do you think?")],
        [_media(saved), _text(""), _text("")],  # the note is used once
    )
    want = {
        "handled": True,
        "text": "video queued: video file abc.mp4 - position 1 of 1",
    }
    assert out["results"] == [[None, want], [None, want], [None, want, None]]
    assert calls == [["door-file", str(saved)]] * 3


@needs_node
@pytest.mark.parametrize(
    "step",
    [
        _media("/tmp/abc.mp4"),
        _media("MEDIA/../../abc.mp4"),
        _media("MEDIA/abc.jpg", content_type="image/jpeg"),
        _media("MEDIA/abc.mp4", n=2),
        _media("MEDIA/abc.mp4", conversationId="999"),
        _media("MEDIA/abc.mp4", accountId="finance"),
        _media("MEDIA/abc.mp4", env=None),
    ],
)
def test_a_file_that_is_not_one_owner_video_in_the_inbound_dir_is_the_models(
    state, step
):
    step[2]["media"] = [
        {**m, "path": m["path"].replace("MEDIA", str(state / "media/inbound"))}
        for m in step[2]["media"]
    ]
    if step[2]["media"][0]["contentType"] == "image/jpeg":
        step[2]["media"][0]["kind"] = "image"
    out, calls = _play(state, [step, _text("")])
    assert out["results"] == [[None, None]] and calls == []


def test_the_platform_patch_allows_and_enables_the_door():
    assert "video-drop-door" in PATCH["plugins"]["allow"]
    assert PATCH["plugins"]["entries"]["video-drop-door"] == {"enabled": True}
    manifest = json.loads((PLUGIN / "openclaw.plugin.json").read_text())
    package = json.loads((PLUGIN / "package.json").read_text())
    assert manifest["id"] == "video-drop-door"
    assert package["openclaw"]["extensions"] == ["./index.js"]


def test_the_gateway_gets_the_owner_chat_the_door_compares_with():
    compose = (REPO / "compose/openclaw/docker-compose.yml").read_text()
    assert "LIFEKIT_TELEGRAM_CHAT: ${LIFEKIT_TELEGRAM_CHAT:-}" in compose


# --- the ensure script ----------------------------------------------------------


def _ensure(tmp_path, live_config=None):
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
    log = tmp_path / "docker.log"
    log.write_text("")
    r = subprocess.run(
        ["bash", str(ENSURE)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "OPENCLAW_CONFIG_DIR": str(config),
            "DOCKER_LOG": str(log),
            "GATEWAY": "gw1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return r, config / "extensions/video-drop-door", log.read_text()


ENABLED = {
    "plugins": {
        "allow": ["video-drop-door"],
        "entries": {"video-drop-door": {"enabled": True}},
    }
}


def test_ensure_installs_the_plugin_readable_and_not_writable_by_others(tmp_path):
    r, dest, _ = _ensure(tmp_path, live_config={})
    assert r.returncode == 0, r.stderr
    for f in ("index.js", "openclaw.plugin.json", "package.json"):
        assert (dest / f).read_bytes() == (PLUGIN / f).read_bytes()
        assert stat.S_IMODE((dest / f).stat().st_mode) == 0o644
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755
    assert "not enabled in the live config yet" in r.stdout


def test_ensure_reloads_an_enabled_plugin_only_when_it_changed(tmp_path):
    r, dest, docker = _ensure(tmp_path, live_config=ENABLED)
    assert r.returncode == 0, r.stderr
    assert "exec gw1 openclaw plugins reload video-drop-door" in docker
    r, _, docker = _ensure(tmp_path, live_config=ENABLED)
    assert "unchanged" in r.stdout and "reload" not in docker
