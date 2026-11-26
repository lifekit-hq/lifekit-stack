"""Behaviour of scripts/fleet-publisher/scheduled.py: the summary's `scheduled` list."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts/fleet-publisher/scheduled.py"
SWEEP = REPO / "scripts/check-cron-declarations.sh"

# One timer, one cron row and one declared row that is not live.
MANIFEST = {
    "schema": "fleet-scheduled-manifest.v1",
    "timers": [
        {
            "unit": "media-prune.timer",
            "service": "media-prune.service",
            "schedule": "OnCalendar=*-*-* 04:30:00",
            "script": "scripts/media-prune/media-prune.timer",
        }
    ],
    "cron": [
        {"name": "ledger-pulse", "script": "scripts/ensure-finance-pulse.sh"},
        {"name": "planning-reminder", "script": "scripts/ensure-planning-reminder.sh"},
    ],
}
LIST_TIMERS = [
    {
        "next": 1_800_003_600_000_000,
        "left": 3_600_000_000,
        "last": 1_799_917_200_000_000,
        "passed": 82_800_000_000,
        "unit": "media-prune.timer",
        "activates": "media-prune.service",
    },
    # A host timer the repo does not declare is not listed.
    {"next": 1, "last": 1, "unit": "apt-daily.timer", "activates": "apt-daily.service"},
]
CRON_LIST = {
    "jobs": [
        {
            "id": "7f1c",
            "agentId": "finance",
            "name": "ledger-pulse",
            "enabled": True,
            "schedule": {"kind": "cron", "expr": "20 9,15,21 * * *", "tz": "Europe/Dublin"},
            "delivery": {"mode": "announce", "channel": "telegram", "to": "4223"},
            "payload": {"kind": "agentTurn", "message": "the checklist"},
            "state": {
                "nextRunAtMs": 1_800_001_200_000,
                "lastRunAtMs": 1_799_979_600_000,
                "lastStatus": "ok",
                "lastRunStatus": "ok",
            },
        }
    ],
    "total": 1,
}


@pytest.fixture
def box(tmp_path):
    """A fake systemctl and the manifest, the way the installed helper sees them."""
    systemctl = tmp_path / "systemctl"
    (tmp_path / "list-timers.json").write_text(json.dumps(LIST_TIMERS))
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        f'case "$1" in\n'
        f'  list-timers) cat {tmp_path}/list-timers.json ;;\n'
        "  show) printf 'Id=media-prune.service\\nResult=exit-code\\nActiveState=failed\\n\\n"
        "Id=apt-daily.service\\nResult=success\\nActiveState=inactive\\n' ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n"
    )
    systemctl.chmod(0o755)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(MANIFEST))
    return tmp_path


def helper(*args, **env):
    return subprocess.run(
        ["python3", str(HELPER), *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def gateway_cache(box, intents=None, cron=CRON_LIST):
    """Run the gateway read against stubbed CLI output and return the cache path."""
    (box / "cron.out").write_text('{"level":"info","message":"noise"}\n' + json.dumps(cron))
    (box / "intents.out").write_text(json.dumps(intents or []))
    r = helper(
        "gateway",
        FLEET_CRON_CMD=f"cat {box / 'cron.out'}",
        FLEET_INTENTS_CMD=f"cat {box / 'intents.out'}",
    )
    assert r.returncode == 0, r.stderr
    cache = box / "gateway.json"
    cache.write_text(r.stdout)
    return cache


def build(box, cache, systemctl=None):
    r = helper(
        "build",
        "--manifest",
        str(box / "manifest.json"),
        "--gateway",
        str(cache),
        FLEET_SYSTEMCTL=str(box / "systemctl") if systemctl is None else systemctl,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_one_timer_one_cron_row_and_one_missing_row(box):
    entries = build(box, gateway_cache(box))
    assert entries == [
        {
            "kind": "timer",
            "name": "media-prune.timer",
            "owner": "media-prune.service",
            "owner_kind": "unit",
            "schedule": "OnCalendar=*-*-* 04:30:00",
            "next_run_epoch": 1_800_003_600,
            "last_run_epoch": 1_799_917_200,
            "last_result": "exit-code",
            "script": "scripts/media-prune/media-prune.timer",
            "state": "live",
        },
        {
            "kind": "cron",
            "name": "ledger-pulse",
            "owner": "finance",
            "owner_kind": "agent",
            "schedule": "cron 20 9,15,21 * * * (Europe/Dublin)",
            "next_run_epoch": 1_800_001_200,
            "last_run_epoch": 1_799_979_600,
            "last_result": "ok",
            "script": "scripts/ensure-finance-pulse.sh",
            "state": "live",
        },
        {
            "kind": "cron",
            "name": "planning-reminder",
            "owner": None,
            "owner_kind": "agent",
            "schedule": None,
            "next_run_epoch": None,
            "last_run_epoch": None,
            "last_result": None,
            "script": "scripts/ensure-planning-reminder.sh",
            "state": "missing",
        },
    ]


def test_the_gateway_cache_keeps_no_payload_delivery_target_or_id(box):
    text = gateway_cache(box).read_text()
    for private in ("4223", "the checklist", "7f1c", "telegram"):
        assert private not in text


def test_standing_intents_of_kit_and_ledger_are_listed_while_active(box):
    intents = [
        {
            "id": "si-1",
            "agent": "kit",
            "description": "Remind me to confirm the rollback owner",
            "trigger_keywords": json.dumps([["launch", "checklist"]]),
            "status": "armed",
            "expires_at": 1_807_776_000_000,
            "last_fired_at": None,
            "created_at": 1_799_000_000_000,
        },
        {
            "id": "si-2",
            "agent": "finance",
            "description": "done one",
            "trigger_keywords": "[]",
            "status": "done",
            "expires_at": 1_807_776_000_000,
        },
    ]
    entries = build(box, gateway_cache(box, intents=intents))
    listed = [e for e in entries if e["kind"] == "intent"]
    assert listed == [
        {
            "kind": "intent",
            "name": "Remind me to confirm the rollback owner",
            "owner": "kit",
            "owner_kind": "agent",
            "schedule": "when: launch checklist",
            "next_run_epoch": None,
            "last_run_epoch": None,
            "last_result": "armed",
            "script": None,
            "state": "live",
            "expires_epoch": 1_807_776_000,
            "id": "si-1",
        }
    ]


def test_an_undeclared_or_disabled_live_row_is_listed_without_a_script(box):
    cron = {
        "jobs": [
            {**CRON_LIST["jobs"][0]},
            {
                "name": "sunday-review",
                "agentId": "kit",
                "enabled": False,
                "schedule": {"kind": "every", "everyMs": 7_200_000},
                "state": {},
            },
        ]
    }
    entries = build(box, gateway_cache(box, cron=cron))
    review = [e for e in entries if e["name"] == "sunday-review"]
    assert review[0]["script"] is None
    assert review[0]["state"] == "disabled"
    assert review[0]["schedule"] == "every 2h"


def test_a_side_that_could_not_be_read_is_unknown_not_missing(box):
    entries = build(box, box / "no-cache.json", systemctl="")
    assert {(e["name"], e["state"]) for e in entries} == {
        ("media-prune.timer", "unknown"),
        ("ledger-pulse", "unknown"),
        ("planning-reminder", "unknown"),
    }


def test_a_declared_timer_systemd_does_not_list_is_missing(box):
    (box / "list-timers.json").write_text("[]")
    entries = build(box, gateway_cache(box))
    assert entries[0]["name"] == "media-prune.timer"
    assert entries[0]["state"] == "missing"


def test_a_failed_gateway_read_is_an_error_and_leaves_the_part_out(box):
    r = helper("gateway", FLEET_CRON_CMD="false", FLEET_INTENTS_CMD="")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert "cron" not in doc and "intents" not in doc
    assert doc["errors"] and doc["errors"][0].startswith("cron:")


def test_the_manifest_lists_every_repo_timer_and_declared_row():
    r = helper("manifest", "--repo", str(REPO))
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert sorted(t["unit"] for t in doc["timers"]) == sorted(
        p.name for p in (REPO / "scripts").rglob("*.timer")
    )
    prune = next(t for t in doc["timers"] if t["unit"] == "media-prune.timer")
    assert prune["service"] == "media-prune.service"
    assert prune["schedule"].startswith("OnCalendar=")
    assert prune["script"] == "scripts/media-prune/media-prune.timer"
    names = {c["name"]: c["script"] for c in doc["cron"]}
    assert names["ledger-pulse"] == "scripts/ensure-finance-pulse.sh"
    assert names["needs-you-mail-reader"] == "scripts/ensure-needs-you-digest.sh"


def test_the_sweep_lists_declared_rows_without_reaching_a_gateway(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 99\n")
    docker.chmod(0o755)
    r = subprocess.run(
        ["bash", str(SWEEP), "--list"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    rows = [line.split("\t") for line in r.stdout.splitlines()]
    assert ["today-brief", "scripts/ensure-today-brief-fleet.sh"] in rows
    assert ["needs-you-digest", "scripts/ensure-needs-you-digest.sh"] in rows
    # ensure-kit-cron-models.sh is a policy over Kit's rows: it names none.
    assert all(script != "scripts/ensure-kit-cron-models.sh" for _, script in rows)
