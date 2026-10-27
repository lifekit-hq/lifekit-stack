#!/usr/bin/env python3
"""fleet-status - the living status page's sections, for fleet-publisher.sh.

Prints one fleet-status-page.v1 JSON object to stdout, which the publisher puts
in the merged summary as `status_page` (contract: README.md "Status page
sections"). Everything is read from structured sources, never from prose:

  records       each on-host home's backlog, whole, through the home's own
                bin/fm-tasks-axi.sh (`list`, then `show --full` for a row whose
                cells the list truncated)
  projects      the records grouped by their own `repo` (none: the "no project"
                group), open PRs per repo from `gh` (cached), plan links
  capacity      each on-host home's bin/fm-capacity.sh --json
  queue         the open queued records, and the 7-day throughput from the
                fleet-metrics daily rows
  intake        the shared job queue's depth (`jobq.py depth` in the gateway)
  how_it_works  one owner page per workflow, linked only once it exists

Usage: fleet_status.py --home <id>=<path> [--home ...] --now <epoch>
         --state-dir <dir> [--daily <jsonl> ...]

Env: TASKS_AXI (absolute tasks-axi; its directory goes on PATH so the home's
wrapper finds it and node), CREDENTIAL_RE (a record whose text matches it is
withheld), FLEET_GH (default gh; empty skips open PRs), FLEET_PR_TTL (seconds,
default 600), FLEET_VAULT (default /srv/memory), FLEET_PLAN_MAP ("<repo>=<vault
project> ..." where the names differ), FLEET_INTAKE_CMD (overrides the gateway
`docker exec`; empty skips intake). A source that fails empties its own part
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
from pathlib import Path
from typing import Any

SCHEMA = "fleet-status-page.v1"
NO_PROJECT = "no project"
TOP_QUEUE = 10
THROUGHPUT_DAYS = 7
LIST_FIELDS = "held,hold_kind,hold_until,hold_reason,priority,closed,links,blocked"
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
# The workflows the page explains, each by the vault owner page that will own
# it. None until that page is written: the link is left out and the publisher
# lists it under `follow_ups`, never a guessed path.
WORKFLOWS = (
    ("intake", "Reel and video intake", None),
    ("queue-to-merge", "A task from queue to merge", None),
    ("monday-loop", "The Monday loop", None),
)


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
    home_id: str, home: str, tasks_axi: str, cache: dict[str, Any]
) -> list[dict[str, Any]]:
    """The home's backlog, whole. A row whose cells the list truncated is read
    again with `show --full`, unless the cache holds the same row already
    resolved (titles rarely change, and each show costs a node start)."""
    wrapper = Path(home) / "bin/fm-tasks-axi.sh"
    if not os.access(wrapper, os.X_OK):
        raise OSError("no bin/fm-tasks-axi.sh")
    env = {**os.environ, "FM_HOME": home}
    if tasks_axi:
        env["PATH"] = f"{os.path.dirname(tasks_axi)}:{env.get('PATH', '')}"

    def axi(*args: str) -> str:
        return run([str(wrapper), *args], env=env, timeout=30)

    rows = toon_rows(axi("list", "--limit", "1000", "--fields", LIST_FIELDS))
    records = []
    for line, row in rows:
        if not row.get("id"):
            continue
        key = f"{home_id}\t{row['id']}"
        if any(TRUNCATED in (v or "") for v in row.values()):
            hit = cache.get(key)
            if isinstance(hit, dict) and hit.get("line") == line:
                full = hit.get("fields") or {}
            else:
                shown = toon_fields(axi("show", str(row["id"]), "--full"))
                full = {k: shown.get(k) for k, v in row.items() if TRUNCATED in (v or "")}
            cache[key] = {"line": line, "fields": full}
            row = {**row, **full}
        records.append(record(home_id, row))
    return records


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
            remote = run(["git", "-C", str(clone), "remote", "get-url", "origin"], timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if slug := github_slug(remote):
            return slug
    return None


def open_prs(
    slugs: set[str], cache: dict[str, Any], now: int, errors: list[str]
) -> None:
    """Refresh cache[slug] = {count, as_of} for slugs older than FLEET_PR_TTL. A
    failed refresh keeps the last count, so one bad minute never blanks a row."""
    gh = os.environ.get("FLEET_GH", "gh")
    if not gh:
        return
    ttl = int(os.environ.get("FLEET_PR_TTL") or 600)
    for slug in sorted(slugs):
        hit = cache.get(slug)
        if isinstance(hit, dict) and now - int(hit.get("as_of") or 0) < ttl:
            continue
        try:
            out = run(
                [gh, "pr", "list", "-R", slug, "--state", "open", "--limit", "200",
                 "--json", "number"],
                timeout=20,
            )
            cache[slug] = {"count": len(json.loads(out)), "as_of": now}
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            errors.append(f"open PRs of {slug} unreadable ({exc.__class__.__name__})")


def vault_page_url(vault: Path, page: str) -> str | None:
    """The vault page's GitHub URL, when the page exists and the vault's origin
    is on GitHub."""
    if not (vault / page).is_file():
        return None
    try:
        remote = run(["git", "-C", str(vault), "remote", "get-url", "origin"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    slug = github_slug(remote)
    return f"https://github.com/{slug}/blob/HEAD/{page}" if slug else None


def plan_map() -> dict[str, str]:
    pairs = (p.split("=", 1) for p in os.environ.get("FLEET_PLAN_MAP", "").split() if "=" in p)
    return {repo: project for repo, project in pairs if repo and project}


def projects(
    records: list[dict[str, Any]],
    homes: list[tuple[str, str]],
    pr_cache: dict[str, Any],
    now: int,
    errors: list[str],
) -> list[dict[str, Any]]:
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for r in records:
        groups.setdefault(r["repo"], []).append(r)
    slugs = {repo: repo_slug(repo, homes) for repo in groups if repo}
    open_prs({s for s in slugs.values() if s}, pr_cache, now, errors)
    vault = Path(os.environ.get("FLEET_VAULT") or "/srv/memory")
    mapped = plan_map()
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
                "last_landed": None
                if last is None
                else {k: last[k] for k in ("id", "home_id", "title", "closed", "pr_url")},
                "open_prs": prs.get("count") if isinstance(prs, dict) else None,
                "open_prs_as_of": prs.get("as_of") if isinstance(prs, dict) else None,
                "plan_url": vault_page_url(vault, f"projects/{project}/plan.md")
                if repo
                else None,
            }
        )
    # Busiest first; "no project" last, so a missing repo stays visible but apart.
    rows.sort(key=lambda p: (p["repo"] is None, -(p["in_flight"] + p["queued"]), p["name"]))
    return rows


# --- capacity, queue, intake ----------------------------------------------------


def idle_reason(cap: dict[str, Any], queued: int, held: int) -> str | None:
    """Why a home runs fewer workers than it may, from the fixed list; None when
    nothing explains it (room and open work: it should dispatch)."""
    running, allowed = cap["running"], cap["allowed"]
    if allowed is not None and running >= allowed:
        return "cap-reached"
    if cap["quota"] == "exhausted":
        return "quota"
    if queued == 0:
        return "all-held" if held else "queue-empty"
    return None


def capacity(
    home_id: str, home: str, records: list[dict[str, Any]] | None
) -> dict[str, Any]:
    script = Path(home) / "bin/fm-capacity.sh"
    doc = json.loads(
        run([str(script), "--json"], env={**os.environ, "FM_HOME": home}, timeout=30)
    )
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
    if records is not None:
        out["idle_reason"] = idle_reason(
            out,
            sum(r["state"] == "queued" for r in records),
            sum(r["state"] == "held" for r in records),
        )
    return out


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


def queue(records: list[dict[str, Any]], paths: list[str], now: int) -> dict[str, Any]:
    open_ = [r for r in records if r["state"] == "queued"]
    # The backlog's own order within a priority; unprioritised work last.
    ranked = sorted(
        enumerate(open_), key=lambda e: (e[1]["priority"] is None, e[1]["priority"] or 0, e[0])
    )
    return {
        "count": len(open_),
        "held": sum(r["state"] == "held" for r in records),
        "top": [
            {k: r[k] for k in ("id", "home_id", "title", "repo", "priority", "blocked")}
            for _, r in ranked[:TOP_QUEUE]
        ],
        "throughput": throughput(paths, now),
    }


def intake() -> dict[str, Any]:
    cmd = os.environ.get("FLEET_INTAKE_CMD")
    if cmd is None:
        found = run(
            ["docker", "ps", "-q", *(a for f in GATEWAY_LABELS for a in ("--filter", f))],
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


# --- assembly --------------------------------------------------------------------


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
    kept = 0
    for home_id, home in homes:
        try:
            got = read_records(home_id, home, tasks_axi, record_cache)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"{home_id}: backlog unreadable ({exc.__class__.__name__}: {exc})"[:300])
            by_home[home_id] = None
            continue
        safe = [r for r in got if not withheld(r, pattern)]
        kept += len(got) - len(safe)
        by_home[home_id] = safe
        records.extend(safe)
    if kept:
        errors.append(f"{kept} record(s) withheld: they look like they hold a credential")
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
    rows = projects(records, homes, pr_cache, args.now, errors)
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
        "follow_ups": [f"owner page for {h['title'].lower()}" for h in how if not h["url"]],
        "errors": errors,
    }


def home_arg(value: str) -> tuple[str, str]:
    home_id, sep, path = value.partition("=")
    if not sep or not home_id or not path.startswith("/"):
        raise argparse.ArgumentTypeError(f"--home wants <id>=<absolute path>, not {value!r}")
    return home_id, path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=home_arg, action="append", default=[])
    parser.add_argument("--now", type=int, required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--daily", action="append", default=[])
    args = parser.parse_args(argv)
    json.dump(build(args), sys.stdout, separators=(",", ":"))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
