"""Behaviour of the identity provider's host scripts against stubbed tools.

scripts/deploy-identity.sh (issuer URLs, Serve and sign-in-mode report) with
a stubbed `tailscale` and `curl`; scripts/identity-backup/identity-backup.sh
with a stubbed `docker`.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/deploy-identity.sh"
BACKUP = REPO / "scripts/identity-backup/identity-backup.sh"

TS_STUB = """#!/bin/sh
[ -n "$TS_FAIL" ] && exit 1
if [ "$1" = serve ]; then printf '%s' "$TS_SERVE"; else printf '%s' "$TS_JSON"; fi
"""

CURL_STUB = """#!/bin/sh
[ -z "$SIGNIN_JSON" ] && exit 7
printf '%s' "$SIGNIN_JSON"
"""

NAME = "box.example.ts.net"


def serve_json(ports=(3001, 3002), funnel=(), target="127.0.0.1"):
    return json.dumps(
        {
            "TCP": {str(p): {"HTTPS": True} for p in ports},
            "Web": {
                f"{NAME}:{p}": {"Handlers": {"/": {"Proxy": f"http://{target}:{p}"}}}
                for p in ports
            },
            "AllowFunnel": {f"{NAME}:{p}": True for p in funnel},
        }
    )


def stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir, "tailscale", TS_STUB)
    stub(bin_dir, "curl", CURL_STUB)

    def _run(
        cmd,
        dns=NAME + ".",
        fail=False,
        env_lines=(),
        shell=None,
        serve=None,
        mode="SignIn",
    ):
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        env["TS_JSON"] = json.dumps({"Self": {"DNSName": dns}})
        env["TS_SERVE"] = serve if serve is not None else serve_json()
        env["SIGNIN_JSON"] = json.dumps({"signInMode": mode}) if mode else ""
        if fail:
            env["TS_FAIL"] = "1"
        ef = tmp_path / "env"
        ef.write_text("\n".join(env_lines) + "\n")
        env["ENV_FILE"] = str(ef)
        env.update(shell or {})
        return subprocess.run(
            ["bash", str(SCRIPT), cmd],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    return _run


def parse(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


def test_endpoints_derive_from_the_tailnet_name(run):
    r = run("endpoints")
    assert r.returncode == 0
    assert parse(r.stdout) == {
        "IDENTITY_ENDPOINT": f"https://{NAME}:3001",
        "IDENTITY_ADMIN_ENDPOINT": f"https://{NAME}:3002",
    }


def test_explicit_values_win_env_file_and_shell(run):
    r = run(
        "endpoints",
        env_lines=['IDENTITY_ENDPOINT="https://id.example.org"'],
        shell={"IDENTITY_ADMIN_ENDPOINT": "https://admin.example.org"},
    )
    assert parse(r.stdout) == {
        "IDENTITY_ENDPOINT": "https://id.example.org",
        "IDENTITY_ADMIN_ENDPOINT": "https://admin.example.org",
    }


def test_both_explicit_needs_no_tailscale(run):
    r = run(
        "endpoints",
        fail=True,
        env_lines=[
            "IDENTITY_ENDPOINT=https://id.example.org",
            "IDENTITY_ADMIN_ENDPOINT=https://admin.example.org",
        ],
    )
    assert parse(r.stdout)["IDENTITY_ENDPOINT"] == "https://id.example.org"


@pytest.mark.parametrize("dns", [None, "", "bad name!.ts.net.", "localhost"])
def test_no_derivable_name_prints_nothing(run, dns):
    r = run("endpoints", dns=dns)
    assert r.returncode == 0
    assert r.stdout == ""
    assert "not derivable" in r.stderr


def test_tailscale_down_prints_nothing(run):
    r = run("endpoints", fail=True)
    assert r.returncode == 0
    assert r.stdout == ""


def test_check_passes_when_served_tailnet_only_and_sign_in_only(run):
    r = run("check")
    assert r.returncode == 0, r.stdout
    assert r.stdout.count("tailnet-only") == 2
    assert "SignIn (no self-registration)" in r.stdout


def test_check_flags_funnel_loudly(run):
    r = run("check", serve=serve_json(funnel=(3001,)))
    assert r.returncode == 1
    assert ":3001: FUNNEL IS ON" in r.stdout
    assert ":3002: https -> 127.0.0.1:3002, tailnet-only" in r.stdout


@pytest.mark.parametrize(
    "serve",
    [
        serve_json(ports=(3001,)),
        serve_json(target="127.0.0.1:9", ports=()),
        "{}",
        "not json",
    ],
)
def test_check_flags_a_port_not_published(run, serve):
    r = run("check", serve=serve)
    assert r.returncode == 1
    assert "not published" in r.stdout or "unreadable" in r.stdout


def test_check_flags_a_proxy_to_another_port(run):
    served = json.loads(serve_json())
    served["Web"][f"{NAME}:3002"]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:3000"
    r = run("check", serve=json.dumps(served))
    assert r.returncode == 1
    assert ":3002: not published" in r.stdout


@pytest.mark.parametrize(
    ("mode", "expect"),
    [
        ("SignInAndRegister", "expected SignIn"),
        ("Register", "expected SignIn"),
        (None, "unreadable"),
    ],
)
def test_check_flags_sign_in_mode(run, mode, expect):
    r = run("check", mode=mode)
    assert r.returncode == 1
    assert expect in r.stdout


def test_usage(run):
    r = run("bogus")
    assert r.returncode == 2


# ─── identity-backup.sh ──────────────────────────────────────────────────────

DOCKER_STUB = """#!/bin/sh
case "$1" in
  ps)
    case " $* " in
      *" -aq "*) printf '%s' "$DOCKER_ALL" ;;
      *) printf '%s' "$DOCKER_RUNNING" ;;
    esac ;;
  exec)
    [ -n "$DUMP_FAIL" ] && exit 1
    printf -- '-- dump of %s\\n' "$2" ;;
esac
"""


@pytest.fixture
def backup(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir, "docker", DOCKER_STUB)
    out = tmp_path / "backups" / "identity"

    def _run(all_ids="abc\n", running="abc\n", dump_fail=False, keep=None):
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "IDENTITY_BACKUP_DIR": str(out),
            "DOCKER_ALL": all_ids,
            "DOCKER_RUNNING": running,
        }
        if dump_fail:
            env["DUMP_FAIL"] = "1"
        if keep is not None:
            env["IDENTITY_BACKUP_KEEP"] = str(keep)
        r = subprocess.run(
            ["bash", str(BACKUP)], env=env, capture_output=True, text=True, check=False
        )
        return r, out

    return _run


def test_backup_skips_a_box_without_the_project(backup):
    r, out = backup(all_ids="", running="")
    assert r.returncode == 0
    assert "not deployed here" in r.stdout
    assert not out.exists()


def test_backup_fails_when_postgres_is_stopped(backup):
    r, out = backup(running="")
    assert r.returncode == 1
    assert "not running" in r.stderr


def test_backup_writes_a_private_gzipped_dump(backup):
    r, out = backup()
    assert r.returncode == 0, r.stderr
    (dump,) = out.glob("identity-*.sql.gz")
    assert gzip.decompress(dump.read_bytes()) == b"-- dump of abc\n"
    assert dump.stat().st_mode & 0o777 == 0o600
    assert out.stat().st_mode & 0o777 == 0o700


def test_backup_failed_dump_leaves_no_partial(backup):
    r, out = backup(dump_fail=True)
    assert r.returncode != 0
    assert list(out.iterdir()) == []


def test_backup_keeps_the_newest(backup, tmp_path):
    out = tmp_path / "backups" / "identity"
    out.mkdir(parents=True)
    old = [f"identity-2026010{d}T024500Z.sql.gz" for d in range(1, 6)]
    for name in old:
        (out / name).write_bytes(b"")
    (out / "unrelated.txt").write_text("x")
    r, _ = backup(keep=3)
    assert r.returncode == 0, r.stderr
    kept = sorted(p.name for p in out.glob("identity-*.sql.gz"))
    assert len(kept) == 3
    assert old[3] in kept and old[4] in kept
    assert (out / "unrelated.txt").exists()
