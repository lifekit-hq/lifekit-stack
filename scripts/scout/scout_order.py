"""Helpers for scout-fetch.sh: which feeds to seed, and the order and cap of a lens's items.

    scout_order.py config <ini> <db> seed    the lens config cut to its sections
                                             with no row in the seen database yet
                                             (first sighting: to be caught up)
    scout_order.py config <ini> <db> fetch   the config cut to its other sections,
                                             echo output swapped for scout_echo
    scout_order.py order <lens> <budget> <overflow file> [<reoffer file>]
        stdin: scout_echo lines (<feed> TAB <epoch> TAB <echo line>)
        stdout: candidate lines (<lens> TAB <echo line>), grouped by feed in
        config order, each feed newest first, at most <budget> in all

`config` prints nothing when no section qualifies. `order` shares the budget
fairly: a feed with fewer items than its share keeps them all and the rest is
split among the larger feeds, so one big feed can never crowd out a small one.
What does not fit is appended to the overflow file (feed2exec has already
marked it seen; nothing is dropped silently). Lines from the reoffer file (old
candidate lines put back by the operator, newest first per feed) join their
feed after its fresh items; the ones that fit are removed from that file, the
rest wait for the next run.
"""

from __future__ import annotations

import configparser
import io
import sqlite3
import sys
from pathlib import Path

ECHO = "feed2exec.plugins.echo"
PLUGIN = "scout_echo"


def read_ini(path: str) -> configparser.RawConfigParser:
    # feed2exec reads its config with RawConfigParser too: same keys, same values
    parser = configparser.RawConfigParser()
    parser.read(path)
    return parser


def seen_feeds(db: str) -> set[str]:
    if not Path(db).is_file():
        return set()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT DISTINCT name FROM feedcache").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        # no feedcache table yet: nothing has been seen
        return set()
    return {r[0] for r in rows}


def config(ini: str, db: str, mode: str) -> str:
    parser = read_ini(ini)
    seen = seen_feeds(db)
    keep = [s for s in parser.sections() if (s in seen) == (mode == "fetch")]
    if not keep:
        return ""
    for section in parser.sections():
        if section not in keep:
            parser.remove_section(section)
    if mode == "fetch":
        if parser.defaults().get("output", "").strip() == ECHO:
            parser.defaults()["output"] = PLUGIN
        for section in keep:
            if parser.get(section, "output", fallback="").strip() == ECHO:
                parser.set(section, "output", PLUGIN)
    out = io.StringIO()
    parser.write(out)
    return out.getvalue()


def allocate(sizes: dict[str, int], budget: int) -> dict[str, int]:
    """Fair shares of `budget`: small feeds whole, the rest split evenly."""
    alloc = dict.fromkeys(sizes, 0)
    pending = sorted(sizes, key=lambda g: sizes[g])
    remaining = max(budget, 0)
    while pending and sizes[pending[0]] <= remaining // len(pending):
        g = pending.pop(0)
        alloc[g] = sizes[g]
        remaining -= sizes[g]
    if pending:
        share, extra = divmod(remaining, len(pending))
        for g in [g for g in sizes if g in pending]:  # config order gets the remainder
            alloc[g] = share + (1 if extra > 0 else 0)
            extra -= 1
    return alloc


def tag(line: str) -> str:
    return line.split(" ", 1)[0]


def order(lens: str, budget: int, overflow: str, reoffer: str | None) -> None:
    groups: dict[str, list[tuple[int, int, str, bool]]] = {}
    n = 0
    for raw in sys.stdin.read().splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t", 2)
        if len(parts) == 3 and parts[1].lstrip("-").isdigit():
            feed, stamp, line = parts[0], int(parts[1]), parts[2]
        else:  # not a scout_echo line: keep it, undated
            feed, stamp, line = tag(raw), 0, raw
        groups.setdefault(feed or tag(line), []).append((stamp, n, line, False))
        n += 1
    queued: list[str] = []
    if reoffer and Path(reoffer).is_file():
        queued = [x for x in Path(reoffer).read_text().splitlines() if x.strip()]
        for raw in queued:
            line = raw.split("\t", 1)[1] if "\t" in raw else raw
            # after every fresh item, in the file's own order
            groups.setdefault(tag(line), []).append((-1, n, raw, True))
            n += 1
    for items in groups.values():
        items.sort(key=lambda it: (-it[0], it[1]))
    alloc = allocate({g: len(v) for g, v in groups.items()}, budget)
    kept: list[str] = []
    over: list[str] = []
    taken: set[int] = set()
    for g, items in groups.items():
        for i, (_, idx, line, from_queue) in enumerate(items):
            if from_queue:
                line = line.split("\t", 1)[1] if "\t" in line else line
            if i < alloc[g]:
                kept.append(f"{lens}\t{line}")
                if from_queue:
                    taken.add(idx)
            elif not from_queue:
                over.append(f"{lens}\t{line}")
    if kept:
        sys.stdout.write("\n".join(kept) + "\n")
    if over:
        with open(overflow, "a") as f:
            f.write("\n".join(over) + "\n")
    if reoffer and queued:
        first = n - len(queued)
        left = [raw for k, raw in enumerate(queued) if first + k not in taken]
        if left:
            Path(reoffer).write_text("\n".join(left) + "\n")
        else:
            Path(reoffer).unlink()


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[0] == "config" and argv[3] in ("seed", "fetch"):
        sys.stdout.write(config(argv[1], argv[2], argv[3]))
        return 0
    if len(argv) in (4, 5) and argv[0] == "order" and argv[2].isdigit():
        order(argv[1], int(argv[2]), argv[3], argv[4] if len(argv) == 5 else None)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
