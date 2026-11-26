#!/usr/bin/env python3
"""kit-held - a held item that names a fleet task becomes a shaping item.

Kit sometimes holds an item for later ("he said tomorrow"). When the held item
names a task for firstmate or another owner, a daily note is the wrong place
for it: no script reads Kit's notes, so the item waits until someone happens to
look (lifekit-stack#684, gap G1 of vault decision 0019). Two verbs:

  file    the door. Kit's relay (`relay.sh hold`, the kit-relay `hold` verb)
          pipes one held item here, an envelope block and its text on stdin:

            ---
            origin: <where it was said>
            owner: <who acts next: firstmate, devclaw, ...>
            class: dev | ops | finance | life | unclear
            fingerprint: <one stable id for the item>
            acceptance: <the done-when>
            thread: <the conversation it belongs to>
            ---
            <the held item, in Kit's words>

          It opens a SHAPING ITEM: a captain-held task (fm-captain-hold.sh
          hold, the same primitive scripts/intake-shape uses) shown on the
          Calls page, id `held-<fingerprint>`, its reason carrying the
          envelope. A repeat of the same fingerprint while the item is open
          adds a count to its title (`(seen Nx)`); a repeat of an answered
          item opens `held-<fingerprint>-<n>` naming the earlier one (the
          decision 0016 repeat rule). An item whose owner is Kit itself is a
          reminder, stays in the note and is refused here (exit 65). Prints the
          receipt `{"id","url","state","count"}`; state `submitted` for a new
          item, `working` for a repeat or a replay. A request id seen before
          (`--request-id`, the relay's retry of the same text) is a replay: the
          same receipt, no second count. Every filing is kept in the record
          `filed.json` (KIT_HELD_STATE): per item base its fingerprint, chain
          length and filed date, because the hub archives answered items and
          its own state then reads them as absent.

  check   the safety net. Lists the open items in Kit's daily notes
          (`--notes`, the dir of `YYYY-MM-DD.md` files) that name a fleet task
          and have no shaping item, as a JSON list of `{id, note, title,
          since}`. `since` is when this check first saw the item unfiled
          (`--seen`, a JSON file it keeps), never earlier than the note's day;
          the fleet publisher marks an item stalled 24 hours after it
          (`kit_held.hours` in fleet-stalled.json).

          An open item is an unchecked task line (`- [ ] ...`) or a `Next:`
          line. It names a fleet task when it says `for <owner>:` with an
          owner other than Kit, or mentions firstmate, the second mate,
          devclaw, the fleet or a `task:` note. `for kit:` (or `for me:`) is a
          reminder to Kit itself and never listed. It has a shaping item when
          it carries `(shaping: <item id>)` and that item is in the record
          or exists in one of the homes (`--home`, repeatable; closed counts,
          it was answered).

Environment: FM_HOME (file: the firstmate home holding the hold), TASKS_AXI
(the tasks-axi command, default `tasks-axi`; an absolute one puts its directory
first on PATH, where fm-captain-hold.sh finds it), KIT_HELD_STATE (where the
seen request ids and the record live, default ~/.local/state/lifekit-kit-held;
`check` reads the record, so it runs with the same one).

Usage: kit_held.py file [--request-id ID]           (held item on stdin)
       kit_held.py check --notes DIR --home DIR [--home DIR ...] [--seen FILE]
                         [--days N] [--now EPOCH]

Exit: 0 done; 1 the hub refused (stderr says why; nothing half-written that a
retry does not finish); 64 bad usage; 65 a refused item (an envelope field
missing, a bad class, a reminder to Kit itself); 78 no FM_HOME.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Installed side by side with intake_shape.py; in the repo it is a sibling dir.
for p in (HERE, HERE.parent / "intake-shape"):
    if (p / "intake_shape.py").is_file():
        sys.path.insert(0, str(p))
        break
from intake_shape import Hub, item_id, one_line, slug  # noqa: E402

FIELDS = ("origin", "owner", "class", "fingerprint", "acceptance", "thread")
CLASSES = ("dev", "ops", "finance", "life", "unclear")
SELF_OWNERS = ("kit", "me", "self", "myself")
REQUEST_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
SEEN_RE = re.compile(r" \(seen ([0-9]+)x\)$")
TITLE_MAX = 150
REASON_MAX = 900
MAX_BODY = 32768
MAX_CHAIN = 200  # a hub that answers every id as existing must not loop forever

NOTE_RE = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})\.md$")
OPEN_RE = re.compile(
    r"^\s*(?:[-*+]\s+\[ \]\s+(?P<task>.*)|(?:[-*+]\s+)?\**Next:?\**:?\s+(?P<next>.*))$"
)
FOR_RE = re.compile(r"^\**for\s+([A-Za-z0-9 _-]{1,40}?)\**\s*:", re.I)
FLEET_RE = re.compile(r"\b(?:first ?mate|second ?mate|devclaw|fleet)\b|\btask:", re.I)
SHAPING_RE = re.compile(r"\(shaping:\s*([A-Za-z0-9._-]+)\s*\)")
DAYS = 7


def log(message: str) -> None:
    print(f"kit-held: {message}", file=sys.stderr)


# ---------------------------------------------------------------- file


class Refused(Exception):
    pass


def parse_item(raw: str) -> tuple[dict[str, str], str]:
    """The envelope and the item's text; Refused names what is wrong."""
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        raise Refused("held item must start with the envelope block (---)")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise Refused("envelope block is not closed (---)") from None
    env: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep and key.strip() in FIELDS:
            env[key.strip()] = one_line(value)
    missing = [f for f in FIELDS if not env.get(f)]
    if missing:
        raise Refused("envelope missing: " + " ".join(missing))
    if env["class"] not in CLASSES:
        raise Refused("envelope class must be dev, ops, finance, life or unclear")
    if env["owner"].lower() in SELF_OWNERS:
        raise Refused(
            "a reminder to Kit itself is not filed; it stays in the daily note"
        )
    if not slug(env["fingerprint"]):
        raise Refused("fingerprint has no letters or digits")
    text = "\n".join(lines[end + 1 :]).strip()
    if not text:
        raise Refused("the held item has no text")
    return env, text


def held_title(env: dict[str, str], text: str, count: int) -> str:
    seen = f" (seen {count}x)" if count > 1 else ""
    head = one_line(text)[: TITLE_MAX - len(seen) - 40]
    return one_line(f"Held for {env['owner']}: {head}")[: TITLE_MAX - len(seen)] + seen


def held_reason(env: dict[str, str], text: str, previous: str | None) -> str:
    again = f" Repeat of {previous}, already answered." if previous else ""
    envelope = ", ".join(f"{f}={env[f]}" for f in FIELDS)
    return one_line(
        f"Kit held this for {env['owner']}: {text}{again} Envelope: {envelope}."
    )[:REASON_MAX]


def current_title(hub: Hub, item: str) -> str:
    res = hub.run([*hub.tasks, "show", item, "--full"])
    if res.returncode != 0:
        raise RuntimeError(
            f"tasks-axi show {item} exit {res.returncode}: {one_line(res.stderr)[:200]}"
        )
    for line in res.stdout.splitlines():
        if line.startswith("  title: "):
            raw = line[len("  title: ") :]
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
            return value if isinstance(value, str) else raw
    return ""


def seen_count(title: str) -> int:
    m = SEEN_RE.search(title)
    return int(m[1]) if m else 1


def state_dir() -> Path:
    named = os.environ.get("KIT_HELD_STATE")
    if named:
        return Path(named)
    return (
        Path(os.environ.get("HOME", "/nonexistent")) / ".local/state/lifekit-kit-held"
    )


def load_filed() -> dict[str, dict[str, object]]:
    """The record of filed items: base id -> {fingerprint, chain, filed}."""
    try:
        loaded = json.loads((state_dir() / "filed.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("filed.json is not a JSON object")
    return {
        k: v
        for k, v in loaded.items()
        if isinstance(v, dict) and isinstance(v.get("chain"), int)
    }


def filed_ids(record: dict[str, dict[str, object]]) -> set[str]:
    return {
        item_id(base, n)
        for base, entry in record.items()
        for n in range(1, int(entry["chain"]) + 1)
    }


def file_item(raw: str, hub: Hub, request_id: str | None) -> dict[str, object]:
    """Open or count the shaping item; returns the receipt."""
    env, text = parse_item(raw)
    marker = None
    if request_id:
        marker = state_dir() / "requests" / request_id
        if marker.is_file():
            known = json.loads(marker.read_text(encoding="utf-8"))
            return {**known, "state": "working", "replay": True}
    base = "held-" + slug(env["fingerprint"])[:60].strip("-")
    record = load_filed()
    seen = int(record.get(base, {}).get("chain", 0))
    while hub.state(item_id(base, seen + 1)) != "absent":
        seen += 1
        if seen >= MAX_CHAIN:
            raise RuntimeError(
                f"{base}: {MAX_CHAIN} items already; the hub looks wrong"
            )
    previous = None
    if seen and hub.state(item_id(base, seen)) == "open":
        item = item_id(base, seen)
        count = seen_count(current_title(hub, item)) + 1
        hub.retitle(item, held_title(env, text, count))
        state = "working"
    else:
        previous = item_id(base, seen) if seen else None
        seen += 1
        item = item_id(base, seen)
        count = 1
        hub.hold(item, held_title(env, text, 1), held_reason(env, text, previous))
        state = "submitted"
    record[base] = {
        "fingerprint": env["fingerprint"],
        "chain": seen,
        "filed": dt.datetime.now(dt.timezone.utc).date().isoformat(),
    }
    write_json(state_dir() / "filed.json", record)
    receipt: dict[str, object] = {
        "id": item,
        "url": f"calls:{Path(hub.home).name}/{item}",
        "state": state,
        "count": count,
    }
    if previous:
        receipt["repeat_of"] = previous
    if marker is not None:
        write_json(marker, receipt)
    return receipt


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def cmd_file(args: argparse.Namespace) -> int:
    home = os.environ.get("FM_HOME", "")
    if not home or not Path(home).is_dir():
        log("FM_HOME is not set to a firstmate home")
        return 78
    if args.request_id and not REQUEST_RE.fullmatch(args.request_id):
        log("bad request id")
        return 64
    data = sys.stdin.buffer.read(MAX_BODY + 1)
    if len(data) > MAX_BODY:
        log(f"refused: held item over {MAX_BODY} bytes")
        return 65
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:
        log("refused: held item is not valid UTF-8")
        return 65
    try:
        receipt = file_item(raw, Hub(home), args.request_id)
    except Refused as why:
        log(f"refused: {why}")
        return 65
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        log(str(error))
        return 1
    print(json.dumps(receipt, separators=(",", ":")))
    return 0


# ---------------------------------------------------------------- check


def note_items(path: Path, day: str) -> list[dict[str, str]]:
    """The open items of one daily note that name a fleet task."""
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = OPEN_RE.match(line)
        if not m:
            continue
        text = (m["task"] if m["task"] is not None else m["next"] or "").strip()
        owner = FOR_RE.match(text)
        if owner:
            if owner[1].strip().lower() in SELF_OWNERS:
                continue  # a reminder to Kit itself
        elif not FLEET_RE.search(text):
            continue
        ref = SHAPING_RE.search(text)
        digest = hashlib.sha256(SHAPING_RE.sub("", text).strip().encode()).hexdigest()
        out.append(
            {
                "id": f"{day}:{digest[:12]}",
                "note": path.name,
                "line": str(n),
                "title": one_line(SHAPING_RE.sub("", text))[:160],
                "ref": ref[1] if ref else "",
            }
        )
    return out


def cmd_check(args: argparse.Namespace) -> int:
    now = int(args.now if args.now is not None else time.time())
    notes = Path(args.notes)
    hubs = [Hub(h) for h in args.home]
    cutoff = (
        dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
        - dt.timedelta(days=args.days)
    ).isoformat()
    seen_path = Path(args.seen) if args.seen else None
    seen: dict[str, int] = {}
    if seen_path is not None:
        try:
            loaded = json.loads(seen_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                seen = {k: int(v) for k, v in loaded.items() if isinstance(v, int)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            log(f"{seen_path}: {error.__class__.__name__}; starting a new record")
    try:
        files = sorted(p for p in notes.iterdir() if NOTE_RE.match(p.name))
        known = {ref: True for ref in filed_ids(load_filed())}
    except (OSError, ValueError) as error:
        log(f"{error}")
        return 1
    found = []
    failed = False
    for path in files:
        day = NOTE_RE.match(path.name)[1]
        if day < cutoff:
            continue
        try:
            items = note_items(path, day)
        except (OSError, UnicodeDecodeError) as error:
            log(f"{path.name}: {error.__class__.__name__}")
            failed = True
            continue
        for it in items:
            ref = it["ref"]
            try:
                if ref and ref not in known:
                    known[ref] = any(h.state(ref) != "absent" for h in hubs)
                if ref and known[ref]:
                    continue
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                log(str(error))
                failed = True
                continue
            day_start = int(
                dt.datetime.fromisoformat(day)
                .replace(tzinfo=dt.timezone.utc)
                .timestamp()
            )
            first = seen.setdefault(it["id"], max(now, day_start))
            found.append({**it, "since": max(first, day_start)})
    if seen_path is not None:
        live = {it["id"] for it in found}
        kept = {k: v for k, v in seen.items() if k in live}
        try:
            write_json(seen_path, kept)
        except OSError as error:
            log(f"{seen_path}: {error}")
    print(json.dumps(found, separators=(",", ":")))
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kit_held.py")
    sub = parser.add_subparsers(dest="verb", required=True)
    f = sub.add_parser("file")
    f.add_argument("--request-id")
    c = sub.add_parser("check")
    c.add_argument("--notes", required=True)
    c.add_argument("--home", action="append", default=[], required=True)
    c.add_argument("--seen")
    c.add_argument("--days", type=int, default=DAYS)
    c.add_argument("--now", type=int)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 64 if exc.code else 0
    tasks = os.environ.get("TASKS_AXI", "")
    if os.path.isabs(tasks):
        os.environ["PATH"] = f"{os.path.dirname(tasks)}:{os.environ.get('PATH', '')}"
    return cmd_file(args) if args.verb == "file" else cmd_check(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
