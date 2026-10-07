"""The youtube-transcript skill and the PC tunnel unit behind it
(docs/runbook.md, "YouTube transcripts through the PC").

yt-dlp is replaced by a stub that writes the caption files a real run would,
so nothing here touches YouTube or the network beyond a loopback socket.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills/youtube-transcript"
INSTALLER = REPO / "scripts/yt-tunnel/install-yt-tunnel.sh"
UNIT = REPO / "scripts/yt-tunnel/yt-tunnel.service"
ENSURE = REPO / "scripts/ensure-youtube-transcript.sh"

_SPEC = importlib.util.spec_from_file_location("transcript", SKILL / "transcript.py")
transcript = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(transcript)

VID = "bQPU6UJ4iCw"

AUTO_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.160 --> 00:00:02.230 align:start position:0%

Today we're<00:00:00.640><c> going</c><00:00:00.880><c> to</c> talk

00:00:02.230 --> 00:00:02.240 align:start position:0%
Today we're going to talk

00:00:02.240 --> 00:00:04.000 align:start position:0%
Today we're going to talk
about&nbsp;AI &gt;&gt; agents
"""

# A yt-dlp stand-in: argv decides what it writes; STUB_MODE picks the scenario.
STUB = r"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
argv = sys.argv[1:]
mode = os.environ["STUB_MODE"]
out = Path(argv[argv.index("-o") + 1]).parent
open(os.environ["STUB_LOG"], "a").write(" ".join(argv) + "\n")
Path(argv[argv.index("--print-to-file") + 2]).write_text("A title\n")
manual = "--write-subs" in argv
if mode == "unreachable":
    sys.stderr.write("ERROR: Unable to connect to proxy: [Errno 111] Connection refused\n")
    sys.exit(1)
if mode == "boom":
    sys.stderr.write("ERROR: [youtube] x: Video unavailable\n")
    sys.exit(1)
if mode == "none":
    sys.exit(0)
if mode == "manual" and manual:
    (out / f"{os.environ['VID']}.en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHuman caption\n")
if mode == "auto" and not manual:
    (out / f"{os.environ['VID']}.en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nTranslated\n")
    (out / f"{os.environ['VID']}.en-orig.vtt").write_text(os.environ["AUTO_VTT"])
"""


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def run(tmp_path):
    stub = tmp_path / "yt-dlp-stub"
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / "calls.log"

    def go(arg, mode="auto", proxy=None, listening=True, extra_env=None):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1] if listening else closed_port()
        if not listening:
            listener.close()
        env = {
            **os.environ,
            "YT_DLP": str(stub),
            "YT_TRANSCRIPT_PROXY": proxy or f"socks5h://127.0.0.1:{port}",
            "STUB_MODE": mode,
            "STUB_LOG": str(log),
            "VID": VID,
            "AUTO_VTT": AUTO_VTT,
            **(extra_env or {}),
        }
        try:
            r = subprocess.run(
                [sys.executable, str(SKILL / "transcript.py"), arg],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
        finally:
            listener.close()
        return r, log.read_text() if log.exists() else ""

    return go


@pytest.mark.parametrize(
    "arg",
    [
        VID,
        f"https://www.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/watch?v={VID}&t=30s&list=PLx",
        f"https://youtu.be/{VID}?si=abc",
        f"https://m.youtube.com/shorts/{VID}",
        f"youtube.com/embed/{VID}",
        f"https://www.youtube.com/live/{VID}",
    ],
)
def test_video_id_forms(arg):
    assert transcript.video_id(arg) == VID


@pytest.mark.parametrize(
    "arg",
    [
        "",
        "short",
        "https://example.com/watch?v=" + VID,
        "https://www.youtube.com/",
        "https://youtu.be/",
    ],
)
def test_video_id_rejects(arg):
    assert transcript.video_id(arg) is None


def test_vtt_to_text_drops_tags_timing_and_rolling_repeats():
    assert (
        transcript.vtt_to_text(AUTO_VTT)
        == "Today we're going to talk\nabout AI >> agents"
    )


def test_pick_prefers_original_language_track():
    files = [Path("v.en.vtt"), Path("v.en-orig.vtt")]
    assert transcript.pick(files) == Path("v.en-orig.vtt")
    assert transcript.pick([Path("v.en.vtt")]) == Path("v.en.vtt")
    assert transcript.pick([]) is None


def test_pc_off_closed_port_is_a_normal_answer_and_never_calls_yt_dlp(run):
    r, calls = run(VID, listening=False)
    assert r.returncode == 0
    assert r.stdout.strip() == "transcript unavailable - the PC is off"
    assert calls == ""


def test_proxy_up_but_pc_unreachable_is_pc_off(run):
    r, _ = run(VID, mode="unreachable")
    assert (r.returncode, r.stdout.strip()) == (
        0,
        "transcript unavailable - the PC is off",
    )


def test_human_captions_win_and_auto_pass_is_skipped(run):
    r, calls = run(VID, mode="manual")
    assert r.returncode == 0
    assert r.stdout.startswith(
        "Title: A title\nVideo: https://www.youtube.com/watch?v=" + VID
    )
    assert r.stdout.rstrip().endswith("Human caption")
    assert len(calls.splitlines()) == 1 and "--write-subs" in calls


def test_auto_captions_are_the_fallback_and_use_the_original_track(run):
    r, calls = run(VID, mode="auto")
    assert r.returncode == 0
    assert "Today we're going to talk\nabout AI >> agents" in r.stdout
    assert "Translated" not in r.stdout
    manual_call, auto_call = calls.splitlines()
    assert "--write-subs" in manual_call and "--write-auto-subs" not in manual_call
    assert "--write-auto-subs" in auto_call


def test_yt_dlp_is_called_through_the_proxy_with_captions_only(run):
    _, calls = run(
        VID, mode="manual", proxy="socks5h://127.0.0.1:1"
    )  # 1 is closed: no call
    assert calls == ""
    r, calls = run(VID, mode="manual")
    argv = calls.splitlines()[0].split()
    assert argv[argv.index("--proxy") + 1].startswith("socks5h://127.0.0.1:")
    assert "--skip-download" in argv
    assert "--cookies" not in argv and "--cookies-from-browser" not in argv


def test_no_captions_is_a_normal_answer(run):
    r, _ = run(VID, mode="none")
    assert (r.returncode, r.stdout.strip()) == (
        0,
        "transcript unavailable - this video has no captions",
    )


def test_unexpected_yt_dlp_failure_is_exit_1_with_the_reason(run):
    r, _ = run(VID, mode="boom")
    assert r.returncode == 1
    assert r.stdout == ""
    assert "Video unavailable" in r.stderr


def test_bad_argument_is_a_usage_error(run):
    r, calls = run("not a video")
    assert r.returncode == 2 and r.stdout == "" and calls == ""


def test_langs_override_reaches_yt_dlp(run):
    _, calls = run(VID, mode="manual", extra_env={"YT_TRANSCRIPT_LANGS": "uk.*"})
    assert "--sub-langs uk.*" in calls


# --- the tunnel unit and its installer ---------------------------------------


def render(**env):
    return subprocess.run(
        ["bash", str(INSTALLER), "--print"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "YT_TUNNEL_BIND": "172.17.0.1",
            "YT_TUNNEL_HOST": "pc-alias",
            **env,
        },
    )


def test_unit_is_a_user_unit_forwarding_only_to_the_docker_bridge():
    r = render()
    assert r.returncode == 0, r.stderr
    unit = r.stdout
    assert not any(
        f"@{p}@" in unit for p in ("BIND", "PORT", "HOST")
    )  # every placeholder filled
    assert "-D 172.17.0.1:18081" in unit
    assert "\n  pc-alias\n" in unit
    for opt in ("ExitOnForwardFailure=yes", "ServerAliveInterval=15", "BatchMode=yes"):
        assert opt in unit
    assert "Restart=always" in unit and "RestartMaxDelaySec" in unit
    assert "StartLimitIntervalSec=0" in unit
    assert "WantedBy=default.target" in unit
    assert "0.0.0.0" not in unit


def test_installer_overrides_and_refuses_wildcard_bind():
    r = render(YT_TUNNEL_PORT="1999", YT_TUNNEL_HOST="somepc")
    assert "-D 172.17.0.1:1999" in r.stdout and "\n  somepc\n" in r.stdout
    for bad in ("0.0.0.0", "::", "*"):
        r = render(YT_TUNNEL_BIND=bad)
        assert r.returncode == 1 and "docker bridge only" in r.stderr
    assert render(YT_TUNNEL_PORT="x").returncode == 1
    r = render(YT_TUNNEL_HOST="")
    assert r.returncode == 1 and "YT_TUNNEL_HOST" in r.stderr


def test_unit_template_runs_nothing_on_the_pc():
    text = UNIT.read_text()
    assert " -N " in text  # no remote command
    assert "/etc" not in text.replace("under /etc", "")


def test_ensure_script_pins_ytdlp_by_checksum_and_installs_the_skill_files():
    text = ENSURE.read_text()
    assert "sha256sum -c" in text
    assert "YT_DLP_SHA256=" in text and "YT_DLP_VERSION=" in text
    assert "transcript.py" in text and "SKILL.md" in text
    assert (
        (SKILL / "SKILL.md").read_text().startswith("---\nname: youtube-transcript\n")
    )
