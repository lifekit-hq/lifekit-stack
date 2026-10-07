#!/usr/bin/env python3
"""Fetch a YouTube video's captions as plain text, through the PC tunnel.

YouTube refuses this host's datacenter IP, so the one yt-dlp call that talks to
YouTube goes through a SOCKS proxy on the owner's home connection
(scripts/yt-tunnel: an allow-list relay in front of an ssh tunnel). No API
key, no cookies, no audio download: captions only.

    transcript.py <url-or-video-id>

stdout is the transcript, or one line starting "transcript unavailable - ".
That line is a normal answer, not a failure: exit 0. Exit 1 is an unexpected
yt-dlp failure (reason on stderr), exit 2 a usage error.

Env: YT_TRANSCRIPT_PROXY  SOCKS proxy (default socks5h://host.docker.internal:18081)
     YT_TRANSCRIPT_LANGS  yt-dlp --sub-langs pattern (default en.*)
     YT_DLP               yt-dlp to run (default: yt-dlp beside this file, else PATH)
"""

from __future__ import annotations

import html
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_PROXY = "socks5h://host.docker.internal:18081"
PC_OFF = "transcript unavailable - the PC is off"
NO_CAPTIONS = "transcript unavailable - this video has no captions"

# yt-dlp stderr that means the relay, the tunnel or the PC behind it did not
# answer: the relay is down, or it answered SOCKS 0x05 (upstream unreachable).
# Any other SOCKS error (0x02 allow-list refusal, 0x01) is a real failure.
UNREACHABLE = re.compile(
    r"timed out|connection (refused|reset|aborted)|network is unreachable"
    r"|no route to host|temporary failure in name resolution",
    re.IGNORECASE,
)
# yt-dlp reports caption trouble (a refused download, a missing PO token) as a
# WARNING or ERROR about subtitles/captions and can still exit 0 with no file.
SUBS_TROUBLE = re.compile(r"^(WARNING|ERROR).*(subtitle|caption)", re.I | re.M)
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def video_id(arg: str) -> str | None:
    if VIDEO_ID.fullmatch(arg):
        return arg
    u = urlparse(arg if "//" in arg else "https://" + arg)
    host = (u.hostname or "").removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        cand = u.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        parts = [p for p in u.path.split("/") if p]
        if parts[:1] == ["watch"]:
            cand = dict(q.split("=", 1) for q in u.query.split("&") if "=" in q).get(
                "v", ""
            )
        elif parts[:1] in (["shorts"], ["embed"], ["live"], ["v"]) and len(parts) > 1:
            cand = parts[1]
        else:
            cand = ""
    else:
        cand = ""
    return cand if VIDEO_ID.fullmatch(cand) else None


def proxy_reachable(proxy: str, timeout: float = 10.0) -> bool:
    """A real SOCKS5 CONNECT to YouTube through the proxy. The relay listens
    even when the PC is off, so a bare TCP connect proves nothing; it answers a
    CONNECT it cannot carry out with a failure code."""
    u = urlparse(proxy)
    host = b"www.youtube.com"
    try:
        with socket.create_connection(
            (u.hostname, u.port or 1080), timeout=timeout
        ) as s:
            s.settimeout(timeout)
            s.sendall(b"\x05\x01\x00")
            if recv_exact(s, 2) != b"\x05\x00":
                return False
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + b"\x01\xbb")
            return recv_exact(s, 4)[1] == 0
    except OSError:
        return False


def recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("proxy closed the connection")
        buf += chunk
    return buf


def vtt_to_text(vtt: str) -> str:
    """Cue text only. Auto captions repeat each line across rolling cues and
    carry inline <00:00:01.000><c> word tags; both are dropped."""
    out: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or "-->" in line:
            continue
        if line == "WEBVTT" or line.startswith(
            ("Kind:", "Language:", "NOTE", "STYLE", "REGION")
        ):
            continue
        line = " ".join(html.unescape(re.sub(r"<[^>]+>", "", line)).split())
        if line and (not out or out[-1] != line):
            out.append(line)
    return "\n".join(out)


def yt_dlp_cmd() -> list[str]:
    explicit = os.environ.get("YT_DLP")
    if explicit:
        return [explicit]
    beside = Path(__file__).resolve().parent / "yt-dlp"
    if beside.is_file():
        return [sys.executable, str(beside)]
    found = shutil.which("yt-dlp")
    return [found] if found else []


def pick(files: list[Path]) -> Path | None:
    """Prefer the original-language track over a translated one."""
    files = sorted(files)
    return next(
        (f for f in files if f.name.endswith("-orig.vtt")), files[0] if files else None
    )


def fetch(vid: str, proxy: str, langs: str) -> tuple[str | None, str, str]:
    """(text, title, error). text None with error "" means no captions;
    error "unreachable" means the relay or PC did not answer; any other error
    is a failed fetch, which is not the same as no captions."""
    base = yt_dlp_cmd()
    if not base:
        return None, "", "yt-dlp is not installed"
    title = ""
    last_err = ""
    for kind in ("--write-subs", "--write-auto-subs"):  # human captions first
        with tempfile.TemporaryDirectory(prefix="yt-transcript-") as tmp:
            cmd = base + [
                "--proxy",
                proxy,
                "--skip-download",
                "--no-playlist",
                "--no-cache-dir",
                "--no-simulate",
                "--socket-timeout",
                "20",
                "--retries",
                "2",
                kind,
                "--sub-langs",
                langs,
                "--sub-format",
                "vtt",
                "--print-to-file",
                "%(title)s",
                str(Path(tmp, "title.txt")),
                "-o",
                str(Path(tmp, "%(id)s")),
                f"https://www.youtube.com/watch?v={vid}",
            ]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            except subprocess.TimeoutExpired:
                return None, "", "unreachable"
            err = (r.stderr or "").strip()
            chosen = pick(list(Path(tmp).glob("*.vtt")))
            if chosen:
                t = Path(tmp, "title.txt")
                title = t.read_text(errors="replace").strip() if t.exists() else ""
                text = vtt_to_text(chosen.read_text(errors="replace"))
                if text:
                    return text, title, ""
            if r.returncode != 0 or SUBS_TROUBLE.search(err):
                if UNREACHABLE.search(err):
                    return None, "", "unreachable"
                last_err = next(
                    (
                        ln
                        for ln in reversed(err.splitlines())
                        if ln.startswith("ERROR") or SUBS_TROUBLE.search(ln)
                    ),
                    err,
                )
    return None, "", last_err


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(
            __doc__.split("\n\n")[0] + "\nusage: transcript.py <url-or-video-id>",
            file=sys.stderr,
        )
        return 2
    vid = video_id(argv[1].strip())
    if not vid:
        print(f"not a YouTube video URL or id: {argv[1]}", file=sys.stderr)
        return 2
    proxy = os.environ.get("YT_TRANSCRIPT_PROXY", DEFAULT_PROXY)
    if not proxy_reachable(proxy):
        print(PC_OFF)
        return 0
    text, title, err = fetch(vid, proxy, os.environ.get("YT_TRANSCRIPT_LANGS", "en.*"))
    if text:
        print(f"Title: {title}\nVideo: https://www.youtube.com/watch?v={vid}\n\n{text}")
        return 0
    if err == "unreachable":
        print(PC_OFF)
        return 0
    if not err:
        print(NO_CAPTIONS)
        return 0
    print(f"transcript failed: {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
