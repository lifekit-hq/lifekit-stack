#!/usr/bin/env python3
"""Tests for scripts/ops-report/jobs.py - synthetic fixtures only."""

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
OPS = os.path.dirname(HERE)
sys.path.insert(0, OPS)
SPEC = importlib.util.spec_from_file_location("jobs", os.path.join(OPS, "jobs.py"))
jobs = importlib.util.module_from_spec(SPEC)
sys.modules["jobs"] = jobs
SPEC.loader.exec_module(jobs)

NOW = datetime.now(timezone.utc)


def iso(delta_hours):
    return (NOW - timedelta(hours=delta_hours)).isoformat()


def user(text, ts):
    return json.dumps({"type": "user", "timestamp": ts, "message": {"content": text}})


def asst(rid, ts, out=1_000_000, cw=0, model="claude-sonnet-x"):
    usage = {"output_tokens": out, "cache_creation_input_tokens": cw}
    return json.dumps(
        {
            "type": "assistant",
            "requestId": rid,
            "timestamp": ts,
            "message": {"model": model, "usage": usage},
        }
    )


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proj = self.root / "projects"
        self.cut = NOW - timedelta(days=7)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, projdir, name, lines):
        d = self.proj / projdir
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("\n".join(lines) + "\n")

    def test_trigger_classes(self):
        t = jobs.trigger
        self.assertEqual(
            t("x needs a handling turn now. check: process-event y"),
            "wake:check:process-event",
        )
        self.assertEqual(
            t("needs a handling turn now. supervision-host: cycle"),
            "wake:supervision-host:cycle-boundary",
        )
        self.assertEqual(
            t("This session is being continued from a previous"), "compaction-resume"
        )
        self.assertEqual(t("MAIN DIALOG MIRROR ..."), "supervision-host-engine")
        self.assertEqual(t("<task-notification>done"), "bg-task-notice")
        self.assertEqual(
            t("<system-reminder>only</system-reminder>"), "system-reminder"
        )
        self.assertEqual(t("hello there"), "captain/other")

    def test_text_of_skips_tool_results(self):
        msg = {"content": [{"type": "tool_result", "content": "x"}]}
        self.assertIsNone(jobs.text_of(msg))
        self.assertEqual(
            jobs.text_of({"content": [{"type": "text", "text": "hi"}]}), "hi"
        )

    def test_supervisor_calls_charged_to_trigger_and_cold_cache(self):
        self.write(
            "-home-denys-firstmate",
            "s.jsonl",
            [
                user("needs a handling turn now. check: process-event", iso(30)),
                asst("r1", iso(29.9)),
                asst("r2", iso(2), out=0, cw=1_000_000),  # >1h idle: cold cache
                user("hello", iso(1.6)),
                asst("r3", iso(1.5)),
                asst("r3", iso(1.5)),  # duplicate requestId counts once
            ],
        )
        rows, cold = jobs.scan_host(str(self.proj), self.cut)
        wake = rows[("main", "wake:check:process-event")]
        self.assertAlmostEqual(wake[1], 10.0 + 2 * 2.0)
        self.assertAlmostEqual(rows[("main", "captain/other")][1], 10.0)
        self.assertEqual(cold["main"][0], 1)
        self.assertAlmostEqual(cold["main"][1], 4.0)

    def test_count_triggers_counts_turns(self):
        self.write(
            "-home-denys-fm-vps",
            "s.jsonl",
            [
                user("a", iso(5)),
                asst("r1", iso(4.9)),
                user("b", iso(3)),
                user("old", iso(500)),
            ],
        )
        rows, _ = jobs.scan_host(str(self.proj), self.cut)
        jobs.count_triggers(str(self.proj), self.cut, rows)
        self.assertEqual(rows[("vps", "captain/other")][0], 2)

    def test_task_sessions_charged_to_task_id(self):
        self.write(
            "-home-denys-treehouse-demo",
            "w.jsonl",
            [
                user("see firstmate/state/demo-job.inbox now", iso(3)),
                asst("w1", iso(2)),
            ],
        )
        self.write(
            "-home-denys-treehouse-demo",
            "x.jsonl",
            [user("fm-vps/state/nightly-weekly.inbox", iso(3)), asst("x1", iso(2))],
        )
        rows, _ = jobs.scan_host(str(self.proj), self.cut)
        self.assertEqual(rows[("workers", "task:demo-job")], [1, 10.0])
        self.assertEqual(rows[("workers", "recurring:nightly-weekly")], [1, 10.0])

    def test_old_files_and_nomistakes_dirs_skipped(self):
        self.write("-home-denys-no-mistakes-x", "n.jsonl", [asst("n1", iso(1))])
        self.write("-home-denys-firstmate", "o.jsonl", [asst("o1", iso(24 * 20))])
        rows, _ = jobs.scan_host(str(self.proj), self.cut)
        self.assertEqual(len(rows), 0)

    def test_supervisor_subagent_spend_follows_parent_trigger(self):
        self.write(
            "-home-denys-firstmate",
            "s.jsonl",
            [
                user("needs a handling turn now. check: process-event", iso(10)),
                asst("p1", iso(9.9)),
                user("hello", iso(5)),
                asst("p2", iso(4.9)),
            ],
        )
        sub = self.proj / "-home-denys-firstmate" / "s" / "subagents"
        sub.mkdir(parents=True)
        (sub / "a.jsonl").write_text(
            "\n".join([asst("a1", iso(9.5)), asst("a2", iso(4.5))]) + "\n"
        )
        rows, _ = jobs.scan_host(str(self.proj), self.cut)
        self.assertNotIn(("main", "start"), rows)
        self.assertAlmostEqual(rows[("main", "wake:check:process-event")][1], 20.0)
        self.assertAlmostEqual(rows[("main", "captain/other")][1], 20.0)

    def run_oc_script(self, root):
        r = subprocess.run(
            [sys.executable, "-I", "-c", jobs.OC_SCRIPT, self.cut.isoformat(), str(root)],
            capture_output=True,
            text=True,
            check=True,
        )  # fmt: skip
        return json.loads(r.stdout)

    def test_oc_script_classifies_jobs_and_charges_subagents_to_parent(self):
        def write(name, first, calls, subs=()):
            d = self.proj / "-workspace"
            d.mkdir(parents=True, exist_ok=True)
            lines = [user(first, iso(5))] + [asst(*c) for c in calls]
            (d / f"{name}.jsonl").write_text("\n".join(lines) + "\n")
            if subs:
                sd = d / name / "subagents"
                sd.mkdir(parents=True)
                (sd / "x.jsonl").write_text("\n".join(asst(*c) for c in subs) + "\n")

        write("c1", "[cron:abc ledger-pulse] go", [("c1a", iso(4))], [("c1s", iso(3))])
        write("c2", "[cron:def ledger-pulse] go", [("c2a", iso(4))])
        write("k1", "x companion signal: kind digest y", [("k1a", iso(4))])
        write("h1", "Job ID: 77", [("h1a", iso(4))])
        write("d1", "Run the post-deploy SMOKE check", [("d1a", iso(4))])
        write("i1", "hello", [("i1a", iso(4)), ("i1a", iso(4)), ("i1old", iso(24 * 20))])
        got = self.run_oc_script(self.proj)
        out = 15.0  # 1M sonnet output tokens at the 15/Mtok list price
        self.assertEqual(got["cron:ledger-pulse"][0], 2)
        self.assertAlmostEqual(got["cron:ledger-pulse"][1], out * 3)
        for job in (
            "hook:companion-digest",
            "hook:other",
            "deploy-smoke",
            "interactive/other",
        ):
            self.assertEqual(got[job][0], 1)
            self.assertAlmostEqual(got[job][1], out)

    def test_oc_rows_parses_gateway_output_and_degrades(self):
        def run(*results):
            return mock.patch.object(jobs.subprocess, "run", side_effect=list(results))

        def done(stdout):
            return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

        rows = {}
        with run(done("abc\n"), done(json.dumps({"cron:x": [2, 3.5]}))):
            self.assertTrue(jobs.oc_rows(self.cut, rows))
        self.assertEqual(rows[("openclaw", "cron:x")], [2, 3.5])
        with run(done("")):
            self.assertFalse(jobs.oc_rows(self.cut, {}))  # no gateway container
        with run(done("abc\n"), done("Traceback")):
            self.assertFalse(jobs.oc_rows(self.cut, {}))  # script failed
        with run(OSError("no docker")):
            self.assertFalse(jobs.oc_rows(self.cut, {}))

    def test_nm_rows(self):
        db = self.root / "state.sqlite"
        c = sqlite3.connect(db)
        c.execute("create table runs (id text, created_at integer)")
        c.execute(
            "create table agent_invocations (run_id text, step_name text, purpose text,"
            " model text, cache_read_tokens int, cache_creation_tokens int, output_tokens int)"
        )
        c.execute("insert into runs values ('r1', ?)", (int(NOW.timestamp()),))
        c.execute("insert into runs values ('r0', 1)")
        for run in ("r1", "r1", "r0"):
            c.execute(
                "insert into agent_invocations values (?, 'test', 'test', 'claude-sonnet-x',"
                " 0, 0, 1000000)",
                (run,),
            )
        c.commit()
        c.close()
        from collections import defaultdict

        rows = defaultdict(lambda: [0, 0.0])
        jobs.nm_rows(str(db), self.cut, rows)
        self.assertEqual(rows[("no-mistakes", "test/test")], [2, 20.0])
        jobs.nm_rows(str(self.root / "missing.sqlite"), self.cut, rows)  # no raise

    def test_render_folds_small_rows_and_hides_tokens(self):
        rows = {
            ("main", "big"): [2, 10.0],
            ("main", "tiny"): [1, 0.2],
            ("main", "tiny2"): [1, 0.3],
        }
        out = jobs.render(rows, {"main": [1, 1.0]}, 7, NOW, 1.0, False)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("jobs 7d to "))
        self.assertIn("main,big,2,10.0,5.00", out)
        self.assertIn("main,other(<min),2,0.5,0.25", out)
        self.assertIn("cold_cache[1]{consumer,calls,usd,share}:", out)
        self.assertIn("  main,1,1.0,10%", out)
        self.assertTrue(lines[-1].endswith("(openclaw n/a)"))

    def test_main_end_to_end(self):
        self.write(
            "-home-denys-firstmate",
            "s.jsonl",
            [user("hello", iso(2)), asst("r1", iso(1.9))],
        )
        clip = self.root / "clip" / "run1"
        clip.mkdir(parents=True)
        (clip / "run.json").write_text(json.dumps({"usage": {"cost_usd": 3.5}}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = jobs.main(
                [
                    "--days", "7",
                    "--no-openclaw",
                    "--claude-projects", str(self.proj),
                    "--clip-data", str(self.root / "clip"),
                    "--nm-db", str(self.root / "none.sqlite"),
                ]
            )  # fmt: skip
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("main,captain/other,1,10.0,10.00", out)
        self.assertIn("clip,pipeline-runs,0,3.5,0.00", out)
        self.assertIn("total: 14 (openclaw n/a)", out)


if __name__ == "__main__":
    unittest.main()
