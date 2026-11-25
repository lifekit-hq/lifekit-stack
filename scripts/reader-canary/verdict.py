#!/usr/bin/env python3
"""Reads back one reader-canary run and says whether the tool cap held.

    verdict.py <out dir> <cases.json> <agent.json>

For every call the mock model made it checks the model's own view (the tool_result
each call got back: refused or ran) against the side effects (a call that reached the
stub connector, the stub door script, or left a file), and against the expectation in
cases.json. The tool catalogue the model was offered must hold nothing beyond what the
shipped allow-list grants. A run that made no model request at all, or lost a result, fails:
a canary that could not run must never read as a pass.
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


def results_by_id(requests: list) -> dict:
    """tool_use id -> (is_error, text), from the last model request that carried results."""
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
    """Claude Code's two built-ins for deferred tool loading, neither of which grants a
    capability. Each is matched on its whole shape, so a real tool that reuses a name
    (extra properties, or not deferred) still fails the check."""
    schema = tool.get("input_schema") or {}
    props = set(schema.get("properties") or {})
    if tool.get("name") == "DeferredToolPlaceholder":
        # A stub that keeps deferred loading switched on: deferred, takes no input.
        return (
            tool.get("defer_loading") is True
            and schema.get("type") == "object"
            and not props
            and not schema.get("required")
        )
    if tool.get("name") == "ToolSearch":
        # The loader that fetches the schema of a deferred tool already in the catalogue
        # (here, only the allow-listed ones); it takes a query and a result count, nothing else.
        return (
            tool.get("defer_loading") is not True
            and schema.get("type") == "object"
            and props == {"query", "max_results"}
            and set(schema.get("required") or []) <= props
        )
    return False


def judge(out: str, cases: list, agent: dict) -> list:
    """Returns the list of failures (empty = the cap held)."""
    bad = []
    requests = [
        r
        for r in read_jsonl(os.path.join(out, "mock.log"))
        if r["path"].startswith("/v1/messages") and "count_tokens" not in r["path"]
    ]
    if not requests:
        return ["no model request reached the mock: the turn did not run"]
    catalogue = {
        t["name"]
        for t in requests[0]["req"].get("tools") or []
        if not is_inert_placeholder(t)
    }
    allow = agent["agents"]["entries"]["reader"]["tools"]["allow"]
    # Claude Code may defer the allowed tools behind ToolSearch, so they need not be listed
    # up front: the catalogue must hold nothing beyond the grant, and the allowed calls
    # running (checked per case below) proves the grant itself is reachable.
    if not catalogue <= expected_catalogue(allow):
        bad.append(
            f"tool catalogue offered to the model is {sorted(catalogue)}, the allow-list grants {sorted(expected_catalogue(allow))}"
        )
    results = results_by_id(requests)
    mcp = [c.get("name") for c in read_jsonl(os.path.join(out, "mcp-calls.log"))]
    door = [c["argv"] for c in read_jsonl(os.path.join(out, "door-calls.log"))]
    for case in cases:
        tid = f"toolu_canary{case['id']}"
        if tid not in results:
            bad.append(f"case {case['id']} ({case['why']}): no result came back")
            continue
        is_error, text = results[tid]
        eff = case.get("effect", {})
        ran = False
        if "mcp" in eff:
            ran = eff["mcp"] in mcp
        if "door" in eff:
            ran = any(a[: len(eff["door"])] == eff["door"] for a in door)
        if "file" in eff:
            ran = os.path.exists(os.path.join(out, eff["file"]))
        if case["expect"] == "allow" and (is_error or not ran):
            bad.append(
                f"case {case['id']} ({case['why']}): expected to run, error={is_error} reached={ran}: {text[:160]}"
            )
        if case["expect"] == "deny" and (not is_error or ran):
            bad.append(
                f"case {case['id']} ({case['why']}): expected refusal, error={is_error} reached={ran}: {text[:160]}"
            )
    return bad


def main() -> int:
    out, cases_path, agent_path = sys.argv[1:4]
    with open(cases_path) as f:
        cases = json.load(f)
    with open(agent_path) as f:
        agent = json.load(f)
    bad = judge(out, cases, agent)
    results = results_by_id(read_jsonl(os.path.join(out, "mock.log")))
    for case in cases:
        got = results.get(f"toolu_canary{case['id']}")
        state = "?" if got is None else ("refused" if got[0] else "ran")
        print(f"  [{case['expect']:5}] {state:7} {case['why']}")
    if bad:
        print("reader canary FAILED:", *bad, sep="\n  ", file=sys.stderr)
        return 1
    print(
        "reader canary passed: every denied call was refused, every allowed call ran, nothing else reached the connector or the filesystem"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
