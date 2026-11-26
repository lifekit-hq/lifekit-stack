#!/usr/bin/env python3
"""The fleet's scheduled work as one list: the summary's `scheduled` array.

Three subcommands, each printing JSON to stdout (contract in README.md,
"Scheduled work"):

  manifest --repo DIR
      What the repo declares, read at install time: every scripts/**/*.timer
      unit with its schedule and the service it starts, and every cron row a
      declaring script names (`check-cron-declarations.sh --list`, which reads
      the names through scripts/lib/cron-check.sh). The installer writes it
      beside the publisher, so the publisher never reads or runs the checkout.

  gateway
      The gateway's live rows: `openclaw cron list --all --json` and the active
      standing intents of the agents in FLEET_INTENT_AGENTS (default "kit
      finance": Kit and Ledger), read-only through `docker exec` in the running
      openclaw-gateway container. Only names, owners, schedules, times and
      results are kept: never a payload, prompt, delivery target or sender.
      FLEET_CRON_CMD and FLEET_INTENTS_CMD override the two reads (a command
      printing the CLI's JSON, or a JSON list of intent rows); empty skips one.
      A read that fails is listed in `errors` and its key is absent. An agent
      with no store file or no `standing_intents` table is skipped; the other
      agents' intents are still listed. Any other store error fails the read.

  build --now EPOCH --max-age SECONDS [--manifest FILE] [--gateway FILE]
      The array itself: the manifest's timers joined to `systemctl list-timers`
      (FLEET_SYSTEMCTL, default systemctl; empty skips), the gateway file's
      cron rows joined to the manifest's declared ones, and its intents. A
      declared timer or cron row the live side does not list is `missing`; one
      whose live side could not be read is `unknown`. The gateway file stamps
      each part it holds (`cron_read_epoch`, `intents_read_epoch`); a part read
      more than --max-age seconds before --now, or never stamped, makes its
      rows (for cron, the declared ones too) `unknown` with their age in
      `stale_seconds` (null when never read).
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

GATEWAY_LABELS = (
    "label=com.docker.compose.project=openclaw",
    "label=com.docker.compose.service=openclaw-gateway",
)
# Standing intents live in the owning agent's SQLite database (OpenClaw docs,
# concepts/standing-intents: agents/<agentId>/agent/openclaw-agent.sqlite).
INTENT_DB = "/home/node/.openclaw/agents/{agent}/agent/openclaw-agent.sqlite"
ACTIVE_INTENTS = ("pending", "armed", "fired")
# Run inside the gateway (its image has python3, no sqlite3 CLI): print the
# active intents of each agent named in argv as one JSON list, read-only.
INTENT_READER = """
import json, os, sqlite3, sys
rows = []
for agent in sys.argv[2:]:
    path = sys.argv[1].format(agent=agent)
    if not os.path.exists(path):
        continue
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    if not con.execute(
        "select 1 from sqlite_master where type = 'table'"
        " and name = 'standing_intents'"
    ).fetchone():
        continue
    found = con.execute(
        "select id, description, trigger_keywords, status, expires_at,"
        " last_fired_at, created_at from standing_intents"
        " where status in ('pending', 'armed', 'fired') order by created_at"
    ).fetchall()
    for r in found:
        rows.append(dict(zip(
            ("id", "description", "trigger_keywords", "status", "expires_at",
             "last_fired_at", "created_at"), r), agent=agent))
print(json.dumps(rows))
"""
TIMER_KEYS = (
    "OnCalendar",
    "OnBootSec",
    "OnStartupSec",
    "OnActiveSec",
    "OnUnitActiveSec",
    "OnUnitInactiveSec",
)
KIND_ORDER = {"timer": 0, "cron": 1, "intent": 2}
TEXT_CAP = 120


def run(argv: list[str], timeout: int) -> str:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=True
    ).stdout


def first_object(text: str, key: str) -> dict[str, Any]:
    """The first JSON object in text carrying key: the CLI may interleave JSON
    log lines with its result."""
    dec, i = json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and key in obj:
            return obj
        i = end
    raise ValueError(f"no JSON object with {key!r}")


def epoch(value: Any) -> int | None:
    """Seconds since the epoch from seconds, milliseconds or microseconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    if value > 1e14:
        return int(value // 1_000_000)
    if value > 1e11:
        return int(value // 1000)
    return int(value)


def cap(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    text = " ".join(text.split())
    return text if len(text) <= TEXT_CAP else text[: TEXT_CAP - 1] + "…"


# --- manifest ----------------------------------------------------------------


def timer_schedule(path: Path) -> tuple[str | None, str]:
    """The timer's schedule ("OnCalendar=...; ...") and the service it starts."""
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    cfg.optionxform = str
    cfg.read_string(path.read_text())
    timer = cfg["Timer"] if cfg.has_section("Timer") else {}
    parts = [f"{k}={timer[k]}" for k in TIMER_KEYS if k in timer]
    service = timer.get("Unit") or path.with_suffix(".service").name
    return ("; ".join(parts) or None), service


def manifest(repo: Path) -> dict[str, Any]:
    timers = []
    for path in sorted((repo / "scripts").rglob("*.timer")):
        schedule, service = timer_schedule(path)
        timers.append(
            {
                "unit": path.name,
                "service": service,
                "schedule": schedule,
                "script": str(path.relative_to(repo)),
            }
        )
    listed = run(
        ["bash", str(repo / "scripts/check-cron-declarations.sh"), "--list"],
        timeout=30,
    )
    cron = []
    for line in listed.splitlines():
        name, _, script = line.partition("\t")
        if name and script:
            cron.append({"name": name, "script": script})
    return {"schema": "fleet-scheduled-manifest.v1", "timers": timers, "cron": cron}


# --- gateway -----------------------------------------------------------------


def gateway_container() -> str:
    found = run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            "status=running",
            *(a for f in GATEWAY_LABELS for a in ("--filter", f)),
        ],
        timeout=10,
    ).split()
    if not found:
        raise OSError("no running openclaw-gateway container")
    return found[0]


def cron_schedule(s: Any) -> str | None:
    if not isinstance(s, dict):
        return None
    kind = s.get("kind")
    if kind == "cron" and isinstance(s.get("expr"), str):
        return f"cron {s['expr']}" + (f" ({s['tz']})" if s.get("tz") else "")
    if kind == "every" and isinstance(s.get("everyMs"), (int, float)):
        secs = int(s["everyMs"] // 1000)
        for unit, size in (("h", 3600), ("min", 60)):
            if secs >= size and secs % size == 0:
                return f"every {secs // size}{unit}"
        return f"every {secs}s"
    if kind == "at" and isinstance(s.get("at"), str):
        return f"at {s['at']}"
    return None


def cron_rows(doc: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for job in doc.get("jobs") or []:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str):
            continue
        state = job.get("state") if isinstance(job.get("state"), dict) else {}
        rows.append(
            {
                "name": job["name"],
                "agent": job.get("agentId")
                if isinstance(job.get("agentId"), str)
                else None,
                "enabled": job.get("enabled") is True,
                "schedule": cron_schedule(job.get("schedule")),
                "next": epoch(state.get("nextRunAtMs")),
                "last": epoch(state.get("lastRunAtMs")),
                "result": state.get("lastRunStatus") or state.get("lastStatus"),
            }
        )
    return rows


def intent_rows(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("not a list of standing intents")
    rows = []
    for r in raw:
        if not isinstance(r, dict) or r.get("status") not in ACTIVE_INTENTS:
            continue
        keywords = r.get("trigger_keywords")
        try:
            parsed = json.loads(keywords) if isinstance(keywords, str) else keywords
        except ValueError:
            parsed = keywords
        if isinstance(parsed, list):
            keywords = ", ".join(
                " ".join(k) if isinstance(k, list) else str(k) for k in parsed
            )
        rows.append(
            {
                "id": str(r.get("id") or ""),
                "agent": r.get("agent"),
                "description": cap(r.get("description")),
                "trigger": cap(keywords if isinstance(keywords, str) else None),
                "status": r["status"],
                "expires": epoch(r.get("expires_at")),
                "last": epoch(r.get("last_fired_at")),
            }
        )
    return rows


def gateway() -> dict[str, Any]:
    out: dict[str, Any] = {"errors": []}
    container: str | None = None

    def argv_for(env: str, default: list[str]) -> list[str] | None:
        nonlocal container
        cmd = os.environ.get(env)
        if cmd is not None:
            return shlex.split(cmd) or None
        if container is None:
            container = gateway_container()
        return ["docker", "exec", container, *default]

    agents = os.environ.get("FLEET_INTENT_AGENTS", "kit finance").split()
    reads = (
        (
            "cron",
            "FLEET_CRON_CMD",
            ["openclaw", "cron", "list", "--all", "--json"],
            lambda text: cron_rows(first_object(text, "jobs")),
        ),
        (
            "intents",
            "FLEET_INTENTS_CMD",
            ["python3", "-c", INTENT_READER, INTENT_DB, *agents],
            lambda text: intent_rows(json.loads(text)),
        ),
    )
    for key, env, default, parse in reads:
        try:
            argv = argv_for(env, default)
            if argv is None:
                continue
            out[key] = parse(run(argv, timeout=60))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            out["errors"].append(f"{key}: {type(exc).__name__}: {str(exc)[:200]}")
    return out


# --- build -------------------------------------------------------------------


def live_timers() -> dict[str, dict[str, Any]] | None:
    """Each loaded timer's next and last run and its service's result, by
    timer unit; None when systemd could not be read."""
    systemctl = os.environ.get("FLEET_SYSTEMCTL", "systemctl")
    if not systemctl:
        return None
    try:
        listed = json.loads(
            run([systemctl, "list-timers", "--all", "--output=json"], timeout=15)
        )
        timers = {
            t["unit"]: t
            for t in listed
            if isinstance(t, dict) and isinstance(t.get("unit"), str)
        }
        services = sorted(
            {
                t["activates"]
                for t in timers.values()
                if isinstance(t.get("activates"), str)
            }
        )
        results: dict[str, dict[str, str]] = {}
        if services:
            shown = run(
                [
                    systemctl,
                    "show",
                    "-p",
                    "Id",
                    "-p",
                    "Result",
                    "-p",
                    "ActiveState",
                    *services,
                ],
                timeout=15,
            )
            for block in re.split(r"\n\s*\n", shown.strip()):
                props = dict(
                    line.split("=", 1) for line in block.splitlines() if "=" in line
                )
                if props.get("Id"):
                    results[props["Id"]] = props
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"scheduled: systemctl: {exc}", file=sys.stderr)
        return None
    for t in timers.values():
        t["service"] = results.get(t.get("activates") or "", {})
    return timers


def entry(kind: str, name: str, **fields: Any) -> dict[str, Any]:
    base = {
        "kind": kind,
        "name": name,
        "owner": None,
        "owner_kind": "agent" if kind != "timer" else "unit",
        "schedule": None,
        "next_run_epoch": None,
        "last_run_epoch": None,
        "last_result": None,
        "script": None,
        "state": "live",
    }
    return {**base, **fields}


def staleness(
    gateway_doc: dict[str, Any], part: str, now: int, max_age: int
) -> dict[str, Any]:
    """Nothing while the part was read within max_age seconds; otherwise its
    rows are unknown, with the part's age (None when it was never read)."""
    read = gateway_doc.get(f"{part}_read_epoch")
    if not isinstance(read, int) or isinstance(read, bool):
        return {"state": "unknown", "stale_seconds": None}
    if now - read <= max_age:
        return {}
    return {"state": "unknown", "stale_seconds": now - read}


def build(
    manifest_doc: dict[str, Any], gateway_doc: dict[str, Any], now: int, max_age: int
) -> list[dict[str, Any]]:
    entries = []
    timers = live_timers()
    for t in manifest_doc.get("timers") or []:
        live = None if timers is None else timers.get(t["unit"])
        fields: dict[str, Any] = {
            "owner": t.get("service"),
            "schedule": t.get("schedule"),
            "script": t.get("script"),
        }
        if timers is None:
            fields["state"] = "unknown"
        elif live is None:
            fields["state"] = "missing"
        else:
            svc = live.get("service") or {}
            result = svc.get("Result") or None
            if svc.get("ActiveState") in ("activating", "deactivating"):
                result = "running"
            fields.update(
                next_run_epoch=epoch(live.get("next")),
                last_run_epoch=epoch(live.get("last")),
                last_result=result,
            )
        entries.append(entry("timer", t["unit"], **fields))

    declared = {c["name"]: c["script"] for c in manifest_doc.get("cron") or []}
    live_cron = gateway_doc.get("cron")
    cron_stale = staleness(gateway_doc, "cron", now, max_age)
    seen = set()
    for row in live_cron or []:
        seen.add(row["name"])
        entries.append(
            entry(
                "cron",
                row["name"],
                owner=row.get("agent"),
                schedule=row.get("schedule"),
                next_run_epoch=row.get("next"),
                last_run_epoch=row.get("last"),
                last_result=row.get("result"),
                script=declared.get(row["name"]),
                **{"state": "live" if row.get("enabled") else "disabled", **cron_stale},
            )
        )
    for name, script in declared.items():
        if name not in seen:
            entries.append(
                entry(
                    "cron", name, script=script, **(cron_stale or {"state": "missing"})
                )
            )

    intents_stale = staleness(gateway_doc, "intents", now, max_age)
    for it in gateway_doc.get("intents") or []:
        entries.append(
            entry(
                "intent",
                it.get("description") or it.get("id") or "",
                owner=it.get("agent"),
                schedule=f"when: {it['trigger']}" if it.get("trigger") else None,
                last_run_epoch=it.get("last"),
                last_result=it.get("status"),
                expires_epoch=it.get("expires"),
                id=it.get("id"),
                **intents_stale,
            )
        )
    entries.sort(key=lambda e: (KIND_ORDER[e["kind"]], e["name"], e["owner"] or ""))
    return entries


def read_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("--repo", required=True, type=Path)
    sub.add_parser("gateway")
    b = sub.add_parser("build")
    b.add_argument("--manifest")
    b.add_argument("--gateway")
    b.add_argument("--now", required=True, type=int)
    b.add_argument("--max-age", required=True, type=int)
    args = parser.parse_args()
    if args.cmd == "manifest":
        doc: Any = manifest(args.repo)
    elif args.cmd == "gateway":
        doc = gateway()
    else:
        doc = build(
            read_json(args.manifest), read_json(args.gateway), args.now, args.max_age
        )
    json.dump(doc, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
