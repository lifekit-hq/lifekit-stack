#!/usr/bin/env python3
"""check-wger-compose - static gate for compose/wger/docker-compose.yml.

Reads a `docker compose config --format json` document (a file, or - for
stdin) and fails (exit 1, one line per problem) when:

  - a service's image has no exact version tag (no tag, `latest`, or a bare
    major such as `postgres:17`): a deploy must not move under us;
  - the database (`db`, /var/lib/postgresql/data) or the app's media
    (`web`, /home/wger/media) is not on a named volume the file declares;
  - a service publishes a port on anything but loopback (the only way in is
    Tailscale Serve, docs/runbook.md "Workout manager (wger)");
  - `nginx` is not on `wger-gateway` (the gateway's route to wger, docs/runbook.md
    "Kit and wger"), any other service joins it, any service joins the wide
    `lifekit-shared` network, or `wger-gateway` is not a declared external network.

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
# nginx is the only wger service on the network shared with the OpenClaw gateway;
# the wide cross-project network is not for wger at all.
GATEWAY_NETWORK = "wger-gateway"
GATEWAY_SERVICE = "nginx"
WIDE_NETWORK = "lifekit-shared"


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
        networks = svc.get("networks") or {}
        if WIDE_NETWORK in networks:
            problems.append(f"{name}: must not join {WIDE_NETWORK}")
        if GATEWAY_NETWORK in networks and name != GATEWAY_SERVICE:
            problems.append(
                f"{name}: must not join {GATEWAY_NETWORK} (only {GATEWAY_SERVICE} may)"
            )
        if name == GATEWAY_SERVICE and GATEWAY_NETWORK not in networks:
            problems.append(f"{name}: must join {GATEWAY_NETWORK}")
        for port in svc.get("ports") or []:
            host_ip = port.get("host_ip") if isinstance(port, dict) else None
            if host_ip != "127.0.0.1":
                problems.append(f"{name}: port {port} is not bound to 127.0.0.1")

    gateway = (config.get("networks") or {}).get(GATEWAY_NETWORK)
    if gateway is None or not gateway.get("external"):
        problems.append(f"network {GATEWAY_NETWORK} must be declared external")

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
