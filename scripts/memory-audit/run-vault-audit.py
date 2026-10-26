#!/usr/bin/env python3
"""Run the weekly vault audit wrapper on the host and tell the owner the result.

Run by lifekit-vault-audit.timer as `lifekit` (docs/runbook.md "Weekly vault
audit (host timer)"). It runs memory-vault-audit.sh next to it and turns the
wrapper's one stdout line into one notify-relay envelope (docs/message-format.md),
which replaces the Telegram announce of the OpenClaw cron it succeeds:

  success   exit 0 from the wrapper: an `info` envelope whose headline is the
            wrapper's summary line.
  failure   any other exit, a missing wrapper or a timeout: an `act` envelope
            carrying the wrapper's `AUDIT FAIL: ...` line, and the exit is 1.
            That fails the service, which the unit gauge reports and the
            "host systemd unit is down or failed" rule alerts on - so a
            failure the relay cannot carry is still not silent.

A relay that does not accept the envelope is also exit 1. notify-relay is
internal to the compose network, so the envelope is POSTed from inside its own
container with `docker exec`, as scripts/ledger-persona/drift-alert.py does.
WRAPPER and DOCKER override the wrapper path and the docker binary (tests).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WRAPPER = os.environ.get(
    "WRAPPER", str(Path(__file__).resolve().with_name("memory-vault-audit.sh"))
)
DOCKER = os.environ.get("DOCKER", "docker")
AUDIT_TIMEOUT = 840
POST_TIMEOUT = 60
HEADLINE_MAX = 700
# Runs inside the notify-relay container (node, no curl): envelope on stdin.
POST_JS = (
    "let b='';"
    "process.stdin.on('data',d=>b+=d).on('end',async()=>{"
    "const r=await fetch('http://127.0.0.1:'+(process.env.NOTIFY_RELAY_PORT||8090)+'/notify',"
    "{method:'POST',headers:{'content-type':'application/json'},body:b});"
    "console.log(r.status+' '+await r.text());process.exit(r.ok?0:1)})"
)
FAIL_ACTION = "journalctl -u lifekit-vault-audit -n 40"


def run_audit() -> tuple[bool, str, str]:
    """Return (ok, wrapper stdout, stderr)."""
    try:
        res = subprocess.run(
            ["sh", WRAPPER],
            capture_output=True,
            text=True,
            timeout=AUDIT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"AUDIT FAIL: no result within {AUDIT_TIMEOUT}s", ""
    except OSError as err:
        return False, f"AUDIT FAIL: cannot run {WRAPPER}: {err}", ""
    out = res.stdout.strip()
    if res.returncode != 0 and not out:
        out = f"AUDIT FAIL: wrapper exited {res.returncode} with no output"
    elif res.returncode == 0 and not out:
        return False, "AUDIT FAIL: wrapper printed no summary", res.stderr.strip()
    return res.returncode == 0, out, res.stderr.strip()


def envelope(ok: bool, out: str, err: str) -> dict:
    line = out.splitlines()[0][:HEADLINE_MAX]
    if ok:
        return {
            "level": "info",
            "source": "host",
            "subject": "vault-audit",
            "headline": line,
        }
    env = {
        "level": "act",
        "source": "host",
        "subject": "vault-audit",
        "headline": line,
        "action": FAIL_ACTION,
    }
    if err:
        env["detail"] = err[-2000:]
    return env


def find_container(project: str, service: str) -> str:
    res = subprocess.run(
        [
            DOCKER,
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
        capture_output=True,
        text=True,
        timeout=POST_TIMEOUT,
        check=False,
    )
    return res.stdout.split()[0] if res.stdout.split() else ""


def post(env: dict) -> str | None:
    """Send the envelope; return why it was not accepted, else None."""
    try:
        cid = find_container("compose", "notify-relay")
        if not cid:
            return "no running notify-relay container in compose project compose"
        res = subprocess.run(
            [DOCKER, "exec", "-i", cid, "node", "-e", POST_JS],
            input=json.dumps(env),
            capture_output=True,
            text=True,
            timeout=POST_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return f"cannot reach notify-relay: {err}"
    if res.returncode != 0:
        return f"notify-relay refused the envelope: {(res.stdout + res.stderr).strip()}"
    return None


def main() -> int:
    ok, out, err = run_audit()
    print(out)
    if err:
        print(err, file=sys.stderr)
    refused = post(envelope(ok, out, err))
    if refused:
        print(f"vault audit: {refused}", file=sys.stderr)
    return 0 if ok and not refused else 1


if __name__ == "__main__":
    sys.exit(main())
