#!/usr/bin/env python3
"""A stand-in for the model (UNROUTED_MODEL_CMD) in the unrouted tests: it
reads the numbered chat on stdin and lists asks and promises by keyword, in
the `claude -p --output-format json` envelope. Synthetic, deterministic and
free; the real model is exercised only by the replay test on the host."""

import json
import re
import sys

ASK = re.compile(r"\b(do this|please|can you|should)\b", re.I)
PROMISE = re.compile(r"\b(I will|I'll|saved this|added it)\b", re.I)
RECEIPT = re.compile(r"\b(?:[0-9]{10}-[A-Za-z0-9]{6}|held-[a-z0-9-]+[a-z0-9])\b")
LINE = re.compile(r"^\[(\d+)\] \S+ \S+ (owner|agent|other): (.*)$")

items, last_ask = [], None
for line in sys.stdin.read().splitlines():
    m = LINE.match(line)
    if not m:
        continue
    n, who, text = int(m.group(1)), m.group(2), m.group(3)
    if who == "owner":
        last_ask = None
        if ASK.search(text):
            last_ask = len(items)
            items.append(
                {"kind": "ask", "msg": n, "about": None, "quote": text, "receipt": None}
            )
    elif who == "agent" and PROMISE.search(text):
        r = RECEIPT.search(text)
        item = {
            "kind": "promise",
            "msg": n,
            "quote": text,
            "receipt": r.group(0) if r else None,
        }
        if last_ask is not None:
            item["about"] = items[last_ask]["msg"]
            items[last_ask] = item
            last_ask = None
        else:
            item["about"] = None
            items.append(item)
print(
    json.dumps(
        {
            "type": "result",
            "is_error": False,
            "total_cost_usd": 0.001,
            "result": json.dumps({"items": items}),
        }
    )
)
