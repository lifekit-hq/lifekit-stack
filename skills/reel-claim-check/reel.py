#!/usr/bin/env python3
"""Download an Instagram reel and transcribe it, so an agent can fact-check it.

    reel.py <instagram reel url>

yt-dlp fetches the audio (no login, no cookies), ffmpeg turns it into 16 kHz
mono wav and the image's local whisper transcribes it: no API key, no paid
service. Everything is kept on this host in a private directory
(REEL_CHECK_DIR, default ~/.life-state/reel-claim-check, mode 0700): the
download is deleted as soon as the audio is extracted, and only the transcript
stays, REEL_RETENTION_DAYS (default 14) days, pruned at the start of every run.

stdout is the reel's details and transcript, or one line starting
"reel unavailable - ". That line is a normal answer (a reel Instagram only
serves to a logged-in browser, a post without speech), not a failure: exit 0.
Exit 1 is an unexpected failure (reason on stderr), exit 2 a usage error.

Only the reel's shortcode is taken from the argument; the URL handed to yt-dlp
is rebuilt from it, so nothing else in the message reaches the command line.

Env: REEL_CHECK_DIR        private working and transcript directory
     REEL_RETENTION_DAYS   days a transcript is kept (default 14)
     REEL_MAX_SECONDS      longest reel taken, in seconds (default 900)
     REEL_WHISPER_MODEL    whisper model (default base, the one baked in the image)
     REEL_WHISPER_LANG     force a spoken language (default: whisper detects it)
     YT_DLP, REEL_FFMPEG, REEL_WHISPER   the tools to run (default: yt-dlp beside
                           this file, else the youtube-transcript skill's, else
                           PATH; ffmpeg and whisper from PATH)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

INSTAGRAM_HOSTS = {"instagram.com", "instagr.am"}
# Instagram's shortcode alphabet; "audio" is the /reels/audio/<id> page, not a reel.
SHORTCODE = re.compile(r"[A-Za-z0-9_-]{6,}")
NOT_A_CODE = {"audio"}
POST_KINDS = {"reel", "reels", "p", "tv"}

LOGIN = (
    "reel unavailable - instagram only serves this one to a logged-in browser "
    "(or is rate-limiting this server); no login is used here"
)
GONE = "reel unavailable - that post is gone or the link is wrong"
NO_VIDEO = "reel unavailable - that post has no video"
TOO_LONG = "reel unavailable - too long to transcribe here"
NO_SPEECH = "reel unavailable - no speech in this reel to transcribe"
NO_SHARE = "reel unavailable - could not resolve that instagram share link"

# yt-dlp's wording for "Instagram will not give this to an anonymous client".
# It cannot tell a private account from a rate limit, so one answer covers both.
LOGIN_WORDS = re.compile(
    r"empty media response|not granting access|login required|rate-limit"
    r"|private|restricted video|HTTP Error (401|403|429)|Too Many Requests",
    re.IGNORECASE,
)
GONE_WORDS = re.compile(r"HTTP Error 404|not found|does not exist|removed", re.I)
NO_VIDEO_WORDS = re.compile(r"no video in this post", re.IGNORECASE)
TOO_LONG_WORDS = re.compile(r"does not pass filter|larger than max-filesize", re.I)

DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_SECONDS = 900
CAPTION_LIMIT = 1500
WORK_PREFIX = "work-"


@dataclass(frozen=True)
class Reel:
    code: str
    share: bool = False

    @property
    def url(self) -> str:
        if self.share:
            return f"https://www.instagram.com/share/reel/{self.code}/"
        return f"https://www.instagram.com/reel/{self.code}/"


def instagram_host(host: str) -> bool:
    host = (host or "").lower().removeprefix("www.").removeprefix("m.")
    return host in INSTAGRAM_HOSTS


def parse_reel(arg: str) -> Reel | None:
    """The reel a link points at, or None when it is not an Instagram video
    post. Query strings, fragments and the account name are dropped. The link
    may be the first word of a longer message."""
    words = arg.strip().split()
    if not words:
        return None
    u = urlparse(words[0] if "//" in words[0] else "https://" + words[0])
    if u.scheme not in ("http", "https") or not instagram_host(u.hostname or ""):
        return None
    parts = [p for p in u.path.split("/") if p]
    share = parts[:1] == ["share"]
    if share:
        parts = parts[1:]
    elif len(parts) > 2 and parts[1] in POST_KINDS:
        parts = parts[1:]  # /<account>/reel/<code>
    # Share links read /share/reel/<token> or /share/<token>.
    if share and len(parts) == 1:
        parts = ["reel", parts[0]]
    if len(parts) < 2 or parts[0] not in POST_KINDS:
        return None
    code = parts[1]
    if code in NOT_A_CODE or not SHORTCODE.fullmatch(code):
        return None
    return Reel(code, share)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def resolve_share(reel: Reel, hops: int = 3) -> Reel | None:
    """A /share/reel/<token> link redirects to the reel's own URL, which yt-dlp
    understands and the share link it does not. Follows only redirects that stay
    on Instagram; a redirect to the login page carries the target in `next`."""
    opener = urllib.request.build_opener(_NoRedirect)
    url = reel.url
    for _ in range(hops):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            opener.open(req, timeout=20).close()
            return None
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location") if 300 <= e.code < 400 else None
        except OSError:
            return None
        if not location:
            return None
        target = urlparse(urllib.request.urljoin(url, location))
        if not instagram_host(target.hostname or ""):
            return None
        if target.path.startswith("/accounts/login"):
            nxt = parse_qs(target.query).get("next", [""])[0]
            target = urlparse("https://www.instagram.com" + nxt)
        found = parse_reel(target.geturl())
        if found and not found.share:
            return found
        url = target.geturl()
    return None


def private_root() -> Path:
    root = Path(
        os.environ.get("REEL_CHECK_DIR")
        or Path.home() / ".life-state" / "reel-claim-check"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def prune(root: Path, days: int, now: float | None = None) -> None:
    """Retention: transcripts older than `days`, and the work directory of a run
    that died before it could clean up (a day is longer than any run)."""
    now = time.time() if now is None else now
    for entry in root.iterdir():
        age = now - entry.stat().st_mtime
        if entry.is_dir() and entry.name.startswith(WORK_PREFIX):
            if age > 86400:
                shutil.rmtree(entry, ignore_errors=True)
        elif entry.is_file() and entry.suffix == ".txt" and age > days * 86400:
            entry.unlink(missing_ok=True)


def yt_dlp_cmd() -> list[str]:
    explicit = os.environ.get("YT_DLP")
    if explicit:
        return [explicit]
    here = Path(__file__).resolve().parent
    # The pinned yt-dlp the youtube-transcript skill ships: one pin to bump.
    for candidate in (
        here / "yt-dlp",
        Path.home() / ".openclaw" / "skills" / "youtube-transcript" / "yt-dlp",
    ):
        if candidate.is_file():
            return [sys.executable, str(candidate)]
    found = shutil.which("yt-dlp")
    return [found] if found else []


def download(reel: Reel, work: Path, max_seconds: int) -> tuple[Path | None, dict, str]:
    """(audio file, yt-dlp's info, error). No file and no error means the reel
    was skipped for its length; the error is yt-dlp's own text otherwise."""
    base = yt_dlp_cmd()
    if not base:
        return None, {}, "yt-dlp is not installed"
    cmd = base + [
        "--ignore-config",
        "--no-playlist",
        "--no-cache-dir",
        "--no-part",
        "--socket-timeout",
        "20",
        "--retries",
        "2",
        "--max-filesize",
        "100M",
        "--match-filter",
        f"duration<=?{max_seconds}",
        "--format",
        "ba/b",
        "--write-info-json",
        "-o",
        str(work / "%(id)s.%(ext)s"),
        "--",
        reel.url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return None, {}, "yt-dlp timed out"
    said = f"{r.stdout}\n{r.stderr}".strip()
    info: dict = {}
    for f in work.glob("*.info.json"):
        try:
            info = json.loads(f.read_text())
        except ValueError:
            info = {}
    media = [p for p in work.iterdir() if not p.name.endswith(".json") and p.is_file()]
    if media:
        return media[0], info, ""
    if TOO_LONG_WORDS.search(said):
        return None, info, ""
    return None, info, last_error(said)


def last_error(said: str) -> str:
    lines = [ln for ln in said.splitlines() if ln.strip()]
    return next((ln for ln in reversed(lines) if ln.startswith("ERROR")), "") or (
        lines[-1] if lines else "yt-dlp produced no file"
    )


def unavailable_line(error: str) -> str | None:
    """The normal-answer line for a yt-dlp error, or None for a real failure."""
    if NO_VIDEO_WORDS.search(error):
        return NO_VIDEO
    if LOGIN_WORDS.search(error):
        return LOGIN
    if GONE_WORDS.search(error):
        return GONE
    return None


def extract_audio(media: Path, wav: Path) -> bool:
    ffmpeg = os.environ.get("REEL_FFMPEG", "ffmpeg")
    try:
        subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
            + ["-i", str(media), "-vn", "-ar", "16000", "-ac", "1", "-f", "wav"]
            + [str(wav)],
            capture_output=True,
            timeout=240,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return wav.is_file() and wav.stat().st_size > 0


def transcribe(wav: Path, out: Path) -> tuple[str, str, str]:
    """(text, detected language, error). Whisper prints the language it detected
    on stdout; a forced REEL_WHISPER_LANG is reported as given."""
    cmd = [
        os.environ.get("REEL_WHISPER", "whisper"),
        "--model",
        os.environ.get("REEL_WHISPER_MODEL", "base"),
        "--fp16",
        "False",
        "--output_format",
        "txt",
        "--output_dir",
        str(out),
    ]
    forced = os.environ.get("REEL_WHISPER_LANG", "")
    if forced:
        cmd += ["--language", forced]
    cmd.append(str(wav))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return "", "", "whisper timed out"
    txt = out / f"{wav.stem}.txt"
    if r.returncode != 0 or not txt.is_file():
        return "", "", last_error(r.stderr or r.stdout or "whisper produced no text")
    seen = re.search(r"Detected language:\s*(\S+)", r.stdout)
    language = forced or (seen.group(1) if seen else "unknown")
    return " ".join(txt.read_text(errors="replace").split()), language, ""


def render(reel: Reel, info: dict, language: str, text: str) -> str:
    caption = " ".join((info.get("description") or "").split())
    if len(caption) > CAPTION_LIMIT:
        caption = caption[:CAPTION_LIMIT] + "..."
    duration = info.get("duration")
    lines = [
        f"Reel: {reel.url}",
        f"Account: {info.get('uploader') or info.get('channel') or 'unknown'}",
        f"Duration: {round(duration)}s" if duration else "Duration: unknown",
        f"Language: {language}",
        f"Caption: {caption or '(none)'}",
        "Transcript: automatic speech-to-text, names and numbers can be wrong",
        "",
        text,
    ]
    return "\n".join(lines)


def run(reel: Reel) -> tuple[int, str]:
    """(exit code, stdout). Normal answers are exit 0."""
    root = private_root()
    prune(root, int(os.environ.get("REEL_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))
    if reel.share:
        resolved = resolve_share(reel)
        if not resolved:
            return 0, NO_SHARE
        reel = resolved
    max_seconds = int(os.environ.get("REEL_MAX_SECONDS", DEFAULT_MAX_SECONDS))
    work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=root))
    try:
        media, info, error = download(reel, work, max_seconds)
        if not media:
            if not error:
                return 0, TOO_LONG
            line = unavailable_line(error)
            if line:
                return 0, line
            print(f"reel failed: {error}", file=sys.stderr)
            return 1, ""
        wav = work / "audio.wav"
        ok = extract_audio(media, wav)
        media.unlink(missing_ok=True)  # the download is not kept past this point
        if not ok:
            return 0, NO_SPEECH
        text, language, error = transcribe(wav, work)
        if error:
            print(f"reel failed: {error}", file=sys.stderr)
            return 1, ""
        if not text:
            return 0, NO_SPEECH
        out = render(reel, info, language, text)
        saved = root / f"{reel.code}.txt"
        saved.write_text(out + "\n")
        saved.chmod(0o600)
        return 0, out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(
            __doc__.split("\n\n")[0] + "\nusage: reel.py <instagram reel url>",
            file=sys.stderr,
        )
        return 2
    reel = parse_reel(argv[1])
    if not reel:
        print(f"not an Instagram reel link: {argv[1]}", file=sys.stderr)
        return 2
    os.umask(0o077)
    try:
        code, out = run(reel)
    except FileNotFoundError as e:  # ffmpeg or whisper missing from this image
        print(f"reel failed: {e.filename} is not installed", file=sys.stderr)
        return 1
    if out:
        print(out)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
