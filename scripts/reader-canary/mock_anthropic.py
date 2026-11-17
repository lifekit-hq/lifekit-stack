#!/usr/bin/env python3
"""Mock Anthropic Messages API for the reader canary.

The first model request of a turn is answered with one tool_use block per
[name, input] pair in $MOCK_TOOL_CALLS (ids toolu_canary0, toolu_canary1, ...);
the follow-up request, which carries the tool_results, is logged and answered
with plain text. Every request is appended to $MOCK_LOG, so the canary reads
back what the model was offered (the tool catalogue) and what each call got.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("MOCK_LOG", "/tmp/mock.log")
CALLS = json.loads(os.environ.get("MOCK_TOOL_CALLS", "[]"))


def sse(events: list) -> bytes:
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


def has_tool_result(messages: list) -> bool:
    return any(
        isinstance(m.get("content"), list)
        and any(c.get("type") == "tool_result" for c in m["content"])
        for m in messages
    )


def stream_events(blocks: list, stop: str, model: str) -> list:
    usage = {"input_tokens": 10, "output_tokens": 1}
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "usage": usage,
                },
            },
        )
    ]
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            start, delta = (
                {"type": "text", "text": ""},
                {"type": "text_delta", "text": b["text"]},
            )
        else:
            start = {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}
        events += [
            (
                "content_block_start",
                {"type": "content_block_start", "index": i, "content_block": start},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": i, "delta": delta},
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": i}),
        ]
    events += [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return events


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def reply(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            req = {}
        with open(LOG, "a") as f:
            f.write(json.dumps({"path": self.path, "req": req}) + "\n")
        if self.path.startswith("/v1/messages/count_tokens"):
            return self.reply(
                json.dumps({"input_tokens": 10}).encode(), "application/json"
            )
        blocks = []
        if (
            not has_tool_result(req.get("messages", []))
            and req.get("tools") is not None
        ):
            blocks = [
                {"type": "tool_use", "id": f"toolu_canary{i}", "name": n, "input": inp}
                for i, (n, inp) in enumerate(CALLS)
            ]
        stop = "tool_use" if blocks else "end_turn"
        blocks = blocks or [{"type": "text", "text": "canary done"}]
        model = req.get("model", "m")
        if req.get("stream"):
            return self.reply(
                sse(stream_events(blocks, stop, model)), "text/event-stream"
            )
        msg = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": blocks,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        self.reply(json.dumps(msg).encode(), "application/json")


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
