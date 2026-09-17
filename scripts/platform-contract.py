#!/usr/bin/env python3
"""platform-contract - does every product on the box meet the platform contract?

The contract (vault system/architecture.md, locked 2026-09-13): health +
readiness, /metrics, JSON logs with trace id, OTLP traces, sits behind the
edge, owns its topics. Guardrail 1 (adopted 2026-09-16) enforces it at deploy:
the script lives here, lifekit-stack's deploy runs it on its own project, and
every product deploy runs it on its own project. There are no waivers.

A product declares itself with service labels in its OWN compose file, so the
facts about its endpoints live next to the code that serves them:

  lifekit.contract: "v1"                       # opt in; "none" = not a product
                                               #  (datastore, platform piece, upstream image)
  lifekit.contract.port: "5000"                # container port the paths below answer on
  lifekit.contract.health: /api/v1/health      # liveness, unauthenticated, 2xx, not HTML
  lifekit.contract.ready: /api/v1/health/ready # readiness, same rules, a different path
  lifekit.contract.ready.via: edge             # optional: readiness answers only an
                                               #  attributed proxy; probed through the edge
  lifekit.contract.metrics: /metrics           # Prometheus text, scraped by the box Prometheus
  lifekit.contract.metrics.auth: token         # optional: the path needs a credential only
                                               #  Prometheus holds; met by its up target
  lifekit.contract.ingress: edge               # edge (behind Traefik) | internal (no host port)
  lifekit.contract.service: finance-sentry-api # OTLP service.name (default: compose service)

Two modes, both read-only:

  --static FILE   the pre-up gate: checks the declarations in a
                  `docker compose config --format json` document (FILE, or - for
                  stdin). A service that fails here never reaches `up`.
  (default)       the runtime gate: checks the RUNNING containers from the host -
                  HTTP GETs to the container's own IP, `docker inspect`,
                  `docker logs`, and the box Prometheus' targets API.

The metrics item is "metrics reach Prometheus" (captain, 2026-09-16): a
Prometheus target for the declared address and path is up. /metrics stays the
default for our own products.

Items with no platform piece behind them (traces: collector checks not decided;
edge: no Traefik; topics: no broker) are SKIP with the reason until the PR that
lands the piece adds them to ENFORCED - one PR per piece. SKIP is never set per
product: there is no waiver label.

Usage (a product deploy - a missing script must fail the deploy, so no `|| true`):
  docker compose ... config --format json \\
    | python3 /srv/lifekit-stack/scripts/platform-contract.py --static -
  python3 /srv/lifekit-stack/scripts/platform-contract.py --project <its project>

Runtime exit 1 when a container of an enforced project (--enforce; default:
every project in scope) FAILs an enforced item or has no lifekit.contract label.
Containers of other projects are printed as a census and never fail the run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
TIMEOUT = 4
LOG_LINES = 300
JSON_SHARE = 0.9

# Grows by one id per platform piece: traces once the trace rule is decided
# (otel-collector landed in #159), edge with Traefik, topics with Redpanda.
ENFORCED = {"health", "ready", "metrics", "scraped", "logs"}
SKIP_REASON = {
    "traces": "trace rule not decided yet (captain Q8)",
    "edge": "no edge proxy yet (build order step 2)",
    "topics": "no broker yet (build order step 3)",
}
PROM_TYPES = ("text/plain; version=0.0.4", "application/openmetrics-text")
# Serilog compact JSON writes @tr; OpenClaw and most OTel log bridges traceId /
# trace_id; ECS nests it as trace.id.
TRACE_KEYS = re.compile(
    r"^(@tr|trace_?id|traceparent|trace\.id|otel\.trace_id)$", re.IGNORECASE
)
LABEL = "lifekit.contract"
INGRESS = ("edge", "internal")


def label(labels: dict, key: str = "") -> str:
    return (labels or {}).get(f"{LABEL}.{key}" if key else LABEL, "")


# ─── Static gate ─────────────────────────────────────────────────────────────


def static_findings(service: str, spec: dict) -> list[str]:
    """Declaration problems for one service of a `compose config` document."""
    labels = spec.get("labels") or {}
    declared = label(labels)
    if declared == "none":
        return []
    if declared != "v1":
        return [f"no {LABEL}=v1|none label"]
    out = []
    for key in ("port", "health", "ready", "metrics", "ingress"):
        if not label(labels, key):
            out.append(f"no {LABEL}.{key} label")
    port = label(labels, "port")
    if port and not port.isdigit():
        out.append(f"{LABEL}.port={port!r} is not a port number")
    for key in ("health", "ready", "metrics"):
        path = label(labels, key)
        if path and not path.startswith("/"):
            out.append(f"{LABEL}.{key}={path!r} is not a path")
    if label(labels, "health") and label(labels, "health") == label(labels, "ready"):
        out.append("readiness path is the liveness path")
    ingress = label(labels, "ingress")
    if ingress and ingress not in INGRESS:
        out.append(f"{LABEL}.ingress={ingress!r} is not one of {'|'.join(INGRESS)}")
    if ingress == "internal" and spec.get("ports"):
        out.append("ingress=internal but the service publishes host ports")
    if label(labels, "ready.via") not in ("", "edge"):
        out.append(f"{LABEL}.ready.via must be 'edge'")
    if label(labels, "metrics.auth") not in ("", "token"):
        out.append(f"{LABEL}.metrics.auth must be 'token'")
    return out


def run_static(source: str) -> int:
    if source == "-":
        doc = json.load(sys.stdin)
    else:
        with open(source) as fh:
            doc = json.load(fh)
    failed = []
    for service, spec in sorted(doc.get("services", {}).items()):
        findings = static_findings(service, spec)
        verdict = "none" if label(spec.get("labels"), "") == "none" else "ok"
        print(f"{service:28} {'FAIL' if findings else verdict}")
        for f in findings:
            print(f"{'':28}   {f}")
            failed.append(f"{service}: {f}")
    project = doc.get("name", "?")
    if failed:
        print(
            f"\nplatform contract (static, {project}): {len(failed)} declaration failure(s)"
        )
        for f in failed:
            print(f"  - {f}")
        return 1
    print(f"\nplatform contract (static, {project}): every service is declared")
    return 0


# ─── Runtime gate ────────────────────────────────────────────────────────────


def get(url: str, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            r.read(65536)
            return r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "")
    except (OSError, ValueError):
        return 0, ""


def containers(projects: list[str]) -> list[dict]:
    ids = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.project"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not ids:
        return []
    found = json.loads(
        subprocess.run(
            ["docker", "inspect", *ids], check=True, capture_output=True, text=True
        ).stdout
    )
    # Compose bakes its project/service labels into the images it builds, so a
    # bare `docker run` of such an image carries them too (2026-09-16: worker
    # test containers and a 3-month-old dashboard one-shot). Only compose sets
    # working_dir, at container create - that is the real membership mark.
    # `compose run` one-offs are not the serving instance.
    found = [
        c
        for c in found
        if c["Config"]["Labels"].get("com.docker.compose.project.working_dir")
        and c["Config"]["Labels"].get("com.docker.compose.oneoff") != "True"
    ]
    if projects:
        found = [
            c
            for c in found
            if c["Config"]["Labels"].get("com.docker.compose.project") in projects
        ]
    return sorted(found, key=lambda c: c["Name"])


def log_verdict(lines: list[str], probe_trace_id: str) -> tuple[str, str]:
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return "FAIL", "no log lines"
    parsed = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict):
            parsed.append(d)
    traced = [
        d
        for d in parsed
        if any(TRACE_KEYS.match(k) for k in d)
        or (isinstance(d.get("trace"), dict) and "id" in d["trace"])
    ]
    ok = len(parsed) / len(lines) >= JSON_SHARE and bool(traced)
    detail = f"{len(parsed)}/{len(lines)} JSON, {len(traced)} with trace id"
    if any(probe_trace_id in ln for ln in lines):
        detail += ", probe trace id logged"
    return ("PASS" if ok else "FAIL"), detail


def check(c: dict, targets: list[dict]) -> dict[str, tuple[str, str]]:
    labels = c["Config"]["Labels"]
    name = c["Name"].lstrip("/")
    networks = c["NetworkSettings"]["Networks"].values()
    ip = next((n["IPAddress"] for n in networks if n["IPAddress"]), "")
    port = label(labels, "port")
    base = f"http://{ip}:{port}"
    trace_id = secrets.token_hex(16)
    tp = {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}
    r: dict[str, tuple[str, str]] = {}

    for item in ("health", "ready"):
        path = label(labels, item)
        if not path:
            r[item] = ("FAIL", "no label")
        elif item == "ready" and label(labels, "ready.via") == "edge":
            # Answers only an attributed proxy (OpenClaw's /readyz); never
            # forge X-Forwarded-For from the host. Probed through the edge
            # once it lands (captain Q7) - the edge PR implements that probe.
            r[item] = (
                ("FAIL", "edge probe not implemented")
                if "edge" in ENFORCED
                else (
                    "SKIP",
                    f"{path} answers only via the edge; {SKIP_REASON['edge']}",
                )
            )
        else:
            code, ctype = get(base + path, tp)
            # An SPA fallback answers 200 text/html to anything.
            ok = 200 <= code < 300 and "text/html" not in ctype
            r[item] = (
                "PASS" if ok else "FAIL",
                f"GET {path} -> {code} {ctype.split(';')[0]}",
            )
    if r["ready"][0] == "PASS" and label(labels, "ready") == label(labels, "health"):
        r["ready"] = ("FAIL", "readiness path is the liveness path")

    mpath = label(labels, "metrics")
    if not mpath:
        r["metrics"] = r["scraped"] = ("FAIL", "no label")
    else:
        hosts = {f"{name}:{port}", f"{ip}:{port}"} | {
            f"{a}:{port}"
            for n in networks
            for a in (n.get("Aliases") or []) + (n.get("DNSNames") or [])
        }
        hit = [
            t
            for t in targets
            if t["discoveredLabels"].get("__address__") in hosts
            and t["discoveredLabels"].get("__metrics_path__", "/metrics") == mpath
        ]
        up = [t for t in hit if t["health"] == "up"]
        if up:
            job, instance = up[0]["labels"]["job"], up[0]["labels"].get("instance", "")
            # An up target can still export nothing: an untrusted OpenClaw
            # plugin copy answers 200 with an empty body.
            n = scraped_samples(job, instance)
            scraped = (
                ("PASS", f"job={job} up, {n:.0f} samples")
                if n
                else ("FAIL", f"job={job} up but exports no samples")
            )
        elif hit:
            scraped = ("FAIL", f"job={hit[0]['labels']['job']} {hit[0]['health']}")
        else:
            scraped = ("FAIL", f"no Prometheus target for {name}:{port}{mpath}")
        if label(labels, "metrics.auth") == "token":
            # Only Prometheus holds the credential: its up target is the proof.
            r["metrics"] = (scraped[0], f"{mpath} needs a token; see scraped")
        else:
            code, ctype = get(base + mpath)
            ok = code == 200 and ctype.startswith(PROM_TYPES)
            r["metrics"] = (
                "PASS" if ok else "FAIL",
                f"GET {mpath} -> {code} {ctype.split(';')[0]}",
            )
        r["scraped"] = scraped

    logs = subprocess.run(
        ["docker", "logs", "--tail", str(LOG_LINES), c["Id"]],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    r["logs"] = log_verdict((logs.stdout + logs.stderr).splitlines(), trace_id)

    service = label(labels, "service") or labels.get("com.docker.compose.service", name)
    r["traces"] = ("SKIP", f"service.name={service}; {SKIP_REASON['traces']}")

    published = any(c["HostConfig"].get("PortBindings") or {})
    ingress = label(labels, "ingress")
    if ingress == "internal":
        r["edge"] = (
            ("FAIL", "internal, host port published")
            if published
            else ("PASS", "internal")
        )
    elif ingress == "edge":
        routed = labels.get("traefik.enable") == "true"
        r["edge"] = (
            "PASS" if routed and not published else "FAIL",
            f"traefik.enable={routed}, host port published={published}",
        )
    else:
        r["edge"] = ("FAIL", "no ingress label")

    r["topics"] = ("SKIP", SKIP_REASON["topics"])

    for item, (verdict, detail) in r.items():
        if item not in ENFORCED and verdict == "FAIL":
            r[item] = ("SKIP", f"{SKIP_REASON[item]}: {detail}")
    return r


def scraped_samples(job: str, instance: str) -> float:
    """Samples in the target's last scrape (0 when unknown)."""
    query = urllib.parse.urlencode(
        {"query": f'scrape_samples_scraped{{job="{job}",instance="{instance}"}}'}
    )
    try:
        with urllib.request.urlopen(
            f"{PROMETHEUS_URL}/api/v1/query?{query}", timeout=TIMEOUT
        ) as resp:
            result = json.load(resp)["data"]["result"]
        return float(result[0]["value"][1]) if result else 0.0
    except (OSError, ValueError, KeyError, IndexError):
        return 0.0


def prometheus_targets() -> list[dict]:
    try:
        with urllib.request.urlopen(
            f"{PROMETHEUS_URL}/api/v1/targets", timeout=TIMEOUT
        ) as resp:
            return json.load(resp)["data"]["activeTargets"]
    except (OSError, ValueError, KeyError) as e:
        print(f"! Prometheus targets unreadable ({e}); every `scraped` check fails")
        return []


def run_runtime(projects: list[str], enforce: list[str], report_only: bool) -> int:
    targets = prometheus_targets()
    failed, reported = [], 0
    for c in containers(projects):
        name = c["Name"].lstrip("/")
        labels = c["Config"]["Labels"]
        gating = not enforce or labels["com.docker.compose.project"] in enforce
        declared = label(labels)
        if declared == "none":
            continue
        if declared != "v1":
            print(f"{name:34} UNDECLARED  no {LABEL}=v1|none label")
            if gating:
                failed.append(f"{name}: undeclared")
            else:
                reported += 1
            continue
        for item, (verdict, detail) in check(c, targets).items():
            print(f"{name:34} {item:8} {verdict:5} {detail}")
            if verdict == "FAIL" and gating:
                failed.append(f"{name}: {item} ({detail})")
            elif verdict == "FAIL":
                reported += 1

    census = f"{reported} failure(s) outside the enforced projects, reported only"
    if failed:
        print(f"\nplatform contract: {len(failed)} failure(s); {census}")
        for f in failed:
            print(f"  - {f}")
        return 0 if report_only else 1
    print(f"\nplatform contract: every enforced container passes; {census}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--static",
        metavar="FILE",
        help="check a `docker compose config --format json` document (- = stdin)",
    )
    ap.add_argument(
        "--project",
        action="append",
        default=[],
        help="compose project(s) to check; default all",
    )
    ap.add_argument(
        "--enforce",
        action="append",
        default=[],
        help="project(s) whose failures fail the run; default every project in scope",
    )
    ap.add_argument("--report-only", action="store_true", help="print, never fail")
    args = ap.parse_args()
    if args.static:
        return run_static(args.static)
    return run_runtime(args.project, args.enforce, args.report_only)


if __name__ == "__main__":
    sys.exit(main())
