#!/usr/bin/env python3
"""Stand-in for the google-workspace MCP server: the two Gmail read tools plus the
send tool. Every tools/call is appended to $STUB_LOG, so the canary can prove which
calls reached the server at all."""

import json
import sys

# The gateway starts MCP servers with a scrubbed environment: the path is fixed.
LOG = "/out/mcp-calls.log"
NAMES = (
    "search_gmail_messages",
    "get_gmail_messages_content_batch",
    "send_gmail_message",
    "draft_gmail_message",
)
TOOLS = [
    {
        "name": n,
        "description": n,
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
    for n in NAMES
]


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    i, method = m.get("id"), m.get("method")
    if method == "initialize":
        version = m.get("params", {}).get("protocolVersion", "2025-06-18")
        out(
            {
                "jsonrpc": "2.0",
                "id": i,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "stub", "version": "0"},
                },
            }
        )
    elif method == "tools/list":
        out({"jsonrpc": "2.0", "id": i, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        with open(LOG, "a") as f:
            f.write(json.dumps(m["params"]) + "\n")
        out(
            {
                "jsonrpc": "2.0",
                "id": i,
                "result": {"content": [{"type": "text", "text": "stub ok"}]},
            }
        )
    elif i is not None:
        out({"jsonrpc": "2.0", "id": i, "result": {}})
