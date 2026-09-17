#!/usr/bin/env python3
"""quota-share - divide quota-axi's account-wide percent used across the
consumers of one Claude Max account, by their logged Claude Code session
usage, and print a report.

Why this exists (guard-45-agent-registry-quota scout, 2026-09-16): quota-axi
only reports the account as a whole - one number for "5h window" and one for
"7-day window" - with no per-consumer breakdown. But the captain, firstmate
(crew + no-mistakes review), devclaw, and every OpenClaw agent all draw on
the same Max account, and the 7-day pace was already projecting exhaustion
ahead of the weekly reset. This tool answers "who is actually spending the
budget" from evidence that already exists (Claude Code's own session JSONL)
without adding any new instrumentation, control plane, or daemon.

It is read-only and reports only: it never enforces a cap, and it never
prints a raw token count or a credential. Run it as a cron/report for a
week before the captain sets real `share` numbers in quota-shares.json -
see that file and report.md Part B "Proposal".

Consumer attribution:
  - local paths (this host's ~/.claude/projects) match `match` globs directly
    against each session's cwd, e.g. "~/.treehouse/*".
  - `lifekit:<glob>` matches the OpenClaw gateway's Claude Code session
    directory (bind-mounted at /home/lifekit/.claude, read directly if this
    account can, else via `docker exec` into the gateway container).
  - `metric:<name>` adds a Prometheus counter's 7-day increase (currently
    only devclaw_tokens_total) as a raw-token approximation for a consumer
    that does not go through Claude Code sessions.
  - exactly one consumer may use `match: "residual"` (the captain): its
    imputed usage is not measured, only its configured share is shown.

Usage weight (a cost proxy, not the real per-plan weighting Anthropic does
not publish): input + 5*output + 1.25*cache_write + 0.1*cache_read tokens,
times 5/3 for Opus/Fable and 1/3 for Haiku relative to Sonnet.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).with_name("quota-shares.json")
LOCAL_PROJECTS_DIR = Path.home() / ".claude" / "projects"
GATEWAY_PROJECTS_DIR = Path("/home/lifekit/.claude/projects")
GATEWAY_CONTAINER = "compose-openclaw-gateway-1"
GATEWAY_PROJECTS_IN_CONTAINER = "/home/node/.claude/projects"
PROMETHEUS_URL = "http://127.0.0.1:9090"
WINDOW_DAYS = 7
QUOTA_AXI_WINDOW_ID = "seven_day"


@dataclass
class Session:
    cwd: str
    model: str
    weight: float


@dataclass
class ConsumerResult:
    name: str
    configured_share: float | None
    measured_share_pct: float | None
    imputed_used_pct: float | None
    residual: bool = False
    sources: list[str] = field(default_factory=list)


def opus_or_haiku_multiplier(model: str) -> float:
    model_l = (model or "").lower()
    if "opus" in model_l or "fable" in model_l:
        return 5 / 3
    if "haiku" in model_l:
        return 1 / 3
    return 1.0


def usage_weight(usage: dict, model: str) -> float:
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    base = input_tokens + 5 * output_tokens + 1.25 * cache_write + 0.1 * cache_read
    return base * opus_or_haiku_multiplier(model)


def iter_jsonl_lines(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def parse_sessions(
    lines: list[str], since: datetime, seen: set[tuple[str, str]]
) -> list[Session]:
    sessions = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message") or {}
        usage = message.get("usage")
        if not usage:
            continue
        timestamp = record.get("timestamp")
        try:
            when = datetime.fromisoformat((timestamp or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < since:
            continue
        dedup_key = (message.get("id", ""), record.get("requestId", ""))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        cwd = record.get("cwd", "")
        if not cwd:
            continue
        sessions.append(
            Session(
                cwd=cwd,
                model=message.get("model", ""),
                weight=usage_weight(usage, message.get("model", "")),
            )
        )
    return sessions


def local_sessions(since: datetime) -> list[Session]:
    seen: set[tuple[str, str]] = set()
    sessions: list[Session] = []
    if not LOCAL_PROJECTS_DIR.is_dir():
        return sessions
    for jsonl in LOCAL_PROJECTS_DIR.glob("*/*.jsonl"):
        sessions.extend(parse_sessions(iter_jsonl_lines(jsonl), since, seen))
    return sessions


def gateway_sessions(since: datetime) -> tuple[list[Session], str | None]:
    """Returns (sessions, warning). warning is set when the gateway's
    session logs could not be read at all (not when they simply have no
    sessions in the window)."""
    seen: set[tuple[str, str]] = set()
    try:
        readable = GATEWAY_PROJECTS_DIR.is_dir()
    except PermissionError:
        readable = False
    if readable:
        sessions: list[Session] = []
        for jsonl in GATEWAY_PROJECTS_DIR.glob("*/*.jsonl"):
            sessions.extend(parse_sessions(iter_jsonl_lines(jsonl), since, seen))
        return sessions, None

    # One docker exec for every file (there can be 1000+) is too slow; batch
    # the reads into a single `find -exec cat +` inside the container. Each
    # JSONL line is self-contained (cwd, message.id, requestId, timestamp),
    # so nothing is lost by not tracking which file a line came from.
    try:
        cat = subprocess.run(
            [
                "docker",
                "exec",
                GATEWAY_CONTAINER,
                "find",
                GATEWAY_PROJECTS_IN_CONTAINER,
                "-name",
                "*.jsonl",
                "-exec",
                "cat",
                "{}",
                "+",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"could not reach the gateway container: {exc}"
    if cat.returncode != 0:
        return (
            [],
            f"docker exec find/cat failed: {cat.stderr.strip() or cat.returncode}",
        )

    sessions = parse_sessions(cat.stdout.splitlines(), since, seen)
    return sessions, None


def devclaw_metric_tokens() -> tuple[float, str | None]:
    query = f"increase(devclaw_tokens_total[{WINDOW_DAYS}d])"
    url = f"{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(query)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return 0.0, f"could not reach Prometheus for devclaw_tokens_total: {exc}"
    if payload.get("status") != "success":
        return 0.0, "Prometheus query for devclaw_tokens_total did not succeed"
    total = 0.0
    for result in payload.get("data", {}).get("result", []):
        try:
            total += float(result["value"][1])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return total, None


def expand_local_pattern(pattern: str) -> str:
    if pattern.startswith("~/"):
        return str(Path.home() / pattern[2:])
    return pattern


def match_local(cwd: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(cwd, expand_local_pattern(p))
        for p in patterns
        if not p.startswith(("lifekit:", "metric:"))
    )


def match_gateway(cwd: str, patterns: list[str]) -> bool:
    gateway_patterns = [
        p[len("lifekit:") :] for p in patterns if p.startswith("lifekit:")
    ]
    return any(fnmatch.fnmatch(cwd, p) for p in gateway_patterns)


def metric_names(patterns: list[str]) -> list[str]:
    return [p[len("metric:") :] for p in patterns if p.startswith("metric:")]


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    consumers = config.get("consumers", {})
    if not consumers:
        raise ValueError(f"{path} defines no consumers")
    residual = [name for name, c in consumers.items() if c.get("match") == "residual"]
    if len(residual) > 1:
        raise ValueError(f"more than one residual consumer: {residual}")
    return config


def attribute(
    consumers: dict,
    local: list[Session],
    gateway: list[Session],
    metrics: dict[str, float],
) -> tuple[dict[str, float], list[str]]:
    weighted: dict[str, float] = defaultdict(float)
    unmatched = 0.0
    metrics_claimed: set[str] = set()

    for name, cfg in consumers.items():
        patterns = cfg.get("match")
        if patterns == "residual" or not patterns:
            continue
        for metric_name in metric_names(patterns):
            if metric_name in metrics:
                weighted[name] += metrics[metric_name]
                metrics_claimed.add(metric_name)

    for session in local + gateway:
        matched = None
        for name, cfg in consumers.items():
            patterns = cfg.get("match")
            if patterns == "residual" or not patterns:
                continue
            if match_local(session.cwd, patterns) or match_gateway(
                session.cwd, patterns
            ):
                matched = name
                break
        if matched is None:
            unmatched += session.weight
        else:
            weighted[matched] += session.weight

    notes = []
    unclaimed_metrics = set(metrics) - metrics_claimed
    if unclaimed_metrics:
        notes.append(
            f"metrics not claimed by any consumer: {sorted(unclaimed_metrics)}"
        )
    if unmatched:
        notes.append(
            f"{unmatched:.0f} weighted units did not match any consumer's `match` globs"
        )
    return dict(weighted), notes


def compute_results(
    consumers: dict, weighted: dict[str, float], account_percent_used: float
) -> list[ConsumerResult]:
    total = sum(weighted.values())
    results = []
    for name, cfg in consumers.items():
        share = cfg.get("share")
        patterns = cfg.get("match")
        if patterns == "residual":
            results.append(
                ConsumerResult(
                    name=name,
                    configured_share=share,
                    measured_share_pct=None,
                    imputed_used_pct=None,
                    residual=True,
                )
            )
            continue
        consumer_weight = weighted.get(name, 0.0)
        measured_share_pct = (consumer_weight / total * 100) if total else 0.0
        imputed_used_pct = account_percent_used * measured_share_pct / 100
        results.append(
            ConsumerResult(
                name=name,
                configured_share=share,
                measured_share_pct=measured_share_pct,
                imputed_used_pct=imputed_used_pct,
            )
        )
    return results


def run_quota_axi() -> dict:
    proc = subprocess.run(
        [
            "quota-axi",
            "--provider",
            "claude",
            "--no-credential-refresh",
            "--full",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"quota-axi exited {proc.returncode}: {proc.stderr.strip()}")
    payload = json.loads(proc.stdout)
    for provider in payload.get("providers", []):
        if provider.get("provider") != "claude":
            continue
        for window in provider.get("windows", []):
            if window.get("id") == QUOTA_AXI_WINDOW_ID:
                return window
    raise RuntimeError(
        f"quota-axi did not report a {QUOTA_AXI_WINDOW_ID} window for claude"
    )


def format_table(results: list[ConsumerResult], window: dict) -> str:
    lines = []
    pace_status = window.get("pace", {}).get("status", "unknown")
    lines.append(
        f"Account (7-day window): {window.get('percentUsed', '?')}% used, "
        f"resets {window.get('resetsAt', '?')}, pace {pace_status}"
    )
    lines.append("")
    header = f"{'consumer':<12} {'share %':>8} {'measured %':>11} {'imputed used %':>15} {'status':>8}"
    lines.append(header)
    lines.append("-" * len(header))
    for r in results:
        if r.residual:
            share = (
                f"{r.configured_share:.0f}" if r.configured_share is not None else "-"
            )
            lines.append(
                f"{r.name:<12} {share:>8} {'n/a':>11} {'not measured':>15} {'-':>8}"
            )
            continue
        share = f"{r.configured_share:.0f}" if r.configured_share is not None else "-"
        measured = (
            f"{r.measured_share_pct:.1f}" if r.measured_share_pct is not None else "n/a"
        )
        used = f"{r.imputed_used_pct:.1f}" if r.imputed_used_pct is not None else "n/a"
        over = (
            r.configured_share is not None
            and r.imputed_used_pct is not None
            and r.imputed_used_pct > r.configured_share
        )
        status = "OVER" if over else "ok"
        lines.append(f"{r.name:<12} {share:>8} {measured:>11} {used:>15} {status:>8}")
    return "\n".join(lines)


def any_over_share(results: list[ConsumerResult]) -> bool:
    return any(
        r.configured_share is not None
        and r.imputed_used_pct is not None
        and r.imputed_used_pct > r.configured_share
        for r in results
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the report as JSON instead of a table",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    consumers = config["consumers"]

    try:
        window = run_quota_axi()
    except (
        RuntimeError,
        json.JSONDecodeError,
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"quota-share: could not read quota-axi: {exc}", file=sys.stderr)
        return 2

    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    local = local_sessions(since)
    gateway, gateway_warning = gateway_sessions(since)

    metrics: dict[str, float] = {}
    all_metric_names = {
        m
        for cfg in consumers.values()
        if isinstance(cfg.get("match"), list)
        for m in metric_names(cfg["match"])
    }
    warnings = []
    if gateway_warning:
        warnings.append(gateway_warning)
    if "devclaw_tokens_total" in all_metric_names:
        value, metric_warning = devclaw_metric_tokens()
        metrics["devclaw_tokens_total"] = value
        if metric_warning:
            warnings.append(metric_warning)

    weighted, notes = attribute(consumers, local, gateway, metrics)
    results = compute_results(consumers, weighted, window.get("percentUsed", 0))

    if args.json:
        payload = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "window": {
                "id": window.get("id"),
                "percentUsed": window.get("percentUsed"),
                "resetsAt": window.get("resetsAt"),
                "pace": window.get("pace", {}).get("status"),
            },
            "consumers": [
                {
                    "name": r.name,
                    "configuredSharePct": r.configured_share,
                    "measuredSharePct": r.measured_share_pct,
                    "imputedUsedPct": r.imputed_used_pct,
                    "residual": r.residual,
                }
                for r in results
            ],
            "warnings": warnings + notes,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(format_table(results, window))
        for w in warnings + notes:
            print(f"note: {w}", file=sys.stderr)

    pace_ahead = window.get("pace", {}).get("status") == "ahead"
    if pace_ahead and any_over_share(results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
