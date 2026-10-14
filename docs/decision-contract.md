# Decision contract — the captain-facing decision push and its answer

A decision that needs the owner reaches the phone as one typed payload, answerable from a review
board. `notify-relay` builds it (`compose/notify-relay/decision.js`) and the dashboard decision page
renders the same payload. The schema is [`decision-contract.schema.json`](../compose/notify-relay/decision-contract.schema.json);
`compose/notify-relay/test/decision.test.js` keeps it equal to the code and is the executable form of
this page (when they disagree, the tests win). How an answer travels back is
[Answering a decision](#answering-a-decision-dashboard---captain-inbox), implemented in
lifekit-dashboard rather than here.

## Payload (v1)

| Field | Meaning |
| --- | --- |
| `kind` | `decision` · `blocked` · `done` · `info` |
| `project` | The project the ask belongs to, in plain words |
| `title` | `<Severity>: <project> — <ask>`. Severity word first (`Decision`, `Blocked`, `Done`, `FYI`) |
| `body` | Numbered options, the recommended one first and marked `(recommended)`; `no action needed` when there are none |
| `decision_id` | Stable id, `[A-Za-z0-9][A-Za-z0-9._-]*`, at most 64 characters |
| `options` | `[{ id, label, recommended }]`, recommended first, at most one recommended, at most 6 |
| `free_text_allowed` | `true` when the owner may answer in their own words |
| `link` | The board when one exists, otherwise the dashboard decision page |

The title is plain words. Producers send `project` and `ask`; the relay rejects an `ask` or
`project` that carries a task or issue id, a branch name, a hash, or a status prefix
(`working:`, `needs-decision -`, …) with a 400.

## Link

- Board: `<board base>/session/<board_id>` — used when the producer sends `board_id` and
  `NOTIFY_BOARD_BASE_URL` is set.
- Otherwise the dashboard deep link `<dashboard base>/decisions/<decision_id>`. The dashboard serves
  `/decisions/<decision id>` (the page itself is a separate change). With
  `NOTIFY_DASHBOARD_BASE_URL` unset the link is the bare route `/decisions/<decision id>`.

Both bases are deploy configuration (`.env`), never committed.

## One notification per decision id

`POST /decision` with the producer input:

```json
{
  "kind": "decision",
  "project": "Billing dashboard",
  "ask": "Which export format should ship first?",
  "decision_id": "export-format",
  "options": [
    { "label": "CSV" },
    { "label": "JSON", "recommended": true }
  ],
  "free_text": true,
  "board_id": "optional-board-id"
}
```

(`free_text: true` sets `free_text_allowed`.) Answers `200 {"ok":true,"replaced":false,"decision":{…}}`
with the contract payload, `400` for invalid input, `502` when Telegram refused it.

- **Replace:** posting the same `decision_id` again edits the existing Telegram message in place
  (`editMessageText`); it does not send a second one.
- **Clear:** `POST /decision/clear` with `{"decision_id":"…"}` deletes the message once the
  decision is answered (`{"ok":true,"cleared":true}`; an unknown id is `cleared:false`).

The id → message mapping is in memory (the container is read-only). After a relay restart the next
`POST /decision` for an id sends a fresh message, and a clear for a forgotten id is a no-op.

## Answering a decision (dashboard -> captain inbox)

The owner answers from a dashboard decision card. The answer reaches the captain inbox of the
fleet home that owns the decision as one note, and firstmate closes the decision from that note.
This section is the contract the dashboard backend and its tap-to-answer UI code against.

**notify-relay is not on this path.** It only sends the push above and has no route to any
inbox. The dashboard backend owns this endpoint, because putting it in notify-relay would need two
new secrets plus a host change; this repo carries only the contract. The path is:

browser -> edge sign-in (`edge/`) -> dashboard backend `POST /api/relay/note` -> ssh with the
dashboard's relay key -> the `kit-relay` forced command (`dashboard` sender, `dash-` request ids)
-> `fm-inbox.sh note` in the owning home (`docs/runbook.md` "Dashboard sender (proposal box)").

As installed on 2026-10-14, the live `kit-relay` pins both homes (`fm-vps` as default, `firstmate`
as the second pinned home) and has the `dashboard` sender arm, and the dashboard key's
authorized_keys line is in place. `docs/runbook.md` "Kit's second-mate relay" (install with a second
pinned home) and "Dashboard sender (proposal box)" (the dashboard key) set these up.

### Where the options come from

The source is the fleet feed (`GET /api/fleet`, `decisions_open[]`). Each item carries `id` and
`home_id`. Once the producer publishes it, an item also carries `ask`:

```json
{ "question": "…", "options": [{ "id": "…", "label": "…", "recommended": true }],
  "free_text_allowed": true, "link": "https://…" }
```

The recommended option comes first, there is at most one recommended option, and there are at
most 6 options. `ask` is `null` when the call has none. The backend reads the item fresh on every
request and never trusts options sent by the browser.

### Request

`POST /api/relay/note`, JSON. It is accepted only through the edge (`EdgeGate`: the oauth2-proxy
identity header plus the edge proof). The existing `proposal` and `task` kinds are unchanged.

| Field | Answer | Confirm |
| --- | --- | --- |
| `kind` | `"answer"` | `"confirm"` |
| `decisionId` | required, the item's `id` (`[A-Za-z0-9][A-Za-z0-9._-]*`, at most 64) | required |
| `homeId` | required, the item's `home_id` | required |
| `submissionId` | required, opaque, `[A-Za-z0-9-]{8,64}` (for example a UUID) | required |
| `optionId` | the chosen option's `id`; exactly one of `optionId` or `text` | not allowed |
| `text` | the owner's own words, trimmed, not empty | not allowed |

### Validation, in order

The first failure answers. Nothing is sent to the host unless every check passes.

1. `kind`, `decisionId` shape, `homeId`, `submissionId` shape, and exactly one of `optionId`/`text`
   (none for `confirm`) -> else `400 invalid`.
2. The fleet feed is readable -> else `503 unavailable` (the backend does not guess the home).
3. The (`homeId`, `decisionId`) pair is in `decisions_open` now, matched on the item's (`home_id`,
   `id`) because ids are per home -> else `409 not-open` (unknown, already closed, or not yet published).
   More than one matching item -> `409 ambiguous`, nothing sent.
4. `homeId` is one the relay pins (`fm-vps`, `firstmate`) -> else `400 invalid`.
5. When `ask` is not null:
   - `optionId` must be one of `ask.options[].id`;
   - `text` is allowed only when `ask.free_text_allowed` is true.

   Otherwise `400 invalid`.
6. When `ask` is null: `text` passes through, and `optionId` is `400 invalid` (no offered options to match).
7. The note body is at most 32768 bytes, has no NUL bytes and is valid UTF-8 (the relay refuses
   either with exit 65) -> else `400 invalid`, with nothing sent.

### Note body and home

| Kind | Body (exact) |
| --- | --- |
| answer, option | `answer <decisionId>: <the option's label, as published>` |
| answer, text | `answer <decisionId>: <text>` |
| confirm | `confirm <decisionId>` |

The backend runs `kit-relay note <rid> <home>`, where `<home>` is the request's `homeId`, which
matched the item's `home_id`. The
relay matches it only against its pinned homes. Firstmate's standing answer-note rule closes
exactly that decision through `fm-captain-hold.sh answer`. If the id is unknown or already closed,
it gets one reply saying so and nothing changes.

### Idempotency

`rid = dash-<UTC yyyyMMdd>-<first 16 hex of sha256(body + "\n" + submissionId)>`, with the note
body unchanged. The dashboard UI generates one `submissionId` per deliberate submit: each time the
owner picks and sends it is a fresh id, and the same id is reused on automatic retries of that
submit. This refines the plan report's `<prefix>-<UTC day>-<sha256(body)[:16]>` scheme so that a
deliberate same-day re-answer is never swallowed as a replay.

Retrying one submit on the same UTC day replays the same note: `outcome: "replay"`, never a second
note. When that note was saved but not announced (relay exit 3, `announced: false`), retrying with
the same `submissionId` re-announces it, so the UI can offer a retry of the same answer. A retry on
a later UTC day is a new note, and firstmate replies "unknown or closed id" when the first one
already closed the decision. Every deliberate submit is a new note, and firstmate handles them in
order: A, then B, then A again on one day records three answers, the last being A.

### Response

The response is always JSON `{ "status", "id", "outcome", "announced", "problem" }` with
`Cache-Control: no-store`. The relay's stdout and stderr, the key path and the edge proof never
appear in a response or a log line.

| HTTP | `status` | Meaning |
| --- | --- | --- |
| 200 | `saved` | The note is in the inbox. `id` is the inbox note id and `outcome` is `created` or `replay`. `announced: false` means saved but the wake did not fire (relay exit 3): the next session start raises it, and retrying the same `submissionId` the same UTC day re-announces it without a second note. |
| 400 | `invalid` | A rule above failed. `problem` says which one, in plain words. |
| 401 / 403 | `unauthorized` / `forbidden` | The request did not come through the edge sign-in. |
| 409 | `not-open` | The decision is not open. The UI drops the card and refreshes the feed. |
| 409 | `ambiguous` | More than one open item has that `homeId` and `decisionId`. Nothing was sent; the UI tells the owner to answer that decision on the board. |
| 429 | `rate-limited` | The host bucket is full: 30 new `dash-` notes per rolling hour per home (relay exit 75). Nothing was saved. |
| 502 | `error` | The relay refused or failed, or timed out after 20 s. Not confirmed saved; `problem` explains. |
| 503 | `not-configured` / `unavailable` | The relay key or edge proof is not configured, or the fleet feed cannot be read. |

### Confirm-first for risky calls

Firstmate replies first when an answer would authorize a merge or a destructive, irreversible or
security-sensitive action. The reply says what it is about to do, and the decision **stays open**.
It acts only on a later `confirm <decisionId>`. An ordinary answer closes the decision, and the
item leaves `decisions_open` within about a minute.

The UI reads replies with `GET /api/relay/receipts?homeId=<home>[&after=<cursor>]` (edge-gated,
`no-store`). `homeId` is required and must be one the relay pins (`fm-vps`, `firstmate`),
otherwise `400 invalid` with nothing sent (the relay exits 64). `after`, when sent, is exactly
12 digits, else `400 invalid` with nothing sent. This runs `kit-relay receipts [<cursor>] <home>`
and returns its `kit-relay-receipts.v1` view, which holds only `dash-` notes and their replies.
The note and reply fields are firstmate's `fm-inbox.sh receipts` view, passed through by kit-relay:

- `pending[]` and `handled[]`: notes, each with `id`, `request_id`, `acknowledged`, `announced` and `reply`.
- `replies[]`: each with `id` (the note id), `at`, `body` and `cursor`.
- `reply_cursor`: pass it back as `after` to read only newer replies. `after` must be a cursor from a
  receipts response for the same `homeId`.

Statuses reuse the answer path's rows: `200` with the view; `400 invalid`; `401` / `403` from the
edge; `502 error` when the relay fails or its output is unreadable, which the UI shows as "replies
unavailable", never as "no replies yet"; `503 unavailable` when the relay key is not configured.

The UI matches `replies[].id` to the `id` it got back from the answer and shows the reply `body`
verbatim. It offers **Confirm** only when the feed read that still lists the decision has
`generated_epoch` later than the `at` of the latest reply to the owner's most recent note for that
decision. `at` is an ISO-8601 UTC timestamp, which the UI parses to epoch seconds before comparing
it with `generated_epoch`. Otherwise it waits for the next feed read and shows no Confirm, so a
feed that has not caught up with an ordinary close never prompts.
A `confirm` with nothing pending is harmless: firstmate replies saying so.
