"""Platform pieces of the ops door (issue #543): the Grafana route, the compose
service, the host drain with a stub fleet-policy, and the ensure script's
failure path. The receiver's own lifecycle is tested by
compose/ops-door/test (node:test)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
ALERTING = REPO / "compose/observability/grafana/provisioning/alerting"
DRAIN = REPO / "scripts/ops-door/drain-escalations.py"
ENSURE = REPO / "scripts/ensure-ops-door.sh"


def test_every_alert_also_reaches_the_door_and_telegram_is_unchanged():
    doc = yaml.safe_load((ALERTING / "policies.yml.tmpl").read_text().replace("__HEARTBEAT_ROUTES__", ""))
    routes = doc["policies"][0]["routes"]
    door = next(r for r in routes if r["receiver"] == "ops-door")
    assert door["continue"] is True
    assert "object_matchers" not in door  # any severity
    # Telegram: critical pages, everything else stays muted, and both come after the door.
    tg = [r for r in routes if r["receiver"] == "telegram-lifekit"]
    assert [r.get("object_matchers") for r in tg] == [[["severity", "=", "critical"]], None]
    assert routes.index(door) < routes.index(tg[0])


def test_contact_point_posts_to_the_receiver():
    text = (ALERTING / "contact-points.yml.tmpl").read_text()
    points = yaml.safe_load(text)["contactPoints"]
    door = next(p for p in points if p["name"] == "ops-door")
    (receiver,) = door["receivers"]
    assert receiver["type"] == "webhook"
    assert receiver["settings"]["url"] == "http://ops-door:8097/alerts"
    assert receiver["disableResolveMessage"] is False
    assert any(p["name"] == "telegram-lifekit" for p in points)


def test_compose_service_meets_the_policy_and_contract():
    svc = yaml.safe_load((REPO / "compose/docker-compose.yml").read_text())["services"]["ops-door"]
    assert svc["read_only"] is True
    assert svc["deploy"]["resources"]["limits"]["memory"] == "128m"
    labels = svc["labels"]
    assert labels["lifekit.contract"] == "v1"
    assert labels["lifekit.contract.port"] == "8097"
    assert labels["lifekit.contract.ingress"] == "internal"
    assert "ports" not in svc
    # The token is an env reference, never a value.
    assert svc["environment"]["OPS_DOOR_GITHUB_TOKEN"] == "${OPS_DOOR_GITHUB_TOKEN:-}"


def fake_fleet_policy(tmp: Path, exit_code: int = 0) -> tuple[Path, Path]:
    log = tmp / "calls.log"
    bin_ = tmp / "fleet-policy"
    bin_.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> {log}\nexit {exit_code}\n')
    bin_.chmod(bin_.stat().st_mode | stat.S_IXUSR)
    return bin_, log


def spool_call(spool: Path, call_id: str) -> None:
    (spool / f"{call_id}.json").write_text(
        json.dumps({"id": call_id, "title": "Alert still repeating", "question": "What next?", "kind": "call", "tier": 2})
    )


def drain(spool: Path, fp: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "OPS_DOOR_SPOOL_DIR": str(spool), "FLEET_POLICY_BIN": str(fp)}
    return subprocess.run(["python3", str(DRAIN)], env=env, capture_output=True, text=True, check=False)


def test_drain_files_one_decide_call_and_never_a_second(tmp_path):
    fp, log = fake_fleet_policy(tmp_path)
    spool = tmp_path / "spool"
    spool.mkdir()
    spool_call(spool, "ops-repeat-abc123-7")
    assert drain(spool, fp).returncode == 0
    assert drain(spool, fp).returncode == 0  # a second run finds nothing to do
    calls = log.read_text().splitlines()
    assert len(calls) == 1
    assert calls[0].startswith("decide ops-repeat-abc123-7 --kind call --tier 2 --rec another-fix")
    assert "--origin alertmanager" in calls[0]
    assert (spool / "done/ops-repeat-abc123-7.json").exists()
    assert not (spool / "ops-repeat-abc123-7.json").exists()


def test_a_failed_decide_stays_spooled_for_the_next_run(tmp_path):
    fp, log = fake_fleet_policy(tmp_path, exit_code=3)
    spool = tmp_path / "spool"
    spool.mkdir()
    spool_call(spool, "ops-repeat-abc123-7")
    result = drain(spool, fp)
    assert result.returncode == 1
    assert (spool / "ops-repeat-abc123-7.json").exists()
    assert len(log.read_text().splitlines()) == 1


def test_a_request_whose_id_does_not_match_its_file_is_refused(tmp_path):
    fp, log = fake_fleet_policy(tmp_path)
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "ops-repeat-a-1.json").write_text(json.dumps({"id": "other; rm -rf", "title": "t"}))
    assert drain(spool, fp).returncode == 1
    assert not log.exists()


def test_ensure_fails_loudly_when_there_is_no_receiver(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n")
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(ENSURE)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "no running container" in result.stderr


def test_ensure_reports_a_missing_token_without_printing_one(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text('#!/usr/bin/env bash\n[ "$1" = ps ] && { echo abc123; exit 0; }\nexit 1\n')
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "OPS_DOOR_WAIT": "0"}
    result = subprocess.run(["bash", str(ENSURE)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "OPS_DOOR_GITHUB_TOKEN" in result.stderr
