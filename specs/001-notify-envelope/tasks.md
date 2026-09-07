# Tasks — 001 notify envelope

## US1 — the relay renders the envelope (`POST /notify`)

- [x] T001 `compose/notify-relay/render.js`: level table, HTML escaping, and
      rendered-length clipping helper.
- [x] T002 `render.js`: `validateEnvelope` — required fields, level vocabulary,
      link shape and `http(s)` scheme.
- [x] T003 `render.js`: `renderEnvelope` — line 1, body, collapsed `detail`,
      links, `action` last and only for `act`/`wait`; budget so the message
      always fits.
- [x] T004 `compose/notify-relay/render.test.js`: cover each rendering rule,
      the escaping, the `good`-drops-`action` fix, and the truncation path.
- [x] T005 `compose/notify-relay/server.js`: `POST /notify` — parse, validate,
      render, send with `parse_mode: HTML`; `400` on a bad envelope, `502` on a
      Telegram failure.
- [x] T006 `compose/notify-relay/Dockerfile`: copy `render.js` into the image.
- [x] T007 `devclaw.json`: `verifyCmd` runs the memory-audit pytest suite *and*
      `node --test compose/notify-relay/`.
- [x] T008 `docs/notify-envelope.md` + `README.md` row: the producer-facing
      contract, with the Grafana exemption written down.
- [x] T009 `compose/notify-relay/server.test.js`: drive the real HTTP surface —
      `/notify` sends rendered HTML with `parse_mode: HTML`, `400` without
      calling Telegram on a bad envelope, `413` on an over-large body, `502`
      when Telegram refuses, a body split mid-character still arrives whole, and
      the legacy `/text` + `/devclaw` paths still deliver.
- [ ] T010 Run the Node suite in CI's `tests` job alongside the pytest one.
      *Deferred, needs the maintainer: `.github/workflows/ci.yml` is a gate
      input no worker may edit — an increment that tried was rejected for it.
      Until a human makes the edit, `devclaw.json`'s `verifyCmd` is what runs
      both Node suites plus the pytest one. Do not re-attempt.*
- [x] T011 Close the last unescaped path: `/text` escapes and clips its payload
      and sends `parse_mode: HTML`, so `sendTelegram` has no plain-text mode.
      Cover the link *text* in the escaping test, not just the href.

## US2 — `/devclaw` renders through the envelope

- [x] T101 Map the devclaw task row onto a v1 envelope (`task-row.js`); delete
      the hand-rolled `formatMessage` and send `/devclaw` as HTML.
- [x] T102 Tests for the task-row → envelope mapping, including
      `result_json`-as-string and unknown statuses.

## US3 — devclaw's producers post envelopes *(cross-repo: devclaw)*

- [ ] T201 Goal layer posts an envelope to `/notify`.
- [ ] T202 Task callbacks post an envelope to `/notify`.
- [ ] T203 Retire `/text` here once devclaw's side has deployed.

## US4 — Grafana's dead-man template speaks the same grammar

- [x] T301 Re-implement line 1 and the level glyphs in the Go template; move
      each rule's remediation out of `description` into an `action` annotation
      so it renders as one tap-to-copy `→ <code>…</code>`.
- [x] T302 Resolved messages drop the firing body's remediation text — line 1
      alone, headline `recovered`.
- [x] T303 `compose/observability/contact-points.test.js`: render the template
      and hold both rules, plus the no-`<>&`-in-`rules.yml` constraint that
      standing in for escaping requires. `devclaw.json` runs it.

## US5 — OpenClaw agents produce envelopes

- [ ] T401 Point the agent workspace contract at `/notify` with a level table.
