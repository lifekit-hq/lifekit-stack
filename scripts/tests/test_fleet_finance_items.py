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

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/fleet-publisher"
SCRIPT = DIR / "finance_items.py"

# Made-up finance-sentry UserIds: the owner, and another user of the same app.
OWNER = "0b8f6a52-3c1d-4e7a-9f20-5d6c7b8a9e01"
OTHER = "7e2d4c19-8a5b-4f36-b1c0-2a9d8e7f6c54"

# Every row finance-sentry holds, each with its "UserId" (`_user`), which the
# fake psql filters on the way a real one would and never prints.
ROWS = {
    "revisions": [
        {
            "_user": OWNER,
            "id": "rev-1",
            "kind": "Promote",
            "ticker": "ACME",
            "since": 1794756157,
            "expires": 1794929000,
        },
        {"_user": OTHER, "id": "rev-other", "kind": "Retire", "since": 1794756158},
    ],
    "recommendations": [
        {
            "_user": OWNER,
            "id": "rec-1",
            "action": "Exit",
            "ticker": "IREN",
            "since": 1795066224,
            "expires": None,
        },
        {"_user": OTHER, "id": "rec-other", "action": "Buy", "since": 1795066225},
    ],
    "events": [
        {
            "_user": OWNER,
            "id": "ev-1",
            "kind": "ThesisBreak",
            "subject": "ACME",
            "summary": "ACME  broke\nits thesis",
            "app_path": "/assets/ACME",
            "since": 1795000000,
        },
        {
            "_user": OTHER,
            "id": "ev-other",
            "kind": "SpendAlert",
            "summary": "Another user's spend notice",
            "since": 1795000000,
        },
        {
            "_user": OWNER,
            "id": "ev-2",
            "kind": "SyncFailure",
            "subject": None,
            "summary": None,
            "app_path": "//elsewhere.example/x",
            "since": 1795000001,
        },
    ],
}

# `docker exec` runs the command it is given locally, with the postgres
# container's environment; `docker ps` answers DOCKER_PS.
FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["DOCKER_LOG"], "a") as f:
    f.write(json.dumps({"prog": "docker", "argv": sys.argv[1:]}) + "\\n")
if sys.argv[1:2] == ["ps"]:
    print(os.environ.get("DOCKER_PS", "c0ffee"))
    sys.exit(0)
if os.environ.get("DOCKER_FAIL"):
    sys.exit(2)
args = sys.argv[2:]
env = dict(os.environ, POSTGRES_USER="fs", POSTGRES_DB="finance")
while args[0].startswith("-"):
    if args[0] == "-e":
        key, _, value = args[1].partition("=")
        env[key] = value
        args = args[2:]
    else:
        args = args[1:]
os.execvpe(args[1], args[1:], env)
"""

# psql as finance-sentry's database answers the finance query: `-v` variables
# interpolate `:'name'`, and a table's rows are filtered to the owner only when
# its subquery says `"UserId" = :'owner'`.
FAKE_PSQL = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
query = sys.stdin.read()
with open(os.environ["DOCKER_LOG"], "a") as f:
    f.write(json.dumps({"prog": "psql", "argv": argv, "stdin": query}) + "\\n")
variables = dict(
    argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-v"
)
if ":'owner'" in query and "owner" not in variables:
    print("syntax error at or near \\":\\"", file=sys.stderr)
    sys.exit(3)
try:
    rows = json.loads(os.environ["DOCKER_OUT"])
except ValueError:
    rows = None
if not isinstance(rows, dict):
    sys.stdout.write(os.environ["DOCKER_OUT"])
    sys.exit(0)
tables = {
    "revisions": "research.thesis_revisions",
    "recommendations": "research.recommendations",
    "events": "companion.companion_events",
}
doc = {}
for key, table in tables.items():
    sub = query.split("FROM " + table, 1)[1].split(") r)", 1)[0]
    scoped = "\\"UserId\\" = :'owner'" in sub
    doc[key] = [
        {k: v for k, v in r.items() if k != "_user"}
        for r in rows[key]
        if not scoped or r["_user"] == variables["owner"]
    ]
print(json.dumps(doc))
"""


def run(tmp_path, *args, out=None, owner=OWNER, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("docker", FAKE_DOCKER), ("psql", FAKE_PSQL)):
        fake = bin_dir / name
        fake.write_text(body)
        fake.chmod(0o755)
    log = tmp_path / "docker.log"
    log.unlink(missing_ok=True)
    base = {k: v for k, v in os.environ.items() if k != "FLEET_FINANCE_OWNER"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env={
            **base,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOCKER_LOG": str(log),
            "DOCKER_OUT": json.dumps(ROWS) + "\n" if out is None else out,
            **({} if owner is None else {"FLEET_FINANCE_OWNER": owner}),
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
    ps, exec_, psql = calls
    # The container is found by its compose labels, never by a guessed name.
    assert ps["argv"][:2] == ["ps", "-q"]
    assert "label=com.docker.compose.project=finance-sentry" in ps["argv"]
    assert "label=com.docker.compose.service=postgres" in ps["argv"]
    argv = exec_["argv"]
    assert argv[:3] == ["exec", "-i", "-e"]
    assert argv[3] == "PGOPTIONS=-c default_transaction_read_only=on"
    assert argv[4] == "c0ffee"
    # psql connects as the container's own user and database.
    assert psql["argv"][-4:] == ["-U", "fs", "-d", "finance"]
    query = psql["stdin"]
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


def test_only_the_owners_rows_are_listed(tmp_path):
    result, calls = run(tmp_path)
    assert result.returncode == 0, result.stderr
    # finance-sentry holds another user's revision, recommendation and event too.
    assert [i["id"] for i in json.loads(result.stdout)] == [
        "rev-1",
        "rec-1",
        "ev-1",
        "ev-2",
    ]
    psql = calls[-1]["argv"]
    assert psql[psql.index(f"owner={OWNER}") - 1] == "-v"


@pytest.mark.parametrize("owner", [None, ""])
def test_an_unconfigured_owner_reads_nothing(tmp_path, owner):
    result, calls = run(tmp_path, owner=owner)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "finance owner not configured\n"
    assert calls == []


@pytest.mark.parametrize(
    "owner", ["owner", "' OR 1=1 --", f"{OWNER}x", OWNER.replace("-", "")]
)
def test_an_owner_that_is_not_a_guid_reads_nothing(tmp_path, owner):
    result, calls = run(tmp_path, owner=owner)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "finance owner is not a GUID\n"
    assert calls == []


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
