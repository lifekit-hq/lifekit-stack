"""The video-eval skill, its ensure script and its host wrapper
(docs/runbook.md, "Video evaluation").

yt-dlp, ffmpeg and whisper are replaced by stubs that write the files a real
run would, and Instagram's share-link redirect by a fake opener, so nothing
here touches the network or needs the models. The queue is the real one
(skills/job-queue), and so is the media code (skills/youtube-transcript/media.py),
in a temporary directory with the "direct" egress unless a test sets another.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills/video-eval"
ENSURE = REPO / "scripts/ensure-video-eval.sh"
WRAPPER = REPO / "scripts/video-eval.sh"

_SPEC = importlib.util.spec_from_file_location("video", SKILL / "video.py")
video = importlib.util.module_from_spec(_SPEC)
sys.modules["video"] = video  # dataclasses looks its own module up here
_SPEC.loader.exec_module(video)

CODE = "DAbcdEFGhij"
REEL = f"https://www.instagram.com/reel/{CODE}/"
VID = "bQPU6UJ4iCw"
YT = f"https://www.youtube.com/watch?v={VID}"
TOMORROW = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

# A yt-dlp stand-in. The captions call (media.py: --print-to-file) writes
# a title and, when YT_CAPTIONS=yes, an English track; the info call
# (--skip-download --write-info-json) writes the info; a download writes the
# audio and the info. STUB_MODE picks a failure; argv is logged for the test.
YT_DLP_STUB = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
mode = os.environ["STUB_MODE"]
open(os.environ["STUB_LOG"], "a").write("yt-dlp " + " ".join(argv) + "\n")
url = argv[-1]
ident = url.split("v=", 1)[1] if "v=" in url else url.rstrip("/").rsplit("/", 1)[-1]
out = Path(argv[argv.index("-o") + 1]).parent
if "--print-to-file" in argv:
    if os.environ.get("YT_CAPTIONS") == "unreachable":
        sys.stderr.write("ERROR: Unable to download webpage: Connection refused\n")
        sys.exit(1)
    files = [argv[i + 2] for i, a in enumerate(argv) if a == "--print-to-file"]
    Path(files[0]).write_text("A talk about agents\n")
    Path(files[1]).write_text("en\n")
    if os.environ.get("YT_CAPTIONS") == "yes" and "--write-subs" in argv:
        (out / (ident + ".en.vtt")).write_text(
            "WEBVTT\n\n00:00.000 --> 00:02.000\nAgents cut costs by half.\n"
        )
    sys.exit(0)
if mode == "login":
    sys.stderr.write("ERROR: [Instagram] x: Instagram sent an empty media response.\n")
    sys.exit(1)
if mode == "gone":
    sys.stderr.write("ERROR: [Instagram] x: HTTP Error 404: Not Found\n")
    sys.exit(1)
if mode == "novideo":
    sys.stderr.write("ERROR: [Instagram] x: There is no video in this post\n")
    sys.exit(1)
if mode == "private":
    sys.stderr.write("ERROR: [youtube] x: Private video. Sign in if you've been granted access\n")
    sys.exit(1)
if mode == "boom":
    sys.stderr.write("ERROR: Unable to download webpage: <urlopen error timed out>\n")
    sys.exit(1)
info = {
    "id": ident, "uploader": "some_account",
    "duration": float(os.environ.get("STUB_DURATION", "41.6")),
    "title": os.environ.get(
        "STUB_TITLE", "A talk about agents" if "v=" in url else "Video by some_account"
    ),
    "description": "Drink lemon water\nit cures everything",
}
(out / (ident + ".info.json")).write_text(json.dumps(info))
if "--skip-download" in argv:
    sys.exit(0)
if mode == "toolong":
    sys.stdout.write("[download] x does not pass filter (duration<=?900), skipping ..\n")
    sys.exit(0)
(out / (ident + ".m4a")).write_bytes(b"audio-bytes")
"""

FFMPEG_STUB = r"""#!/bin/sh
[ "$FFMPEG_MODE" = fail ] && exit 1
for a; do out="$a"; done
printf 'RIFF' > "$out"
"""

WHISPER_STUB = r"""#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
argv = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write("whisper " + " ".join(argv) + "\n")
time.sleep(float(os.environ.get("WHISPER_SLEEP", "0")))
if os.environ.get("WHISPER_MODE") == "fail":
    sys.stderr.write("Traceback: boom\n")
    sys.exit(1)
out = Path(argv[argv.index("--output_dir") + 1])
(out / (Path(argv[-1]).stem + ".txt")).write_text(os.environ.get("WHISPER_TEXT", ""))
print("Detected language: Ukrainian")
"""


def _stub(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def report_text(
    verdict: str = "accept",
    *,
    tests: str = "evidence pass, cost pass, goal-trace pass, no-overengineering pass",
    category: str = "dev-tooling",
    extra: str = "",
    claims: str = '"Agents cut costs by half" - Partly true. One vendor case. (vendor docs)',
    sections: list[str] | None = None,
) -> str:
    lines = {
        "accept": "Verdict: accept\nProject: the agent fleet",
        "park": f"Verdict: park\nRecheck: {TOMORROW}",
        "drop": "Verdict: drop\nReason: paid-funnel - It sells a course.",
    }[verdict]
    body = {
        "What it is": "A talk, 42s, Ukrainian; agents save money.",
        "Claims and how they hold": claims,
        "Useful for the goals": "The fleet's cost goal.",
        "Cost of adoption": "Two hours to try; no money.",
        "Verdict": f"Tests: {tests}\n{lines}\nNext step: try it on one job{extra}",
        "Confidence": "medium - automatic transcript, one source.",
        "Sources": f"{REEL}\nhttps://docs.example/agents",
    }
    names = sections or list(body)
    out = [
        "Title: Agents at half the cost",
        f"Category: {category}",
        "Gist: a pitch that agents halve costs",
    ]
    for name in names:
        out += ["", f"## {name}", body.get(name, "text")]
    return "\n".join(out) + "\n"


CLAIM = (
    "import sys; sys.path.insert(0, sys.argv[1]); import jobq; jobq.claim(sys.argv[2])"
)


@pytest.fixture
def box(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stubs = {
        "YT_DLP": _stub(bindir / "yt-dlp", YT_DLP_STUB),
        "VIDEO_FFMPEG": _stub(bindir / "ffmpeg", FFMPEG_STUB),
        "VIDEO_WHISPER": _stub(bindir / "whisper", WHISPER_STUB),
    }
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        **os.environ,
        **{k: str(v) for k, v in stubs.items()},
        "INTAKE_DIR": str(tmp_path / "intake"),
        "JOBQ_DIR": str(tmp_path / "queue"),
        "LIFEKIT_EGRESS_PROXY": "direct",
        "STUB_LOG": str(log),
        "STUB_MODE": "ok",
        "YT_CAPTIONS": "no",
        "WHISPER_TEXT": " Lemon water cures cancer.\nStudies show it. ",
    }
    for k in ("JOBQ_MAX_ATTEMPTS", "VIDEO_RUBRIC", "VIDEO_MAX_SECONDS"):
        env.pop(k, None)
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Box(env, tmp_path / "intake", log, tmp_path / "queue")


class Box:
    def __init__(self, env, root, log, queue):
        self.env, self.root, self.log, self.queue = env, root, log, queue

    def cli(self, *args: str, stdin: str | bytes | None = None, **extra):
        r = subprocess.run(
            [sys.executable, str(SKILL / "video.py"), *args],
            env={**self.env, **extra},
            input=stdin.encode() if isinstance(stdin, str) else stdin,
            capture_output=True,
            check=False,
        )
        r.stdout, r.stderr = r.stdout.decode(), r.stderr.decode()
        return r

    def run(self, link: str, *flags: str, **extra):
        """Queue the link as kit does, then the queue's run of it."""
        r = self.cli(link, *flags, **extra)
        if r.returncode != 0 or not r.stdout.startswith("video "):
            return r
        return self.start(self.jid(link), **extra)

    def start(self, jid: str, **extra):
        """The queue's run of the job, in the foreground: claim it as `next`
        does, then its handler as the runner starts it. A job `next` would not
        start is left as it is, and its handler says nothing."""
        env = {**self.env, **extra}
        subprocess.run(
            [sys.executable, "-c", CLAIM, str(SKILL.parent / "job-queue"), jid],
            env=env,
            check=True,
        )
        return self.cli("run", jid, **extra, JOBQ_RUN_ID=jid)

    @staticmethod
    def jid(link: str) -> str:
        return f"video-{video.parse_link(link).key}"

    def job(self, jid: str) -> dict:
        return json.loads((self.queue / f"{jid}.json").read_text())

    def index(self) -> list[dict]:
        return video.read_index(self.root)

    def calls(self, tool: str) -> list[list[str]]:
        return [
            ln.split()[1:]
            for ln in self.log.read_text().splitlines()
            if ln.startswith(tool + " ")
        ]


# --- links -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arg,ident,share",
    [
        (REEL, CODE, False),
        (
            f"https://instagram.com/reel/{CODE}/?igsh=MWZ4eHh4&utm_source=ig",
            CODE,
            False,
        ),
        (f"http://m.instagram.com/reels/{CODE}/", CODE, False),
        (f"https://www.instagram.com/p/{CODE}/", CODE, False),
        (f"https://www.instagram.com/some.account/reel/{CODE}/", CODE, False),
        (f"https://instagr.am/reel/{CODE}/", CODE, False),
        (f"  {REEL} is this true?  ", CODE, False),
        ("https://www.instagram.com/share/reel/BAbcdEFgh/", "BAbcdEFgh", True),
        ("https://www.instagram.com/share/BAbcdEFgh/?igsh=x", "BAbcdEFgh", True),
    ],
)
def test_reel_links_are_parsed_to_their_shortcode(arg, ident, share):
    assert video.parse_link(arg) == video.Item("instagram", ident, share)


@pytest.mark.parametrize(
    "arg",
    [
        YT,
        f"https://youtube.com/watch?v={VID}&t=42s&si=abc",
        f"https://m.youtube.com/watch?feature=share&v={VID}",
        f"https://youtu.be/{VID}?si=x",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/live/{VID}",
        f"youtube.com/embed/{VID}",
        f"{YT} worth it?",
    ],
)
def test_youtube_links_are_parsed_to_their_video_id(arg):
    item = video.parse_link(arg)
    assert item == video.Item("youtube", VID) and item.url == YT


@pytest.mark.parametrize(
    "arg",
    [
        "",
        "is this true?",
        "https://www.instagram.com.evil.com/reel/DAbcdEFGhij/",
        "https://instagram.com@evil.com/reel/DAbcdEFGhij/",
        "https://www.instagram.com/reels/audio/1234567890123/",
        "https://www.instagram.com/reel/short/",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/@some_channel",
        "https://youtube.com.evil.com/watch?v=bQPU6UJ4iCw",
        "https://vimeo.com/123456",
        "javascript:alert(1)",
    ],
)
def test_anything_else_is_not_a_link(arg):
    assert video.parse_link(arg) is None


@pytest.mark.parametrize(
    "a,b",
    [
        (f"https://youtu.be/{VID}?si=x", YT),
        (f"https://instagram.com/x/reel/{CODE}/?igsh=1", REEL),
        (
            "HTTPS://WWW.Example.com/post/?utm_source=x&id=3#top",
            "https://example.com/post?id=3",
        ),
        ("example.com/a/", "https://example.com/a"),
    ],
)
def test_urls_are_normalized_for_matching(a, b):
    assert video.normalize_url(a) == b


def test_titles_match_without_case_or_punctuation_and_generic_ones_never():
    assert video.normalize_title("Agents: at HALF the cost!") == video.normalize_title(
        "agents at half the cost"
    )
    assert video.normalize_title("Video by some_account") == ""
    assert video.normalize_title("short") == ""


# --- share links -------------------------------------------------------------------


class FakeOpener:
    """open() raises the HTTPError urllib raises for an unfollowed redirect."""

    def __init__(self, locations):
        self.locations = list(locations)
        self.asked = []

    def open(self, req, timeout=None):
        self.asked.append(req.full_url)
        loc = self.locations.pop(0)
        if loc is None:
            raise urllib.error.URLError("down")
        headers = Message()
        if loc:
            headers["Location"] = loc
            raise urllib.error.HTTPError(req.full_url, 302, "Found", headers, None)
        return type("R", (), {"close": lambda self: None})()


def _share(monkeypatch, *locations):
    op = FakeOpener(locations)
    monkeypatch.setattr(video.urllib.request, "build_opener", lambda *_a: op)
    return op


SHARE = video.Item("instagram", "BAbcdEFgh", True)


def test_a_share_link_resolves_to_the_reel_it_redirects_to(monkeypatch):
    op = _share(monkeypatch, f"/reel/{CODE}/?igsh=x")
    assert video.resolve_share(SHARE) == video.Item("instagram", CODE)
    assert op.asked == [SHARE.url]


def test_a_share_link_sent_to_the_login_page_reads_the_target_from_next(monkeypatch):
    _share(
        monkeypatch,
        f"https://www.instagram.com/accounts/login/?next=%2Freel%2F{CODE}%2F",
    )
    assert video.resolve_share(SHARE) == video.Item("instagram", CODE)


@pytest.mark.parametrize(
    "locations",
    [["https://evil.example/reel/DAbcdEFGhij/"], [""], ["/somewhere/else/"] * 3],
)
def test_a_share_link_that_does_not_resolve_is_none(monkeypatch, locations):
    _share(monkeypatch, *locations)
    assert video.resolve_share(SHARE) is None


def test_a_share_link_is_resolved_through_the_egress(box, monkeypatch):
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", "socks5h://proxy.example:1080")
    q = video.jobq()
    asked = []

    def refuse(host, port, proxy, timeout):
        asked.append((host, port, proxy))
        raise ConnectionRefusedError("no egress")

    monkeypatch.setattr(q, "open_connection", refuse)
    with pytest.raises(OSError):
        video.resolve_share(SHARE)
    assert asked == [("www.instagram.com", 443, "socks5h://proxy.example:1080")]


def _queued_run(monkeypatch, jid: str) -> str:
    """Box.start, in this process."""
    video.jobq().claim(jid)
    monkeypatch.setenv("JOBQ_RUN_ID", jid)
    return video.run_job(jid)


def test_a_shared_reel_is_judged_and_indexed_as_the_reel(box, monkeypatch):
    monkeypatch.setattr(video, "resolve_share", lambda r: video.Item("instagram", CODE))
    assert video.enqueue(SHARE.url, "kit").startswith("video queued")
    jid = f"video-{SHARE.key}"
    out = _queued_run(monkeypatch, jid)
    assert out.startswith(f"Video: {REEL}\n")
    code, _ = video.file_report(jid, report_text())
    assert code == 0
    assert box.index()[0]["source"] == REEL
    assert (box.root / f"reports/ig-{CODE}.v3.md").is_file()


def test_an_unresolvable_share_link_is_a_normal_answer_without_a_download(
    box, monkeypatch
):
    monkeypatch.setattr(video, "resolve_share", lambda r: None)
    video.enqueue(SHARE.url, "kit")
    out = _queued_run(monkeypatch, f"video-{SHARE.key}")
    assert out == f"{SHARE.url}\n{video.NO_SHARE}"
    assert box.log.read_text() == ""


# --- the queue: typing and routing --------------------------------------------------


@pytest.mark.parametrize(
    "origin,quiet", [("kit", False), ("firstmate", True), ("scout", True)]
)
def test_every_producer_files_the_same_typed_job(box, origin, quiet):
    r = box.cli("enqueue", f"{REEL}?igsh=abc", "--origin", origin)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith(f"video queued - job video-ig-{CODE}")
    job = box.job(f"video-ig-{CODE}")
    assert job["kind"] == "video" and job["owner"] == "video-eval"
    assert job["probe"] == "www.instagram.com" and job["quiet"] is quiet
    assert job["handler"][1:] == [str((SKILL / "video.py").resolve()), "run"]
    assert job["payload"]["origin"] == origin
    assert job["payload"]["mode"] == "claim-check"
    assert stat.S_IMODE((box.queue / f"video-ig-{CODE}.json").stat().st_mode) == 0o600
    assert box.log.read_text() == ""  # nothing fetched yet


def test_kit_is_the_origin_of_a_bare_link(box):
    box.cli(YT)
    job = box.job(f"video-yt-{VID}")
    assert job["payload"]["origin"] == "kit" and job["probe"] == "www.youtube.com"
    assert job["payload"]["mode"] == "standard"


def test_an_unknown_origin_is_refused(box):
    r = box.cli("enqueue", YT, "--origin", "someone")
    assert r.returncode == 1 and "unknown origin" in r.stderr


def test_the_same_video_twice_is_already_queued(box):
    box.cli(YT)
    r = box.cli("enqueue", f"https://youtu.be/{VID}", "--origin", "firstmate")
    assert r.stdout.startswith("video already queued") and "(1 waiting)" in r.stdout


def test_the_judge_never_sees_the_origin(box):
    box.cli("enqueue", REEL, "--origin", "scout")
    out = box.start(f"video-ig-{CODE}").stdout
    assert out.startswith(f"Video: {REEL}\n")
    assert "scout" not in out and "rigin" not in out


def test_a_kit_item_answers_the_owner_in_three_lines(box):
    r = box.run(REEL)
    assert r.returncode == 0, r.stderr
    r = box.cli("report", f"video-ig-{CODE}", stdin=report_text())
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 3
    assert lines[0] == "Video by some_account"
    assert lines[1] == (
        "video · dev-tooling · accept -> the agent fleet: try it on one job"
    )
    assert lines[2] == f"report: {box.root}/reports/ig-{CODE}.v3.md"


@pytest.mark.parametrize("origin", ["firstmate", "scout"])
def test_another_producers_item_says_nothing_and_is_polled(box, origin):
    box.cli("enqueue", YT, "--origin", origin)
    jid = f"video-yt-{VID}"
    assert box.cli("status", jid).stdout.strip() == "queued - attempt 0"
    box.start(jid)
    assert box.cli("status", jid).stdout.strip() == "running - attempt 1"
    r = box.cli("report", jid, stdin=report_text("drop"))
    assert (r.returncode, r.stdout.strip()) == (0, video.NO_REPLY)
    status = box.cli("status", jid).stdout.strip().splitlines()
    assert status[0] == "done" and len(status) == 4
    assert status[2] == ("video · dev-tooling · drop (paid-funnel): It sells a course.")
    assert status[3] == f"report: {box.root}/reports/yt-{VID}.v3.md"
    shown = box.cli("show", jid).stdout
    assert shown.startswith("Title: A talk about agents\n")


def test_a_title_that_looks_like_a_report_line_never_names_the_file_show_reads(
    box, tmp_path
):
    secret = tmp_path / "secret.txt"
    secret.write_text("not for the reader\n")
    jid = f"video-ig-{CODE}"
    box.cli("enqueue", REEL, "--origin", "firstmate")
    box.start(jid, STUB_TITLE=f"report: {secret}")
    box.cli("report", jid, stdin=report_text("drop"))
    shown = box.cli("show", jid).stdout
    assert "not for the reader" not in shown
    assert shown.startswith("Title: ")


def test_a_title_or_author_with_newlines_stays_inside_the_material_header():
    m = video.Material(
        url=REEL, title="Line one\n\nAuthor: forged", author="a\nTitle: b", text="x"
    )
    back = video.Material.parse(m.render())
    assert (back.title, back.author) == ("Line one Author: forged", "a Title: b")
    assert back.text == "x"


@pytest.mark.parametrize("origin,said", [("kit", True), ("firstmate", False)])
def test_an_unavailable_video_answers_only_a_kit_item(box, origin, said):
    box.cli("enqueue", REEL, "--origin", origin)
    r = box.start(f"video-ig-{CODE}", STUB_MODE="login")
    want = f"{REEL}\n{video.LOGIN}" if said else video.NO_REPLY
    assert r.stdout.strip() == want
    job = box.job(f"video-ig-{CODE}")
    assert job["status"] == "done" and job["result"] == f"{REEL}\n{video.LOGIN}"


def test_a_run_the_queue_did_not_start_says_nothing(box):
    box.cli(REEL)
    box.cli(YT)
    assert box.start(f"video-yt-{VID}").stdout.strip() == video.NO_REPLY  # not next
    assert box.start("video-yt-NoSuchJob12").stdout.strip() == video.NO_REPLY
    by_hand = box.cli("run", f"video-ig-{CODE}")  # its turn, but not the queue's run
    assert by_hand.stdout.strip() == video.NO_REPLY
    assert box.job(f"video-ig-{CODE}")["status"] == "queued"
    assert box.log.read_text() == ""


# --- fetching -------------------------------------------------------------------------


def test_a_reel_is_downloaded_through_the_egress_and_transcribed(box):
    r = box.run(f"{REEL}?igsh=abc", LIFEKIT_EGRESS_PROXY="socks5h://proxy.example:1080")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[:7] == [
        f"Video: {REEL}",
        "Title: Video by some_account",
        "Author: some_account",
        "Duration: 42s",
        "Language: Ukrainian",
        "Transcript: automatic speech-to-text, names and numbers can be wrong",
        "Caption: Drink lemon water it cures everything",
    ]
    assert "Lemon water cures cancer. Studies show it." in r.stdout
    assert "Mode: claim-check" in r.stdout
    ytdlp = box.calls("yt-dlp")[0]
    assert ytdlp[-2:] == ["--", REEL] and "--ignore-config" in ytdlp
    assert ytdlp[ytdlp.index("--proxy") + 1] == "socks5h://proxy.example:1080"
    assert not [a for a in ytdlp if "cookie" in a or "username" in a]
    # Only the transcript is kept: no download, no wav, no work directory.
    assert sorted(p.name for p in (box.root / "transcripts").iterdir()) == [
        f"ig-{CODE}.txt"
    ]
    assert not [p for p in box.root.iterdir() if p.name.startswith("work-")]
    assert stat.S_IMODE(box.root.stat().st_mode) == 0o700


def test_a_youtube_video_with_captions_is_read_from_them_without_audio(box):
    r = box.run(YT, YT_CAPTIONS="yes")
    assert r.returncode == 0, r.stderr
    assert "Transcript: captions" in r.stdout
    assert "Title: A talk about agents" in r.stdout
    assert "Agents cut costs by half." in r.stdout
    assert "Mode: standard" in r.stdout
    assert not [c for c in box.calls("yt-dlp") if "--format" in c]
    assert box.calls("whisper") == []


def test_a_youtube_video_without_captions_falls_back_to_its_audio(box):
    r = box.run(YT, YT_CAPTIONS="no")
    assert r.returncode == 0, r.stderr
    assert "Transcript: automatic speech-to-text" in r.stdout
    assert "Lemon water cures cancer." in r.stdout
    download = [c for c in box.calls("yt-dlp") if "--format" in c]
    assert len(download) == 1 and download[0][-2:] == ["--", YT]
    assert len(box.calls("whisper")) == 1


def test_a_long_youtube_video_without_captions_is_not_downloaded(box):
    r = box.run(YT, STUB_DURATION="3600")
    assert r.stdout.strip() == f"{YT}\n{video.TOO_LONG}"
    assert not [c for c in box.calls("yt-dlp") if "--format" in c]


def test_the_check_flag_asks_a_youtube_video_for_a_claim_check(box):
    r = box.run(YT, "--check", YT_CAPTIONS="yes")
    assert "Mode: claim-check" in r.stdout


@pytest.mark.parametrize(
    "link,mode,line",
    [
        (REEL, "login", video.LOGIN),
        (REEL, "gone", video.GONE),
        (REEL, "novideo", video.NO_VIDEO),
        (REEL, "toolong", video.TOO_LONG),
        (YT, "private", video.PRIVATE),
    ],
)
def test_a_video_that_cannot_be_fetched_is_a_normal_answer(box, link, mode, line):
    r = box.run(link, STUB_MODE=mode)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [link, line]
    assert list((box.root / "transcripts").iterdir()) == []
    assert box.calls("whisper") == []


def test_a_video_without_speech_is_a_normal_answer(box):
    r = box.run(REEL, WHISPER_TEXT="  \n")
    assert r.stdout.strip() == f"{REEL}\n{video.NO_SPEECH}"


def test_an_unexpected_failure_is_retried_quietly_then_reported_once(box):
    r = box.run(REEL, STUB_MODE="boom", JOBQ_MAX_ATTEMPTS="2")
    assert r.stdout.strip() == video.NO_REPLY
    job = box.job(f"video-ig-{CODE}")
    assert (job["status"], job["attempts"]) == ("queued", 1)
    r = box.start(f"video-ig-{CODE}", STUB_MODE="boom", JOBQ_MAX_ATTEMPTS="2")
    assert r.stdout.splitlines() == [
        REEL,
        f"{video.FAILED} after 2 tries - ERROR: Unable to download webpage: <urlopen error timed out>",
    ]
    assert box.job(f"video-ig-{CODE}")["status"] == "failed"


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize(
    "link,extra", [(REEL, {}), (YT, {"YT_CAPTIONS": "unreachable"})]
)
def test_an_unreachable_egress_leaves_the_job_queued_uncounted(box, link, extra):
    proxy = f"socks5h://127.0.0.1:{_closed_port()}"
    r = box.run(link, STUB_MODE="boom", LIFEKIT_EGRESS_PROXY=proxy, **extra)
    assert (r.returncode, r.stdout.strip()) == (0, video.NO_REPLY)
    job = box.job(box.jid(link))
    assert (job["status"], job["attempts"]) == ("queued", 0)


def test_a_missing_tool_is_a_failed_run_not_a_traceback(box):
    r = box.run(REEL, VIDEO_FFMPEG="/nonexistent/ffmpeg", JOBQ_MAX_ATTEMPTS="1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[1] == (
        f"{video.FAILED} after 1 tries - /nonexistent/ffmpeg is not installed"
    )


def test_the_whisper_model_and_language_can_be_set(box):
    r = box.run(REEL, VIDEO_WHISPER_MODEL="small", VIDEO_WHISPER_LANG="uk")
    assert r.returncode == 0, r.stderr
    whisper = box.calls("whisper")[0]
    assert whisper[:2] == ["--model", "small"] and "uk" in whisper
    assert "Language: uk" in r.stdout


def test_a_second_attempt_reuses_the_kept_transcript(box):
    box.run(REEL)
    q = video.jobq()
    q.release(f"video-ig-{CODE}")
    box.log.write_text("")
    r = box.start(f"video-ig-{CODE}")
    assert r.stdout.startswith(f"Video: {REEL}\n") and box.log.read_text() == ""


def test_a_long_transcript_is_read_in_parts(box):
    long = " ".join(f"Sentence number {i} says something." for i in range(2000))
    r = box.run(REEL, WHISPER_TEXT=long)
    jid = f"video-ig-{CODE}"
    assert (
        f"[part 1 of 4; read the next with: python3 {SKILL.resolve()}/video.py part {jid} 2]"
        in r.stdout
    )
    joined = []
    for n in range(1, 5):
        p = box.cli("part", jid, str(n))
        assert p.returncode == 0 and p.stdout.startswith(f"[part {n} of 4]\n")
        joined.append(p.stdout.split("\n", 1)[1].strip())
    assert " ".join(joined) == long
    assert box.cli("part", jid, "5").returncode == 1


# --- the report ---------------------------------------------------------------------


def _judged(box, link=REEL, origin="kit", **extra):
    box.cli("enqueue", link, "--origin", origin, **extra)
    jid = box.jid(link)
    r = box.start(jid, **extra)
    assert r.stdout.startswith("Video: "), r.stdout + r.stderr
    return jid


def test_the_report_is_stamped_with_the_rubric_version_and_mode(box):
    jid = _judged(box, YT, YT_CAPTIONS="yes")
    r = box.cli("report", jid, stdin=report_text("park"))
    assert r.returncode == 0, r.stderr
    saved = box.root / f"reports/yt-{VID}.v3.md"
    head = saved.read_text().splitlines()[:5]
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert head == [
        "Title: A talk about agents",
        "Kind: video | Category: dev-tooling | Origin: kit",
        f"Source: {YT}",
        f"Evaluated: {today} | Rubric: video-eval v3 (standard)",
        "Gist: a pitch that agents halve costs",
    ]
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    row = box.index()[0]
    assert (row["rubric"], row["verdict"], row["recheck"]) == ("v3", "park", TOMORROW)


def test_a_new_rubric_version_is_the_one_stamped(box, tmp_path):
    bumped = tmp_path / "rubric.md"
    bumped.write_text(
        (SKILL / "rubric.md").read_text().replace("version: 3\n", "version: 7\n", 1)
    )
    jid = _judged(box, VIDEO_RUBRIC=str(bumped))
    r = box.cli("report", jid, stdin=report_text(), VIDEO_RUBRIC=str(bumped))
    assert r.returncode == 0, r.stderr
    saved = box.root / f"reports/ig-{CODE}.v7.md"
    assert "| Rubric: video-eval v7 (claim-check)" in saved.read_text()
    assert box.index()[0]["rubric"] == "v7"


def test_a_claim_check_report_says_so(box):
    jid = _judged(box)
    box.cli("report", jid, stdin=report_text())
    text = (box.root / f"reports/ig-{CODE}.v3.md").read_text()
    assert "Rubric: video-eval v3 (claim-check)" in text
    assert '"Agents cut costs by half" - Partly true.' in text


@pytest.mark.parametrize(
    "text,why",
    [
        (report_text(category="cooking"), "Category 'cooking' is not one of"),
        (
            report_text(sections=["What it is", "Verdict", "Confidence", "Sources"]),
            "the sections must be exactly, in order",
        ),
        (
            report_text(tests="evidence pass, cost pass"),
            "Tests: must give pass or fail",
        ),
        (
            report_text(
                tests="evidence fail, cost pass, goal-trace pass, no-overengineering pass"
            ),
            "accept needs every test passed",
        ),
        (
            report_text("park").replace(TOMORROW, "2020-01-01"),
            "park needs a Recheck: YYYY-MM-DD date after today",
        ),
        (
            report_text("drop").replace("paid-funnel - It sells a course.", "scam"),
            "drop needs Reason: <code> - <one sentence>",
        ),
        (
            report_text().replace("Project: the agent fleet\n", ""),
            "accept needs a Project: line",
        ),
        (
            report_text().replace("medium - automatic", "fairly sure - automatic"),
            "Confidence must start with one of",
        ),
        (report_text().replace("Gist: ", "Summary: "), "the header has no Gist: line"),
    ],
    ids=[
        "category",
        "sections",
        "tests",
        "accept-failed-test",
        "park-past",
        "drop-reason",
        "accept-project",
        "confidence",
        "gist",
    ],
)
def test_a_report_off_the_rubric_is_rejected_and_the_job_waits(box, text, why):
    jid = _judged(box)
    r = box.cli("report", jid, stdin=text)
    assert r.returncode == 3 and why in r.stderr, r.stderr
    assert box.job(jid)["status"] == "running"
    assert box.index() == []
    r = box.cli("report", jid, stdin=report_text())  # fixed: filed
    assert r.returncode == 0, r.stderr


def test_a_long_report_is_cut_to_the_cap_with_a_marker(box):
    jid = _judged(box)
    claims = "\n".join(f'"Claim {i}" - Holds up. ' + "why " * 30 for i in range(40))
    r = box.cli("report", jid, stdin=report_text(claims=claims))
    assert r.returncode == 0, r.stderr
    text = (box.root / f"reports/ig-{CODE}.v3.md").read_text()
    rubric = video.load_rubric()
    # The cap is the judge's body; the stamp adds three header lines.
    assert len(text.splitlines()) <= rubric.list("max-lines") + 3
    assert video.CUT in text
    assert "Next step: try it on one job" in text  # the verdict is never cut


def test_a_report_for_a_job_not_waiting_for_one_is_refused(box):
    r = box.cli("report", "video-yt-NoSuchJob12", stdin=report_text())
    assert r.returncode == 2 and "not waiting for a report" in r.stderr


# --- the index ------------------------------------------------------------------------


def test_the_index_is_appended_once_per_source_and_rubric_version(box):
    root = video.private_root()
    row = {c: "x" for c in video.INDEX_COLUMNS} | {"source": YT, "rubric": "v1"}
    assert video.append_row(root, row) is True
    assert video.append_row(root, row | {"verdict": "drop"}) is False
    assert video.append_row(root, row | {"rubric": "v2"}) is True
    text = (root / "index.md").read_text()
    assert text.count("# Intake index") == 1
    assert [r["rubric"] for r in video.read_index(root)] == ["v1", "v2"]
    assert stat.S_IMODE((root / "index.md").stat().st_mode) == 0o600


def test_index_cells_cannot_break_the_table(box):
    root = video.private_root()
    row = {c: "a | b\nc" for c in video.INDEX_COLUMNS}
    video.append_row(root, row)
    assert video.read_index(root)[0]["title"] == "a / b c"


def test_the_index_columns_carry_the_intake_fields(box):
    jid = _judged(box, origin="firstmate")
    box.cli("report", jid, stdin=report_text("drop", category="finance"))
    row = box.index()[0]
    assert row == {
        "date": time.strftime("%Y-%m-%d", time.gmtime()),
        "kind": "video",
        "origin": "firstmate",
        "category": "finance",
        "source": REEL,
        "title": "Video by some_account",
        "gist": "a pitch that agents halve costs",
        "verdict": "drop",
        "reason": "paid-funnel",
        "project": "",
        "recheck": "",
        "next step or why": "It sells a course.",
        "rubric": "v3",
        "report": f"reports/ig-{CODE}.v3.md",
    }
    assert box.cli("index").stdout.startswith("# Intake index")


def test_a_video_judged_before_answers_with_the_earlier_verdict(box):
    jid = _judged(box, YT, origin="scout", YT_CAPTIONS="yes")
    box.cli("report", jid, stdin=report_text("drop"))
    box.log.write_text("")
    r = box.cli(f"https://youtu.be/{VID}?si=x")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert r.stdout.strip().splitlines() == [
        "A talk about agents",
        f"video · dev-tooling · already judged {today}: drop (paid-funnel): It sells a course.",
        f"report: {box.root}/reports/yt-{VID}.v3.md",
    ]
    assert box.job(jid)["status"] == "done"  # not queued again
    assert box.log.read_text() == "" and len(box.index()) == 1


def test_the_same_video_from_another_source_is_matched_by_title(box):
    jid = _judged(box, YT, YT_CAPTIONS="yes")
    box.cli("report", jid, stdin=report_text())
    # The same talk re-posted as a reel: fetched, matched by title, not judged.
    r = box.run(REEL, STUB_TITLE="A talk about AGENTS!")
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 3 and "already judged" in lines[1]
    rows = box.index()
    assert len(rows) == 2
    assert (rows[1]["source"], rows[1]["verdict"], rows[1]["reason"]) == (
        REEL,
        "drop",
        "duplicate",
    )
    assert rows[1]["report"] == rows[0]["report"]
    assert box.job(f"video-ig-{CODE}")["status"] == "done"


def test_a_generic_title_is_never_taken_for_a_repeat(box):
    jid = _judged(box)
    box.cli("report", jid, stdin=report_text())
    other = "https://www.instagram.com/reel/OtherCode1/"
    r = box.run(other)
    assert r.stdout.startswith(f"Video: {other}\n")


# --- ideas --------------------------------------------------------------------------

IDEA_SOURCE = "https://github.com/example/tool"


def idea_text(
    verdict: str = "accept",
    *,
    claim: str = "Claim: one tool replaces two scripts",
    source: str = f"Source: {IDEA_SOURCE}",
    title: str = "Title: One tool for both scripts",
    **kw,
) -> str:
    """A judged idea's report: the video template's, with its claim and source."""
    lines = report_text(verdict, **kw).splitlines()
    lines[0] = title
    return "\n".join([*lines[:3], claim, source, *lines[3:]]) + "\n"


def test_an_idea_is_filed_directly_as_its_own_kind(box):
    r = box.cli("idea", stdin=idea_text())
    assert r.returncode == 0, r.stderr
    rows = box.index()
    assert len(rows) == 1
    row = rows[0]
    assert (row["kind"], row["origin"], row["source"], row["verdict"]) == (
        "idea",
        "scout",
        IDEA_SOURCE,
        "accept",
    )
    assert row["rubric"] == f"v{video.load_rubric().version}"
    saved = box.root / row["report"]
    assert saved.parent == box.root / "reports/ideas"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert saved.read_text().splitlines()[:6] == [
        "Title: One tool for both scripts",
        "Kind: idea | Category: dev-tooling | Origin: scout",
        f"Source: {IDEA_SOURCE}",
        f"Evaluated: {today} | Rubric: video-eval {row['rubric']} (claim-check)",
        "Gist: a pitch that agents halve costs",
        "Claim: one tool replaces two scripts",
    ]
    assert r.stdout.strip().splitlines() == [
        "One tool for both scripts",
        "idea · dev-tooling · accept -> the agent fleet: try it on one job",
        f"report: {saved}",
    ]
    assert not box.queue.exists() or not list(box.queue.glob("*.json"))
    assert box.log.read_text() == ""  # nothing fetched


def test_an_idea_is_always_filed_as_the_scout_and_takes_no_origin(box):
    assert box.cli("idea", stdin=idea_text()).returncode == 0
    assert box.index()[0]["origin"] == "scout"
    other = idea_text(source="Source: https://example.org/x")
    r = box.cli("idea", "--origin", "kit", stdin=other)
    assert r.returncode == 2
    assert len(box.index()) == 1


@pytest.mark.parametrize(
    "text,why",
    [
        (idea_text(claim=""), "no Claim: line"),
        (idea_text(source=""), "no Source: line"),
        (idea_text(source="Source: the vendor docs"), "http(s) link"),
        (
            idea_text(source=f"Source: {IDEA_SOURCE}/{'a' * 140}"),
            "at most 160 characters",
        ),
        (
            idea_text(source=f"Source: {IDEA_SOURCE}/{'a' * 300}"),
            "at most 160 characters",
        ),
        (idea_text(category="hobby"), "Category 'hobby' is not one of"),
        (
            idea_text("drop", extra="").replace("paid-funnel", "boring"),
            "drop needs Reason",
        ),
        (
            idea_text(
                tests="evidence fail, cost pass, goal-trace pass, no-overengineering pass"
            ),
            "accept needs every test passed",
        ),
    ],
)
def test_an_idea_off_the_rubric_is_rejected_and_nothing_is_filed(box, text, why):
    r = box.cli("idea", stdin=text)
    assert r.returncode == 3 and why in r.stderr, r.stderr
    assert box.index() == []


def test_an_idea_source_that_fills_an_index_cell_is_kept_whole(box):
    long = f"{IDEA_SOURCE}/" + "a" * (160 - len(IDEA_SOURCE) - 1)
    r = box.cli("idea", stdin=idea_text(source=f"Source: {long}"))
    assert r.returncode == 0, r.stderr
    assert box.index()[0]["source"] == long


def test_the_same_idea_filed_again_answers_with_its_earlier_verdict(box):
    box.cli("idea", stdin=idea_text("park"))
    again = idea_text("drop", source=f"Source: {IDEA_SOURCE}/?utm_source=x")
    r = box.cli("idea", stdin=again)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert "already judged" in lines[1] and f"park until {TOMORROW}" in lines[1]
    assert len(box.index()) == 1


def test_an_idea_with_an_earlier_items_title_is_its_duplicate(box):
    box.cli("idea", stdin=idea_text())
    other = "https://example.org/the-same-tool"
    r = box.cli("idea", stdin=idea_text(source=f"Source: {other}"))
    assert "already judged" in r.stdout
    rows = box.index()
    assert [(x["kind"], x["source"], x["verdict"], x["reason"]) for x in rows] == [
        ("idea", IDEA_SOURCE, "accept", ""),
        ("idea", other, "drop", "duplicate"),
    ]
    assert rows[1]["report"] == rows[0]["report"]


def test_an_idea_whose_source_was_judged_as_a_video_is_not_filed_again(box):
    jid = _judged(box, YT, YT_CAPTIONS="yes")
    box.cli("report", jid, stdin=report_text("drop"))
    r = box.cli("idea", stdin=idea_text(source=f"Source: https://youtu.be/{VID}"))
    assert "video · dev-tooling · already judged" in r.stdout
    assert len(box.index()) == 1


def test_an_idea_needs_a_rubric_that_has_the_idea_kind(box, tmp_path):
    old = tmp_path / "rubric.md"
    old.write_text(
        (SKILL / "rubric.md")
        .read_text()
        .replace("kinds: video, idea, link\n", "kinds: video\n")
    )
    r = box.cli("idea", stdin=idea_text(), VIDEO_RUBRIC=str(old))
    assert r.returncode == 1 and "has no idea kind" in r.stderr
    assert box.index() == []


# --- links and articles -------------------------------------------------------------

ARTICLE = "https://example.org/blog/agents-at-work"


def link_text(
    verdict: str = "accept",
    *,
    source: str = f"Source: {ARTICLE}",
    title: str = "Title: Agents at work, a field report",
    **kw,
) -> str:
    """A judged link's report: the video template's, with its own link."""
    lines = report_text(verdict, **kw).splitlines()
    lines[0] = title
    return "\n".join([*lines[:3], source, *lines[3:]]) + "\n"


def test_a_link_is_filed_directly_as_its_own_kind(box):
    r = box.cli("link", "--origin", "firstmate", stdin=link_text())
    assert r.returncode == 0, r.stderr
    rows = box.index()
    assert len(rows) == 1
    row = rows[0]
    assert (row["kind"], row["origin"], row["source"], row["verdict"]) == (
        "link",
        "firstmate",
        ARTICLE,
        "accept",
    )
    assert row["rubric"] == f"v{video.load_rubric().version}"
    saved = box.root / row["report"]
    assert saved.parent == box.root / "reports/links"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert saved.read_text().splitlines()[:6] == [
        "Title: Agents at work, a field report",
        "Kind: link | Category: dev-tooling | Origin: firstmate",
        f"Source: {ARTICLE}",
        f"Evaluated: {today} | Rubric: video-eval {row['rubric']} (standard)",
        "Gist: a pitch that agents halve costs",
        "",
    ]
    assert r.stdout.strip().splitlines() == [
        "Agents at work, a field report",
        "link · dev-tooling · accept -> the agent fleet: try it on one job",
        f"report: {saved}",
    ]
    assert not box.queue.exists() or not list(box.queue.glob("*.json"))
    assert box.log.read_text() == ""  # nothing fetched


def test_a_link_asked_to_be_checked_is_filed_in_claim_check(box):
    r = box.cli("link", "--origin", "kit", "--check", stdin=link_text())
    assert r.returncode == 0, r.stderr
    row = box.index()[0]
    assert row["origin"] == "kit"
    assert f"Rubric: video-eval {row['rubric']} (claim-check)" in (
        (box.root / row["report"]).read_text()
    )


def test_a_link_from_an_unknown_origin_is_refused(box):
    r = box.cli("link", "--origin", "someone", stdin=link_text())
    assert r.returncode == 1 and "unknown origin" in r.stderr
    assert box.index() == []


@pytest.mark.parametrize(
    "text,why",
    [
        (link_text(source=""), "no Source: line"),
        (link_text(source="Source: the blog post"), "http(s) link of the item"),
        (link_text(source=f"Source: {YT}"), "is a YouTube or Instagram video"),
        (link_text(source=f"Source: {REEL}"), "is a YouTube or Instagram video"),
        (
            link_text(source=f"Source: {ARTICLE}/{'a' * 140}"),
            "at most 160 characters",
        ),
        (link_text(category="hobby"), "Category 'hobby' is not one of"),
        (
            link_text(
                tests="evidence fail, cost pass, goal-trace pass, no-overengineering pass"
            ),
            "accept needs every test passed",
        ),
    ],
)
def test_a_link_off_the_rubric_is_rejected_and_nothing_is_filed(box, text, why):
    r = box.cli("link", "--origin", "firstmate", stdin=text)
    assert r.returncode == 3 and why in r.stderr, r.stderr
    assert box.index() == []


def test_the_same_link_filed_again_answers_with_its_earlier_verdict(box):
    box.cli("link", "--origin", "firstmate", stdin=link_text("park"))
    again = link_text("drop", source=f"Source: {ARTICLE}/?utm_source=x#top")
    r = box.cli("link", "--origin", "kit", stdin=again)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert "link · dev-tooling · already judged" in lines[1]
    assert f"park until {TOMORROW}" in lines[1]
    assert len(box.index()) == 1


def test_a_link_with_an_earlier_ideas_title_is_its_duplicate(box):
    box.cli("idea", stdin=idea_text())
    other = "https://example.org/one-tool"
    title = "Title: One tool for both scripts"
    r = box.cli(
        "link",
        "--origin",
        "firstmate",
        stdin=link_text(source=f"Source: {other}", title=title),
    )
    assert "idea · dev-tooling · already judged" in r.stdout
    rows = box.index()
    assert [(x["kind"], x["source"], x["verdict"], x["reason"]) for x in rows] == [
        ("idea", IDEA_SOURCE, "accept", ""),
        ("link", other, "drop", "duplicate"),
    ]
    assert rows[1]["report"] == rows[0]["report"]


def test_a_link_needs_a_rubric_that_has_the_link_kind(box, tmp_path):
    old = tmp_path / "rubric.md"
    old.write_text(
        (SKILL / "rubric.md")
        .read_text()
        .replace("kinds: video, idea, link\n", "kinds: video, idea\n")
    )
    r = box.cli(
        "link", "--origin", "firstmate", stdin=link_text(), VIDEO_RUBRIC=str(old)
    )
    assert r.returncode == 1 and "has no link kind" in r.stderr
    assert box.index() == []


# --- files and backfill ---------------------------------------------------------------


def test_a_backfilled_reel_is_the_reel_and_is_not_downloaded(box):
    r = box.cli("ingest", f"reel-{CODE}.mp4", "--origin", "firstmate", stdin=b"video")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith(
        f"video queued - job video-ig-{CODE}, judged on the next drain turn"
    )
    job = box.job(f"video-ig-{CODE}")
    assert job["probe"] == "" and job["payload"]["mode"] == "claim-check"
    out = box.start(f"video-ig-{CODE}").stdout
    assert out.startswith(f"Video: {REEL}\n") and "Lemon water" in out
    assert box.calls("yt-dlp") == []
    assert list((box.root / "inbox").iterdir()) == []  # used, then removed


def test_a_slow_transcription_outlives_the_turn_and_still_reports_back(box):
    # The stuck reels this fixes: the drain turn ran the handler in the
    # agent's exec tool, whose timeout killed it mid-transcription, leaving
    # only the audio, and the job never reported back. Now the gate's own
    # command starts the run, and killing that command's process group, as
    # the tool's timeout does, leaves the run going.
    jid = f"video-ig-{CODE}"
    box.cli("ingest", f"reel-{CODE}.mp4", "--origin", "firstmate", stdin=b"video")
    next_ = shlex.join(
        [sys.executable, str(SKILL.parent / "job-queue/jobq.py"), "next"]
    )
    gate = subprocess.Popen(
        ["sh", "-c", f"{next_}; sleep 60"],
        env={**box.env, "WHISPER_SLEEP": "2"},
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert gate.stdout.readline() == "JOBQ idle\n"  # no turn yet
    os.killpg(gate.pid, signal.SIGKILL)
    gate.wait(timeout=10)
    gate.stdout.close()
    assert box.job(jid)["status"] == "running"
    deadline = time.monotonic() + 30
    while (
        said := subprocess.run(
            shlex.split(next_), env=box.env, capture_output=True, text=True, check=True
        ).stdout
    ).startswith("JOBQ idle"):
        assert time.monotonic() < deadline, box.job(jid)
        time.sleep(0.2)
    lines = said.splitlines()
    assert lines[:2] == [
        "JOBQ ready",
        f"Job {jid} has run (attempt 1 of 3, queued {lines[1].split('queued ')[1]}",
    ]
    turn = shlex.split(lines[2].removeprefix("Run exactly: "))
    assert turn[-2:] == ["output", jid]
    out = subprocess.run(turn, env=box.env, capture_output=True, text=True).stdout
    assert out.startswith(f"Video: {REEL}\n") and "Lemon water" in out
    assert list(box.root.glob("work-*")) == []
    assert box.cli("report", jid, stdin=report_text()).returncode == 0
    assert box.job(jid)["status"] == "done"


def test_a_failed_backfill_sent_again_is_queued_afresh(box):
    # The runbook's re-run of a job that gave up.
    jid = f"video-ig-{CODE}"
    ingest = ("ingest", f"reel-{CODE}.mp4", "--origin", "firstmate")
    box.cli(*ingest, stdin=b"video")
    box.start(jid, WHISPER_MODE="fail", JOBQ_MAX_ATTEMPTS="1")
    assert box.job(jid)["status"] == "failed"
    assert box.cli(*ingest, stdin=b"video").stdout.startswith("video queued")
    assert (box.job(jid)["status"], box.job(jid)["attempts"]) == ("queued", 0)
    assert box.start(jid).stdout.startswith(f"Video: {REEL}\n")


def test_a_backfilled_vtt_is_the_youtube_video_read_from_its_captions(box):
    vtt = (
        "WEBVTT\nKind: captions\n\n00:00.000 --> 00:02.000\nAgents <c>cut</c> costs.\n"
    )
    r = box.cli(
        "ingest", f"yt-{VID}.en.vtt", "--origin", "firstmate", stdin=vtt.encode()
    )
    assert r.returncode == 0, r.stderr
    out = box.start(f"video-yt-{VID}").stdout
    assert out.startswith(f"Video: {YT}\n")
    assert "Transcript: captions" in out and "Language: en" in out
    assert "Agents cut costs." in out and "Mode: standard" in out
    assert box.calls("yt-dlp") == [] and box.calls("whisper") == []


def test_another_file_is_its_own_item_keyed_by_its_content(box, tmp_path):
    clip = tmp_path / "my clip.mp4"
    clip.write_bytes(b"some video")
    r = box.cli(str(clip))  # a file kit received
    assert r.returncode == 0, r.stderr
    jid = r.stdout.split("job ", 1)[1].split(",")[0]
    assert jid.startswith("video-file-") and len(jid) == len("video-file-") + 16
    assert clip.read_bytes() == b"some video"  # the owner's file is left alone
    out = box.start(jid).stdout
    assert out.startswith("Video: file:") and out.splitlines()[0].endswith(
        "/my clip.mp4"
    )


@pytest.mark.parametrize(
    "name,data,why",
    [
        ("notes.txt", b"x", "not a media or .vtt file"),
        ("clip.mp4", b"", "is empty"),
        ("captions.vtt", b"WEBVTT", "must be named yt-<video id>"),
    ],
)
def test_ingest_refuses_what_it_cannot_evaluate(box, name, data, why):
    r = box.cli("ingest", name, "--origin", "firstmate", stdin=data)
    assert r.returncode == 1 and why in r.stderr


def test_a_backfill_of_a_video_judged_before_queues_nothing(box):
    jid = _judged(box)
    box.cli("report", jid, stdin=report_text())
    r = box.cli("ingest", f"reel-{CODE}.mp4", "--origin", "firstmate", stdin=b"v")
    assert "already judged" in r.stdout
    assert list((box.root / "inbox").iterdir()) == []


# --- the drop door -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,ident",
    [
        (YT, VID),
        (f"  https://youtu.be/{VID}?si=x\n", VID),
        (f"https://www.youtube.com/shorts/{VID}", VID),
        (f"HTTPS://M.YOUTUBE.COM/watch?v={VID}", VID),
        (f"https://youtu.be:443/{VID}", VID),
        (REEL, CODE),
        (f"https://instagram.com/reel/{CODE}/?igsh=MWZ4eHh4", CODE),
        ("https://www.instagram.com/share/reel/BAbcdEFgh/", "BAbcdEFgh"),
    ],
)
def test_the_door_takes_a_message_that_is_one_bare_link(text, ident):
    assert video.door_link(text).ident == ident


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        f"{REEL} is this true?",
        f"is this true? {REEL}",
        f"{YT} {REEL}",
        f"{YT}\n{REEL}",
        f"{YT}\tx",
        f"youtube.com/watch?v={VID}",
        f"www.youtube.com/watch?v={VID}",
        f"ftp://youtu.be/{VID}",
        f"https://user@youtu.be/{VID}",
        f"https://user:pw@www.youtube.com/watch?v={VID}",
        f"https://evil.com\\@youtu.be/{VID}",
        f"https://youtu.be:8080/{VID}",
        f"https://youtu.be:99999/{VID}",
        f"https://youtu.be.evil.com/{VID}",
        f"https://youtu.be/{VID}​",
        f"https://youtu.be/{VID}?x=" + "a" * 2100,
        "https://vimeo.com/123456",
        "https://www.youtube.com/@some_channel",
        "--check",
        "/tmp/clip.mp4",
    ],
)
def test_the_door_leaves_anything_else_to_the_model(text):
    assert video.door_link(text) is None


def _q(item, **kw):
    return video.Queued(item, "video-x", **kw)


@pytest.mark.parametrize(
    "q,place,line",
    [
        (
            _q(video.Item("youtube", VID), added=True),
            (1, 3),
            f"video queued: youtube video {VID} - position 1 of 3; "
            "the verdict comes here once judged",
        ),
        (
            _q(video.Item("instagram", CODE), added=False),
            (2, 2),
            f"video already queued: instagram reel {CODE} - position 2 of 2; "
            "the verdict comes here once judged",
        ),
        (
            _q(video.Item("instagram", "BAbcdEFgh", True), added=True),
            None,
            "video queued: instagram share link BAbcdEFgh - in the queue; "
            "the verdict comes here once judged",
        ),
        (
            _q(video.Item("file", "ab12", name="a\nb" + "x" * 80 + ".mp4"), added=True),
            (1, 1),
            "video queued: video file a?b" + "x" * 57 + " - position 1 of 1; "
            "the verdict comes here once judged",
        ),
    ],
)
def test_the_receipt_is_one_line_naming_the_item_and_its_place(q, place, line):
    assert video.receipt(q, place) == line


def test_the_receipt_of_a_video_judged_before_is_its_verdict():
    q = video.Queued(video.Item("youtube", VID), repeat=("title", "verdict", "report"))
    assert video.receipt(q, (1, 1)) == "title\nverdict\nreport"


def test_the_door_queues_a_bare_link_as_kit_and_answers_its_place(box):
    r = box.cli("door", f" https://youtu.be/{VID}?si=x ")
    assert r.returncode == 0, r.stderr
    assert r.stdout == (
        f"video queued: youtube video {VID} - position 1 of 1; "
        "the verdict comes here once judged\n"
    )
    job = box.job(f"video-yt-{VID}")
    assert job["payload"]["origin"] == "kit" and job["payload"]["mode"] == "standard"
    r = box.cli("door", REEL)
    assert f"instagram reel {CODE} - position 2 of 2" in r.stdout
    r = box.cli("door", YT)
    assert r.stdout.startswith(
        f"video already queued: youtube video {VID} - position 1 of 2"
    )


@pytest.mark.parametrize("text", [f"{REEL} is this true?", "--check", "hello", ""])
def test_the_door_refuses_what_is_not_one_bare_link_and_queues_nothing(box, text):
    r = box.cli("door", text)
    assert r.returncode == 2 and r.stdout == "" and "not for the door" in r.stderr
    assert not box.queue.exists()


def test_the_door_answers_a_video_judged_before_with_its_verdict(box):
    jid = _judged(box, YT, origin="scout", YT_CAPTIONS="yes")
    box.cli("report", jid, stdin=report_text("drop"))
    r = box.cli("door", f"https://youtu.be/{VID}")
    assert r.returncode == 0 and "already judged" in r.stdout
    assert box.job(jid)["status"] == "done"


def test_the_door_queues_a_saved_video_file_and_leaves_it_in_place(box, tmp_path):
    clip = tmp_path / "0d3c-uuid.mp4"
    clip.write_bytes(b"some video")
    r = box.cli("door-file", str(clip))
    assert r.returncode == 0, r.stderr
    assert r.stdout == (
        "video queued: video file 0d3c-uuid.mp4 - position 1 of 1; "
        "the verdict comes here once judged\n"
    )
    assert clip.read_bytes() == b"some video"
    (kept,) = (box.root / "inbox").iterdir()
    assert kept.read_bytes() == b"some video"
    assert box.cli("door-file", str(clip)).stdout.startswith("video already queued")


def test_the_door_refuses_a_link_a_directory_another_type_or_a_missing_file(
    box, tmp_path
):
    real = tmp_path / "real.mp4"
    real.write_bytes(b"v")
    (tmp_path / "link.mp4").symlink_to(real)
    (tmp_path / "dir.mp4").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    for name in ("link.mp4", "dir.mp4", "notes.txt", "missing.mp4"):
        r = box.cli("door-file", str(tmp_path / name))
        assert r.returncode == 2 and "not for the door" in r.stderr, name
    assert not box.queue.exists()


def test_the_door_keeps_the_ingest_limits(box, tmp_path, monkeypatch):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    r = box.cli("door-file", str(empty))
    assert r.returncode == 1 and r.stderr.startswith("video failed:")
    big = tmp_path / "big.mp4"
    big.write_bytes(b"12345")
    monkeypatch.setattr(video, "MAX_INGEST_BYTES", 4)
    with pytest.raises(ValueError, match="larger than"):
        video.door_file(big)
    assert not (box.root / "inbox").exists() or not any((box.root / "inbox").iterdir())


# --- retention -------------------------------------------------------------------------


def _age(path: Path, days: float, now: float) -> None:
    t = now - days * 86400
    os.utime(path, (t, t))


def test_prune_deletes_old_transcripts_and_waiting_files_never_reports(tmp_path):
    now = time.time()
    root = tmp_path
    for sub in ("transcripts", "inbox", "reports"):
        (root / sub).mkdir()
    for rel, days in [
        ("transcripts/old.txt", 15),
        ("transcripts/new.txt", 13),
        ("inbox/old.mp4", 15),
        ("reports/old.v1.md", 400),
        ("index.md", 400),
    ]:
        (root / rel).write_text("x")
        _age(root / rel, days, now)
    (root / "work-dead").mkdir()
    _age(root / "work-dead", 2, now)
    (root / "work-live").mkdir()
    video.prune(root, 14, now)
    left = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert left == [
        "inbox",
        "index.md",
        "reports",
        "reports/old.v1.md",
        "transcripts",
        "transcripts/new.txt",
        "work-live",
    ]


# --- the command line and the skill file ------------------------------------------------


@pytest.mark.parametrize("arg", ["hello", "https://vimeo.com/123456"])
def test_anything_but_a_link_or_a_file_is_a_usage_error(box, arg):
    r = box.cli(arg)
    assert r.returncode == 2 and "not a YouTube or Instagram video link" in r.stderr
    assert not box.queue.exists()


@pytest.mark.parametrize("args", [[], ["run"], ["run", "a", "b"], [REEL, REEL]])
def test_wrong_arguments_are_a_usage_error(box, args):
    r = box.cli(*args)
    assert r.returncode == 2 and "usage" in r.stderr


def test_skill_frontmatter_parses_into_the_name_and_bins_openclaw_reads():
    meta = yaml.safe_load((SKILL / "SKILL.md").read_text().split("---")[1])
    assert meta["name"] == "video-eval"
    assert meta["metadata"]["openclaw"]["requires"]["bins"] == [
        "python3",
        "ffmpeg",
        "whisper",
    ]


def test_the_rubric_carries_the_intake_lists_and_the_four_tests():
    rubric = video.load_rubric()
    assert rubric.version == 3
    assert rubric.list("kinds") == ["video", "idea", "link"]
    assert rubric.list("categories") == [
        "persona",
        "channels",
        "finance",
        "dev-tooling",
        "home",
        "other",
    ]
    assert rubric.list("verdicts") == ["accept", "park", "drop"]
    assert rubric.list("drop-reasons") == [
        "duplicate",
        "already-have",
        "no-evidence",
        "policy-risk",
        "off-goal",
        "paid-funnel",
        "other",
    ]
    assert rubric.list("tests") == [
        "evidence",
        "cost",
        "goal-trace",
        "no-overengineering",
    ]


def test_the_rubric_template_is_a_shape_the_validator_accepts():
    rubric = video.load_rubric()
    template = rubric.path.read_text().split("```")[1]
    headings = [ln for ln in template.splitlines() if ln.startswith("## ")]
    filled = report_text(sections=[h[3:] for h in headings])
    report, errors = video.parse_report(filled, rubric, datetime.date.today())
    assert errors == []
    assert [n for n, _ in report.sections] == rubric.list("sections")


def test_the_judge_reads_the_goals_file_when_there_is_one(box):
    (video.private_root() / "goals.md").write_text("1. Ship the agent fleet\n")
    out = box.run(REEL).stdout
    assert "Goals:\n1. Ship the agent fleet\n" in out


# --- the ensure script ---------------------------------------------------------------------


DOCKER = """#!/bin/sh
case "$1" in
  ps) [ -n "$GATEWAY" ] && echo "$GATEWAY" ;;
  exec)
    case "$*" in
      *node*) printf %s "$ALLOWLIST" ;;
      *) printf 'tool: ffmpeg present\\ntool: whisper present\\n' ;;
    esac ;;
esac
exit 0
"""


def _ensure(tmp_path, *, workspace=True, gateway="", allowlist="yes"):
    state = tmp_path / "state"
    ws = state / "agents/kit/workspace"
    if workspace:
        ws.mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "dbin"
    bindir.mkdir(exist_ok=True)
    _stub(bindir / "docker", DOCKER)
    r = subprocess.run(
        ["bash", str(ENSURE)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "OPENCLAW_CONFIG_DIR": str(state),
            "GATEWAY": gateway,
            "ALLOWLIST": allowlist,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return r, ws / "skills"


def test_ensure_installs_the_skill_into_kits_workspace(tmp_path):
    r, skills = _ensure(tmp_path)
    assert r.returncode == 0, r.stderr
    for name in ("SKILL.md", "video.py", "rubric.md"):
        assert (skills / "video-eval" / name).read_bytes() == (
            SKILL / name
        ).read_bytes()
    assert os.access(skills / "video-eval/video.py", os.X_OK)
    assert "no running openclaw-gateway container" in r.stdout


def test_ensure_retires_the_reel_claim_check_skill(tmp_path):
    old = tmp_path / "state/agents/kit/workspace/skills/reel-claim-check"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("old")
    (old / "reel.py").write_text("old")
    r, _ = _ensure(tmp_path)
    assert r.returncode == 0 and not old.exists()
    assert "retired skill removed" in r.stdout


def test_ensure_leaves_a_retired_dir_with_other_files_alone(tmp_path):
    old = tmp_path / "state/agents/kit/workspace/skills/reel-claim-check"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("old")
    (old / "notes.md").write_text("mine")
    r, _ = _ensure(tmp_path)
    assert r.returncode == 0 and (old / "notes.md").exists()
    assert not (old / "SKILL.md").exists()
    assert "left in place" in r.stdout


def test_ensure_skips_a_box_without_kit_and_succeeds(tmp_path):
    r, skills = _ensure(tmp_path, workspace=False)
    assert r.returncode == 0 and "no workspace for agent 'kit'" in r.stdout
    assert not skills.exists()


@pytest.mark.parametrize(
    "allowlist,said",
    [
        ("yes", "is on agent 'kit''s skills list"),
        ("unfiltered", "has no skills list; the skill is visible"),
        ("no", "NOT on agent 'kit''s skills list"),
    ],
)
def test_ensure_reports_kits_allowlist_without_touching_it(tmp_path, allowlist, said):
    r, _ = _ensure(tmp_path, gateway="abc123", allowlist=allowlist)
    assert r.returncode == 0, r.stderr
    assert said in r.stdout and "tool: whisper present" in r.stdout


# --- the host wrapper ----------------------------------------------------------------------


WRAPPER_DOCKER = r"""#!/bin/sh
echo "docker $*" >> "$DOCKER_LOG"
case "$1" in
  ps) echo gw1 ;;
  exec)
    case "$*" in
      *" ingest "*) n=$(wc -c); echo "got $n bytes" >> "$DOCKER_LOG"; echo "video queued - job video-x" ;;
      *" idea"*) n=$(wc -c); echo "got $n bytes" >> "$DOCKER_LOG"; echo "report: /home/node/.life-state/intake/reports/ideas/idea-x.v2.md" ;;
      *" link "*) n=$(wc -c); echo "got $n bytes" >> "$DOCKER_LOG"; echo "report: /home/node/.life-state/intake/reports/links/link-x.v3.md" ;;
      *" status "*) printf 'done\nt\nl\nreport: /home/node/.life-state/intake/reports/x.v1.md\n' ;;
      *) echo "ran" ;;
    esac ;;
esac
"""


def _wrapper(tmp_path, *args, stdin=None, **extra):
    log = tmp_path / "docker.log"
    log.write_text("")
    docker = _stub(tmp_path / "docker", WRAPPER_DOCKER)
    r = subprocess.run(
        ["bash", str(WRAPPER), *args],
        env={**os.environ, "DOCKER": str(docker), "DOCKER_LOG": str(log), **extra},
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    return r, log.read_text().splitlines()


SCRIPT = "/home/node/.openclaw/agents/kit/workspace/skills/video-eval/video.py"


def test_the_wrapper_enqueues_as_firstmate(tmp_path):
    r, calls = _wrapper(tmp_path, "enqueue", YT)
    assert r.returncode == 0, r.stderr
    assert (
        calls[-1] == f"docker exec gw1 python3 {SCRIPT} enqueue {YT} --origin firstmate"
    )


def test_the_wrapper_backfills_files_without_downloading_them(tmp_path):
    clip = tmp_path / f"reel-{CODE}.mp4"
    clip.write_bytes(b"12345")
    r, calls = _wrapper(tmp_path, "backfill", str(clip))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"reel-{CODE}.mp4: video queued - job video-x"
    assert (
        f"docker exec -i gw1 python3 {SCRIPT} ingest reel-{CODE}.mp4 --origin firstmate"
        in calls
    )
    assert "got 5 bytes" in calls


def test_the_wrapper_files_an_idea_from_a_file_or_stdin(tmp_path):
    report = tmp_path / "idea.txt"
    report.write_text("12345")
    for args, stdin in ((["idea", str(report)], None), (["idea"], "123")):
        r, calls = _wrapper(tmp_path, *args, stdin=stdin, VIDEO_EVAL_ORIGIN="kit")
        assert r.returncode == 0, r.stderr
        assert f"docker exec -i gw1 python3 {SCRIPT} idea" in calls
        assert f"got {len(stdin or '12345')} bytes" in calls
        assert r.stdout.strip() == (
            "report: /var/lib/lifekit/intake/reports/ideas/idea-x.v2.md"
        )


def test_the_wrapper_files_a_link_as_its_origin_from_a_file_or_stdin(tmp_path):
    report = tmp_path / "link.txt"
    report.write_text("12345")
    for args, stdin in (
        (["link", str(report)], None),
        (["link", "--check"], "123"),
        (["link", "--check", str(report)], None),
    ):
        r, calls = _wrapper(tmp_path, *args, stdin=stdin)
        assert r.returncode == 0, r.stderr
        check = " --check" if "--check" in args else ""
        assert (
            f"docker exec -i gw1 python3 {SCRIPT} link --origin firstmate{check}"
            in calls
        )
        assert f"got {len(stdin or '12345')} bytes" in calls
        assert r.stdout.strip() == (
            "report: /var/lib/lifekit/intake/reports/links/link-x.v3.md"
        )
    r, calls = _wrapper(tmp_path, "link", str(report), VIDEO_EVAL_ORIGIN="kit")
    assert f"docker exec -i gw1 python3 {SCRIPT} link --origin kit" in calls


def test_the_wrapper_refuses_a_missing_link_report(tmp_path):
    r, _ = _wrapper(tmp_path, "link", str(tmp_path / "nope.txt"))
    assert r.returncode == 1 and "no such file" in r.stderr


def test_the_wrapper_reports_a_missing_file_and_fails(tmp_path):
    r, _ = _wrapper(tmp_path, "backfill", str(tmp_path / "nope.mp4"))
    assert r.returncode == 1 and "no such file" in r.stderr


def test_the_wrapper_prints_host_paths(tmp_path):
    r, _ = _wrapper(tmp_path, "status", "video-x")
    assert (
        r.stdout.splitlines()[-1] == "report: /var/lib/lifekit/intake/reports/x.v1.md"
    )


@pytest.mark.parametrize(
    "args",
    [[], ["bogus"], ["enqueue"], ["report"], ["idea", "a", "b"], ["link", "a", "b"]],
)
def test_the_wrapper_usage(tmp_path, args):
    r, _ = _wrapper(tmp_path, *args)
    assert r.returncode == 2
