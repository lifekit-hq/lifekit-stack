"""The youtube-transcript skill, its allow-list SOCKS relay, the tunnel units
and the ensure script (docs/runbook.md, "YouTube transcripts through the PC").

yt-dlp is replaced by a stub that writes the caption files a real run would,
and the PC end of the tunnel by a fake SOCKS5 server, so nothing here touches
YouTube or any network beyond loopback sockets.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills/youtube-transcript"
INSTALLER = REPO / "scripts/yt-tunnel/install-yt-tunnel.sh"
RELAY = REPO / "scripts/yt-tunnel/yt-relay.py"
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
if mode == "subs429":
    sys.stderr.write("WARNING: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests\n")
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


def recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"wanted {n} bytes, got {len(buf)}")
        buf += chunk
    return buf


class FakeSocks:
    """A SOCKS5 server on loopback standing in for the ssh -D forward: records
    each CONNECT as (atyp, name, port), answers with `reply`, echoes bytes."""

    def __init__(self, reply: int = 0):
        self.requests: list[tuple[int, str, int]] = []
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                s = self.request
                s.settimeout(5)
                try:
                    _, n = recv_exact(s, 2)
                    recv_exact(s, n)
                    s.sendall(b"\x05\x00")
                    _, _, _, atyp = recv_exact(s, 4)
                    assert atyp == 3
                    name = recv_exact(s, recv_exact(s, 1)[0]).decode()
                    port = int.from_bytes(recv_exact(s, 2), "big")
                    fake.requests.append((atyp, name, port))
                    s.sendall(bytes([5, reply, 0, 1]) + bytes(6))
                    while reply == 0 and (data := s.recv(4096)):
                        s.sendall(data)
                except (EOFError, OSError, AssertionError):
                    pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def run(tmp_path):
    stub = tmp_path / "yt-dlp-stub"
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / "calls.log"

    def go(arg, mode="auto", proxy=None, listening=True, extra_env=None, pc_reply=0):
        fake = FakeSocks(pc_reply)
        port = fake.port if listening else closed_port()
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
            fake.close()
        go.connects = fake.requests
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


def test_precheck_is_a_socks_connect_to_youtube_through_the_proxy(run):
    r, _ = run(VID, mode="manual")
    assert r.returncode == 0
    assert run.connects == [(3, "www.youtube.com", 443)]


def test_relay_up_but_pc_down_is_pc_off_and_never_calls_yt_dlp(run):
    for code in (5, 2):
        r, calls = run(VID, pc_reply=code)
        assert (r.returncode, r.stdout.strip()) == (
            0,
            "transcript unavailable - the PC is off",
        )
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


def test_failed_subtitle_download_is_an_error_not_no_captions(run):
    r, calls = run(VID, mode="subs429")
    assert r.returncode == 1
    assert r.stdout == ""
    assert "429" in r.stderr and "Unable to download video subtitles" in r.stderr
    assert len(calls.splitlines()) == 2  # the auto pass still got its turn


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


# --- the allow-list relay ---------------------------------------------------


@pytest.fixture
def relay():
    procs, fakes = [], []

    def start(upstream_port=None, reply=0):
        fake = None
        if upstream_port is None:
            fake = FakeSocks(reply)
            fakes.append(fake)
            upstream_port = fake.port
        p = subprocess.Popen(
            [
                sys.executable,
                str(RELAY),
                "--listen",
                "127.0.0.1:0",
                "--upstream",
                f"127.0.0.1:{upstream_port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append(p)
        port = int(p.stdout.readline().split(":")[-1])
        return port, fake

    yield start
    for p in procs:
        p.terminate()
        p.wait(timeout=10)
        p.stdout.close()
        p.stderr.close()
    for f in fakes:
        f.close()


def socks_request(port, atyp, addr, dport=443):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(b"\x05\x01\x00")
    assert recv_exact(s, 2) == b"\x05\x00"
    s.sendall(bytes([5, 1, 0, atyp]) + addr + dport.to_bytes(2, "big"))
    return s, recv_exact(s, 10)[1]


def domain(name):
    return bytes([len(name)]) + name.encode()


@pytest.mark.parametrize(
    "name",
    ["www.youtube.com", "YouTube.com", "rr1---sn-x.googlevideo.com.", "i.ytimg.com"],
)
def test_relay_forwards_allowed_names_unresolved_and_pipes_both_ways(relay, name):
    port, fake = relay()
    s, rep = socks_request(port, 3, domain(name))
    with s:
        assert rep == 0
        s.sendall(b"hello")
        assert recv_exact(s, 5) == b"hello"
        s.sendall(b"x" * 100_000)
        assert recv_exact(s, 100_000) == b"x" * 100_000
    assert fake.requests == [(3, name.lower().rstrip("."), 443)]


@pytest.mark.parametrize(
    "atyp, addr",
    [
        (3, domain("example.com")),
        (3, domain("evilyoutube.com")),
        (3, domain("youtube.com.evil.com")),
        (3, domain("notgooglevideo.com")),
        (3, domain("youtube.com@evil.com")),
        (3, domain("192.168.1.1")),
        (3, domain("::1")),
        (3, domain("3232235777")),
        (3, domain("0xc0a80101")),
        (3, domain("localhost")),
        (3, domain("")),
        (1, bytes([192, 168, 1, 1])),
        (4, bytes(15) + b"\x01"),
    ],
)
def test_relay_refuses_everything_else_with_not_allowed(relay, atyp, addr):
    port, fake = relay()
    s, rep = socks_request(port, atyp, addr)
    s.close()
    assert rep == 2
    assert fake.requests == []


def test_relay_refuses_non_connect_commands(relay):
    port, fake = relay()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(b"\x05\x01\x00")
        recv_exact(s, 2)
        s.sendall(b"\x05\x02\x00\x03" + domain("www.youtube.com") + b"\x01\xbb")
        assert recv_exact(s, 10)[1] == 2
    assert fake.requests == []


def test_relay_answers_connection_refused_when_the_upstream_is_down(relay):
    port, _ = relay(upstream_port=closed_port())
    s, rep = socks_request(port, 3, domain("www.youtube.com"))
    s.close()
    assert rep == 5


def test_relay_passes_an_upstream_failure_on_as_connection_refused(relay):
    port, fake = relay(reply=4)
    s, rep = socks_request(port, 3, domain("www.youtube.com"))
    s.close()
    assert rep == 5
    assert fake.requests == [(3, "www.youtube.com", 443)]


def test_skill_precheck_through_the_real_relay(relay, run):
    up, _ = relay()
    r, before = run(VID, mode="manual", proxy=f"socks5h://127.0.0.1:{up}")
    assert r.returncode == 0 and "Human caption" in r.stdout
    down, _ = relay(upstream_port=closed_port())
    r, after = run(VID, proxy=f"socks5h://127.0.0.1:{down}")
    assert r.stdout.strip() == "transcript unavailable - the PC is off"
    assert after == before  # no further yt-dlp call


# --- the tunnel units and their installer -------------------------------------


def render(**env):
    return subprocess.run(
        ["bash", str(INSTALLER), "--print"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "YT_TUNNEL_RELAY_BIND": "172.17.0.1",
            "YT_TUNNEL_HOST": "pc-alias",
            **env,
        },
    )


def parse_units(out: str) -> dict[str, dict[str, dict[str, list[str]]]]:
    """`--print` output -> {unit: {section: {key: [values]}}}, backslash
    continuations joined."""
    units: dict = {}
    cur = sect = None
    pending = ""
    for raw in out.splitlines():
        if raw.startswith("# ==> "):
            cur = units.setdefault(raw.split()[2], {})
            continue
        line = pending + raw.strip()
        pending = ""
        if line.endswith("\\"):
            pending = line[:-1] + " "
            continue
        if not line or line.startswith("#") or cur is None:
            continue
        if line.startswith("["):
            sect = cur.setdefault(line.strip("[]"), {})
        else:
            k, _, v = line.partition("=")
            sect.setdefault(k, []).append(v)
    return units


def test_installer_renders_a_loopback_tunnel_and_a_bridge_relay():
    r = render()
    assert r.returncode == 0, r.stderr
    assert "@" not in "".join(
        ln for ln in r.stdout.splitlines() if not ln.startswith("#")
    )  # every placeholder filled
    units = parse_units(r.stdout)
    assert set(units) == {"yt-tunnel.service", "yt-relay.service"}

    tunnel = units["yt-tunnel.service"]
    ssh = shlex.split(tunnel["Service"]["ExecStart"][0])
    assert ssh[0].endswith("/ssh") and "-N" in ssh  # no remote command
    assert ssh[ssh.index("-D") + 1] == "127.0.0.1:18082"
    assert ssh[-1] == "pc-alias"
    opts = [ssh[i + 1] for i, a in enumerate(ssh) if a == "-o"]
    for opt in ("ExitOnForwardFailure=yes", "BatchMode=yes", "ServerAliveInterval=15"):
        assert opt in opts

    relay = units["yt-relay.service"]
    argv = shlex.split(relay["Service"]["ExecStart"][0])
    assert argv[1].endswith("scripts/yt-tunnel/yt-relay.py") and Path(argv[1]).is_file()
    assert argv[argv.index("--listen") + 1] == "172.17.0.1:18081"
    assert argv[argv.index("--upstream") + 1] == "127.0.0.1:18082"
    assert "yt-tunnel.service" not in relay["Unit"].get("Requires", [])

    for u in units.values():  # user units that outlive the PC being off
        assert u["Install"]["WantedBy"] == ["default.target"]
        assert u["Service"]["Restart"] == ["always"]
        assert u["Unit"]["StartLimitIntervalSec"] == ["0"]


def test_installer_overrides_apply():
    r = render(
        YT_TUNNEL_PORT="1999",
        YT_TUNNEL_BIND="127.0.0.2",
        YT_TUNNEL_RELAY_PORT="1998",
        YT_TUNNEL_HOST="somepc",
    )
    assert r.returncode == 0, r.stderr
    units = parse_units(r.stdout)
    ssh = shlex.split(units["yt-tunnel.service"]["Service"]["ExecStart"][0])
    assert ssh[ssh.index("-D") + 1] == "127.0.0.2:1999" and ssh[-1] == "somepc"
    argv = shlex.split(units["yt-relay.service"]["Service"]["ExecStart"][0])
    assert argv[argv.index("--listen") + 1] == "172.17.0.1:1998"
    assert argv[argv.index("--upstream") + 1] == "127.0.0.2:1999"


@pytest.mark.parametrize(
    "bad", ["172.17.0.1", "0.0.0.0", "::", "*", "10.0.0.5", "localhost"]
)
def test_installer_refuses_a_non_loopback_tunnel_bind(bad):
    r = render(YT_TUNNEL_BIND=bad)
    assert r.returncode == 1 and "loopback only" in r.stderr and r.stdout == ""


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "*"])
def test_installer_refuses_a_wildcard_relay_bind(bad):
    r = render(YT_TUNNEL_RELAY_BIND=bad)
    assert r.returncode == 1 and "docker bridge only" in r.stderr and r.stdout == ""


def test_installer_refuses_bad_ports_and_a_missing_host():
    assert render(YT_TUNNEL_PORT="x").returncode == 1
    assert render(YT_TUNNEL_RELAY_PORT="18082").returncode == 1  # same as the tunnel
    r = render(YT_TUNNEL_HOST="")
    assert r.returncode == 1 and "YT_TUNNEL_HOST" in r.stderr


# --- the ensure script -------------------------------------------------------

PAYLOAD = b"#!/usr/bin/env python3\nprint('a pretend yt-dlp')\n"


@pytest.fixture
def ensure(tmp_path):
    """A repo copy whose pinned yt-dlp sha is PAYLOAD's, a stub curl that
    serves PAYLOAD (or fails), and a stub docker so no gateway is consulted."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload = tmp_path / "payload"
    payload.write_bytes(PAYLOAD)
    curl_log = tmp_path / "curl.log"
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$CURL_LOG"\n'
        '[ -z "$CURL_FAIL" ] || exit 22\n'
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
        'cp "$CURL_PAYLOAD" "$out"\n'
    )
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 1\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()

    def make_repo(matching_pin: bool) -> Path:
        repo = tmp_path / ("repo-pinned" if matching_pin else "repo-real")
        (repo / "scripts").mkdir(parents=True)
        shutil.copytree(SKILL, repo / "skills/youtube-transcript")
        text = ENSURE.read_text()
        if matching_pin:
            old = next(
                ln for ln in text.splitlines() if ln.startswith("YT_DLP_SHA256=")
            )
            text = text.replace(
                old, f'YT_DLP_SHA256="{hashlib.sha256(PAYLOAD).hexdigest()}"'
            )
        (repo / "scripts/ensure-youtube-transcript.sh").write_text(text)
        return repo / "scripts/ensure-youtube-transcript.sh"

    def run_script(script: Path, curl_fail: bool = False):
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "OPENCLAW_CONFIG_DIR": str(state),
            "CURL_LOG": str(curl_log),
            "CURL_PAYLOAD": str(payload),
        }
        if curl_fail:
            env["CURL_FAIL"] = "1"
        r = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, env=env
        )
        calls = curl_log.read_text().splitlines() if curl_log.exists() else []
        return r, calls

    run_script.make_repo = make_repo
    run_script.dest = state / "skills/youtube-transcript"
    return run_script


def test_ensure_refuses_a_download_that_fails_the_pinned_checksum(ensure):
    r, calls = ensure(ensure.make_repo(matching_pin=False))
    assert r.returncode != 0
    assert len(calls) == 1 and "releases/download/" in calls[0]
    assert not (ensure.dest / "yt-dlp").exists()


def test_ensure_installs_a_verified_ytdlp_and_the_skill_then_converges(ensure):
    script = ensure.make_repo(matching_pin=True)
    r, calls = ensure(script)
    assert r.returncode == 0, r.stderr
    assert len(calls) == 1
    ytdlp = ensure.dest / "yt-dlp"
    assert ytdlp.read_bytes() == PAYLOAD and os.access(ytdlp, os.X_OK)
    for f in ("SKILL.md", "transcript.py"):
        assert (ensure.dest / f).read_bytes() == (SKILL / f).read_bytes()
    assert os.access(ensure.dest / "transcript.py", os.X_OK)

    r, calls = ensure(script, curl_fail=True)  # nothing to download the second time
    assert r.returncode == 0, r.stderr
    assert "already in place" in r.stdout
    assert len(calls) == 1  # still only the first run's download


def test_ensure_replaces_a_tampered_ytdlp(ensure):
    script = ensure.make_repo(matching_pin=True)
    ensure(script)
    (ensure.dest / "yt-dlp").write_bytes(b"tampered")
    r, calls = ensure(script)
    assert r.returncode == 0, r.stderr
    assert len(calls) == 2
    assert (ensure.dest / "yt-dlp").read_bytes() == PAYLOAD
