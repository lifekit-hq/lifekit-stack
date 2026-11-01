#!/usr/bin/env python3
"""ops-report consumers - one line: list-price USD per token consumer.

Read-only, stdlib only. Prints exactly one line:

    7d to <date> list-USD main N | vps N | workers N | clip N | no-mistakes N | memory N | openclaw N|n/a | total N

The figures are list-price weighting of logged token usage, NOT spend (the
prices below are public list prices, cache write counted at the input rate x2;
OpenClaw uses its own table and x1.25). Sources, one per figure:

- main / vps / workers / memory / clip: Claude Code session logs,
  <claude-projects>/<project-dir>/*.jsonl and subagents/*.jsonl, de-duplicated
  by requestId and bucketed by project-dir name. no-mistakes project dirs are
  skipped here (counted from its own database, so nothing is counted twice).
- clip also adds the pipeline runs' run.json `cost_usd` (those runs keep no
  session log), skipping `_judge/render/` duplicates.
- no-mistakes: the agent_invocations table of its sqlite state, opened read-only.
- openclaw: the same log scan run inside the gateway container (found by
  compose labels); n/a when the container is down.

A source that is absent or unreadable contributes n/a (openclaw) or 0 and the
line still prints; this module never raises into the weekly report.

Paths (flag, then env, then the default on this box):
    --days N              LIFEKIT_COST_DAYS            7
    --claude-projects P   LIFEKIT_COST_CLAUDE_PROJECTS ~/.claude/projects
    --clip-data P         LIFEKIT_COST_CLIP_DATA       ~/clip-data
    --nm-db P             LIFEKIT_COST_NM_DB           ~/.no-mistakes/state.sqlite
    --no-openclaw         skip the docker lookup
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# List-price weighting (USD per million tokens: input, output, cache read), not spend.
PRICES = {
    "fable": (10, 50, 0.25),
    "opus": (4, 20, 0.2),
    "sonnet": (2, 10, 0.2),
    "haiku": (0.1, 0.5, 0.01),
}
# OpenClaw's claude-cli runtime is billed on a different table; cache write 1.25x.
OC_PRICES = {
    "opus": (5, 25, 0.5),
    "sonnet": (3, 15, 0.3),
    "haiku": (1, 5, 0.1),
}


def usd(model, u, prices=PRICES, write_mult=2.0):
    fam = next((k for k in prices if k in (model or "")), None)
    if not fam:
        return 0.0
    i, o, cr = prices[fam]
    return (
        (u.get("input_tokens") or 0) * i
        + (u.get("output_tokens") or 0) * o
        + (u.get("cache_read_input_tokens") or 0) * cr
        + (u.get("cache_creation_input_tokens") or 0) * i * write_mult
    ) / 1e6


def bucket(proj):
    if proj.startswith("-home-denys-firstmate"):
        return "main"
    if proj == "-home-denys-fm-vps":
        return "vps"
    if "no-mistakes" in proj:
        return None
    if "clip" in proj or "science-shorts" in proj:
        return "clip"
    if "lifekit-stack" in proj:
        return "workers"
    if "memory" in proj:
        return "memory"
    return "workers"


def scan(root, cut, bucketer, prices=PRICES, write_mult=2.0):
    """Sum list-USD per bucket over session logs newer than `cut` (aware datetime)."""
    out: dict[str, float] = {}
    seen: set = set()
    paths = glob.glob(root + "/*/*.jsonl") + glob.glob(root + "/*/*/subagents/*.jsonl")
    for p in paths:
        try:
            if os.path.getmtime(p) < cut.timestamp():
                continue
            b = bucketer(os.path.relpath(p, root).split(os.sep)[0])
            if not b:
                continue
            with open(p, errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    m = d.get("message") or {}
                    k = d.get("requestId") or m.get("id")
                    if (
                        d.get("type") != "assistant"
                        or k in seen
                        or (d.get("timestamp") or "") < cut.isoformat()
                    ):
                        continue
                    seen.add(k)
                    out[b] = out.get(b, 0.0) + usd(
                        m.get("model"), m.get("usage") or {}, prices, write_mult
                    )
        except OSError:
            continue
    return out


def no_mistakes(db, cut):
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = c.execute(
                "select model,cache_read_tokens,cache_creation_tokens,output_tokens "
                "from agent_invocations a join runs r on r.id=a.run_id "
                "where r.created_at>=?",
                (int(cut.timestamp()),),
            ).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return 0.0
    return sum(
        usd(
            m,
            {
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
                "output_tokens": o,
            },
        )
        for m, cr, cw, o in rows
    )


def clip_pipeline(root, cut):
    t = 0.0
    for p in glob.glob(os.path.join(root, "**", "run.json"), recursive=True):
        if "/_judge/render/" in p:
            continue
        try:
            if os.path.getmtime(p) < cut.timestamp():
                continue
            with open(p) as fh:
                d = json.load(fh)
            t += float((d.get("tokens") or d.get("usage") or {}).get("cost_usd") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return t


# Runs inside the gateway container: the same scan, OpenClaw's table.
OC_SCRIPT = r"""
import glob, json, os, sys
cut = sys.argv[1]
P = %r
seen, t = set(), 0.0
root = "/home/node/.claude/projects"
for p in glob.glob(root + "/*/*.jsonl") + glob.glob(root + "/*/*/subagents/*.jsonl"):
    for line in open(p, errors="replace"):
        if '"usage"' not in line: continue
        try: d = json.loads(line)
        except ValueError: continue
        m = d.get("message") or {}; k = d.get("requestId") or m.get("id")
        if d.get("type") != "assistant" or k in seen or (d.get("timestamp") or "") < cut: continue
        seen.add(k); u = m.get("usage") or {}
        f = next((x for x in P if x in (m.get("model") or "")), None)
        if not f: continue
        i, o, cr = P[f]
        t += ((u.get("input_tokens") or 0) * i + (u.get("output_tokens") or 0) * o
              + (u.get("cache_read_input_tokens") or 0) * cr
              + (u.get("cache_creation_input_tokens") or 0) * i * 1.25) / 1e6
print(t)
""" % (OC_PRICES,)


def openclaw(cut):
    """Gateway-container scan, or None (n/a) when docker or the container is absent."""
    try:
        ids = subprocess.run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                "label=com.docker.compose.project=openclaw",
                "--filter",
                "label=com.docker.compose.service=openclaw-gateway",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.split()
        if not ids:
            return None
        r = subprocess.run(
            [
                "docker",
                "exec",
                ids[0],
                "python3",
                "-I",
                "-c",
                OC_SCRIPT,
                cut.isoformat(),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return float(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def cost_line(
    days=7,
    now=None,
    claude_projects="~/.claude/projects",
    clip_data="~/clip-data",
    nm_db="~/.no-mistakes/state.sqlite",
    use_openclaw=True,
):
    now = now or datetime.now(timezone.utc)
    cut = now - timedelta(days=days)
    c = scan(os.path.expanduser(claude_projects), cut, bucket)
    c["clip"] = c.get("clip", 0.0) + clip_pipeline(os.path.expanduser(clip_data), cut)
    c["nm"] = no_mistakes(os.path.expanduser(nm_db), cut)
    oc = openclaw(cut) if use_openclaw else None
    total = sum(c.values()) + (oc or 0.0)
    g = lambda k: f"{c.get(k, 0.0):.0f}"  # noqa: E731
    return (
        f"{days}d to {now.strftime('%Y-%m-%d')} list-USD main {g('main')} | "
        f"vps {g('vps')} | workers {g('workers')} | clip {g('clip')} | "
        f"no-mistakes {g('nm')} | memory {g('memory')} | "
        f"openclaw {'n/a' if oc is None else f'{oc:.0f}'} | total {total:.0f}"
    )


def safe_cost_line(**kw):
    """cost_line that can never fail the caller."""
    try:
        return cost_line(**kw)
    except Exception as exc:  # noqa: BLE001 - weekly report must always render
        return f"unavailable ({type(exc).__name__})"


def main(argv=None):
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=int(env("LIFEKIT_COST_DAYS", "7")))
    ap.add_argument(
        "--claude-projects",
        default=env("LIFEKIT_COST_CLAUDE_PROJECTS", "~/.claude/projects"),
    )
    ap.add_argument("--clip-data", default=env("LIFEKIT_COST_CLIP_DATA", "~/clip-data"))
    ap.add_argument(
        "--nm-db", default=env("LIFEKIT_COST_NM_DB", "~/.no-mistakes/state.sqlite")
    )
    ap.add_argument("--no-openclaw", action="store_true")
    a = ap.parse_args(argv)
    print(
        safe_cost_line(
            days=a.days,
            claude_projects=a.claude_projects,
            clip_data=a.clip_data,
            nm_db=a.nm_db,
            use_openclaw=not a.no_openclaw,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
