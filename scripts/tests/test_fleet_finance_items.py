"""finance_items.py: the fleet publisher's FLEET_FINANCE_CMD. A fake `docker` on
PATH stands in for finance-sentry's postgres container."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/fleet-publisher"
SCRIPT = DIR / "finance_items.py"

ROWS = {
    "revisions": [
        {
            "id": "rev-1",
            "kind": "Promote",
            "ticker": "ACME",
            "since": 1794756157,
            "expires": 1794929000,
        }
    ],
    "recommendations": [
        {
            "id": "rec-1",
            "action": "Exit",
            "ticker": "IREN",
            "since": 1795066224,
            "expires": None,
        }
    ],
    "events": [
        {
            "id": "ev-1",
            "kind": "ThesisBreak",
            "subject": "ACME",
            "summary": "ACME  broke\nits thesis",
            "app_path": "/assets/ACME",
            "since": 1795000000,
        },
        {
            "id": "ev-2",
            "kind": "SyncFailure",
            "subject": None,
            "summary": None,
            "app_path": "//elsewhere.example/x",
            "since": 1795000001,
        },
    ],
}

FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
log = os.environ["DOCKER_LOG"]
stdin = "" if sys.argv[1:2] == ["ps"] else sys.stdin.read()
with open(log, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\\n")
if sys.argv[1:2] == ["ps"]:
    print(os.environ.get("DOCKER_PS", "c0ffee"))
elif os.environ.get("DOCKER_FAIL"):
    sys.exit(2)
else:
    sys.stdout.write(os.environ["DOCKER_OUT"])
"""


def run(tmp_path, *args, out=None, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOCKER_LOG": str(log),
            "DOCKER_OUT": json.dumps(ROWS) + "\n" if out is None else out,
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    calls = (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    return result, calls


def test_prints_the_pending_items_in_the_publishers_list_shape(tmp_path):
    result, _ = run(tmp_path, "--base-url", "https://finance.example/")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {
            "kind": "thesis-revision",
            "id": "rev-1",
            "title": "ACME: Promote thesis revision to decide",
            "acceptance": "The revision is confirmed or rejected by 2026-11-17",
            "since": 1794756157,
        },
        {
            "kind": "recommendation",
            "id": "rec-1",
            "title": "Exit IREN: recommendation to decide",
            "acceptance": "The recommendation is accepted or declined",
            "since": 1795066224,
        },
        {
            "kind": "companion-event",
            "id": "ev-1",
            "title": "ACME  broke\nits thesis",
            "acceptance": "Ledger delivers the event and acknowledges it",
            "since": 1795000000,
            "url": "https://finance.example/assets/ACME",
        },
        # No summary or subject: titled by its kind. An off-site path gets no link.
        {
            "kind": "companion-event",
            "id": "ev-2",
            "title": "SyncFailure",
            "acceptance": "Ledger delivers the event and acknowledges it",
            "since": 1795000001,
        },
    ]


def test_without_a_base_url_no_item_carries_a_link(tmp_path):
    result, _ = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert all("url" not in item for item in json.loads(result.stdout))


def test_the_read_is_one_select_in_a_read_only_session(tmp_path):
    result, calls = run(tmp_path)
    assert result.returncode == 0, result.stderr
    ps, exec_ = calls
    # The container is found by its compose labels, never by a guessed name.
    assert ps["argv"][:2] == ["ps", "-q"]
    assert "label=com.docker.compose.project=finance-sentry" in ps["argv"]
    assert "label=com.docker.compose.service=postgres" in ps["argv"]
    argv = exec_["argv"]
    assert argv[:3] == ["exec", "-i", "-e"]
    assert argv[3] == "PGOPTIONS=-c default_transaction_read_only=on"
    assert argv[4] == "c0ffee"
    query = exec_["stdin"]
    assert query.lstrip().upper().startswith("SELECT")
    assert not re.search(
        r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE|CREATE|GRANT)\b", query, re.I
    )
    # The undelivered dispositions of finance-sentry's pending-events query;
    # held-for-digest belongs to the digest.
    assert (
        "'Pending', 'Dispatched', 'DeferredQuietHours', 'SuppressedByRateLimit'"
        in query
    )
    assert "HeldForDigest" not in query


def test_a_named_container_skips_the_label_lookup(tmp_path):
    result, calls = run(tmp_path, "--container", "pg-1")
    assert result.returncode == 0, result.stderr
    assert [c["argv"][0] for c in calls] == ["exec"]
    assert calls[0]["argv"][4] == "pg-1"


def test_an_unreadable_source_fails_and_prints_nothing(tmp_path):
    for env, out in (
        ({"DOCKER_FAIL": "1"}, None),
        ({}, "not json"),
        ({}, "[]"),
        ({"DOCKER_PS": ""}, None),
        ({"DOCKER_PS": "a\nb"}, None),
    ):
        result, _ = run(tmp_path, out=out, **env)
        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr.startswith("finance items unreadable")


def test_the_publisher_stamps_the_finance_envelope_on_its_output(tmp_path, monkeypatch):
    result, _ = run(tmp_path)
    feed = tmp_path / "finance.json"
    feed.write_text(result.stdout)
    spec = importlib.util.spec_from_file_location(
        "fleet_status", DIR / "fleet_status.py"
    )
    fleet_status = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet_status)
    monkeypatch.setenv("FLEET_FINANCE_CMD", f"cat {feed}")
    errors: list[str] = []
    items, source = fleet_status.finance_items(errors)
    assert source == {"read": True, "reason": None} and errors == []
    assert [i["id"] for i in items] == [
        "finance-db:thesis-revision/rev-1",
        "finance-db:recommendation/rec-1",
        "finance-db:companion-event/ev-1",
        "finance-db:companion-event/ev-2",
    ]
    assert {(i["owner"], i["class"], i["store"]) for i in items} == {
        ("finance", "finance", "finance-db")
    }
    assert (
        items[0]["acceptance"] == "The revision is confirmed or rejected by 2026-11-17"
    )
    assert items[2]["title"] == "ACME broke its thesis"
