#!/usr/bin/env python3
"""Tests for scripts/ops-report/consumers.py - fixture usage files only."""

import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "consumers", os.path.join(os.path.dirname(HERE), "consumers.py")
)
consumers = importlib.util.module_from_spec(SPEC)
sys.modules["consumers"] = consumers
SPEC.loader.exec_module(consumers)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
LINE_RE = re.compile(
    r"^7d to 2026-11-01 list-USD main \d+ \| vps \d+ \| workers \d+ \| clip \d+ "
    r"\| no-mistakes \d+ \| memory \d+ \| openclaw (\d+|n/a) \| total \d+$"
)


def entry(rid, model="claude-sonnet-x", out=1_000_000, ts="2026-10-31T00:00:00+00:00"):
    return json.dumps(
        {
            "type": "assistant",
            "requestId": rid,
            "timestamp": ts,
            "message": {"model": model, "usage": {"output_tokens": out}},
        }
    )


class ConsumersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proj = self.root / "projects"
        self.orig_oc = consumers.openclaw
        consumers.openclaw = lambda cut: None

    def tearDown(self):
        consumers.openclaw = self.orig_oc
        self.tmp.cleanup()

    def write(self, projdir, name, lines):
        d = self.proj / projdir
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("\n".join(lines) + "\n")

    def line(self, **kw):
        return consumers.cost_line(
            now=NOW,
            claude_projects=str(self.proj),
            clip_data=str(self.root / "clip"),
            nm_db=str(self.root / "nm.sqlite"),
            **kw,
        )

    def test_bucketing_and_dedupe(self):
        # sonnet output = $10/M, so 1M tokens = 10
        self.write("-home-denys-firstmate", "a.jsonl", [entry("r1"), entry("r1")])
        self.write("-home-denys-fm-vps", "a.jsonl", [entry("r2")])
        self.write("x-lifekit-stack-1", "a.jsonl", [entry("r3")])
        self.write("-home-denys-memory", "a.jsonl", [entry("r4")])
        self.write("-home-denys-no-mistakes-x", "a.jsonl", [entry("r5")])
        line = self.line()
        self.assertRegex(line, LINE_RE)
        self.assertIn("main 10 | vps 10 | workers 10 | clip 0", line)
        self.assertIn("no-mistakes 0 | memory 10 | openclaw n/a | total 40", line)

    def test_subagent_logs_and_window(self):
        sub = self.proj / "-home-denys-firstmate" / "s" / "subagents"
        sub.mkdir(parents=True)
        (sub / "b.jsonl").write_text(
            entry("r1") + "\n" + entry("old", ts="2026-01-01T00:00:00+00:00") + "\n"
        )
        self.assertIn("main 10 |", self.line())

    def test_clip_pipeline_skips_judge_duplicates(self):
        for rel, cost in (("a/run.json", 5), ("_judge/render/run.json", 99)):
            p = self.root / "clip" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"usage": {"cost_usd": cost}}))
        (self.root / "clip" / "bad").mkdir()
        (self.root / "clip" / "bad" / "run.json").write_text("{not json")
        self.assertIn("clip 5 |", self.line())

    def test_no_mistakes_db(self):
        db = self.root / "nm.sqlite"
        c = sqlite3.connect(db)
        c.execute("create table runs(id, created_at)")
        c.execute(
            "create table agent_invocations(run_id, model, cache_read_tokens,"
            " cache_creation_tokens, output_tokens)"
        )
        c.execute("insert into runs values(1, ?)", (int(NOW.timestamp()) - 3600,))
        c.execute("insert into runs values(2, 1)")
        c.execute("insert into agent_invocations values(1,'sonnet',0,0,1000000)")
        c.execute("insert into agent_invocations values(2,'sonnet',0,0,1000000)")
        c.commit()
        c.close()
        self.assertIn("no-mistakes 10 |", self.line())

    def test_all_sources_absent_still_prints(self):
        line = self.line()
        self.assertRegex(line, LINE_RE)
        self.assertIn("total 0", line)

    def test_openclaw_figure_counts_in_total(self):
        consumers.openclaw = lambda cut: 7.0
        line = self.line()
        self.assertIn("openclaw 7 | total 7", line)

    def test_safe_cost_line_never_raises(self):
        orig = consumers.scan
        consumers.scan = lambda *a, **k: 1 / 0
        try:
            self.assertEqual(
                consumers.safe_cost_line(), "unavailable (ZeroDivisionError)"
            )
        finally:
            consumers.scan = orig


if __name__ == "__main__":
    unittest.main()
