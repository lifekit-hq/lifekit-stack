"""feed2exec output plugin for scout-fetch.sh: the echo line plus its feed and date.

scout-fetch.sh swaps a lens's `output = feed2exec.plugins.echo` for this module
(on PYTHONPATH from this directory) so each new item prints as

    <feed section name> TAB <item epoch seconds> TAB <echo line>

where the echo line is exactly what feed2exec.plugins.echo prints (the `args`,
placeholders filled, each shell-quoted). The feed name and date let
scout_order.py put every feed newest first and cap it, whatever order the feed
itself lists its items in. feed2exec's normalize() always sets
`updated_parsed` (a time.struct_time, or a float timestamp when the item has no
parseable date), so the date never crashes a run the way an absent
`{item.published}` placeholder does.

In catch-up mode it prints nothing: the item is only recorded as seen.
"""

from __future__ import annotations

import calendar
import re
import shlex
import time

_CONTROL = re.compile(r"[\t\r\n\v\f]+")


def epoch(item) -> int:
    stamp = item.get("updated_parsed") if item else None
    if isinstance(stamp, time.struct_time):
        return calendar.timegm(stamp)
    try:
        return int(float(stamp))
    except (TypeError, ValueError):
        return 0


def output(*args, feed=None, item=None, **kwargs):
    feed = feed or {}
    if feed.get("catchup"):
        return True
    # one item, one line: a tab or newline inside a title would split it
    line = " ".join(shlex.quote(_CONTROL.sub(" ", a)) for a in args)
    print(f"{feed.get('name', '')}\t{epoch(item)}\t{line}", flush=True)
    return True
