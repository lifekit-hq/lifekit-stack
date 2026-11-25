#!/usr/bin/env python3
"""check-wger-powersync - does the PowerSync sync service answer through wger's nginx?

    check-wger-powersync.py [BASE_URL] [--wait SECONDS]

BASE_URL is wger's front door (default http://127.0.0.1:8480, the loopback
binding; the tailnet address works too). It exercises the same path the phone
app takes, through nginx, and exits 1 with one line per failed check:

  - GET /ps/probes/liveness   the sync service is up (nginx proxies /ps/ to it);
  - GET /ps/probes/readiness  it is ready: connected to wger's Postgres and
                              replicating (this fails while the replication
                              slot or the storage role is broken);
  - GET /api/v2/powersync-keys  wger serves the public half of the sign-in
                              key (the service reads it to verify the app's
                              tokens): a JWKS with at least one key.

--wait keeps retrying for that many seconds (a fresh start takes a while to
become ready); default 0, one attempt. No credentials are sent or needed, and
nothing but status lines is printed. Standard library only, so it runs on the
host as is: scripts/deploy.sh runs it after bringing wger up, and
docs/runbook.md "Workout manager (wger)" lists it among the verify steps.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8480"
TIMEOUT = 5


def fetch(url: str) -> tuple[int, bytes]:
    """(status, body); status 0 when nothing answered."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, b""
    except (urllib.error.URLError, OSError):
        return 0, b""


def run_checks(base: str) -> list[str]:
    """One pass over the three checks; the problems found, empty when all pass."""
    base = base.rstrip("/")
    problems: list[str] = []
    for name in ("liveness", "readiness"):
        status, _ = fetch(f"{base}/ps/probes/{name}")
        if status != 200:
            problems.append(
                f"/ps/probes/{name} answered {status or 'nothing'}, want 200"
                " (powersync down, or nginx's /ps/ proxy broken)"
            )
    status, body = fetch(f"{base}/api/v2/powersync-keys")
    try:
        keys = json.loads(body).get("keys") if status == 200 else None
    except (ValueError, AttributeError):
        keys = None
    if not keys:
        problems.append(
            f"/api/v2/powersync-keys answered {status or 'nothing'} without a key"
            " (WGER_JWT_* not set, or web down)"
        )
    return problems


def main(argv: list[str]) -> int:
    args = argv[1:]
    wait = 0.0
    if "--wait" in args:
        i = args.index("--wait")
        try:
            wait = float(args[i + 1])
        except (IndexError, ValueError):
            print("check-wger-powersync: --wait needs a number of seconds", file=sys.stderr)
            return 2
        del args[i : i + 2]
    if len(args) > 1:
        print(__doc__, file=sys.stderr)
        return 2
    base = args[0] if args else DEFAULT_BASE

    deadline = time.monotonic() + wait
    while True:
        problems = run_checks(base)
        if not problems or time.monotonic() >= deadline:
            break
        time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
    for p in problems:
        print(f"check-wger-powersync: {p}", file=sys.stderr)
    if not problems:
        print(f"check-wger-powersync: ok ({base}/ps/ live and ready, keys served)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
