#!/usr/bin/env python3
"""check-wger-compose - static gate for compose/wger/docker-compose.yml.

Reads a `docker compose config --format json` document (a file, or - for
stdin) and fails (exit 1, one line per problem) when:

  - a service's image has no exact version tag (no tag, `latest`, or a bare
    major such as `postgres:17`): a deploy must not move under us;
  - the database (`db`, /var/lib/postgresql/data) or the app's media
    (`web`, /home/wger/media) is not on a named volume the file declares;
  - a service publishes a port on anything but loopback (the only way in is
    Tailscale Serve, docs/runbook.md "Workout manager (wger)").

Pure mechanism, no Docker. scripts/deploy.sh runs it before `up`; the tests
(scripts/tests/test_wger_compose.py) hold the blocked and the allowed case.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# service -> container path that must sit on a named volume
PERSISTENT = {
    "db": "/var/lib/postgresql/data",
    "web": "/home/wger/media",
}
# An exact tag has a digit-dotted version in it: 2.7.0, 17.10-alpine3.22.
EXACT_TAG = re.compile(r"^\d+\.\d+(\.\d+)?([.-].+)?$")


def check(config: dict) -> list[str]:
    problems: list[str] = []
    services = config.get("services") or {}
    volumes = config.get("volumes") or {}

    for name, svc in sorted(services.items()):
        image = svc.get("image") or ""
        ref = image.split("@", 1)[0]
        tag = ref.rsplit(":", 1)[1] if ":" in ref.rsplit("/", 1)[-1] else ""
        if "@sha256:" in image:
            pass  # a digest is the strongest pin
        elif not EXACT_TAG.match(tag):
            problems.append(
                f"{name}: image {image or '(none)'} is not pinned to an exact version tag"
            )
        for port in svc.get("ports") or []:
            host_ip = port.get("host_ip") if isinstance(port, dict) else None
            if host_ip != "127.0.0.1":
                problems.append(f"{name}: port {port} is not bound to 127.0.0.1")

    for name, path in sorted(PERSISTENT.items()):
        svc = services.get(name)
        if svc is None:
            problems.append(f"{name}: service is missing")
            continue
        mounts = [
            v
            for v in svc.get("volumes") or []
            if isinstance(v, dict) and v.get("target") == path
        ]
        if (
            not mounts
            or mounts[0].get("type") != "volume"
            or not mounts[0].get("source")
        ):
            problems.append(f"{name}: {path} is not on a named volume")
        elif mounts[0]["source"] not in volumes:
            problems.append(f"{name}: volume {mounts[0]['source']} is not declared")
    return problems


def main(argv: list[str]) -> int:
    src = argv[1] if len(argv) > 1 else "-"
    raw = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
    problems = check(json.loads(raw))
    for p in problems:
        print(f"check-wger-compose: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
