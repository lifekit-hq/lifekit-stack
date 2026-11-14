#!/usr/bin/env python3
"""Estimate OpenClaw's Anthropic API spend, export it, and fall back to subscription.

OpenClaw's usage records carry tokens, not dollars, and only turns the direct
runtime answers (provider "anthropic") are billed to the API credit; turns on
the claude-cli runtime ride the subscription. usage-extract.mjs, run inside the
gateway, lists the former; this script prices them with prices.json, keeps a
host-side ledger (so spend survives OpenClaw pruning old sessions), measures
them against credit.json, and writes a node-exporter textfile metric.

    spend_guard.py run     [--textfile DIR]   one guard pass (the timer's job)
    spend_guard.py report  [--json]           print the estimate, change nothing
                                              but the ledger (when writable)

The guard pass, when auth-mode.sh says the mode is "api", flips to subscription
(auth-mode.sh subscription) when any of these holds: spend reached
fallback_percent, OpenClaw disabled the anthropic:api-key profile for billing,
or a live probe says the key is rejected or out of credit. The reason is kept in
a marker file and exported as openclaw_api_fallback_active{reason} until the
guard next sees mode "api" again, which is what the Grafana rule alerts on.
When the usage extraction fails the pass still checks the mode, the billing
state and the key probe (the signals that do not need the estimate), skips the
spend rule and the spend metrics, and exits 2 afterwards.
Exit 0 on a completed pass, 1 when a needed flip failed, 2 when an input cannot
be read (without the mode the previous textfile is left in place).

Metrics (openclaw_api_spend.prom):
  openclaw_api_spend_usd{window="since_topup|month"}   estimated dollars
  openclaw_api_credit_usd                              credit.json credit_usd
  openclaw_api_credit_used_percent                     since_topup / credit * 100
  openclaw_api_auth_mode{mode="api|subscription|mixed"} 1 for the current mode
  openclaw_api_key_billing_disabled                    1 when a profile is disabled for billing
  openclaw_api_key_probe_ok                            1 ok, 0 refused (api mode only)
  openclaw_api_fallback_active{reason}                 1 while the guard's fallback stands
  openclaw_api_spend_unpriced_tokens                   tokens priced at 'default'
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = "/var/lib/lifekit-openclaw-spend"
PROM_NAME = "openclaw_api_spend.prom"
DAY_MS = 86_400_000
MODES = ("api", "subscription", "mixed")


class InputError(Exception):
    """An input the pass needs could not be read; nothing is written."""


def parse_time(text: str) -> int:
    """ISO-8601 (a trailing Z is UTC) -> epoch milliseconds."""
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.timestamp() * 1000)


def month_start_ms(now_ms: int) -> int:
    now = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    return int(
        now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )


def load_config(config_dir: Path) -> tuple[dict, dict]:
    try:
        credit = json.loads((config_dir / "credit.json").read_text())
        prices = json.loads((config_dir / "prices.json").read_text())
        credit["since_ms"] = parse_time(credit["since"])
        float(credit["credit_usd"])
        float(credit["fallback_percent"])
        prices["default"]["input"], prices["default"]["output"]
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise InputError(f"config unreadable: {err.__class__.__name__}: {err}") from err
    return credit, prices


def price_for(model: str, prices: dict) -> tuple[float, float, bool]:
    for row in prices["models"]:
        if row["match"] in model:
            return float(row["input"]), float(row["output"]), True
    return float(prices["default"]["input"]), float(prices["default"]["output"]), False


def record_usd(rec: dict, prices: dict) -> tuple[float, bool]:
    rate_in, rate_out, matched = price_for(rec["model"], prices)
    usd = (
        rec["in"] * rate_in
        + rec["out"] * rate_out
        + rec["cw"] * rate_in * float(prices["cache_write_multiplier"])
        + rec["cr"] * rate_in * float(prices["cache_read_multiplier"])
    ) / 1_000_000
    return usd, matched


def summarize(records: list[dict], credit: dict, prices: dict, now_ms: int) -> dict:
    month_ms = month_start_ms(now_ms)
    since = month = 0.0
    unpriced = 0
    for rec in records:
        usd, matched = record_usd(rec, prices)
        if rec["ts"] >= credit["since_ms"]:
            since += usd
            if not matched:
                unpriced += rec["in"] + rec["out"] + rec["cw"] + rec["cr"]
        if rec["ts"] >= month_ms:
            month += usd
    total = float(credit["credit_usd"])
    return {
        "since_topup_usd": round(since, 4),
        "month_usd": round(month, 4),
        "credit_usd": total,
        "used_percent": round(since / total * 100, 2) if total > 0 else 100.0,
        "fallback_percent": float(credit["fallback_percent"]),
        "unpriced_tokens": unpriced,
        "since": credit["since"],
    }


def decide(
    mode: str, est: dict | None, billing_disabled: bool, probe: str | None
) -> str | None:
    """The reason to fall back to subscription, or None. Only an api-mode box flips."""
    if mode != "api":
        return None
    if billing_disabled:
        return "billing"
    if probe in ("rejected", "exhausted"):
        return "key-refused"
    if est and est["used_percent"] >= est["fallback_percent"]:
        return "spend"
    return None


# ── ledger ────────────────────────────────────────────────────────────────────


def read_ledger(path: Path) -> dict[str, dict]:
    ledger: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
                ledger[f"{rec['agent']}:{rec['id']}"] = rec
            except (ValueError, KeyError):
                continue  # a torn last line is re-extracted next pass
    return ledger


def merge_ledger(path: Path, ledger: dict[str, dict], fresh: list[dict]) -> None:
    new = []
    for rec in fresh:
        key = f"{rec['agent']}:{rec['id']}"
        if key not in ledger:
            ledger[key] = rec
            new.append(rec)
    if not new:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as out:
            out.writelines(json.dumps(rec, separators=(",", ":")) + "\n" for rec in new)
    except OSError:
        pass  # a reader without write access still gets this pass's numbers


def extract(docker: str, container: str, since_ms: int) -> list[dict]:
    script = (HERE / "usage-extract.mjs").read_text()
    proc = subprocess.run(
        [
            docker,
            "exec",
            "-i",
            "-e",
            f"SINCE_MS={since_ms}",
            container,
            "node",
            "--input-type=module",
            "-",
        ],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise InputError(f"usage extraction failed: {proc.stderr.strip()[:200]}")
    out = []
    for line in proc.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def gateway_container(docker: str) -> str:
    proc = subprocess.run(
        [
            docker, "ps", "-q",
            "--filter", "label=com.docker.compose.project=openclaw",
            "--filter", "label=com.docker.compose.service=openclaw-gateway",
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    cid = proc.stdout.split()[0] if proc.returncode == 0 and proc.stdout.split() else ""
    if not cid:
        raise InputError("no running openclaw-gateway container")
    return cid


def collect(args, credit: dict, prices: dict, now_ms: int) -> dict:
    state = Path(args.state_dir)
    ledger_path = state / "ledger.jsonl"
    ledger = read_ledger(ledger_path)
    floor = min(credit["since_ms"], month_start_ms(now_ms))
    newest = max((r["ts"] for r in ledger.values()), default=0)
    since = max(floor, newest - 36 * 3_600_000) if newest else floor
    fresh = extract(args.docker, gateway_container(args.docker), since)
    merge_ledger(ledger_path, ledger, fresh)
    return summarize(list(ledger.values()), credit, prices, now_ms)


# ── auth-mode.sh ──────────────────────────────────────────────────────────────


def auth_mode(args, *words: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HERE / "auth-mode.sh"), *words],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "DOCKER": args.docker},
    )


def current_state(args) -> dict:
    """Mode, and in api mode the per-agent key state. One gateway call when not api."""
    proc = auth_mode(args, "mode")
    mode = proc.stdout.strip()
    if mode not in MODES:
        raise InputError(f"auth-mode.sh mode unreadable: {proc.stderr.strip()[:200]}")
    if mode != "api":
        return {"mode": mode, "billingDisabled": False}
    proc = auth_mode(args, "status", "--json", "--quick")
    try:
        state = json.loads(proc.stdout)
        if state["mode"] not in MODES:
            raise KeyError(state["mode"])
    except (ValueError, KeyError) as err:
        raise InputError(
            f"auth-mode.sh status unreadable: {proc.stderr.strip()[:200]}"
        ) from err
    return state


def probe_key(args) -> str | None:
    proc = auth_mode(args, "probe")
    try:
        return json.loads(proc.stdout)["result"]
    except (ValueError, KeyError):
        return None


# ── output ────────────────────────────────────────────────────────────────────


def esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prom(
    est: dict | None, state: dict, probe: str | None, marker: dict | None
) -> str:
    metrics: list[tuple[str, str, list[tuple[str, float]]]] = []
    if est:
        metrics += [
            (
                "openclaw_api_spend_usd|Estimated API spend in USD at list prices.",
                "gauge",
                [
                    ('{window="since_topup"}', est["since_topup_usd"]),
                    ('{window="month"}', est["month_usd"]),
                ],
            ),
            (
                "openclaw_api_credit_usd|Prepaid API credit in USD (scripts/openclaw-auth/credit.json).",
                "gauge",
                [("", est["credit_usd"])],
            ),
            (
                "openclaw_api_credit_used_percent|Share of the credit estimated spent since its start date.",
                "gauge",
                [("", est["used_percent"])],
            ),
            (
                "openclaw_api_spend_unpriced_tokens|Tokens priced at the default rate because prices.json has no row for their model.",
                "gauge",
                [("", est["unpriced_tokens"])],
            ),
        ]
    metrics += [
        (
            "openclaw_api_auth_mode|1 for the mode OpenClaw's anthropic models run in.",
            "gauge",
            [(f'{{mode="{m}"}}', int(state["mode"] == m)) for m in MODES],
        ),
        (
            "openclaw_api_key_billing_disabled|1 when OpenClaw disabled an anthropic:api-key profile for billing.",
            "gauge",
            [("", int(bool(state.get("billingDisabled"))))],
        ),
    ]
    if probe is not None:
        metrics.append(
            (
                "openclaw_api_key_probe_ok|1 when the live key probe was accepted, 0 when refused.",
                "gauge",
                [("", int(probe not in ("rejected", "exhausted")))],
            )
        )
    if marker:
        metrics.append(
            (
                "openclaw_api_fallback_active|1 while the spend guard's own flip to subscription stands.",
                "gauge",
                [(f'{{reason="{esc(marker["reason"])}"}}', 1)],
            )
        )
    lines = []
    for head, kind, samples in metrics:
        name, helptext = head.split("|", 1)
        lines += [f"# HELP {name} {helptext}", f"# TYPE {name} {kind}"]
        lines += [f"{name}{labels} {value}" for labels, value in samples]
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run_pass(args, credit: dict, prices: dict, now_ms: int) -> int:
    est = None
    try:
        est = collect(args, credit, prices, now_ms)
    except InputError as err:
        print(
            f"spend guard: {err}; carrying on without the spend estimate",
            file=sys.stderr,
        )
    state = current_state(args)
    marker_path = Path(args.state_dir) / "fallback.json"
    marker = None
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text())
        except (OSError, ValueError):
            marker = {"reason": "unknown"}
    probe = probe_key(args) if state["mode"] == "api" else None
    reason = decide(state["mode"], est, bool(state.get("billingDisabled")), probe)
    code = 0
    if reason:
        text = f"spend guard: {reason}"
        if est:
            text += f" (estimated {est['used_percent']}% of ${est['credit_usd']:.0f} credit used)"
        proc = auth_mode(args, "subscription", "--reason", text)
        print(text, file=sys.stderr)
        if proc.returncode == 0:
            marker = {
                "reason": reason,
                "at": now_ms // 1000,
                "used_percent": est["used_percent"] if est else None,
            }
            Path(args.state_dir).mkdir(parents=True, exist_ok=True)
            marker_path.write_text(json.dumps(marker))
            state["mode"] = "subscription"
        else:
            print(
                f"spend guard: flip to subscription failed: {proc.stderr.strip()[-300:]}",
                file=sys.stderr,
            )
            code = 1
    elif state["mode"] == "api" and marker:
        marker_path.unlink(missing_ok=True)  # api mode is back: the fallback is over
        marker = None
    if args.textfile:
        write_atomic(
            Path(args.textfile) / PROM_NAME, render_prom(est, state, probe, marker)
        )
    return code or (2 if est is None else 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("run", "report"))
    parser.add_argument("--textfile", help="node-exporter textfile directory (run)")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("OPENCLAW_SPEND_STATE_DIR", DEFAULT_STATE_DIR),
    )
    parser.add_argument("--config-dir", default=str(HERE))
    parser.add_argument("--docker", default=os.environ.get("DOCKER", "docker"))
    parser.add_argument(
        "--now-ms", type=int, default=int(time.time() * 1000), help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    try:
        credit, prices = load_config(Path(args.config_dir))
        if args.command == "report":
            est = collect(args, credit, prices, args.now_ms)
            if args.json:
                print(json.dumps(est))
            else:
                print(
                    f"since {est['since']}: ${est['since_topup_usd']:.2f} of ${est['credit_usd']:.0f} "
                    f"({est['used_percent']:.1f}%); this month ${est['month_usd']:.2f}; "
                    f"fallback at {est['fallback_percent']:.0f}%"
                )
            return 0
        return run_pass(args, credit, prices, args.now_ms)
    except InputError as err:
        print(f"spend guard: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
