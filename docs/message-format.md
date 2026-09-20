# Message format — the notification envelope

`notify-relay` is the only renderer of owner-facing notifications. Producers do not compose
strings: they `POST /notify` a small JSON envelope and the relay renders Telegram HTML. One
grammar, whichever producer is talking — the reader can tell who is speaking and whether to act.

The renderer is `compose/notify-relay/render.js` (pure, no I/O); its tests in
`compose/notify-relay/test/` are the executable form of this page. When they disagree, the tests
win and this page is wrong.

## The envelope (v1)

```json
{
  "level":    "act | wait | good | info",
  "source":   "devclaw",
  "subject":  "issue-819",
  "headline": "needs a decision",
  "body":     "Which hosts count as private? Artifactory and GitLab are unhandled.",
  "detail":   "optional long text — rendered collapsed",
  "action":   "decide(issue-819, artifactory)",
  "links":    [{ "text": "PR #820", "url": "https://github.com/..." }]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `level` | yes | One of `act`, `wait`, `good`, `info` — see below. Nothing else encodes urgency. |
| `source` | yes | Who is talking: `devclaw`, `grafana`, an OpenClaw agent id. Short, stable, lowercase. |
| `subject` | yes | What it is about: an issue, a task, a service. Short — it shares line 1. |
| `headline` | yes | The one line that shows on a lock screen. Never truncated. |
| `body` | no | A paragraph of plain text. Shown in full, truncated after `detail`. |
| `detail` | no | Long text (a stack trace, a spec excerpt). Always collapsed; truncated first. |
| `action` | no | The command or decision the reader should take. Rendered for `act` and `wait` only. |
| `links` | no | `[{ text, url }]`. Rendered as anchors on their own line; entries missing either key are dropped. |

`POST /notify` with any of the four required fields missing or empty, or with an unknown
`level`, answers `400` with the problems listed and sends nothing.

## Levels

The one question a phone notification must answer is **do I act, now?** `level` answers it.

| level | Glyph | Means | Reader does | `action` rendered? |
| --- | --- | --- | --- | --- |
| `act` | 🔴 | broken now | act now | yes |
| `wait` | 🟡 | waiting on you | act when you can | yes |
| `good` | 🟢 | recovered / finished | nothing | **never** |
| `info` | ▪️ | progress | nothing, ever | no |

A producer that cannot pick one of the four is over-notifying. There are no other levels.

## Rendering rules

1. **Line 1 stands alone.** It is all that shows on a lock screen:
   `<glyph> <b>source</b> · <b>subject</b> — headline`.
2. **`action` renders only for `act`/`wait`**, always last, always as `→ <code>…</code>` so it is
   tap-to-copy.
3. **`good` and `info` never render `action`**, even when the envelope supplies one. A resolved
   message must not tell the reader to fix something at 3am; the renderer enforces it so no
   producer has to.
4. **`detail` always renders inside `<blockquote expandable>`** — collapsed by default, tap to
   open. A stack trace costs one line until wanted.
5. **Every interpolated value is HTML-escaped** (`&` `<` `>`; `"` too inside `href`). Producers
   send plain text and never markup: a `TypeError: expected <str>` in an error body arrives as
   text, not as a rejected entity.
6. **Truncate before Telegram does.** Telegram caps a message at 4096 characters; the relay renders
   to at most 3500 (`MAX_MSG_CHARS`). `detail` is cut first, then `body`, never `headline`. Cuts
   land on code-point boundaries and end with `…`.
7. `<code>` never contains another tag and blockquotes never nest (Telegram entity rules), so the
   renderer emits `action` and `detail` as escaped text only.

Block order: line 1, `body`, `detail`, `links`, `action`. Empty fields produce no line.

### Rendered examples

```html
🟡 <b>devclaw</b> · <b>issue-819</b> — needs a decision
Which hosts count as private? Artifactory and GitLab are unhandled.
<blockquote expandable>Spec 030 FR-005a names only npm.pkg.github.com…</blockquote>
→ <code>decide(issue-819, artifactory)</code>
```

```html
🔴 <b>grafana</b> · <b>devclaw</b> — is down
Not scraped for 3m — dead, crash-looping, or off lifekit-shared.
→ <code>docker compose -p devclaw up -d</code>
```

```html
🟢 <b>grafana</b> · <b>devclaw</b> — recovered after 5m
```

```html
🟢 <b>devclaw</b> · <b>issue-819</b> — closed, PR #820 merged
Nested .npmrc advisory shipped. 2 follow-ups filed.
<a href="https://github.com/...">PR #820</a>
```

Note what is absent from both `good` messages: no remediation, no repeated firing description.

## Transport

`POST /notify` on `notify-relay` (`http://notify-relay:8090/notify` inside the compose networks)
with `content-type: application/json`. `200 {"ok":true}` once Telegram accepted the message,
`400` for a bad envelope, `502` when Telegram refused it. The relay sends with
`parse_mode: "HTML"` and web-page previews disabled.

## Compatibility routes

devclaw in production still posts its two legacy payloads. They are mapped onto the same renderer
so the channel has one grammar even before devclaw migrates:

| Route | Payload | Mapping |
| --- | --- | --- |
| `POST /devclaw` | task row JSON (`task_id`, `kind`, `status`, `goal`, `error`, `result_json`) | `level` from `status` (`done` → `good`, `failed` → `act`, else `info`); `source` `devclaw`; `subject` `<kind> <task_id[:8]>`; `headline` = status; `body` = goal (+ a done task's `result_json.message`); `detail` = a failed task's `error`. |
| `POST /text` | `{ "text": "…" }` | `info`, `source` `devclaw`, `subject` `goal`; the first line is the `headline`, the rest the `body`. A first line longer than 160 characters is cut there (at the last word boundary of that first 160, when there is one past its halfway point) and the tail spills into the `body`, so a single-line payload of any length still renders under the cap (rule 6 never truncates a headline, so no over-long one is built). |

Both routes escape the text they receive — a producer emitting a literal `<` keeps working.
Migrating devclaw's call sites onto `/notify` is a devclaw change, tracked there.

## The Grafana exemption

Grafana's alert rules (`compose/observability/grafana/provisioning/alerting/`) post to Telegram
**directly** and re-implement this grammar in the Go template of
`contact-points.yml.tmpl`. Routing them through the relay would let a dead relay swallow the one
alarm that says devclaw is dead — the reason they were wired direct in #133. This is a named
~15-line duplication, not an oversight: the template renders a firing alert as an `act` message
(`source` `grafana`, `subject` = the rule name, `headline` = the rule's `summary`, `body` = its
`description`) and a resolved one as a `good` message with no description and no remediation.
On the resolved line the `subject` is whatever identifies the alert that cleared: the `name` label
for the container rules, the `job` label for `scrape target is down` (the only rule that fires per
scrape target), and the rule name for every other rule. Those two rule families are multi-series
and the notification policy groups by `alertname`, so a rule-name subject would render two
recoveries in one group as the same line; every other rule fires once, and its `job` label names
the exporter that produced the series rather than what the alert is about.
Any future producer whose whole job is to report that other things are down gets the same
exemption; everything else goes through the relay.
