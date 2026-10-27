# monday-page

The Monday page's board payload, built from the fleet's own records. `monday_page.py` reads the
fleet publisher's merged `home-summary.json` (`scripts/fleet-publisher/`), the newest
`fleet-policy audit` report and `fleet-policy pace --json`, and prints one
`fm-bearings-board.v1` payload to stdout. The firstmate `bin/fm-bearings-board.sh build` turns it
into the page unchanged. No model, no network, no writes.

Run it from the firstmate home that owns the board (`build` needs a file, not a pipe):

```bash
p=$(mktemp) && python3 /srv/lifekit-stack/scripts/monday-page/monday_page.py --audit "$FM_HOME/data/policy-audit" > "$p" && "$FM_HOME/bin/fm-bearings-board.sh" build "$p"
```

| Source | Becomes |
| --- | --- |
| `decisions_open[]` | a Captain's Call decision card: options and `recommend_value` from the `rec=ID opts: ID=LABEL; ... - context` tokens `fleet-policy decide` writes, `about` = origin (when the summary carries one) and home plus that context, `detail` = the whole reason |
| audit + pace + fleet counts | one card first in Captain's Call, "Accept this week's scope", recommended `accept`: the audit line in `about`, the pace line in `decide`, both plus the fleet counts in `detail` (the board shows `detail` on merge cards only) |
| `active_children[]` | Underway |
| `queued[]` | Charted Next, filed by `since`; held or blocked rows are not dispatchable; an id that is also an open decision is skipped |
| `landed[]` | Recently Landed, owner = the home |

**Refusals.** A decision missing options, a recommendation or context is never a call. It
becomes a Charted Next row of kind `warning` (shown "needs repair", never dispatchable), titled
`Refused call: ...`, whose reason lists every missing piece: `no options`, `no recommendation`,
`no context`, `options cut off in the summary` (the published reason was truncated inside its
options), `recommendation is not one of the options`, `option id reconcile is reserved`,
`duplicate key`. Its `filed` is the generation time, so refusals sort above all queued work.
Each refusal is also named on stderr.

**Missing sources.** Without the summary nothing is printed and the exit is 1. A missing or
unreadable audit or pace still prints the page; the weekly card says which source is
unavailable and why.

`[[`/`]]` in copied text is backslash-escaped, as vault-door does, so it never reads as a wikilink.

Environment (flags win): `MONDAY_PAGE_SUMMARY` (default `/var/lib/lifekit-fleet/home-summary.json`),
`MONDAY_PAGE_AUDIT` (report file or directory; `--audit`), `MONDAY_PAGE_PACE` (a file holding the
pace JSON; default runs `fleet-policy pace --json`), `MONDAY_PAGE_FLEET_POLICY`, `MONDAY_PAGE_NOW`.

Tests: `scripts/tests/test_monday_page.py`. Set `MONDAY_PAGE_BOARD_SH` to a firstmate
`bin/fm-bearings-board.sh` to also check the payload against the board's own validator.
