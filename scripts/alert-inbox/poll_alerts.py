#!/usr/bin/env python3
"""alert-inbox - pull firing Grafana alerts into an fm-inbox note, on transitions only.

Why this exists (2026-09-29 monitoring read): every Grafana alert routes to
exactly one Telegram chat on the owner's phone, so nothing reaches an agent.
Grafana's HTTP API needs admin credentials this account cannot read and must
not use. Prometheus's query API (http://127.0.0.1:9090, loopback-bound,
unauthenticated) already scrapes Grafana's own alerting metrics, but those
are counts only (`grafana_alerting_alerts{state=...}` has no rule name -
confirmed live, 2026-09-29). So this evaluates the provisioned rules' own
PromQL directly against Prometheus, rather than hand-copying thresholds into
a second place they can drift from rules.yml.

Every rule in rules.yml has the same shape (checked, 2026-09-29, all 20
current rules): refId A is a raw instant PromQL query on the `prometheus`
datasource, refId B reduces it with `last`/`dropNN`, refId C is a single
`lt`/`gt` threshold against one param. This script reproduces exactly that:
query A, drop non-numeric samples, apply the rule's own threshold - it does
not reimplement Grafana's wider expression language (math, multi-step
reduce, classic conditions), so a future rule that uses those will be
skipped with a warning rather than silently misjudged.

`for` is approximated as wall-clock elapsed since the condition first read
true, using this poller's own state file - coarser than Grafana's continuous
evaluation, quantized to how often this script runs (the documented install
is every 5 minutes), but it reuses the rule's own `for:` duration rather than
inventing a new one.

Notifies only on state transitions (pending/normal -> alerting, and
alerting -> normal) via `fm-inbox.sh note`, one line per transition. A
Prometheus connection failure is its own transition ("prometheus-unreachable"
starting/resolving) so an outage is reported once, not every poll.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_FILE = (
    REPO_ROOT / "compose/observability/grafana/provisioning/alerting/rules.yml"
)
DEFAULT_PROMETHEUS_URL = "http://127.0.0.1:9090"
DEFAULT_STATE_FILE = Path.home() / ".local/state/lifekit-alert-inbox/state.json"
OUTAGE_UID = "prometheus-unreachable"

STATE_NORMAL = "normal"
STATE_PENDING = "pending"
STATE_ALERTING = "alerting"


class Rule:
    def __init__(
        self,
        uid: str,
        title: str,
        severity: str,
        expr: str,
        evaluator_type: str,
        evaluator_param: float,
        for_seconds: float,
        no_data_state: str,
    ) -> None:
        self.uid = uid
        self.title = title
        self.severity = severity
        self.expr = expr
        self.evaluator_type = evaluator_type
        self.evaluator_param = evaluator_param
        self.for_seconds = for_seconds
        self.no_data_state = no_data_state


_FOR_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    """Parse a Grafana-style duration like '5m' or '0s' into seconds."""
    text = text.strip()
    if not text:
        return 0.0
    unit = text[-1]
    if unit not in _FOR_UNITS:
        raise ValueError(f"unrecognized duration unit in {text!r}")
    return float(text[:-1]) * _FOR_UNITS[unit]


def parse_rules(doc: dict[str, Any]) -> tuple[list[Rule], list[str]]:
    """Parse the standard-shape rules out of a rules.yml document.

    Returns (rules, warnings) - a rule using a data-flow this script does not
    model (anything other than one PromQL query -> reduce last/dropNN ->
    single lt/gt threshold) is skipped and named in warnings, never guessed at.
    """
    rules: list[Rule] = []
    warnings: list[str] = []
    for group in doc.get("groups", []):
        for rule in group.get("rules", []):
            uid = rule.get("uid", "<unknown>")
            if rule.get("isPaused"):
                continue
            try:
                by_ref = {d["refId"]: d for d in rule["data"]}
                cond_ref = rule["condition"]
                cond = by_ref[cond_ref]
                cond_model = cond["model"]
                if cond_model.get("type") != "threshold":
                    raise ValueError("condition is not a threshold expression")
                (evaluator,) = cond_model["conditions"]
                ev = evaluator["evaluator"]
                if ev["type"] not in ("lt", "gt"):
                    raise ValueError(f"unsupported evaluator type {ev['type']!r}")
                (param,) = ev["params"]
                reduce_ref = cond_model["expression"]
                reduce_d = by_ref[reduce_ref]
                reduce_model = reduce_d["model"]
                if reduce_model.get("type") != "reduce":
                    raise ValueError("threshold does not feed from a reduce step")
                if reduce_model.get("reducer") != "last":
                    raise ValueError(
                        f"unsupported reducer {reduce_model.get('reducer')!r}"
                    )
                query_ref = reduce_model["expression"]
                query_d = by_ref[query_ref]
                if query_d.get("datasourceUid") != "prometheus":
                    raise ValueError("reduce does not feed from a prometheus query")
                expr = query_d["model"]["expr"]
                for_seconds = parse_duration(str(rule.get("for", "0s")))
            except (KeyError, ValueError) as exc:
                warnings.append(f"{uid}: cannot evaluate ({exc})")
                continue
            rules.append(
                Rule(
                    uid=uid,
                    title=rule.get("title", uid),
                    severity=rule.get("labels", {}).get("severity", "unknown"),
                    expr=expr,
                    evaluator_type=ev["type"],
                    evaluator_param=float(param),
                    for_seconds=for_seconds,
                    no_data_state=rule.get("noDataState", "OK"),
                )
            )
    return rules, warnings


def evaluate_condition(value: float, evaluator_type: str, param: float) -> bool:
    if evaluator_type == "lt":
        return value < param
    if evaluator_type == "gt":
        return value > param
    raise ValueError(f"unsupported evaluator type {evaluator_type!r}")


def series_fingerprint(metric: dict[str, str]) -> str:
    labels = {k: v for k, v in metric.items() if k != "__name__"}
    return json.dumps(labels, sort_keys=True)


class PrometheusError(Exception):
    pass


def query_instant(prometheus_url: str, expr: str, timeout: float = 10.0) -> list[dict]:
    url = f"{prometheus_url}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise PrometheusError(str(exc)) from exc
    if payload.get("status") != "success":
        raise PrometheusError(payload.get("error", "query did not succeed"))
    return payload.get("data", {}).get("result", [])


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def advance_state(
    entry: dict[str, Any] | None, condition_true: bool, now: float, for_seconds: float
) -> tuple[dict[str, Any] | None, str | None]:
    """Advance one series' state machine by one poll.

    Returns (new_entry, transition) where transition is "start", "resolve",
    or None. new_entry of None means "drop this series from the state file"
    (it is back to normal and was never reported firing).
    """
    state = (entry or {}).get("state", STATE_NORMAL)
    since = (entry or {}).get("since", now)

    if condition_true:
        if state == STATE_NORMAL:
            since = now
            if now - since >= for_seconds:
                return {"state": STATE_ALERTING, "since": since}, "start"
            return {"state": STATE_PENDING, "since": since}, None
        if state == STATE_PENDING:
            if now - since >= for_seconds:
                return {"state": STATE_ALERTING, "since": since}, "start"
            return {"state": STATE_PENDING, "since": since}, None
        return {"state": STATE_ALERTING, "since": since}, None

    if state == STATE_ALERTING:
        return None, "resolve"
    return None, None


def format_started(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(epoch))


def notify(fm_inbox_bin: str, fm_home: str | None, text: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] would notify: {text}")
        return
    env = dict(os.environ)
    if fm_home:
        env["FM_HOME"] = fm_home
    subprocess.run([fm_inbox_bin, "note", "--", text], check=True, env=env)


def poll(
    rules_file: Path,
    prometheus_url: str,
    state_file: Path,
    fm_inbox_bin: str,
    fm_home: str | None,
    dry_run: bool,
    now: float | None = None,
) -> int:
    import yaml

    now = time.time() if now is None else now
    state = load_state(state_file)
    rules_state: dict[str, dict] = state.setdefault("rules", {})
    outage_state: dict | None = state.get("outage")

    doc = yaml.safe_load(rules_file.read_text())
    rules, warnings = parse_rules(doc)
    for warning in warnings:
        print(f"alert-inbox: skipping {warning}", file=sys.stderr)

    try:
        results_by_uid: dict[str, list[dict]] = {}
        for rule in rules:
            results_by_uid[rule.uid] = query_instant(prometheus_url, rule.expr)
    except PrometheusError as exc:
        new_entry, transition = advance_state(outage_state, True, now, 0.0)
        state["outage"] = new_entry
        if transition == "start":
            notify(
                fm_inbox_bin,
                fm_home,
                f"alert-inbox: cannot reach Prometheus at {prometheus_url}"
                f" since {format_started(now)}: {exc}",
                dry_run,
            )
        save_state(state_file, state)
        return 1

    if outage_state is not None:
        new_entry, transition = advance_state(outage_state, False, now, 0.0)
        state["outage"] = new_entry
        if transition == "resolve":
            notify(
                fm_inbox_bin,
                fm_home,
                "alert-inbox: Prometheus is reachable again",
                dry_run,
            )

    for rule in rules:
        rule_state: dict[str, dict] = rules_state.setdefault(rule.uid, {})
        results = results_by_uid[rule.uid]
        seen_fingerprints: set[str] = set()

        for series in results:
            try:
                value = float(series["value"][1])
            except (KeyError, IndexError, ValueError, TypeError):
                continue
            fp = series_fingerprint(series.get("metric", {}))
            seen_fingerprints.add(fp)
            condition_true = evaluate_condition(
                value, rule.evaluator_type, rule.evaluator_param
            )
            new_entry, transition = advance_state(
                rule_state.get(fp), condition_true, now, rule.for_seconds
            )
            if new_entry is None:
                rule_state.pop(fp, None)
            else:
                rule_state[fp] = new_entry
            if transition:
                labels = {
                    k: v for k, v in series.get("metric", {}).items() if k != "__name__"
                }
                label_text = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
                if transition == "start":
                    notify(
                        fm_inbox_bin,
                        fm_home,
                        f"alert firing: {rule.title} [{rule.severity}]"
                        f" ({label_text}) since {format_started(new_entry['since'])}",
                        dry_run,
                    )
                else:
                    notify(
                        fm_inbox_bin,
                        fm_home,
                        f"alert resolved: {rule.title} [{rule.severity}]"
                        f" ({label_text})",
                        dry_run,
                    )

        if not results and rule.no_data_state == "Alerting":
            fp = "__no_data__"
            new_entry, transition = advance_state(
                rule_state.get(fp), True, now, rule.for_seconds
            )
            rule_state[fp] = new_entry
            if transition == "start":
                notify(
                    fm_inbox_bin,
                    fm_home,
                    f"alert firing: {rule.title} [{rule.severity}]"
                    f" (no data) since {format_started(new_entry['since'])}",
                    dry_run,
                )
        else:
            for fp in list(rule_state.keys()):
                if fp == "__no_data__" and results and rule.no_data_state == "Alerting":
                    entry = rule_state[fp]
                    new_entry, transition = advance_state(entry, False, now, 0.0)
                    if new_entry is None:
                        rule_state.pop(fp, None)
                    if transition == "resolve":
                        notify(
                            fm_inbox_bin,
                            fm_home,
                            f"alert resolved: {rule.title} [{rule.severity}] (no data)",
                            dry_run,
                        )
                elif fp not in seen_fingerprints and fp != "__no_data__":
                    entry = rule_state[fp]
                    new_entry, transition = advance_state(entry, False, now, 0.0)
                    if new_entry is None:
                        rule_state.pop(fp, None)
                    if transition == "resolve":
                        labels = json.loads(fp)
                        label_text = ", ".join(
                            f"{k}={v}" for k, v in sorted(labels.items())
                        )
                        notify(
                            fm_inbox_bin,
                            fm_home,
                            f"alert resolved: {rule.title} [{rule.severity}]"
                            f" ({label_text})",
                            dry_run,
                        )

        if not rule_state:
            rules_state.pop(rule.uid, None)

    save_state(state_file, state)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules-file", type=Path, default=DEFAULT_RULES_FILE)
    parser.add_argument("--prometheus-url", default=DEFAULT_PROMETHEUS_URL)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument(
        "--fm-inbox-bin",
        default=os.environ.get("FM_INBOX_BIN", ""),
        help="path to fm-inbox.sh (or set FM_INBOX_BIN); required unless --dry-run",
    )
    parser.add_argument(
        "--fm-home",
        default=os.environ.get("FM_INBOX_HOME"),
        help="FM_HOME to pass to fm-inbox.sh (or set FM_INBOX_HOME); "
        "defaults to fm-inbox.sh's own home",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print notifications instead of calling fm-inbox.sh",
    )
    args = parser.parse_args(argv)

    if not args.dry_run and not args.fm_inbox_bin:
        parser.error("--fm-inbox-bin (or FM_INBOX_BIN) is required unless --dry-run")

    return poll(
        rules_file=args.rules_file,
        prometheus_url=args.prometheus_url,
        state_file=args.state_file,
        fm_inbox_bin=args.fm_inbox_bin,
        fm_home=args.fm_home,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
