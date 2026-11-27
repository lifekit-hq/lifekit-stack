# scout

Fetch step of the generic timed scouting pipeline (design: the scout pipeline
report, section 4; this is its unit 1). A daily host timer runs the Debian
[`feed2exec`](https://gitlab.com/anarcat/feed2exec) package over the feed list
of each vault lens page, keeps only items not seen before, and when there are
any sends **one** note to firstmate's inbox with the count and the path of the
candidates file. No new items: the run ends silently, with no note and no model
call. Judging, filing and the digest are other units and live elsewhere.

## Files

| File | Job |
| --- | --- |
| `scout-fetch.sh` | the run: lens pages -> feed2exec -> candidates file -> one `fm-inbox.sh note` |
| `scout_echo.py` | feed2exec output plugin: the echo plugin's line plus the item's feed and date |
| `scout_order.py` | which feeds are new (to catch up), and each lens's order and cap |
| `lifekit-scout.service` / `.timer` | oneshot unit, daily 05:30 Europe/London, `Persistent=true` |
| `install-scout.sh` | `apt-get install feed2exec`, installs both units, writes `/etc/lifekit/scout.env`. **Does not enable the timer.** |

## Lens pages

Read live from `SCOUT_LENS_DIR` (default `/srv/memory/system/scout`), one
`<lens>.md` per interest. A page is a lens when its frontmatter has
`scout: on`; `off`, a missing key or a page without frontmatter is ignored. The
first fenced `ini` block under `## Sources` is feed2exec's own configuration,
passed to it untouched. The script refuses (skips and reports) a block that:

- has no feed `url`;
- sets `output` to anything but `feed2exec.plugins.echo` (the `exec` plugin
  would put feed text on a command line);
- uses an `args` placeholder other than `{item.link}` / `{item.title}` (an
  absent field such as `{item.published}` crashes the whole feed2exec run);
- has `args` that do not start with a literal source tag.

A page whose frontmatter is not closed, or whose feed2exec run exits non-zero,
is skipped the same way. A skipped lens is reported **once**, on the
transition (`state/broken/<lens>` is the marker; it is removed when the lens
runs clean, so a later breakage is reported again). Other lenses still run.

## State

`SCOUT_STATE_DIR` (default `~/.local/state/lifekit-scout`):

- `db/<lens>.sqlite` - feed2exec's seen-items database, one per lens, plus a
  `scout_seeded` table of the feeds a first-sighting catch-up recorded (deleting
  the file resets both);
- `candidates/<date>.txt` - new items of that UTC day, `lens<TAB>` plus the
  echo-plugin line as printed (shell-quoted: split with a quoting-aware parser,
  never `eval`). Each lens's lines are appended as soon as its feed2exec call
  returns, failed call included, because feed2exec has already marked them seen; `<date>.sent` marks the note as delivered. A failed note leaves the
  file unmarked and the next run announces it (exit 1 meanwhile);
- `candidates/<date>.overflow` - items past the lens cap (below), same format:
  seen, kept for the record, not announced;
- `broken/<lens>` - the reason a lens is currently skipped.

## Order and cap

The normal fetch runs with the lens's `feed2exec.plugins.echo` swapped for
`scout_echo`, which prints the same line plus the item's feed and date
(feed2exec's normalized `updated_parsed`, always present). A lens's lines are
then written grouped by feed in config order, **each feed newest first**,
whatever order the feed itself lists them in. At most 60 lines (the judge's
triage cap) per lens and day go to the candidates file, shared fairly: a feed with fewer items than its share keeps
them all and the larger feeds split the rest, so a big feed never crowds out a
small one. A later same-day run gets what is left of the cap.

## Use

```bash
FM_INBOX_BIN=/path/to/fm-inbox.sh FM_INBOX_HOME=/path/to/home scripts/scout/scout-fetch.sh
scripts/scout/scout-fetch.sh --catchup   # record current feed contents as seen; sends nothing
```

A feed not yet in the lens's database (a new lens, or a source just added to
one) is caught up automatically before the normal fetch: its back catalogue is
recorded as seen and the first offered items are the ones published after
that. The catch-up fetches each new feed itself and hands feed2exec a local
copy only when the answer is an HTTP 2xx that parses as a feed; such a feed is
recorded as seen even when it was empty, so its first item is offered, not
caught up. An error page (404, 429, 5xx), an HTML page or a network error is
not a feed: that source is left out of the normal fetch, the lens is reported
broken naming it, and the next run tries its catch-up again. `--catchup`
records every feed of every lens as seen by hand.

To offer cut or overflow lines again (operator, once), put them in their own
file, at most 60 per lens, e.g. `candidates/<date>-reoffer.txt`: the next run
announces every candidates file without a `.sent` marker, once, with its own
note.

Note text: `scout: N new items from M lens(es); candidates: <path>`, request id
`scout-<date>-<N>`, N being the candidates file's line count (a retry of the same content is a no-op for the inbox; a later same-day run with new items sends its own note).

## Install (operator, not enabled)

```bash
sudo FM_INBOX_BIN=/path/to/fm-inbox.sh FM_INBOX_HOME=/path/to/home \
  bash /srv/lifekit-stack/scripts/scout/install-scout.sh
# later, after the measured firing:  sudo systemctl enable --now lifekit-scout.timer
```

Undo: `apt-get remove feed2exec`, remove `/etc/systemd/system/lifekit-scout.*`
and `/etc/lifekit/scout.env`, delete the state directory.

## Tests

`scripts/tests/test_scout_fetch.py` runs the script against a stub `feed2exec`
and a stub `fm-inbox.sh` (the real `fm-inbox.sh note [--request-id <id>] [--] <text>`
usage is documented in its header), and checks the timer with `systemd-analyze
calendar` and the installed units with `systemd-analyze verify`.
