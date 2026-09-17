#!/usr/bin/env python3
"""Tests for scripts/quota-share/quota_share.py.

Covers the pure logic only: usage weighting, JSONL dedup/parsing, consumer
matching, and share math. Nothing here calls quota-axi, docker, or
Prometheus.
"""

import importlib.util
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "quota_share", os.path.join(os.path.dirname(HERE), "quota_share.py")
)
quota_share = importlib.util.module_from_spec(SPEC)
sys.modules["quota_share"] = (
    quota_share  # dataclasses need __module__ resolvable in sys.modules
)
SPEC.loader.exec_module(quota_share)


class UsageWeightTests(unittest.TestCase):
    def test_sonnet_weight_matches_formula(self):
        usage = {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
        }
        expected = 10 + 5 * 20 + 1.25 * 100 + 0.1 * 1000
        self.assertAlmostEqual(
            quota_share.usage_weight(usage, "claude-sonnet-5"), expected
        )

    def test_opus_gets_5_3_multiplier(self):
        usage = {
            "input_tokens": 100,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        self.assertAlmostEqual(
            quota_share.usage_weight(usage, "claude-opus-5"), 100 * (5 / 3)
        )

    def test_haiku_gets_1_3_multiplier(self):
        usage = {
            "input_tokens": 100,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        self.assertAlmostEqual(
            quota_share.usage_weight(usage, "claude-haiku-4-5-20251001"), 100 * (1 / 3)
        )

    def test_fable_gets_opus_multiplier(self):
        usage = {
            "input_tokens": 100,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        self.assertAlmostEqual(
            quota_share.usage_weight(usage, "claude-fable-5-1"), 100 * (5 / 3)
        )


class ParseSessionsTests(unittest.TestCase):
    def _line(self, msg_id, request_id, cwd, when, model="claude-sonnet-5"):
        import json

        return json.dumps(
            {
                "message": {
                    "id": msg_id,
                    "model": model,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
                "requestId": request_id,
                "cwd": cwd,
                "timestamp": when,
            }
        )

    def test_dedupes_by_message_id_and_request_id(self):
        now = datetime.now(timezone.utc).isoformat()
        lines = [self._line("m1", "r1", "/home/denys/firstmate", now)] * 3
        seen = set()
        sessions = quota_share.parse_sessions(
            lines, datetime.now(timezone.utc) - timedelta(days=7), seen
        )
        self.assertEqual(len(sessions), 1)

    def test_drops_lines_older_than_window(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        lines = [self._line("m1", "r1", "/home/denys/firstmate", old)]
        sessions = quota_share.parse_sessions(
            lines, datetime.now(timezone.utc) - timedelta(days=7), set()
        )
        self.assertEqual(sessions, [])

    def test_skips_lines_without_usage(self):
        lines = [
            '{"message": {"id": "m1"}, "cwd": "/x", "timestamp": "2026-09-16T00:00:00Z"}'
        ]
        sessions = quota_share.parse_sessions(
            lines, datetime.now(timezone.utc) - timedelta(days=7), set()
        )
        self.assertEqual(sessions, [])

    def test_skips_malformed_json(self):
        sessions = quota_share.parse_sessions(
            ["not json"], datetime.now(timezone.utc) - timedelta(days=7), set()
        )
        self.assertEqual(sessions, [])


class MatchingTests(unittest.TestCase):
    def test_local_tilde_pattern_matches_nested_path(self):
        home = quota_share.Path.home()
        cwd = str(home / ".treehouse" / "finance-sentry-abc" / "3" / "finance-sentry")
        self.assertTrue(quota_share.match_local(cwd, ["~/.treehouse/*"]))

    def test_local_pattern_does_not_match_other_consumer(self):
        home = quota_share.Path.home()
        cwd = str(home / "some-other-project")
        self.assertFalse(
            quota_share.match_local(cwd, ["~/.treehouse/*", "~/firstmate"])
        )

    def test_gateway_pattern_matches_prefixed_glob_only(self):
        patterns = ["lifekit:/app", "~/firstmate"]
        self.assertTrue(quota_share.match_gateway("/app", patterns))
        self.assertFalse(quota_share.match_gateway("/app", ["~/firstmate"]))

    def test_metric_names_extracts_metric_prefixed_entries(self):
        patterns = ["lifekit:/app", "metric:devclaw_tokens_total", "~/firstmate"]
        self.assertEqual(quota_share.metric_names(patterns), ["devclaw_tokens_total"])


class AttributeTests(unittest.TestCase):
    def _consumers(self):
        return {
            "captain": {"share": 35, "match": "residual"},
            "firstmate": {"share": 30, "match": ["~/firstmate"]},
            "devclaw": {
                "share": 20,
                "match": ["lifekit:/app", "metric:devclaw_tokens_total"],
            },
        }

    def test_splits_weighted_usage_by_consumer(self):
        home = quota_share.Path.home()
        local = [
            quota_share.Session(
                cwd=str(home / "firstmate"), model="claude-sonnet-5", weight=100.0
            )
        ]
        gateway = [
            quota_share.Session(cwd="/app", model="claude-sonnet-5", weight=50.0)
        ]
        weighted, notes = quota_share.attribute(self._consumers(), local, gateway, {})
        self.assertEqual(weighted, {"firstmate": 100.0, "devclaw": 50.0})
        self.assertEqual(notes, [])

    def test_unmatched_session_is_noted_not_dropped_silently(self):
        local = [
            quota_share.Session(
                cwd="/no/such/consumer", model="claude-sonnet-5", weight=100.0
            )
        ]
        weighted, notes = quota_share.attribute(self._consumers(), local, [], {})
        self.assertEqual(weighted, {})
        self.assertTrue(any("did not match" in n for n in notes))

    def test_metric_is_added_to_its_claiming_consumer(self):
        weighted, notes = quota_share.attribute(
            self._consumers(), [], [], {"devclaw_tokens_total": 42.0}
        )
        self.assertEqual(weighted["devclaw"], 42.0)
        self.assertEqual(notes, [])

    def test_unclaimed_metric_is_noted(self):
        _, notes = quota_share.attribute(
            self._consumers(), [], [], {"some_other_metric": 1.0}
        )
        self.assertTrue(any("not claimed" in n for n in notes))


class ComputeResultsTests(unittest.TestCase):
    def test_residual_consumer_has_no_measured_or_imputed_values(self):
        consumers = {
            "captain": {"share": 35, "match": "residual"},
            "firstmate": {"share": 30, "match": ["~/firstmate"]},
        }
        results = quota_share.compute_results(
            consumers, {"firstmate": 100.0}, account_percent_used=52
        )
        captain = next(r for r in results if r.name == "captain")
        self.assertTrue(captain.residual)
        self.assertIsNone(captain.measured_share_pct)
        self.assertIsNone(captain.imputed_used_pct)

    def test_measured_share_and_imputed_used_pct(self):
        consumers = {
            "firstmate": {"share": 30, "match": ["~/firstmate"]},
            "devclaw": {"share": 20, "match": ["lifekit:/app"]},
        }
        results = quota_share.compute_results(
            consumers, {"firstmate": 75.0, "devclaw": 25.0}, account_percent_used=52
        )
        firstmate = next(r for r in results if r.name == "firstmate")
        devclaw = next(r for r in results if r.name == "devclaw")
        self.assertAlmostEqual(firstmate.measured_share_pct, 75.0)
        self.assertAlmostEqual(firstmate.imputed_used_pct, 52 * 0.75)
        self.assertAlmostEqual(devclaw.measured_share_pct, 25.0)
        self.assertAlmostEqual(devclaw.imputed_used_pct, 52 * 0.25)

    def test_zero_total_weight_does_not_raise(self):
        consumers = {"firstmate": {"share": 30, "match": ["~/firstmate"]}}
        results = quota_share.compute_results(consumers, {}, account_percent_used=52)
        self.assertEqual(results[0].measured_share_pct, 0.0)
        self.assertEqual(results[0].imputed_used_pct, 0.0)


class AnyOverShareTests(unittest.TestCase):
    def test_flags_a_consumer_over_its_configured_share(self):
        over = quota_share.ConsumerResult(
            name="firstmate",
            configured_share=30,
            measured_share_pct=80.0,
            imputed_used_pct=45.0,
        )
        under = quota_share.ConsumerResult(
            name="devclaw",
            configured_share=20,
            measured_share_pct=20.0,
            imputed_used_pct=10.0,
        )
        self.assertTrue(quota_share.any_over_share([over, under]))
        self.assertFalse(quota_share.any_over_share([under]))

    def test_residual_consumer_never_trips_over_share(self):
        captain = quota_share.ConsumerResult(
            name="captain",
            configured_share=35,
            measured_share_pct=None,
            imputed_used_pct=None,
            residual=True,
        )
        self.assertFalse(quota_share.any_over_share([captain]))


class LoadConfigTests(unittest.TestCase):
    def test_rejects_config_with_no_consumers(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"consumers": {}}, f)
            path = f.name
        with self.assertRaises(ValueError):
            quota_share.load_config(quota_share.Path(path))

    def test_rejects_more_than_one_residual_consumer(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(
                {"consumers": {"a": {"match": "residual"}, "b": {"match": "residual"}}},
                f,
            )
            path = f.name
        with self.assertRaises(ValueError):
            quota_share.load_config(quota_share.Path(path))


if __name__ == "__main__":
    unittest.main()
