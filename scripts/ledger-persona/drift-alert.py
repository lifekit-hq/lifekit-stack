#!/usr/bin/env python3
"""Daily Ledger persona drift alert: run `ledger-persona.py check` against the
pin and tell the owner when the live AGENTS.md differs from the repo version
or a retired big-picture copy is back in the finance workspace.

Run by lifekit-ledger-persona-check.timer as the deploy account, the only one
that can read the finance workspace (docs/runbook.md "Ledger persona drift
alert"). Three outcomes, nothing else:

  in sync   silent, exit 0
  drift     one `act` envelope through notify-relay (docs/message-format.md),
            exit 0. The timer is daily, so a drift nobody fixed is reported
            again each day.
  failure   the check itself failed (fetch, unreadable file, bad pin) or the
            relay did not accept the envelope: the reason goes to stderr (the
            journal) and the exit is 1. That fails the service, which the unit
            gauge reports and the "host systemd unit is down or failed" rule
            alerts on - the existing route, so a broken check is never silent.

Arguments after the program name go to `check` unchanged (--workspace, --pin,
--source). notify-relay is internal to the compose network, so the envelope is
POSTed from inside its own container with `docker exec`; no port is published
and no secret is read. DOCKER overrides the docker binary (tests).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CHECK = Path(__file__).resolve().with_name("ledger-persona.py")
DOCKER = os.environ.get("DOCKER", "docker")
CHECK_TIMEOUT = 180
POST_TIMEOUT = 60
DETAIL_LINES = 40
# Runs inside the notify-relay container (node, no curl): envelope on stdin.
POST_JS = (
    "let b='';"
    "process.stdin.on('data',d=>b+=d).on('end',async()=>{"
    "const r=await fetch('http://127.0.0.1:'+(process.env.NOTIFY_RELAY_PORT||8090)+'/notify',"
    "{method:'POST',headers:{'content-type':'application/json'},body:b});"
    "console.log(r.status+' '+await r.text());process.exit(r.ok?0:1)})"
)
ACTION = (
    "fold the change into finance-sentry agent/ledger (or revert the live file), "
    "bump scripts/ledger-persona/PIN, then ledger-persona.py install"
)
STALE_ACTION = (
    "delete the retired path, or cut the stale text down to owner facts and a "
    "pointer to the finance-sentry primer"
)


def envelope(check_stdout: str) -> dict:
    lines = check_stdout.strip().splitlines() or ["live AGENTS.md differs"]
    head = lines[0].removeprefix("ledger persona: ")
    detail = "\n".join(lines[1 : 1 + DETAIL_LINES])
    stale = any(line.startswith("stale: ") for line in lines[1:])
    persona = not stale or "DRIFT" in lines[0]
    body, action = [], []
    if persona:
        body.append(
            "The finance agent's live AGENTS.md is not what finance-sentry "
            "agent/ledger composes at the pinned commit. Agent instructions "
            "change through the repo, not live."
        )
        action.append(ACTION)
    if stale:
        body.append(
            "A retired big-picture copy is back in the finance workspace; "
            "finance-sentry's primer is Ledger's only big-picture source."
        )
        action.append(STALE_ACTION)
    return {
        "level": "act",
        "source": "host",
        "subject": "ledger-persona",
        "headline": f"Ledger persona drifted: {head}"[:780],
        "body": " ".join(body),
        "detail": detail,
        "action": "; ".join(action),
    }


def post(env: dict) -> None:
    """POST the envelope to notify-relay; raise RuntimeError on any refusal."""
    ps = subprocess.run(
        [
            DOCKER,
            "ps",
            "-q",
            "--filter",
            "label=com.docker.compose.project=compose",
            "--filter",
            "label=com.docker.compose.service=notify-relay",
        ],
        capture_output=True,
        text=True,
        timeout=POST_TIMEOUT,
    )
    cid = ps.stdout.split()[0] if ps.returncode == 0 and ps.stdout.split() else ""
    if not cid:
        raise RuntimeError(
            "no running notify-relay container in compose project compose"
        )
    out = subprocess.run(
        [DOCKER, "exec", "-i", cid, "node", "-e", POST_JS],
        input=json.dumps(env),
        capture_output=True,
        text=True,
        timeout=POST_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"notify-relay refused the envelope: {(out.stdout + out.stderr).strip()}"
        )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        res = subprocess.run(
            [sys.executable, str(CHECK), "check", *args],
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"ledger persona check timed out after {CHECK_TIMEOUT}s", file=sys.stderr)
        return 1
    if res.returncode == 0:
        print(res.stdout.strip())
        return 0
    if res.returncode != 1:
        msg = res.stderr.strip() or res.stdout.strip() or f"exit {res.returncode}"
        print(f"ledger persona check failed: {msg}", file=sys.stderr)
        return 1
    try:
        post(envelope(res.stdout))
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"ledger persona drifted but the alert was not delivered: {exc}",
            file=sys.stderr,
        )
        return 1
    print(res.stdout.strip().splitlines()[0])
    print("ledger persona drift alert sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
