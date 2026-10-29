"""The reminder bot's operator-built image (scripts/reminder-bot/build-image.sh)
against stubbed `docker` and `gh`, and its compose file against the platform
contract's static gate."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/reminder-bot/build-image.sh"
COMPOSE = REPO / "compose/reminder-bot/docker-compose.yml"
PIN = (REPO / "scripts/reminder-bot/PIN").read_text().strip()

DOCKER_STUB = """#!/bin/sh
echo "docker $*" >> "$CALL_LOG"
if [ "$1 $2" = "image inspect" ]; then
  [ -n "$IMAGE_EXISTS" ]
  exit
fi
exit 0
"""

# A tarball the way `gh api repos/<r>/tarball/<sha>` returns one: one top
# directory holding the repo.
GH_STUB = """#!/bin/sh
echo "gh $*" >> "$CALL_LOG"
[ -z "$GH_FAIL" ] || exit 1
cat "$TARBALL"
"""


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", DOCKER_STUB), ("gh", GH_STUB)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    src = tmp_path / "tarsrc/dsdevq-reminder-bot-abc"
    src.mkdir(parents=True)
    (src / "Dockerfile").write_text("FROM scratch\n")
    tarball = tmp_path / "src.tgz"
    subprocess.run(
        ["tar", "-czf", str(tarball), "-C", str(tmp_path / "tarsrc"), src.name],
        check=True,
    )
    log = tmp_path / "calls.log"
    log.write_text("")
    scratch = tmp_path / "scratch"

    def go(*args, **extra):
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "TARBALL": str(tarball),
            "REMINDER_BOT_SCRATCH": str(scratch),
            **extra,
        }
        r = subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True
        )
        return r, log.read_text().splitlines(), scratch

    return go


def test_pin_is_one_full_commit_sha():
    assert len(PIN) == 40 and set(PIN) <= set("0123456789abcdef")


def test_tag_is_the_short_pin(run):
    r, calls, _ = run("--tag")
    assert (r.returncode, r.stdout.strip()) == (0, PIN[:7])
    assert calls == []


def test_check_reports_an_absent_tag_and_names_the_build_command(run):
    r, calls, _ = run("--check")
    assert r.returncode == 1
    assert "bash scripts/reminder-bot/build-image.sh" in r.stderr
    assert calls == [f"docker image inspect reminder-bot:{PIN[:7]}"]
    assert run("--check", IMAGE_EXISTS="1")[0].returncode == 0


def test_build_fetches_the_pinned_commit_builds_and_cleans_up(run):
    r, calls, scratch = run()
    assert r.returncode == 0, r.stderr
    assert calls[0] == f"docker image inspect reminder-bot:{PIN[:7]}"
    assert calls[1] == f"gh api repos/dsdevq/reminder-bot/tarball/{PIN}"
    build = calls[2].split()
    assert build[:5] == ["docker", "build", "--memory", "1g", "-t"]
    assert build[5] == f"reminder-bot:{PIN[:7]}"
    assert build[6].startswith(str(scratch))
    assert list(scratch.iterdir()) == []


def test_build_is_idempotent(run):
    r, calls, _ = run(IMAGE_EXISTS="1")
    assert r.returncode == 0
    assert "already exists" in r.stdout
    assert calls == [f"docker image inspect reminder-bot:{PIN[:7]}"]


def test_failed_fetch_builds_nothing_and_leaves_no_scratch(run):
    r, calls, scratch = run(GH_FAIL="1")
    assert r.returncode != 0
    assert not [c for c in calls if c.startswith("docker build")]
    assert list(scratch.iterdir()) == []


def test_compose_service_meets_the_stack_policy(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("docker compose not installed")
    r = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "reminder-bot",
            "-f",
            str(COMPOSE),
            "config",
            "--format",
            "json",
        ],
        env={**os.environ, "REMINDER_BOT_TAG": "abc1234", "REMINDER_BOT_TOKEN": "x"},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["name"] == "reminder-bot"
    assert list(doc["services"]) == ["reminder-bot"]
    svc = doc["services"]["reminder-bot"]
    assert svc["image"] == "reminder-bot:abc1234"
    assert "build" not in svc and "ports" not in svc
    assert svc["restart"] == "unless-stopped"
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
    assert svc["environment"]["TZ"] == "Europe/Dublin"
    assert svc["environment"]["DB_PATH"].startswith("/data/")
    assert [v["target"] for v in svc["volumes"]] == ["/data"]
    assert int(svc["deploy"]["resources"]["limits"]["pids"]) > 0
    assert svc["deploy"]["resources"]["limits"]["memory"] in (
        268435456,
        "268435456",
        "256m",
    )
    assert svc["logging"]["driver"] == "json-file"
    gate = subprocess.run(
        ["python3", str(REPO / "scripts/platform-contract.py"), "--static", "-"],
        input=r.stdout,
        capture_output=True,
        text=True,
    )
    assert gate.returncode == 0, gate.stdout + gate.stderr
