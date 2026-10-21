#!/usr/bin/env python3
"""Verdict for one deploy smoke turn: `openclaw agent --json` output on stdin.

Prints one line: PASS, `WARN <detail>` or `FAIL <detail>`; scripts/deploy-openclaw.sh
maps it to ok / warn / fail_later. A good turn is status "ok" with a non-empty
reply (there is no top-level "ok" key); anything else prints the error, or
status/summary/reply when there is none. Auth and quota errors are external
state (re-login, weekly cap) and only warn; anything else fails.

The input is the CLI's stdout and stderr together, so the reply object sits
among log lines that may carry braces of their own. Slicing from the first "{"
to the last "}" broke on that (#382: a valid pretty-printed reply judged
"no JSON"), so every top-level JSON object is decoded in turn and the last one
with a "status" key is the reply (the last object at all when none has one).
"""

from __future__ import annotations

import json
import re
import sys

SOFT = re.compile(
    r"rate_limit|weekly limit|usage limit|no usable profiles|expired|not logged in|auth",
    re.I,
)


def find_reply(raw: str) -> dict | None:
    decoder = json.JSONDecoder()
    reply = last = None
    pos = raw.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(raw, pos)
        except ValueError:
            pos = raw.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            last = obj
            if "status" in obj:
                reply = obj
        pos = raw.find("{", end)
    return reply if reply is not None else last


def verdict(raw: str) -> str:
    d = find_reply(raw)
    if d is None:
        return "FAIL no JSON: " + raw[:200].replace("\n", " ")
    texts = [p.get("text") or "" for p in (d.get("result") or {}).get("payloads") or []]
    if d.get("status") == "ok" and "".join(texts).strip():
        return "PASS"
    err = json.dumps(
        d.get("error")
        or {"status": d.get("status"), "summary": d.get("summary"), "reply": texts}
    )
    return ("WARN " if SOFT.search(err) else "FAIL ") + err[:300]


if __name__ == "__main__":
    print(verdict(sys.stdin.read()))
