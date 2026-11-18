"""Every recurring job and the deploy smoke turn run on the model modelPolicy names (#589).

The model is defined once: agents.defaults.model.primary in
compose/openclaw-gateway/platform.patch.json, read by scripts/lib/cron-model.sh. This
lint fails when a script spells a model out instead of asking the lib, when a job
declaration drops its `--model`, or when the model the lib hands out is not on
modelPolicy.allow. Kit's own chat model (host state) is not covered.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PATCH = REPO / "compose/openclaw-gateway/platform.patch.json"
DEFAULTS = json.loads(PATCH.read_text())["agents"]["defaults"]
ALLOW = DEFAULTS["modelPolicy"]["allow"]

SCRIPTS = sorted(
    p
    for p in [*REPO.glob("scripts/*.sh"), *REPO.glob("scripts/lib/*.sh")]
    if p.is_file()
)
# A model id, with or without its provider: anything of this shape in a script is a
# model somebody named by hand.
MODEL_ID = re.compile(r"(?:anthropic/)?claude-(?:opus|sonnet|haiku|fable)-[0-9][0-9.-]*")
# Job-declaring scripts whose rows run no model (kind command) or whose model is the
# Kit sweep's (ensure-kit-cron-models.sh): they carry no `--model` of their own.
NO_MODEL_JOBS = {
    "ensure-memory-vault-cleanup.sh",  # a command-kind row
    "ensure-learning-line.sh",  # edits message/tools of an existing row
    "ensure-today-brief-fleet.sh",  # edits message of an existing row
}


def test_the_cron_model_is_on_the_allow_list():
    assert DEFAULTS["model"]["primary"] in ALLOW
    assert DEFAULTS["subagents"]["model"] in ALLOW
    assert DEFAULTS["heartbeat"]["model"] in ALLOW


def test_the_lib_hands_out_the_policy_model():
    out = subprocess.run(
        ["bash", "-c", f"source {REPO}/scripts/lib/cron-model.sh && cron_model"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == DEFAULTS["model"]["primary"]


def test_the_lib_refuses_a_model_outside_the_policy(tmp_path):
    bad = json.loads(PATCH.read_text())
    bad["agents"]["defaults"]["model"]["primary"] = "anthropic/claude-opus-4-7"
    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps(bad))
    res = subprocess.run(
        ["bash", "-c", f"source {REPO}/scripts/lib/cron-model.sh && cron_model"],
        capture_output=True,
        text=True,
        env={"CRON_MODEL_PATCH": str(patch), "PATH": "/usr/bin:/bin"},
    )
    assert res.returncode != 0 and "not in modelPolicy.allow" in res.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_script_names_a_model_outside_the_policy(script):
    for n, line in enumerate(script.read_text().splitlines(), 1):
        for m in MODEL_ID.findall(line):
            full = m if m.startswith("anthropic/") else f"anthropic/{m}"
            assert full in ALLOW, f"{script.name}:{n} names {m}, not in modelPolicy.allow"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_model_flag_takes_the_policy_variable_never_a_literal(script):
    for n, line in enumerate(script.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for arg in re.findall(r"--model[ =](\S+)", line):
            assert arg.startswith(('"$', "$")), (
                f"{script.name}:{n}: --model {arg} is a literal; use cron_model"
            )


@pytest.mark.parametrize(
    "script",
    [p for p in SCRIPTS if "cron add" in p.read_text()],
    ids=lambda p: p.name,
)
def test_a_script_that_adds_a_job_declares_its_model(script):
    text = script.read_text()
    if script.name in NO_MODEL_JOBS:
        return
    assert "lib/cron-model.sh" in text, f"{script.name} adds a job without cron_model"
    assert '--model "$' in text, f"{script.name} adds a job with no --model"


def test_the_deploy_smoke_turn_runs_on_the_policy_model():
    text = (REPO / "scripts/deploy-openclaw.sh").read_text()
    assert "lib/cron-model.sh" in text
    assert re.search(r'openclaw agent [^\n]*--model "\$\{SMOKE_MODEL\}"', text)
