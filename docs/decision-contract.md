# Decision contract — the captain-facing decision push

A decision that needs the owner reaches the phone as one typed payload, answerable from a review
board. `notify-relay` builds it (`compose/notify-relay/decision.js`) and the dashboard decision page
renders the same payload. The schema is [`decision-contract.schema.json`](../compose/notify-relay/decision-contract.schema.json);
`compose/notify-relay/test/decision.test.js` keeps it equal to the code and is the executable form of
this page (when they disagree, the tests win).

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
