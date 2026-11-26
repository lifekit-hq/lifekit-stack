"""learn-mcp (compose/learn-mcp/, docs/runbook.md "learn-mcp (the study coach's
connector)"): the gate, the two tools, the access log, and learning_context on
a fixture vault and a fixture LearnHarness mirror.

The vault is a temporary git repo shaped like the real one with a stub
bin/lint.sh (as scripts/tests/test_learning_record.py); the mirror is two
session files in LearnHarness's native format 1. The app runs in-process under
Starlette's TestClient; record_artefact runs the real record.py.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SERVICE = HERE.parent
REPO = SERVICE.parents[1]
sys.path.insert(0, str(SERVICE))

import context  # noqa: E402
import server  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

TOKEN = "t" * 64
INSIDE = "160.79.104.10"  # in Anthropic's 160.79.104.0/21
OUTSIDE = "203.0.113.7"

PAGE = """\
---
name: learning
updatedAt: 2026-10-01
status: active
---

# Learning

## Artefact record (v0 - one row per artefact)

| date | topic | artefact (repo/path) | reading | applied | post | grilled | status |
|---|---|---|---|---|---|---|---|
| 2026-11-23 | leetcode: two-sum | https://leetcode.com/problems/two-sum/ | - | - | - | pattern: hash map; time O(n); space O(n) | done |
| 2026-11-25 | leetcode: valid-anagram | https://leetcode.com/problems/valid-anagram/ | - | - | - | - | in progress |
| 2026-11-27 | leetcode: contains-duplicate | https://leetcode.com/problems/contains-duplicate/ | - | - | - | - | in progress |

## In-progress
_(topics touched but not solid — to fill as you go)_
- sliding window: still guessing the shrink condition
- heaps

## Notes
- not an in-progress line
"""

LINT = r"""#!/bin/sh
for f in $(git diff --name-only); do
  for l in $(grep -o '\[\[[^]]*\]\]' "$f" | tr -d '[]'); do
    [ -e "$l.md" ] || printf 'medium\tbroken-link\t%s\t[[%s]] resolves to nothing\n' "$f" "$l"
  done
done
"""

PROBLEMS = {
    "patterns": [
        {
            "id": "A2",
            "name": "Arrays and hashing",
            "problems": [
                {"slug": "contains-duplicate", "title": "Contains Duplicate"},
                {"slug": "valid-anagram", "title": "Valid Anagram"},
                {"slug": "two-sum", "title": "Two Sum"},
                {"slug": "group-anagrams", "title": "Group Anagrams"},
            ],
        }
    ]
}


def session(sid, updated, messages):
    return {
        "id": sid,
        "title": f"session {sid}",
        "createdAt": updated,
        "updatedAt": updated,
        "kind": "practice",
        "messages": messages,
    }


def result(tool, **details):
    return {
        "role": "toolResult",
        "toolName": tool,
        "toolCallId": "c",
        "details": details,
    }


MIRROR = {
    "old": session(
        "old",
        "2026-11-20T10:00:00Z",
        [
            result(
                "quiz",
                kind="quiz",
                questionText="Q-wrong",
                correct=False,
                answeredAt="2026-11-20T10:01:00Z",
            ),
            result(
                "quiz",
                kind="quiz",
                questionText="Q-right",
                correct=True,
                confidence=90,
                answeredAt="2026-11-20T10:02:00Z",
            ),
        ],
    ),
    "new": session(
        "new",
        "2026-11-25T18:00:00Z",
        [
            result(
                "probe",
                kind="quiz",
                questionText="Q-unsure",
                correct=True,
                confidence=30,
                answeredAt="2026-11-25T18:01:00Z",
            ),
            result(
                "explain",
                kind="explain",
                questionText="Q-dunno",
                dontKnow=True,
                answeredAt="2026-11-25T18:02:00Z",
            ),
            result(
                "ask_user_question",
                kind="ask",
                questionText="Q-ask",
                dismissed=True,
                answeredAt="2026-11-25T18:03:00Z",
            ),
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "p1",
                        "name": "problem",
                        "arguments": {
                            "statement": "Find the window",
                            "modelSolution": "x",
                        },
                    }
                ],
            },
            {
                "role": "custom",
                "customType": "problem-dismissed",
                "timestamp": 1795629840000,
                "details": {"problemCallId": "p1"},
            },
        ],
    ),
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / "domains").mkdir(parents=True)
    (v / "bin").mkdir()
    (v / "domains/learning.md").write_text(PAGE)
    (v / "bin/lint.sh").write_text(LINT)
    git = ["git", "-C", str(v), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    m = tmp_path / "mirror"
    (m / "chats").mkdir(parents=True)
    for sid, s in MIRROR.items():
        (m / "chats" / f"{sid}.json").write_text(json.dumps(s))
    problems = tmp_path / "problems.json"
    problems.write_text(json.dumps(PROBLEMS))
    values = {
        "LEARNING_VAULT": str(v),
        "LEARNING_TODAY": "2026-11-27",
        "LEETCODE_TODAY": "2026-11-27",  # a Friday: a problem day
        "LEETCODE_PROBLEMS": str(problems),
        "LEETCODE_RECORD_PY": str(REPO / "skills/learning-record/record.py"),
        "LEARN_MCP_DAILY_PY": str(REPO / "skills/leetcode-daily/daily.py"),
        "LEARN_MCP_MIRROR": str(m),
        "LEARN_MCP_TOKEN": TOKEN,
    }
    for k, val in values.items():
        monkeypatch.setenv(k, val)
    monkeypatch.setattr(server, "RECORD_PY", Path(values["LEETCODE_RECORD_PY"]))
    return {"vault": v, "mirror": m}


@pytest.fixture
def client(env):
    with TestClient(server.build_app(), client=("10.0.0.5", 50000)) as c:
        yield c


AUTH = {"authorization": f"Bearer {TOKEN}"}
MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}


def rpc(client, method, params=None, headers=AUTH, **kw):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers={**MCP_HEADERS, **headers}, **kw)


def call(client, tool, **args):
    r = rpc(client, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def rows(env):
    lines = (env["vault"] / "domains/learning.md").read_text().splitlines()
    return [ln for ln in lines if ln.startswith("| 2")]


# --- the gate ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong"},
        {"authorization": f"Bearer {TOKEN}x"},
        {"authorization": TOKEN},
        {"authorization": f"bearer {TOKEN}"},
    ],
)
def test_a_request_without_the_exact_bearer_gets_401_and_no_tool_runs(
    client, env, headers, monkeypatch
):
    ran = []
    monkeypatch.setattr(server, "append_row", lambda **kw: ran.append(kw))
    monkeypatch.setattr(context, "learning_context", lambda: ran.append("read"))
    for tool, args in (
        ("record_artefact", {"topic": "x", "artefact": "y"}),
        ("learning_context", {}),
    ):
        r = rpc(
            client, "tools/call", {"name": tool, "arguments": args}, headers=headers
        )
        assert r.status_code == 401
    assert ran == []
    assert len(rows(env)) == 3


def test_no_token_configured_refuses_everything(env, monkeypatch):
    monkeypatch.setenv("LEARN_MCP_TOKEN", "")
    with TestClient(server.build_app()) as c:
        assert (
            rpc(c, "tools/list", headers={"authorization": "Bearer "}).status_code
            == 401
        )


def test_the_token_is_compared_in_constant_time(client, monkeypatch):
    seen = []
    real = server.hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(server.hmac, "compare_digest", spy)
    assert rpc(client, "tools/list").status_code == 200
    assert seen == [(f"Bearer {TOKEN}".encode(), f"Bearer {TOKEN}".encode())]


def test_a_forwarded_request_from_outside_anthropics_ranges_gets_403(client):
    r = rpc(client, "tools/list", headers={**AUTH, "x-forwarded-for": OUTSIDE})
    assert r.status_code == 403
    r = rpc(client, "tools/list", headers={**AUTH, "x-forwarded-for": "not-an-ip"})
    assert r.status_code == 403


def test_a_forwarded_request_from_inside_the_ranges_passes_the_gate(client):
    r = rpc(
        client,
        "tools/list",
        headers={**AUTH, "x-forwarded-for": f"{INSIDE}, 172.18.0.1"},
    )
    assert r.status_code == 200


def test_the_range_check_comes_before_the_token(client):
    r = rpc(client, "tools/list", headers={"x-forwarded-for": OUTSIDE})
    assert r.status_code == 403


def test_health_ready_and_metrics_answer_only_a_direct_caller(client):
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200, client.get("/ready").text
    m = client.get("/metrics")
    assert m.status_code == 200 and "learn_mcp_requests_total" in m.text
    assert "job=" not in m.text and "instance=" not in m.text
    for path in ("/health", "/ready", "/metrics"):
        assert client.get(path, headers={"x-forwarded-for": INSIDE}).status_code == 404


def test_ready_says_why_not(env, client, monkeypatch):
    (env["vault"] / "bin/lint.sh").unlink()
    r = client.get("/ready")
    assert r.status_code == 503 and "no vault lint" in r.text


def test_a_large_body_gets_413(client, env, monkeypatch):
    monkeypatch.setattr(server, "append_row", lambda **kw: pytest.fail("ran"))
    r = rpc(
        client,
        "tools/call",
        {
            "name": "record_artefact",
            "arguments": {"topic": "x" * 70000, "artefact": "y"},
        },
    )
    assert r.status_code == 413


def test_the_rate_limit_answers_429(env, monkeypatch):
    monkeypatch.setenv("LEARN_MCP_RATE_PER_MIN", "2")
    with TestClient(server.build_app()) as c:
        codes = [rpc(c, "tools/list").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# --- the tools -----------------------------------------------------------------------------


def test_exactly_two_tools_and_neither_takes_a_path(client):
    r = rpc(client, "tools/list")
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {"learning_context", "record_artefact"}
    assert tools["learning_context"]["inputSchema"].get("properties", {}) == {}
    assert set(tools["record_artefact"]["inputSchema"]["properties"]) == {
        "topic",
        "artefact",
        "status",
        "grilled",
        "reading",
        "applied",
        "post",
    }
    assert tools["learning_context"]["annotations"]["readOnlyHint"] is True


def structured(res):
    assert not res.get("isError"), res
    return res.get("structuredContent") or json.loads(res["content"][0]["text"])


def test_learning_context_on_a_problem_day(client):
    ctx = structured(call(client, "learning_context"))
    assert ctx["today"] == "2026-11-27" and ctx["weekday"] == "Friday"
    today = ctx["todays_problem"]
    assert today["problem"]["slug"] == "contains-duplicate"
    assert (
        today["problem"]["url"] == "https://leetcode.com/problems/contains-duplicate/"
    )
    assert today["next_pick"]["slug"] == "group-anagrams"
    assert {p["slug"] for p in ctx["open_problems"]} == {
        "valid-anagram",
        "contains-duplicate",
    }
    assert ctx["week"].startswith("solved: 1 (Monday 2026-11-23 to Sunday 2026-11-29")
    assert ctx["in_progress"] == [
        "sliding window: still guessing the shrink condition",
        "heaps",
    ]
    assert ctx["recent_rows"][0]["topic"] == "leetcode: contains-duplicate"
    assert ctx["newest_session"] == {
        "title": "session new",
        "updated": "2026-11-25T18:00:00Z",
    }
    assert [(g["question"], g["why"]) for g in ctx["gaps"]] == [
        ("Find the window", "practice problem skipped"),
        ("Q-dunno", "don't know"),
        ("Q-unsure", "right but unsure (30%)"),
        ("Q-wrong", "wrong"),
    ]


@pytest.mark.parametrize(
    "day,why",
    [
        ("2026-11-24", "office day (Tuesday)"),
        ("2026-11-26", "office day (Thursday)"),
        ("2026-11-29", "rest day (Sunday)"),
    ],
)
def test_learning_context_says_no_problem_today_with_the_next_pick(
    client, monkeypatch, day, why
):
    monkeypatch.setenv("LEETCODE_TODAY", day)
    today = structured(call(client, "learning_context"))["todays_problem"]
    assert today["problem"] is None
    assert today["note"] == f"no problem today ({why})"
    assert today["next_pick"]["slug"] == "group-anagrams"


def test_learning_context_before_the_morning_send(client, monkeypatch):
    monkeypatch.setenv("LEETCODE_TODAY", "2026-11-28")
    today = structured(call(client, "learning_context"))["todays_problem"]
    assert today["problem"] is None and "no problem offered yet" in today["note"]


def test_learning_context_without_a_mirror_still_answers(client, env):
    for f in (env["mirror"] / "chats").iterdir():
        f.unlink()
    (env["mirror"] / "chats").rmdir()
    ctx = structured(call(client, "learning_context"))
    assert ctx["gaps"] == [] and ctx["newest_session"] is None
    assert "unavailable" in ctx["mirror"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through mode 000")
def test_learning_context_with_an_unreadable_mirror_still_answers(client, env):
    env["mirror"].chmod(0)
    try:
        ctx = structured(call(client, "learning_context"))
    finally:
        env["mirror"].chmod(0o755)
    assert ctx["gaps"] == [] and "unavailable" in ctx["mirror"]
    assert ctx["todays_problem"]["problem"]["slug"]


def test_learning_context_never_writes_to_the_mirror(client, env):
    before = sorted((p, p.stat().st_mtime_ns) for p in env["mirror"].rglob("*"))
    call(client, "learning_context")
    assert sorted((p, p.stat().st_mtime_ns) for p in env["mirror"].rglob("*")) == before


def test_record_artefact_appends_one_row_through_record_py(client, env):
    res = call(
        client,
        "record_artefact",
        topic="leetcode: contains-duplicate",
        artefact="https://leetcode.com/problems/contains-duplicate/",
        grilled="pattern: hash set; time O(n); space O(n)",
        status="done",
    )
    assert not res.get("isError"), res
    assert (
        "recorded: | 2026-11-27 | leetcode: contains-duplicate |"
        in res["content"][0]["text"]
    )
    assert rows(env)[-1] == (
        "| 2026-11-27 | leetcode: contains-duplicate | "
        "https://leetcode.com/problems/contains-duplicate/ | - | - | - | "
        "pattern: hash set; time O(n); space O(n) | done |"
    )
    # read back through learning_context
    ctx = structured(call(client, "learning_context"))
    assert ctx["recent_rows"][0]["status"] == "done"
    assert ctx["week"].startswith("solved: 2 ")


@pytest.mark.parametrize(
    "args,said",
    [
        ({"topic": "a|b", "artefact": "x"}, "pipe"),
        ({"topic": "x", "artefact": "[[nowhere]]"}, "lint refused"),
        ({"topic": "x", "artefact": "y", "status": "maybe"}, "status"),
        ({"topic": "--status=done", "artefact": "--help"}, None),
    ],
)
def test_record_artefact_refusals_write_nothing(client, env, args, said):
    before = (env["vault"] / "domains/learning.md").read_bytes()
    res = call(client, "record_artefact", **args)
    if said is None:  # an option-looking value is a cell, not an option
        assert not res.get("isError"), res
        assert rows(env)[-1].startswith("| 2026-11-27 | --status=done | --help |")
        return
    assert res["isError"] is True and said in res["content"][0]["text"]
    assert (env["vault"] / "domains/learning.md").read_bytes() == before


# --- the access log --------------------------------------------------------------------------


def test_the_access_log_has_time_source_and_tool_and_never_the_arguments(
    client, caplog
):
    caplog.set_level(logging.INFO, logger="learn-mcp")
    secret_words = "my-own-words-about-hash-sets"
    rpc(
        client,
        "tools/call",
        {
            "name": "record_artefact",
            "arguments": {"topic": secret_words, "artefact": secret_words},
        },
        headers={**AUTH, "x-forwarded-for": INSIDE},
    )
    rpc(client, "tools/list", headers={"authorization": "Bearer nope"})
    lines = [
        json.loads(server.JsonFormatter().format(r))
        for r in caplog.records
        if r.name == "learn-mcp"
    ]
    tool = next(ln for ln in lines if ln.get("tool"))
    assert tool["tool"] == "record_artefact" and tool["src"] == INSIDE
    assert tool["status"] == 200 and tool["ts"] and len(tool["trace_id"]) == 32
    denied = next(ln for ln in lines if ln["status"] == 401)
    assert denied["src"] == "10.0.0.5"
    text = "\n".join(json.dumps(ln) for ln in lines)
    assert secret_words not in text and TOKEN not in text


def test_the_server_module_never_reads_a_caller_path():
    # No tool argument reaches a filesystem call: the only paths are env-set.
    src = (SERVICE / "server.py").read_text() + (SERVICE / "context.py").read_text()
    assert "subprocess" not in (SERVICE / "context.py").read_text()
    assert src.count("subprocess.run(") == 1
