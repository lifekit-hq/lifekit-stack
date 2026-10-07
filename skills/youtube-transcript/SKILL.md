---
name: youtube-transcript
description: Get the spoken text of a YouTube video (its captions) so you can summarize it or answer questions about it. Use when the user shares a YouTube link or id and wants to know what the video says or is about.
metadata: { "openclaw": { "requires": { "bins": ["python3"] } } }
---

# YouTube transcript

Fetches a video's captions as plain text. Free: no API key, no cookies, no paid service. YouTube refuses this server's IP, so the fetch goes out through the owner's PC (an allow-list relay and an SSH tunnel on the host). When the PC is off the tunnel is down and there is nothing to fetch, which is a normal answer, not a fault.

## Run

```
python3 {baseDir}/transcript.py "<youtube url or 11-character video id>" > /tmp/yt-transcript-<video id>.txt
```

Redirect to a file named for the video (agents and videos share /tmp): transcripts run to tens of thousands of characters. Read the first line of the file before anything else.

- **A transcript** starts with `Title:` and `Video:` lines, then the text. Human-written captions are used when they exist, auto-generated ones otherwise. Read the file in chunks (or run `summarize` on it) and answer from it; for "what is it about" give the topic, the main points and the title. Say when the captions are auto-generated speech-to-text, since names and numbers in them can be wrong.
- **`transcript unavailable - the PC is off`** (exit 0): the tunnel to the PC is down. Tell the owner exactly that: it works again when the PC is on. Do not retry in a loop, do not try another route to YouTube (no cookies, no third-party transcript sites, no downloading audio), and do not guess what the video says from its title or link.
- **`transcript unavailable - this video has no captions`** (exit 0): say so. There is no audio fallback on this server.
- **Exit 1** with `transcript failed: ...` on stderr (private or removed video, a caption download YouTube refused or rate-limited, YouTube changed): report the line as it is. It is not proof the video has no captions; a later retry may work.
- **Exit 2**: the argument was not a YouTube URL or id.

Captions are English by default (`en.*`). For another language set `YT_TRANSCRIPT_LANGS` (a yt-dlp `--sub-langs` pattern, e.g. `uk.*`) in front of the command.
