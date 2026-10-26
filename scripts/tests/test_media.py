"""The media code the youtube-transcript and video-eval skills share
(skills/youtube-transcript/media.py): captions, .vtt to text, the pinned
yt-dlp and its downloads, ffmpeg and whisper.

The skills' own suites run it end to end through their CLIs
(test_youtube_transcript.py, test_video_eval.py); this one covers its helpers
and the tool calls directly. yt-dlp, ffmpeg and whisper are stubs that log
their argv, so nothing here touches the network or needs the models.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills/youtube-transcript"

sys.path.insert(0, str(SKILL))
import media  # noqa: E402

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

# A yt-dlp stand-in for download(): logs argv, writes the info and, unless
# STUB_MODE says otherwise, the audio.
YT_DLP_STUB = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
mode = os.environ.get("STUB_MODE", "ok")
open(os.environ["STUB_LOG"], "a").write(json.dumps(argv) + "\n")
out = Path(argv[argv.index("-o") + 1]).parent
if mode == "gone":
    sys.stderr.write("WARNING: noise\nERROR: [youtube] x: Video unavailable\n")
    sys.exit(1)
(out / "x.info.json").write_text(json.dumps({"id": "x", "title": "A title"}))
if "--skip-download" in argv:
    sys.exit(0)
if mode == "toolong":
    sys.stdout.write("[download] x does not pass filter (duration<=?900), skipping ..\n")
    sys.exit(0)
(out / "x.m4a").write_bytes(b"audio")
"""

FFMPEG_STUB = r"""#!/bin/sh
[ "$FFMPEG_MODE" = fail ] && exit 1
for a; do out="$a"; done
printf 'RIFF' > "$out"
"""

WHISPER_STUB = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write(json.dumps(argv) + "\n")
out = Path(argv[argv.index("--output_dir") + 1])
(out / (Path(argv[-1]).stem + ".txt")).write_text(" Hello\n  there ")
print("Detected language: Ukrainian")
"""


def _stub(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def tools(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("YT_DLP", str(_stub(tmp_path / "yt-dlp", YT_DLP_STUB)))
    monkeypatch.setenv("VIDEO_FFMPEG", str(_stub(tmp_path / "ffmpeg", FFMPEG_STUB)))
    monkeypatch.setenv(
        "VIDEO_WHISPER", str(_stub(tmp_path / "whisper", WHISPER_STUB))
    )
    for k in ("STUB_MODE", "FFMPEG_MODE", "VIDEO_WHISPER_MODEL", "VIDEO_WHISPER_LANG"):
        monkeypatch.delenv(k, raising=False)
    work = tmp_path / "work"
    work.mkdir()

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text().splitlines()]

    tools.calls = calls
    tools.work = work
    return tools


# --- both skills, one module --------------------------------------------------------


def test_video_eval_imports_the_module_the_youtube_transcript_skill_ships():
    spec = importlib.util.spec_from_file_location(
        "video_for_media", REPO / "skills/video-eval/video.py"
    )
    video = importlib.util.module_from_spec(spec)
    sys.modules["video_for_media"] = video  # dataclasses looks its module up here
    try:
        spec.loader.exec_module(video)
        loaded = video.media()
    finally:
        sys.modules.pop("video_for_media", None)
    assert Path(loaded.__file__).resolve() == SKILL / "media.py"


# --- captions -------------------------------------------------------------------------


def test_vtt_to_text_drops_tags_timing_and_rolling_repeats():
    assert media.vtt_to_text(AUTO_VTT) == "Today we're going to talk\nabout AI >> agents"


def test_vtt_to_text_keeps_a_cue_that_is_only_digits():
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n100\n\n00:00:01.000 --> 00:00:02.000\n2024\n"
    assert media.vtt_to_text(vtt) == "100\n2024"


def test_pick_prefers_original_language_track():
    files = [Path("v.en.vtt"), Path("v.en-orig.vtt")]
    assert media.pick(files) == Path("v.en-orig.vtt")
    assert media.pick([Path("v.en.vtt")]) == Path("v.en.vtt")
    assert media.pick([]) is None


def test_pick_takes_the_spoken_language_among_several_original_tracks():
    files = [Path(f"v.{lang}-orig.vtt") for lang in ("bn", "de", "en", "es")]
    assert media.pick(files, "de") == Path("v.de-orig.vtt")
    assert media.pick(files, "en-US") == Path("v.en-orig.vtt")
    assert media.pick(files, "sv") == Path("v.en-orig.vtt")  # English fallback
    assert media.pick(files) == Path("v.en-orig.vtt")
    assert media.pick(files[:2]) is None  # never the alphabetically first
    assert media.pick([Path("v.uk-orig.vtt")], "en") == Path("v.uk-orig.vtt")


@pytest.mark.parametrize(
    ("printed", "spoken", "tracks"),
    [
        ("en-US\n", "en-US", "en-US-orig,en-orig"),
        ("de", "de", "de-orig,en-orig"),
        ("en", "en", "en-orig"),
        ("NA\n", "", ".*-orig"),
        ("", "", ".*-orig"),
    ],
)
def test_auto_pass_asks_for_the_spoken_language_then_english(printed, spoken, tracks):
    assert media.spoken_language(printed) == spoken
    assert media.original_tracks(spoken) == tracks


def test_track_language_says_when_it_is_auto_generated():
    assert media.language("abc", Path("abc.en.vtt")) == "en"
    assert media.language("abc", Path("abc.uk-orig.vtt")) == "uk (auto-generated)"


# --- yt-dlp ---------------------------------------------------------------------------


def test_yt_dlp_is_the_explicit_one_else_the_pinned_one_else_path(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("YT_DLP", "/opt/yt-dlp")
    assert media.yt_dlp_cmd() == ["/opt/yt-dlp"]
    monkeypatch.delenv("YT_DLP")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(media, "__file__", str(tmp_path / "media.py"))
    beside = _stub(tmp_path / "yt-dlp", "")
    assert media.yt_dlp_cmd() == [sys.executable, str(beside)]
    beside.unlink()
    shared = tmp_path / ".openclaw/skills/youtube-transcript/yt-dlp"
    shared.parent.mkdir(parents=True)
    shared.write_text("")
    assert media.yt_dlp_cmd() == [sys.executable, str(shared)]
    shared.unlink()
    (tmp_path / "bin").mkdir()
    on_path = _stub(tmp_path / "bin" / "yt-dlp", "")
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert media.yt_dlp_cmd() == [str(on_path)]
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert media.yt_dlp_cmd() == []


def test_download_goes_through_the_proxy_it_is_given(tools):
    audio, info, error = media.download(
        "https://x/v", tools.work, 900, "socks5h://relay:1"
    )
    assert (audio.name, info["title"], error) == ("x.m4a", "A title", "")
    argv = tools.calls()[0]
    assert argv[argv.index("--proxy") + 1] == "socks5h://relay:1"
    assert argv[argv.index("--match-filter") + 1] == "duration<=?900"
    assert argv[-2:] == ["--", "https://x/v"]
    assert not list(tools.work.glob("*.info.json"))  # read, then removed


def test_download_without_a_proxy_goes_direct(tools):
    media.download("https://x/v", tools.work, 900, None)
    assert "--proxy" not in tools.calls()[0]


def test_download_of_the_info_only_skips_the_media(tools):
    audio, info, error = media.download(
        "https://x/v", tools.work, 900, None, media=False
    )
    assert (audio, info["id"], error) == (None, "x", "")
    assert "--skip-download" in tools.calls()[0]


def test_a_download_skipped_for_its_length_is_no_file_and_no_error(
    tools, monkeypatch
):
    monkeypatch.setenv("STUB_MODE", "toolong")
    assert media.download("https://x/v", tools.work, 900, None)[::2] == (None, "")


def test_a_failed_download_is_yt_dlps_last_error_line(tools, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "gone")
    audio, _, error = media.download("https://x/v", tools.work, 900, None)
    assert (audio, error) == (None, "ERROR: [youtube] x: Video unavailable")


def test_no_yt_dlp_is_an_error_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_DLP", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(media, "__file__", str(tmp_path / "media.py"))
    assert media.download("https://x/v", tmp_path, 900, None) == (
        None,
        {},
        "yt-dlp is not installed",
    )
    assert media.captions("x", "", "en.*")[3] == "yt-dlp is not installed"


# --- audio ----------------------------------------------------------------------------


def test_speech_is_whispers_text_and_detected_language(tools):
    clip = tools.work / "clip.m4a"
    clip.write_bytes(b"audio")
    assert media.speech(clip, tools.work) == ("Hello there", "Ukrainian", "")
    argv = tools.calls()[0]
    assert argv[argv.index("--model") + 1] == "base" and "--language" not in argv


def test_a_forced_whisper_language_and_model_are_used_and_reported(
    tools, monkeypatch
):
    monkeypatch.setenv("VIDEO_WHISPER_MODEL", "small")
    monkeypatch.setenv("VIDEO_WHISPER_LANG", "uk")
    clip = tools.work / "clip.m4a"
    clip.write_bytes(b"audio")
    assert media.speech(clip, tools.work) == ("Hello there", "uk", "")
    argv = tools.calls()[0]
    assert argv[argv.index("--model") + 1] == "small"
    assert argv[argv.index("--language") + 1] == "uk"


def test_audio_ffmpeg_cannot_extract_is_no_speech_and_whisper_never_runs(
    tools, monkeypatch
):
    monkeypatch.setenv("FFMPEG_MODE", "fail")
    clip = tools.work / "clip.m4a"
    clip.write_bytes(b"audio")
    assert media.speech(clip, tools.work) == ("", "", "")
    assert tools.calls() == []
