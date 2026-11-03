#!/usr/bin/env python3
"""ops-report jobs - list-price USD per recurring job, axi-style (compact, agent-readable).

Read-only, stdlib only. The per-consumer line (consumers.py) says WHO spends; this says
WHAT inside each consumer recurs, so a token-diet verdict can be checked week over week:

    jobs 7d to 2026-11-03 list-USD (list-price weighting, not spend)
    jobs[N]{consumer,job,runs,usd,per_run}:
      main,wake:check:process-event,27,31.4,1.16
      openclaw,cron:ledger-pulse,8,2.5,0.32
      no-mistakes,test/test,47,17.6,0.37
      ...
    cold_cache[2]{consumer,calls,usd,share}:
      main,24,31.6,34%
    total: 812 (rows above min-usd shown; rest folded into <consumer>,other)

Rows:
- main / vps (supervisor sessions): every assistant call is charged to the last real user
  message before it, classified as a supervision wake class (the Stop-hook "needs a handling
  turn now. <class>" text), compaction resume, background-task notice, or captain/other.
  cold_cache is an "of which" overlay: the cache-write part of calls that came after more
  than an hour idle in the same session (the whole context re-written at 2x input).
- task: crewmate sessions, charged to the Firstmate task id found in the session
  (`state/<task>.inbox`); recurring ones (`*-weekly`, `*scout-YYYY-MM-DD*`) show as such.
- no-mistakes: agent_invocations by step/purpose.
- clip: pipeline run.json cost_usd (dev sessions are task rows).
- openclaw: gateway claude-cli logs, by cron job name, companion-signal kind, deploy smoke,
  or interactive; OpenClaw's own price table.

Usage: python3 scripts/ops-report/jobs.py [--days 7] [--min-usd 1] [--no-openclaw]
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from consumers import PRICES, bucket, clip_pipeline, usd  # noqa: E402

TASK = re.compile(r"(?:fm-vps|firstmate)/state/([a-z0-9][a-z0-9.-]+?)\.inbox")
WAKE = re.compile(
    r"needs a handling turn now\. (check: [a-z-]+|signal: \S+|supervision-host: [a-z-]+|[a-z-]+:)"
)
SR = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def text_of(msg):
    c = (msg or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None  # a tool result, not a turn trigger
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return str(c or "")


def trigger(t):
    """Classify the user message that started a supervisor turn."""
    flat = re.sub(r"\s+", " ", t)
    m = WAKE.search(flat)
    if m:
        k = m.group(1).replace(": ", ":").rstrip(":")
        k = {"supervision-host:cycle": "supervision-host:cycle-boundary"}.get(k, k)
        if k.startswith("signal:"):
            name = os.path.basename(k[7:]).split(".")[0]
            k = "signal:" + (name if name in ("vps", "pc") else "worker-status")
        return "wake:" + k
    t = re.sub(r"\s+", " ", SR.sub("", t)).strip()
    if not t:
        return "system-reminder"
    if t.startswith("This session is being continued"):
        return "compaction-resume"
    if t.startswith("MAIN DIALOG MIRROR"):
        return "supervision-host-engine"
    if t.startswith("<task-notification>"):
        return "bg-task-notice"
    if "operational input waiting" in t[:200]:
        return "fm-operational-inbox"
    return "captain/other"


def recurring(task):
    return bool(re.search(r"-weekly$|scout-\d{4}-\d{2}-\d{2}|ideas-scout", task))


def model_rate_write(model):
    fam = next((k for k in PRICES if k in (model or "")), None)
    return PRICES[fam][0] * 2 / 1e6 if fam else 0.0


def scan_host(root, cut):
    rows = defaultdict(lambda: [0, 0.0])  # (consumer, job) -> [runs, usd]
    cold = defaultdict(lambda: [0, 0.0])
    seen = set()
    for p in glob.glob(root + "/*/*.jsonl"):
        try:
            if os.path.getmtime(p) < cut.timestamp():
                continue
        except OSError:
            continue
        proj = os.path.relpath(p, root).split(os.sep)[0]
        b = bucket(proj)
        if not b:
            continue
        sup = b in ("main", "vps")
        if not sup:
            with open(p, errors="replace") as fh:
                m = TASK.search(fh.read(400000))
            tid = m.group(1) if m else "(no task id)"
            job = ("recurring:" if recurring(tid) else "task:") + tid
            consumer = b
        files = [p] + glob.glob(p[:-6] + "/subagents/*.jsonl")
        spent = False
        trigs = []  # parent's (timestamp, trigger) pairs, in file order
        for f in files:
            trig, last_t = "start", None
            for line in open(f, errors="replace"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ty = d.get("type")
                if ty == "user" and sup and f == p:
                    t = text_of(d.get("message"))
                    if t is not None and t.strip():
                        trig = trigger(t)
                        trigs.append((d.get("timestamp") or "", trig))
                    continue
                if ty != "assistant":
                    continue
                mm = d.get("message") or {}
                k = d.get("requestId") or mm.get("id")
                ts = d.get("timestamp") or ""
                if k in seen or ts < cut.isoformat():
                    continue
                if sup and f != p:
                    i = bisect.bisect_right(trigs, (ts, "\U0010ffff"))
                    trig = trigs[i - 1][1] if i else "start"
                seen.add(k)
                u = mm.get("usage") or {}
                c = usd(mm.get("model"), u)
                key = (b, trig) if sup else (consumer, job)
                rows[key][1] += c
                spent = True
                tt = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if sup and last_t is not None and tt - last_t > 3600:
                    cw = (u.get("cache_creation_input_tokens") or 0) * model_rate_write(
                        mm.get("model")
                    )
                    cold[b][0] += 1
                    cold[b][1] += cw
                last_t = tt
        if spent and not sup:
            rows[(consumer, job)][0] += 1  # sessions
    return rows, cold


def count_triggers(root, cut, rows):
    """Supervisor rows count triggers (turns started), not sessions."""
    for b, proj in (("main", "-home-denys-firstmate"), ("vps", "-home-denys-fm-vps")):
        for p in glob.glob(f"{root}/{proj}/*.jsonl"):
            if os.path.getmtime(p) < cut.timestamp():
                continue
            for line in open(p, errors="replace"):
                if '"type":"user"' not in line.replace(" ", ""):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if (d.get("timestamp") or "") < cut.isoformat():
                    continue
                t = text_of(d.get("message"))
                if t is not None and t.strip():
                    rows[(b, trigger(t))][0] += 1


def nm_rows(db, cut, rows):
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        q = (
            "select a.step_name,a.purpose,a.model,a.cache_read_tokens,a.cache_creation_tokens,"
            "a.output_tokens from agent_invocations a join runs r on r.id=a.run_id "
            "where r.created_at>=?"
        )
        for step, purpose, model, cr, cw, o in c.execute(q, (int(cut.timestamp()),)):
            r = rows[("no-mistakes", f"{step}/{purpose}")]
            r[0] += 1
            r[1] += usd(
                model,
                {
                    "cache_read_input_tokens": cr or 0,
                    "cache_creation_input_tokens": cw or 0,
                    "output_tokens": o or 0,
                },
            )
        c.close()
    except sqlite3.Error:
        pass


OC_ROOT = "/home/node/.claude/projects"
OC_SCRIPT = r"""
import glob, json, os, re, sys
P = {"opus": (5, 25, 0.5), "sonnet": (3, 15, 0.3), "haiku": (1, 5, 0.1)}
cut, root = sys.argv[1], sys.argv[2]
seen, out = set(), {}
for p in glob.glob(root + "/*/*.jsonl"):
    job, cost, hit = None, 0.0, False
    for f in [p] + glob.glob(p[:-6] + "/subagents/*.jsonl"):
      for line in open(f, errors="replace"):
        try: d = json.loads(line)
        except ValueError: continue
        if d.get("type") == "user" and job is None and f == p:
            c = (d.get("message") or {}).get("content")
            if isinstance(c, list): c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            c = str(c or "")
            m = re.search(r"\[cron:\S+ ([^\]]+)\]", c)
            k = re.search(r"companion signal: kind (\w+)", c)
            if m: job = "cron:" + m.group(1).strip()
            elif k: job = "hook:companion-" + k.group(1)
            elif c.startswith("Job ID:"): job = "hook:other"
            elif "smoke" in c[:200].lower(): job = "deploy-smoke"
            else: job = "interactive/other"
        if d.get("type") != "assistant": continue
        m = d.get("message") or {}; k = d.get("requestId") or m.get("id")
        if k in seen or (d.get("timestamp") or "") < cut: continue
        seen.add(k); u = m.get("usage") or {}
        fam = next((x for x in P if x in (m.get("model") or "")), None)
        if not fam: continue
        i, o, cr = P[fam]; hit = True
        cost += ((u.get("input_tokens") or 0) * i + (u.get("output_tokens") or 0) * o
                 + (u.get("cache_read_input_tokens") or 0) * cr
                 + (u.get("cache_creation_input_tokens") or 0) * i * 1.25) / 1e6
    if hit:
        r = out.setdefault(job or "interactive/other", [0, 0.0]); r[0] += 1; r[1] += cost
print(json.dumps(out))
"""


def oc_rows(cut, rows):
    try:
        ids = subprocess.run(
            ["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=openclaw",
             "--filter", "label=com.docker.compose.service=openclaw-gateway"],
            capture_output=True, text=True, timeout=30,
        ).stdout.split()  # fmt: skip
        if not ids:
            return False
        r = subprocess.run(
            ["docker", "exec", ids[0], "python3", "-I", "-c", OC_SCRIPT, cut.isoformat(), OC_ROOT],
            capture_output=True, text=True, timeout=300,
        )  # fmt: skip
        for job, (n, c) in json.loads(r.stdout).items():
            rows[("openclaw", job)] = [n, c]
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def render(rows, cold, days, now, min_usd, oc_ok):
    shown, folded = [], defaultdict(lambda: [0, 0.0])
    for (consumer, job), (n, c) in rows.items():
        if c >= min_usd:
            shown.append((consumer, job, n, c))
        else:
            f = folded[consumer]
            f[0] += n
            f[1] += c
    shown += [(k, "other(<min)", v[0], v[1]) for k, v in folded.items() if v[1] >= 0.05]
    shown.sort(key=lambda r: -r[3])
    total = sum(c for _, _, _, c in shown)
    out = [
        f"jobs {days}d to {now:%Y-%m-%d} list-USD (list-price weighting, not spend)",
        f"jobs[{len(shown)}]{{consumer,job,runs,usd,per_run}}:",
    ]
    for consumer, job, n, c in shown:
        out.append(f"  {consumer},{job},{n},{c:.1f},{(c / n if n else 0):.2f}")
    sup = defaultdict(float)
    for (consumer, _), (_, c) in rows.items():
        sup[consumer] += c
    out.append(f"cold_cache[{len(cold)}]{{consumer,calls,usd,share}}:")
    for b, (n, c) in sorted(cold.items()):
        out.append(f"  {b},{n},{c:.1f},{100 * c / max(sup[b], 1e-9):.0f}%")
    out.append(f"total: {total:.0f}" + ("" if oc_ok else " (openclaw n/a)"))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-usd", type=float, default=1.0)
    ap.add_argument("--claude-projects", default="~/.claude/projects")
    ap.add_argument("--clip-data", default="~/clip-data")
    ap.add_argument("--nm-db", default="~/.no-mistakes/state.sqlite")
    ap.add_argument("--no-openclaw", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    cut = now - timedelta(days=a.days)
    root = os.path.expanduser(a.claude_projects)
    rows, cold = scan_host(root, cut)
    count_triggers(root, cut, rows)
    nm_rows(os.path.expanduser(a.nm_db), cut, rows)
    clip = clip_pipeline(os.path.expanduser(a.clip_data), cut)
    if clip:
        rows[("clip", "pipeline-runs")] = [0, clip]
    oc_ok = False if a.no_openclaw else oc_rows(cut, rows)
    print(render(rows, cold, a.days, now, a.min_usd, oc_ok))
    return 0


if __name__ == "__main__":
    sys.exit(main())
