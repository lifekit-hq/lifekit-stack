"""Helpers for scout-fetch.sh: which feeds to seed, and the order and cap of a lens's items.

    scout_order.py seed <ini> <db> <dir>     the lens config cut to its sections
                                             not seen yet (first sighting: to be
                                             caught up), each fetched here and
                                             its url pointed at the copy saved in
                                             <dir>; a section whose fetch is not
                                             an HTTP 2xx feed is left out and
                                             listed in <dir>/failed
    scout_order.py config <ini> <db>         the config cut to its other sections,
                                             echo output swapped for scout_echo
    scout_order.py seeded <ini> <db>         record the sections of <ini> as seen
    scout_order.py order <lens> <budget> <overflow file>
        stdin: scout_echo lines (<feed> TAB <epoch> TAB <echo line>)
        stdout: candidate lines (<lens> TAB <echo line>), grouped by feed in
        config order, each feed newest first, at most <budget> in all

A section is seen once it has a row in feedcache or is recorded by `seeded`
(a feed caught up while still empty has no row, and must not be caught up again
when it first publishes). Both live in the lens database, so deleting it resets
both. feed2exec parses whatever body a server answers, error pages included, so
`seed` checks the answer itself: only a 2xx body that feedparser reads as a feed
(an empty one counts) is caught up and then recorded; anything else stays unseen
and is tried again on the next run. `seed` and `config` print nothing when no
section qualifies. `order` shares the
budget fairly: a feed with fewer items than its share keeps them all and the
rest is split among the larger feeds, so one big feed can never crowd out a
small one. What does not fit is appended to the overflow file (feed2exec has
already marked it seen; nothing is dropped silently).
"""

from __future__ import annotations

import configparser
import http.client
import io
import sqlite3
import sys
import urllib.request
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


def seeded(ini: str, db: str) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(f"CREATE TABLE IF NOT EXISTS {SEEDED} (name text PRIMARY KEY)")
        con.executemany(
            f"INSERT OR IGNORE INTO {SEEDED} VALUES (?)",
            [(s,) for s in read_ini(ini).sections()],
        )
        con.commit()
    finally:
        con.close()


def select(ini: str, db: str, seen_ones: bool) -> configparser.RawConfigParser:
    parser = read_ini(ini)
    seen = seen_feeds(db)
    for section in parser.sections():
        if (section in seen) != seen_ones:
            parser.remove_section(section)
    return parser


def dump(parser: configparser.RawConfigParser) -> str:
    if not parser.sections():
        return ""
    out = io.StringIO()
    parser.write(out)
    return out.getvalue()


def download(url: str) -> bytes:
    # urlopen returns only a 2xx answer (redirects followed); any other is an error
    req = urllib.request.Request(url, headers={"User-Agent": "feed2exec"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def seed(ini: str, db: str, workdir: str) -> str:
    import feedparser

    parser = select(ini, db, seen_ones=False)
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    for n, section in enumerate(parser.sections()):
        try:
            body = download(parser.get(section, "url", fallback="").strip())
        except (OSError, ValueError, http.client.HTTPException) as e:
            failed.append(f"{section} ({e})")
            parser.remove_section(section)
            continue
        if not feedparser.parse(body).get("version"):
            failed.append(f"{section} (not a feed)")
            parser.remove_section(section)
            continue
        copy = work / f"{n}.feed"
        copy.write_bytes(body)
        parser.set(section, "url", copy.resolve().as_uri())
    (work / "failed").write_text("; ".join(failed))
    return dump(parser)


def config(ini: str, db: str) -> str:
    parser = select(ini, db, seen_ones=True)
    if parser.defaults().get("output", "").strip() == ECHO:
        parser.defaults()["output"] = PLUGIN
    for section in parser.sections():
        if parser.get(section, "output", fallback="").strip() == ECHO:
            parser.set(section, "output", PLUGIN)
    return dump(parser)


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
    if len(argv) == 4 and argv[0] == "seed":
        sys.stdout.write(seed(argv[1], argv[2], argv[3]))
        return 0
    if len(argv) == 3 and argv[0] == "config":
        sys.stdout.write(config(argv[1], argv[2]))
        return 0
    if len(argv) == 3 and argv[0] == "seeded":
        seeded(argv[1], argv[2])
        return 0
    if len(argv) == 4 and argv[0] == "order" and argv[2].isdigit():
        order(argv[1], int(argv[2]), argv[3])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
