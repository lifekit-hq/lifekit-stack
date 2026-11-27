"""scripts/lib/openclaw-gateway.sh: the one production gateway, or a refusal (issue 716).

A fake docker holds a list of containers with their labels and applies `ps`
label filters the way docker does (every filter must match), so a second
container carrying the gateway's compose labels is a real second match.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts/lib/openclaw-gateway.sh"
PROD_DIR = "/srv/lifekit-stack/compose/openclaw"

# `ps [-q] [--filter label=k=v]... [--format '{{.Names}}']` over $STATE_DIR/containers.json;
# `ps` fails when $STATE_DIR/_broken exists; every other call is logged and fails.
FAKE = r"""#!/usr/bin/env python3
import json, os, sys
state = os.environ["STATE_DIR"]
args = sys.argv[1:]
with open(os.path.join(state, "_calls"), "a") as f:
    f.write(" ".join(args) + "\n")
if not args or args[0] != "ps" or os.path.exists(os.path.join(state, "_broken")):
    sys.exit(1)
want, names, i = [], False, 1
while i < len(args):
    if args[i] == "--filter":
        k, _, v = args[i + 1].removeprefix("label=").partition("=")
        want.append((k, v))
        i += 2
    elif args[i] == "--format":
        names = args[i + 1] == "{{.Names}}"
        i += 2
    else:
        i += 1
for c in json.load(open(os.path.join(state, "containers.json"))):
    if all(c["labels"].get(k) == v for k, v in want):
        print(c["name"] if names else c["id"])
"""


def labels(working_dir=PROD_DIR, oneoff="False"):
    return {
        "com.docker.compose.project": "openclaw",
        "com.docker.compose.service": "openclaw-gateway",
        "com.docker.compose.oneoff": oneoff,
        "com.docker.compose.project.working_dir": working_dir,
    }


PROD = {"id": "0b94095bce4c", "name": "openclaw-openclaw-gateway-1", "labels": labels()}
# A throwaway gateway cloned with every label of the production one.
TWIN = {"id": "7f1e2d3c4b5a", "name": "jq711-gw", "labels": labels()}


@pytest.fixture
def docker(tmp_path):
    state, bin_dir = tmp_path / "state", tmp_path / "bin"
    state.mkdir()
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE)
    fake.chmod(0o755)

    class Docker:
        def __init__(self):
            self.state = state
            self.env = dict(os.environ)
            self.env |= {
                "PATH": f"{bin_dir}:{self.env['PATH']}",
                "STATE_DIR": str(state),
            }
            self.env.pop("DOCKER", None)
            self.env.pop("OPENCLAW_GATEWAY_WORKDIR", None)
            self.containers([])

        def containers(self, items):
            (state / "containers.json").write_text(json.dumps(items))

        def calls(self):
            p = state / "_calls"
            return p.read_text().splitlines() if p.exists() else []

        def run(self, argv):
            return subprocess.run(
                argv, env=self.env, capture_output=True, text=True, check=False
            )

        def resolve(self):
            return self.run(
                [
                    "bash",
                    "-c",
                    f'set -euo pipefail; source "{LIB}"; openclaw_gateway_id',
                ]
            )

    return Docker()


def test_the_one_production_gateway_is_printed(docker):
    docker.containers([PROD])
    p = docker.resolve()
    assert (p.returncode, p.stdout) == (0, "0b94095bce4c\n"), p.stderr


def test_no_gateway_prints_nothing_and_succeeds(docker):
    p = docker.resolve()
    assert (p.returncode, p.stdout) == (0, ""), p.stderr


@pytest.mark.parametrize(
    "other",
    [
        {
            "id": "aaa111",
            "name": "rehearsal-gw",
            "labels": labels("/tmp/rehearsal/compose/openclaw"),
        },
        {
            "id": "bbb222",
            "name": "openclaw-openclaw-gateway-run-1",
            "labels": labels(oneoff="True"),
        },
    ],
    ids=["other-working-dir", "compose-run-oneoff"],
)
def test_a_labelled_container_that_is_not_production_is_ignored(docker, other):
    docker.containers([other, PROD])
    p = docker.resolve()
    assert (p.returncode, p.stdout) == (0, "0b94095bce4c\n"), p.stderr


def test_two_matching_containers_are_refused_naming_both(docker):
    docker.containers([TWIN, PROD])
    p = docker.resolve()
    assert p.returncode == 1
    assert p.stdout == ""
    assert "refusing to act: 2 running containers match" in p.stderr
    assert "jq711-gw" in p.stderr and "openclaw-openclaw-gateway-1" in p.stderr


def test_docker_ps_failure_is_an_error_not_no_gateway(docker):
    (docker.state / "_broken").write_text("")
    p = docker.resolve()
    assert p.returncode == 1
    assert "docker ps failed" in p.stderr


def test_lenient_mode_reads_docker_down_as_no_gateway_but_still_refuses_two(docker):
    lenient = [
        "bash",
        "-c",
        f'set -euo pipefail; source "{LIB}"; openclaw_gateway_id_lenient',
    ]
    (docker.state / "_broken").write_text("")
    p = docker.run(lenient)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    (docker.state / "_broken").unlink()
    docker.containers([TWIN, PROD])
    p = docker.run(lenient)
    assert p.returncode == 1
    assert "refusing to act" in p.stderr


# Every host script that acts on the gateway, with the argv that reaches its
# gateway lookup. Each must stop on two matches before touching either container.
GATEWAY_SCRIPTS = [
    ["scripts/ledger-drift/lifekit-ledger-drift.sh"],
    ["scripts/video-eval.sh", "status", "video-x"],
    ["scripts/openclaw-auth/auth-mode.sh", "status"],
    ["scripts/ensure-doc-door.sh"],
    ["scripts/ensure-finance-pulse.sh"],
    ["scripts/ensure-fleet-gate.sh"],
    ["scripts/ensure-goal-checkin-solved.sh"],
    ["scripts/ensure-intake-door.sh"],
    ["scripts/ensure-job-queue.sh"],
    ["scripts/ensure-kit-cron-models.sh"],
    ["scripts/ensure-learnclaw-morning.sh"],
    ["scripts/ensure-learnclaw-weekcount.sh"],
    ["scripts/ensure-learnclaw.sh"],
    ["scripts/ensure-leetcode-daily.sh"],
    ["scripts/ensure-memory-vault-cleanup.sh"],
    ["scripts/ensure-needs-you-digest.sh"],
    ["scripts/ensure-planning-reminder.sh"],
    ["scripts/ensure-reader-agent.sh"],
    ["scripts/ensure-secondmate-relay.sh"],
    ["scripts/ensure-study-panel.sh"],
    ["scripts/ensure-today-brief-fleet.sh"],
    ["scripts/ensure-video-drop-door.sh"],
    ["scripts/ensure-video-eval.sh"],
    ["scripts/ensure-workout-log.sh"],
    ["scripts/ensure-youtube-transcript.sh"],
]


@pytest.fixture
def host(docker, tmp_path):
    """A throwaway host for the scripts' pre-gateway steps: agent workspaces, an
    owner chat id, a fleet-gate table, the pinned learnharness skill and a
    yt-dlp that passes its checksum, so each one reaches its gateway lookup
    without the network. Nothing here touches /srv or a real container."""
    config = tmp_path / "config"
    for agent in ("kit", "learnclaw", "reader", "study-panel"):
        (config / "agents" / agent / "workspace").mkdir(parents=True)
    lh = config / "agents/learnclaw/workspace/skills/learnharness"
    lh.mkdir(parents=True)
    commit = re.search(
        r"^LH_SKILL_COMMIT=(\w+)",
        (REPO / "scripts/ensure-learnclaw.sh").read_text(),
        re.MULTILINE,
    )
    (lh / ".commit").write_text(commit.group(1) + "\n")
    ytdlp = config / "skills/youtube-transcript/yt-dlp"
    ytdlp.parent.mkdir(parents=True)
    ytdlp.write_text("#!/bin/sh\n")
    ytdlp.chmod(0o755)
    env_file = tmp_path / "stack.env"
    env_file.write_text("LIFEKIT_TELEGRAM_CHAT=12345\n")
    table = tmp_path / "p16-openclaw.json"
    table.write_text(
        json.dumps({"schema": "p16-openclaw.v1", "tools": {"read": {"class": "A"}}})
    )
    bin_dir = Path(docker.env["PATH"].split(":", 1)[0])
    for name, body in (("curl", "exit 22"), ("sha256sum", "exit 0")):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    docker.env |= {
        "HOME": str(home),
        "OPENCLAW_CONFIG_DIR": str(config),
        "ENV_FILE": str(env_file),
        "P16_OPENCLAW_TABLE": str(table),
        "DOC_DOOR_AGENT": "kit",
        "LIFEKIT_INSTALLED_DIR": str(tmp_path / "installed"),
    }
    return docker


@pytest.mark.parametrize("argv", GATEWAY_SCRIPTS, ids=lambda a: Path(a[0]).name)
def test_every_gateway_script_stops_before_any_exec_when_two_gateways_match(host, argv):
    """The 2026-11-27 incident: ledger-drift ran against the throwaway twin."""
    host.containers([TWIN, PROD])
    p = host.run(["bash", str(REPO / argv[0]), *argv[1:]])
    assert p.returncode != 0, p.stdout + p.stderr
    assert "refusing to act: 2 running containers match" in p.stderr, (
        p.stdout + p.stderr
    )
    assert not [c for c in host.calls() if not c.startswith("ps")]
