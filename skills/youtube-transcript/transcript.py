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

The fetch is media.py's (beside this file, shared with the video-eval skill):
human captions first, else the auto-generated original-language track of the
language spoken, else English's, never YouTube's machine translations. The
transcript says which language it is.

Env: YT_TRANSCRIPT_PROXY  SOCKS proxy (default socks5h://host.docker.internal:18081)
     YT_TRANSCRIPT_LANGS  yt-dlp --sub-langs pattern for human captions (default en.*)
     YT_DLP               yt-dlp to run (default: yt-dlp beside this file, else PATH)
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlparse

import media

DEFAULT_PROXY = "socks5h://host.docker.internal:18081"
PC_OFF = "transcript unavailable - the PC is off"
NO_CAPTIONS = "transcript unavailable - this video has no captions"
BLOCKED = "transcript unavailable - youtube is blocking this route"

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
    if not media.proxy_reachable(proxy):
        print(PC_OFF)
        return 0
    text, title, lang, err = media.captions(
        vid, proxy, os.environ.get("YT_TRANSCRIPT_LANGS", "en.*")
    )
    if text:
        print(
            f"Title: {title}\nVideo: https://www.youtube.com/watch?v={vid}"
            f"\nLanguage: {lang}\n\n{text}"
        )
        return 0
    if err == "unreachable":
        print(PC_OFF)
        return 0
    if err == "blocked":
        print(BLOCKED)
        return 0
    if not err:
        print(NO_CAPTIONS)
        return 0
    print(f"transcript failed: {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
