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

- `db/<lens>.sqlite` - feed2exec's seen-items database, one per lens;
- `candidates/<date>.txt` - new items of that UTC day, `lens<TAB>tag link title`
  (echo-plugin output, shell-quoted: split with a quoting-aware parser, never
  `eval`); `<date>.sent` marks the note as delivered. A failed note leaves the
  file unmarked and the next run announces it (exit 1 meanwhile);
- `health/<lens>.<feed>` - consecutive days a feed timed out or came back empty
  (read from `feed2exec -v`); a dead source must not look like a quiet one;
- `broken/<lens>` - the reason a lens is currently skipped.

## Use

```bash
FM_INBOX_BIN=/path/to/fm-inbox.sh FM_INBOX_HOME=/path/to/home scripts/scout/scout-fetch.sh
scripts/scout/scout-fetch.sh --catchup   # record current feed contents as seen; sends nothing
```

`--catchup` is the seed step before the first real firing (and for a newly
added source): without it the first run would hand the judge a whole backlog.

Note text: `scout: N new items from M lens(es); candidates: <path>`, request id
`scout-<date>` (a repeat the same day is a no-op for the inbox).

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
and a stub `fm-inbox.sh`.
