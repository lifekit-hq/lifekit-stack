#!/usr/bin/env python3
"""Tests for compose/container-exporter/exporter.py.

The docker API is faked with recorded document shapes (docker API 1.54, the
box's daemon on 2026-09-13); nothing here touches a socket. The fixtures are
the two containers that motivated the exporter: finance-sentry-mcp mid crash
loop, and a healthy finance-sentry-api.
"""

import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "exporter", os.path.join(os.path.dirname(HERE), "exporter.py")
)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


def inspect_doc(
    name,
    status,
    *,
    restarting=False,
    exit_code=0,
    restart_count=0,
    health=None,
    oom=False,
    started="2026-09-13T09:23:03.800309061Z",
    project="docker",
    service=None,
):
    labels = {"com.docker.compose.project": project} if project else {}
    if service:
        labels["com.docker.compose.service"] = service
    state = {
        "Status": status,
        # The daemon reports Running=true while restarting - the trap the
        # exporter reads Status instead of.
        "Running": status in ("running", "restarting"),
        "Restarting": restarting,
        "OOMKilled": oom,
        "ExitCode": exit_code,
        "StartedAt": started,
        "FinishedAt": "2026-09-13T09:23:05.689913813Z",
    }
    if health:
        state["Health"] = {"Status": health}
    return {
        "Id": name + "-id",
        "Name": "/" + name,
        "RestartCount": restart_count,
        "State": state,
        "Config": {"Labels": labels},
    }


def stats_doc(usage, inactive_file, limit):
    return {
        "memory_stats": {
            "usage": usage,
            "limit": limit,
            "stats": {"inactive_file": inactive_file},
        }
    }


def sample(samples, metric, name):
    hits = [v for m, labels, v in samples if m == metric and labels.get("name") == name]
    assert len(hits) <= 1, (metric, name, hits)
    return hits[0] if hits else None


class ContainerSamplesTests(unittest.TestCase):
    def test_crash_loop_reads_as_not_running_and_restarting(self):
        doc = inspect_doc(
            "finance-sentry-mcp",
            "restarting",
            restarting=True,
            exit_code=139,
            restart_count=4235,
            service="mcp",
        )
        samples = exporter.container_samples(doc)
        self.assertEqual(
            sample(samples, "docker_container_running", "finance-sentry-mcp"), 0.0
        )
        self.assertEqual(
            sample(samples, "docker_container_restarting", "finance-sentry-mcp"), 1.0
        )
        self.assertEqual(
            sample(samples, "docker_container_restart_count", "finance-sentry-mcp"),
            4235.0,
        )
        self.assertEqual(
            sample(samples, "docker_container_exit_code", "finance-sentry-mcp"), 139.0
        )
        self.assertIsNone(
            sample(samples, "docker_container_healthy", "finance-sentry-mcp"),
            "no healthcheck -> no healthy series, so the unhealthy rule stays silent",
        )

    def test_labels_carry_compose_project_and_service(self):
        samples = exporter.container_samples(
            inspect_doc("finance-sentry-api", "running", service="api")
        )
        labels = next(
            labels for m, labels, _ in samples if m == "docker_container_running"
        )
        self.assertEqual(
            labels,
            {"name": "finance-sentry-api", "project": "docker", "service": "api"},
        )

    def test_container_outside_compose_has_empty_project_labels(self):
        samples = exporter.container_samples(
            inspect_doc("sweet_varahamihira", "running", project=None)
        )
        labels = next(
            labels for m, labels, _ in samples if m == "docker_container_running"
        )
        self.assertEqual(
            labels, {"name": "sweet_varahamihira", "project": "", "service": ""}
        )

    def test_health_maps_healthy_to_one_and_anything_else_to_zero(self):
        healthy = exporter.container_samples(
            inspect_doc("api", "running", health="healthy")
        )
        starting = exporter.container_samples(
            inspect_doc("api", "running", health="starting")
        )
        unhealthy = exporter.container_samples(
            inspect_doc("api", "running", health="unhealthy")
        )
        self.assertEqual(sample(healthy, "docker_container_healthy", "api"), 1.0)
        self.assertEqual(sample(starting, "docker_container_healthy", "api"), 0.0)
        self.assertEqual(sample(unhealthy, "docker_container_healthy", "api"), 0.0)

    def test_stopped_container_reports_no_health(self):
        samples = exporter.container_samples(
            inspect_doc("cli", "exited", exit_code=1, health="unhealthy")
        )
        self.assertIsNone(
            sample(samples, "docker_container_healthy", "cli"),
            "a stopped container's stale health verdict must not fire the unhealthy rule beside exited-abnormally",
        )

    def test_memory_matches_docker_stats_semantics(self):
        samples = exporter.container_samples(
            inspect_doc("gw", "running"),
            stats_doc(
                usage=1_523_294_208, inactive_file=23_294_208, limit=6_442_450_944
            ),
        )
        self.assertEqual(
            sample(samples, "docker_container_memory_usage_bytes", "gw"),
            1_500_000_000.0,
        )
        self.assertEqual(
            sample(samples, "docker_container_memory_limit_bytes", "gw"),
            6_442_450_944.0,
        )

    def test_no_stats_means_no_memory_series(self):
        samples = exporter.container_samples(
            inspect_doc("stopped", "exited", exit_code=1)
        )
        self.assertIsNone(
            sample(samples, "docker_container_memory_usage_bytes", "stopped")
        )

    def test_oom_and_start_time(self):
        samples = exporter.container_samples(
            inspect_doc(
                "x", "exited", exit_code=137, oom=True, started="2026-09-13T09:23:03.5Z"
            )
        )
        self.assertEqual(sample(samples, "docker_container_oom_killed", "x"), 1.0)
        self.assertAlmostEqual(
            sample(samples, "docker_container_started_at_seconds", "x"), 1789291383.5
        )


class ParseDockerTimeTests(unittest.TestCase):
    def test_nanoseconds_and_zero_time(self):
        self.assertAlmostEqual(
            exporter.parse_docker_time("2026-09-13T09:23:03.800309061Z"),
            1789291383.800309,
            places=5,
        )
        self.assertEqual(exporter.parse_docker_time("0001-01-01T00:00:00Z"), 0.0)
        self.assertEqual(exporter.parse_docker_time(None), 0.0)


class CollectTests(unittest.TestCase):
    def test_one_unreadable_container_is_counted_not_fatal(self):
        docs = {
            "/containers/json?all=1": [
                {"Id": "a-id", "Names": ["/a"]},
                {"Id": "gone-id", "Names": ["/gone"]},
                {"Id": "b-id", "Names": ["/b"]},
            ],
            "/containers/a-id/json": inspect_doc("a", "running"),
            "/containers/a-id/stats?stream=false&one-shot=true": stats_doc(
                100, 0, 1000
            ),
            "/containers/b-id/json": inspect_doc("b", "exited", exit_code=1),
        }
        calls = []

        def fake_get(path):
            calls.append(path)
            if path not in docs:
                raise RuntimeError(f"docker GET {path}: HTTP 404")
            return docs[path]

        samples = exporter.collect(get=fake_get)
        self.assertEqual(sample(samples, "docker_container_running", "a"), 1.0)
        self.assertEqual(sample(samples, "docker_container_running", "b"), 0.0)
        self.assertEqual(
            [v for m, _, v in samples if m == "docker_exporter_scrape_errors"], [1.0]
        )
        self.assertNotIn(
            "/containers/b-id/stats?stream=false&one-shot=true",
            calls,
            "stats are only fetched for running containers",
        )

    def test_failing_list_is_fatal(self):
        def fake_get(path):
            raise RuntimeError("docker GET /containers/json?all=1: HTTP 500")

        with self.assertRaises(RuntimeError):
            exporter.collect(get=fake_get)


class RenderTests(unittest.TestCase):
    def test_groups_samples_per_metric_with_one_help_and_type(self):
        text = exporter.render(
            [
                (
                    "docker_container_running",
                    {"name": "a", "project": "", "service": ""},
                    1.0,
                ),
                (
                    "docker_container_exit_code",
                    {"name": "a", "project": "", "service": ""},
                    0.0,
                ),
                (
                    "docker_container_running",
                    {"name": "b", "project": "", "service": ""},
                    0.0,
                ),
                ("docker_exporter_scrape_errors", {}, 0.0),
            ]
        )
        lines = text.splitlines()
        self.assertEqual(lines.count("# TYPE docker_container_running gauge"), 1)
        running = [
            i
            for i, line in enumerate(lines)
            if line.startswith("docker_container_running{")
        ]
        self.assertEqual(
            running, [running[0], running[0] + 1], "a metric's samples are contiguous"
        )
        self.assertIn("docker_exporter_scrape_errors 0.0", lines)
        self.assertTrue(text.endswith("\n"))

    def test_escapes_label_values(self):
        text = exporter.render(
            [
                (
                    "docker_container_running",
                    {"name": 'we"ird\\', "project": "", "service": ""},
                    1.0,
                )
            ]
        )
        self.assertIn('name="we\\"ird\\\\"', text)

    def test_refuses_unknown_metric(self):
        with self.assertRaises(ValueError):
            exporter.render([("docker_container_bogus", {}, 1.0)])


if __name__ == "__main__":
    unittest.main()
