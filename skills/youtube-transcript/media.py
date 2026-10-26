"""The media code the youtube-transcript and video-eval skills share: the SOCKS
check of the egress through the owner's PC, YouTube captions, .vtt to text,
the pinned yt-dlp and its downloads, and ffmpeg plus whisper for audio.

It lives in the youtube-transcript skill beside the pinned yt-dlp, so
scripts/ensure-youtube-transcript.sh installs both into the gateway's shared
skills directory: transcript.py imports it from beside itself, video.py from
there. Stdlib only. The proxy is always the caller's: the youtube-transcript
skill's tunnel (YT_TRANSCRIPT_PROXY), or the job queue's egress.

Env: YT_DLP                yt-dlp to run (default: yt-dlp beside this file,
                           else the shared skills directory's, else PATH)
     VIDEO_FFMPEG          ffmpeg to run (default ffmpeg)
     VIDEO_WHISPER         whisper to run (default whisper)
     VIDEO_WHISPER_MODEL   whisper model (default base, the one baked in the image)
     VIDEO_WHISPER_LANG    force a spoken language (default: whisper detects it)
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

# yt-dlp stderr that means the relay, the tunnel or the PC behind it did not
# answer: the relay is down, or it answered SOCKS 0x05 (upstream unreachable).
# Any other SOCKS error (0x02 allow-list refusal, 0x01) is a real failure.
UNREACHABLE = re.compile(
    r"timed out|connection (refused|reset|aborted)|network is unreachable"
    r"|no route to host|temporary failure in name resolution",
    re.IGNORECASE,
)
# YouTube refusing this route itself (rate limit, bot check), as opposed to a
# problem with one video: a normal answer, since a retry from here will not help.
BLOCKED_BY_YOUTUBE = re.compile(
    r"HTTP Error 429|Too Many Requests|confirm you.re not a bot", re.IGNORECASE
)
# yt-dlp reports caption trouble (a refused download, a missing PO token) as a
# WARNING or ERROR about subtitles/captions and can still exit 0 with no file.
SUBS_TROUBLE = re.compile(r"^(WARNING|ERROR).*(subtitle|caption)", re.I | re.M)
# A language code as yt-dlp prints it ("en", "en-US", "pt-BR"); anything else,
# "NA" included, means it does not know.
LANG_CODE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]+)*")
ANY_ORIGINAL = ".*-orig"
FALLBACK_LANG = "en"
# A download yt-dlp skipped for its length (--match-filter, --max-filesize).
TOO_LONG_WORDS = re.compile(
    r"does not pass filter|larger than max-filesize", re.IGNORECASE
)


# --- the egress -------------------------------------------------------------------


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


# --- yt-dlp -------------------------------------------------------------------------


def yt_dlp_cmd() -> list[str]:
    explicit = os.environ.get("YT_DLP")
    if explicit:
        return [explicit]
    # The pinned yt-dlp ensure-youtube-transcript.sh installs: one pin to bump.
    for candidate in (
        Path(__file__).resolve().parent / "yt-dlp",
        Path.home() / ".openclaw" / "skills" / "youtube-transcript" / "yt-dlp",
    ):
        if candidate.is_file():
            return [sys.executable, str(candidate)]
    found = shutil.which("yt-dlp")
    return [found] if found else []


def last_error(said: str) -> str:
    lines = [ln for ln in said.splitlines() if ln.strip()]
    return next((ln for ln in reversed(lines) if ln.startswith("ERROR")), "") or (
        lines[-1] if lines else "yt-dlp produced no file"
    )


def download(
    url: str, work: Path, max_seconds: int, proxy: str | None, media: bool = True
) -> tuple[Path | None, dict, str]:
    """(audio file, yt-dlp's info, error). With media=False only the info is
    fetched. No file and no error means the video was skipped for its length;
    the error is yt-dlp's own text otherwise. proxy None goes direct."""
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
        "--write-info-json",
    ]
    if media:
        cmd += ["--max-filesize", "100M", "--match-filter", f"duration<=?{max_seconds}"]
        cmd += ["--format", "ba/b"]
    else:
        cmd += ["--skip-download"]
    cmd += ["-o", str(work / "%(id)s.%(ext)s")]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += ["--", url]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=240, check=False
        )
    except subprocess.TimeoutExpired:
        return None, {}, "yt-dlp timed out"
    said = f"{r.stdout}\n{r.stderr}".strip()
    info: dict = {}
    for f in work.glob("*.info.json"):
        try:
            info = json.loads(f.read_text())
        except ValueError:
            info = {}
        f.unlink(missing_ok=True)
    files = [p for p in work.iterdir() if p.is_file() and p.suffix != ".wav"]
    if files:
        return files[0], info, ""
    if not media and info:
        return None, info, ""
    if TOO_LONG_WORDS.search(said):
        return None, info, ""
    return None, info, last_error(said)


# --- captions -----------------------------------------------------------------------


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


def spoken_language(printed: str) -> str:
    """The audio language from yt-dlp's %(language)s ("en-US"; "a+b" for a
    merged format), or "" when it does not know."""
    lang = printed.strip().split("+")[0]
    return lang if LANG_CODE.fullmatch(lang) else ""


def preferred(spoken: str) -> list[str]:
    """Languages to look for among the -orig tracks, best first: the spoken
    one, then its primary subtag ("en-US" -> "en"), then English."""
    langs = [spoken, spoken.split("-")[0]] if spoken else []
    return list(dict.fromkeys(langs + [FALLBACK_LANG]))


def original_tracks(spoken: str) -> str:
    """The auto pass's --sub-langs: only the -orig tracks pick() can choose,
    or every -orig track when the spoken language is unknown."""
    if not spoken:
        return ANY_ORIGINAL
    return ",".join(f"{lang}-orig" for lang in preferred(spoken))


def pick(files: list[Path], spoken: str = "") -> Path | None:
    """The track to read. Original-language auto tracks: the one for the
    spoken language, else English's, else a lone one; several with neither is
    None, never an arbitrary one. Otherwise (human captions) the first."""
    files = sorted(files)
    orig = {
        f.name.rsplit(".", 2)[-2].removesuffix("-orig"): f
        for f in files
        if f.name.endswith("-orig.vtt")
    }
    if not orig:
        return files[0] if files else None
    for lang in preferred(spoken):
        if lang in orig:
            return orig[lang]
    return next(iter(orig.values())) if len(orig) == 1 else None


def language(vid: str, f: Path) -> str:
    """The track's language from its file name (<id>.<lang>.vtt)."""
    lang = f.name.removeprefix(vid + ".").removesuffix(".vtt")
    return (
        f"{lang.removesuffix('-orig')} (auto-generated)"
        if lang.endswith("-orig")
        else lang
    )


def captions(vid: str, proxy: str, langs: str) -> tuple[str | None, str, str, str]:
    """(text, title, language, error). text None with error "" means no captions;
    error "unreachable" means the relay or PC did not answer, "blocked" that
    YouTube refused the route; any other error is a failed fetch, which is not
    the same as no captions.

    Auto-generated captions are fetched as original-language tracks only
    (yt-dlp's "<lang>-orig"). The other auto tracks are YouTube's machine
    translations; it answers every request for one with HTTP 429 on this route,
    and the pattern "en.*" matches them, so asking for them failed the whole
    run. A video with auto-dubbed audio has one "-orig" track per audio
    language, so the track is the one for the language spoken (the video's
    audio language, as yt-dlp reports it), else English's; when neither exists
    a lone "-orig" track is the original whatever its language (the auto pass
    is asked again for every "-orig" track)."""
    base = yt_dlp_cmd()
    if not base:
        return None, "", "", "yt-dlp is not installed"
    title = ""
    last_err = ""
    spoken = ""
    narrowed_empty = False
    # Human captions first; that run also reports the spoken language. Auto
    # captions are only ever asked for as original-language tracks, first the
    # spoken language's and English's, then (when those downloaded nothing)
    # every one; --ignore-errors keeps one refused track from discarding the
    # ones that did download.
    for kind, widen in (
        ("--write-subs", False),
        ("--write-auto-subs", False),
        ("--write-auto-subs", True),
    ):
        if widen and not narrowed_empty:
            continue
        if kind == "--write-subs":
            want = langs
        else:
            want = ANY_ORIGINAL if widen else original_tracks(spoken)
        with tempfile.TemporaryDirectory(prefix="yt-transcript-") as tmp:
            cmd = base + [
                "--proxy",
                proxy,
                "--skip-download",
                "--no-playlist",
                "--no-cache-dir",
                "--ignore-errors",
                "--no-simulate",
                "--socket-timeout",
                "20",
                "--retries",
                "2",
                kind,
                "--sub-langs",
                want,
                "--sub-format",
                "vtt",
                "--print-to-file",
                "%(title)s",
                str(Path(tmp, "title.txt")),
                "--print-to-file",
                "%(language)s",
                str(Path(tmp, "language.txt")),
                "-o",
                str(Path(tmp, "%(id)s")),
                f"https://www.youtube.com/watch?v={vid}",
            ]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            except subprocess.TimeoutExpired:
                return None, "", "", "unreachable"
            err = (r.stderr or "").strip()
            lf = Path(tmp, "language.txt")
            if lf.exists():
                spoken = spoken_language(lf.read_text(errors="replace")) or spoken
            found = list(Path(tmp).glob("*.vtt"))
            if kind == "--write-auto-subs" and not widen:
                narrowed_empty = bool(spoken) and not found
            chosen = pick(found, spoken)
            if found and not chosen:
                last_err = (
                    f"{len(found)} original-language caption tracks and none is "
                    f"the spoken language ({spoken or 'unknown'}) or English"
                )
                continue
            if chosen:
                t = Path(tmp, "title.txt")
                title = t.read_text(errors="replace").strip() if t.exists() else ""
                text = vtt_to_text(chosen.read_text(errors="replace"))
                if text:
                    return text, title, language(vid, chosen), ""
            if r.returncode != 0 or SUBS_TROUBLE.search(err):
                if UNREACHABLE.search(err):
                    return None, "", "", "unreachable"
                last_err = next(
                    (
                        ln
                        for ln in reversed(err.splitlines())
                        if ln.startswith("ERROR") or SUBS_TROUBLE.search(ln)
                    ),
                    err,
                )
    if BLOCKED_BY_YOUTUBE.search(last_err):
        return None, "", "", "blocked"
    return None, "", "", last_err


# --- audio --------------------------------------------------------------------------


def extract_audio(media: Path, wav: Path) -> bool:
    ffmpeg = os.environ.get("VIDEO_FFMPEG", "ffmpeg")
    try:
        subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
            + ["-i", str(media), "-vn", "-ar", "16000", "-ac", "1", "-f", "wav"]
            + [str(wav)],
            capture_output=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return wav.is_file() and wav.stat().st_size > 0


def transcribe(wav: Path, out: Path) -> tuple[str, str, str]:
    """(text, detected language, error). Whisper prints the language it detected
    on stdout; a forced VIDEO_WHISPER_LANG is reported as given."""
    cmd = [
        os.environ.get("VIDEO_WHISPER", "whisper"),
        "--model",
        os.environ.get("VIDEO_WHISPER_MODEL", "base"),
        "--fp16",
        "False",
        "--output_format",
        "txt",
        "--output_dir",
        str(out),
    ]
    forced = os.environ.get("VIDEO_WHISPER_LANG", "")
    if forced:
        cmd += ["--language", forced]
    cmd.append(str(wav))
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800, check=False
        )
    except subprocess.TimeoutExpired:
        return "", "", "whisper timed out"
    txt = out / f"{wav.stem}.txt"
    if r.returncode != 0 or not txt.is_file():
        return "", "", last_error(r.stderr or r.stdout or "whisper produced no text")
    seen = re.search(r"Detected language:\s*(\S+)", r.stdout)
    lang = forced or (seen.group(1) if seen else "unknown")
    return " ".join(txt.read_text(errors="replace").split()), lang, ""


def speech(media: Path, work: Path) -> tuple[str, str, str]:
    """(text, language, error) for a media file; empty text means no speech."""
    wav = work / "audio.wav"
    if not extract_audio(media, wav):
        return "", "", ""
    return transcribe(wav, work)
