# monday-page

The Monday page's board payload, built from the fleet's own records. `monday_page.py` reads the
fleet publisher's merged `home-summary.json` (`scripts/fleet-publisher/`), the calls of each
home on this host from that home's backlog, the newest `fleet-policy audit` report and
`fleet-policy pace --json`, and prints one
`fm-bearings-board.v1` payload to stdout. The firstmate `bin/fm-bearings-board.sh build` turns it
into the page unchanged. No model, no network, no writes.

Run it from the firstmate home that owns the board (`build` needs a file, not a pipe):

```bash
p=$(mktemp) && python3 /srv/lifekit-stack/scripts/monday-page/monday_page.py --tasks-axi "$FM_HOME/bin/fm-tasks-axi.sh" --audit "$FM_HOME/data/policy-audit" > "$p" && "$FM_HOME/bin/fm-bearings-board.sh" build "$p"
```

**Where calls come from.** The summary cuts each hold reason at 160 characters, usually inside
the options. So for every summary home whose `path` exists on this host (or each `--home`), the
calls are read whole from that home's backlog: `FM_HOME=<home> fm-tasks-axi.sh list --state held
--fields hold_reason,hold_kind,hold_until`, keeping hold kind `captain`, then `show <id> --full`
for the whole title and reason and the body's origin and hold time. These replace that home's
summary decisions. Every other home, such as a remote PC home, keeps its summary decisions, and
one whose reason was cut (`…`) is refused as `reason truncated in the published summary`. A
backlog that cannot be read falls back to the summary for that home, noted on stderr. Without
`--tasks-axi` every call comes from the summary.

| Source | Becomes |
| --- | --- |
| backlog calls, then `decisions_open[]` of other homes | a Captain's Call decision card: options and `recommend_value` from the `rec=ID opts: ID=LABEL; ... - context` tokens `fleet-policy decide` writes (a part with no `=` continues the label before it), `about` = origin (when known) and home plus that context, `detail` = the whole reason |
| audit + pace + fleet counts | one card first in Captain's Call, "Accept this week's scope", recommended `accept`: the audit line in `about`, the pace line in `decide`, both plus the fleet counts in `detail` (the board shows `detail` on merge cards only) |
| `active_children[]` | Underway |
| `queued[]` | Charted Next, filed by `since`; held or blocked rows are not dispatchable; an id that is also an open decision is skipped |
| `landed[]` | Recently Landed, owner = the home |

**Refusals.** A decision missing options, a recommendation or context is never a call. It
becomes a Charted Next row of kind `warning` (shown "needs repair", never dispatchable), titled
`Refused call: ...`, whose reason lists every missing piece: `no options`, `no recommendation`,
`no context`, `no board key`, `reason truncated in the published summary` (a summary-sourced
reason ends in `…`; nothing after the cut can be judged), `options ambiguous` (an option label held ` - `, so the options
cannot be told from the context), `an option id is not a slug`, `duplicate option ids`,
`recommendation is not one of the options`, `option id reconcile is reserved`, `duplicate key`.
Its `filed` is the generation time, so refusals sort above all queued work. Row ids are
`refused-<key>`, suffixed `-2`, `-3` ... when a key repeats. Each refusal is also named on stderr.

**Missing sources.** Without the summary nothing is printed and the exit is 1. A missing or
unreadable audit or pace still prints the page; the weekly card says which source is
unavailable and why.

`[[`/`]]` in copied text is backslash-escaped, as vault-door does, so it never reads as a wikilink.

Environment (flags win): `MONDAY_PAGE_SUMMARY` (default `/var/lib/lifekit-fleet/home-summary.json`),
`MONDAY_PAGE_AUDIT` (report file or directory; `--audit`), `MONDAY_PAGE_PACE` (a file holding the
pace JSON; default runs `fleet-policy pace --json`), `MONDAY_PAGE_FLEET_POLICY`,
`MONDAY_PAGE_TASKS_AXI` (`--tasks-axi`), `MONDAY_PAGE_HOMES` (`:`-separated home directories;
`--home`, repeatable), `MONDAY_PAGE_NOW`.

Tests: `scripts/tests/test_monday_page.py`. Set `MONDAY_PAGE_BOARD_SH` to a firstmate
`bin/fm-bearings-board.sh` to also check the payload against the board's own validator.
