#!/usr/bin/env python3
"""fleet-status - the living status page's sections, for fleet-publisher.sh.

Prints one fleet-status-page.v1 JSON object to stdout, which the publisher puts
in the merged summary as `status_page` (contract: README.md "Status page
sections"). Everything is read from structured sources, never from prose:

  records       each on-host home's backlog, whole, through the home's own
                bin/fm-tasks-axi.sh (`list`, then `show --full` for a row whose
                cells the list truncated)
  projects      the records grouped by their `repo`, one group per project
                whatever spelling (short name, owner/name, FLEET_PLAN_MAP alias;
                none: the "no project" group), open PRs per repo from `gh`
                (cached), plan links
  capacity      each on-host home's bin/fm-capacity.sh --json, then each remote
                home (the PC) from the publisher's read of its cached summary
  queue         the open queued records, and the 7-day throughput from the
                fleet-metrics daily rows
  intake        the shared job queue's depth (`jobq.py depth` in the gateway)
  how_it_works  one owner page per workflow, linked only once it exists
  stalled       items whose state has not changed for their queue's threshold
                (fleet-stalled.json beside this script): GitHub issues of the
                registered projects, backlog items, jobq jobs, A2A threads
  problems      the three oldest stalled items by first-seen age, one brief line
  side_hustles  per-channel numbers from the clip stats feed (stats.jsonl), as
                the Monday/Thursday brief lines and the Monday week-sheet rows

Usage: fleet_status.py --home <id>=<path> [--home ...] --now <epoch>
         --state-dir <dir> [--daily <jsonl> ...] [--remote <json> ...]

Env: TASKS_AXI (absolute tasks-axi; its directory goes on PATH so the home's
wrapper finds it and node), CREDENTIAL_RE (a record whose text matches it is
withheld), FLEET_GH (default gh; empty skips open PRs), FLEET_PR_TTL (seconds,
default 600), FLEET_VAULT (default /srv/memory), FLEET_PLAN_MAP ("<repo>=<vault
project> ..." where the names differ), FLEET_INTAKE_CMD (overrides the gateway
`docker exec`; empty skips intake), FLEET_JOBS_CMD (the same for the job ages
the stalled check reads; empty skips them, unset skips them too when
FLEET_INTAKE_CMD is set),
FLEET_A2A_CMD (prints the open A2A threads as JSON; unset: none reachable),
FLEET_STALLED_CONFIG (the thresholds file, else fleet-stalled.json beside this
script), FLEET_CLIP_STATS_FEED (path of clip's stats.jsonl; unset: no
side_hustles section, an unreadable file: its "no data" line). A source that fails empties its own part
and adds a line to `errors`; the helper itself only fails on bad arguments.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import time
import zoneinfo
from pathlib import Path
from typing import Any

SCHEMA = "fleet-status-page.v1"
NO_PROJECT = "no project"
TOP_QUEUE = 10
THROUGHPUT_DAYS = 7
LIST_LIMIT = 1000
PR_BUDGET = 15
SHOW_BUDGET = 20
LIST_FIELDS = (
    "held,hold_kind,hold_until,hold_reason,priority,closed,links,blocked,created"
)
ISSUE_LIMIT = 200
TITLE_CUT = 160
STATUS_AT_RE = re.compile(r"\[at=(\d+)\]")
STATUS_TAIL = 65536
A2A_CLOSED = ("completed", "failed", "canceled", "rejected")
STALLED_CONFIG = ("fleet-stalled.json", "lifekit-fleet-stalled.json")
DECISIONS_CONFIG = ("fleet-decisions.json", "lifekit-fleet-decisions.json")
DECISION_FILE_RE = re.compile(r"^(\d{4})-.+\.md$")
ITEM_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$")
TRUNCATED = "\n... (truncated, "
TOON_TABLE_RE = re.compile(r"^tasks\[\d+\]\{([^}]*)\}:$")
TOON_FIELD_RE = re.compile(r"^  ([a-z_]+): (.*)$")
PR_LINK_RE = re.compile(r"pr:(https://github\.com/[^\s,;\"]+)")
GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Who restarts a hold, by its hold kind: the captain answers a call, the
# outside world ends an external wait, and firstmate's own parked holds (and
# any kind it does not know) restart on its date.
HOLD_OWNER = {"captain": "captain", "external": "external"}
GATEWAY_LABELS = (
    "label=com.docker.compose.project=openclaw",
    "label=com.docker.compose.service=openclaw-gateway",
)
GATEWAY_JOBQ = "/home/node/.openclaw/skills/job-queue/jobq.py"
# The workflows the page explains, each by its vault owner page. A page missing
# from the vault leaves the link out and lists it under `follow_ups`.
WORKFLOWS = (
    ("intake", "Reel and video intake", "system/workflows/intake.md"),
    (
        "queue-to-merge",
        "A task from queue to merge",
        "system/workflows/queue-to-merge.md",
    ),
    ("monday-loop", "The Monday loop", "system/workflows/monday-loop.md"),
)
# Repo spellings whose vault project is not their own name; FLEET_PLAN_MAP adds
# to and overrides these.
PLAN_MAP = {"vault": "memory"}


def run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 20) -> str:
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout, check=True
    ).stdout


# --- backlog records -----------------------------------------------------------


def toon_value(raw: str) -> str | None:
    """One TOON scalar as tasks-axi prints it: quoted JSON string, or bare; "-" is none."""
    raw = raw.strip()
    if raw.startswith('"'):
        raw = json.loads(raw)
    return None if raw in ("", "-") else raw


def split_row(line: str) -> list[str]:
    cells, cell, quoted, escaped = [], "", False, False
    for ch in line.strip():
        if escaped:
            cell, escaped = cell + ch, False
        elif ch == "\\" and quoted:
            cell, escaped = cell + ch, True
        elif ch == '"':
            cell, quoted = cell + ch, not quoted
        elif ch == "," and not quoted:
            cells.append(cell)
            cell = ""
        else:
            cell += ch
    cells.append(cell)
    return cells


def toon_rows(out: str) -> list[tuple[str, dict[str, str | None]]]:
    """(raw line, row) for each row of the `tasks[N]{cols}:` table `list` prints."""
    lines = out.splitlines()
    for at, line in enumerate(lines):
        if m := TOON_TABLE_RE.match(line):
            columns = m.group(1).split(",")
            break
    else:
        return []  # `list` prints no table when nothing matches
    rows = []
    for line in lines[at + 1 :]:
        if not line.startswith("  "):
            break
        cells = split_row(line)
        if len(cells) == len(columns):
            rows.append((line, dict(zip(columns, map(toon_value, cells)))))
    return rows


def toon_fields(out: str) -> dict[str, str | None]:
    """The `key: value` lines `tasks-axi show` prints under `task:`."""
    return {
        m.group(1): toon_value(m.group(2))
        for m in map(TOON_FIELD_RE.match, out.splitlines())
        if m
    }


def as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def record(home_id: str, row: dict[str, str | None]) -> dict[str, Any]:
    held = row.get("held") == "yes"
    state = row.get("state")
    links = row.get("links") or ""
    pr = PR_LINK_RE.search(links)
    item: dict[str, Any] = {
        "id": row.get("id"),
        "home_id": home_id,
        "title": row.get("title"),
        "state": "held" if held and state == "queued" else state,
        "repo": row.get("repo"),
        "kind": row.get("kind"),
        "priority": as_int(row.get("priority")),
        "blocked": row.get("blocked") == "yes",
        "closed": row.get("closed"),
        "pr_url": pr.group(1) if pr else None,
        "hold": None,
        "created": row.get("created"),
        "stalled": None,
    }
    if held:
        kind = row.get("hold_kind")
        item["hold"] = {
            "kind": kind,
            "owner": HOLD_OWNER.get(kind or "", "firstmate"),
            "until": row.get("hold_until"),
            "reason": row.get("hold_reason"),
        }
    return item


def read_records(
    home_id: str,
    home: str,
    tasks_axi: str,
    cache: dict[str, Any],
    now: int,
    errors: list[str],
    deadline: float,
) -> tuple[list[dict[str, Any]], int]:
    """The home's open backlog, whole, and the records done within the throughput
    window, with the count of cut rows left unresolved. A row whose cells the
    list truncated is read again with `show --full`, unless the cache holds the
    same row already resolved (titles rarely change, and each show costs a node
    start), and only until `deadline`: a cut row past it, or whose show fails,
    keeps the list's cells and is not cached."""
    wrapper = Path(home) / "bin/fm-tasks-axi.sh"
    if not os.access(wrapper, os.X_OK):
        raise OSError("no bin/fm-tasks-axi.sh")
    env = {**os.environ, "FM_HOME": home}
    if tasks_axi:
        env["PATH"] = f"{os.path.dirname(tasks_axi)}:{env.get('PATH', '')}"

    def axi(*args: str, timeout: int = 30) -> str:
        return run([str(wrapper), *args], env=env, timeout=timeout)

    rows = toon_rows(axi("list", "--limit", str(LIST_LIMIT), "--fields", LIST_FIELDS))
    if len(rows) >= LIST_LIMIT:
        errors.append(
            f"{home_id}: backlog list hit its {LIST_LIMIT}-row limit; records may be missing"
        )
    since = (
        (
            dt.datetime.fromtimestamp(now, dt.timezone.utc)
            - dt.timedelta(days=THROUGHPUT_DAYS)
        )
        .date()
        .isoformat()
    )
    records = []
    unresolved = 0
    for line, row in rows:
        if not row.get("id"):
            continue
        if row.get("state") == "done" and (row.get("closed") or "")[:10] < since:
            continue
        key = f"{home_id}\t{row['id']}"
        if any(TRUNCATED in (v or "") for v in row.values()):
            hit = cache.get(key)
            left = deadline - time.monotonic()
            if isinstance(hit, dict) and hit.get("line") == line:
                full = hit.get("fields") or {}
            elif left < 1:
                unresolved += 1
                records.append(record(home_id, row))
                continue
            else:
                try:
                    out = axi(
                        "show", str(row["id"]), "--full", timeout=min(30, int(left))
                    )
                except (OSError, subprocess.SubprocessError):
                    unresolved += 1
                    records.append(record(home_id, row))
                    continue
                shown = toon_fields(out)
                full = {
                    k: shown[k]
                    for k, v in row.items()
                    if TRUNCATED in (v or "") and shown.get(k) is not None
                }
            cache[key] = {"line": line, "fields": full}
            row = {**row, **full}
        records.append(record(home_id, row))
    return records, unresolved


# --- projects --------------------------------------------------------------------


def github_slug(remote: str) -> str | None:
    m = GITHUB_REMOTE_RE.match(remote.strip())
    return m.group(1) if m else None


def repo_slug(repo: str, homes: list[tuple[str, str]]) -> str | None:
    """A record's repo as owner/name: as written when it is one, else the origin
    of the first home's projects/<repo> clone."""
    if SLUG_RE.match(repo):
        return repo
    for _, home in homes:
        clone = Path(home) / "projects" / repo
        if not clone.is_dir():
            continue
        try:
            remote = run(
                ["git", "-C", str(clone), "remote", "get-url", "origin"], timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if slug := github_slug(remote):
            return slug
    return None


def open_prs(
    slugs: set[str], cache: dict[str, Any], now: int, errors: list[str]
) -> None:
    """Refresh cache[slug] = {count, as_of, issues} for slugs older than
    FLEET_PR_TTL (or cached before issues were read), within PR_BUDGET seconds in
    all: the open PR count, then the open issues the stalled check ages. A failed
    or skipped refresh keeps the last count and issues, so one bad minute never
    blanks a row."""
    gh = os.environ.get("FLEET_GH", "gh")
    if not gh:
        return
    ttl = int(os.environ.get("FLEET_PR_TTL") or 600)
    deadline = time.monotonic() + PR_BUDGET
    for slug in sorted(slugs):
        hit = cache.get(slug)
        if (
            isinstance(hit, dict)
            and "issues" in hit
            and now - int(hit.get("as_of") or 0) < ttl
        ):
            continue
        left = deadline - time.monotonic()
        if left < 1:
            errors.append("open PRs not refreshed: the time budget ran out")
            return
        try:
            out = run(
                [
                    gh,
                    "pr",
                    "list",
                    "-R",
                    slug,
                    "--state",
                    "open",
                    "--limit",
                    "200",
                    "--json",
                    "number",
                ],
                timeout=min(20, int(left)),
            )
            kept = hit if isinstance(hit, dict) else {}
            cache[slug] = {**kept, "count": len(json.loads(out)), "as_of": now}
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            errors.append(f"open PRs of {slug} unreadable ({exc.__class__.__name__})")
            continue
        left = deadline - time.monotonic()
        if left < 1:
            errors.append("open issues not refreshed: the time budget ran out")
            return
        try:
            out = run(
                [
                    gh,
                    "issue",
                    "list",
                    "-R",
                    slug,
                    "--state",
                    "open",
                    "--limit",
                    str(ISSUE_LIMIT),
                    "--json",
                    "number,title,url,updatedAt,labels",
                ],
                timeout=min(20, int(left)),
            )
            listed = json.loads(out)
            cache[slug]["issues"] = [i for i in map(slim_issue, listed) if i]
            if len(listed) >= ISSUE_LIMIT:
                errors.append(
                    f"open issues of {slug} hit the {ISSUE_LIMIT}-issue limit; some are not aged"
                )
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            errors.append(
                f"open issues of {slug} unreadable ({exc.__class__.__name__})"
            )


def slim_issue(raw: Any) -> dict[str, Any] | None:
    """The fields of one `gh issue list` row the stalled check needs; None for a
    row without a number and an update time."""
    if not isinstance(raw, dict):
        return None
    when = parse_time(raw.get("updatedAt"))
    if not isinstance(raw.get("number"), int) or when is None:
        return None
    labels = raw.get("labels") or []
    return {
        "number": raw["number"],
        "title": one_line(raw.get("title"), TITLE_CUT),
        "url": raw.get("url") if isinstance(raw.get("url"), str) else None,
        "updated": when,
        "labels": [
            str(label["name"]).lower()
            for label in labels
            if isinstance(label, dict) and label.get("name")
        ],
    }


def parse_time(value: Any) -> int | None:
    """An epoch from a number or an ISO 8601 time, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value:
        try:
            when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return int(when.timestamp())
    return None


def one_line(value: Any, cut: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text if len(text) <= cut else text[: cut - 1] + "…"


def vault_page_url(vault: Path, page: str) -> str | None:
    """The vault page's GitHub URL, when the page exists and the vault's origin
    is on GitHub."""
    if not (vault / page).is_file():
        return None
    try:
        remote = run(
            ["git", "-C", str(vault), "remote", "get-url", "origin"], timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    slug = github_slug(remote)
    return f"https://github.com/{slug}/blob/HEAD/{page}" if slug else None


def plan_map() -> dict[str, str]:
    pairs = (
        p.split("=", 1)
        for p in os.environ.get("FLEET_PLAN_MAP", "").split()
        if "=" in p
    )
    return PLAN_MAP | {repo: project for repo, project in pairs if repo and project}


def canonical_repos(
    repos: list[str], slugs: dict[str, str | None], mapped: dict[str, str]
) -> dict[str, str]:
    """Each spelling of a repo -> the one spelling its row is published under.

    A project is its name (the repo part of owner/name, or the short name as
    written), or the FLEET_PLAN_MAP entry when the repo has one - so `x`,
    `owner/x` and a mapped alias of either are one project. Two known slugs that
    differ (owner/x, other/x) stay apart; a spelling with no slug joins the
    project's only slug. The row is named by its owner/name spelling when one
    exists, else its alphabetically first short name."""
    keys: dict[str, tuple[str, str | None]] = {
        repo: (mapped.get(repo, repo.rsplit("/", 1)[-1]), slugs.get(repo))
        for repo in repos
    }
    for project in {k[0] for k in keys.values()}:
        known = {k[1] for k in keys.values() if k[0] == project and k[1]}
        if len(known) == 1:
            (only,) = known
            keys = {r: (p, only if p == project else s) for r, (p, s) in keys.items()}
    best: dict[tuple[str, str | None], str] = {}
    for repo in sorted(repos):
        key = keys[repo]
        held = best.get(key)
        if held is None or ("/" in repo) > ("/" in held):
            best[key] = repo
    return {repo: best[keys[repo]] for repo in repos}


def projects(
    records: list[dict[str, Any]],
    homes: list[tuple[str, str]],
    pr_cache: dict[str, Any],
    now: int,
    errors: list[str],
) -> list[dict[str, Any]]:
    spellings = sorted({r["repo"] for r in records if r["repo"]})
    spelt = {repo: repo_slug(repo, homes) for repo in spellings}
    mapped = plan_map()
    canon = canonical_repos(spellings, spelt, mapped)
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for r in records:
        groups.setdefault(canon.get(r["repo"]), []).append(r)
    slugs: dict[str, str | None] = {
        repo: next((spelt[s] for s in spellings if canon[s] == repo and spelt[s]), None)
        for repo in groups
        if repo
    }
    open_prs({s for s in slugs.values() if s}, pr_cache, now, errors)
    vault = Path(os.environ.get("FLEET_VAULT") or "/srv/memory")
    rows = []
    for repo, items in groups.items():
        done = [r for r in items if r["state"] == "done" and r["closed"]]
        last = max(done, key=lambda r: r["closed"], default=None)
        slug = slugs.get(repo) if repo else None
        prs = pr_cache.get(slug) if slug else None
        project = mapped.get(repo or "", (repo or "").rsplit("/", 1)[-1])
        rows.append(
            {
                "repo": repo,
                "name": repo or NO_PROJECT,
                "github": slug,
                "in_flight": sum(r["state"] == "in_flight" for r in items),
                "queued": sum(r["state"] == "queued" for r in items),
                "held": sum(r["state"] == "held" for r in items),
                "held_external": held_external(items),
                "last_landed": None
                if last is None
                else {
                    k: last[k] for k in ("id", "home_id", "title", "closed", "pr_url")
                },
                "open_prs": prs.get("count") if isinstance(prs, dict) else None,
                "open_prs_as_of": prs.get("as_of") if isinstance(prs, dict) else None,
                "plan_url": vault_page_url(vault, f"projects/{project}/plan.md")
                if repo
                else None,
            }
        )
    # Busiest first; "no project" last, so a missing repo stays visible but apart.
    rows.sort(
        key=lambda p: (p["repo"] is None, -(p["in_flight"] + p["queued"]), p["name"])
    )
    return rows


# --- capacity, queue, intake ----------------------------------------------------


def idle_reason(cap: dict[str, Any], queued: int | None, held: int) -> str | None:
    """Why a home runs fewer workers than it may, from the fixed list; None when
    nothing explains it (room and open work: it should dispatch). A home with no
    declared cap never reads as cap-reached; one whose queue is not known
    (queued None) gets no queue-based reason."""
    running, allowed = cap["running"], cap["allowed"]
    if allowed is not None and running >= allowed:
        return "cap-reached"
    if cap["quota"] == "exhausted_now":
        return "quota"
    if queued is None:
        return None
    if queued == 0:
        return "all-held" if held else "queue-empty"
    return None


def capacity_row(
    home_id: str, doc: Any, queued: int | None, held: int
) -> dict[str, Any]:
    """One capacity row from an fm-lane-capacity.v1 document. `allowed` is the
    declared cap; with none declared it is None and `cap_status` says "none"
    (the page shows "no cap declared"), never an invented count."""
    if not isinstance(doc, dict) or doc.get("schema") != "fm-lane-capacity.v1":
        raise ValueError("not an fm-lane-capacity.v1 document")
    lanes, cap, quota = doc.get("lanes") or {}, doc.get("cap") or {}, doc.get("quota")
    out = {
        "home_id": home_id,
        "machine": doc.get("machine"),
        "running": int(lanes.get("count") or 0),
        "allowed": cap.get("target") if cap.get("status") == "ok" else None,
        "cap_status": cap.get("status"),
        "quota": quota.get("runway") if isinstance(quota, dict) else None,
        "generated_epoch": doc.get("generated_epoch"),
        "idle_reason": None,
    }
    out["idle_reason"] = idle_reason(out, queued, held)
    return out


def capacity(
    home_id: str, home: str, records: list[dict[str, Any]] | None
) -> dict[str, Any]:
    script = Path(home) / "bin/fm-capacity.sh"
    doc = json.loads(
        run([str(script), "--json"], env={**os.environ, "FM_HOME": home}, timeout=30)
    )
    if records is None:  # an unreadable backlog explains nothing
        row = capacity_row(home_id, doc, None, 0)
        return {**row, "idle_reason": None}
    return capacity_row(
        home_id,
        doc,
        sum(r["state"] == "queued" for r in records),
        sum(r["state"] == "held" for r in records),
    )


def remote_capacity(entry: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    """A remote home's row (the PC), from what the publisher read of its cached
    summary: off is host-offline; on, the lane capacity its summary carries, or,
    when it carries none, capacity-unpublished rather than a row of nulls."""
    home_id = str(entry.get("id"))
    seen = entry.get("last_seen_epoch")
    blank = {
        "home_id": home_id,
        "machine": None,
        "running": None,
        "allowed": None,
        "cap_status": None,
        "quota": None,
        "generated_epoch": seen,
        "idle_reason": "host-offline",
    }
    if entry.get("state") == "off":
        return blank
    doc = entry.get("lane_capacity")
    if doc is not None:
        queued = entry.get("queued")
        try:
            return capacity_row(
                home_id,
                doc,
                queued if isinstance(queued, int) else None,
                entry.get("held") if isinstance(entry.get("held"), int) else 0,
            )
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append(
                f"{home_id}: published capacity unreadable ({exc.__class__.__name__})"
            )
    return {**blank, "idle_reason": "capacity-unpublished"}


def remote_entries(paths: list[str], errors: list[str]) -> list[dict[str, Any]]:
    entries = []
    for path in paths:
        try:
            doc = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            errors.append(f"remote homes unreadable ({exc.__class__.__name__})")
            continue
        if not isinstance(doc, list):
            errors.append("remote homes unreadable (not a list)")
            continue
        entries.extend(e for e in doc if isinstance(e, dict))
    return entries


def daily_rows(paths: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("date"), str):
                rows[(row["date"], str(row.get("home")))] = row
    return rows


def throughput(paths: list[str], now: int) -> dict[str, Any]:
    """Merged PRs and runs over the THROUGHPUT_DAYS whole UTC days before today."""
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    first = today - dt.timedelta(days=THROUGHPUT_DAYS)
    window = [
        r
        for (date, _), r in daily_rows(paths).items()
        if first.isoformat() <= date < today.isoformat()
    ]

    def total(key: str) -> int:
        return sum(r[key] for r in window if isinstance(r.get(key), int))

    return {
        "days": THROUGHPUT_DAYS,
        "from": first.isoformat(),
        "to": (today - dt.timedelta(days=1)).isoformat(),
        "days_with_data": len({r["date"] for r in window}),
        "merged_prs": total("merged_prs"),
        "runs": total("runs"),
    }


def held_external(records: list[dict[str, Any]]) -> int:
    """The held records waiting on the outside world: an externally blocked item
    is a state on the page, never a message to the owner."""
    return sum(
        r["state"] == "held" and (r["hold"] or {}).get("owner") == "external"
        for r in records
    )


def queue(records: list[dict[str, Any]], paths: list[str], now: int) -> dict[str, Any]:
    open_ = [r for r in records if r["state"] == "queued"]
    # The backlog's own order within a priority; unprioritised work last.
    ranked = sorted(
        enumerate(open_),
        key=lambda e: (e[1]["priority"] is None, e[1]["priority"] or 0, e[0]),
    )
    return {
        "count": len(open_),
        "held": sum(r["state"] == "held" for r in records),
        "held_external": held_external(records),
        "top": [
            {
                k: r[k]
                for k in (
                    "id",
                    "home_id",
                    "title",
                    "repo",
                    "priority",
                    "blocked",
                    "stalled",
                )
            }
            for _, r in ranked[:TOP_QUEUE]
        ],
        "throughput": throughput(paths, now),
    }


def intake() -> dict[str, Any]:
    cmd = os.environ.get("FLEET_INTAKE_CMD")
    if cmd is None:
        found = run(
            [
                "docker",
                "ps",
                "-q",
                *(a for f in GATEWAY_LABELS for a in ("--filter", f)),
            ],
            timeout=10,
        ).split()
        if not found:
            raise OSError("no running openclaw-gateway container")
        argv = ["docker", "exec", found[0], "python3", GATEWAY_JOBQ, "depth"]
    else:
        argv = shlex.split(cmd)
    doc = json.loads(run(argv, timeout=15))
    keys = ("waiting", "running", "done_24h", "failed_24h")
    if not isinstance(doc, dict) or not all(
        isinstance(doc.get(k), int) and doc[k] >= 0 for k in keys
    ):
        raise ValueError("not a jobq depth document")
    return {k: doc[k] for k in keys}


# --- stalled ---------------------------------------------------------------------
#
# One mechanical check, no model: an item whose state has not changed for its
# queue's threshold is stalled, and carries its owner. The thresholds are the
# fleet-stalled.json beside this script, the one source (the dashboard reads
# the result, never decides).


def stalled_config(errors: list[str]) -> dict[str, Any] | None:
    """The thresholds, or None (with an `errors` line) when the file is missing
    or malformed: nothing is then marked, never guessed."""
    here = Path(__file__).resolve().parent
    named = os.environ.get("FLEET_STALLED_CONFIG")
    paths = [Path(named)] if named else [here / n for n in STALLED_CONFIG]
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        errors.append("stalled thresholds not found: no item is marked stalled")
        return None
    try:
        cfg = json.loads(path.read_text())
        owners = cfg["owners"]
        github, backlog = cfg["github"], cfg["backlog"]
        jobq, a2a = cfg["jobq"], cfg["a2a"]
        numbers = (
            github["days"],
            backlog["hours"],
            jobq["waiting_hours"],
            a2a["idle_hours"],
        )
        assert isinstance(owners, list) and owners
        assert all(isinstance(o, str) and o for o in owners)
        assert "firstmate" in owners and github["default_owner"] in owners
        assert all(
            isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0
            for n in numbers
        )
        assert isinstance(github["held_labels"], list)
        assert isinstance(github["repo_owners"], dict)
        assert set(github["repo_owners"].values()) <= set(owners)
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        errors.append(f"stalled thresholds in {path.name} unreadable: nothing marked")
        return None
    return cfg


def stall(since: int, threshold: float, owner: str) -> dict[str, Any]:
    return {"since": int(since), "threshold": int(threshold), "owner": owner}


def last_status_epoch(home: str, rec_id: str) -> int | None:
    """The newest `[at=<epoch>]` in the task's status file under the home's
    state/, from the file's tail; None when it has none."""
    path = Path(home) / "state" / f"{rec_id}.status"
    if "/" in rec_id or not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - STATUS_TAIL))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = STATUS_AT_RE.findall(tail)
    return int(found[-1]) if found else None


def backlog_stalls(
    records: list[dict[str, Any]],
    homes: list[tuple[str, str]],
    cfg: dict[str, Any],
    now: int,
) -> list[dict[str, Any]]:
    """Queued or in-flight records with no status line (or, for one that has
    none yet, no creation) inside the threshold. Held and done records keep
    their own buckets. Marks each record's `stalled` and returns the rows."""
    threshold = cfg["backlog"]["hours"] * 3600
    paths = dict(homes)
    rows = []
    for rec in records:
        if rec["state"] not in ("queued", "in_flight") or rec["home_id"] not in paths:
            continue
        created = parse_time(f"{rec['created']}T00:00:00Z") if rec["created"] else None
        seen = [
            t
            for t in (created, last_status_epoch(paths[rec["home_id"]], rec["id"]))
            if t is not None
        ]
        if not seen or now - max(seen) < threshold:
            continue
        rec["stalled"] = stall(max(seen), threshold, "firstmate")
        rows.append(
            {
                "source": "backlog",
                "id": rec["id"],
                "home_id": rec["home_id"],
                "title": rec["title"],
                "url": None,
                "reason": "no-status-line",
                **{k: rec["stalled"][k] for k in ("since", "threshold")},
                "owner": "firstmate",
            }
        )
    return rows


def issue_owner(slug: str, labels: list[str], cfg: dict[str, Any]) -> str:
    """The issue's class label (`ops` or `class:ops`) when it is an owner, else
    the repo's configured owner, else the default."""
    for label in labels:
        name = label.removeprefix("class:")
        if name in cfg["owners"]:
            return name
    return cfg["github"]["repo_owners"].get(slug) or cfg["github"]["default_owner"]


def issue_stalls(
    slugs: set[str], pr_cache: dict[str, Any], cfg: dict[str, Any], now: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Open, unheld issues of each registered project with no update inside the
    threshold, from the issues the open-PR refresh read (so within its budget)."""
    gh = cfg["github"]
    threshold = gh["days"] * 86400
    held = {label.lower() for label in gh["held_labels"]}
    rows, missing = [], []
    for slug in sorted(slugs):
        hit = pr_cache.get(slug)
        issues = hit.get("issues") if isinstance(hit, dict) else None
        if not isinstance(issues, list):
            missing.append(slug)
            continue
        for issue in issues:
            if held & set(issue["labels"]) or now - issue["updated"] < threshold:
                continue
            rows.append(
                {
                    "source": "issue",
                    "id": f"{slug}#{issue['number']}",
                    "home_id": None,
                    "title": issue["title"],
                    "url": issue["url"],
                    "reason": "no-update",
                    "since": issue["updated"],
                    "threshold": int(threshold),
                    "owner": issue_owner(slug, issue["labels"], cfg),
                }
            )
    if not os.environ.get("FLEET_GH", "gh"):
        source = {"read": False, "reason": "FLEET_GH is empty"}
    elif not slugs:
        source = {"read": False, "reason": "no registered project has a GitHub repo"}
    elif missing:
        source = {"read": False, "reason": f"issues not read for {', '.join(missing)}"}
    else:
        source = {"read": True, "reason": None}
    return rows, source


def jobs_cmd() -> list[str] | None:
    """FLEET_JOBS_CMD as argv; [] for the gateway default; None to skip. Unset, it
    is the default only while FLEET_INTAKE_CMD is unset too: an overridden intake
    command means the gateway is not the source here."""
    cmd = os.environ.get("FLEET_JOBS_CMD")
    if cmd is None:
        return [] if "FLEET_INTAKE_CMD" not in os.environ else None
    return shlex.split(cmd) or None


def job_stalls(
    cfg: dict[str, Any], now: int, errors: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Jobs whose attempts are used up, or that have waited past the threshold,
    from `jobq.py ages` (ids, kinds, states and times: never a label or result)."""
    argv = jobs_cmd()
    if argv is None:
        return [], {"read": False, "reason": "job ages skipped"}
    try:
        if not argv:
            found = run(
                [
                    "docker",
                    "ps",
                    "-q",
                    *(a for f in GATEWAY_LABELS for a in ("--filter", f)),
                ],
                timeout=10,
            ).split()
            if not found:
                raise OSError("no running openclaw-gateway container")
            argv = ["docker", "exec", found[0], "python3", GATEWAY_JOBQ, "ages"]
        doc = json.loads(run(argv, timeout=15))
        jobs, limit = doc["jobs"], doc["max_attempts"]
        assert isinstance(jobs, list) and isinstance(limit, int)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AssertionError,
        subprocess.SubprocessError,
    ) as exc:
        errors.append(f"job ages unreadable ({exc.__class__.__name__})")
        return [], {"read": False, "reason": "job ages unreadable"}
    threshold = cfg["jobq"]["waiting_hours"] * 3600
    owners = cfg["owners"]
    rows = []
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            continue
        status, attempts = job.get("status"), job.get("attempts")
        queued, finished = job.get("queued_at"), job.get("finished_at")
        if not all(isinstance(n, (int, float)) for n in (attempts, queued)):
            continue
        klass = job.get("class")
        owner = klass if klass in owners and klass != "unclear" else "firstmate"
        if status in ("queued", "failed") and attempts >= limit:
            since, reason, cut = finished or queued, "attempts-exhausted", 0
        elif status == "queued" and now - queued >= threshold:
            since, reason, cut = queued, "waiting", threshold
        else:
            continue
        rows.append(
            {
                "source": "job",
                "id": job["id"],
                "home_id": None,
                "title": one_line(job.get("kind"), TITLE_CUT),
                "url": None,
                "reason": reason,
                "since": int(since),
                "threshold": int(cut),
                "owner": owner,
            }
        )
    return rows, {"read": True, "reason": None}


def thread_stalls(
    cfg: dict[str, Any], now: int, errors: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Open A2A threads past their own deadline, or with no turn inside the
    threshold, from FLEET_A2A_CMD (a JSON list of `{thread, owner, deadline,
    last_turn, state}`). With no such source the section says so: a thread's age
    is never guessed."""
    argv = shlex.split(os.environ.get("FLEET_A2A_CMD") or "")
    if not argv:
        return [], {
            "read": False,
            "reason": "no relay or session records reachable",
        }
    try:
        doc = json.loads(run(argv, timeout=15))
        assert isinstance(doc, list)
    except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
        errors.append(f"A2A threads unreadable ({exc.__class__.__name__})")
        return [], {"read": False, "reason": "A2A threads unreadable"}
    idle = cfg["a2a"]["idle_hours"] * 3600
    owners = cfg["owners"]
    rows = []
    for t in doc:
        if not isinstance(t, dict) or not isinstance(t.get("thread"), str):
            continue
        if t.get("state") in A2A_CLOSED:
            continue
        deadline, turn = parse_time(t.get("deadline")), parse_time(t.get("last_turn"))
        if deadline is not None and now > deadline:
            since, reason, cut = deadline, "past-deadline", 0
        elif turn is not None and now - turn >= idle:
            since, reason, cut = turn, "no-turn", idle
        else:
            continue
        owner = t.get("owner") if t.get("owner") in owners else "firstmate"
        rows.append(
            {
                "source": "thread",
                "id": t["thread"],
                "home_id": None,
                "title": one_line(t.get("title"), TITLE_CUT),
                "url": None,
                "reason": reason,
                "since": int(since),
                "threshold": int(cut),
                "owner": owner,
            }
        )
    return rows, {"read": True, "reason": None}


def stalled_section(
    rows: list[dict[str, Any]], owners: list[str]
) -> list[dict[str, Any]]:
    """The stalled rows grouped by owner, one group per configured owner (a
    count of 0 is shown), the oldest first inside a group."""
    groups = []
    for owner in owners:
        items = sorted(
            (r for r in rows if r["owner"] == owner),
            key=lambda r: (r["since"], r["source"], r["id"]),
        )
        groups.append(
            {
                "owner": owner,
                "count": len(items),
                "items": [{k: v for k, v in r.items() if k != "owner"} for r in items],
            }
        )
    return groups


# --- problems --------------------------------------------------------------------

PROBLEMS_SHOWN = 3
PROBLEM_TITLE_CUT = 50
PROBLEM_SOURCE_KEY = {
    "backlog": "backlog",
    "issue": "github",
    "job": "jobq",
    "thread": "a2a",
}
PROBLEM_PHRASE = {
    "no-status-line": "silent",
    "no-update": "no update",
    "attempts-exhausted": "job failed",
    "waiting": "job waiting",
    "past-deadline": "past deadline",
    "no-turn": "thread quiet",
}


def problem_line(shown: list[dict[str, Any]], more: int) -> str:
    """The one brief line: `Problems: <what> (<owner>, <n>d); ...; +<N> more`."""
    parts = [f"{p['label']} ({p['owner']}, {p['age_days']}d)" for p in shown]
    if more:
        parts.append(f"+{more} more")
    return "Problems: " + "; ".join(parts)


def problems(
    rows: list[dict[str, Any]] | None,
    sources: dict[str, Any] | None,
    state: Path,
    now: int,
    errors: list[str],
) -> dict[str, Any] | None:
    """The problems of the day, mechanically, from the stalled rows (red CI is
    not read by the publisher, so it is not here): `{count, shown, more, line}`
    with the three oldest first, or None when there is none.

    A problem's age counts from the first run that saw it. The first-seen mark
    lives in this publisher's state (`problems-seen.json`) and is dropped only
    when a run reads the problem's source and no longer finds it, so the age
    resets only when the problem clears; a source that could not be read this
    run keeps its marks. Unreadable stalled thresholds or mark file: None
    (and an `errors[]` line from its own reader or here), never a guess."""
    if rows is None:
        return None
    path = state / "problems-seen.json"
    try:
        marks = json.loads(path.read_text()) if path.exists() else {}
        assert isinstance(marks, dict)
    except (OSError, ValueError, AssertionError):
        errors.append("problems-seen.json unreadable: no problems line")
        return None
    current = {f"{r['source']}:{r.get('home_id')}:{r['id']}": r for r in rows}
    kept: dict[str, Any] = {}
    for key, mark in marks.items():
        source = key.split(":", 1)[0]
        read = ((sources or {}).get(PROBLEM_SOURCE_KEY.get(source, ""), {})).get("read")
        if key in current or not read:
            kept[key] = mark
    for key in current:
        mark = kept.get(key)
        if not isinstance(mark, dict) or not isinstance(mark.get("first_seen"), int):
            kept[key] = {"first_seen": now}
    if kept != marks:
        save_cache(path, kept)
    found = []
    for key, row in current.items():
        first = kept[key]["first_seen"]
        title = one_line(row.get("title"), PROBLEM_TITLE_CUT) or row["id"]
        found.append(
            {
                "source": row["source"],
                "id": row["id"],
                "home_id": row.get("home_id"),
                "owner": row["owner"],
                "reason": row["reason"],
                "label": f"{title} {PROBLEM_PHRASE.get(row['reason'], 'stalled')}",
                "url": row.get("url"),
                "first_seen": first,
                "age_days": max(0, now - first) // 86400,
            }
        )
    if not found:
        return None
    found.sort(key=lambda p: (p["first_seen"], p["source"], p["id"]))
    shown, more = found[:PROBLEMS_SHOWN], max(0, len(found) - PROBLEMS_SHOWN)
    return {
        "count": len(found),
        "shown": shown,
        "more": more,
        "line": problem_line(shown, more),
    }


# --- side hustles ----------------------------------------------------------------

SIDE_SCHEMA = "fleet-side-hustles.v1"
SIDE_BRIEF_DAYS = (0, 3)  # Monday and Thursday, in the brief's time zone
SIDE_WEEK_SHEET_DAY = 0
SIDE_WINDOW = 7 * 86400
SIDE_LINES = 3  # brief lines at most: the brief keeps its 12-line cap
NO_DATA = "no data"
CHANNEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SIDE_CLIP_CUT = 40


def feed_epoch(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        when = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return int(when.timestamp())


def feed_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def needs_consent(row: dict[str, Any]) -> bool:
    """The latest channel row says the YouTube Analytics scope is rejected or
    expired: clip's own `needs_reconsent`, or its credential-rejected error."""
    if row.get("status") == "needs_reconsent":
        return True
    return row.get("status") == "error" and "clip auth" in str(row.get("reason") or "")


def side_hustle_numbers(
    chan_rows: list[dict[str, Any]],
    video_rows: list[dict[str, Any]],
    now: int,
    pattern: re.Pattern[str] | None,
) -> dict[str, Any]:
    """The four numbers of one channel from its rows, oldest first, or a
    `no_data` / `needs_consent` verdict. Never a zero for a missing number."""
    latest = chan_rows[-1]
    at = feed_epoch(latest.get("at"))
    if needs_consent(latest):
        return {"status": "needs_consent"}
    week = latest.get("days7")
    if (
        latest.get("status") != "ok"
        or at is None
        or now - at > SIDE_WINDOW
        or not isinstance(week, dict)
    ):
        return {"status": "no_data"}
    views = feed_int(week.get("views"))
    subscribers = feed_int(latest.get("subscribers"))
    uploads = None
    now_count = feed_int(latest.get("videos_recorded"))
    # Uploads in the period: the videos clip has recorded now, less the count
    # at the last row from at least a week earlier. No such row: no data.
    base = [
        r
        for r in chan_rows[:-1]
        if (e := feed_epoch(r.get("at"))) is not None and at - e >= SIDE_WINDOW
    ]
    base_count = feed_int(base[-1].get("videos_recorded")) if base else None
    if now_count is not None and base_count is not None and now_count >= base_count:
        uploads = now_count - base_count
    best = None
    for r in video_rows:
        v = feed_int(r.get("views"))
        if r.get("at") != latest.get("at") or r.get("found") is not True or v is None:
            continue
        clip = one_line(r.get("clip"), SIDE_CLIP_CUT)
        if (
            clip
            and not (pattern and pattern.search(clip))
            and (best is None or v > best[1])
        ):
            best = (clip, v)
    return {
        "status": "ok",
        "views_7d": views,
        "subscribers": subscribers,
        "uploads": uploads,
        "best_short": {"clip": best[0], "views": best[1]} if best else None,
    }


def side_hustle_row(name: str, week_of: str, n: dict[str, Any]) -> dict[str, Any]:
    """One weekly sheet row: the numbers, each a number or "no data"."""
    row: dict[str, Any] = {"channel": name, "week_of": week_of, "status": n["status"]}
    if n["status"] == "ok":
        for k in ("views_7d", "subscribers", "uploads"):
            row[k] = NO_DATA if n[k] is None else n[k]
        best = n["best_short"]
        row["best_short"] = NO_DATA if best is None else best
    return row


def side_hustle_cells(n: dict[str, Any]) -> list[str]:
    def num(v: int | None) -> str:
        return NO_DATA if v is None else f"{v:,}"

    best = n["best_short"]
    best_text = NO_DATA if best is None else f"{best['clip']} ({best['views']:,} views)"
    return [num(n["views_7d"]), num(n["subscribers"]), num(n["uploads"]), best_text]


def side_hustles(
    now: int, tz: str, errors: list[str], pattern: re.Pattern[str] | None
) -> dict[str, Any] | None:
    """Per-channel side-hustle numbers from clip's stats feed, read-only and
    only from that feed (`FLEET_CLIP_STATS_FEED`). None when it is not set.

    `channels` always holds the verdicts; `lines` (the brief's, on Monday and
    Thursday only) and `week_rows` (Monday only) are what the surfaces print.
    A channel whose Analytics scope needs consent again gets no numbers: one
    shared credential line names them. An unreadable feed gives one no-data line."""
    feed = os.environ.get("FLEET_CLIP_STATS_FEED", "")
    if not feed:
        return None
    zone = zoneinfo.ZoneInfo(tz)
    local = dt.datetime.fromtimestamp(now, zone)
    brief_day = local.weekday() in SIDE_BRIEF_DAYS
    sheet_day = local.weekday() == SIDE_WEEK_SHEET_DAY
    week_of = (local.date() - dt.timedelta(days=local.weekday())).isoformat()
    out: dict[str, Any] = {
        "schema": SIDE_SCHEMA,
        "feed_read": True,
        "brief_day": brief_day,
        "week_of": week_of,
        "channels": [],
        "credential": None,
        "lines": [],
        "week_rows": [],
    }
    chans: dict[str, list[dict[str, Any]]] = {}
    videos: dict[str, list[dict[str, Any]]] = {}
    try:
        text = Path(feed).read_text()
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"clip stats feed unreadable ({exc.__class__.__name__})")
        out["feed_read"] = False
        if brief_day:
            out["lines"] = [f"Side hustles: {NO_DATA} (stats feed unreadable)"]
        if sheet_day:
            out["week_rows"] = [
                {"channel": "all channels", "week_of": week_of, "status": "no_data"}
            ]
        return out
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        name = row.get("channel") if isinstance(row, dict) else None
        if not isinstance(name, str) or not CHANNEL_RE.match(name):
            continue
        if row.get("kind") == "channel":
            chans.setdefault(name, []).append(row)
        elif row.get("kind") == "video":
            videos.setdefault(name, []).append(row)
    consent: list[str] = []
    lines: list[str] = []
    for name in sorted(chans):
        n = side_hustle_numbers(chans[name], videos.get(name, []), now, pattern)
        out["channels"].append({"channel": name, **n})
        cells = side_hustle_cells(n) if n["status"] == "ok" else None
        if n["status"] == "needs_consent":
            consent.append(name)
        elif cells is None:
            lines.append(f"{name}: {NO_DATA}")
        else:
            views, subs, uploads, best = cells
            lines.append(
                f"{name}: 7d views {views} · subs {subs} · uploads {uploads} · best Short {best}"
            )
        if sheet_day:
            out["week_rows"].append(side_hustle_row(name, week_of, n))
    if consent:
        out["credential"] = (
            "Side hustles: YouTube Analytics needs consent again for "
            + ", ".join(consent)
            + " - owner: run `clip auth --channel <name>` on the PC; no numbers shown"
        )
        lines.append(out["credential"])
    if not chans:
        lines = [f"Side hustles: {NO_DATA} (no channel rows in the stats feed)"]
    if len(lines) > SIDE_LINES:
        lines = [
            *lines[: SIDE_LINES - 1],
            f"+{len(lines) - SIDE_LINES + 1} more channels in the week sheet",
        ]
    if brief_day:
        out["lines"] = lines
    return out


# --- assembly --------------------------------------------------------------------


def decisions_config(errors: list[str]) -> dict[str, Any]:
    """The decision follow-through config (brief time zone and hour, and the
    items of records that do not declare their own), or defaults when absent."""
    here = Path(__file__).resolve().parent
    named = os.environ.get("FLEET_DECISIONS_CONFIG")
    paths = [Path(named)] if named else [here / n for n in DECISIONS_CONFIG]
    path = next((p for p in paths if p.is_file()), None)
    cfg: dict[str, Any] = {"brief": {"tz": "UTC", "at": "08:30"}, "backfill": {}}
    if path is None:
        return cfg
    try:
        doc = json.loads(path.read_text())
        brief, backfill = doc["brief"], doc["backfill"]
        dt.datetime.strptime(brief["at"], "%H:%M")
        zoneinfo.ZoneInfo(brief["tz"])
        assert isinstance(backfill, dict)
        assert all(
            isinstance(v, list) and all(ITEM_RE.match(str(i)) for i in v)
            for v in backfill.values()
        )
    except (OSError, ValueError, KeyError, TypeError, AssertionError, LookupError):
        errors.append(f"decision config {path.name} unreadable: defaults used")
        return cfg
    return {"brief": brief, "backfill": backfill}


def record_fields(text: str) -> tuple[dict[str, str], str]:
    """A decision record's frontmatter (flat `key: value` lines) and its title."""
    fields: dict[str, str] = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip().strip("\"'")
    title = next(
        (m.group(1) for ln in lines if (m := re.match(r"^# \d{4} - (.+)$", ln))), ""
    )
    return fields, title


def record_items(fields: dict[str, str], backfill: list[str] | None) -> list[str]:
    """The record's own `items: [owner/repo#N, ...]` when it declares them,
    else the config's list; empty (never a guess) when neither has any."""
    raw = fields.get("items", "")
    if raw.startswith("[") and raw.endswith("]"):
        own = [i.strip().strip("\"'") for i in raw[1:-1].split(",") if i.strip()]
        if own and all(ITEM_RE.match(i) for i in own):
            return own
    return list(backfill or [])


def issue_state(
    ref: str, cache: dict[str, Any], now: int, errors: list[str]
) -> dict[str, Any] | None:
    """{state, title, url, closed_at} of one issue; a closed one is cached for
    good, an open or unread one is looked up again after FLEET_PR_TTL seconds.
    None when gh could not say (the item then counts as not closed)."""
    hit = cache.get(ref)
    ttl = int(os.environ.get("FLEET_PR_TTL") or 600)
    if isinstance(hit, dict) and (
        hit.get("state") == "CLOSED" or now - int(hit.get("as_of") or 0) < ttl
    ):
        return hit
    gh = os.environ.get("FLEET_GH", "gh")
    if not gh:
        return hit if isinstance(hit, dict) else None
    repo, _, number = ref.partition("#")
    try:
        out = run(
            [
                *shlex.split(gh),
                "issue",
                "view",
                number,
                "-R",
                repo,
                "--json",
                "state,title,url,closedAt",
            ],
            timeout=15,
        )
        raw = json.loads(out)
        got = {
            "state": str(raw["state"]).upper(),
            "title": one_line(raw.get("title"), TITLE_CUT) or ref,
            "url": raw.get("url")
            if str(raw.get("url", "")).startswith("https://")
            else None,
            "closed_at": raw.get("closedAt"),
            "as_of": now,
        }
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        errors.append(f"decision item {ref} unreadable")
        return hit if isinstance(hit, dict) else None
    cache[ref] = got
    return got


def next_brief_day(epoch: int, brief: dict[str, Any]) -> dt.date:
    """The date of the first brief that runs after `epoch`."""
    zone = zoneinfo.ZoneInfo(brief["tz"])
    when = dt.datetime.fromtimestamp(epoch, zone)
    hour, minute = (int(x) for x in brief["at"].split(":"))
    day = when.date()
    if (when.hour, when.minute) >= (hour, minute):
        day += dt.timedelta(days=1)
    return day


def outcome_lines(
    fields: dict[str, str], title: str, issues: list[dict[str, Any]]
) -> list[str]:
    """The 3-line outcome, composed from the record and its closed items only."""
    names = [i["title"] for i in issues]
    shown = "; ".join(names[:3]) + (
        f"; +{len(names) - 3} more" if len(names) > 3 else ""
    )
    name = title or fields.get("name", "decision")
    changed = fields.get("summary") or "the decision's chosen option is in force."
    left = (
        fields.get("still_open")
        or "nothing from this decision; follow-ups are filed as their own items."
    )
    return [
        one_line(
            f"Shipped ({name}): {len(issues)} of {len(issues)} items closed - {shown}.",
            240,
        )
        or "",
        one_line(f"Changed: {changed}", 240) or "",
        one_line(f"Still open: {left}", 240) or "",
    ]


def delivered(
    state: Path, now: int, errors: list[str], vault: Path, gh_cache: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """Mark every accepted decision record whose items are all closed as
    delivered, once, and return all delivered outcomes (oldest first).

    The delivered mark lives in this publisher's state (`decisions-delivered.json`),
    never in the vault, and an outcome is composed the run it is marked and kept
    verbatim, so a delivered decision is never posted again and its text never
    drifts. `brief_date` is the one daily brief that carries it: the first brief
    after delivery with no other outcome, so a brief never holds more than one.
    An unreadable mark file posts nothing (None) rather than risk a second post."""
    path = state / "decisions-delivered.json"
    try:
        marks = json.loads(path.read_text()) if path.exists() else {}
        assert isinstance(marks, dict)
    except (OSError, ValueError, AssertionError):
        errors.append("decisions-delivered.json unreadable: no outcome posted")
        return None
    cfg = decisions_config(errors)
    folder = vault / "system" / "decisions"
    try:
        files = sorted(f for f in folder.iterdir() if DECISION_FILE_RE.match(f.name))
    except OSError:
        files = []
    changed = False
    for f in files:
        did = f.name[:4]
        if did in marks:
            continue
        try:
            fields, title = record_fields(f.read_text())
        except OSError:
            continue
        if fields.get("status") != "accepted":
            continue
        items = record_items(fields, cfg["backfill"].get(did))
        if not items:
            continue
        got = [issue_state(i, gh_cache, now, errors) for i in items]
        if not all(g and g["state"] == "CLOSED" for g in got):
            continue
        issues = [g for g in got if g]
        taken = {m.get("brief_date") for m in marks.values()}
        day = next_brief_day(now, cfg["brief"])
        while day.isoformat() in taken:
            day += dt.timedelta(days=1)
        marks[did] = {
            "id": did,
            "title": title or fields.get("name", did),
            "delivered_epoch": now,
            "date": dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat(),
            "brief_date": day.isoformat(),
            "lines": outcome_lines(fields, title, issues),
            "items": [
                {
                    "ref": r,
                    "title": g["title"],
                    "url": g["url"],
                    "closed_at": g["closed_at"],
                }
                for r, g in zip(items, issues)
            ],
        }
        changed = True
    if changed:
        save_cache(path, marks)
        try:
            marks = json.loads(path.read_text())
        except (OSError, ValueError):
            errors.append("decisions-delivered.json not saved: outcome may repeat")
    return sorted(
        marks.values(), key=lambda m: (m.get("delivered_epoch", 0), m.get("id", ""))
    )


def load_cache(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_cache(path: Path, doc: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, sort_keys=True))
        tmp.replace(path)
    except OSError as exc:
        print(f"fleet-status: cache {path.name} not saved: {exc}", file=sys.stderr)


def withheld(item: dict[str, Any], pattern: re.Pattern[str] | None) -> bool:
    return bool(pattern and pattern.search(json.dumps(item)))


def build(args: argparse.Namespace) -> dict[str, Any]:
    homes: list[tuple[str, str]] = args.home
    tasks_axi = os.environ.get("TASKS_AXI", "")
    raw_re = os.environ.get("CREDENTIAL_RE")
    pattern = re.compile(raw_re) if raw_re else None
    state = Path(args.state_dir)
    record_cache = load_cache(state / "status-records.json")
    pr_cache = load_cache(state / "status-open-prs.json")
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    by_home: dict[str, list[dict[str, Any]] | None] = {}
    kept = unresolved = 0
    show_deadline = time.monotonic() + SHOW_BUDGET
    for home_id, home in homes:
        try:
            got, cut = read_records(
                home_id, home, tasks_axi, record_cache, args.now, errors, show_deadline
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(
                f"{home_id}: backlog unreadable ({exc.__class__.__name__}: {exc})"[:300]
            )
            by_home[home_id] = None
            continue
        unresolved += cut
        safe = [r for r in got if not withheld(r, pattern)]
        kept += len(got) - len(safe)
        by_home[home_id] = safe
        records.extend(safe)
    if unresolved:
        errors.append(
            f"{unresolved} cut record(s) not shown in full: show failed or ran out of time"
        )
    if kept:
        errors.append(
            f"{kept} record(s) withheld: they look like they hold a credential"
        )
    # Ids no longer listed drop out of the cache; an unreadable home keeps its own.
    listed = {f"{r['home_id']}\t{r['id']}" for r in records}
    record_cache = {
        k: v
        for k, v in record_cache.items()
        if k in listed or by_home.get(k.split("\t", 1)[0], []) is None
    }
    caps = []
    for home_id, home in homes:
        try:
            caps.append(capacity(home_id, home, by_home.get(home_id)))
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            errors.append(f"{home_id}: capacity unreadable ({exc.__class__.__name__})")
    caps.extend(remote_capacity(e, errors) for e in remote_entries(args.remote, errors))
    rows = projects(records, homes, pr_cache, args.now, errors)
    stalled, sources, stalled_rows = None, None, None
    if cfg := stalled_config(errors):
        found = backlog_stalls(records, homes, cfg, args.now)
        issues, github = issue_stalls(
            {r["github"] for r in rows if r["github"]}, pr_cache, cfg, args.now
        )
        jobs, jobq = job_stalls(cfg, args.now, errors)
        threads, a2a = thread_stalls(cfg, args.now, errors)
        stalled_rows = found + issues + jobs + threads
        stalled = stalled_section(stalled_rows, cfg["owners"])
        sources = {
            "backlog": {
                "read": all(v is not None for v in by_home.values()),
                "reason": None
                if all(v is not None for v in by_home.values())
                else "a home's backlog is unreadable",
            },
            "github": github,
            "jobq": jobq,
            "a2a": a2a,
        }
    depth: dict[str, Any] | None = None
    if os.environ.get("FLEET_INTAKE_CMD") != "":
        try:
            depth = intake()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"intake depth unreadable ({exc.__class__.__name__})")
    vault = Path(os.environ.get("FLEET_VAULT") or "/srv/memory")
    how = []
    for wid, title, page in WORKFLOWS:
        url = vault_page_url(vault, page) if page else None
        how.append({"id": wid, "title": title, "url": url})
    decided_cache = load_cache(state / "status-decisions.json")
    outcomes = delivered(state, args.now, errors, vault, decided_cache)
    save_cache(state / "status-decisions.json", decided_cache)
    problem_list = problems(stalled_rows, sources, state, args.now, errors)
    side = side_hustles(
        args.now, decisions_config(errors)["brief"]["tz"], errors, pattern
    )
    save_cache(state / "status-records.json", record_cache)
    save_cache(state / "status-open-prs.json", pr_cache)
    return {
        "schema": SCHEMA,
        "generated_epoch": args.now,
        "records": records,
        "projects": rows,
        "capacity": caps,
        "queue": queue(records, args.daily, args.now),
        "intake": depth,
        "how_it_works": how,
        "stalled": stalled,
        "stalled_sources": sources,
        "delivered": outcomes,
        "problems": problem_list,
        "side_hustles": side,
        "follow_ups": [
            f"owner page for {h['title'].lower()}" for h in how if not h["url"]
        ],
        "errors": errors,
    }


def home_arg(value: str) -> tuple[str, str]:
    home_id, sep, path = value.partition("=")
    if not sep or not home_id or not path.startswith("/"):
        raise argparse.ArgumentTypeError(
            f"--home wants <id>=<absolute path>, not {value!r}"
        )
    return home_id, path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=home_arg, action="append", default=[])
    parser.add_argument("--now", type=int, required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--daily", action="append", default=[])
    parser.add_argument("--remote", action="append", default=[])
    args = parser.parse_args(argv)
    json.dump(build(args), sys.stdout, separators=(",", ":"))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
