#!/usr/bin/env python3
"""Stand-in for the document door's door.py: records its argv and stdin to $DOOR_LOG,
so the canary can prove the one allowlisted exec ran and nothing else did."""

import json
import sys

with open("/out/door-calls.log", "a") as f:
    f.write(
        json.dumps(
            {
                "argv": sys.argv[1:],
                "stdin": sys.stdin.read() if not sys.stdin.isatty() else "",
            }
        )
        + "\n"
    )
print("No new documents.")
