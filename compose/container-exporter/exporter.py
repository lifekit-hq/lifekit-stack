#!/usr/bin/env python3
"""container-exporter - the Docker daemon's own facts about every container on
the box, as Prometheus metrics.

One scrape target covers every service the box runs - OpenClaw, devclaw,
finance-sentry, the dashboard, this stack - so a crash loop, a failing
healthcheck or a container that stopped is one Grafana rule away from Telegram,
whichever repo owns the container. The rules live in
observability/grafana/provisioning/alerting/rules.yml (group `box`).

Why this exists instead of cAdvisor or an off-the-shelf exporter (2026-09-13):
finance-sentry-mcp restarted every ~60 seconds for three days (4211 restarts)
and nothing said so. The daemon knows restart counts, health status and exit
codes; cgroup samplers do not, and a fast crash loop barely has a cgroup to
sample. prometheus-net/docker_exporter has no arm64 image. So: ~150 lines of
stdlib, read-only GETs on the socket, tested in tests/.

Metrics (labels: name, project, service - the compose project/service labels,
empty for containers compose did not start):
  docker_container_running              1 when State.Status == "running"
  docker_container_restarting           1 while the daemon is between restarts
  docker_container_restart_count        the daemon's RestartCount (counter)
  docker_container_exit_code            last exit code
  docker_container_oom_killed           1 if the last exit was the OOM killer
  docker_container_started_at_seconds   last start, unix time
  docker_container_healthy              1 healthy / 0 otherwise; only with a healthcheck
  docker_container_memory_usage_bytes   running containers; usage minus inactive_file,
  docker_container_memory_limit_bytes   the same numbers `docker stats` shows
  docker_exporter_scrape_errors         containers this scrape could not read
  docker_exporter_scrape_duration_seconds
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
PORT = int(os.environ.get("EXPORTER_PORT", "9417"))

# Exposition order and metadata. Every metric emitted by container_samples()
# and collect() must be listed here; render() refuses unknown names.
METRICS = {
    "docker_container_running": (
        "gauge",
        "1 when the container's State.Status is running.",
    ),
    "docker_container_restarting": (
        "gauge",
        "1 while the daemon is between restarts of the container.",
    ),
    "docker_container_restart_count": (
        "counter",
        "Times the daemon restarted the container (RestartCount).",
    ),
    "docker_container_exit_code": ("gauge", "Exit code of the container's last exit."),
    "docker_container_oom_killed": (
        "gauge",
        "1 if the container's last exit was the OOM killer.",
    ),
    "docker_container_started_at_seconds": (
        "gauge",
        "Unix time of the container's last start.",
    ),
    "docker_container_healthy": (
        "gauge",
        "1 when the healthcheck reports healthy; absent without a healthcheck.",
    ),
    "docker_container_memory_usage_bytes": (
        "gauge",
        "Memory in use (usage minus inactive_file), running containers only.",
    ),
    "docker_container_memory_limit_bytes": (
        "gauge",
        "Memory limit the container runs under.",
    ),
    "docker_exporter_scrape_errors": (
        "gauge",
        "Containers this scrape failed to inspect.",
    ),
    "docker_exporter_scrape_duration_seconds": (
        "gauge",
        "Wall time of the last scrape.",
    ),
}


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over the docker unix socket. No third-party dependency."""

    def __init__(self, path: str, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def docker_get(path: str):
    """GET a docker API path, decoded from JSON. Raises on any non-200."""
    conn = _UnixHTTPConnection(DOCKER_SOCKET)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
    finally:
        conn.close()
    if resp.status != 200:
        raise RuntimeError(f"docker GET {path}: HTTP {resp.status}")
    return json.loads(body)


def parse_docker_time(value: str | None) -> float:
    """Docker's RFC3339 with nanoseconds -> unix seconds. The zero time is 0."""
    if not value or value.startswith("0001-"):
        return 0.0
    head, _, frac = value.rstrip("Z").partition(".")
    stamp = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return stamp.timestamp() + (float("0." + frac) if frac else 0.0)


def container_samples(
    inspect: dict, stats: dict | None = None
) -> list[tuple[str, dict, float]]:
    """Pure: one container's inspect document (+ optional one-shot stats) -> samples."""
    compose = (inspect.get("Config") or {}).get("Labels") or {}
    labels = {
        "name": inspect.get("Name", "").lstrip("/"),
        "project": compose.get("com.docker.compose.project", ""),
        "service": compose.get("com.docker.compose.service", ""),
    }
    state = inspect.get("State") or {}
    # State.Running stays true while the daemon is between restarts, so a crash
    # loop would read as "running"; Status is the daemon's actual verdict.
    samples = [
        (
            "docker_container_running",
            labels,
            1.0 if state.get("Status") == "running" else 0.0,
        ),
        (
            "docker_container_restarting",
            labels,
            1.0 if state.get("Restarting") else 0.0,
        ),
        (
            "docker_container_restart_count",
            labels,
            float(inspect.get("RestartCount") or 0),
        ),
        ("docker_container_exit_code", labels, float(state.get("ExitCode") or 0)),
        ("docker_container_oom_killed", labels, 1.0 if state.get("OOMKilled") else 0.0),
        (
            "docker_container_started_at_seconds",
            labels,
            parse_docker_time(state.get("StartedAt")),
        ),
    ]
    health = (state.get("Health") or {}).get("Status")
    if health:
        samples.append(
            ("docker_container_healthy", labels, 1.0 if health == "healthy" else 0.0)
        )
    memory = (stats or {}).get("memory_stats") or {}
    if "usage" in memory:
        inactive_file = (memory.get("stats") or {}).get("inactive_file", 0)
        samples.append(
            (
                "docker_container_memory_usage_bytes",
                labels,
                float(max(memory["usage"] - inactive_file, 0)),
            )
        )
        if memory.get("limit"):
            samples.append(
                ("docker_container_memory_limit_bytes", labels, float(memory["limit"]))
            )
    return samples


def collect(get=docker_get) -> list[tuple[str, dict, float]]:
    """Every container on the daemon (running or not) -> samples.

    One unreadable container is counted and skipped, never fatal: a container
    that vanishes between the list and its inspect is normal during a deploy.
    A failing list IS fatal - the caller answers 500 and Prometheus records
    up=0, which is the alert.
    """
    started = time.monotonic()
    samples: list[tuple[str, dict, float]] = []
    errors = 0
    for entry in get("/containers/json?all=1"):
        cid = entry["Id"]
        try:
            inspect = get(f"/containers/{cid}/json")
            running = (inspect.get("State") or {}).get("Status") == "running"
            stats = (
                get(f"/containers/{cid}/stats?stream=false&one-shot=true")
                if running
                else None
            )
        except (OSError, RuntimeError, ValueError) as exc:
            errors += 1
            print(f"container {entry.get('Names')}: {exc}", file=sys.stderr)
            continue
        samples.extend(container_samples(inspect, stats))
    samples.append(("docker_exporter_scrape_errors", {}, float(errors)))
    samples.append(
        ("docker_exporter_scrape_duration_seconds", {}, time.monotonic() - started)
    )
    return samples


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(samples: list[tuple[str, dict, float]]) -> str:
    """Prometheus text exposition: samples grouped per metric, HELP/TYPE once."""
    by_metric: dict[str, list[tuple[dict, float]]] = {}
    for metric, labels, value in samples:
        if metric not in METRICS:
            raise ValueError(f"unknown metric {metric}; add it to METRICS")
        by_metric.setdefault(metric, []).append((labels, value))
    lines: list[str] = []
    for metric, (kind, help_text) in METRICS.items():
        if metric not in by_metric:
            continue
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} {kind}")
        for labels, value in by_metric[metric]:
            label_text = ",".join(
                f'{key}="{_escape(str(val))}"' for key, val in labels.items()
            )
            lines.append(
                f"{metric}{{{label_text}}} {value!r}"
                if label_text
                else f"{metric} {value!r}"
            )
    return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/health":
            self._reply(200, "ok\n", "text/plain")
            return
        if self.path.split("?", 1)[0] != "/metrics":
            self._reply(404, "not found\n", "text/plain")
            return
        try:
            body = render(collect())
        except Exception as exc:  # noqa: BLE001 - any failure to read the daemon is the signal
            print(f"scrape failed: {exc}", file=sys.stderr)
            self._reply(500, f"scrape failed: {exc}\n", "text/plain")
            return
        self._reply(200, body, "text/plain; version=0.0.4; charset=utf-8")

    def _reply(self, status: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:  # one line per scrape is noise
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    print(
        f"container-exporter listening on :{PORT}, docker socket {DOCKER_SOCKET}",
        file=sys.stderr,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
