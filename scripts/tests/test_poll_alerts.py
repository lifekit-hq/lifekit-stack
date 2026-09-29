#!/usr/bin/env python3
"""Tests for scripts/alert-inbox/poll_alerts.py.

Covers the pure logic only: duration parsing, rule parsing against the real
rules.yml shape, threshold evaluation, and the pending/alerting/normal state
machine. Nothing here calls Prometheus or fm-inbox.sh.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "poll_alerts", REPO / "scripts/alert-inbox/poll_alerts.py"
)
poll_alerts = importlib.util.module_from_spec(SPEC)
sys.modules["poll_alerts"] = poll_alerts
SPEC.loader.exec_module(poll_alerts)

RULES_FILE = REPO / "compose/observability/grafana/provisioning/alerting/rules.yml"


class DurationTests(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(poll_alerts.parse_duration("0s"), 0.0)

    def test_minutes(self):
        self.assertEqual(poll_alerts.parse_duration("5m"), 300.0)

    def test_hours(self):
        self.assertEqual(poll_alerts.parse_duration("1h"), 3600.0)

    def test_bad_unit_raises(self):
        with self.assertRaises(ValueError):
            poll_alerts.parse_duration("5x")


class EvaluateConditionTests(unittest.TestCase):
    def test_lt(self):
        self.assertTrue(poll_alerts.evaluate_condition(0, "lt", 1))
        self.assertFalse(poll_alerts.evaluate_condition(1, "lt", 1))

    def test_gt(self):
        self.assertTrue(poll_alerts.evaluate_condition(1, "gt", 0))
        self.assertFalse(poll_alerts.evaluate_condition(0, "gt", 0))

    def test_unsupported_type_raises(self):
        with self.assertRaises(ValueError):
            poll_alerts.evaluate_condition(1, "within_range", 1)


class ParseRealRulesFileTests(unittest.TestCase):
    """Every rule currently in the tracked rules.yml must parse cleanly."""

    @classmethod
    def setUpClass(cls):
        doc = yaml.safe_load(RULES_FILE.read_text())
        cls.rules, cls.warnings = poll_alerts.parse_rules(doc)

    def test_no_warnings(self):
        self.assertEqual(self.warnings, [], self.warnings)

    def test_all_rules_parsed(self):
        doc = yaml.safe_load(RULES_FILE.read_text())
        expected = sum(len(g["rules"]) for g in doc["groups"])
        self.assertEqual(len(self.rules), expected)

    def test_target_down_shape(self):
        (rule,) = [r for r in self.rules if r.uid == "target-down"]
        self.assertEqual(rule.expr, "up")
        self.assertEqual(rule.evaluator_type, "lt")
        self.assertEqual(rule.evaluator_param, 1.0)
        self.assertEqual(rule.for_seconds, 180.0)
        self.assertEqual(rule.no_data_state, "Alerting")
        self.assertEqual(rule.severity, "critical")

    def test_container_oom_killed_shape(self):
        (rule,) = [r for r in self.rules if r.uid == "container-oom-killed"]
        self.assertEqual(rule.evaluator_type, "gt")
        self.assertEqual(rule.evaluator_param, 0.0)
        self.assertEqual(rule.for_seconds, 0.0)
        self.assertEqual(rule.no_data_state, "OK")


class ParseRulesSkipsUnmodeledShapesTests(unittest.TestCase):
    def _rule(self, condition_model, extra_data=None):
        data = [
            {
                "refId": "A",
                "datasourceUid": "prometheus",
                "model": {"refId": "A", "expr": "up"},
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "B",
                    "type": "reduce",
                    "expression": "A",
                    "reducer": "last",
                },
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "model": {"refId": "C", **condition_model},
            },
        ]
        if extra_data:
            data.extend(extra_data)
        return {
            "uid": "test-rule",
            "title": "test rule",
            "condition": "C",
            "for": "0s",
            "labels": {"severity": "warning"},
            "data": data,
        }

    def _doc(self, rule):
        return {"groups": [{"rules": [rule]}]}

    def test_unsupported_evaluator_type_is_skipped(self):
        rule = self._rule(
            {
                "type": "threshold",
                "expression": "B",
                "conditions": [
                    {"evaluator": {"type": "within_range", "params": [0, 1]}}
                ],
            }
        )
        rules, warnings = poll_alerts.parse_rules(self._doc(rule))
        self.assertEqual(rules, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("test-rule", warnings[0])

    def test_non_threshold_condition_is_skipped(self):
        rule = self._rule({"type": "math", "expression": "$B > 0"})
        rules, warnings = poll_alerts.parse_rules(self._doc(rule))
        self.assertEqual(rules, [])
        self.assertEqual(len(warnings), 1)

    def test_paused_rule_is_skipped_silently(self):
        rule = self._rule(
            {
                "type": "threshold",
                "expression": "B",
                "conditions": [{"evaluator": {"type": "gt", "params": [0]}}],
            }
        )
        rule["isPaused"] = True
        rules, warnings = poll_alerts.parse_rules(self._doc(rule))
        self.assertEqual(rules, [])
        self.assertEqual(warnings, [])


class SeriesFingerprintTests(unittest.TestCase):
    def test_drops_metric_name_and_is_order_independent(self):
        a = poll_alerts.series_fingerprint({"__name__": "up", "job": "x", "z": "1"})
        b = poll_alerts.series_fingerprint({"z": "1", "job": "x", "__name__": "up"})
        self.assertEqual(a, b)
        self.assertNotIn("up", a)


class AdvanceStateTests(unittest.TestCase):
    def test_normal_to_pending_no_transition(self):
        entry, transition = poll_alerts.advance_state(None, True, now=100, for_seconds=60)
        self.assertEqual(entry["state"], poll_alerts.STATE_PENDING)
        self.assertEqual(entry["since"], 100)
        self.assertIsNone(transition)

    def test_pending_holds_until_for_elapses(self):
        pending = {"state": poll_alerts.STATE_PENDING, "since": 100}
        entry, transition = poll_alerts.advance_state(
            pending, True, now=130, for_seconds=60
        )
        self.assertEqual(entry["state"], poll_alerts.STATE_PENDING)
        self.assertIsNone(transition)

    def test_pending_becomes_alerting_after_for_elapses(self):
        pending = {"state": poll_alerts.STATE_PENDING, "since": 100}
        entry, transition = poll_alerts.advance_state(
            pending, True, now=161, for_seconds=60
        )
        self.assertEqual(entry["state"], poll_alerts.STATE_ALERTING)
        self.assertEqual(entry["since"], 100)
        self.assertEqual(transition, "start")

    def test_alerting_stays_alerting_no_repeat_notify(self):
        alerting = {"state": poll_alerts.STATE_ALERTING, "since": 100}
        entry, transition = poll_alerts.advance_state(
            alerting, True, now=500, for_seconds=60
        )
        self.assertEqual(entry["state"], poll_alerts.STATE_ALERTING)
        self.assertIsNone(transition)

    def test_alerting_to_normal_resolves(self):
        alerting = {"state": poll_alerts.STATE_ALERTING, "since": 100}
        entry, transition = poll_alerts.advance_state(
            alerting, False, now=500, for_seconds=60
        )
        self.assertIsNone(entry)
        self.assertEqual(transition, "resolve")

    def test_pending_to_normal_is_silent(self):
        pending = {"state": poll_alerts.STATE_PENDING, "since": 100}
        entry, transition = poll_alerts.advance_state(
            pending, False, now=110, for_seconds=60
        )
        self.assertIsNone(entry)
        self.assertIsNone(transition)

    def test_zero_for_fires_on_first_true_poll(self):
        entry, transition = poll_alerts.advance_state(None, True, now=100, for_seconds=0)
        self.assertEqual(entry["state"], poll_alerts.STATE_ALERTING)
        self.assertEqual(transition, "start")


class PollEndToEndTests(unittest.TestCase):
    """Drive poll() against a stub Prometheus and fm-inbox.sh, over multiple ticks."""

    def _write_rules(self, tmp_path, for_seconds="0s"):
        rules_file = tmp_path / "rules.yml"
        rules_file.write_text(
            yaml.safe_dump(
                {
                    "groups": [
                        {
                            "name": "box",
                            "rules": [
                                {
                                    "uid": "target-down",
                                    "title": "scrape target is down",
                                    "condition": "C",
                                    "for": for_seconds,
                                    "noDataState": "OK",
                                    "labels": {"severity": "critical"},
                                    "data": [
                                        {
                                            "refId": "A",
                                            "datasourceUid": "prometheus",
                                            "model": {"refId": "A", "expr": "up"},
                                        },
                                        {
                                            "refId": "B",
                                            "datasourceUid": "__expr__",
                                            "model": {
                                                "refId": "B",
                                                "type": "reduce",
                                                "expression": "A",
                                                "reducer": "last",
                                            },
                                        },
                                        {
                                            "refId": "C",
                                            "datasourceUid": "__expr__",
                                            "model": {
                                                "refId": "C",
                                                "type": "threshold",
                                                "expression": "B",
                                                "conditions": [
                                                    {
                                                        "evaluator": {
                                                            "type": "lt",
                                                            "params": [1],
                                                        }
                                                    }
                                                ],
                                            },
                                        },
                                    ],
                                }
                            ],
                        }
                    ]
                }
            )
        )
        return rules_file

    def test_start_and_resolve_transitions_notify_exactly_once(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            tmp_path = Path(d)
            rules_file = self._write_rules(tmp_path, for_seconds="0s")
            state_file = tmp_path / "state.json"
            notified: list[str] = []

            results_sequence = [
                [{"metric": {"job": "x"}, "value": [0, "0"]}],  # down -> starts
                [{"metric": {"job": "x"}, "value": [0, "0"]}],  # stays down, no repeat
                [{"metric": {"job": "x"}, "value": [0, "1"]}],  # back up -> resolves
            ]

            def fake_query_instant(prometheus_url, expr, timeout=10.0):
                return results_sequence.pop(0)

            original_query = poll_alerts.query_instant
            original_notify = poll_alerts.notify
            poll_alerts.query_instant = fake_query_instant
            poll_alerts.notify = lambda bin_, home, text, dry_run: notified.append(text)
            try:
                for t in (1000, 1001, 1002):
                    poll_alerts.poll(
                        rules_file=rules_file,
                        prometheus_url="http://unused",
                        state_file=state_file,
                        fm_inbox_bin="unused",
                        fm_home=None,
                        dry_run=False,
                        now=t,
                    )
            finally:
                poll_alerts.query_instant = original_query
                poll_alerts.notify = original_notify

            self.assertEqual(len(notified), 2)
            self.assertIn("firing", notified[0])
            self.assertIn("resolved", notified[1])

    def test_prometheus_outage_notifies_once_then_recovery(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            tmp_path = Path(d)
            rules_file = self._write_rules(tmp_path)
            state_file = tmp_path / "state.json"
            notified: list[str] = []

            call_count = {"n": 0}

            def flaky_query_instant(prometheus_url, expr, timeout=10.0):
                call_count["n"] += 1
                if call_count["n"] <= 2:
                    raise poll_alerts.PrometheusError("connection refused")
                return [{"metric": {"job": "x"}, "value": [0, "1"]}]

            original_query = poll_alerts.query_instant
            original_notify = poll_alerts.notify
            poll_alerts.query_instant = flaky_query_instant
            poll_alerts.notify = lambda bin_, home, text, dry_run: notified.append(text)
            try:
                for t in (1000, 1001, 1002):
                    poll_alerts.poll(
                        rules_file=rules_file,
                        prometheus_url="http://unused",
                        state_file=state_file,
                        fm_inbox_bin="unused",
                        fm_home=None,
                        dry_run=False,
                        now=t,
                    )
            finally:
                poll_alerts.query_instant = original_query
                poll_alerts.notify = original_notify

            self.assertEqual(len(notified), 2)
            self.assertIn("cannot reach Prometheus", notified[0])
            self.assertIn("reachable again", notified[1])


if __name__ == "__main__":
    unittest.main()
