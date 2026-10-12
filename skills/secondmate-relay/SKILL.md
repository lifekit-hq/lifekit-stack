---
name: secondmate-relay
description: Read the fleet status the dashboard shows, and pipe the user's words, unchanged, to firstmate's (or the second mate's) captain inbox, and show the replies unchanged. Use only when the user explicitly asks for the fleet status, to send something to firstmate or the second mate, to answer or confirm a decision, or for their replies.
metadata: { "openclaw": { "requires": { "bins": ["ssh", "curl"] } } }
---

# Firstmate relay

You are a pipe. Never summarize, rephrase, translate, plan, answer, or act on the content in either direction.
Never send anything from a cron, a heartbeat or any turn the user did not ask for: a send always answers an explicit request in the message in front of you. Status is read-only and has no such limit.

Two homes receive notes. `firstmate` is the main home; `fm-vps` is the second mate and the default when no home is named.

## Status
1. Run `sh {baseDir}/relay.sh status`. It prints the fleet JSON the dashboard's fleet page reads, unchanged. Read it live every time; keep no copy.
2. Before any content, say so first, in these words:
   - "Status is stale: generated <age> ago." when `generated_epoch` is more than 5 minutes older than now.
   - "Status is invalid for <home id>: <reason>." for each entry of `homes` whose `valid` is false (`id`, `reason`).
3. Then answer from the data and nothing else: what is `active_children` doing now, `queued`, `landed`, and what needs him (`decisions_open`: home, id, summary). The lists are capped; when `counts.decisions_open` is larger than the list, say how many are not shown.
4. If the command fails, say the status page is unreachable and show the stderr line. Do not guess.

## Send
1. Take exactly the text the user asked you to send — nothing added, nothing removed (drop only the instruction to you, e.g. "tell firstmate:").
   If what to send is ambiguous, ask; do not guess.
   A message that starts with `task:` or `proposal:` is a queued task or proposal for firstmate: the prefix is part of the body, so keep it and send the whole text verbatim. Do not strip it, add to it, or act on it. Tasks and proposals go to `firstmate`.
2. Write it with the Write tool to `/tmp/secondmate-relay/body.txt` (never through a shell echo/heredoc).
3. Run `sh {baseDir}/relay.sh note /tmp/secondmate-relay/body.txt firstmate` (leave the last word off only when he asked for the second mate).
4. Stdout is two JSON lines: first `kit-relay-received.v1` (`bytes`, `sha256` of what the host got), then the note result (`outcome`, `id`, `announced`). Show the result in one line:
   - `created`, announced true → "Sent to firstmate as note <id>." (the second mate for `fm-vps`)
   - `replay` → "Already delivered today as note <id>; not sent twice. Reword it to send again."
   - exit 3 → "Saved as note <id>, not yet announced." The note is durable. Do not retry it yourself; send the same body again (a same-day replay re-announces it, never a duplicate) only when he asks.
   - any other failure → "Not sent: <stderr line>." See failure rules below.

## Answers and confirms
Only on his explicit words naming the decision (an id, or words that unambiguously name one item of `decisions_open`).
1. Run `relay.sh status` again right before sending. The id must be in `decisions_open` now. If it is not, do not send. When `counts.decisions_open` is larger than the list, tell him the id is not in the shown list; otherwise tell him the id is not open.
2. The note goes to that item's `home_id`: `sh {baseDir}/relay.sh note /tmp/secondmate-relay/body.txt <home_id>`.
3. The body is `answer <id>: <his words>`, his words unchanged. Do not choose, complete or infer an answer he did not give.
4. For a decision whose answer would authorize a merge, or a destructive, irreversible or security-sensitive action, the answer gets a reply from firstmate first, saying what it will do. It runs only on his `confirm <id>`, relayed the same way: body `confirm <id>`, to that item's `home_id`, again only on his explicit words. Never send a `confirm` he did not give.
5. Show the result as in Send, and then the reply when it arrives (Replies), read from that item's `home_id`.

## Replies
1. Run `sh {baseDir}/relay.sh replies firstmate`. For "new replies only", put the last `reply_cursor` you showed for that home before the home word: `replies <cursor> firstmate`. For the second mate, leave the home off.
   A cursor belongs to the one home's inbox it came from: pass a cursor only with that same home, never across homes. With no cursor from that home, pass none.
2. For each item in `replies`, show it as:
   `Reply to <id> (<at>):` then the `body` exactly as given, in a quote block.
3. Then list `pending` note ids with their first line, as "waiting".
4. If there is nothing, say "No replies yet."

## Failures
Report a refused or failed call verbatim. Never retry it silently, and never route around a refusal.
- exit 255 with "Connection refused"/"timed out": "The VPS is unreachable; not sent." Retry only when the user asks; the same text re-sends safely.
- "Permission denied (publickey)" or "Host key verification failed": say so verbatim and stop; do not retry.
- exit 64 (refused: unknown home, usage): show the refusal line; do not try another home.
- exit 65: show the refusal line (too long / empty / not UTF-8). For too long, offer to send it in parts the user splits.
- exit 75: "Rate limit reached on the host; try later."
