"""Helpers for scout-fetch.sh: which feeds to seed, and the order and cap of a lens's items.

    scout_order.py config <ini> <db> seed    the lens config cut to its sections
                                             not seen yet (first sighting: to be
                                             caught up)
    scout_order.py config <ini> <db> fetch   the config cut to its other sections,
                                             echo output swapped for scout_echo
    scout_order.py seeded <ini> <db> <log>   record as seen the sections of <ini>
                                             whose feed the `feed2exec -v` run
                                             logged in <log> reached and parsed
    scout_order.py order <lens> <budget> <overflow file>
        stdin: scout_echo lines (<feed> TAB <epoch> TAB <echo line>)
        stdout: candidate lines (<lens> TAB <echo line>), grouped by feed in
        config order, each feed newest first, at most <budget> in all

A section is seen once it has a row in feedcache or is recorded by `seeded`
(a feed reached while still empty has no row, and must not be caught up again
when it first publishes). Both live in the lens database, so deleting it resets
both. `config` prints nothing when no section qualifies. `order` shares the
budget fairly: a feed with fewer items than its share keeps them all and the
rest is split among the larger feeds, so one big feed can never crowd out a
small one. What does not fit is appended to the overflow file (feed2exec has
already marked it seen; nothing is dropped silently).
"""

from __future__ import annotations

import configparser
import io
import sqlite3
import sys
from pathlib import Path

ECHO = "feed2exec.plugins.echo"
PLUGIN = "scout_echo"
SEEDED = "scout_seeded"


def read_ini(path: str) -> configparser.RawConfigParser:
    # feed2exec reads its config with RawConfigParser too: same keys, same values
    parser = configparser.RawConfigParser()
    parser.read(path)
    return parser


def seen_feeds(db: str) -> set[str]:
    if not Path(db).is_file():
        return set()
    seen: set[str] = set()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for table in ("feedcache", SEEDED):
            try:
                rows = con.execute(f"SELECT DISTINCT name FROM {table}").fetchall()
            except sqlite3.OperationalError:
                # no such table yet: nothing recorded there
                continue
            seen.update(r[0] for r in rows)
    finally:
        con.close()
    return seen


def seeded(ini: str, db: str, log: str) -> None:
    text = Path(log).read_text(errors="replace")
    parser = read_ini(ini)
    reached = [
        s
        for s in parser.sections()
        if f"parsing feed {parser.get(s, 'url', fallback='').strip()} (" in text
    ]
    con = sqlite3.connect(db)
    try:
        con.execute(f"CREATE TABLE IF NOT EXISTS {SEEDED} (name text PRIMARY KEY)")
        con.executemany(
            f"INSERT OR IGNORE INTO {SEEDED} VALUES (?)", [(s,) for s in reached]
        )
        con.commit()
    finally:
        con.close()


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


def order(lens: str, budget: int, overflow: str) -> None:
    groups: dict[str, list[tuple[int, int, str]]] = {}
    for n, raw in enumerate(sys.stdin.read().splitlines()):
        if not raw.strip():
            continue
        parts = raw.split("\t", 2)
        if len(parts) == 3 and parts[1].lstrip("-").isdigit():
            feed, stamp, line = parts[0], int(parts[1]), parts[2]
        else:  # not a scout_echo line: keep it, undated
            feed, stamp, line = tag(raw), 0, raw
        groups.setdefault(feed or tag(line), []).append((stamp, n, line))
    for items in groups.values():
        items.sort(key=lambda it: (-it[0], it[1]))
    alloc = allocate({g: len(v) for g, v in groups.items()}, budget)
    kept: list[str] = []
    over: list[str] = []
    for g, items in groups.items():
        for i, (_, _, line) in enumerate(items):
            (kept if i < alloc[g] else over).append(f"{lens}\t{line}")
    if kept:
        sys.stdout.write("\n".join(kept) + "\n")
    if over:
        with open(overflow, "a") as f:
            f.write("\n".join(over) + "\n")


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[0] == "config" and argv[3] in ("seed", "fetch"):
        sys.stdout.write(config(argv[1], argv[2], argv[3]))
        return 0
    if len(argv) == 4 and argv[0] == "seeded":
        seeded(argv[1], argv[2], argv[3])
        return 0
    if len(argv) == 4 and argv[0] == "order" and argv[2].isdigit():
        order(argv[1], int(argv[2]), argv[3])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
