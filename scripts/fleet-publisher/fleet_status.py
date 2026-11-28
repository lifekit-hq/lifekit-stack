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
  intake_spool  hand-offs the intake door holds for retry (`spool.mjs count`
                in the gateway): pending, oldest_epoch
  how_it_works  one owner page per workflow, linked only once it exists
  stalled       items whose state has not changed for their queue's threshold
                (fleet-stalled.json beside this script): GitHub issues of the
                registered projects, backlog items, jobq jobs, A2A threads,
                and items left in the Shape stage (shaping items, owner-ask
                issues without `ready`) past `shape.days`
  clip_queue    one row per clip channel from the clip queue's own outputs
                (daily-check.json, egress.json, queue-run.json, events.jsonl
                and the channel state directories, read-only): waiting, oldest
                age, buffer days, state counts, 7-day event counts, egress,
                stalled
  items         one record per open finance item (FLEET_FINANCE_CMD), per
                needs-you decision (Kit's ledger), per waiting or running job
                (`jobq.py list --json` in the gateway) and per open doc-door
                document (`door.py list --json` in the gateway), each with the item
                envelope (owner, class, store, ref, and acceptance when the
                source has one)
  problems      the three oldest stalled items by first-seen age, one brief line
  side_hustles  always null: retired, its stats feed was never written

Usage: fleet_status.py --home <id>=<path> [--home ...] --now <epoch>
         --state-dir <dir> [--daily <jsonl> ...] [--remote <json> ...]

Env: TASKS_AXI (absolute tasks-axi; its directory goes on PATH so the home's
wrapper finds it and node), CREDENTIAL_RE (a record whose text matches it is
withheld), FLEET_GH (default gh; empty skips open PRs), FLEET_PR_TTL (seconds,
default 600), FLEET_VAULT (default /srv/memory), FLEET_PLAN_MAP ("<repo>=<vault
project> ..." where the names differ), FLEET_INTAKE_CMD (overrides the gateway
`docker exec`; empty skips intake), FLEET_JOBS_CMD (the same for the job envelopes
the stalled check and the items read, `jobq.py list --json`; empty skips them, unset skips them too when
FLEET_INTAKE_CMD is set), FLEET_INTAKE_SPOOL_CMD (the same for the intake
door's spool count; empty skips it, unset skips it too when FLEET_INTAKE_CMD is
set),
FLEET_A2A_CMD (prints the open A2A threads as JSON; unset: none reachable),
FLEET_FINANCE_CMD (prints finance's open items as JSON; unset: not read),
FLEET_NEEDS_YOU_LEDGER (Kit's needs-you/open.json; unset: not read),
FLEET_SCOUT_STATE (the scout's state dir, `candidates/<date>.txt` inside; unset:
the scout is not read),
FLEET_KIT_HELD_CMD (prints the held fleet tasks in Kit's daily notes that have
no shaping item, as JSON - `kit_held.py check`; unset: not read),
FLEET_DOC_DOOR_CMD (the same for doc-door's open documents, `door.py list
--json`; empty skips them, unset skips them too when FLEET_INTAKE_CMD is set),
FLEET_INSTALLED_PROM (the release gauge's lifekit_installed.prom, which says
per installed component whether it is behind main; unset: not read),
FLEET_STALLED_CONFIG (the thresholds file, else fleet-stalled.json beside this
script). The clip queue is read from clip's default ~/clip-data/queue; a
missing directory or input file is named in the clip source's reason, not in
`errors`. A source that fails empties its own part
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
GATEWAY_SPOOL = "/home/node/.openclaw/extensions/intake-door/spool.mjs"
# doc-door runs as the reader agent (scripts/ensure-doc-door.sh); its state is the
# gateway's ~/.life-state/doc-door, readable only inside the container.
GATEWAY_DOOR = "/home/node/.openclaw/agents/reader/workspace/skills/doc-door/door.py"
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
            and all("created" in i for i in hit["issues"])
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
                    "number,title,url,createdAt,updatedAt,labels",
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
        "created": parse_time(raw.get("createdAt")),
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


def gateway() -> str:
    found = run(
        ["docker", "ps", "-q", *(a for f in GATEWAY_LABELS for a in ("--filter", f))],
        timeout=10,
    ).split()
    if not found:
        raise OSError("no running openclaw-gateway container")
    return found[0]


def intake() -> dict[str, Any]:
    cmd = os.environ.get("FLEET_INTAKE_CMD")
    if cmd is None:
        argv = ["docker", "exec", gateway(), "python3", GATEWAY_JOBQ, "depth"]
    else:
        argv = shlex.split(cmd)
    doc = json.loads(run(argv, timeout=15))
    keys = ("waiting", "running", "done_24h", "failed_24h")
    if not isinstance(doc, dict) or not all(
        isinstance(doc.get(k), int) and doc[k] >= 0 for k in keys
    ):
        raise ValueError("not a jobq depth document")
    return {k: doc[k] for k in keys}


def spool_cmd() -> list[str] | None:
    """FLEET_INTAKE_SPOOL_CMD as argv; [] for the gateway default; None to skip.
    Unset, it is the default only while FLEET_INTAKE_CMD is unset too, as for
    the job envelopes."""
    cmd = os.environ.get("FLEET_INTAKE_SPOOL_CMD")
    if cmd is None:
        return [] if "FLEET_INTAKE_CMD" not in os.environ else None
    return shlex.split(cmd) or None


def intake_spool(argv: list[str]) -> dict[str, Any]:
    """The intake door's spool: hand-offs that failed and wait for the retry
    (docs/runbook.md, "Intake door")."""
    if not argv:
        argv = ["docker", "exec", gateway(), "node", GATEWAY_SPOOL, "count"]
    doc = json.loads(run(argv, timeout=15))
    if not isinstance(doc, dict) or doc.get("schema") != "intake-spool-count.v1":
        raise ValueError("not an intake spool count")
    pending, oldest = doc.get("pending"), doc.get("oldest_epoch")
    if not isinstance(pending, int) or pending < 0:
        raise ValueError("not an intake spool count")
    if oldest is not None and not (isinstance(oldest, int) and oldest > 0):
        raise ValueError("not an intake spool count")
    return {"pending": pending, "oldest_epoch": oldest}


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
        for key, fields in (
            ("clip", ("buffer_floor_days", "slot_window_hours")),
            ("scout", ("idle_hours",)),
            ("kit_held", ("hours",)),
            ("installed", ("behind_hours",)),
            ("shape", ("days",)),
            ("doc_door", ("open_hours",)),
            ("inbox", ("hours",)),
        ):
            if key in cfg:  # optional: absent leaves that source unread
                block = cfg[key]
                assert all(
                    isinstance(block[f], (int, float))
                    and not isinstance(block[f], bool)
                    and block[f] > 0
                    for f in fields
                )
                assert block.get("owner", "firstmate") in owners
        if "shape" in cfg:
            shape = cfg["shape"]
            assert isinstance(shape.get("ready_label", "ready"), str)
            for key in ("ask_label_prefixes", "id_prefixes"):
                assert isinstance(shape.get(key, []), list)
                assert all(isinstance(v, str) and v for v in shape.get(key, []))
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
            if issue_in_shape(issue, cfg):  # the Shape clock ages it instead
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


def read_jobs(errors: list[str]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The job queue's item envelopes, `jobq.py list --json` in the gateway
    (one per job: ids, states, times and the envelope, never a label, result or
    payload), read once for both the stalled check and the items. Returns (the
    document, the source's `{read, reason}`)."""
    argv = jobs_cmd()
    if argv is None:
        return None, {"read": False, "reason": "job queue skipped"}
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
            argv = [
                "docker",
                "exec",
                found[0],
                "python3",
                GATEWAY_JOBQ,
                "list",
                "--json",
            ]
        doc = json.loads(run(argv, timeout=15))
        assert isinstance(doc["jobs"], list) and isinstance(doc["max_attempts"], int)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AssertionError,
        subprocess.SubprocessError,
    ) as exc:
        errors.append(f"job queue unreadable ({exc.__class__.__name__})")
        return None, {"read": False, "reason": "job queue unreadable"}
    return doc, {"read": True, "reason": None}


def job_native(job: Any) -> str | None:
    """A listed job's own id in the queue (`ref`), or None for a malformed one."""
    if not isinstance(job, dict) or job.get("store") != "jobq":
        return None
    ref = job.get("ref")
    if not isinstance(ref, str) or not ref or job.get("id") != f"jobq:{ref}":
        return None
    return ref


def job_stalls(
    cfg: dict[str, Any], now: int, doc: dict[str, Any]
) -> list[dict[str, Any]]:
    """Jobs whose attempts are used up, or that have waited past the threshold."""
    jobs, limit = doc["jobs"], doc["max_attempts"]
    threshold = cfg["jobq"]["waiting_hours"] * 3600
    owners = cfg["owners"]
    rows = []
    for job in jobs:
        native = job_native(job)
        if native is None:
            continue
        status, attempts = job.get("stage"), job.get("attempts")
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
                "id": native,
                "home_id": None,
                "title": one_line(job.get("kind"), TITLE_CUT),
                "url": None,
                "reason": reason,
                "since": int(since),
                "threshold": int(cut),
                "owner": owner,
            }
        )
    return rows


JOB_OPEN = ("queued", "running")
JOB_CLASSES = ("dev", "ops", "finance", "life", "unclear")
JOB_ITEM_KEYS = ("stage", "state", "fingerprint", "thread", "waiting_on", "waiting")


def job_items(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One item per job still waiting or running, from the queue's own envelopes:
    a job carries its envelope's owner and class when it has one (else the skill
    that answers it, and `unclear`), its queue stage and A2A state, and for a
    queued job what it waits on (the egress, today the PC relay, or its turn)."""
    items = []
    for job in (doc or {}).get("jobs", []):
        native = job_native(job)
        if native is None or job.get("stage") not in JOB_OPEN:
            continue
        owner = job.get("owner") if isinstance(job.get("owner"), str) else ""
        klass = job.get("class") if job.get("class") in JOB_CLASSES else "unclear"
        item = envelope_item(
            {"owner": owner or "unclear", "class": klass, "store": "jobq"},
            "jobq",
            one_line(job.get("kind"), TITLE_CUT) or "job",
            native,
            title=native,
            since=job.get("since"),
            acceptance=job.get("acceptance"),
            origin=job.get("origin"),
        )
        for key in JOB_ITEM_KEYS:
            if line := one_line(job.get(key), TITLE_CUT):
                item[key] = line
        items.append(item)
    return items


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


CLIP_CHANNELS = ("cosmos", "mind", "neural", "yc", "vice-footage")
CLIP_FILES = ("daily-check.json", "egress.json", "queue-run.json", "events.jsonl")
CLIP_STATES = ("pending", "running", "done", "failed")
CLIP_EVENT_KINDS = ("published", "judged", "failed")
CLIP_EVENT_WINDOW = 7 * 86400
CLIP_EVENTS_TAIL = 4 * 1024 * 1024  # the newest bytes of events.jsonl read per run
# The 0019 item envelope each adapter stamps on its records: who owns the next
# move, its class (0016), and the one store that owns the item. `ref` is the
# item's native id (or URL) in that store.
CLIP_ENVELOPE = {"owner": "clip", "class": "ops", "store": "clip-queue"}
FINANCE_ENVELOPE = {"owner": "finance", "class": "finance", "store": "finance-db"}
NEEDS_YOU_ENVELOPE = {"owner": "kit", "class": "life", "store": "needs-you"}
FINANCE_KINDS = ("alert", "companion-event", "thesis-revision", "recommendation")
SCOUT_CANDIDATES = "candidates"
ADDED_RE = re.compile(r"^added:\s*(\S+)\s*$", re.M)


def clip_dir() -> Path:
    """The clip queue's directory: clip's own default `~/clip-data/queue` of
    the account the publisher runs as."""
    return Path.home() / "clip-data" / "queue"


def clip_counts(queue: Path, channel: str) -> dict[str, int] | None:
    """The number of sources in each of the channel's state directories
    (pending, running, done, failed). A state directory the channel does not
    have yet holds none; an unreadable channel directory gives None."""
    if not CHANNEL_RE.match(channel) or not (queue / channel).is_dir():
        return None
    counts = {}
    try:
        for state in CLIP_STATES:
            d = queue / channel / state
            counts[state] = (
                sum(1 for f in d.iterdir() if f.is_file()) if d.is_dir() else 0
            )
    except OSError:
        return None
    return counts


def clip_events(path: Path, now: int) -> dict[str, dict[str, Any]] | None:
    """Per channel, the events.jsonl counts of the last 7 days by kind
    (published, judged, failed) and the newest `published` time (epoch). Only
    the newest CLIP_EVENTS_TAIL bytes are read; None when the file is
    missing or unreadable. A line that does not parse is skipped."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - CLIP_EVENTS_TAIL))
            raw = f.read()
    except OSError:
        return None
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > CLIP_EVENTS_TAIL and lines:
        lines = lines[1:]  # the first line may be cut mid-row
    out: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        name, kind, at = ev.get("channel"), ev.get("kind"), feed_epoch(ev.get("at"))
        if not isinstance(name, str) or kind not in CLIP_EVENT_KINDS or at is None:
            continue
        rec = out.setdefault(
            name, {"week": dict.fromkeys(CLIP_EVENT_KINDS, 0), "last_published": None}
        )
        if 0 <= now - at <= CLIP_EVENT_WINDOW:
            rec["week"][kind] += 1
        if kind == "published" and at <= now:
            rec["last_published"] = max(rec["last_published"] or 0, at)
    return out


def read_json_object(path: Path) -> dict[str, Any] | None:
    """A JSON object file, or None when it is missing or unreadable. Never writes."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def clip_oldest_waiting(queue: Path, channel: str, now: int) -> tuple[bool, int | None]:
    """(readable, age in seconds of the oldest waiting source). The waiting
    sources are `<channel>/pending/<key>` files with an `added:` line; an empty
    directory reads as (True, None), an unreadable one as (False, None)."""
    if not CHANNEL_RE.match(channel):
        return False, None
    try:
        files = [f for f in (queue / channel / "pending").iterdir() if f.is_file()]
        added = []
        for f in files:
            m = ADDED_RE.search(f.read_text())
            if (at := feed_epoch(m.group(1)) if m else None) is not None:
                added.append(at)
    except (OSError, UnicodeDecodeError):
        return False, None
    if files and not added:
        return False, None
    return True, max(0, now - min(added)) if added else None


def clip_channel_row(
    name: str,
    check: dict[str, Any] | None,
    egress: str | None,
    events: dict[str, dict[str, Any]] | None,
    queue: Path,
    cfg: dict[str, Any] | None,
    now: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One channel's published row and, when it is stalled, its stalled row.
    A number the inputs do not hold is None and named in `unavailable`: never 0."""
    rec = check.get("channels", {}).get(name) if check else None
    rec = rec if isinstance(rec, dict) else None
    unavailable = []
    waiting = buffer_days = None
    if rec is not None:
        waiting = feed_int(rec.get("buffer_waiting"))
        days = rec.get("buffer_days")
        if isinstance(days, (int, float)) and not isinstance(days, bool) and days >= 0:
            buffer_days = days
    if waiting is None:
        unavailable.append("waiting")
    if buffer_days is None:
        unavailable.append("buffer_days")
    readable, oldest = clip_oldest_waiting(queue, name, now)
    if not readable:
        unavailable.append("oldest_age_s")
    if egress is None:
        unavailable.append("egress")
    counts = clip_counts(queue, name)
    if counts is None:
        unavailable.append("counts")
    if events is None:
        unavailable += ["events_7d", "last_published_at"]
        week = last_published = None
    else:
        mine = events.get(name)
        week = dict(mine["week"]) if mine else dict.fromkeys(CLIP_EVENT_KINDS, 0)
        last_published = mine["last_published"] if mine else None
    stalled = reason = None
    since = cut = 0
    if rec is not None and cfg is not None and "clip" in cfg:
        last_post = rec.get("last_post")
        posted = feed_epoch(last_post)
        slot, checked = feed_epoch(rec.get("slot")), feed_epoch(rec.get("checked_at"))
        window = int(cfg["clip"]["slot_window_hours"] * 3600)
        post_ok: bool | None = None  # a post time that does not parse is unknown
        if last_post is None:
            post_ok, since = False, slot or checked or now
        elif posted is not None:
            post_ok, since = now - posted <= window, posted
        buffer_ok = (
            None
            if buffer_days is None
            else buffer_days >= cfg["clip"]["buffer_floor_days"]
        )
        if post_ok is False:
            reason, cut = "no-post", window
        elif buffer_ok is False:
            reason, since = "buffer-short", checked or now
        if reason:
            stalled = True
        elif post_ok is not None and buffer_ok is not None:
            stalled = False
    if stalled is None:
        unavailable.append("stalled")
    row = {
        "channel": name,
        "status": "unavailable" if rec is None else "ok",
        "waiting": waiting,
        "oldest_age_s": oldest if readable else None,
        "buffer_days": buffer_days,
        "counts": counts,
        "events_7d": week,
        "last_published_at": last_published,
        "egress": egress,
        "stalled": stalled,
        "stalled_reason": reason,
        "unavailable": unavailable,
        **CLIP_ENVELOPE,
        "ref": name,
    }
    out = None
    if reason and cfg is not None:
        out = {
            "source": "clip",
            "id": name,
            "home_id": None,
            "title": f"clip channel {name}",
            "url": None,
            "reason": reason,
            "since": int(since),
            "threshold": int(cut),
            "owner": cfg["clip"].get("owner", "firstmate"),
        }
    return row, out


def clip_queue(
    cfg: dict[str, Any] | None, now: int
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any], bool]:
    """The clip queue's rows and its stalled rows, read-only from the queue's
    own outputs: `daily-check.json`, `egress.json`, `queue-run.json`,
    `events.jsonl` and each channel's state directories. Returns (section,
    stalled rows, the source's `{read, reason}`, whether the stall check read
    `daily-check.json` with its thresholds). An input that is missing or
    unreadable makes its own numbers unavailable (None), never zero, and is
    named once: in the section's `unavailable` and the source's reason (never
    an `errors` line, since a missing clip output is the clip pipeline's state,
    not a publisher fault)."""
    queue = clip_dir()
    if not queue.is_dir():
        return None, [], {"read": False, "reason": "no clip queue directory"}, False
    check = read_json_object(queue / CLIP_FILES[0])
    egress_doc = read_json_object(queue / CLIP_FILES[1])
    run_doc = read_json_object(queue / CLIP_FILES[2])
    events = clip_events(queue / CLIP_FILES[3], now)
    if check is not None and not isinstance(check.get("channels"), dict):
        check = None
    missing = [
        n
        for n, d in zip(CLIP_FILES, (check, egress_doc, run_doc, events), strict=True)
        if d is None
    ]
    state = egress_doc.get("state") if egress_doc else None
    egress = state if isinstance(state, str) and state else None
    names = list(CLIP_CHANNELS)
    if check:
        names += sorted(n for n in check["channels"] if n not in CLIP_CHANNELS)
    rows, stalled_rows = [], []
    for name in names:
        row, hit = clip_channel_row(name, check, egress, events, queue, cfg, now)
        rows.append(row)
        if hit:
            stalled_rows.append(hit)
    ran = run_doc.get("at") if run_doc else None
    ran_at = feed_epoch(ran)
    section = {
        "schema": "fleet-clip-queue.v1",
        "channels": rows,
        "egress": {
            "state": egress,
            "since": feed_epoch(egress_doc.get("since")) if egress_doc else None,
        },
        "queue_run": {
            "at": ran_at,
            "age_s": None if ran_at is None else max(0, now - ran_at),
        },
        "unavailable": missing,
    }
    unread = f"unread: {', '.join(missing)}" if missing else None
    checked = cfg is not None and "clip" in cfg
    if not checked:
        source = {"read": False, "reason": "no clip thresholds in fleet-stalled.json"}
    else:
        source = {"read": not missing, "reason": unread}
    return section, stalled_rows, source, checked and check is not None


def envelope_item(
    envelope: dict[str, str],
    source: str,
    kind: str,
    native: str,
    *,
    title: Any,
    ref: Any = None,
    since: Any = None,
    acceptance: Any = None,
    origin: Any = None,
    state: Any = None,
    fingerprint: Any = None,
) -> dict[str, Any]:
    """One item record (0019): `id` is `<store>:<native id>`, `ref` the item's URL
    when the source gives one, else its native id. `acceptance`, `origin`, `state`
    and `fingerprint` are present only when the source holds them: an item without
    one still publishes."""
    item = {
        "id": f"{envelope['store']}:{native}",
        "source": source,
        "kind": kind,
        "title": one_line(title, TITLE_CUT),
        **envelope,
        "ref": ref if isinstance(ref, str) and ref else native,
        "since": parse_time(since),
    }
    if line := one_line(acceptance, TITLE_CUT):
        item["acceptance"] = line
    if line := one_line(origin, TITLE_CUT):
        item["origin"] = line
    if line := one_line(state, TITLE_CUT):
        item["state"] = line
    if line := one_line(fingerprint, TITLE_CUT):
        item["fingerprint"] = line
    return item


def finance_items(errors: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Finance's open items (alerts, companion events, thesis revisions,
    recommendations) from FLEET_FINANCE_CMD, a command printing a JSON list of
    `{kind, id, title, acceptance, url, since}` (acceptance, url and since
    optional). Unset, the source says so: nothing is guessed."""
    argv = shlex.split(os.environ.get("FLEET_FINANCE_CMD") or "")
    if not argv:
        return [], {"read": False, "reason": "FLEET_FINANCE_CMD is not set"}
    try:
        doc = json.loads(run(argv, timeout=15))
        assert isinstance(doc, list)
    except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
        errors.append(f"finance items unreadable ({exc.__class__.__name__})")
        return [], {"read": False, "reason": "finance items unreadable"}
    items = []
    for e in doc:
        if not isinstance(e, dict) or e.get("kind") not in FINANCE_KINDS:
            continue
        native = e.get("id")
        if isinstance(native, bool) or not isinstance(native, (str, int)):
            continue
        items.append(
            envelope_item(
                FINANCE_ENVELOPE,
                "finance",
                e["kind"],
                f"{e['kind']}/{native}",
                title=e.get("title"),
                ref=e.get("url"),
                since=e.get("since"),
                acceptance=e.get("acceptance"),
            )
        )
    return items, {"read": True, "reason": None}


def needs_you_acceptance(entry: dict[str, Any]) -> str:
    """The ledger's done-when: the owner's answer closes it (Kit's AGENTS.md,
    "One answer closes its decision"), with the entry's options when it has them."""
    opts = entry.get("options")
    options = (
        [o for o in opts if isinstance(o, str) and o] if isinstance(opts, list) else []
    )
    if options:
        return "The owner answers in Kit's chat: " + " / ".join(options)
    return "The owner answers in Kit's chat"


def needs_you_items(errors: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Kit's open needs-you decisions, read-only from its ledger file
    (`needs-you/open.json`, named by FLEET_NEEDS_YOU_LEDGER, the installer's
    read-only bind of it into the unit): one record per entry, origin `kit`,
    its options in the acceptance line."""
    path = os.environ.get("FLEET_NEEDS_YOU_LEDGER", "")
    if not path:
        return [], {"read": False, "reason": "FLEET_NEEDS_YOU_LEDGER is not set"}
    try:
        doc = json.loads(Path(path).read_text())
        assert isinstance(doc, list)
    except FileNotFoundError:
        return [], {"read": False, "reason": "needs-you ledger missing"}
    except (OSError, ValueError, AssertionError) as exc:
        errors.append(f"needs-you ledger unreadable ({exc.__class__.__name__})")
        return [], {"read": False, "reason": "needs-you ledger unreadable"}
    items = []
    for e in doc:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"]:
            continue
        items.append(
            envelope_item(
                NEEDS_YOU_ENVELOPE,
                "needs-you",
                "decision",
                e["id"],
                title=e.get("ask"),
                since=e.get("opened"),
                acceptance=needs_you_acceptance(e),
                origin="kit",
            )
        )
    return items, {"read": True, "reason": None}


DOOR_REF_RE = re.compile(r"^[0-9a-f]{16}$")


def door_cmd() -> list[str] | None:
    """FLEET_DOC_DOOR_CMD as argv; [] for the gateway default; None to skip.
    Unset, it is the default only while FLEET_INTAKE_CMD is unset too, as for
    the job ages."""
    cmd = os.environ.get("FLEET_DOC_DOOR_CMD")
    if cmd is None:
        return [] if "FLEET_INTAKE_CMD" not in os.environ else None
    return shlex.split(cmd) or None


def doc_door_read(
    errors: list[str],
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """doc-door's open documents, from `door.py list --json` in the gateway (the
    state is 0700 to its writer, so it is asked, never read as files): one
    envelope per document, the report's title and never the document. Returns
    the valid entries, or None when the source was not read."""
    argv = door_cmd()
    if argv is None:
        return None, {"read": False, "reason": "doc-door skipped"}
    try:
        if not argv:
            argv = ["docker", "exec", gateway(), "python3", GATEWAY_DOOR]
            argv += ["list", "--json"]
        doc = json.loads(run(argv, timeout=15))
        assert isinstance(doc, list)
    except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
        errors.append(f"doc-door documents unreadable ({exc.__class__.__name__})")
        return None, {"read": False, "reason": "doc-door documents unreadable"}
    entries = [
        e
        for e in doc
        if isinstance(e, dict)
        and isinstance(e.get("ref"), str)
        and DOOR_REF_RE.match(e["ref"])
        and isinstance(e.get("state"), str)
    ]
    return entries, {"read": True, "reason": None}


def doc_door_items(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """One item per open document, the store's own envelope kept (owner, origin,
    acceptance, fingerprint, state); `class` and `store` are the door's."""
    door = {"class": "life", "store": "doc-door"}
    return [
        envelope_item(
            {"owner": one_line(e.get("owner"), TITLE_CUT) or "reader", **door},
            "doc-door",
            "document",
            e["ref"],
            title=e.get("title"),
            since=e.get("since"),
            acceptance=e.get("acceptance"),
            origin=e.get("origin"),
            state=e["state"],
            fingerprint=e.get("fingerprint"),
        )
        for e in entries or []
    ]


def doc_door_stalls(
    entries: list[dict[str, Any]] | None,
    source: dict[str, Any],
    cfg: dict[str, Any] | None,
    now: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """An open document is stalled once it has been open `doc_door.open_hours`
    since the door took it in. A document with no intake time is never a row;
    an unread source or no `doc_door` block leaves the source unread."""
    if cfg is None or "doc_door" not in cfg:
        return [], {
            "read": False,
            "reason": "no doc_door thresholds in fleet-stalled.json",
        }
    if entries is None:
        return [], source
    window = int(cfg["doc_door"]["open_hours"] * 3600)
    owner = cfg["doc_door"].get("owner", "firstmate")
    rows = []
    for e in entries:
        since = e.get("since")
        if not isinstance(since, int) or isinstance(since, bool):
            continue
        if now - since < window:
            continue
        rows.append(
            {
                "source": "doc",
                "id": e["ref"],
                "home_id": None,
                "title": one_line(e.get("title"), TITLE_CUT),
                "url": None,
                "reason": "open",
                "since": since,
                "threshold": window,
                "owner": owner,
            }
        )
    return rows, {"read": True, "reason": None, "open": len(entries)}


INBOX_DIR = ("state", "inbox")
INBOX_HEAD = 4096
INBOX_FIELD_RE = re.compile(r"^([a-z_]+)=(.*)$")


def note_head(path: Path) -> dict[str, str]:
    """A note's `key=value` header (up to the `--` line), never its body."""
    with path.open("rb") as fh:
        head = fh.read(INBOX_HEAD).decode("utf-8", "replace")
    fields: dict[str, str] = {}
    for line in head.splitlines():
        if line == "--":
            break
        if m := INBOX_FIELD_RE.match(line):
            fields[m.group(1)] = m.group(2)
    return fields


def inbox_stalls(
    homes: list[tuple[str, str]],
    cfg: dict[str, Any] | None,
    now: int,
    errors: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Each home's pending fm-inbox notes (`<home>/state/inbox/*.note`; an
    acknowledged note has moved to `handled/`). A note is stalled once it has
    waited `inbox.hours` since its `at`; `open` counts every pending note and
    `oldest` is the oldest `at` (null with none). A home with no inbox
    directory has no notes; an unreadable one leaves the source unread."""
    if cfg is None or "inbox" not in cfg:
        return [], {
            "read": False,
            "reason": "no inbox thresholds in fleet-stalled.json",
        }
    window = int(cfg["inbox"]["hours"] * 3600)
    owner = cfg["inbox"].get("owner", "firstmate")
    rows: list[dict[str, Any]] = []
    listed, oldest, unread = 0, None, []
    for home_id, home in homes:
        inbox = Path(home).joinpath(*INBOX_DIR)
        if not inbox.is_dir():
            continue
        try:
            heads = []
            # iterdir, not glob: glob swallows a PermissionError and would
            # read an unreadable inbox as an empty one.
            names = sorted(
                p
                for p in inbox.iterdir()
                if p.name.endswith(".note") and not p.name.startswith(".")
            )
            for path in names:
                try:
                    heads.append(note_head(path))
                except FileNotFoundError:  # acknowledged while this run read
                    continue
        except OSError as exc:
            errors.append(f"{home_id}: fm-inbox unreadable ({exc.__class__.__name__})")
            unread.append(home_id)
            continue
        for head in heads:
            listed += 1
            at = parse_time(head.get("at"))
            if at is None:
                continue
            oldest = at if oldest is None else min(oldest, at)
            if now - at < window:
                continue
            rows.append(
                {
                    "source": "note",
                    "id": head.get("id") or str(at),
                    "home_id": home_id,
                    "title": one_line(
                        f"fm-inbox note from {head.get('source') or 'unknown'}",
                        TITLE_CUT,
                    ),
                    "url": None,
                    "reason": "unacknowledged",
                    "since": at,
                    "threshold": window,
                    "owner": owner,
                }
            )
    source: dict[str, Any] = {
        "read": not unread,
        "reason": f"fm-inbox unreadable in {', '.join(unread)}" if unread else None,
        "open": listed,
        "oldest": oldest,
    }
    return rows, source


def scout_stalls(
    cfg: dict[str, Any] | None, now: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The scout is stalled when its newest candidates file (`candidates/<date>.txt`
    in FLEET_SCOUT_STATE) is older than `scout.idle_hours`. A state dir that is not
    set, unreadable or holds no candidates yet is unread: an age is never guessed."""
    state = os.environ.get("FLEET_SCOUT_STATE", "")
    if cfg is None or "scout" not in cfg:
        return [], {
            "read": False,
            "reason": "no scout thresholds in fleet-stalled.json",
        }
    if not state:
        return [], {"read": False, "reason": "FLEET_SCOUT_STATE is not set"}
    try:
        newest = max(
            f.stat().st_mtime
            for f in (Path(state) / SCOUT_CANDIDATES).glob("*.txt")
            if f.is_file()
        )
    except (OSError, ValueError):
        return [], {"read": False, "reason": "scout output unreadable or none yet"}
    window = int(cfg["scout"]["idle_hours"] * 3600)
    if now - int(newest) < window:
        return [], {"read": True, "reason": None}
    return [
        {
            "source": "scout",
            "id": "scout",
            "home_id": None,
            "title": "scout candidates",
            "url": None,
            "reason": "no-output",
            "since": int(newest),
            "threshold": window,
            "owner": cfg["scout"].get("owner", "firstmate"),
        }
    ], {"read": True, "reason": None}


def held_stalls(
    cfg: dict[str, Any] | None, now: int, errors: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Held items in Kit's daily notes that name a fleet task and were never
    filed as a shaping item (lifekit-stack#684), from FLEET_KIT_HELD_CMD (a
    JSON list of `{id, note, title, since}`, `kit_held.py check`). An item is
    stalled once it has waited `kit_held.hours`; `open` counts every listed
    item, stalled or not. Unset or unreadable: unread, never guessed."""
    if cfg is None or "kit_held" not in cfg:
        return [], {
            "read": False,
            "reason": "no kit_held thresholds in fleet-stalled.json",
        }
    argv = shlex.split(os.environ.get("FLEET_KIT_HELD_CMD") or "")
    if not argv:
        return [], {"read": False, "reason": "FLEET_KIT_HELD_CMD is not set"}
    try:
        doc = json.loads(run(argv, timeout=30))
        assert isinstance(doc, list)
    except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
        errors.append(f"Kit's held items unreadable ({exc.__class__.__name__})")
        return [], {"read": False, "reason": "Kit's held items unreadable"}
    window = int(cfg["kit_held"]["hours"] * 3600)
    owner = cfg["kit_held"].get("owner", "firstmate")
    rows, listed = [], 0
    for item in doc:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        since = item.get("since")
        if not isinstance(since, int) or isinstance(since, bool):
            continue
        listed += 1
        if now - since < window:
            continue
        rows.append(
            {
                "source": "held",
                "id": item["id"],
                "home_id": None,
                "title": one_line(item.get("title"), TITLE_CUT),
                "url": None,
                "reason": "not-filed",
                "since": since,
                "threshold": window,
                "owner": owner,
            }
        )
    return rows, {"read": True, "reason": None, "open": listed}


SHAPE_DEFAULTS = {
    "ready_label": "ready",
    "ask_label_prefixes": ["intake:"],
    "id_prefixes": ["shape-", "held-"],
}


def shape_rule(cfg: dict[str, Any], key: str) -> Any:
    return cfg["shape"].get(key, SHAPE_DEFAULTS[key])


def issue_in_shape(issue: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """An open issue is in Shape (decision 0019, stage 3) while it carries an
    owner ask - the `intake:<fingerprint>` receipt label the intake door
    (`file_intake`) puts on what it files - and not yet the `ready` label, the
    shape gate's one mark."""
    if "shape" not in cfg:
        return False
    labels = issue["labels"]
    if shape_rule(cfg, "ready_label").lower() in labels:
        return False
    asks = [p.lower() for p in shape_rule(cfg, "ask_label_prefixes")]
    return any(label.startswith(p) for label in labels for p in asks)


def record_in_shape(rec: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """A backlog record is in Shape while it is a captain-held shaping item: the
    intake shape step's `shape-<key>` (an accepted idea) or Kit's held item
    `held-<fingerprint>` (lifekit-stack#684). Answering it releases the hold or
    closes it, and it leaves Shape."""
    hold = rec.get("hold") or {}
    return (
        rec["state"] == "held"
        and hold.get("kind") == "captain"
        and isinstance(rec.get("id"), str)
        and rec["id"].startswith(tuple(shape_rule(cfg, "id_prefixes")))
    )


def shape_stalls(
    records: list[dict[str, Any]],
    by_home: dict[str, list[dict[str, Any]] | None],
    slugs: set[str],
    pr_cache: dict[str, Any],
    cfg: dict[str, Any],
    now: int,
    state: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Items in Shape longer than `shape.days` (decision 0019, amended
    2026-11-26: Shape stalls on a clock): captain-held shaping items of the
    backlog and open issues that carry an owner ask but not `ready`. An issue
    ages from its creation; a record from its creation date, or, with none,
    from the first run that saw it in Shape (`shape-seen.json` in the state
    dir; a mark is dropped when its home is read and the record has left
    Shape). Getting `ready`, being answered or closed takes an item off."""
    if "shape" not in cfg:
        return [], {
            "read": False,
            "reason": "no shape thresholds in fleet-stalled.json",
        }
    window = int(cfg["shape"]["days"] * 86400)
    owner = cfg["shape"].get("owner", "firstmate")
    rows: list[dict[str, Any]] = []
    listed = 0
    path = state / "shape-seen.json"
    marks = load_cache(path)
    kept: dict[str, Any] = {}
    shaping = [r for r in records if record_in_shape(r, cfg)]
    for rec in shaping:
        key = f"{rec['home_id']}\t{rec['id']}"
        mark = marks.get(key)
        kept[key] = mark if isinstance(mark, int) and mark <= now else now
    for key, mark in marks.items():
        if key not in kept and by_home.get(key.split("\t", 1)[0], []) is None:
            kept[key] = mark  # its home was not read: keep the clock
    if kept != marks:
        save_cache(path, kept)
    for rec in shaping:
        listed += 1
        created = parse_time(f"{rec['created']}T00:00:00Z") if rec["created"] else None
        since = (
            created if created is not None else kept[f"{rec['home_id']}\t{rec['id']}"]
        )
        if now - since < window:
            continue
        rec["stalled"] = stall(since, window, owner)
        rows.append(
            {
                "source": "shape",
                "id": rec["id"],
                "home_id": rec["home_id"],
                "title": rec["title"],
                "url": None,
                "reason": "not-ready",
                "since": since,
                "threshold": window,
                "owner": owner,
            }
        )
    missing = []
    for slug in sorted(slugs):
        hit = pr_cache.get(slug)
        issues = hit.get("issues") if isinstance(hit, dict) else None
        if not isinstance(issues, list):
            missing.append(slug)
            continue
        for issue in issues:
            if not issue_in_shape(issue, cfg) or issue.get("created") is None:
                continue
            listed += 1
            if now - issue["created"] < window:
                continue
            rows.append(
                {
                    "source": "shape",
                    "id": f"{slug}#{issue['number']}",
                    "home_id": None,
                    "title": issue["title"],
                    "url": issue["url"],
                    "reason": "not-ready",
                    "since": issue["created"],
                    "threshold": window,
                    "owner": issue_owner(slug, issue["labels"], cfg),
                }
            )
    if any(v is None for v in by_home.values()):
        source = {"read": False, "reason": "a home's backlog is unreadable"}
    elif not os.environ.get("FLEET_GH", "gh"):
        source = {"read": False, "reason": "FLEET_GH is empty"}
    elif missing:
        source = {"read": False, "reason": f"issues not read for {', '.join(missing)}"}
    else:
        source = {"read": True, "reason": None}
    return rows, {**source, "open": listed}


INSTALLED_PROM_FRESH = 3600  # the gauge rewrites it every 5 minutes
INSTALLED_LINE = re.compile(
    r'^lifekit_component_behind(_since_seconds)?\{component="([a-z0-9][a-z0-9-]*)"\}'
    r" (-?[0-9]+)$"
)


def installed_stalls(
    cfg: dict[str, Any] | None, now: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """An installed component is stalled when origin/main has changed a path it
    owns for longer than `installed.behind_hours` without its installer running
    (issue 689). Read from the release gauge's textfile (FLEET_INSTALLED_PROM):
    `lifekit_component_behind` 1 with `_since_seconds`, the commit time of the
    oldest change not installed. A component with no stamp (-1) is unknown, not
    stalled and not current; a file that is unset, unreadable or not rewritten
    within the hour is unread: an age is never guessed."""
    path = os.environ.get("FLEET_INSTALLED_PROM", "")
    if cfg is None or "installed" not in cfg:
        return [], {
            "read": False,
            "reason": "no installed thresholds in fleet-stalled.json",
        }
    if not path:
        return [], {"read": False, "reason": "FLEET_INSTALLED_PROM is not set"}
    try:
        prom = Path(path)
        age = now - int(prom.stat().st_mtime)
        text = prom.read_text()
    except (OSError, UnicodeDecodeError):
        return [], {"read": False, "reason": "installed-commit gauge unreadable"}
    if age > INSTALLED_PROM_FRESH:
        return [], {"read": False, "reason": "installed-commit gauge is stale"}
    behind: dict[str, int] = {}
    since: dict[str, int] = {}
    for line in text.splitlines():
        m = INSTALLED_LINE.match(line.strip())
        if m:
            (since if m.group(1) else behind)[m.group(2)] = int(m.group(3))
    window = int(cfg["installed"]["behind_hours"] * 3600)
    owner = cfg["installed"].get("owner", "firstmate")
    rows = [
        {
            "source": "installed",
            "id": name,
            "home_id": None,
            "title": name,
            "url": None,
            "reason": "behind-main",
            "since": since[name],
            "threshold": window,
            "owner": owner,
        }
        for name in sorted(behind)
        if behind[name] == 1 and name in since and now - since[name] >= window
    ]
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
    "clip": "clip",
    "scout": "scout",
    "held": "kit_held",
    "installed": "installed",
    "shape": "shape",
    "doc": "doc_door",
    "note": "inbox",
}
PROBLEM_PHRASE = {
    "no-status-line": "silent",
    "no-update": "no update",
    "attempts-exhausted": "job failed",
    "waiting": "job waiting",
    "past-deadline": "past deadline",
    "no-turn": "thread quiet",
    "no-post": "no post",
    "buffer-short": "buffer short",
    "no-output": "no output",
    "not-filed": "held, not filed",
    "behind-main": "behind main",
    "not-ready": "stuck in Shape",
    "open": "document open",
    "unacknowledged": "not acknowledged",
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


# --- clip feed parsing -----------------------------------------------------------

CHANNEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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
    cfg = stalled_config(errors)
    job_doc, jobq = read_jobs(errors)
    clip_section, clip_rows, clip_source, clip_checked = clip_queue(cfg, args.now)
    door_entries, door_source = doc_door_read(errors)
    if cfg:
        found = backlog_stalls(records, homes, cfg, args.now)
        issues, github = issue_stalls(
            {r["github"] for r in rows if r["github"]}, pr_cache, cfg, args.now
        )
        jobs = job_stalls(cfg, args.now, job_doc) if job_doc else []
        threads, a2a = thread_stalls(cfg, args.now, errors)
        scout_rows, scout_source = scout_stalls(cfg, args.now)
        held_rows, held_source = held_stalls(cfg, args.now, errors)
        installed_rows, installed_source = installed_stalls(cfg, args.now)
        shape_rows, shape_source = shape_stalls(
            records,
            by_home,
            {r["github"] for r in rows if r["github"]},
            pr_cache,
            cfg,
            args.now,
            state,
        )
        door_rows, door_stalled = doc_door_stalls(
            door_entries, door_source, cfg, args.now
        )
        inbox_rows, inbox_source = inbox_stalls(homes, cfg, args.now, errors)
        stalled_rows = (
            found
            + issues
            + jobs
            + threads
            + clip_rows
            + scout_rows
            + held_rows
            + installed_rows
            + shape_rows
            + door_rows
            + inbox_rows
        )
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
            "clip": clip_source,
            "scout": scout_source,
            "kit_held": held_source,
            "installed": installed_source,
            "shape": shape_source,
            "doc_door": door_stalled,
            "inbox": inbox_source,
        }
    depth: dict[str, Any] | None = None
    if os.environ.get("FLEET_INTAKE_CMD") != "":
        try:
            depth = intake()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"intake depth unreadable ({exc.__class__.__name__})")
    spool: dict[str, Any] | None = None
    spool_argv = spool_cmd()
    if spool_argv is not None:
        try:
            spool = intake_spool(spool_argv)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"intake spool unreadable ({exc.__class__.__name__})")
    vault = Path(os.environ.get("FLEET_VAULT") or "/srv/memory")
    how = []
    for wid, title, page in WORKFLOWS:
        url = vault_page_url(vault, page) if page else None
        how.append({"id": wid, "title": title, "url": url})
    decided_cache = load_cache(state / "status-decisions.json")
    outcomes = delivered(state, args.now, errors, vault, decided_cache)
    save_cache(state / "status-decisions.json", decided_cache)
    problem_sources = sources and {**sources, "clip": {"read": clip_checked}}
    problem_list = problems(stalled_rows, problem_sources, state, args.now, errors)
    items: list[dict[str, Any]] = []
    item_sources: dict[str, dict[str, Any]] = {}
    door_read = (doc_door_items(door_entries), door_source)
    for name, read in (
        ("finance", finance_items),
        ("needs_you", needs_you_items),
        ("doc_door", lambda _errors: door_read),
    ):
        got, item_sources[name] = read(errors)
        safe = [i for i in got if not withheld(i, pattern)]
        if len(safe) < len(got):
            errors.append(
                f"{len(got) - len(safe)} {name} item(s) withheld: they look like"
                " they hold a credential"
            )
        items.extend(safe)
    item_sources["jobq"] = jobq
    got = job_items(job_doc)
    safe = [i for i in got if not withheld(i, pattern)]
    if len(safe) < len(got):
        errors.append(
            f"{len(got) - len(safe)} jobq item(s) withheld: they look like"
            " they hold a credential"
        )
    items.extend(safe)
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
        "intake_spool": spool,
        "how_it_works": how,
        "stalled": stalled,
        "stalled_sources": sources,
        "delivered": outcomes,
        "problems": problem_list,
        # Retired (lifekit-stack#692): its only source, clip's stats.jsonl,
        # was never written. The key stays null so v1 consumers are unchanged.
        "side_hustles": None,
        "clip_queue": clip_section,
        "items": items,
        "item_sources": item_sources,
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
