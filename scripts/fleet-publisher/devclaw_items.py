#!/usr/bin/env python3
"""devclaw_items.py - print devclaw's open goals and tasks for the fleet publisher.

The publisher's FLEET_DEVCLAW_CMD (README "Queue items"): runs devclaw's own
read-only export, `devclaw tasks --json`, inside the running devclaw container
(found by its compose labels) and prints its JSON list on stdout unchanged: one
envelope per open goal and task, `{id, kind, store, ref, refs, stage, state,
since, owner, class}`. The export opens devclaw's state read-only and carries no
issue text, prompt or session content (devclaw's README); this command adds
nothing and writes nothing.

A failed read (no container, the command failing, output that is not a JSON
list) exits nonzero with a one-line reason on stderr and prints nothing, so the
publisher reports the source unreadable rather than empty.

  python3 devclaw_items.py
"""

from __future__ import annotations

import json
import subprocess
import sys

CONTAINER_LABELS = (
    "label=com.docker.compose.project=devclaw",
    "label=com.docker.compose.service=devclaw-mcp",
)
EXPORT = ("devclaw", "tasks", "--json")


def devclaw_container() -> str:
    filters = [a for f in CONTAINER_LABELS for a in ("--filter", f)]
    found = subprocess.run(
        ["docker", "ps", "-q", *filters],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.split()
    if len(found) != 1:
        raise RuntimeError(f"{len(found)} devclaw containers running")
    return found[0]


def export(container: str) -> list:
    out = subprocess.run(
        ["docker", "exec", container, *EXPORT],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    doc = json.loads(out)
    if not isinstance(doc, list):
        raise ValueError("export is not a list")
    return doc


def main(argv: list[str]) -> int:
    if argv:
        print("usage: devclaw_items.py", file=sys.stderr)
        return 2
    try:
        doc = export(devclaw_container())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"devclaw tasks unreadable: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    json.dump(doc, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
