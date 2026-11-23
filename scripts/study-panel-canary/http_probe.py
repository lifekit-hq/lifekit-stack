#!/usr/bin/env python3
"""Waits for the in-container gateway, then sends it the request the study door sends:
model openclaw/study-panel, no tools, tool_choice "none", the injection-bearing page
text as the user turn. The response is saved for verdict.py.

    http_probe.py <port> <token> <page.txt> <out file>
"""

import json
import sys
import time
import urllib.error
import urllib.request

port, token, page_path, out_path = sys.argv[1:5]
base = f"http://127.0.0.1:{port}"
auth = {"authorization": f"Bearer {token}"}

for _ in range(90):
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/v1/models", headers=auth), timeout=3).read()
        break
    except Exception:
        time.sleep(2)
else:
    json.dump({"error": "the gateway did not come up"}, open(out_path, "w"))
    sys.exit(1)

body = {
    "model": "openclaw/study-panel",
    "user": "study:canary:20260101",
    "messages": [{"role": "user", "content": open(page_path).read()}],
    "tool_choice": "none",
}
req = urllib.request.Request(
    base + "/v1/chat/completions",
    data=json.dumps(body).encode(),
    headers={**auth, "content-type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=240) as r:
        out = {"status": r.status, "body": json.loads(r.read())}
except urllib.error.HTTPError as e:
    out = {"status": e.code, "body": e.read().decode(errors="replace")}
json.dump(out, open(out_path, "w"))
