"""The study-panel canary (scripts/study-panel-canary): does the tool-less study-panel
cap hold, and does the door's JSON-text request (no tools) still come back?

Two layers, as for the reader canary. The verdict logic is tested on synthetic
evidence, so CI proves the canary can fail (a leaked write, a turn that never ran, a
lost result, a wider catalogue, a reply that is not the JSON text) without Docker.
The live test runs the real thing - a local turn plus the gateway's chat-completions
endpoint in a throwaway network-less container from the gateway image, against a mock
model API - and so needs Docker and the `lifekit-openclaw:local` image: it skips with a
loud reason where they are absent (CI's hosted runner) and fails instead of skipping
when LIFEKIT_REQUIRE_CANARY=1 (the box).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts/study-panel-canary"
AGENT_JSON = REPO / "defaults/agents/study-panel/agent.json"

spec = importlib.util.spec_from_file_location("study_verdict", CANARY / "verdict.py")
verdict = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verdict)

CASES = json.loads((CANARY / "cases.json").read_text())
AGENT = json.loads(AGENT_JSON.read_text())
REPLY = json.loads((CANARY / "reply.json").read_text())

PLACEHOLDER = {
    "name": "DeferredToolPlaceholder",
    "description": "Reserved placeholder that keeps deferred tool loading active; never call this tool.",
    "input_schema": {"type": "object", "properties": {}},
    "defer_loading": True,
}


def write_jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def http_response(**over) -> dict:
    body = {
        "status": 200,
        "body": {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(REPLY)},
                }
            ]
        },
    }
    body.update(over)
    return body


def evidence(
    tmp_path: Path,
    leak: int | None = None,
    drop: int | None = None,
    local_tools: list | None = None,
    http_tools: list | None = None,
    response: dict | None = None,
) -> Path:
    """Synthetic evidence of a run in which the cap held, except for what is given:
    `leak` is a case that ran, `drop` a case whose result never came back."""
    out = tmp_path / "out"
    out.mkdir()
    results = []
    for case in CASES:
        ran = case["id"] == leak
        if ran and "file" in case.get("effect", {}):
            (out / case["effect"]["file"]).write_text("x")
        if case["id"] != drop:
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": f"toolu_canary{case['id']}",
                    "is_error": not ran,
                    "content": "stub ok" if ran else "No such tool available",
                }
            )
    local = [PLACEHOLDER] if local_tools is None else local_tools
    write_jsonl(
        out / "mock.log",
        [
            {"path": "/v1/messages", "req": {"tools": local, "messages": []}},
            {
                "path": "/v1/messages",
                "req": {
                    "tools": local,
                    "messages": [{"role": "user", "content": results}],
                },
            },
        ],
    )
    over_http = [] if http_tools is None else http_tools
    write_jsonl(
        out / "mock-http.log",
        [{"path": "/v1/messages", "req": {"tools": over_http, "messages": []}}],
    )
    (out / "http-response.json").write_text(json.dumps(response or http_response()))
    return out


def judge(out: Path) -> list:
    return verdict.judge_local(str(out), CASES, AGENT) + verdict.judge_http(
        str(out), REPLY
    )


def test_a_run_where_the_cap_held_passes(tmp_path):
    assert judge(evidence(tmp_path)) == []


def test_every_case_is_a_denial_and_ids_follow_the_mock_index():
    assert {c["expect"] for c in CASES} == {"deny"}
    assert [c["id"] for c in CASES] == list(range(len(CASES)))


@pytest.mark.parametrize(
    "why",
    [
        "any shell command",
        "native file write of the solution",
        "native file edit",
        "an interpreter one-liner",
    ],
)
def test_a_denied_call_that_ran_fails_the_canary(tmp_path, why):
    case = next(c for c in CASES if c["why"].startswith(f"denied: {why}"))
    bad = judge(evidence(tmp_path, leak=case["id"]))
    assert bad and any(f"case {case['id']}" in b for b in bad)


def test_the_cases_cover_what_the_cap_is_for():
    """The canary must probe the classes the cap exists to refuse, by tool."""
    tools = {c["tool"] for c in CASES}
    assert {
        "mcp__openclaw__exec",
        "Write",
        "Read",
        "WebFetch",
        "mcp__openclaw__message",
        "mcp__openclaw__cron",
        "mcp__openclaw__sessions_send",
        "mcp__openclaw__memory_search",
    } <= tools


def test_a_turn_that_never_ran_is_not_a_pass(tmp_path):
    out = tmp_path / "empty"
    out.mkdir()
    bad = judge(out)
    assert "no model request reached the mock: the local turn did not run" in bad
    assert "no model request reached the mock: the HTTP stage did not run" in bad


def test_a_lost_result_is_not_a_pass(tmp_path):
    bad = judge(evidence(tmp_path, drop=2))
    assert any("no result came back" in b for b in bad)


def test_an_entry_with_no_tools_block_fails_the_canary(tmp_path):
    """The negative control: swap in an entry with no `tools` block and the gateway
    offers its default set and runs the calls."""
    default_set = [
        {"name": "mcp__openclaw__exec"},
        {"name": "Write"},
        {"name": "Read"},
    ]
    out = evidence(tmp_path, local_tools=default_set, leak=0)
    bad = judge(out)
    assert any("tool catalogue" in b for b in bad)
    assert any("case 0" in b for b in bad)


def test_a_tool_offered_to_a_local_turn_fails(tmp_path):
    bad = judge(evidence(tmp_path, local_tools=[{"name": "mcp__openclaw__message"}]))
    assert any("tool catalogue" in b for b in bad)


def test_the_inert_deferred_placeholder_is_not_a_wider_catalogue(tmp_path):
    assert judge(evidence(tmp_path, local_tools=[PLACEHOLDER])) == []


@pytest.mark.parametrize(
    "tool",
    [
        {**PLACEHOLDER, "defer_loading": False},
        {**PLACEHOLDER, "input_schema": {"type": "object", "properties": {"cmd": {}}}},
        {**PLACEHOLDER, "name": "DeferredToolPlaceholder2"},
    ],
)
def test_a_lookalike_of_the_placeholder_still_fails(tmp_path, tool):
    assert any(
        "tool catalogue" in b
        for b in judge(evidence(tmp_path, local_tools=[PLACEHOLDER, tool]))
    )


@pytest.mark.parametrize(
    "tools",
    [
        [{"name": "reply"}],
        [{"name": "mcp__openclaw__reply"}, {"name": "mcp__openclaw__exec"}],
        [{"name": "mcp__openclaw__exec"}],
    ],
)
def test_the_http_request_must_offer_no_tool(tmp_path, tools):
    bad = judge(evidence(tmp_path, http_tools=tools))
    assert any("chat-completions request" in b and "want none" in b for b in bad)


def test_the_inert_placeholder_is_not_a_tool_on_the_http_request(tmp_path):
    assert judge(evidence(tmp_path, http_tools=[PLACEHOLDER])) == []


def test_whitespace_around_the_json_text_is_fine(tmp_path):
    padded = http_response()
    padded["body"]["choices"][0]["message"]["content"] = f"\n {json.dumps(REPLY)} \n"
    assert judge(evidence(tmp_path, response=padded)) == []


def test_a_reply_that_is_a_tool_call_fails(tmp_path):
    call = http_response()
    call["body"]["choices"][0] = {
        "finish_reason": "tool_calls",
        "message": {
            "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "reply", "arguments": json.dumps(REPLY)}}
            ]
        },
    }
    assert any("finished text answer" in b for b in judge(evidence(tmp_path, response=call)))


@pytest.mark.parametrize(
    "content",
    [
        "def solve(): pass",
        "Sure! " + json.dumps(REPLY),
        "```json\n" + json.dumps(REPLY) + "\n```",
        json.dumps(REPLY) + json.dumps(REPLY),
        json.dumps({**REPLY, "text": "something else"}),
        None,
    ],
)
def test_content_that_is_not_exactly_the_reply_object_fails(tmp_path, content):
    wrong = http_response()
    wrong["body"]["choices"][0]["message"]["content"] = content
    assert any("want the JSON text" in b for b in judge(evidence(tmp_path, response=wrong)))


def test_an_endpoint_error_fails(tmp_path):
    bad = judge(evidence(tmp_path, response={"status": 400, "body": "no tools"}))
    assert any("answered 400" in b for b in bad)


def test_a_missing_response_fails(tmp_path):
    out = evidence(tmp_path)
    (out / "http-response.json").unlink()
    assert any("no response came back" in b for b in judge(out))


def test_the_live_canary_passes_on_the_shipped_entry():
    proc = subprocess.run(
        ["bash", str(CANARY / "run.sh")],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if proc.returncode == 77 and os.environ.get("LIFEKIT_REQUIRE_CANARY") != "1":
        pytest.skip(
            f"LIVE CANARY NOT RUN (no docker or gateway image): {proc.stderr.strip()}; run it on the box"
        )
    assert proc.returncode == 0, proc.stdout + proc.stderr
