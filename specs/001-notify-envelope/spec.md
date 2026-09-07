# Feature 001 — one message format for every notify producer

Tracking issue: lifekit-hq/lifekit-stack#134

## Problem

Four producers post into one Telegram chat as one bot, each hand-rolling its own
string format: the devclaw goal layer (`notify-relay` `/text`), devclaw task
callbacks (`notify-relay` `/devclaw`), Grafana alert rules (direct to Telegram),
and Netdata. The result is a channel you skim instead of read:

- The emoji vocabulary collided — in devclaw alone `🟡` means five different
  things and `🔴` means "CI red" here but "alert firing" in Grafana.
- No source tag: four voices, one bubble style.
- Resolved messages repeat the firing body verbatim, remediation included, so a
  3am reader acts on an already-resolved alert.
- The devclaw paths send no `parse_mode` at all — no bold subject, no
  tap-to-copy command, no collapsible stack trace.
- Nothing is reusable: a fifth producer (OpenClaw agents) would arrive with a
  fifth format.

## Approach

**The envelope is the contract; `notify-relay` is the only renderer.** Producers
span four languages (Python, Node, Go templates, the OpenClaw runtime), so a
shared *library* cannot span them — a shared *wire format* can. Producers stop
composing strings and POST a small JSON envelope; the relay renders Telegram
HTML.

**Named exception:** Grafana's dead-man rules keep posting to Telegram directly
and re-implement the grammar in their Go template. Routing them through the
relay would let a dead relay swallow the one alarm that says devclaw is dead —
the reason they were wired direct in #133. Any future producer whose whole job
is to report that other things are down gets the same exemption.

## The envelope (v1)

`level`, `source`, `subject` and `headline` are required; `body`, `detail`,
`action` and `links` are optional. **The normative contract — every field, the
level table and the rendering guarantees — is
[`docs/notify-envelope.md`](../../docs/notify-envelope.md)**, written for the
producers that have to implement it. That doc is the single source of truth;
this spec does not restate it.

The decision behind it: the one question a phone notification must answer is
**do I act, now?** `level` (`act` 🔴 / `wait` 🟡 / `good` 🟢 / `info` ▪️) answers
it and nothing else encodes it — four levels replacing ~12 ad-hoc emoji. A
producer that cannot pick one is over-notifying.

Two rules carry the failures this feature exists to fix, so they are
requirements rather than guidance:

- **`good` and `info` never render `action`**, even when a producer sends one —
  the fix for resolved messages repeating the firing remediation. It belongs in
  the renderer, not in every producer's discipline.
- **Every producer-supplied string is HTML-escaped**, the line-1 fields are
  folded to one line, and the message always fits Telegram's limit. A message
  that cannot be escaped or sized safely is one that silently fails to send.

## User stories

- **US1 — the relay renders the envelope.** `POST /notify` takes a v1 envelope,
  validates it, renders Telegram HTML per the rules above, and sends it with
  `parse_mode: HTML`. Invalid envelopes are rejected with a `400` naming the
  problem. *(Acceptance: a `good` envelope carrying an `action` renders without
  it; a `detail` renders collapsed; a 10k-char `detail` still sends.)*
- **US2 — the legacy task callback renders through the envelope.** `/devclaw`
  maps a devclaw task row onto an envelope internally, so the relay has exactly
  one renderer even for producers that have not migrated.
- **US3 — devclaw's producers post envelopes.** The goal layer and task
  callbacks in the devclaw repo stop composing strings; `/text` is retired.
  *(Cross-repo — lands in devclaw, not here.)*
- **US4 — Grafana's dead-man template speaks the same grammar.** The exempted
  direct-to-Telegram template re-implements line 1 and the level glyphs, and
  drops remediation text from resolved messages.
- **US5 — OpenClaw agents produce envelopes** instead of arriving as a fifth
  format on the `devclaw` Telegram account.

## Out of scope

- Routing Grafana or Netdata through the relay (see the named exception).
- Envelope v2 concerns: threading, per-level chats, delivery retries.
