#!/usr/bin/env python3
"""finance_items.py - print finance-sentry's open items for the fleet publisher.

The publisher's FLEET_FINANCE_CMD (README "Queue items"): prints one JSON list of
`{kind, id, title, acceptance, url, since}` on stdout, where `kind` is
`thesis-revision` (a pending revision not yet expired), `recommendation` (a
pending recommendation not yet expired) or `companion-event` (an event Ledger has
not delivered yet: the dispositions finance-sentry's own pending-events query
returns, held-for-digest left out). The list is the raw table read: it does not
apply finance-sentry's reconcile step (SyncFailureReconciler), so a resolved
alert's companion event can still show until Ledger next polls the
pending-events API, and a held-for-digest event appears late, only once a Ledger
poll promotes it. `since` is an epoch, `url` is present only
for a companion event with an app path when --base-url is given (the app's
public base URL; no link is guessed for the others). The publisher stamps the
item envelope (`owner` finance, `class` finance, `store` finance-db).

Only the owner's rows: finance-sentry is multi-user, so every list is filtered by
"UserId" = FLEET_FINANCE_OWNER (the owner's finance-sentry UserId, a GUID; host
config, set by the installer). Unset or empty, nothing is queried: the command
exits nonzero with "finance owner not configured" on stderr.

Read-only: one SELECT through `docker exec` into finance-sentry's postgres
container (found by its compose labels), in a session whose transactions are
read-only (`default_transaction_read_only`), so the database refuses any write. A failed read exits nonzero with a one-line reason on
stderr and prints nothing, so the publisher reports the source unreadable rather
than empty.

  FLEET_FINANCE_OWNER=<owner UserId> python3 finance_items.py [--base-url URL]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from typing import Any

POSTGRES_LABELS = (
    "label=com.docker.compose.project=finance-sentry",
    "label=com.docker.compose.service=postgres",
)
GUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
# GetPendingCompanionEventsQuery's undelivered set, held-for-digest excluded.
UNDELIVERED = ("Pending", "Dispatched", "DeferredQuietHours", "SuppressedByRateLimit")
# One document: the three lists, each row with the columns the items need.
QUERY = """
SELECT json_build_object(
  'revisions', (SELECT coalesce(json_agg(r ORDER BY r.since), '[]') FROM (
    SELECT "Id" AS id, "Kind" AS kind, "Ticker" AS ticker,
           extract(epoch FROM "CreatedAt")::bigint AS since,
           extract(epoch FROM "ExpiresAt")::bigint AS expires
    FROM research.thesis_revisions
    WHERE "UserId" = :'owner' AND "Status" = 'Pending'
      AND ("ExpiresAt" IS NULL OR "ExpiresAt" > now())) r),
  'recommendations', (SELECT coalesce(json_agg(r ORDER BY r.since), '[]') FROM (
    SELECT "Id" AS id, "Action" AS action, "Ticker" AS ticker,
           extract(epoch FROM "CreatedAt")::bigint AS since,
           extract(epoch FROM "ExpiresAt")::bigint AS expires
    FROM research.recommendations
    WHERE "UserId" = :'owner' AND "Status" = 'Pending'
      AND ("ExpiresAt" IS NULL OR "ExpiresAt" > now())) r),
  'events', (SELECT coalesce(json_agg(r ORDER BY r.since), '[]') FROM (
    SELECT "Id" AS id, "Kind" AS kind, "Subject" AS subject, "Summary" AS summary,
           "AppPath" AS app_path, extract(epoch FROM "OccurredAt")::bigint AS since
    FROM companion.companion_events
    WHERE "UserId" = :'owner' AND "Disposition" IN ({undelivered})) r)
);
""".format(undelivered=", ".join(f"'{d}'" for d in UNDELIVERED))
PSQL = (
    'exec psql -X -q -A -t -v ON_ERROR_STOP=1 -v owner="$1"'
    ' -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
)


def postgres_container() -> str:
    filters = [a for f in POSTGRES_LABELS for a in ("--filter", f)]
    found = subprocess.run(
        ["docker", "ps", "-q", *filters],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.split()
    if len(found) != 1:
        raise RuntimeError(f"{len(found)} finance-sentry postgres containers running")
    return found[0]


def read_rows(container: str, owner: str) -> dict[str, Any]:
    out = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            "PGOPTIONS=-c default_transaction_read_only=on",
            container,
            "sh",
            "-c",
            PSQL,
            "sh",
            owner,
        ],
        input=QUERY,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    doc = json.loads(out)
    if not isinstance(doc, dict):
        raise ValueError("query result is not an object")
    return doc


def day(epoch: Any) -> str | None:
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        return None
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d")


def app_url(base: str | None, path: Any) -> str | None:
    """The app link, like finance-sentry's AppUrl.For: an http(s) base and a path
    inside the app, else none."""
    if not base or not base.startswith(("http://", "https://")):
        return None
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return None
    return base.rstrip("/") + path


def by(expires: Any) -> str:
    return f" by {d}" if (d := day(expires)) else ""


def items(doc: dict[str, Any], base_url: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in doc.get("revisions") or []:
        out.append(
            {
                "kind": "thesis-revision",
                "id": r["id"],
                "title": f"{r.get('ticker') or 'Thesis'}: {r.get('kind') or 'revise'}"
                " thesis revision to decide",
                "acceptance": f"The revision is confirmed or rejected{by(r.get('expires'))}",
                "since": r.get("since"),
            }
        )
    for r in doc.get("recommendations") or []:
        out.append(
            {
                "kind": "recommendation",
                "id": r["id"],
                "title": f"{r.get('action') or 'Act on'} {r.get('ticker') or ''}".strip()
                + ": recommendation to decide",
                "acceptance": "The recommendation is accepted or declined"
                + by(r.get("expires")),
                "since": r.get("since"),
            }
        )
    for r in doc.get("events") or []:
        item = {
            "kind": "companion-event",
            "id": r["id"],
            "title": r.get("summary") or r.get("subject") or r.get("kind"),
            "acceptance": "Ledger delivers the event and acknowledges it",
            "since": r.get("since"),
        }
        if url := app_url(base_url, r.get("app_path")):
            item["url"] = url
        out.append(item)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", help="the finance app's public base URL")
    args = ap.parse_args(argv)
    owner = os.environ.get("FLEET_FINANCE_OWNER") or ""
    if not owner:
        print("finance owner not configured", file=sys.stderr)
        return 1
    if not GUID.fullmatch(owner):
        print("finance owner is not a GUID", file=sys.stderr)
        return 1
    try:
        doc = read_rows(postgres_container(), owner)
        listed = items(doc, args.base_url)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"finance items unreadable: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    json.dump(listed, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
