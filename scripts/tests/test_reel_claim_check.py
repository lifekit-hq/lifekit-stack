"""The reel-claim-check skill and its ensure script (docs/runbook.md,
"Instagram reel claim check").

yt-dlp, ffmpeg and whisper are replaced by stubs that write the files a real
run would, and Instagram's share-link redirect by a fake opener, so nothing
here touches the network or needs the models. The queue is the real one
(skills/job-queue) in a temporary directory, with the "direct" egress unless a
test sets another.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
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
SKILL = REPO / "skills/reel-claim-check"
ENSURE = REPO / "scripts/ensure-reel-claim-check.sh"

_SPEC = importlib.util.spec_from_file_location("reel", SKILL / "reel.py")
reel = importlib.util.module_from_spec(_SPEC)
sys.modules["reel"] = reel  # dataclasses looks its own module up here
_SPEC.loader.exec_module(reel)

CODE = "DAbcdEFGhij"
LINK = f"https://www.instagram.com/reel/{CODE}/"

# A yt-dlp stand-in: STUB_MODE picks the scenario; argv is logged for the test.
YT_DLP_STUB = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
mode = os.environ["STUB_MODE"]
open(os.environ["STUB_LOG"], "a").write(" ".join(argv) + "\n")
out = Path(argv[argv.index("-o") + 1]).parent
code = argv[-1].rstrip("/").rsplit("/", 1)[-1]
if mode == "login":
    sys.stderr.write("WARNING: [Instagram] x: Instagram API is not granting access\n")
    sys.stderr.write("ERROR: [Instagram] x: Instagram sent an empty media response. Check if this post is accessible in your browser without being logged-in.\n")
    sys.exit(1)
if mode == "gone":
    sys.stderr.write("ERROR: [Instagram] x: HTTP Error 404: Not Found\n")
    sys.exit(1)
if mode == "novideo":
    sys.stderr.write("ERROR: [Instagram] x: There is no video in this post\n")
    sys.exit(1)
if mode == "toolong":
    sys.stdout.write("[download] x does not pass filter (duration<=?900), skipping ..\n")
    sys.exit(0)
if mode == "boom":
    sys.stderr.write("ERROR: Unable to download webpage: <urlopen error timed out>\n")
    sys.exit(1)
(out / (code + ".m4a")).write_bytes(b"audio-bytes")
(out / (code + ".info.json")).write_text(json.dumps({
    "id": code, "uploader": "some_account", "duration": 41.6,
    "description": "Drink lemon water\nit cures everything",
}))
"""

FFMPEG_STUB = r"""#!/bin/sh
[ "$FFMPEG_MODE" = fail ] && exit 1
for a; do out="$a"; done
printf 'RIFF' > "$out"
"""

WHISPER_STUB = r"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
argv = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write("whisper " + " ".join(argv) + "\n")
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


@pytest.fixture
def box(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stubs = {
        "YT_DLP": _stub(bindir / "yt-dlp", YT_DLP_STUB),
        "REEL_FFMPEG": _stub(bindir / "ffmpeg", FFMPEG_STUB),
        "REEL_WHISPER": _stub(bindir / "whisper", WHISPER_STUB),
    }
    root = tmp_path / "private"
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        **os.environ,
        **{k: str(v) for k, v in stubs.items()},
        "REEL_CHECK_DIR": str(root),
        "JOBQ_DIR": str(tmp_path / "queue"),
        "LIFEKIT_EGRESS_PROXY": "direct",
        "STUB_LOG": str(log),
        "STUB_MODE": "ok",
        "WHISPER_TEXT": " Lemon water cures cancer.\nStudies show it. ",
    }
    env.pop("JOBQ_MAX_ATTEMPTS", None)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Box(env, root, log, tmp_path / "queue")


class Box:
    def __init__(self, env, root, log, queue):
        self.env, self.root, self.log, self.queue = env, root, log, queue

    def __iter__(self):  # run, root, log
        return iter((self.check, self.root, self.log))

    def cli(self, *args: str, **extra):
        return subprocess.run(
            [sys.executable, str(SKILL / "reel.py"), *args],
            env={**self.env, **extra},
            capture_output=True,
            text=True,
            check=False,
        )

    def check(self, link: str, **extra):
        """Queue the link, then the drain turn's run of it."""
        r = self.cli(link, **extra)
        if r.returncode != 0 or not r.stdout.startswith("reel "):
            return r
        code = reel.parse_reel(link).code
        return self.cli("run", f"reel-{code}", **extra)

    def job(self, code: str = CODE) -> dict:
        return json.loads((self.queue / f"reel-{code}.json").read_text())


# --- URL parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    "arg,code,share",
    [
        (LINK, CODE, False),
        (f"https://www.instagram.com/reel/{CODE}", CODE, False),
        (
            f"https://instagram.com/reel/{CODE}/?igsh=MWZ4eHh4&utm_source=ig",
            CODE,
            False,
        ),
        (f"http://m.instagram.com/reels/{CODE}/", CODE, False),
        (f"https://www.instagram.com/p/{CODE}/", CODE, False),
        (f"https://www.instagram.com/tv/{CODE}/", CODE, False),
        (f"https://www.instagram.com/some.account/reel/{CODE}/", CODE, False),
        (f"https://www.instagram.com/some_account/p/{CODE}/#x", CODE, False),
        (f"https://instagr.am/reel/{CODE}/", CODE, False),
        (f"instagram.com/reel/{CODE}", CODE, False),
        (f"  {LINK} is this true?  ", CODE, False),
        ("https://www.instagram.com/share/reel/BAbcdEFgh/", "BAbcdEFgh", True),
        ("https://www.instagram.com/share/BAbcdEFgh/?igsh=x", "BAbcdEFgh", True),
    ],
)
def test_parse_reel_accepts_instagram_video_links(arg, code, share):
    found = reel.parse_reel(arg)
    assert found == reel.Reel(code, share)


@pytest.mark.parametrize(
    "arg",
    [
        "",
        "   ",
        "is this true?",
        "https://www.youtube.com/watch?v=bQPU6UJ4iCw",
        "https://www.instagram.com.evil.com/reel/DAbcdEFGhij/",
        "https://evilinstagram.com/reel/DAbcdEFGhij/",
        "https://evil.com/instagram.com/reel/DAbcdEFGhij/",
        "https://instagram.com@evil.com/reel/DAbcdEFGhij/",
        "https://www.instagram.com/",
        "https://www.instagram.com/some_account/",
        "https://www.instagram.com/stories/some_account/3456789012345/",
        "https://www.instagram.com/reels/",
        "https://www.instagram.com/reels/audio/1234567890123/",
        "https://www.instagram.com/reel/short/",
        "ftp://www.instagram.com/reel/DAbcdEFGhij/",
        "javascript:alert(1)",
    ],
)
def test_parse_reel_refuses_everything_else(arg):
    assert reel.parse_reel(arg) is None


def test_the_url_is_rebuilt_from_the_shortcode_alone():
    r = reel.parse_reel(f"https://instagram.com/x/reel/{CODE}/?igsh=1&a=--exec%3Drm")
    assert r.url == LINK
    assert reel.Reel("BAbcdEFgh", True).url.endswith("/share/reel/BAbcdEFgh/")


# --- share links --------------------------------------------------------------


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
        code = 302 if loc else 200
        if code == 200:
            return type("R", (), {"close": lambda self: None})()
        raise urllib.error.HTTPError(req.full_url, code, "Found", headers, None)


def _share(monkeypatch, *locations):
    op = FakeOpener(locations)
    monkeypatch.setattr(reel.urllib.request, "build_opener", lambda *_a: op)
    return op


SHARE = reel.Reel("BAbcdEFgh", True)


def test_share_link_resolves_to_the_reel_it_redirects_to(monkeypatch):
    op = _share(monkeypatch, f"/reel/{CODE}/?igsh=x")
    assert reel.resolve_share(SHARE) == reel.Reel(CODE)
    assert op.asked == [SHARE.url]


def test_share_link_redirected_to_the_login_page_reads_the_target_from_next(
    monkeypatch,
):
    _share(
        monkeypatch,
        f"https://www.instagram.com/accounts/login/?next=%2Freel%2F{CODE}%2F",
    )
    assert reel.resolve_share(SHARE) == reel.Reel(CODE)


def test_share_link_follows_a_second_hop_on_instagram(monkeypatch):
    _share(monkeypatch, "/share/reel/Other12345/", f"/reel/{CODE}/")
    assert reel.resolve_share(SHARE) == reel.Reel(CODE)


@pytest.mark.parametrize(
    "locations",
    [
        ["https://evil.example/reel/DAbcdEFGhij/"],  # leaves instagram
        [""],  # answers 200: nothing to follow
        ["/somewhere/else/"] * 3,  # never reaches a reel
    ],
)
def test_share_link_that_does_not_resolve_is_none(monkeypatch, locations):
    _share(monkeypatch, *locations)
    assert reel.resolve_share(SHARE) is None


def test_an_unreachable_share_link_is_a_failed_run_not_an_answer(monkeypatch):
    _share(monkeypatch, None)
    with pytest.raises(OSError):
        reel.resolve_share(SHARE)


def test_a_share_link_is_resolved_through_the_egress(box, monkeypatch):
    monkeypatch.setenv("LIFEKIT_EGRESS_PROXY", "socks5h://proxy.example:1080")
    q = reel.jobq()
    asked = []

    def refuse(host, port, proxy, timeout):
        asked.append((host, port, proxy))
        raise ConnectionRefusedError("no egress")

    monkeypatch.setattr(q, "open_connection", refuse)
    with pytest.raises(OSError):
        reel.resolve_share(SHARE)
    assert asked == [("www.instagram.com", 443, "socks5h://proxy.example:1080")]


def test_a_share_link_is_resolved_before_yt_dlp_sees_it(box, monkeypatch):
    monkeypatch.setattr(reel, "resolve_share", lambda r: reel.Reel(CODE))
    out, error = reel.check(SHARE)
    assert error == "" and out.startswith(f"Reel: {LINK}")
    assert LINK in box.log.read_text().splitlines()[0]


def test_an_unresolvable_share_link_is_a_normal_answer_without_a_download(
    box, monkeypatch
):
    monkeypatch.setattr(reel, "resolve_share", lambda r: None)
    assert reel.check(SHARE) == (reel.NO_SHARE, "")
    assert box.log.read_text() == ""


def test_a_queued_share_link_answers_with_the_link_the_owner_sent(box, monkeypatch):
    monkeypatch.setattr(reel, "resolve_share", lambda r: None)
    assert reel.enqueue(SHARE).startswith(reel.QUEUED)
    out = reel.run_job(f"reel-{SHARE.code}")
    assert out == f"Reel: {SHARE.url}\n{reel.NO_SHARE}"


# --- the pipeline ---------------------------------------------------------------


def test_a_reel_is_downloaded_transcribed_and_kept_privately(box):
    run, root, log = box
    r = run(f"{LINK}?igsh=abc")
    assert r.returncode == 0, r.stderr
    head, _, text = r.stdout.partition("\n\n")
    assert head.splitlines() == [
        f"Reel: {LINK}",
        "Account: some_account",
        "Duration: 42s",
        "Language: Ukrainian",
        "Caption: Drink lemon water it cures everything",
        "Transcript: automatic speech-to-text, names and numbers can be wrong",
    ]
    assert text.strip() == "Lemon water cures cancer. Studies show it."
    # Only the transcript is kept: no download, no wav, no work directory.
    assert [p.name for p in root.iterdir()] == [f"{CODE}.txt"]
    saved = root / f"{CODE}.txt"
    assert saved.read_text().strip() == r.stdout.strip()
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_the_command_line_carries_only_the_rebuilt_url_and_never_a_login(box):
    run, _, log = box
    run(f"https://instagram.com/x/reel/{CODE}/?igsh=1&a=--exec%3Drm")
    ytdlp = log.read_text().splitlines()[0].split()
    assert ytdlp[-2:] == ["--", LINK]
    assert "--ignore-config" in ytdlp
    assert not [a for a in ytdlp if "cookie" in a or "username" in a or "exec" in a]
    assert "--match-filter" in ytdlp and "duration<=?900" in ytdlp
    whisper = next(
        ln for ln in log.read_text().splitlines() if ln.startswith("whisper")
    )
    assert "--model base" in whisper and "--language" not in whisper


def test_the_whisper_model_and_language_can_be_set(box):
    run, _, log = box
    r = run(LINK, REEL_WHISPER_MODEL="small", REEL_WHISPER_LANG="uk")
    assert r.returncode == 0, r.stderr
    whisper = next(
        ln for ln in log.read_text().splitlines() if ln.startswith("whisper")
    )
    assert "--model small" in whisper and "--language uk" in whisper
    assert "Language: uk" in r.stdout


@pytest.mark.parametrize(
    "mode,line",
    [
        ("login", reel.LOGIN),
        ("gone", reel.GONE),
        ("novideo", reel.NO_VIDEO),
        ("toolong", reel.TOO_LONG),
    ],
)
def test_a_reel_that_cannot_be_fetched_is_a_normal_answer(box, mode, line):
    run, root, log = box
    r = run(LINK, STUB_MODE=mode)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [f"Reel: {LINK}", line]
    assert list(root.iterdir()) == []  # nothing left behind
    assert "whisper" not in log.read_text()


def test_an_unexpected_failure_is_retried_quietly_then_reported_once(box):
    r = box.check(LINK, STUB_MODE="boom", JOBQ_MAX_ATTEMPTS="2")
    assert (r.returncode, r.stdout.strip()) == (0, reel.NO_REPLY)
    job = box.job()
    assert (job["status"], job["attempts"]) == ("queued", 1)
    assert job["last_error"].startswith("ERROR: Unable to download webpage")
    r = box.cli("run", f"reel-{CODE}", STUB_MODE="boom", JOBQ_MAX_ATTEMPTS="2")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        f"Reel: {LINK}",
        f"{reel.FAILED} after 2 tries - ERROR: Unable to download webpage: <urlopen error timed out>",
    ]
    assert box.job()["status"] == "failed"
    assert list(box.root.iterdir()) == []
    r = box.cli("run", f"reel-{CODE}", STUB_MODE="boom", JOBQ_MAX_ATTEMPTS="2")
    assert r.stdout.strip() == reel.NO_REPLY  # reported once, never again


def test_a_failure_while_the_egress_is_down_waits_for_it_uncounted(box):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    r = box.check(
        LINK, STUB_MODE="boom", LIFEKIT_EGRESS_PROXY=f"socks5h://127.0.0.1:{closed}"
    )
    assert (r.returncode, r.stdout.strip()) == (0, reel.NO_REPLY)
    job = box.job()
    assert (job["status"], job["attempts"]) == ("queued", 0)


def test_a_reel_without_speech_is_a_normal_answer_and_keeps_nothing(box):
    run, root, _ = box
    r = run(LINK, WHISPER_TEXT="  \n")
    assert (r.returncode, r.stdout.strip()) == (0, f"Reel: {LINK}\n{reel.NO_SPEECH}")
    assert list(root.iterdir()) == []


def test_a_reel_whose_audio_cannot_be_extracted_has_no_speech(box):
    run, root, _ = box
    r = run(LINK, FFMPEG_MODE="fail")
    assert (r.returncode, r.stdout.strip()) == (0, f"Reel: {LINK}\n{reel.NO_SPEECH}")
    assert list(root.iterdir()) == []


def test_a_whisper_crash_is_a_failed_run(box):
    r = box.check(LINK, WHISPER_MODE="fail", JOBQ_MAX_ATTEMPTS="1")
    assert r.returncode == 0 and "after 1 tries - Traceback: boom" in r.stdout
    assert list(box.root.iterdir()) == []


def test_a_missing_tool_is_a_failed_run_not_a_traceback(box):
    r = box.check(LINK, REEL_FFMPEG="/nonexistent/ffmpeg", JOBQ_MAX_ATTEMPTS="1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[1] == (
        f"{reel.FAILED} after 1 tries - /nonexistent/ffmpeg is not installed"
    )


@pytest.mark.parametrize("arg", ["", "hello", "https://youtu.be/bQPU6UJ4iCw"])
def test_anything_but_a_reel_link_or_a_file_is_a_usage_error(box, arg):
    r = box.cli(arg)
    assert r.returncode == 2 and r.stdout == ""
    assert "not an Instagram reel link or a media file" in r.stderr
    assert box.log.read_text() == ""
    assert not box.root.exists() and not box.queue.exists()


@pytest.mark.parametrize("args", [[], ["run"], ["run", "a", "b"], [LINK, LINK]])
def test_wrong_arguments_are_a_usage_error(box, args):
    r = box.cli(*args)
    assert r.returncode == 2 and "usage" in r.stderr


# --- the queue ----------------------------------------------------------------------


def test_a_link_is_only_queued_and_answered_at_once(box):
    r = box.cli(f"{LINK}?igsh=abc")
    assert (r.returncode, r.stdout.strip()) == (0, f"{reel.QUEUED} (1 waiting)")
    assert box.log.read_text() == ""  # nothing fetched yet
    job = box.job()
    assert job["payload"] == {"code": CODE, "share": False, "url": LINK}
    assert job["label"] == LINK and job["probe"] == "www.instagram.com"
    assert job["owner"] == "reel-claim-check"
    assert job["handler"][1:] == [str((SKILL / "reel.py").resolve()), "run"]
    assert stat.S_IMODE((box.queue / f"reel-{CODE}.json").stat().st_mode) == 0o600


def test_the_same_reel_twice_is_already_queued(box):
    box.cli(LINK)
    r = box.cli(f"https://instagram.com/reels/{CODE}/")
    assert r.stdout.strip() == f"{reel.ALREADY} (1 waiting)"
    r = box.cli("https://www.instagram.com/reel/OtherCode1/")
    assert r.stdout.strip() == f"{reel.QUEUED} (2 waiting)"


def test_a_run_that_is_not_this_jobs_turn_says_nothing(box):
    box.cli(LINK)
    box.cli("https://www.instagram.com/reel/OtherCode1/")
    r = box.cli("run", "reel-OtherCode1")
    assert (r.returncode, r.stdout.strip()) == (0, reel.NO_REPLY)
    assert box.log.read_text() == ""
    assert box.cli("run", "reel-NoSuchJob1").stdout.strip() == reel.NO_REPLY


def test_a_done_run_finishes_the_job_with_its_first_line(box):
    r = box.check(LINK)
    assert r.returncode == 0, r.stderr
    job = box.job()
    assert (job["status"], job["result"]) == ("done", f"Reel: {LINK}")


def test_a_normal_unavailable_answer_names_the_reel_and_finishes_the_job(box):
    r = box.check(LINK, STUB_MODE="login")
    assert r.stdout.splitlines() == [f"Reel: {LINK}", reel.LOGIN]
    assert box.job()["status"] == "done"


def test_the_download_goes_through_the_configured_egress(box):
    r = box.check(LINK, LIFEKIT_EGRESS_PROXY="socks5h://proxy.example:1080")
    assert r.returncode == 0, r.stderr
    ytdlp = box.log.read_text().splitlines()[0].split()
    i = ytdlp.index("--proxy")
    assert ytdlp[i + 1] == "socks5h://proxy.example:1080" and i < ytdlp.index("--")


def test_a_direct_egress_passes_no_proxy(box):
    box.check(LINK)
    assert "--proxy" not in box.log.read_text().splitlines()[0].split()


def test_no_queue_is_exit_1(box, tmp_path, monkeypatch):
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy(SKILL / "reel.py", lone / "reel.py")
    r = subprocess.run(
        [sys.executable, str(lone / "reel.py"), LINK],
        env={**box.env, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 1
    assert (
        r.stderr.strip()
        == "reel failed: the job-queue skill (jobq.py) is not installed"
    )


# --- a media file: the manual fallback ----------------------------------------------------


def test_a_media_file_is_transcribed_at_once_and_left_alone(box, tmp_path):
    video = tmp_path / "my reel.mp4"
    video.write_bytes(b"video")
    r = box.cli(str(video))
    assert r.returncode == 0, r.stderr
    head = r.stdout.partition("\n\n")[0].splitlines()
    assert head[0] == "File: my reel.mp4" and "Language: Ukrainian" in head
    assert video.read_bytes() == b"video"
    assert list(box.root.iterdir()) == []  # nothing kept
    assert not box.queue.exists()  # nothing queued
    assert "yt-dlp" not in box.log.read_text()


def test_a_media_file_without_speech_is_a_normal_answer(box, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    r = box.cli(str(video), WHISPER_TEXT="")
    assert (r.returncode, r.stdout.strip()) == (0, reel.NO_SPEECH)


def test_a_media_file_whisper_crash_is_exit_1(box, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    r = box.cli(str(video), WHISPER_MODE="fail")
    assert r.returncode == 1 and "reel failed: Traceback: boom" in r.stderr


def test_a_long_caption_is_cut():
    out = reel.render(
        f"Reel: {LINK}", {"description": "word " * 600, "duration": 3}, "en", "hi"
    )
    caption = next(ln for ln in out.splitlines() if ln.startswith("Caption: "))
    assert caption.endswith("...") and len(caption) < reel.CAPTION_LIMIT + 20


# --- retention ---------------------------------------------------------------------


def _age(path: Path, days: float, now: float) -> None:
    t = now - days * 86400
    os.utime(path, (t, t))


def test_prune_deletes_old_transcripts_and_dead_work_dirs_only(tmp_path):
    now = time.time()
    (tmp_path / "old.txt").write_text("x")
    (tmp_path / "new.txt").write_text("x")
    (tmp_path / "keep.json").write_text("x")  # not ours to judge
    (tmp_path / "work-dead").mkdir()
    (tmp_path / "work-dead/audio.wav").write_text("x")
    (tmp_path / "work-live").mkdir()
    _age(tmp_path / "old.txt", 15, now)
    _age(tmp_path / "new.txt", 13, now)
    _age(tmp_path / "keep.json", 99, now)
    _age(tmp_path / "work-dead", 2, now)
    reel.prune(tmp_path, 14, now)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "keep.json",
        "new.txt",
        "work-live",
    ]


def test_every_run_prunes_by_the_configured_retention(box):
    run, root, _ = box
    root.mkdir(mode=0o755)
    old = root / "OLDCODE123.txt"
    old.write_text("old transcript")
    _age(old, 20, time.time())
    r = run(LINK, REEL_RETENTION_DAYS="30")
    assert r.returncode == 0 and old.exists()
    r = run(LINK)  # the default is shorter than 20 days
    assert r.returncode == 0 and not old.exists()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700  # tightened, not trusted


# --- tools --------------------------------------------------------------------------


def test_yt_dlp_is_found_explicit_then_beside_then_shared_then_path(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    shared = home / ".openclaw/skills/youtube-transcript/yt-dlp"
    shared.parent.mkdir(parents=True)
    shared.write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("YT_DLP", raising=False)
    assert reel.yt_dlp_cmd() == [sys.executable, str(shared)]
    monkeypatch.setenv("YT_DLP", "/opt/yt-dlp")
    assert reel.yt_dlp_cmd() == ["/opt/yt-dlp"]
    monkeypatch.delenv("YT_DLP")
    shared.unlink()
    monkeypatch.setenv("PATH", str(tmp_path))
    assert reel.yt_dlp_cmd() == []
    path_ytdlp = _stub(tmp_path / "yt-dlp", "#!/bin/sh\n")
    assert reel.yt_dlp_cmd() == [str(path_ytdlp)]


def test_no_yt_dlp_is_a_failed_run(box, tmp_path):
    r = box.check(
        LINK, YT_DLP="", PATH=str(tmp_path), JOBQ_MAX_ATTEMPTS="1", HOME=str(tmp_path)
    )
    assert r.returncode == 0, r.stderr
    assert "yt-dlp is not installed" in r.stdout


# --- the skill file ---------------------------------------------------------------------


def test_skill_frontmatter_parses_into_the_name_and_bins_openclaw_reads():
    front = (SKILL / "SKILL.md").read_text().split("---")[1]
    meta = yaml.safe_load(front)
    assert meta["name"] == "reel-claim-check"
    assert meta["metadata"]["openclaw"]["requires"]["bins"] == [
        "python3",
        "ffmpeg",
        "whisper",
    ]


# --- the ensure script ----------------------------------------------------------------------


DOCKER = """#!/bin/sh
case "$1" in
  ps) [ -n "$GATEWAY" ] && echo "$GATEWAY" ;;
  exec)
    case "$*" in
      *node*) printf %s "$ALLOWLIST" ;;
      *) printf 'tool: ffmpeg present\\ntool: whisper present\\ntool: yt-dlp present\\n' ;;
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
    return r, ws / "skills/reel-claim-check"


def test_ensure_installs_the_skill_into_kits_workspace(tmp_path):
    r, dest = _ensure(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (dest / "SKILL.md").read_bytes() == (SKILL / "SKILL.md").read_bytes()
    assert (dest / "reel.py").read_bytes() == (SKILL / "reel.py").read_bytes()
    assert os.access(dest / "reel.py", os.X_OK)
    assert "no running openclaw-gateway container" in r.stdout


def test_ensure_converges_on_a_rerun_and_overwrites_a_stale_copy(tmp_path):
    _, dest = _ensure(tmp_path)
    (dest / "SKILL.md").write_text("stale\n")
    r, dest = _ensure(tmp_path)
    assert r.returncode == 0
    assert (dest / "SKILL.md").read_bytes() == (SKILL / "SKILL.md").read_bytes()


def test_ensure_skips_a_box_without_kit_and_succeeds(tmp_path):
    r, dest = _ensure(tmp_path, workspace=False)
    assert r.returncode == 0
    assert "no workspace for agent 'kit'" in r.stdout
    assert not dest.exists()


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
    assert said in r.stdout
    assert "tool: whisper present" in r.stdout
