"""The reader canary (scripts/reader-canary): does the reader's tool cap hold on a
managed claude-cli turn?

Two layers. The verdict logic is tested on synthetic evidence, so CI proves the
canary can fail (a leaked send, a turn that never ran, a lost result) without Docker.
The live test runs the real thing - a gateway turn in a throwaway network-less
container from the gateway image against a mock model API - and so needs Docker and
the `lifekit-openclaw:local` image: it skips with a loud reason where they are absent
(CI's hosted runner) and fails instead of skipping when LIFEKIT_REQUIRE_CANARY=1
(the box).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts/reader-canary"
AGENT_JSON = REPO / "defaults/agents/reader/agent.json"

spec = importlib.util.spec_from_file_location("reader_verdict", CANARY / "verdict.py")
verdict = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verdict)

CASES = json.loads((CANARY / "cases.json").read_text())
AGENT = json.loads(AGENT_JSON.read_text())
ENTRY = AGENT["agents"]["entries"]["reader"]


def write_jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def evidence(tmp_path: Path, leak: int | None = None, drop: int | None = None) -> Path:
    """Synthetic evidence of a run in which the cap held, except for the case ids given:
    `leak` is a denied case that ran, `drop` a case whose result never came back."""
    out = tmp_path / "out"
    out.mkdir()
    results, mcp, door = [], [], []
    for case in CASES:
        ran = case["expect"] == "allow" or case["id"] == leak
        eff = case.get("effect", {})
        if ran and "mcp" in eff:
            mcp.append({"name": eff["mcp"]})
        if ran and "door" in eff:
            door.append({"argv": eff["door"]})
        if ran and "file" in eff:
            (out / eff["file"]).write_text("x")
        if case["id"] != drop:
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": f"toolu_canary{case['id']}",
                    "is_error": not ran,
                    "content": "stub ok" if ran else "No such tool available",
                }
            )
    catalogue = [
        {"name": n} for n in sorted(verdict.expected_catalogue(ENTRY["tools"]["allow"]))
    ]
    write_jsonl(
        out / "mock.log",
        [
            {"path": "/v1/messages", "req": {"tools": catalogue, "messages": []}},
            {
                "path": "/v1/messages",
                "req": {
                    "tools": catalogue,
                    "messages": [{"role": "user", "content": results}],
                },
            },
        ],
    )
    write_jsonl(out / "mcp-calls.log", mcp)
    write_jsonl(out / "door-calls.log", door)
    return out


def test_a_run_where_the_cap_held_passes(tmp_path):
    assert verdict.judge(str(evidence(tmp_path)), CASES, AGENT) == []


@pytest.mark.parametrize(
    "why",
    [
        "Gmail send",
        "Gmail draft",
        "any other command",
        "native file write",
        "web fetch",
    ],
)
def test_a_denied_call_that_ran_fails_the_canary(tmp_path, why):
    case = next(c for c in CASES if c["why"] == f"denied: {why}")
    bad = verdict.judge(str(evidence(tmp_path, leak=case["id"])), CASES, AGENT)
    assert bad and any(f"case {case['id']}" in b for b in bad)


def test_a_turn_that_never_ran_is_not_a_pass(tmp_path):
    out = tmp_path / "empty"
    out.mkdir()
    assert verdict.judge(str(out), CASES, AGENT) == [
        "no model request reached the mock: the turn did not run"
    ]


def test_a_lost_result_is_not_a_pass(tmp_path):
    bad = verdict.judge(str(evidence(tmp_path, drop=2)), CASES, AGENT)
    assert any("no result came back" in b for b in bad)


def test_a_wider_tool_catalogue_than_the_allow_list_fails(tmp_path):
    out = evidence(tmp_path)
    rows = [json.loads(line) for line in (out / "mock.log").read_text().splitlines()]
    rows[0]["req"]["tools"].append({"name": "mcp__openclaw__message"})
    write_jsonl(out / "mock.log", rows)
    assert any("tool catalogue" in b for b in verdict.judge(str(out), CASES, AGENT))


def test_the_cases_cover_what_the_cap_is_for():
    """The canary must probe the calls the cap exists to refuse, by effect."""
    deny = {json.dumps(c.get("effect")) for c in CASES if c["expect"] == "deny"}
    assert {'{"mcp": "send_gmail_message"}', '{"mcp": "draft_gmail_message"}'} <= deny
    assert [c["id"] for c in CASES] == list(
        range(len(CASES))
    ), "ids must equal the mock's tool_use index"
    allowed = {c["tool"] for c in CASES if c["expect"] == "allow"}
    assert allowed == {
        "mcp__openclaw__google-workspace__search_gmail_messages",
        "mcp__openclaw__google-workspace__get_gmail_messages_content_batch",
        "mcp__openclaw__exec",
    }


@pytest.mark.timeout(960)
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


PLACEHOLDER = {
    "name": "DeferredToolPlaceholder",
    "description": "Reserved placeholder that keeps deferred tool loading active; never call this tool.",
    "input_schema": {"type": "object", "properties": {}},
    "defer_loading": True,
}


def with_extra_tool(tmp_path, tool):
    out = evidence(tmp_path)
    rows = [json.loads(line) for line in (out / "mock.log").read_text().splitlines()]
    rows[0]["req"]["tools"].append(tool)
    write_jsonl(out / "mock.log", rows)
    return verdict.judge(str(out), CASES, AGENT)


def test_the_inert_deferred_placeholder_is_not_a_wider_catalogue(tmp_path):
    assert with_extra_tool(tmp_path, PLACEHOLDER) == []


@pytest.mark.parametrize(
    "tool",
    [
        {**PLACEHOLDER, "defer_loading": False},
        {**PLACEHOLDER, "input_schema": {"type": "object", "properties": {"cmd": {}}}},
        {**PLACEHOLDER, "name": "DeferredToolPlaceholder2"},
    ],
)
def test_a_lookalike_of_the_placeholder_still_fails(tmp_path, tool):
    assert any("tool catalogue" in b for b in with_extra_tool(tmp_path, tool))


TOOL_SEARCH = {
    "name": "ToolSearch",
    "description": "Fetches full schema definitions for deferred tools so they can be called.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}, "max_results": {"type": "number"}},
        "required": ["query", "max_results"],
    },
}


def test_the_deferred_tool_loader_is_not_a_wider_catalogue(tmp_path):
    assert with_extra_tool(tmp_path, TOOL_SEARCH) == []


@pytest.mark.parametrize(
    "tool",
    [
        {
            **TOOL_SEARCH,
            "input_schema": {
                **TOOL_SEARCH["input_schema"],
                "properties": {
                    **TOOL_SEARCH["input_schema"]["properties"],
                    "cmd": {"type": "string"},
                },
            },
        },
        {**TOOL_SEARCH, "defer_loading": True},
        {**TOOL_SEARCH, "name": "ToolSearch2"},
    ],
)
def test_a_lookalike_of_the_loader_still_fails(tmp_path, tool):
    assert any("tool catalogue" in b for b in with_extra_tool(tmp_path, tool))


def with_catalogue(tmp_path, tools):
    out = evidence(tmp_path)
    rows = [json.loads(line) for line in (out / "mock.log").read_text().splitlines()]
    rows[0]["req"]["tools"] = tools
    write_jsonl(out / "mock.log", rows)
    return out


def test_allowed_tools_deferred_behind_the_loader_still_pass(tmp_path):
    out = with_catalogue(tmp_path, [TOOL_SEARCH])
    assert verdict.judge(str(out), CASES, AGENT) == []


def test_allowed_tools_missing_from_the_catalogue_fail_when_their_call_did_not_run(
    tmp_path,
):
    out = with_catalogue(tmp_path, [TOOL_SEARCH])
    case = next(c for c in CASES if c["expect"] == "allow" and "mcp" in c["effect"])
    (out / "mcp-calls.log").write_text("")
    bad = verdict.judge(str(out), CASES, AGENT)
    assert any(f"case {case['id']}" in b and "expected to run" in b for b in bad)


def test_an_allowed_call_whose_result_is_an_error_fails_with_no_catalogue(tmp_path):
    out = with_catalogue(tmp_path, [])
    case = next(c for c in CASES if c["expect"] == "allow")
    rows = [json.loads(line) for line in (out / "mock.log").read_text().splitlines()]
    for c in rows[1]["req"]["messages"][0]["content"]:
        if c["tool_use_id"] == f"toolu_canary{case['id']}":
            c["is_error"] = True
    write_jsonl(out / "mock.log", rows)
    bad = verdict.judge(str(out), CASES, AGENT)
    assert any(f"case {case['id']}" in b and "expected to run" in b for b in bad)
