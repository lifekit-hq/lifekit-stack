"""devclaw_items.py: the fleet publisher's FLEET_DEVCLAW_CMD. A fake `docker` on
PATH stands in for the devclaw container and its `devclaw tasks --json`."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/fleet-publisher"
SCRIPT = DIR / "devclaw_items.py"

EXPORT = [
    {
        "id": "devclaw:night-1",
        "kind": "goal",
        "store": "devclaw",
        "ref": "lifekit-hq/devclaw#1061",
        "refs": ["lifekit-hq/devclaw#1061", "lifekit-hq/devclaw#1062"],
        "stage": "build",
        "state": "new",
        "since": "2026-11-27T20:44:00Z",
        "owner": "devclaw",
        "class": "dev",
    }
]

# `docker ps` answers DOCKER_PS; `docker exec` prints DOCKER_OUT (or fails).
FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["DOCKER_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["ps"]:
    print(os.environ.get("DOCKER_PS", "c0ffee"))
    sys.exit(0)
if os.environ.get("DOCKER_FAIL"):
    sys.exit(2)
sys.stdout.write(os.environ["DOCKER_OUT"])
"""


def run(tmp_path, *args, out=None, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    log = tmp_path / "docker.log"
    log.unlink(missing_ok=True)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOCKER_LOG": str(log),
            "DOCKER_OUT": json.dumps(EXPORT) + "\n" if out is None else out,
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


def test_prints_devclaws_export_unchanged(tmp_path):
    result, _ = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == EXPORT


def test_the_read_is_devclaws_own_export_in_its_container(tmp_path):
    _, calls = run(tmp_path)
    ps, exec_ = calls
    assert ps[0] == "ps"
    assert "label=com.docker.compose.project=devclaw" in ps
    assert "label=com.docker.compose.service=devclaw-mcp" in ps
    assert exec_ == ["exec", "c0ffee", "devclaw", "tasks", "--json"]


def test_an_empty_export_is_an_empty_list(tmp_path):
    result, _ = run(tmp_path, out="[]\n")
    assert result.returncode == 0
    assert json.loads(result.stdout) == []


def test_an_unreadable_source_fails_and_prints_nothing(tmp_path):
    for env, out in (
        ({"DOCKER_FAIL": "1"}, None),
        ({}, "not json"),
        ({}, "{}"),
        ({"DOCKER_PS": ""}, None),
        ({"DOCKER_PS": "a\nb"}, None),
    ):
        result, _ = run(tmp_path, out=out, **env)
        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr.startswith("devclaw tasks unreadable")


def test_the_publisher_reads_its_output_as_devclaw_items(tmp_path, monkeypatch):
    result, _ = run(tmp_path)
    feed = tmp_path / "devclaw.json"
    feed.write_text(result.stdout)
    spec = importlib.util.spec_from_file_location(
        "fleet_status", DIR / "fleet_status.py"
    )
    fleet_status = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet_status)
    monkeypatch.setenv("FLEET_DEVCLAW_CMD", f"cat {feed}")
    errors: list[str] = []
    entries, source = fleet_status.devclaw_read(errors)
    assert source == {"read": True, "reason": None} and errors == []
    (item,) = fleet_status.devclaw_items(entries)
    assert item["id"] == "devclaw:night-1" and item["store"] == "devclaw"
    assert (item["owner"], item["class"], item["kind"]) == ("devclaw", "dev", "goal")
    assert item["refs"] == EXPORT[0]["refs"]
