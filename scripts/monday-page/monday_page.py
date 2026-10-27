#!/usr/bin/env python3
"""monday-page - the Monday page's board payload, built from the fleet's own records.

Reads three sources and prints one fm-bearings-board.v1 payload to stdout, for the
firstmate `bin/fm-bearings-board.sh build` that turns it into the page unchanged:

  summary  the fleet publisher's merged summary (scripts/fleet-publisher/):
           decisions_open, active_children, queued, landed, counts
  audit    the newest `fleet-policy audit` report (a dated .md file, or the
           directory that holds them)
  pace     `fleet-policy pace --json` (a file, or the command run live)

Mapping, each pinned by scripts/tests/test_monday_page.py:

- An open decision becomes a `captains_call` of type decision: its options and
  recommendation come from the tokens `fleet-policy decide` writes into the hold
  reason (`rec=ID opts: ID=LABEL; ID=LABEL - context`), `about` is the origin
  plus that context, `detail` the full reason. Nothing else in the reason is read.
- A decision lacking options, a recommendation or context is never a call: it is
  a `charted` row of kind `warning` (not dispatchable, filed at generation time so
  it sorts first in Charted Next), titled `Refused call: ...`, whose reason lists
  every missing piece.
- One weekly call, "accept this week's scope", carries the audit summary and the
  pace line in `about`, `decide` and `detail` (v1 has no slot for numbers, and the
  board shows `detail` only on merge cards, so the short lines ride `about` and
  `decide` too). A missing or unreadable audit or pace says so there.
- active_children -> `underway`, queued -> `charted` (minus the open decisions,
  already shown above), landed -> `landed`. A second call with a key already on
  the page is refused as a duplicate.
- A `[[`/`]]` in outside text is backslash-escaped, as vault-door does, so it
  never reads as a wikilink.

Deterministic: no model, no network, no clock except MONDAY_PAGE_NOW or now.

Environment (flags override):
  MONDAY_PAGE_SUMMARY       merged summary (default /var/lib/lifekit-fleet/home-summary.json)
  MONDAY_PAGE_AUDIT         audit report file or directory (no default: unset reads as missing)
  MONDAY_PAGE_PACE          file holding `fleet-policy pace --json` output (default: run it)
  MONDAY_PAGE_FLEET_POLICY  fleet-policy binary for the live pace (default fleet-policy)
  MONDAY_PAGE_NOW           pin the clock (epoch seconds), for tests

Exit: 0 payload printed; 1 the summary is missing or unreadable (nothing printed).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "fm-bearings-board.v1"
HOME = "fleet"
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
HTTPS_RE = re.compile(
    r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?(?:[/?#]\S*)?$"
)
GITHUB_REPO_RE = re.compile(r"^https://github\.com/[^/]+/([^/]+)/")
FILED_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
REC_RE = re.compile(r"\brec=(\S+)")
BRACKET_RE = re.compile(r"\[[^\]]*\]\s*")
AUDIT_HEAD_RE = re.compile(r"^# Policy audit (\S+)")
AUDIT_WINDOW_RE = re.compile(r"^Window: (\S+) to (\S+)\. Violations: (\d+)\.")
AUDIT_REPORT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
RESERVED_OPTION = "reconcile"
WEEKLY_OPTIONS = [
    {"value": "accept", "label": "Accept this week's scope"},
    {"value": "change", "label": "Change the scope", "hint": "Say what to add or drop"},
]


def text(value: Any) -> str:
    """One line of outside text, with any wikilink bracket pair escaped."""
    line = " ".join(str(value or "").split())
    return line.replace("[[", "[\\[").replace("]]", "]\\]")


def iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def dicts(summary: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [item for item in summary.get(key) or [] if isinstance(item, dict)]


def repo_of(item: dict[str, Any]) -> str | None:
    if isinstance(item.get("repo"), str) and item["repo"]:
        return item["repo"]
    m = GITHUB_REPO_RE.match(str(item.get("pr_url") or ""))
    return m.group(1) if m else None


# --- decisions -------------------------------------------------------------


def parse_reason(reason: str) -> dict[str, Any]:
    """The tokens `fleet-policy decide` writes:
    `[Tier ...] [P1 ...] rec=ID opts: ID=LABEL; ID=LABEL - context`.

    Options run to the first " - "; everything after it is context. A reason the
    publisher cut short ("…") inside the options leaves them unreadable, and so
    does an option label holding " - " (more `; ID=` options past the split).
    """
    rest = reason.strip()
    while m := BRACKET_RE.match(rest):
        rest = rest[m.end() :]
    rec = None
    if m := re.match(r"rec=(\S+)\s*", rest):
        rec, rest = m.group(1), rest[m.end() :]
    elif m := REC_RE.search(rest):
        rec = m.group(1)
    options: list[tuple[str, str]] = []
    cut = ambiguous = False
    if rest.startswith("opts:"):
        body, sep, context = rest[len("opts:") :].strip().partition(" - ")
        cut = not sep and body.endswith("…")
        ambiguous = bool(re.search(r"; [A-Za-z0-9._-]+=", context))
        for part in body.split("; "):
            value, eq, label = part.partition("=")
            if eq and value.strip() and label.strip():
                options.append((value.strip(), label.strip()))
    else:
        context = rest[2:] if rest.startswith("- ") else rest
    return {
        "rec": rec,
        "options": options,
        "context": context.strip(),
        "cut": cut,
        "ambiguous": ambiguous,
    }


def judge_call(call: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """A decision's call, or the reasons it is refused."""
    reason = str(call.get("reason") or "")
    parsed = parse_reason(reason)
    key = str(call.get("key") or call.get("id") or "")
    why = []
    if not SLUG_RE.match(key):
        why.append("no board key")
    if parsed["cut"]:
        why.append("options cut off in the summary")
    elif parsed["ambiguous"]:
        why.append("options ambiguous")
    elif not parsed["options"]:
        why.append("no options")
    values = [value for value, _ in parsed["options"]]
    if any(not SLUG_RE.match(value) for value in values):
        why.append("an option id is not a slug")
    if RESERVED_OPTION in values:
        why.append("option id reconcile is reserved")
    if len(values) != len(set(values)):
        why.append("duplicate option ids")
    if not parsed["rec"]:
        why.append("no recommendation")
    elif (
        values
        and not parsed["cut"]
        and not parsed["ambiguous"]
        and parsed["rec"] not in values
    ):
        why.append("recommendation is not one of the options")
    if not parsed["context"]:
        why.append("no context")
    if why:
        return None, why
    origin = text(call.get("origin"))
    where = text(call.get("home_id")) or "fleet"
    about = f"{origin} ({where}): " if origin else f"{where}: "
    detail = text(reason)
    if call.get("hold_until"):
        detail += f" · due {text(call['hold_until'])}"
    if isinstance(call.get("hold_age_days"), int):
        detail += f" · held {call['hold_age_days']} days"
    return {
        "key": key,
        "type": "decision",
        "repo": call.get("repo") if isinstance(call.get("repo"), str) else None,
        "title": text(call.get("summary")) or text(call.get("id")) or key,
        "options": [
            {"value": v, "label": text(label)} for v, label in parsed["options"]
        ],
        "recommend_value": parsed["rec"],
        "about": about + text(parsed["context"]),
        "detail": detail,
    }, []


def refused_row(
    call: dict[str, Any], why: list[str], filed: str, taken: set[str]
) -> dict[str, Any]:
    ident = str(call.get("key") or call.get("id") or "")
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", ident)[:120] or "unnamed"
    row_id, n = f"refused-{slug}", 1
    while row_id in taken:
        n += 1
        row_id = f"refused-{slug}-{n}"
    taken.add(row_id)
    return {
        "id": row_id,
        "repo": call.get("repo") if isinstance(call.get("repo"), str) else None,
        "title": "Refused call: "
        + (text(call.get("summary")) or text(ident) or "unnamed"),
        "reason": "refused: " + ", ".join(why),
        "dispatchable": False,
        "kind": "warning",
        "filed": filed,
    }


# --- weekly numbers ----------------------------------------------------------


def read_audit(path: str | None) -> tuple[str | None, str]:
    """(one-line audit summary, why it is unavailable)."""
    if not path:
        return None, "no audit report configured"
    p = Path(path)
    if p.is_dir():
        reports = sorted(f for f in p.iterdir() if AUDIT_REPORT_RE.match(f.name))
        if not reports:
            return None, "no audit report found"
        p = reports[-1]
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"audit report unreadable ({exc.__class__.__name__})"
    head = AUDIT_HEAD_RE.match(lines[0]) if lines else None
    window = next((m for m in map(AUDIT_WINDOW_RE.match, lines) if m), None)
    if not head or not window:
        return None, "audit report is not a fleet-policy audit"
    sections: dict[str, list[str]] = {}
    current = None
    for line in lines:
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current and line.startswith("- ") and line != "- none":
            sections[current].append(line)
    counts = ", ".join(f"{name} {len(items)}" for name, items in sections.items())
    return (
        f"Audit {head.group(1)}: {window.group(3)} violations "
        f"({window.group(1)} to {window.group(2)}); entries: {counts or 'none'}",
        "",
    )


def read_pace(path: str | None, binary: str) -> tuple[str | None, str]:
    """(one-line pace summary, why it is unavailable)."""
    try:
        if path:
            raw = Path(path).read_text(encoding="utf-8")
        else:
            # pace exits 1 when starts are held but still prints its JSON.
            raw = subprocess.run(
                [binary, "pace", "--json"], capture_output=True, text=True, timeout=60
            ).stdout
        pace = json.loads(raw)
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as exc:
        return None, f"pace unavailable ({exc.__class__.__name__})"
    except ValueError:
        return None, "pace output is not JSON"
    if not isinstance(pace, dict) or not isinstance(pace.get("start_allowed"), bool):
        return None, "pace output has no start_allowed"
    parts = ["new starts " + ("allowed" if pace["start_allowed"] else "held")]
    quota = pace.get("quota")
    if isinstance(quota, dict) and quota.get("percentRemaining") is not None:
        parts.append(
            f"weekly quota {quota['percentRemaining']}% left, resets {text(quota.get('resetsAt'))}"
        )
    else:
        parts.append(
            f"weekly quota unreadable ({text(pace.get('quota_error')) or 'no reason'})"
        )
    if pace.get("next_release"):
        parts.append(f"next release {text(pace['next_release'])}")
    if pace.get("burn"):
        parts.append("burn the allowance now")
    elif pace.get("burn_from"):
        parts.append(f"burn from {text(pace['burn_from'])}")
    return "Pace: " + "; ".join(parts), ""


def weekly_call(
    monday: str, audit: tuple[str | None, str], pace: tuple[str | None, str], fleet: str
) -> dict[str, Any]:
    audit_line = audit[0] or f"Audit unavailable: {audit[1]}"
    pace_line = pace[0] or f"Pace unavailable: {pace[1]}"
    return {
        "key": f"weekly-scope-{monday}",
        "type": "decision",
        "repo": None,
        "title": f"Accept this week's scope (week of {monday})",
        "options": WEEKLY_OPTIONS,
        "recommend_value": "accept",
        "allow_freeform": True,
        "freeform_hint": "What to add or drop this week",
        "about": text(audit_line),
        "decide": text(pace_line),
        "detail": text(f"{audit_line} | {pace_line} | {fleet}"),
    }


# --- payload -------------------------------------------------------------------


def build(
    summary: dict[str, Any],
    audit: tuple[str | None, str],
    pace: tuple[str | None, str],
    now: int,
) -> dict[str, Any]:
    generated = iso(now)
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    monday = (today - dt.timedelta(days=today.weekday())).isoformat()

    calls, refused, keys, refused_ids = [], [], set(), set()
    decisions = dicts(summary, "decisions_open")
    for call in decisions:
        card, why = judge_call(call)
        if card and card["key"] in keys:
            card, why = None, ["duplicate key"]
        if card:
            keys.add(card["key"])
            calls.append(card)
        else:
            refused.append(refused_row(call, why, generated, refused_ids))
    # An open decision shows as its call or its refusal, never again as queued work.
    open_ids = {str(d.get(k)) for d in decisions for k in ("id", "key") if d.get(k)}

    underway = []
    for child in dicts(summary, "active_children"):
        ident = text(child.get("id"))
        if not ident:
            continue
        state = text(child.get("state")) or "unknown"
        underway.append(
            {
                "id": ident,
                "repo": repo_of(child),
                "name": text(child.get("name")) or ident,
                "state": state,
                "doing": text(child.get("doing")) or state,
                "kind": text(child.get("kind")) or "task",
            }
        )

    charted = []
    for item in dicts(summary, "queued"):
        ident = str(item.get("id") or "")
        if not SLUG_RE.match(ident) or ident in open_ids:
            continue
        held = bool(item.get("hold_kind") or item.get("unresolved_blocker_ids"))
        row = {
            "id": ident,
            "repo": repo_of(item),
            "title": text(item.get("title")) or ident,
            "reason": text(item.get("hold_reason") or item.get("blocked_reason") or ""),
            "dispatchable": not held,
            "kind": "queued",
        }
        if FILED_RE.match(str(item.get("since") or "")):
            row["filed"] = item["since"]
        charted.append(row)

    landed = []
    for item in dicts(summary, "landed"):
        ident = text(item.get("id"))
        if not ident:
            continue
        row = {
            "id": ident,
            "repo": repo_of(item),
            "what": text(item.get("title")) or ident,
            "owner": text(item.get("home_id")) or "fleet",
        }
        if HTTPS_RE.match(str(item.get("pr_url") or "")):
            row["pr_url"] = item["pr_url"]
        landed.append(row)

    counts = summary.get("counts") or {}
    fleet = (
        f"Fleet: {len(calls)} calls, {len(refused)} refused, {len(underway)} underway, "
        f"{counts.get('queued', len(charted))} queued, {len(landed)} landed"
    )
    published = summary.get("published_epoch")
    if isinstance(published, int):
        fleet += f"; summary published {iso(published)}"

    return {
        "schema": SCHEMA,
        "home": HOME,
        "generated": generated,
        "prs_live": False,
        "captains_call": [weekly_call(monday, audit, pace, fleet)] + calls,
        "underway": underway,
        "landed": landed,
        "charted": refused + charted,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the Monday page's board payload."
    )
    parser.add_argument(
        "--summary",
        default=os.environ.get(
            "MONDAY_PAGE_SUMMARY", "/var/lib/lifekit-fleet/home-summary.json"
        ),
    )
    parser.add_argument("--audit", default=os.environ.get("MONDAY_PAGE_AUDIT"))
    parser.add_argument("--pace", default=os.environ.get("MONDAY_PAGE_PACE"))
    args = parser.parse_args(argv)

    try:
        summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"monday-page: the fleet summary is unreadable: {exc}", file=sys.stderr)
        return 1
    if not isinstance(summary, dict):
        print("monday-page: the fleet summary is not a JSON object", file=sys.stderr)
        return 1

    now = int(
        os.environ.get("MONDAY_PAGE_NOW")
        or dt.datetime.now(dt.timezone.utc).timestamp()
    )
    payload = build(
        summary,
        read_audit(args.audit),
        read_pace(
            args.pace, os.environ.get("MONDAY_PAGE_FLEET_POLICY", "fleet-policy")
        ),
        now,
    )
    refused = [row for row in payload["charted"] if row["kind"] == "warning"]
    for row in refused:
        print(f"monday-page: {row['title']}: {row['reason']}", file=sys.stderr)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
