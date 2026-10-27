#!/usr/bin/env python3
"""Evaluate a video the owner sends: queue it, transcribe it, have it judged
against the intake rubric (rubric.md beside this file), keep the report and
one index row.

    video.py <link> [--check]                 queue a YouTube video or Instagram reel (kit)
    video.py <media file> [--check]           queue a video file the owner sent (kit)
    video.py enqueue <link> --origin O [--check]   the same from another producer
    video.py ingest <name> --origin O [--check]    queue a file read from stdin (backfill)
    video.py run <job id>                     the queued run, started by the queue's runner
    video.py part <job id> <n>                part n of a long transcript
    video.py report <job id> [file]           file the judge's report (else stdin)
    video.py status <job id>                  one job's state, for a producer that polls
    video.py show <job id>                    the job's report
    video.py index                            the index of every item judged
    video.py idea [file]                      file a judged idea's report (scout)
    video.py link [file] --origin O [--check] file a judged link's or article's report
    video.py note [file] --origin O [--check] file a judged text note's report
    video.py voice <name> --origin O [--check] queue a voice note read from stdin
    video.py door <message text>              Kit's drop door: queue a bare link (kit)
    video.py door-file <saved video>          Kit's drop door: queue a video file (kit)

The door subcommands are run by the video-drop-door gateway plugin before
Kit's model sees a Telegram message (docs/runbook.md, "Video drop door"):
exit 0 prints the one-line receipt (or the earlier verdict), exit 2 means the
message is not for the door and goes to the model unchanged.

Origin is who sent the item: kit (the owner on Telegram), firstmate (the owner
in chat, filed on the host) or scout (the ideas scout). It only routes the
reply: a kit item's answer is kit's message to the owner, any other origin's is
NO_REPLY and its producer reads `status`. The judge never sees it: `run` prints
the material, the rubric and the mode, nothing about who sent it.

YouTube and Instagram refuse this server, so a link is a job on the shared
job queue (skills/job-queue/jobq.py) and every fetch goes out through its
egress (LIFEKIT_EGRESS_PROXY; today the owner's PC). A file already on this
host (a video the owner sent, a backfilled reel or .vtt) is a job that needs
no egress. A YouTube video is read from its captions when it has them, else
its audio is downloaded and transcribed like a reel's: yt-dlp, ffmpeg and the
image's local whisper, no API key, no paid service, no login, no cookies.

The fetching, captions and transcription are the youtube-transcript skill's
media.py, shared by both skills; this file is what video-eval adds to them.

Before a fresh judgment the index is searched for the same item (its
normalized URL, or once fetched its title): a repeat answers with the earlier
verdict, date and reason, and is not judged again.

`run` is the queue's handler: the queue runs it on its own, never an agent
turn, since a fetch and a transcription can outlast any turn's tool timeout.
It prints the material and leaves the job running; the queue hands that to a
drain turn, whose judge writes the report the rubric defines and files it with
`report`, which checks it against the rubric (exit 3 and the reasons: fix it
and file it again), cuts a report over the rubric's length cap with a marker,
stamps the rubric version, saves it, appends the index row and finishes the
job. A report never filed is run again when the queue's lease runs out.

Everything is kept on this host in a private directory (INTAKE_DIR, default
~/.life-state/intake, mode 0700): reports/ and index.md for good (index.md is
appended to by this script only, never rewritten), transcripts/ and inbox/
(files waiting for their run) VIDEO_RETENTION_DAYS (default 14) days, pruned
at the start of every run. goals.md there, written by the operator, is the
owner's standing goals the judge reads; without it the judge says so.

`idea` files an item that is not a video: an idea the ideas scout proposes,
already judged against the same rubric by its producer. Its report carries the
idea's claim and the link of its primary source, the source it is recorded and
matched under; it is checked, stamped and indexed like a video's, with no job
and no fetch, and its three lines are printed at once.

`link` files a link or an article that is not a video the same way, from any
origin: its producer has read it and judged it against the same rubric. Its
report carries the item's own link, the source it is recorded and matched
under (a YouTube or Instagram video is refused: queue it instead). It is
judged in the standard mode, or claim-check with --check, and its three lines
are printed at once whatever the origin, since there is no job to poll.

A note is the owner's own text or voice note, judged as kind note. `note`
files a text note its producer has judged, the same way as a link: its report
carries the note's text, and it is recorded and matched under that text's
digest (note:<digest>). `voice` queues a voice note like a video file: the
same job, the same local whisper, then a drain turn judges the transcript and
files it with `report`; it is recorded under the audio's digest. Either is
judged in the standard mode, or claim-check with --check, and its report is
kept in reports/notes/.

Exit 1 is an unexpected failure (reason on stderr), 2 a usage error, 3 a
report that does not follow the rubric.

Env: INTAKE_DIR            private directory
     VIDEO_RETENTION_DAYS  days a transcript or a waiting file is kept (default 14)
     VIDEO_MAX_SECONDS     longest audio transcribed here, in seconds (default 900)
     VIDEO_WHISPER_MODEL   whisper model (default base, the one baked in the image)
     VIDEO_WHISPER_LANG    force a spoken language (default: whisper detects it)
     YT_TRANSCRIPT_LANGS   human caption languages (default en.*)
     YT_DLP, VIDEO_FFMPEG, VIDEO_WHISPER   the tools to run (default: the
                           youtube-transcript skill's yt-dlp, else PATH;
                           ffmpeg and whisper from PATH)
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import http.client
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

INSTAGRAM_HOSTS = {"instagram.com", "instagr.am"}
YOUTUBE_HOSTS = {"youtube.com", "music.youtube.com", "youtube-nocookie.com"}
# Instagram's shortcode alphabet; "audio" is the /reels/audio/<id> page, not a reel.
SHORTCODE = re.compile(r"[A-Za-z0-9_-]{6,}")
NOT_A_CODE = {"audio"}
POST_KINDS = {"reel", "reels", "p", "tv"}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

LOGIN = (
    "unavailable - instagram only serves this one to a logged-in browser "
    "(or is rate-limiting this server); no login is used here"
)
BLOCKED = "unavailable - youtube is blocking this route"
PRIVATE = "unavailable - the video is private, age-restricted or members-only"
GONE = "unavailable - that video is gone or the link is wrong"
NO_VIDEO = "unavailable - that post has no video"
TOO_LONG = "unavailable - too long to transcribe here, and it has no captions"
NO_SPEECH = "unavailable - no speech in it to transcribe"
NO_SHARE = "unavailable - could not resolve that instagram share link"

# yt-dlp's wording for "Instagram will not give this to an anonymous client".
# It cannot tell a private account from a rate limit, so one answer covers both.
LOGIN_WORDS = re.compile(
    r"empty media response|not granting access|login required|rate-limit"
    r"|private|restricted video|HTTP Error (401|403|429)|Too Many Requests",
    re.IGNORECASE,
)
YT_BLOCKED_WORDS = re.compile(
    r"HTTP Error 429|Too Many Requests|confirm you.re not a bot", re.IGNORECASE
)
YT_PRIVATE_WORDS = re.compile(
    r"private video|members-only|join this channel|age-restricted|confirm your age"
    r"|inappropriate for some users",
    re.IGNORECASE,
)
GONE_WORDS = re.compile(
    r"HTTP Error 404|not found|does not exist|removed|video unavailable"
    r"|incomplete youtube id",
    re.IGNORECASE,
)
NO_VIDEO_WORDS = re.compile(r"no video in this post", re.IGNORECASE)

# The queue (skills/job-queue): the job kind, and the hosts the queue asks the
# egress about before it starts a job ("" for a file here).
KIND = "video"
IDEA = "idea"
IDEA_FIELDS = ("claim", "source")
IDEA_MODE = "claim-check"
IDEA_ORIGIN = "scout"
LINK = "link"
LINK_FIELDS = ("source",)
NOTE = "note"
NOTE_FIELDS = ("note",)
NOTE_LIMIT = 4096
OWNER = "video-eval"
PROBE = {"instagram": "www.instagram.com", "youtube": "www.youtube.com", "file": ""}
KEY_PREFIX = {"instagram": "ig", "youtube": "yt", "file": "file"}
ORIGINS = ("kit", "firstmate", "scout")
REPLY_ORIGINS = {"kit"}
MODES = ("standard", "claim-check")
FAILED = "evaluation failed"
NO_REPLY = "NO_REPLY"
USAGE = (
    "usage: video.py <link or media file> [--check]\n"
    "       video.py enqueue <link> --origin kit|firstmate|scout [--check]\n"
    "       video.py ingest <name> --origin kit|firstmate|scout [--check] < file\n"
    "       video.py run|status|show <job id> | part <job id> <n>\n"
    "       video.py report <job id> [file] | index\n"
    "       video.py idea [file] < report\n"
    "       video.py link [file] --origin kit|firstmate|scout [--check] < report\n"
    "       video.py note [file] --origin kit|firstmate|scout [--check] < report\n"
    "       video.py voice <name> --origin kit|firstmate|scout [--check] < audio\n"
    "       video.py door <message text> | door-file <saved video>"
)

DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_SECONDS = 900
MAX_INGEST_BYTES = 200 * 1024 * 1024
CAPTION_LIMIT = 1500
GOALS_LIMIT = 2000
PART_CHARS = 20000
CELL_LIMIT = 160
HEAD_LIMIT = 200
WORK_PREFIX = "work-"
CUT = "[cut to fit the rubric's one-screen cap]"
MEDIA_SUFFIXES = {".mp4", ".m4a", ".mp3", ".mov", ".webm", ".mkv", ".ogg", ".wav"}
AUDIO_SUFFIXES = {".ogg", ".oga", ".opus", ".m4a", ".mp3", ".wav"}

INDEX_COLUMNS = (
    "date",
    "kind",
    "origin",
    "category",
    "source",
    "title",
    "gist",
    "verdict",
    "reason",
    "project",
    "recheck",
    "next step or why",
    "rubric",
    "report",
)
INDEX_HEAD = (
    "# Intake index\n\n"
    "Every item judged, one row each, appended by skills/video-eval/video.py and\n"
    'never edited by hand (docs/runbook.md, "Video evaluation"). Verdicts and\n'
    "codes are the rubric's (skills/video-eval/rubric.md, its version in `rubric`).\n\n"
    "| " + " | ".join(INDEX_COLUMNS) + " |\n"
    "|" + " --- |" * len(INDEX_COLUMNS) + "\n"
)


@dataclass(frozen=True)
class Item:
    """What a job evaluates. ident is the reel's shortcode, the video's id, or
    a file's digest; share marks an Instagram share link not yet resolved."""

    source: str
    ident: str
    share: bool = False
    name: str = ""

    @property
    def key(self) -> str:
        return f"{KEY_PREFIX[self.source]}-{self.ident}"

    @property
    def url(self) -> str:
        if self.source == "instagram":
            kind = "share/reel" if self.share else "reel"
            return f"https://www.instagram.com/{kind}/{self.ident}/"
        if self.source == "youtube":
            return f"https://www.youtube.com/watch?v={self.ident}"
        return f"file:{self.ident}/{self.name}"


def _host(u) -> str:
    return (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")


def _first_url(arg: str):
    words = arg.strip().split()
    if not words:
        return None
    u = urlparse(words[0] if "//" in words[0] else "https://" + words[0])
    return u if u.scheme in ("http", "https") else None


def parse_reel(arg: str) -> Item | None:
    """The reel a link points at, or None when it is not an Instagram video
    post. Query strings, fragments and the account name are dropped. The link
    may be the first word of a longer message."""
    u = _first_url(arg)
    if u is None or _host(u) not in INSTAGRAM_HOSTS:
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
    if code in NOT_A_CODE or not SHORTCODE.fullmatch(code) or len(code) > 60:
        return None
    return Item("instagram", code, share)


def parse_youtube(arg: str) -> Item | None:
    """The video a YouTube link points at (watch, youtu.be, shorts, live,
    embed), or None. Only the id is kept."""
    u = _first_url(arg)
    if u is None:
        return None
    host, parts = _host(u), [p for p in u.path.split("/") if p]
    cand = ""
    if host == "youtu.be" and parts:
        cand = parts[0]
    elif host in YOUTUBE_HOSTS:
        if parts[:1] == ["watch"]:
            cand = parse_qs(u.query).get("v", [""])[0]
        elif parts[:1] in (["shorts"], ["embed"], ["live"], ["v"]) and len(parts) > 1:
            cand = parts[1]
    return Item("youtube", cand) if VIDEO_ID.fullmatch(cand) else None


def parse_link(arg: str) -> Item | None:
    return parse_reel(arg) or parse_youtube(arg)


TRACKING = re.compile(r"utm_.*|fbclid|gclid|igsh|igshid|si|feature|ref|ref_src")


def normalize_url(url: str) -> str:
    """The form an item is recorded and matched under: a reel or a YouTube
    video by its id alone, any other link without scheme case, www., tracking
    parameters, fragment or trailing slash."""
    item = parse_link(url)
    if item is not None:
        return item.url
    if url.startswith(("file:", "note:")):
        return url
    u = urlparse(url.strip() if "//" in url else "https://" + url.strip())
    query = urlencode(
        [(k, v) for k, v in parse_qsl(u.query) if not TRACKING.fullmatch(k.lower())]
    )
    path = u.path.rstrip("/")
    return f"https://{_host(u)}{path}" + (f"?{query}" if query else "")


def normalize_title(title: str) -> str:
    """A title as matched across sources: case, punctuation and spacing
    dropped. Too short or generic a title ("Video by <account>") matches
    nothing, so two different reels are never taken for one."""
    t = " ".join(re.sub(r"[\W_]+", " ", title.casefold()).split())
    if len(t) < 12 or t.startswith("video by ") or t in ("unknown", "untitled"):
        return ""
    return t


# --- the private directory ------------------------------------------------------


def private_root() -> Path:
    root = Path(os.environ.get("INTAKE_DIR") or Path.home() / ".life-state" / "intake")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    for sub in ("reports", "transcripts", "inbox"):
        (root / sub).mkdir(mode=0o700, exist_ok=True)
    return root


def prune(root: Path, days: int, now: float | None = None) -> None:
    """Retention: transcripts and waiting files older than `days`, and the
    work directory of a run that died before it could clean up (a day is
    longer than any run). Reports and the index are kept."""
    now = time.time() if now is None else now
    for entry in root.iterdir():
        stale = now - entry.stat().st_mtime > 86400
        if entry.is_dir() and entry.name.startswith(WORK_PREFIX) and stale:
            shutil.rmtree(entry, ignore_errors=True)
    for sub in ("transcripts", "inbox"):
        for f in (root / sub).iterdir():
            if f.is_file() and now - f.stat().st_mtime > days * 86400:
                f.unlink(missing_ok=True)


def _private_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# --- the rubric -------------------------------------------------------------------


@dataclass(frozen=True)
class Rubric:
    path: Path
    version: int
    lists: dict

    def list(self, name: str) -> list[str]:
        return self.lists[name]

    @property
    def stamp(self) -> str:
        return f"v{self.version}"


def rubric_path() -> Path:
    return Path(
        os.environ.get("VIDEO_RUBRIC") or Path(__file__).resolve().parent / "rubric.md"
    )


def load_rubric(path: Path | None = None) -> Rubric:
    """The machine-read block at the top of rubric.md: its version, closed
    lists and length cap."""
    path = path or rubric_path()
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        raise ValueError(f"{path}: no front matter")
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    lists = {
        k: [x.strip() for x in v.split(",") if x.strip()]
        for k, v in meta.items()
        if k not in ("rubric", "version", "max-lines", "max-chars")
    }
    lists["max-lines"] = int(meta["max-lines"])
    lists["max-chars"] = int(meta["max-chars"])
    return Rubric(path, int(meta["version"]), lists)


# --- fetching -------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _egress_https():
    """A urllib handler whose HTTPS connections go out through the job queue's
    egress, like yt-dlp's download: Instagram refuses this server."""
    q = jobq()
    proxy = q.egress_proxy()

    class Connection(http.client.HTTPSConnection):
        def connect(self):
            sock = q.open_connection(self.host, self.port, proxy, self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class Handler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(Connection, req)

    return Handler


def resolve_share(reel: Item, hops: int = 3) -> Item | None:
    """A /share/reel/<token> link redirects to the reel's own URL, which yt-dlp
    understands and the share link it does not. Follows only redirects that stay
    on Instagram; a redirect to the login page carries the target in `next`."""
    opener = urllib.request.build_opener(_NoRedirect, _egress_https())
    url = reel.url
    for _ in range(hops):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            opener.open(req, timeout=20).close()
            return None
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location") if 300 <= e.code < 400 else None
        # Any other OSError (the egress went away, a timeout) is the run's
        # failure, for the queue to retry, not an answer about the link.
        if not location:
            return None
        target = urlparse(urllib.request.urljoin(url, location))
        if _host(target) not in INSTAGRAM_HOSTS:
            return None
        if target.path.startswith("/accounts/login"):
            nxt = parse_qs(target.query).get("next", [""])[0]
            target = urlparse("https://www.instagram.com" + nxt)
        found = parse_reel(target.geturl())
        if found and not found.share:
            return found
        url = target.geturl()
    return None


def unavailable_line(error: str, source: str) -> str | None:
    """The normal-answer line for a yt-dlp error, or None for a real failure."""
    if source == "youtube":
        if YT_BLOCKED_WORDS.search(error):
            return BLOCKED
        if YT_PRIVATE_WORDS.search(error):
            return PRIVATE
        return GONE if GONE_WORDS.search(error) else None
    if NO_VIDEO_WORDS.search(error):
        return NO_VIDEO
    if LOGIN_WORDS.search(error):
        return LOGIN
    if GONE_WORDS.search(error):
        return GONE
    return None


def media():
    """The media code shared with the youtube-transcript skill (its media.py:
    the egress fetch, captions, .vtt to text, the pinned yt-dlp and whisper):
    beside this skill, else the gateway's shared skills directory."""
    for d in (
        Path(__file__).resolve().parent.parent / "youtube-transcript",
        Path.home() / ".openclaw" / "skills" / "youtube-transcript",
    ):
        if (d / "media.py").is_file():
            sys.path.insert(0, str(d))
            import media as module

            return module
    raise FileNotFoundError(None, None, "the youtube-transcript skill (media.py)")


@dataclass
class Material:
    """What the judge reads: the item's details and its transcript."""

    url: str
    title: str = ""
    author: str = ""
    duration: float | None = None
    language: str = ""
    captions: bool = False
    caption: str = ""
    text: str = ""

    def header(self) -> list[str]:
        caption = " ".join(self.caption.split())
        if len(caption) > CAPTION_LIMIT:
            caption = caption[:CAPTION_LIMIT] + "..."
        how = (
            "captions"
            if self.captions
            else "automatic speech-to-text, names and numbers can be wrong"
        )
        return [
            f"Video: {self.url}",
            f"Title: {' '.join(self.title.split()) or 'unknown'}",
            f"Author: {' '.join(self.author.split()) or 'unknown'}",
            f"Duration: {round(self.duration)}s"
            if self.duration
            else "Duration: unknown",
            f"Language: {self.language or 'unknown'}",
            f"Transcript: {how}",
            f"Caption: {caption or '(none)'}",
        ]

    def render(self) -> str:
        return "\n".join(self.header()) + "\n\n" + self.text + "\n"

    @classmethod
    def parse(cls, saved: str) -> Material:
        head, _, text = saved.partition("\n\n")
        f = dict(ln.split(": ", 1) for ln in head.splitlines() if ": " in ln)
        dur = f.get("Duration", "").removesuffix("s")
        return cls(
            url=f.get("Video", ""),
            title="" if f.get("Title") == "unknown" else f.get("Title", ""),
            author="" if f.get("Author") == "unknown" else f.get("Author", ""),
            duration=float(dur) if dur.isdigit() else None,
            language=f.get("Language", ""),
            captions=f.get("Transcript") == "captions",
            caption="" if f.get("Caption") == "(none)" else f.get("Caption", ""),
            text=text.strip(),
        )


def _from_info(m: Material, info: dict) -> None:
    m.title = m.title or info.get("title") or ""
    m.author = info.get("uploader") or info.get("channel") or m.author
    m.duration = info.get("duration") or m.duration
    m.caption = info.get("description") or m.caption


def gather(
    item: Item, work: Path, path: Path | None
) -> tuple[Material | None, str, str]:
    """(material, unavailable line, error) for one item: exactly one is set.
    An unavailable line is a normal answer; an error is a real failure."""
    m = Material(item.url)
    max_seconds = int(os.environ.get("VIDEO_MAX_SECONDS", DEFAULT_MAX_SECONDS))
    if path is not None:  # a file already on this host: no download
        if path.suffix == ".vtt":
            m.text = " ".join(
                media().vtt_to_text(path.read_text(errors="replace")).split()
            )
            parts = path.name.split(".")
            m.language = parts[-2] if len(parts) > 2 else "unknown"
            m.captions = True
        else:
            m.text, m.language, error = media().speech(path, work)
            if error:
                return None, "", error
        if not m.text.strip():
            return None, NO_SPEECH, ""
        return m, "", ""
    # The egress is the job queue's one setting (LIFEKIT_EGRESS_PROXY).
    proxy = jobq().egress_proxy()
    if item.source == "youtube":
        _, info, error = media().download(
            item.url, work, max_seconds, proxy, media=False
        )
        if error:
            line = unavailable_line(error, "youtube")
            return (None, line, "") if line else (None, "", error)
        _from_info(m, info)
        text, title, lang, err = media().captions(
            item.ident, proxy or "", os.environ.get("YT_TRANSCRIPT_LANGS", "en.*")
        )
        if err == "unreachable":
            return None, "", "the egress did not answer while fetching captions"
        if err == "blocked":
            return None, BLOCKED, ""
        if text:
            m.title = m.title or title
            m.text, m.language, m.captions = " ".join(text.split()), lang, True
            return m, "", ""
        # No captions (or none this route could read): the audio, like a reel.
        if (m.duration or 0) > max_seconds:
            return None, TOO_LONG, ""
    media_file, info, error = media().download(item.url, work, max_seconds, proxy)
    if not media_file:
        if not error:
            return None, TOO_LONG, ""
        line = unavailable_line(error, item.source)
        return (None, line, "") if line else (None, "", error)
    _from_info(m, info)
    text, m.language, error = media().speech(media_file, work)
    media_file.unlink(missing_ok=True)  # the download is not kept past this point
    if error:
        return None, "", error
    if not text:
        return None, NO_SPEECH, ""
    m.text = text
    return m, "", ""


# --- the queue ------------------------------------------------------------------------


def jobq():
    """The shared job queue's module (skills/job-queue/jobq.py): JOBQ_LIB, else
    beside this skill, else the gateway's shared skills directory."""
    for d in (
        os.environ.get("JOBQ_LIB", ""),
        Path(__file__).resolve().parent.parent / "job-queue",
        Path.home() / ".openclaw" / "skills" / "job-queue",
    ):
        if d and (Path(d) / "jobq.py").is_file():
            sys.path.insert(0, str(d))
            import jobq as module

            return module
    raise FileNotFoundError(None, None, "the job-queue skill (jobq.py)")


def _item(p: dict) -> Item:
    return Item(p["source"], p["ident"], bool(p.get("share")), p.get("name", ""))


@dataclass(frozen=True)
class Queued:
    """What filing an item did: repeat holds the earlier verdict's lines when
    the index already had it (nothing queued), else jid names the job."""

    item: Item
    jid: str = ""
    added: bool = False
    waiting: int = 0
    repeat: tuple[str, ...] = ()


def _queue(
    item: Item, origin: str, mode: str, path: Path | None = None, kind: str = KIND
) -> str:
    q = _file_job(item, origin, mode, path, kind)
    if q.repeat:
        return "\n".join(q.repeat)
    when = (
        "judged on the next drain turn"
        if path is not None
        else "judged once the download route is up"
    )
    head = f"{kind} queued" if q.added else f"{kind} already queued"
    return f"{head} - job {q.jid}, {when} ({q.waiting} waiting)"


def _file_job(
    item: Item, origin: str, mode: str, path: Path | None = None, kind: str = KIND
) -> Queued:
    """Queue an item, unless the index already holds its verdict: then that
    verdict is the answer, and nothing is queued. kind is what it is judged
    as; the queue's job kind is always video."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin {origin!r}: one of {', '.join(ORIGINS)}")
    root = private_root()
    row = earlier(root, item.url, "")
    if row is not None:
        if path is not None:
            path.unlink(missing_ok=True)
        return Queued(item, repeat=tuple(repeat_reply(row)))
    payload = {
        "source": item.source,
        "ident": item.ident,
        "share": item.share,
        "name": item.name,
        "url": item.url,
        "origin": origin,
        "mode": mode,
    }
    if kind != KIND:
        payload["kind"] = kind
    if path is not None:
        payload["path"] = str(path)
    jid = f"{KIND}-{item.key}"
    added, waiting = jobq().add(
        KIND,
        item.key,
        owner=OWNER,
        handler=[sys.executable, str(Path(__file__).resolve()), "run"],
        probe=PROBE["file" if path is not None else item.source],
        payload=payload,
        label=item.url,
        quiet=origin not in REPLY_ORIGINS,
    )
    return Queued(item, jid, added, waiting)


def default_mode(item: Item, check: bool) -> str:
    return "claim-check" if check or item.source != "youtube" else "standard"


def enqueue(arg: str, origin: str, check: bool = False) -> str | None:
    item = parse_link(arg)
    if item is None:
        return None
    return _queue(item, origin, default_mode(item, check))


def ingest(name: str, data: bytes, origin: str, check: bool = False) -> str:
    """Queue a file already on this host: a video the owner sent, or a backfill
    of one already downloaded."""
    item, kept = _kept(name, data)
    return _queue(item, origin, default_mode(item, check), kept)


def voice(name: str, data: bytes, origin: str, check: bool = False) -> str:
    """Queue a voice note: transcribed like a video file, judged as a note."""
    rubric = load_rubric()
    if NOTE not in rubric.list("kinds"):
        raise ValueError(f"{rubric.path} has no {NOTE} kind")
    mode = _producer_mode(origin, check)
    item, kept = _kept(name, data, NOTE)
    return _queue(item, origin, mode, kept, NOTE)


def _kept(name: str, data: bytes, kind: str = KIND) -> tuple[Item, Path]:
    """Keep a file for its run, in the private inbox. reel-<shortcode>.<ext>
    is that reel and yt-<id>.<lang>.vtt that video's captions, so they are
    recorded and matched under their link; any other name, and any voice
    note, is a file of its own."""
    name = Path(name).name
    suffix = Path(name).suffix.lower()
    if kind == NOTE and suffix not in AUDIO_SUFFIXES:
        raise ValueError(f"not an audio file: {name}")
    if kind != NOTE and suffix not in MEDIA_SUFFIXES | {".vtt"}:
        raise ValueError(f"not a media or .vtt file: {name}")
    if not data:
        raise ValueError(f"{name} is empty")
    if len(data) > MAX_INGEST_BYTES:
        raise ValueError(f"{name} is larger than {MAX_INGEST_BYTES >> 20} MB")
    reel = kind != NOTE and re.fullmatch(r"reel-([A-Za-z0-9_-]{6,60})\.\w+", name)
    yt = kind != NOTE and re.fullmatch(r"yt-([A-Za-z0-9_-]{11})(\.[\w-]+)?\.vtt", name)
    if reel and suffix != ".vtt":
        item = Item("instagram", reel.group(1))
    elif yt:
        item = Item("youtube", yt.group(1))
    elif suffix == ".vtt":
        raise ValueError(f"a .vtt must be named yt-<video id>.<lang>.vtt: {name}")
    else:
        item = Item("file", hashlib.sha256(data).hexdigest()[:16], name=name)
    root = private_root()
    kept = root / "inbox" / (item.key + ("" if not yt else "." + name.split(".", 1)[1]))
    if not yt:
        kept = kept.with_suffix(suffix)
    fd = os.open(kept, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return item, kept


# --- the drop door ----------------------------------------------------------------
#
# Kit's Telegram chat has a door in front of the model (the video-drop-door
# plugin, compose/openclaw-gateway/plugins/video-drop-door): a message that is
# one bare link, or that carries a video file, is queued here and answered with
# a receipt, no model turn. The verdict comes back the way every kit item's
# does, from the drain turn to the owner's chat.

DOOR_MAX = 2048
NAME_LIMIT = 60


def door_link(text: str) -> Item | None:
    """The video a message names when its whole text, trimmed, is one YouTube
    or Instagram link; None for anything else, a link inside prose included.
    Strict on shape: printable ASCII with no spaces, an explicit http(s)
    scheme, no user or password, no port but the default."""
    t = text.strip()
    if not t or len(t) > DOOR_MAX or not (t.isascii() and t.isprintable()):
        return None
    if " " in t:
        return None
    try:
        u = urlparse(t)
        port = u.port
    except ValueError:
        return None
    if u.scheme.lower() not in ("http", "https") or not u.hostname:
        return None
    if u.username is not None or u.password is not None or port not in (None, 80, 443):
        return None
    return parse_link(t)


def door_name(item: Item) -> str:
    """The item as a receipt names it: never the link, so the chat shows no
    preview, and a file's name cut to fit."""
    if item.source == "youtube":
        return f"youtube video {item.ident}"
    if item.source == "instagram":
        return f"instagram {'share link' if item.share else 'reel'} {item.ident}"
    name = "".join(c if c.isprintable() else "?" for c in item.name)
    return f"video file {name[:NAME_LIMIT]}"


def receipt(q: Queued, place: tuple[int, int] | None) -> str:
    """The door's answer: one line naming the item and its place in the queue,
    or the earlier verdict when the index already has it."""
    if q.repeat:
        return "\n".join(q.repeat)
    head = "video queued" if q.added else "video already queued"
    at = f"position {place[0]} of {place[1]}" if place else "in the queue"
    return f"{head}: {door_name(q.item)} - {at}; the verdict comes here once judged"


def door(text: str) -> str | None:
    """The door for a message's text: its receipt, or None to leave it to the
    model."""
    item = door_link(text)
    if item is None:
        return None
    return _door_receipt(_file_job(item, "kit", default_mode(item, False)))


def door_file(media: Path) -> str | None:
    """The door for a video file the gateway saved: its receipt, or None when
    it is not one this skill evaluates. Checked before it is read: a regular
    file, not a link, a media suffix, within the ingest cap."""
    try:
        st = media.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or media.suffix.lower() not in MEDIA_SUFFIXES:
        return None
    if st.st_size > MAX_INGEST_BYTES:
        raise ValueError(f"{media.name} is larger than {MAX_INGEST_BYTES >> 20} MB")
    item, kept = _kept(media.name, media.read_bytes())
    return _door_receipt(_file_job(item, "kit", default_mode(item, False), kept))


def _door_receipt(q: Queued) -> str:
    return receipt(q, None if q.repeat else jobq().position(q.jid))


def _transcript_path(root: Path, item: Item) -> Path:
    return root / "transcripts" / f"{item.key}.txt"


def _report_path(root: Path, item: Item, rubric: Rubric, kind: str = KIND) -> Path:
    # A note is kept out of intake-to-vault's reach, which files reports/*.md.
    reports = root / "reports" / "notes" if kind == NOTE else root / "reports"
    return reports / f"{item.key}.{rubric.stamp}.md"


def _goals(root: Path) -> str:
    try:
        goals = (root / "goals.md").read_text().strip()
    except OSError:
        goals = ""
    if not goals:
        return "none on file - judge against what you know of them and say so under Confidence"
    return goals[:GOALS_LIMIT] + ("\n[goals cut]" if len(goals) > GOALS_LIMIT else "")


def parts(text: str) -> list[str]:
    """A long transcript in parts the judge reads one at a time, split at a
    sentence or word end."""
    out = []
    while len(text) > PART_CHARS:
        cut = max(text.rfind(". ", 0, PART_CHARS), text.rfind(" ", 0, PART_CHARS))
        cut = cut + 1 if cut > PART_CHARS // 2 else PART_CHARS
        out.append(text[:cut].strip())
        text = text[cut:]
    out.append(text.strip())
    return out


def material_text(
    jid: str, root: Path, m: Material, mode: str, rubric: Rubric, kind: str = KIND
) -> str:
    me = f"python3 {Path(__file__).resolve()}"
    chunks = parts(m.text)
    lines = m.header() + [
        f"Kind: {kind}",
        f"Rubric: {rubric.path} (video-eval {rubric.stamp}) - read it before judging",
        f"Mode: {mode}",
        "Goals:",
        _goals(root),
        "",
        chunks[0],
    ]
    if len(chunks) > 1:
        lines += [
            "",
            f"[part 1 of {len(chunks)}; read the next with: {me} part {jid} 2]",
        ]
    lines += [
        "",
        f"File the report the rubric defines with: {me} report {jid} <report file>",
    ]
    return "\n".join(lines)


def run_job(jid: str) -> str:
    """The queued run, what the drain turn answers from. NO_REPLY when there
    is nothing to tell anyone yet (not the queue's run of this job, or it will
    be tried again); a repeat's earlier verdict; the material for the judge;
    or the item's line and a normal 'unavailable' answer or the final
    failure."""
    q = jobq()
    job = q.running(jid)
    if job is None or job.get("kind") != KIND:
        return NO_REPLY
    p = job["payload"]
    item = _item(p)
    reply = p.get("origin") in REPLY_ORIGINS
    try:
        rubric = load_rubric()
        root = private_root()
        prune(root, int(os.environ.get("VIDEO_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))
        m, line, error = _material(item, root, p)
    except FileNotFoundError as e:
        m, line, error = None, "", f"{e.filename} is not installed"
    # Reported like any failed run, never left running.
    except (OSError, ValueError) as e:
        m, line, error = None, "", str(e)
    if m is not None:
        row = earlier(root, m.url, m.title)
        if row is not None:
            if normalize_url(row["source"]) != m.url:
                kind = p.get("kind", KIND)
                _append_repeat(root, row, m.url, p.get("origin", ""), rubric, kind)
            answer = repeat_reply(row)
            q.finish(jid, "\n".join(answer))
            return "\n".join(answer) if reply else NO_REPLY
        mode = p.get("mode", "standard")
        return material_text(jid, root, m, mode, rubric, p.get("kind", KIND))
    if line:
        q.finish(jid, f"{p['url']}\n{line}")
        return f"{p['url']}\n{line}" if reply else NO_REPLY
    if job["probe"] and not q.egress_ready(job["probe"]):
        q.release(jid)  # the egress went away mid-run: wait for it again
        return NO_REPLY
    if q.retry(jid, error):
        if p.get("path"):
            Path(p["path"]).unlink(missing_ok=True)
        said = f"{p['url']}\n{FAILED} after {job['attempts']} tries - {error}"
        return said if reply else NO_REPLY
    return NO_REPLY


def _material(item: Item, root: Path, p: dict) -> tuple[Material | None, str, str]:
    """The transcript kept from an earlier attempt, else a fresh fetch. It is
    kept under the job's own key, so a resolved share link is resolved once."""
    saved = _transcript_path(root, item)
    if saved.is_file():
        return Material.parse(saved.read_text()), "", ""
    if item.source == "instagram" and item.share:
        resolved = resolve_share(item)
        if not resolved:
            return None, NO_SHARE, ""
        item = resolved
    path = Path(p["path"]) if p.get("path") else None
    if path is not None and not path.is_file():
        return None, "", f"the queued file {path.name} is gone"
    work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=root))
    try:
        m, line, error = gather(item, work, path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if path is not None and not error:
        path.unlink(missing_ok=True)  # a waiting file is not kept past its run
    if m is not None:
        _private_write(saved, m.render())
    return m, line, error


# --- the report ---------------------------------------------------------------------


@dataclass
class Report:
    head: dict
    sections: list[tuple[str, list[str]]]
    verdict: dict


TEST_LINE = re.compile(r"([a-z-]+)\s+(pass|fail)", re.IGNORECASE)


def parse_report(
    text: str, rubric: Rubric, today: datetime.date, fields: tuple[str, ...] = ()
) -> tuple[Report | None, list[str]]:
    """The judge's report against the rubric: (report, []) or (None, reasons).
    fields are header lines the item's kind needs besides the three."""
    lines = [
        ln.rstrip() for ln in text.strip().splitlines() if not ln.startswith("```")
    ]
    errors: list[str] = []
    head: dict = {}
    sections: list[tuple[str, list[str]]] = []
    for ln in lines:
        if ln.startswith("## "):
            sections.append((ln[3:].strip(), []))
        elif sections:
            if ln.strip() or sections[-1][1]:
                sections[-1][1].append(ln)
        elif ": " in ln:
            k, v = ln.split(": ", 1)
            k, v = k.strip().lower(), v.strip()
            head[k] = v if k in ("source", "note") else v[:HEAD_LIMIT]
    for k in ("title", "category", "gist", *fields):
        if not head.get(k):
            errors.append(f"the header has no {k.capitalize()}: line")
    if head.get("category") and head["category"] not in rubric.list("categories"):
        errors.append(
            f"Category {head['category']!r} is not one of: {', '.join(rubric.list('categories'))}"
        )
    want = rubric.list("sections")
    got = [name for name, _ in sections]
    if [g.lower() for g in got] != [w.lower() for w in want]:
        errors.append(
            f"the sections must be exactly, in order: {'; '.join(want)} (got: {'; '.join(got) or 'none'})"
        )
    sections = [
        (want[i] if i < len(want) else n, body) for i, (n, body) in enumerate(sections)
    ]
    for name, body in sections:
        while body and not body[-1].strip():
            body.pop()
        if not body:
            errors.append(f"section {name!r} is empty")
    verdict: dict = {}
    body = dict(sections).get("Verdict")
    if body is not None:
        verdict, why = _verdict(body, rubric, today)
        errors += why
    conf = dict(sections).get("Confidence")
    if conf:
        word = conf[0].split()[0].strip(".,:-").lower() if conf[0].split() else ""
        if word not in rubric.list("confidence"):
            errors.append(
                f"Confidence must start with one of: {', '.join(rubric.list('confidence'))}"
            )
    if errors:
        return None, errors
    sections = [
        (n, _verdict_lines(verdict) if n == "Verdict" else b) for n, b in sections
    ]
    return Report(head, sections, verdict), []


def _verdict(
    body: list[str], rubric: Rubric, today: datetime.date
) -> tuple[dict, list[str]]:
    f = {}
    for ln in body:
        k, sep, v = ln.partition(":")
        if sep:
            f[k.strip().lower()] = v.strip()
    errors = []
    tests = {t.lower(): r.lower() for t, r in TEST_LINE.findall(f.get("tests", ""))}
    want = rubric.list("tests")
    if sorted(tests) != sorted(want):
        errors.append(f"Tests: must give pass or fail for each of: {', '.join(want)}")
    v = f.get("verdict", "").lower()
    out = {"tests": tests, "verdict": v, "next": f.get("next step", "")}
    if v not in rubric.list("verdicts"):
        errors.append(f"Verdict: must be one of: {', '.join(rubric.list('verdicts'))}")
    if v == "accept":
        if any(r != "pass" for r in tests.values()):
            errors.append(
                "accept needs every test passed; a failed test is a park or a drop"
            )
        out["project"] = f.get("project", "")
        if not out["project"]:
            errors.append("accept needs a Project: line naming what it feeds")
    elif v == "park":
        out["recheck"] = f.get("recheck", "")
        try:
            due = datetime.date.fromisoformat(out["recheck"])
        except ValueError:
            due = None
        if due is None or due <= today:
            errors.append("park needs a Recheck: YYYY-MM-DD date after today")
    elif v == "drop":
        code, _, why = f.get("reason", "").partition(" - ")
        out["reason"], out["why"] = code.strip(), why.strip()
        if out["reason"] not in rubric.list("drop-reasons"):
            errors.append(
                f"drop needs Reason: <code> - <one sentence>, the code one of: {', '.join(rubric.list('drop-reasons'))}"
            )
        elif not out["why"]:
            errors.append("drop needs one sentence after its reason code")
        out["next"] = out["next"] or "none"
    if not out["next"]:
        errors.append("the Verdict section needs a Next step: line")
    return out, errors


def _verdict_lines(v: dict) -> list[str]:
    tests = ", ".join(f"{t} {r}" for t, r in v["tests"].items())
    lines = [f"Tests: {tests}", f"Verdict: {v['verdict']}"]
    if v["verdict"] == "accept":
        lines.append(f"Project: {v['project']}")
    elif v["verdict"] == "park":
        lines.append(f"Recheck: {v['recheck']}")
    elif v["verdict"] == "drop":
        lines.append(f"Reason: {v['reason']} - {v['why']}")
    return lines + [f"Next step: {v['next']}"]


def _render_body(r: Report) -> str:
    out = [
        f"Title: {r.head['title']}",
        f"Category: {r.head['category']}",
        f"Gist: {r.head['gist']}",
    ]
    for name, body in r.sections:
        out += ["", f"## {name}", *body]
    return "\n".join(out)


def fit(r: Report, max_lines: int, max_chars: int) -> bool:
    """Cut the report to the rubric's cap, from the end of the longest section
    first (never the Verdict), each cut section ending in the marker. Returns
    whether anything was cut."""
    cut = set()

    def size() -> tuple[int, int]:
        text = _render_body(r)
        return len(text.splitlines()), len(text)

    for i, (name, body) in enumerate(r.sections):
        long = [ln if len(ln) <= 400 else ln[:397] + "..." for ln in body]
        if long != body:
            r.sections[i] = (name, long)
            cut.add(name)
    while True:
        lines, chars = size()
        if lines <= max_lines and chars <= max_chars:
            break
        cands = [
            (sum(len(x) for x in b), i)
            for i, (n, b) in enumerate(r.sections)
            if n != "Verdict" and len([x for x in b if x != CUT]) > 1
        ]
        if not cands:
            break
        _, i = max(cands)
        name, body = r.sections[i]
        body = [x for x in body if x != CUT][:-1] + [CUT]
        r.sections[i] = (name, body)
        cut.add(name)
    return bool(cut)


def stamp(
    r: Report,
    item_url: str,
    origin: str,
    mode: str,
    rubric: Rubric,
    now: float,
    kind: str = KIND,
) -> str:
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    body = _render_body(r).split("\n", 3)
    field = {IDEA: "claim", NOTE: "note"}.get(kind, "")
    extra = [f"{field.capitalize()}: {r.head[field]}"] if r.head.get(field) else []
    return (
        "\n".join(
            [
                body[0],
                f"Kind: {kind} | Category: {r.head['category']} | Origin: {origin}",
                f"Source: {item_url}",
                f"Evaluated: {day} | Rubric: video-eval {rubric.stamp} ({mode})",
                body[2],
                *extra,
                body[3] if len(body) > 3 else "",
            ]
        ).rstrip()
        + "\n"
    )


def file_report(jid: str, text: str, now: float | None = None) -> tuple[int, str]:
    """(exit code, output) for `report`: the reply for the origin, NO_REPLY,
    or the reasons the report does not follow the rubric."""
    now = time.time() if now is None else now
    q = jobq()
    job = q.get(jid)
    if job is None or job.get("kind") != KIND or job.get("status") != "running":
        return 2, f"job {jid} is not waiting for a report"
    rubric = load_rubric()
    today = datetime.date(*time.gmtime(now)[:3])
    r, errors = parse_report(text, rubric, today)
    if r is None:
        return 3, "report rejected - fix it and file it again:\n" + "\n".join(
            f"- {e}" for e in errors
        )
    p = job["payload"]
    root = private_root()
    item = _item(p)
    saved = _transcript_path(root, item)
    m = Material.parse(saved.read_text()) if saved.is_file() else Material(p["url"])
    url = m.url
    item = parse_link(url) or item  # a share link's report is the reel's
    # The item's own title when it has one, so the same video from another
    # source is found by it.
    r.head["title"] = m.title[:HEAD_LIMIT] or r.head["title"]
    fit(r, rubric.list("max-lines"), rubric.list("max-chars"))
    origin, mode = p.get("origin", "kit"), p.get("mode", "standard")
    kind = p.get("kind", KIND)
    path = _report_path(root, item, rubric, kind)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    _private_write(path, stamp(r, url, origin, mode, rubric, now, kind))
    row = _row(r, kind, origin, url, rubric, root, path, now)
    append_row(root, row)
    answer = reply_lines(row, root)
    q.finish(jid, "\n".join(answer))
    return 0, "\n".join(answer) if origin in REPLY_ORIGINS else NO_REPLY


def _row(
    r: Report,
    kind: str,
    origin: str,
    url: str,
    rubric: Rubric,
    root: Path,
    path: Path,
    now: float,
) -> dict:
    v = r.verdict
    return {
        "date": time.strftime("%Y-%m-%d", time.gmtime(now)),
        "kind": kind,
        "origin": origin,
        "category": r.head["category"],
        "source": url,
        "title": r.head["title"],
        "gist": r.head["gist"],
        "verdict": v["verdict"],
        "reason": v.get("reason", ""),
        "project": v.get("project", ""),
        "recheck": v.get("recheck", ""),
        "next step or why": v.get("why") or v["next"],
        "rubric": rubric.stamp,
        "report": str(path.relative_to(root)),
    }


def file_idea(text: str, now: float | None = None) -> tuple[int, str]:
    """(exit code, output) for `idea`, always filed as the scout's. The idea is
    recorded under its primary source's link."""
    return _file_direct(text, IDEA, IDEA_FIELDS, IDEA_ORIGIN, IDEA_MODE, now)


def file_link(
    text: str, origin: str, check: bool = False, now: float | None = None
) -> tuple[int, str]:
    """(exit code, output) for `link`: a link or an article, recorded under its
    own link."""
    mode = _producer_mode(origin, check)
    return _file_direct(text, LINK, LINK_FIELDS, origin, mode, now)


def file_note(
    text: str, origin: str, check: bool = False, now: float | None = None
) -> tuple[int, str]:
    """(exit code, output) for `note`: a text note, recorded under its text's
    digest."""
    mode = _producer_mode(origin, check)
    return _file_direct(text, NOTE, NOTE_FIELDS, origin, mode, now)


def _producer_mode(origin: str, check: bool) -> str:
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin {origin!r}: one of {', '.join(ORIGINS)}")
    return "claim-check" if check else "standard"


def _file_direct(
    text: str,
    kind: str,
    fields: tuple[str, ...],
    origin: str,
    mode: str,
    now: float | None = None,
) -> tuple[int, str]:
    """(exit code, output) for an item its producer has judged already: the
    three lines, an earlier verdict for the same item, or the reasons the
    report does not follow the rubric. Its report is kept in reports/<kind>s/,
    out of reach of intake-to-vault, which files a video's report with its
    transcript."""
    now = time.time() if now is None else now
    rubric = load_rubric()
    if kind not in rubric.list("kinds"):
        raise ValueError(f"{rubric.path} has no {kind} kind")
    today = datetime.date(*time.gmtime(now)[:3])
    r, errors = parse_report(text, rubric, today, fields)
    url = ""
    if r is not None and kind == NOTE:
        note = " ".join(r.head["note"].split())
        if len(note) > NOTE_LIMIT:
            r, errors = None, [f"Note: must be at most {NOTE_LIMIT} characters"]
        else:
            digest = hashlib.sha256(note.casefold().encode()).hexdigest()[:16]
            url = f"note:{digest}"
    elif r is not None:
        u = urlparse(r.head["source"])
        if u.scheme not in ("http", "https") or not u.netloc:
            what = "the primary source" if kind == IDEA else "the item"
            r, errors = None, [f"Source: must be the http(s) link of {what}"]
        elif kind == LINK and parse_link(r.head["source"]) is not None:
            why = "Source: is a YouTube or Instagram video: queue it with enqueue"
            r, errors = None, [why]
        else:
            url = normalize_url(r.head["source"])
            if _cell(url) != url:
                why = f"Source: must fit an index cell: at most {CELL_LIMIT} characters, no |"
                r, errors = None, [why]
    if r is None:
        return 3, "report rejected - fix it and file it again:\n" + "\n".join(
            f"- {e}" for e in errors
        )
    root = private_root()
    row = earlier(root, url, r.head["title"])
    if row is not None:
        if normalize_url(row["source"]) != url:
            _append_repeat(root, row, url, origin, rubric, kind)
        return 0, "\n".join(repeat_reply(row))
    fit(r, rubric.list("max-lines"), rubric.list("max-chars"))
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    path = root / "reports" / f"{kind}s" / f"{kind}-{key}.{rubric.stamp}.md"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    _private_write(path, stamp(r, url, origin, mode, rubric, now, kind))
    row = _row(r, kind, origin, url, rubric, root, path, now)
    append_row(root, row)
    return 0, "\n".join(reply_lines(row, root))


# --- the index ----------------------------------------------------------------------


def _cell(value: str) -> str:
    v = " ".join(str(value).replace("|", "/").split())
    return v if len(v) <= CELL_LIMIT else v[: CELL_LIMIT - 3] + "..."


def read_index(root: Path) -> list[dict]:
    try:
        text = (root / "index.md").read_text()
    except OSError:
        return []
    rows = []
    for ln in text.splitlines():
        if not ln.startswith("| ") or ln.startswith("| date |"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) == len(INDEX_COLUMNS) and set(cells) != {"---"}:
            rows.append(dict(zip(INDEX_COLUMNS, cells)))
    return rows


@contextlib.contextmanager
def _index_lock(root: Path):
    with open(root / ".index.lock", "a") as f:
        os.chmod(f.fileno(), 0o600)
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def append_row(root: Path, row: dict) -> bool:
    """Append one row, atomically, unless the index already has this source
    under this rubric version (a retried report). The file is only ever
    appended to; the header is written once, when it is created."""
    index = root / "index.md"
    with _index_lock(root):
        for old in read_index(root):
            if old["source"] == _cell(row["source"]) and old["rubric"] == row["rubric"]:
                return False
        line = "| " + " | ".join(_cell(row.get(c, "")) for c in INDEX_COLUMNS) + " |\n"
        fd = os.open(index, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                line = INDEX_HEAD + line
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    return True


def earlier(root: Path, url: str, title: str) -> dict | None:
    """The first index row for the same item: its normalized URL, else its
    normalized title. Any producer's row counts: the index is shared."""
    url = normalize_url(url)
    title = normalize_title(_cell(title))
    for row in read_index(root):
        if normalize_url(row["source"]) == url:
            return row
        if title and normalize_title(row["title"]) == title:
            return row
    return None


def _append_repeat(
    root: Path, row: dict, url: str, origin: str, rubric: Rubric, kind: str = KIND
) -> None:
    """The same item from another source: its own row, a duplicate of the
    earlier one, pointing at the earlier report."""
    append_row(
        root,
        {
            "date": time.strftime("%Y-%m-%d", time.gmtime()),
            "kind": kind,
            "origin": origin,
            "category": row["category"],
            "source": url,
            "title": row["title"],
            "gist": row["gist"],
            "verdict": "drop",
            "reason": "duplicate",
            "next step or why": f"same as {row['source']} ({row['date']}: {row['verdict']})",
            "rubric": rubric.stamp,
            "report": row["report"],
        },
    )


def _verdict_word(row: dict) -> str:
    v = row["verdict"]
    if row.get("reason"):
        return f"{v} ({row['reason']})"
    if v == "accept" and row.get("project"):
        return f"{v} -> {row['project']}"
    if v == "park" and row.get("recheck"):
        return f"{v} until {row['recheck']}"
    return v


def reply_lines(row: dict, root: Path) -> list[str]:
    """The origin's answer, three lines: title; kind, category, verdict and
    reason code with the next step or the reason; the report."""
    return [
        row["title"][:120],
        f"{row['kind']} · {row['category']} · {_verdict_word(row)}: {row['next step or why']}"[
            :300
        ],
        f"report: {root / row['report']}",
    ]


def repeat_reply(row: dict) -> list[str]:
    root = private_root()
    return [
        row["title"][:120],
        f"{row['kind']} · {row['category']} · already judged {row['date']}: "
        f"{_verdict_word(row)}: {row['next step or why']}"[:300],
        f"report: {root / row['report']}",
    ]


# --- polling ----------------------------------------------------------------------


def status(jid: str) -> str:
    job = jobq().get(jid)
    if job is None:
        return f"unknown - no job {jid} (finished jobs are kept 14 days)"
    s = job["status"]
    if s in ("queued", "running"):
        return f"{s} - attempt {job['attempts']}"
    return f"{s}\n{job.get('result', '')}".rstrip()


def show(jid: str) -> str:
    job = jobq().get(jid)
    if job is None:
        raise ValueError(f"no job {jid}")
    for line in reversed(job.get("result", "").splitlines()):
        if line.startswith("report: "):
            return Path(line[8:]).read_text()
    raise ValueError(f"job {jid} has no report")


# --- the command line ---------------------------------------------------------------


def _flags(args: list[str]) -> tuple[list[str], str, bool]:
    origin, check, rest = "kit", False, []
    it = iter(args)
    for a in it:
        if a == "--check":
            check = True
        elif a == "--origin":
            origin = next(it, "")
        else:
            rest.append(a)
    return rest, origin, check


def main(argv: list[str]) -> int:
    # The door's argument is the owner's message as sent, never read for flags.
    door_cmd = argv[1] if len(argv) == 3 and argv[1] in ("door", "door-file") else ""
    args, origin, check = ([], "kit", False) if door_cmd else _flags(argv[1:])
    if not door_cmd and (not args or args[0] in ("-h", "--help")):
        print(__doc__.split("\n\n")[0] + "\n" + USAGE, file=sys.stderr)
        return 2
    os.umask(0o077)
    cmd, rest = (door_cmd, argv[2:]) if door_cmd else (args[0], args[1:])
    try:
        if cmd in ("door", "door-file"):
            out = door(rest[0]) if cmd == "door" else door_file(Path(rest[0]))
            if out is None:
                print("not for the door", file=sys.stderr)
                return 2
            print(out)
            return 0
        if cmd == "run" and len(rest) == 1:
            print(run_job(rest[0]))
            return 0
        if cmd == "part" and len(rest) == 2 and rest[1].isdigit():
            job = jobq().get(rest[0])
            if job is None:
                raise ValueError(f"no job {rest[0]}")
            saved = _transcript_path(private_root(), _item(job["payload"]))
            chunks = parts(Material.parse(saved.read_text()).text)
            n = int(rest[1])
            if not 1 <= n <= len(chunks):
                raise ValueError(f"part {n} of {len(chunks)} does not exist")
            print(f"[part {n} of {len(chunks)}]\n{chunks[n - 1]}")
            return 0
        if cmd == "report" and len(rest) in (1, 2):
            text = Path(rest[1]).read_text() if len(rest) == 2 else sys.stdin.read()
            code, out = file_report(rest[0], text)
            print(out, file=sys.stderr if code else sys.stdout)
            return code
        if cmd == "idea" and len(rest) <= 1 and "--origin" not in argv:
            text = Path(rest[0]).read_text() if rest else sys.stdin.read()
            code, out = file_idea(text)
            print(out, file=sys.stderr if code else sys.stdout)
            return code
        if cmd in ("link", "note") and len(rest) <= 1:
            text = Path(rest[0]).read_text() if rest else sys.stdin.read()
            filer = file_link if cmd == "link" else file_note
            code, out = filer(text, origin, check)
            print(out, file=sys.stderr if code else sys.stdout)
            return code
        if cmd == "status" and len(rest) == 1:
            print(status(rest[0]))
            return 0
        if cmd == "show" and len(rest) == 1:
            print(show(rest[0]), end="")
            return 0
        if cmd == "index" and not rest:
            index = private_root() / "index.md"
            print(
                index.read_text() if index.is_file() else "the index is empty", end=""
            )
            return 0
        if cmd == "enqueue" and len(rest) == 1:
            out = enqueue(rest[0], origin, check)
            if out is None:
                print(
                    f"not a YouTube or Instagram video link: {rest[0]}", file=sys.stderr
                )
                return 2
            print(out)
            return 0
        if cmd == "ingest" and len(rest) == 1:
            print(ingest(rest[0], sys.stdin.buffer.read(), origin, check))
            return 0
        if cmd == "voice" and len(rest) == 1:
            print(voice(rest[0], sys.stdin.buffer.read(), origin, check))
            return 0
        if (
            cmd
            in (
                "run",
                "part",
                "report",
                "status",
                "show",
                "index",
                "idea",
                "link",
                "note",
                "voice",
                "enqueue",
                "ingest",
                "door",
                "door-file",
            )
            or rest
        ):
            print(USAGE, file=sys.stderr)
            return 2
        out = enqueue(cmd, "kit", check)
        if out is None:
            media = Path(cmd.strip()).expanduser()
            if not media.is_file():
                print(
                    f"not a YouTube or Instagram video link or a media file: {cmd}",
                    file=sys.stderr,
                )
                return 2
            out = ingest(media.name, media.read_bytes(), "kit", check)
        print(out)
        return 0
    except FileNotFoundError as e:  # ffmpeg, whisper or the queue missing
        print(f"video failed: {e.filename} is not installed", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(f"video failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
