#!/usr/bin/env python3
"""Tests for scripts/ops-report/weekly.py.

Fixtures only: Prometheus, docker, df, du and crontab are faked, so nothing
here needs a live box.
"""

import importlib.util
import json
import os
import sys
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "weekly", os.path.join(os.path.dirname(HERE), "weekly.py")
)
weekly = importlib.util.module_from_spec(SPEC)
sys.modules["weekly"] = weekly
SPEC.loader.exec_module(weekly)

REPO = Path(HERE).parents[2]
GIB = 1024**3


def vec(*items):
    """Prometheus instant-vector result: (labels, value) pairs."""
    return [{"metric": m, "value": [0, str(v)]} for m, v in items]


class BudgetDocTests(unittest.TestCase):
    def test_real_doc_parses_every_group(self):
        budgets = weekly.parse_budgets(
            (REPO / "docs" / "resource-budget.md").read_text()
        )
        self.assertEqual(budgets["openclaw"], 3.5)
        self.assertEqual(budgets["burst pool"], 3.0)
        self.assertNotIn("sum", budgets)
        self.assertNotIn("total", budgets)

    def test_every_container_group_has_a_selector(self):
        budgets = weekly.parse_budgets(
            (REPO / "docs" / "resource-budget.md").read_text()
        )
        host = set(weekly.HOST_GROUPS) | {weekly.BURST_POOL_BUDGET_LABEL}
        self.assertEqual(set(budgets) - host, set(weekly.GROUP_SELECTORS))

    def test_only_the_budgets_table_is_read(self):
        text = "## Other\n| a | 9 |\n## The budgets\n| Group | Budget |\n| --- | --- |\n| x | 2 |\n"
        self.assertEqual(weekly.parse_budgets(text), {"x": 2.0})


class QueryTests(unittest.TestCase):
    def test_burst_excludes_every_named_group(self):
        q = weekly.burst_sum()
        for sel in weekly.GROUP_SELECTORS.values():
            self.assertIn(sel, q)
        self.assertIn("unless", q)

    def test_breach_query_uses_budget_in_bytes(self):
        q = weekly.breach_query("X", 1.5)
        self.assertIn(f"1.5 * {GIB}", q)
        self.assertIn("[7d:15m]", q)


class ParseTests(unittest.TestCase):
    def test_df(self):
        out = "1B-blocks Used Avail Use%\n100 60 40 60%\n"
        self.assertEqual(
            weekly.parse_df(out),
            {"size": 100.0, "used": 60.0, "avail": 40.0, "pct": 60.0},
        )
        self.assertIsNone(weekly.parse_df(""))

    def test_docker_size(self):
        self.assertEqual(weekly.parse_docker_size("1.5GB (12%)"), 1.5e9)
        self.assertEqual(weekly.parse_docker_size("0B"), 0)
        self.assertEqual(weekly.parse_docker_size("512kB"), 512e3)
        self.assertIsNone(weekly.parse_docker_size(""))

    def test_system_df_skips_junk(self):
        text = (
            json.dumps({"Type": "Images", "Size": "3GB", "Reclaimable": "1GB (33%)"})
            + "\nnot json\n"
        )
        self.assertEqual(weekly.parse_system_df(text), [("Images", "3GB", 1e9)])

    def test_crontab_drops_env_and_comments(self):
        text = "SHELL=/bin/sh\n# note\n\n0 3 * * * /srv/lifekit-stack/scripts/rotate/x.sh --token abc\n"
        self.assertEqual(len(weekly.parse_crontab(text)), 1)

    def test_cron_label_hides_arguments(self):
        label = weekly.cron_label("0 3 * * * /opt/tool/run.sh --token SECRET123")
        self.assertNotIn("SECRET123", label)
        self.assertIn("run.sh", label)

    def test_cron_repo_detection(self):
        files = {"docker-builder-gc.sh"}
        self.assertTrue(
            weekly.cron_repo_installed("0 3 * * * /x/docker-builder-gc.sh", files)
        )
        self.assertTrue(
            weekly.cron_repo_installed("0 3 * * * cd /srv/lifekit-stack && ./y", files)
        )
        self.assertTrue(
            weekly.cron_repo_installed(
                "@daily /usr/local/bin/docker-builder-gc.sh", files
            )
        )
        self.assertFalse(
            weekly.cron_repo_installed("0 3 * * * /opt/other/job.sh", files)
        )
        self.assertFalse(weekly.cron_repo_installed("@reboot /opt/other/job.sh", files))


class InspectTests(unittest.TestCase):
    DOC: ClassVar[dict] = {
        "Name": "/compose-x-1",
        "Config": {
            "Env": ["API_TOKEN=hunter2"],
            "Labels": {
                "com.docker.compose.project": "compose",
                "com.docker.compose.service": "x",
            },
        },
        "HostConfig": {"Memory": 0},
        "State": {"Running": True},
        "NetworkSettings": {"Networks": {"compose_default": {}, "lifekit-shared": {}}},
    }

    def test_reduce_never_carries_env(self):
        reduced = weekly.reduce_inspect(self.DOC)
        self.assertNotIn("hunter2", json.dumps(reduced))
        self.assertEqual(reduced["project"], "compose")
        self.assertEqual(reduced["mem_limit"], 0)


class ReportTests(unittest.TestCase):
    """Whole-report assembly against a faked Prometheus and host."""

    def setUp(self):
        self.orig = (weekly.prom_query, weekly.run_cmd)
        weekly.prom_query = self.fake_prom
        weekly.run_cmd = self.fake_cmd
        self.args = Namespace(
            prometheus="http://x",
            budget_doc=str(REPO / "docs" / "resource-budget.md"),
            rehearsal_dir="/nonexistent-rehearsal",
            backup_dir="/nonexistent-backups",
            repo_root=str(REPO),
        )
        self.prom_down = False
        self.missing_host_groups = set()

    def tearDown(self):
        weekly.prom_query, weekly.run_cmd = self.orig

    def fake_prom(self, base, query, timeout=60.0):
        if self.prom_down:
            raise weekly.Unavailable("prometheus: down")
        for group in ("operator", "runners", "os"):
            if f'{weekly.HOST_GAUGE}{{group="{group}"}}' in query:
                if group in self.missing_host_groups:
                    return []
                if query.startswith("max_over_time(min_over_time"):
                    return vec(({}, 0.4))
                return vec(
                    ({}, {"operator": 3 * GIB, "runners": GIB // 2, "os": GIB}[group])
                )
        if query.startswith("max_over_time(min_over_time"):
            # openclaw is the only group whose 15m-min ratio exceeded 1
            return vec(
                (
                    {},
                    1.2
                    if "openclaw-gateway|google-workspace-mcp" in query
                    and "unless" not in query
                    else 0.4,
                )
            )
        if query.startswith("max_over_time((sum"):
            return vec(({}, 2 * GIB))
        if query.startswith("avg_over_time"):
            return vec(({}, GIB))
        if "oom_killed" in query:
            return vec(({"name": "worker-1"}, 1))
        if "restart_count" in query:
            return vec(({"name": "grafana"}, 2))
        if "docker_container_healthy" in query:
            return []
        if "MemAvailable" in query:
            return vec(({}, 1 * GIB))
        if "MemTotal" in query:
            return vec(({}, 16 * GIB))
        if "SwapTotal_bytes - " in query:
            return vec(({}, 7 * GIB))
        if "SwapTotal" in query:
            return vec(({}, 8 * GIB))
        if "started_at" in query:
            return vec(({"project": "compose"}, 3), ({"project": ""}, 1))
        if "exit_code" in query:
            return []
        return vec(({}, 0))

    def fake_cmd(self, argv, timeout=60.0):
        if argv[0] == "df":
            return (
                "1B-blocks Used Avail Use%\n100000000000 90000000000 10000000000 90%\n"
            )
        if argv[:3] == ["docker", "system", "df"]:
            return json.dumps(
                {"Type": "Images", "Size": "3GB", "Reclaimable": "1GB (33%)"}
            )
        if argv[:3] == ["docker", "ps", "-aq"]:
            return "a\nb\n"
        if argv[:2] == ["docker", "inspect"]:

            def doc(name, project, mem, nets):
                return {
                    "Name": "/" + name,
                    "Config": {
                        "Env": ["SECRET=topsecret"],
                        "Labels": {"com.docker.compose.project": project}
                        if project
                        else {},
                    },
                    "HostConfig": {"Memory": mem},
                    "State": {"Running": True},
                    "NetworkSettings": {"Networks": {n: {} for n in nets}},
                }

            return json.dumps(
                [
                    doc("compose-a-1", "compose", 0, ["lifekit-shared"]),
                    doc("loose", "", 1000, ["lifekit-shared"]),
                ]
            )
        if argv[0] == "crontab":
            return "0 3 * * * /opt/foreign/job.sh --key abc\n"
        raise weekly.Unavailable("unexpected " + argv[0])

    def test_full_report(self):
        report = weekly.build_report(
            self.args, now=datetime(2026, 9, 29, tzinfo=timezone.utc)
        )
        for heading in (
            "# VPS ops report - week to 2026-09-29",
            "## Memory vs budget",
            "## OOM kills, restarts, unhealthy",
            "## Host memory",
            "## Disk",
            "## Deploys",
            "## Boundary drift",
            "## Needs a decision",
        ):
            self.assertIn(heading, report)
        self.assertIn("| openclaw | 3.5 GiB | 2.00 GiB | 1.00 GiB | yes", report)
        self.assertIn("OOM-killed: worker-1", report)
        self.assertIn("grafana x2", report)
        self.assertIn("Highest swap used: 7.00 GiB of 8.00 GiB", report)
        self.assertIn("3.00 GiB total".replace("3.00 GiB", "3GB"), report)
        self.assertIn("(no project): 1", report)
        self.assertIn("Cron jobs no repo script installs: `0 3 * * *` job.sh", report)
        self.assertIn("Cross-project networks: lifekit-shared (compose, loose)", report)
        decision = report.split("## Needs a decision")[1]
        self.assertIn("openclaw breached", decision)
        self.assertIn("OOM kill", decision)
        self.assertIn("root filesystem 90% used", decision)

    def test_host_groups_report_against_their_budgets(self):
        report = weekly.build_report(self.args)
        self.assertIn(
            "| operator sessions | 3.5 GiB | 3.00 GiB | 3.00 GiB | no |", report
        )
        self.assertIn("| runners | 0.75 GiB | 512 MiB | 512 MiB | no |", report)
        self.assertIn("| os | 1 GiB | 1.00 GiB | 1.00 GiB | no |", report)
        self.assertNotIn("no metric yet", report)
        self.assertNotIn("no rule yet", report)

    def test_host_group_without_series_says_so(self):
        self.missing_host_groups = {"runners"}
        report = weekly.build_report(self.args)
        self.assertIn("| runners | 0.75 GiB | no series | no series | - |", report)
        self.assertIn("| os | 1 GiB | 1.00 GiB", report)

    def test_no_crontab_is_none_not_unavailable(self):
        orig = weekly.run_cmd

        def no_crontab(argv, timeout=60.0):
            if argv[0] == "crontab":
                raise weekly.Unavailable("crontab: no crontab for lifekit")
            return orig(argv, timeout)

        weekly.run_cmd = no_crontab
        report = weekly.build_report(self.args)
        self.assertIn("Cron jobs no repo script installs: none", report)

    def test_oom_and_red_queries_are_bounded_to_the_week(self):
        seen = []
        inner = weekly.prom_query
        weekly.prom_query = lambda base, q, timeout=60.0: (
            seen.append(q) or inner(base, q, timeout)
        )
        weekly.build_report(self.args)
        oom = next(q for q in seen if "oom_killed" in q)
        red = next(q for q in seen if "exit_code" in q)
        self.assertIn("[7d]", oom)
        self.assertIn("changes(docker_container_running[7d])", red)

    def test_no_secret_leaks(self):
        report = weekly.build_report(self.args)
        for secret in ("topsecret", "--key abc"):
            self.assertNotIn(secret, report)

    def test_prometheus_down_still_renders(self):
        self.prom_down = True
        report = weekly.build_report(self.args)
        self.assertIn("unavailable (prometheus: down)", report)
        self.assertIn("## Disk", report)
        self.assertIn("## Needs a decision", report)

    def test_missing_budget_doc_still_renders(self):
        self.args.budget_doc = "/nonexistent/doc.md"
        report = weekly.build_report(self.args)
        self.assertIn("## Memory vs budget\n\nunavailable", report)


if __name__ == "__main__":
    unittest.main()
