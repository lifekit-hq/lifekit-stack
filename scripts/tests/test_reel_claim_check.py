"""The reel-claim-check skill and its ensure script (docs/runbook.md,
"Instagram reel claim check").

yt-dlp, ffmpeg and whisper are replaced by stubs that write the files a real
run would, and Instagram's share-link redirect by a fake opener, so nothing
here touches the network or needs the models.
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

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
        "STUB_LOG": str(log),
        "STUB_MODE": "ok",
        "WHISPER_TEXT": " Lemon water cures cancer.\nStudies show it. ",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def run(arg: str, **extra):
        return subprocess.run(
            [sys.executable, str(SKILL / "reel.py"), arg],
            env={**env, **extra},
            capture_output=True,
            text=True,
            check=False,
        )

    return run, root, log


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
        [None],  # unreachable
        ["/somewhere/else/"] * 3,  # never reaches a reel
    ],
)
def test_share_link_that_does_not_resolve_is_none(monkeypatch, locations):
    _share(monkeypatch, *locations)
    assert reel.resolve_share(SHARE) is None


def test_a_share_link_is_resolved_before_yt_dlp_sees_it(box, monkeypatch):
    _, _, log = box
    monkeypatch.setattr(reel, "resolve_share", lambda r: reel.Reel(CODE))
    code, out = reel.run(SHARE)
    assert code == 0 and out.startswith(f"Reel: {LINK}")
    assert LINK in log.read_text().splitlines()[0]


def test_an_unresolvable_share_link_is_a_normal_answer_without_a_download(
    box, monkeypatch
):
    _, root, log = box
    monkeypatch.setattr(reel, "resolve_share", lambda r: None)
    assert reel.run(SHARE) == (0, reel.NO_SHARE)
    assert log.read_text() == ""


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
    assert r.stdout.strip() == line
    assert list(root.iterdir()) == []  # nothing left behind
    assert "whisper" not in log.read_text()


def test_the_login_answer_says_no_login_is_used():
    assert "logged-in" in reel.LOGIN and "no login is used" in reel.LOGIN


def test_an_unexpected_yt_dlp_failure_is_exit_1_with_the_reason(box):
    run, root, _ = box
    r = run(LINK, STUB_MODE="boom")
    assert r.returncode == 1
    assert r.stdout == ""
    assert r.stderr.startswith("reel failed: ERROR: Unable to download webpage")
    assert list(root.iterdir()) == []


def test_a_reel_without_speech_is_a_normal_answer_and_keeps_nothing(box):
    run, root, _ = box
    r = run(LINK, WHISPER_TEXT="  \n")
    assert (r.returncode, r.stdout.strip()) == (0, reel.NO_SPEECH)
    assert list(root.iterdir()) == []


def test_a_reel_whose_audio_cannot_be_extracted_has_no_speech(box):
    run, root, _ = box
    r = run(LINK, FFMPEG_MODE="fail")
    assert (r.returncode, r.stdout.strip()) == (0, reel.NO_SPEECH)
    assert list(root.iterdir()) == []


def test_a_whisper_crash_is_exit_1(box):
    run, root, _ = box
    r = run(LINK, WHISPER_MODE="fail")
    assert r.returncode == 1 and "reel failed: Traceback: boom" in r.stderr
    assert list(root.iterdir()) == []


def test_a_missing_tool_is_exit_1_not_a_traceback(box):
    run, _, _ = box
    r = run(LINK, REEL_FFMPEG="/nonexistent/ffmpeg")
    assert r.returncode == 1
    assert r.stderr.strip() == "reel failed: /nonexistent/ffmpeg is not installed"


@pytest.mark.parametrize("arg", ["", "hello", "https://youtu.be/bQPU6UJ4iCw"])
def test_anything_but_a_reel_link_is_a_usage_error(box, arg):
    run, root, log = box
    r = run(arg)
    assert r.returncode == 2 and r.stdout == ""
    assert "not an Instagram reel link" in r.stderr or "usage" in r.stderr
    assert log.read_text() == ""
    assert not root.exists()


def test_a_long_caption_is_cut():
    out = reel.render(
        reel.Reel(CODE), {"description": "word " * 600, "duration": 3}, "en", "hi"
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


def test_no_yt_dlp_is_exit_1(box, tmp_path):
    run, _, _ = box
    r = run(LINK, YT_DLP="", PATH=str(tmp_path), HOME=str(tmp_path))
    assert r.returncode == 1 and "yt-dlp is not installed" in r.stderr


# --- the skill file ---------------------------------------------------------------------


def test_skill_frontmatter_names_the_skill_and_its_tools():
    text = (SKILL / "SKILL.md").read_text()
    front = text.split("---")[1]
    assert re.search(r"^name: reel-claim-check$", front, re.M)
    assert re.search(r"^description: .*instagram", front, re.M | re.I)
    for tool in ("python3", "ffmpeg", "whisper"):
        assert f'"{tool}"' in front


def test_skill_tells_kit_not_to_log_in_or_publish_the_reel():
    text = (SKILL / "SKILL.md").read_text()
    assert "never logs in to Instagram" in text
    assert "Do not paste the transcript" in text
    assert "{baseDir}/reel.py" in text


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
