"""The deploy smoke turn's verdict: scripts/openclaw-smoke-verdict.py.

deploy-openclaw.sh pipes `openclaw agent --json ... 2>&1` into it, so the reply
is pretty-printed JSON with CLI log lines around it. #382: a good reply (status
ok, text pong) was judged "no JSON" because the first-"{"-to-last-"}" slice took
in log lines that carry braces, and every OpenClaw deploy ended red.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERDICT = REPO / "scripts/openclaw-smoke-verdict.py"

# The reply shape from the failing run (actions run 39585097732), as the CLI
# prints it: indented two spaces, one key per line.
REPLY = """{
  "runId": "0f96f1ce-af75-4a5b-b5ae-eefcecacdd68",
  "status": "ok",
  "summary": "completed",
  "result": {
    "payloads": [
      {
        "text": "pong",
        "mediaUrl": null
      }
    ],
    "meta": {
      "durationMs": 4210
    }
  }
}"""
LEADING_NOISE = "[plugins] loaded {diagnostics-otel}\n"
TRAILING_NOISE = (
    "[diagnostics] flushed {spans: 3}\n"
    '[agent] session closed {"sessionId": "deploy-smoke-kit-20261021T103500Z"}\n'
)


def run(raw: str) -> str:
    r = subprocess.run(
        [sys.executable, str(VERDICT)],
        input=raw,
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.rstrip("\n")


def old_slice_parses(raw: str) -> bool:
    try:
        json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
    except ValueError:
        return False
    return True


def test_regression_382_multiline_reply_among_log_lines_passes():
    raw = LEADING_NOISE + REPLY + "\n" + TRAILING_NOISE
    # The shape that broke: the old slice cannot parse it.
    assert not old_slice_parses(raw)
    assert run(raw) == "PASS"


@pytest.mark.parametrize(
    "raw",
    [
        REPLY,
        REPLY + "\n",
        LEADING_NOISE + REPLY,
        REPLY + "\n" + TRAILING_NOISE,
        REPLY + "\n}\n",
        "warning: { unbalanced\n" + REPLY,
    ],
)
def test_good_reply_passes_whatever_surrounds_it(raw):
    assert run(raw) == "PASS"


def reply(**fields) -> str:
    d = json.loads(REPLY)
    d.update(fields)
    return json.dumps(d, indent=2)


def test_empty_reply_fails():
    out = run(reply(result={"payloads": [{"text": "  ", "mediaUrl": None}]}))
    assert out.startswith("FAIL ")
    assert json.loads(out[5:]) == {
        "status": "ok",
        "summary": "completed",
        "reply": ["  "],
    }


def test_error_status_fails_with_the_error():
    out = run(
        reply(status="error", error={"message": "model crashed"})
        + "\n"
        + TRAILING_NOISE
    )
    assert out == 'FAIL {"message": "model crashed"}'


@pytest.mark.parametrize(
    "message", ["rate_limit_error", "weekly limit reached", "OAuth token expired"]
)
def test_auth_and_quota_errors_only_warn(message):
    out = run(LEADING_NOISE + reply(status="error", error={"message": message}))
    assert out == "WARN " + json.dumps({"message": message})


def test_no_json_fails_with_the_output_head():
    out = run("Error: gateway not reachable\n{ not json }\n")
    assert out == "FAIL no JSON: Error: gateway not reachable { not json } "


def test_empty_output_fails():
    assert run("") == "FAIL no JSON: "


def test_the_smoke_phase_runs_the_verdict_script():
    text = (REPO / "scripts/deploy-openclaw.sh").read_text()
    assert '| python3 "${REPO_DIR}/scripts/openclaw-smoke-verdict.py")"' in text
