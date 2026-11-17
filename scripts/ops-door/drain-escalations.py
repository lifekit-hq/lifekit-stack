#!/usr/bin/env python3
"""ops-door-escalate - turn the receiver's spooled Tier 2 requests into
`fleet-policy decide` calls.

The ops-door receiver (compose/ops-door/) runs in a container and cannot run
`fleet-policy`, which lives in the operator's home. When a repeated alert
passes its threshold the receiver writes one JSON file per call id into the
spool directory; this unit, a systemd timer running as the operator account,
files each as a Tier 2 call and moves it to `done/`. One file per call id and
a deterministic id mean the receiver cannot spool the same call twice, and a
file is moved only after `decide` exited 0, so a failed decide is retried on
the next run.

Env:
  OPS_DOOR_SPOOL_DIR   default /var/lib/lifekit/ops-door/escalations
  FLEET_POLICY_BIN     default `fleet-policy` (on PATH)

Exit 0 when every spooled call was filed (or none waited), 1 when any decide
failed (the unit then shows failed and the file stays for the next run).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")
# The two answers the captain can tap; the first is the recommendation.
OPTIONS = (
    ("another-fix", "Try a different fix: the drainer re-reads the alert and the issue"),
    ("accept", "Accept the alert as it is and park the issue"),
)


def argv_for(call: dict, fleet_policy: str) -> list[str]:
    cmd = [
        fleet_policy,
        "decide",
        call["id"],
        "--kind",
        "call",
        "--tier",
        "2",
        "--rec",
        OPTIONS[0][0],
    ]
    for key, label in OPTIONS:
        cmd += ["--option", f"{key}={label}"]
    cmd += [
        "--title",
        str(call["title"])[:200],
        "--question",
        str(call.get("question", ""))[:600],
        "--repo",
        "lifekit-stack",
        "--origin",
        "alertmanager",
        "--reversible",
    ]
    return cmd


def main() -> int:
    spool = Path(os.environ.get("OPS_DOOR_SPOOL_DIR", "/var/lib/lifekit/ops-door/escalations"))
    fleet_policy = os.environ.get("FLEET_POLICY_BIN", "fleet-policy")
    done = spool / "done"
    if not spool.is_dir():
        print(f"ops-door-escalate: no spool at {spool}; nothing to do")
        return 0
    done.mkdir(exist_ok=True)
    failed = 0
    for path in sorted(spool.glob("*.json")):
        try:
            call = json.loads(path.read_text())
            if not ID.match(str(call.get("id", ""))) or call["id"] != path.stem or not call.get("title"):
                raise ValueError("malformed request")
        except (OSError, ValueError, KeyError) as err:
            print(f"ops-door-escalate: {path.name}: {err}", file=sys.stderr)
            failed += 1
            continue
        proc = subprocess.run(
            argv_for(call, fleet_policy), capture_output=True, text=True, check=False, timeout=60
        )
        if proc.returncode != 0:
            print(
                f"ops-door-escalate: decide {call['id']} exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[:300]}",
                file=sys.stderr,
            )
            failed += 1
            continue
        path.rename(done / path.name)
        print(f"ops-door-escalate: filed Tier 2 call {call['id']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
