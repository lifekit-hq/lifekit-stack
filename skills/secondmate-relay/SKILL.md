---
name: secondmate-relay
description: Pipe the user's words, unchanged, to the VPS second mate's captain inbox, and show its replies unchanged. Use only when the user explicitly asks to send something to the second mate, or asks for the second mate's replies.
metadata: { "openclaw": { "requires": { "bins": ["ssh"] } } }
---

# Second-mate relay

You are a pipe. Never summarize, rephrase, translate, plan, answer, or act on the content in either direction.

## Send
1. Take exactly the text the user asked you to send — nothing added, nothing removed (drop only the instruction to you, e.g. "tell the second mate:").
   If what to send is ambiguous, ask; do not guess.
2. Write it with the Write tool to `/tmp/secondmate-relay/body.txt` (never through a shell echo/heredoc).
3. Run `sh {baseDir}/relay.sh note /tmp/secondmate-relay/body.txt`.
4. Stdout is two JSON lines: first `kit-relay-received.v1` (`bytes`, `sha256` of what the host got), then the note result (`outcome`, `id`, `announced`). Show the result in one line:
   - `created`, announced true → "Sent to the second mate as note <id>."
   - `replay` → "Already delivered today as note <id>; not sent twice. Reword it to send again."
   - exit 3 → "Saved as note <id> but the wake failed; retrying." Run step 3 once more, unchanged.
   - any other failure → "Not sent: <stderr line>." See failure rules below.

## Replies
1. Run `sh {baseDir}/relay.sh replies` (or with the last `reply_cursor` you showed, for "new replies only").
2. For each item in `replies`, show it as:
   `Reply to <id> (<at>):` then the `body` exactly as given, in a quote block.
3. Then list `pending` note ids with their first line, as "waiting".
4. If there is nothing, say "No replies yet."

## Failures
- exit 255 with "Connection refused"/"timed out": "The VPS is unreachable; not sent." Retry only when the user asks; the same text re-sends safely.
- "Permission denied (publickey)" or "Host key verification failed": say so verbatim and stop; do not retry.
- exit 65: show the refusal line (too long / empty / not UTF-8). For too long, offer to send it in parts the user splits.
- exit 75: "Rate limit reached on the host; try later."
