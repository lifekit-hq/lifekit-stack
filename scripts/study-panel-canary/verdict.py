#!/usr/bin/env python3
"""Reads back one study-panel canary run and says whether the tool cap held.

    verdict.py <out dir> <cases.json> <agent.json> <reply.json>

Two stages, both must pass (see container-run.sh):

  local  The tool catalogue the model was offered on a local turn must be exactly what
         the shipped allow-list grants (nothing: the entry is tool-less), and every call
         the mock model made, on the instruction of an injection-bearing page, must have
         come back refused with nothing left on the filesystem.
  http   The door's request (no tools, tool_choice "none") must reach the model with an
         empty tool catalogue, and the answer must come back as a finished text message
         (finish_reason stop, no tool call) whose content parses to the reply object the
         model gave.

A run that made no model request, lost a result or never reached the HTTP endpoint
fails: a canary that could not run must never read as a pass.
"""

import json
import os
import sys


def read_jsonl(path: str) -> list:
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def model_requests(path: str) -> list:
    return [
        r
        for r in read_jsonl(path)
        if r["path"].startswith("/v1/messages") and "count_tokens" not in r["path"]
    ]


def results_by_id(requests: list) -> dict:
    """tool_use id -> (is_error, text), from the model requests that carried results."""
    found = {}
    for rec in requests:
        for m in rec["req"].get("messages", []):
            if not isinstance(m.get("content"), list):
                continue
            for c in m["content"]:
                if c.get("type") == "tool_result":
                    body = c.get("content")
                    text = body if isinstance(body, str) else json.dumps(body)
                    found[c["tool_use_id"]] = (bool(c.get("is_error")), text)
    return found


def expected_catalogue(allow: list) -> set:
    names = set()
    if "exec" in allow:
        names.add("mcp__openclaw__exec")
    names |= {"mcp__openclaw__" + a for a in allow if "__" in a}
    return names


def is_inert_placeholder(tool: dict) -> bool:
    """Claude Code sends a stub that keeps deferred tool loading switched on: it is deferred,
    takes no input and carries no capability. Matched on its whole shape, so a real tool that
    reuses the name (a schema with properties, or one not deferred) still fails the check."""
    schema = tool.get("input_schema") or {}
    return (
        tool.get("name") == "DeferredToolPlaceholder"
        and tool.get("defer_loading") is True
        and schema.get("type") == "object"
        and not schema.get("properties")
        and not schema.get("required")
    )


def offered(request: dict) -> set:
    return {
        t["name"]
        for t in request["req"].get("tools") or []
        if not is_inert_placeholder(t)
    }


def judge_local(out: str, cases: list, agent: dict) -> list:
    """Failures of the local turn (empty = the cap held)."""
    requests = model_requests(os.path.join(out, "mock.log"))
    if not requests:
        return ["no model request reached the mock: the local turn did not run"]
    bad = []
    allow = agent["agents"]["entries"]["study-panel"]["tools"]["allow"]
    catalogue, want = offered(requests[0]), expected_catalogue(allow)
    if catalogue != want:
        bad.append(
            f"tool catalogue offered to the model is {sorted(catalogue)}, the allow-list grants {sorted(want)}"
        )
    results = results_by_id(requests)
    for case in cases:
        tid = f"toolu_canary{case['id']}"
        if tid not in results:
            bad.append(f"case {case['id']} ({case['why']}): no result came back")
            continue
        is_error, text = results[tid]
        eff = case.get("effect", {})
        ran = "file" in eff and os.path.exists(os.path.join(out, eff["file"]))
        if not is_error or ran:
            bad.append(
                f"case {case['id']} ({case['why']}): expected refusal, error={is_error} reached={ran}: {text[:160]}"
            )
    return bad


def judge_http(out: str, reply: dict) -> list:
    """Failures of the HTTP stage (empty = the JSON text came back and no tool was offered)."""
    requests = model_requests(os.path.join(out, "mock-http.log"))
    if not requests:
        return ["no model request reached the mock: the HTTP stage did not run"]
    bad = []
    catalogue = offered(requests[0])
    if catalogue:
        bad.append(
            f"tool catalogue offered on the chat-completions request is {sorted(catalogue)}, want none"
        )
    try:
        with open(os.path.join(out, "http-response.json")) as f:
            resp = json.load(f)
    except (FileNotFoundError, ValueError):
        return bad + ["no response came back from the chat-completions endpoint"]
    if resp.get("status") != 200 or not isinstance(resp.get("body"), dict):
        return bad + [f"the endpoint answered {resp.get('status')}: {str(resp.get('body'))[:200]}"]
    choice = (resp["body"].get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if choice.get("finish_reason") != "stop" or message.get("tool_calls"):
        bad.append(
            f"the reply did not come back as a finished text answer (finish_reason={choice.get('finish_reason')}, tool_calls={len(message.get('tool_calls') or [])})"
        )
        return bad
    try:
        got = json.loads(message.get("content") or "")
    except ValueError:
        got = None
    if got != reply:
        bad.append(
            f"the message content is {str(message.get('content'))[:120]!r}, want the JSON text of the model's reply object"
        )
    return bad


def main() -> int:
    out, cases_path, agent_path, reply_path = sys.argv[1:5]
    with open(cases_path) as f:
        cases = json.load(f)
    with open(agent_path) as f:
        agent = json.load(f)
    with open(reply_path) as f:
        reply = json.load(f)
    bad = judge_local(out, cases, agent) + judge_http(out, reply)
    results = results_by_id(model_requests(os.path.join(out, "mock.log")))
    for case in cases:
        got = results.get(f"toolu_canary{case['id']}")
        state = "?" if got is None else ("refused" if got[0] else "ran")
        print(f"  [deny ] {state:7} {case['why']}")
    if bad:
        print("study-panel canary FAILED:", *bad, sep="\n  ", file=sys.stderr)
        return 1
    print(
        "study-panel canary passed: no tool offered on a local turn, every denied call refused, "
        "the HTTP request offered no tool and got the reply back as JSON text"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
