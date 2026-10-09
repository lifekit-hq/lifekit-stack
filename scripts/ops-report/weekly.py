#!/usr/bin/env python3
"""ops-report weekly - one short Markdown report on the VPS's last 7 days.

Read-only. Sources: the Prometheus HTTP API (the container-exporter,
node-exporter and host-group gauge series), `docker ps` / `docker inspect` /
`docker network inspect` / `docker system df`, `df`, `du` and `crontab -l`. It
writes nothing, restarts nothing, prunes nothing, and never prints an
environment variable, a token or a cron command's arguments - inspect output is
reduced to the few fields the report names before anything is rendered.

Usage (on the VPS, any account that can reach docker and 127.0.0.1:9090):

    python3 scripts/ops-report/weekly.py > weekly-ops-report.md

    --prometheus URL      default http://127.0.0.1:9090
    --budget-doc PATH     default docs/resource-budget.md (budgets are read from
                          its table, never hard-coded here)
    --rehearsal-dir PATH  default /home/lifekit/rehearsal
    --backup-dir PATH     default /srv/openclaw/backups

A source that cannot be read prints "unavailable (reason)" in its section; the
report still renders. A host group whose gauge has no samples in the window
prints "no series". Fleet (worker) numbers belong to the fleet-ledger weekly
metrics and are not repeated here. GitHub deploy verdicts are not queried (the
script stays off the network beyond Prometheus): "deploys" is container
recreations seen in Prometheus, and "red" is containers now down or exited
non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GIB = 1024**3
MIB = 1024**2

# Container-group member selectors, mirroring the `lifekit:group_memory_bytes`
# recording rule in compose/observability/prometheus/rules.yml. The
# budgets themselves come from docs/resource-budget.md; a group in the doc
# with no entry here is reported as "no selector" (and a test fails).
GROUP_SELECTORS = {
    "openclaw": 'project="openclaw",service=~"openclaw-gateway|google-workspace-mcp"',
    "platform": 'project="compose"',
    "finance-sentry": 'project=~"docker|finance-sentry",name=~"finance-sentry-.*"',
    "devclaw-mcp": 'project="devclaw",service="devclaw-mcp"',
    "dashboard": 'project="dashboard"',
    "xui": 'project="xui"',
    "identity": 'project=~"identity|edge"',
}
# Host groups: budget-doc name -> `group` label of host_group_memory_bytes
# (scripts/host-gauge/host-group-gauge.sh).
HOST_GROUPS = {"operator sessions": "operator", "runners": "runners", "os": "os"}
HOST_GAUGE = "host_group_memory_bytes"
BURST_POOL_BUDGET_LABEL = "burst pool"
USAGE = "docker_container_memory_usage_bytes"


# --------------------------------------------------------------------------
# Budget doc
# --------------------------------------------------------------------------


def parse_budgets(text: str) -> dict[str, float]:
    """Group name -> budget in GiB, from the doc's `## The budgets` table."""
    budgets: dict[str, float] = {}
    in_table = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_table = line.strip() == "## The budgets"
            continue
        if not in_table or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0].strip("*").strip()
        try:
            value = float(cells[1].strip("*"))
        except ValueError:
            continue  # header, separator, prose
        if name.lower() in ("sum", "total"):
            continue
        budgets[name.lower()] = value
    return budgets


# --------------------------------------------------------------------------
# PromQL builders
# --------------------------------------------------------------------------


def group_sum(selector: str) -> str:
    return f"(sum({USAGE}{{{selector}}}) or vector(0))"


def host_group_series(group: str) -> str:
    """The gauge series as-is: no `or vector(0)`, so a missing series stays missing."""
    return f'{HOST_GAUGE}{{group="{group}"}}'


def burst_sum() -> str:
    named = " or ".join(f"{USAGE}{{{s}}}" for s in GROUP_SELECTORS.values())
    return f"(sum({USAGE} unless ({named})) or vector(0))"


def peak_query(expr: str) -> str:
    return f"max_over_time({expr}[7d:5m])"


def avg_query(expr: str) -> str:
    return f"avg_over_time({expr}[7d:5m])"


def breach_query(expr: str, budget_gib: float) -> str:
    """Max over 7d of the minimum, over 15 minutes, of the 30-minute average as
    a ratio of budget - above 1 means the 30-minute average sat over budget for
    15 minutes at least once. Budgets are documentation, not an alert; the
    report keeps the check so a drifting group shows up weekly."""
    ratio = f"avg_over_time({expr}[30m:5m]) / ({budget_gib} * {GIB})"
    return f"max_over_time(min_over_time(({ratio})[15m:5m])[7d:15m])"


# --------------------------------------------------------------------------
# Prometheus + host readers (the only I/O; tests fake these)
# --------------------------------------------------------------------------


class Unavailable(Exception):
    pass


def prom_query(base: str, query: str, timeout: float = 60.0) -> list[dict]:
    url = f"{base.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            doc = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Unavailable(f"prometheus: {exc}") from exc
    if doc.get("status") != "success":
        raise Unavailable(f"prometheus: {doc.get('error', 'query failed')}")
    return doc["data"]["result"]


def scalar_of(result: list[dict]) -> float | None:
    if not result:
        return None
    return float(result[0]["value"][1])


def run_cmd(argv: list[str], timeout: float = 60.0) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unavailable(f"{argv[0]}: {exc}") from exc
    if proc.returncode != 0:
        first = (proc.stderr.strip().splitlines() or ["exit " + str(proc.returncode)])[
            0
        ]
        raise Unavailable(f"{argv[0]}: {first[:120]}")
    return proc.stdout


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "n/a"
    if abs(n) >= GIB:
        return f"{n / GIB:.2f} GiB"
    return f"{n / MIB:.0f} MiB"


def fmt_gib_budget(x: float) -> str:
    return f"{x:g} GiB"


def names(result: list[dict], label: str = "name") -> list[str]:
    return sorted(r["metric"].get(label) or "(unlabelled)" for r in result)


def section(title: str, lines: list[str]) -> str:
    return f"## {title}\n\n" + "\n".join(lines) + "\n"


def capped(items: list[str], limit: int = 12) -> str:
    """Comma list with a tail count, so a week of ephemeral containers stays short."""
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", +{len(items) - limit} more"


def unavailable(exc: Exception) -> list[str]:
    return [f"unavailable ({exc})"]


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def memory_section(base: str, budgets: dict[str, float]) -> tuple[str, list[str]]:
    """Per-group 7-day peak / average against budget. Returns (markdown, decisions)."""
    decisions: list[str] = []
    rows = [
        "| Group | Budget | Peak | Average | Over budget 15m+ (30m avg) |",
        "| --- | --- | --- | --- | --- |",
    ]
    groups: list[tuple[str, str, float]] = []
    for name, budget in budgets.items():
        if name == BURST_POOL_BUDGET_LABEL:
            groups.append((name, burst_sum(), budget))
        elif name in GROUP_SELECTORS:
            groups.append((name, group_sum(GROUP_SELECTORS[name]), budget))
        elif name in HOST_GROUPS:
            groups.append((name, host_group_series(HOST_GROUPS[name]), budget))
        else:
            rows.append(
                f"| {name} | {fmt_gib_budget(budget)} | no selector | no selector | - |"
            )
    try:
        for name, expr, budget in groups:
            peak = scalar_of(prom_query(base, peak_query(expr)))
            avg = scalar_of(prom_query(base, avg_query(expr)))
            if peak is None and avg is None and name in HOST_GROUPS:
                rows.append(
                    f"| {name} | {fmt_gib_budget(budget)} | no series | no series | - |"
                )
                continue
            breach = scalar_of(prom_query(base, breach_query(expr, budget)))
            fired = breach is not None and breach > 1
            if fired:
                decisions.append(
                    f"{name} breached its {fmt_gib_budget(budget)} memory budget"
                )
            rows.append(
                f"| {name} | {fmt_gib_budget(budget)} | {fmt_bytes(peak)} | {fmt_bytes(avg)} | "
                + (f"yes ({breach * 100:.0f}% of budget sustained)" if fired else "no")
                + " |"
            )
    except Unavailable as exc:
        return section("Memory vs budget", unavailable(exc)), decisions
    return section("Memory vs budget", rows), decisions


def reliability_section(base: str) -> tuple[str, list[str]]:
    decisions: list[str] = []
    try:
        oom = names(prom_query(base, "increase(docker_container_oom_killed[7d]) > 0"))
        restarts = prom_query(base, "increase(docker_container_restart_count[7d]) > 0")
        unhealthy = names(
            prom_query(
                base, "count_over_time((docker_container_healthy == 0)[7d:1m]) > 15"
            )
        )
    except Unavailable as exc:
        return section("OOM kills, restarts, unhealthy", unavailable(exc)), decisions
    restart_txt = (
        capped(
            [
                f"{r['metric'].get('name', '?')} x{round(float(r['value'][1]))}"
                for r in sorted(restarts, key=lambda r: r["metric"].get("name", ""))
            ]
        )
        or "none"
    )
    if oom:
        decisions.append("OOM kill(s): " + ", ".join(oom))
    lines = [
        f"- OOM-killed: {capped(oom) or 'none'}",
        f"- Restarts: {restart_txt}",
        f"- Unhealthy for over 15 minutes: {capped(unhealthy) or 'none'}",
    ]
    return section("OOM kills, restarts, unhealthy", lines), decisions


def host_section(base: str) -> tuple[str, list[str]]:
    try:
        low = scalar_of(
            prom_query(base, "min_over_time(node_memory_MemAvailable_bytes[7d])")
        )
        total = scalar_of(
            prom_query(base, "max_over_time(node_memory_MemTotal_bytes[7d])")
        )
        swap = scalar_of(
            prom_query(
                base,
                "max_over_time((node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes)[7d:5m])",
            )
        )
        swap_total = scalar_of(
            prom_query(base, "max_over_time(node_memory_SwapTotal_bytes[7d])")
        )
    except Unavailable as exc:
        return section("Host memory", unavailable(exc)), []
    decisions = []
    if swap is not None and swap_total and swap / swap_total > 0.75:
        decisions.append("swap peaked over 75% used")
    lines = [
        f"- Lowest RAM available: {fmt_bytes(low)}"
        + (f" of {fmt_bytes(total)}" if total else ""),
        f"- Highest swap used: {fmt_bytes(swap)}"
        + (f" of {fmt_bytes(swap_total)}" if swap_total else ""),
    ]
    return section("Host memory", lines), decisions


def parse_df(text: str) -> dict[str, float] | None:
    """`df -B1 --output=size,used,avail,pcent /` output -> numbers."""
    rows = [ln.split() for ln in text.strip().splitlines()[1:] if ln.strip()]
    if not rows or len(rows[0]) < 4:
        return None
    size, used, avail = (float(x) for x in rows[0][:3])
    return {
        "size": size,
        "used": used,
        "avail": avail,
        "pct": float(rows[0][3].rstrip("%")),
    }


_SIZE_UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def parse_docker_size(text: str) -> float | None:
    """'1.2GB' / '340MB' / '0B' -> bytes; a trailing '(12%)' is ignored."""
    m = re.match(r"\s*([\d.]+)\s*([kKMGT]?B)", text or "")
    if not m:
        return None
    return float(m.group(1)) * _SIZE_UNITS[m.group(2)]


def parse_system_df(text: str) -> list[tuple[str, str, float | None]]:
    """`docker system df --format '{{json .}}'` -> (type, size text, reclaimable bytes)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        out.append(
            (
                doc.get("Type", "?"),
                doc.get("Size", "?"),
                parse_docker_size(doc.get("Reclaimable", "")),
            )
        )
    return out


def dir_size(path: str) -> str:
    if not os.path.isdir(path):
        return "not present"
    try:
        out = run_cmd(["du", "-sb", path], timeout=120)
    except Unavailable:
        return "unreadable by this account"
    return fmt_bytes(float(out.split()[0]))


def disk_section(rehearsal_dir: str, backup_dir: str) -> tuple[str, list[str]]:
    decisions: list[str] = []
    lines: list[str] = []
    try:
        df = parse_df(run_cmd(["df", "-B1", "--output=size,used,avail,pcent", "/"]))
        if df is None:
            raise Unavailable("df: unexpected output")
        lines.append(
            f"- Root filesystem: {fmt_bytes(df['used'])} used of {fmt_bytes(df['size'])} "
            f"({df['pct']:.0f}%), {fmt_bytes(df['avail'])} free"
        )
        if df["pct"] >= 85:
            decisions.append(f"root filesystem {df['pct']:.0f}% used")
    except Unavailable as exc:
        lines.append(f"- Root filesystem: unavailable ({exc})")
    try:
        labels = {
            "Images": "Images",
            "Containers": "Containers",
            "Local Volumes": "Volumes",
            "Build Cache": "Builder cache",
        }
        for kind, size, reclaim in parse_system_df(
            run_cmd(["docker", "system", "df", "--format", "{{json .}}"])
        ):
            lines.append(
                f"- {labels.get(kind, kind)}: {size} total, {fmt_bytes(reclaim)} reclaimable"
            )
    except Unavailable as exc:
        lines.append(f"- Docker disk use: unavailable ({exc})")
    lines.append(f"- Rehearsal dir ({rehearsal_dir}): {dir_size(rehearsal_dir)}")
    lines.append(f"- Backups ({backup_dir}): {dir_size(backup_dir)}")
    return section("Disk", lines), decisions


def deploys_section(base: str) -> tuple[str, list[str]]:
    try:
        recreated = prom_query(
            base, "sum by (project) (changes(docker_container_started_at_seconds[7d]))"
        )
        red = prom_query(
            base,
            "docker_container_exit_code != 0 and on(name) docker_container_running == 0"
            " and on(name) changes(docker_container_running[7d]) > 0",
        )
    except Unavailable as exc:
        return section("Deploys", unavailable(exc)), []
    rows = sorted(
        (
            (r["metric"].get("project") or "(no project)", round(float(r["value"][1])))
            for r in recreated
        ),
        key=lambda x: x[0],
    )
    lines = [
        "Container (re)starts per project (a proxy for deploys; CI verdicts are in GitHub):",
        "",
    ]
    lines += [f"- {p}: {n}" for p, n in rows if n > 0] or ["- none"]
    red_names = names(red)
    lines += ["", f"Down and exited non-zero now: {', '.join(red_names) or 'none'}"]
    decisions = (
        [f"container(s) down non-zero: {', '.join(red_names)}"] if red_names else []
    )
    return section("Deploys", lines), decisions


# -- boundary drift ---------------------------------------------------------


def reduce_inspect(doc: dict) -> dict:
    """Keep only the fields the report names; Config.Env never leaves this function."""
    labels = (doc.get("Config") or {}).get("Labels") or {}
    return {
        "name": (doc.get("Name") or "").lstrip("/"),
        "project": labels.get("com.docker.compose.project", ""),
        "service": labels.get("com.docker.compose.service", ""),
        "mem_limit": (doc.get("HostConfig") or {}).get("Memory", 0) or 0,
        "running": bool((doc.get("State") or {}).get("Running")),
        "networks": sorted(
            ((doc.get("NetworkSettings") or {}).get("Networks") or {}).keys()
        ),
    }


def cron_command(line: str) -> tuple[str, list[str]]:
    """Split a schedule line into (schedule, command tokens); handles `@daily` forms."""
    parts = line.split()
    if line.startswith("@"):
        return parts[0], parts[1:]
    return " ".join(parts[:5]), parts[5:]


def cron_repo_installed(line: str, repo_files: set[str]) -> bool:
    return "lifekit-stack" in line or any(
        os.path.basename(tok) in repo_files
        for tok in cron_command(line)[1]
        if "/" in tok or "." in tok
    )


def parse_crontab(text: str) -> list[str]:
    """Schedule lines only; env assignments and comments are dropped."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if (
            not line
            or line.startswith("#")
            or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line)
        ):
            continue
        out.append(line)
    return out


def cron_label(line: str) -> str:
    """Schedule plus the command's program only - arguments may hold secrets."""
    sched, cmd = cron_command(line)
    return f"`{sched}` {os.path.basename(cmd[0]) if cmd else '?'}"


def drift_section(repo_root: Path) -> tuple[str, list[str]]:
    lines: list[str] = []
    decisions: list[str] = []
    containers: list[dict] = []
    try:
        ids = run_cmd(["docker", "ps", "-aq"]).split()
        if ids:
            containers = [
                reduce_inspect(d)
                for d in json.loads(run_cmd(["docker", "inspect", *ids]))
            ]
    except (Unavailable, ValueError) as exc:
        lines.append(f"- Containers: unavailable ({exc})")
    else:
        uncapped = sorted(
            c["name"] for c in containers if c["running"] and not c["mem_limit"]
        )
        stray = sorted(c["name"] for c in containers if not c["project"])
        lines.append(
            f"- Running with no memory cap: {len(uncapped)}"
            + (f" ({', '.join(uncapped)})" if uncapped else "")
        )
        lines.append(
            f"- Stray containers (no compose project): {', '.join(stray) or 'none'}"
        )
        # Cross-project networks: one network reaching containers of >1 project.
        by_net: dict[str, set[str]] = {}
        for c in containers:
            for n in c["networks"]:
                if n not in ("bridge", "host", "none"):
                    by_net.setdefault(n, set()).add(c["project"] or c["name"])
        cross = {n: sorted(p) for n, p in by_net.items() if len(p) > 1}
        lines.append(
            "- Cross-project networks: "
            + (
                "; ".join(f"{n} ({', '.join(p)})" for n, p in sorted(cross.items()))
                or "none"
            )
        )
    try:
        try:
            cron = parse_crontab(run_cmd(["crontab", "-l"]))
        except Unavailable as exc:
            if "no crontab" not in str(exc):
                raise
            cron = []
        repo_files = {p.name for p in (repo_root / "scripts").rglob("*") if p.is_file()}
        foreign = [
            cron_label(ln) for ln in cron if not cron_repo_installed(ln, repo_files)
        ]
        lines.append(
            f"- Cron jobs no repo script installs: {'; '.join(foreign) or 'none'}"
        )
        if foreign:
            decisions.append(f"{len(foreign)} cron job(s) not installed by the repo")
    except Unavailable as exc:
        lines.append(f"- Cron jobs: unavailable ({exc})")
    return section("Boundary drift", lines), decisions


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def assemble(now: datetime, sections: list[tuple[str, list[str]]]) -> str:
    decisions = [d for _, ds in sections for d in ds]
    head = f"# VPS ops report - week to {now.strftime('%Y-%m-%d')}\n"
    body = "\n".join(md for md, _ in sections)
    tail = (
        "## Needs a decision\n\n"
        + ("; ".join(decisions) if decisions else "none")
        + "\n"
    )
    return head + "\n" + body + "\n" + tail


def build_report(args: argparse.Namespace, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    try:
        budgets = parse_budgets(Path(args.budget_doc).read_text())
    except OSError as exc:
        budgets = {}
        mem = (section("Memory vs budget", unavailable(exc)), [])
    else:
        mem = memory_section(args.prometheus, budgets)
    return assemble(
        now,
        [
            mem,
            reliability_section(args.prometheus),
            host_section(args.prometheus),
            disk_section(args.rehearsal_dir, args.backup_dir),
            deploys_section(args.prometheus),
            drift_section(Path(args.repo_root)),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--prometheus", default="http://127.0.0.1:9090")
    ap.add_argument(
        "--budget-doc", default=str(REPO_ROOT / "docs" / "resource-budget.md")
    )
    ap.add_argument("--rehearsal-dir", default="/home/lifekit/rehearsal")
    ap.add_argument("--backup-dir", default="/srv/openclaw/backups")
    ap.add_argument("--repo-root", default=str(REPO_ROOT))
    sys.stdout.write(build_report(ap.parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
