# The notify envelope (v1)

`notify-relay` is the only thing in this stack that formats a Telegram message.
Producers do not compose strings — they POST a small JSON **envelope** and the
relay renders it.

Producers span four languages (devclaw is Python, the relay is Node, Grafana is
Go templates, OpenClaw is its own runtime), so a shared *library* cannot span
them. A shared *wire format* can.

```
POST http://notify-relay:8090/notify
content-type: application/json
```

```json
{
  "level":    "act | wait | good | info",
  "source":   "devclaw",
  "subject":  "issue-819",
  "headline": "needs a decision",
  "body":     "Which hosts count as private? Artifactory and GitLab are unhandled.",
  "detail":   "optional long text — rendered collapsed",
  "action":   "decide(issue-819, artifactory)",
  "links":    [{ "text": "PR #820", "url": "https://github.com/lifekit-hq/x/pull/820" }]
}
```

`level`, `source`, `subject` and `headline` are required. Everything else is
optional, and `null` counts as absent — you do not need to prune unset keys
before serializing. Responses: `200 {"ok":true}`, `400 {"error":"invalid
envelope","details":[…]}` naming each problem, `413` if the request body is over
256 KB (clip `detail` yourself before sending a whole log file), `502` when
Telegram refused the send.

## Pick a level

The one question a phone notification must answer is **do I act, now?**
`level` answers it and nothing else encodes it.

| level  | Glyph | Means                | Reader does      | `action` rendered? |
| ------ | ----- | -------------------- | ---------------- | ------------------ |
| `act`  | 🔴    | broken now           | act now          | yes                |
| `wait` | 🟡    | waiting on you       | act when you can | yes                |
| `good` | 🟢    | recovered / finished | nothing          | **never**          |
| `info` | ▪️    | progress             | nothing, ever    | no                 |

A producer that cannot pick one of the four is over-notifying.

## The fields

| Field      | Use it for                                                                   |
| ---------- | ---------------------------------------------------------------------------- |
| `source`   | Who is talking — `devclaw`, `grafana`, `kit`. One word, stable across messages. |
| `subject`  | What it is about — `issue-819`, `openclaw-gateway`. The thing, not the event. |
| `headline` | What happened, lowercase, no glyph, no source. Line 1 is a sentence you read. |
| `body`     | The two lines you need before deciding. Not a log.                            |
| `detail`   | The log. Stack traces, `docker ps` output — rendered collapsed, costs one line. |
| `action`   | ONE command or decision, verbatim, so it can be tapped to copy.               |
| `links`    | Up to 3. `http`/`https` only, 300 characters or fewer — a clipped URL is a link that 404s, so an over-long one is rejected, not truncated. |

## What the relay guarantees

1. **Line 1 stands alone** — `<glyph> <b>source</b> · <b>subject</b> — headline`
   is all a lock screen shows, so it has to be enough. `source`, `subject`,
   `headline` and `action` are folded to a single line; a newline in one of them
   cannot forge an extra block.
2. **`action` renders last**, as `→ <code>…</code>` (tap-to-copy), and **only for
   `act` and `wait`**. A `good` or `info` envelope that carries an `action` gets
   it dropped — a resolved alert must never repeat the firing remediation.
3. **`detail` renders inside `<blockquote expandable>`**, collapsed by default.
4. **Everything is HTML-escaped.** Send raw text; do not send markup.
5. **The message always fits.** Every other field is capped and those caps sum
   well under Telegram's limit, so `detail` is simply clipped to whatever budget
   is left — line 1, the links and `action` always survive.

## The one exemption

Grafana's dead-man alert rules post to Telegram **directly** and re-implement
this grammar in their Go template
(`compose/observability/grafana/provisioning/alerting/contact-points.yml.tmpl`).
Routing them through the relay would let a dead relay swallow the one alarm that
says devclaw is dead — the reason they were wired direct in #133. This is a
named ~15-line duplication, not an oversight. Any future producer whose whole
job is to report that other things are down gets the same exemption; everyone
else uses `/notify`.

The template speaks this grammar rather than its own: line 1 is
`<glyph> <b>grafana</b> · <b>alertname</b> — headline`, where a firing alert's
headline is the rule's `summary` (🔴) and a resolved one's is `recovered` (🟢).
A firing alert renders `description` as the body and the rule's `action`
annotation as `→ <code>…</code>`; a **resolved alert renders line 1 only** — the same
guarantee rule 2 gives the relay's producers, re-implemented so a recovery
notice can never repeat the firing remediation. Because Grafana has no escaping
function the Bot API is guaranteed to accept, that duplication comes with a
duplicated constraint: everything the template renders is authored in
`rules.yml`, so alert titles and annotations must contain no `<`, `>` or `&`.
`compose/observability/contact-points.test.js` renders the template and holds
both halves.

## Legacy endpoints

`POST /devclaw` (devclaw task rows) and `POST /text` (pre-composed goal-layer
text) still accept their old payloads, because their callers deploy separately
from this relay. New producers must not use them.

`/devclaw` no longer formats anything itself: `task-row.js` maps the row onto an
envelope and the message comes out of the same renderer as everyone else's —
`done` → `good`, `failed` → `act`, anything else → `info`; the goal goes in the
headline, the error's first line in `body` and the rest of the traceback in the
collapsed `detail`. `/text` sends its payload as the whole message — the goal
layer composed it already — but escaped and clipped by the same helper the
renderer uses, so it is HTML on the wire like everything else and a `<` in it
cannot fail the send. Send raw text there too; markup you send will be shown,
not parsed. It goes away once devclaw's goal layer posts envelopes.
