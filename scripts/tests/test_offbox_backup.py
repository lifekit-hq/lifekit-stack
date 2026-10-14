"""offbox_backup.py end to end against fakes: rclone on a local directory,
a passthrough age that records its recipients and checks the identity it is
handed, docker answering for the backstage db and notify-relay, and cat for
zstd. Real tar and SQLite. No network, no real key."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
HERE = REPO / "scripts/offbox-backup"
SCRIPT = HERE / "offbox_backup.py"

spec = importlib.util.spec_from_file_location("offbox_backup", SCRIPT)
ob = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ob)

IDENTITY = "AGE-SECRET-KEY-1FAKEFAKEFAKE"
ONBOX_PUB = "age1onboxfake"
KEY_VALUE = b"not-a-real-key-0123456789"
WEDNESDAY = "2026-10-14T03-30-00Z"
SUNDAY = "2026-10-18T03-30-00Z"

FAKE_RCLONE = r"""
import os, shutil, sys
from pathlib import Path
cmd, *rest = sys.argv[1:]
with open(os.environ["FAKE_RCLONE_LOG"], "a") as f:
    f.write(cmd + " " + rest[-1] + "\n")
path = Path(rest[-1])
if cmd == "rcat":
    if os.environ.get("FAKE_RCLONE_FAIL") and path.name == os.environ["FAKE_RCLONE_FAIL"]:
        sys.stdin.buffer.read()
        sys.exit("upload refused")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(sys.stdin.buffer.read())
elif cmd == "cat":
    sys.stdout.buffer.write(path.read_bytes())
elif cmd == "lsf":
    if not path.is_dir():
        sys.exit(3)
    for p in sorted(path.iterdir()):
        if "--dirs-only" in rest and not p.is_dir():
            continue
        print(p.name + ("/" if p.is_dir() else ""))
elif cmd == "purge":
    shutil.rmtree(path)
else:
    sys.exit(f"fake rclone: {cmd}")
"""

FAKE_AGE = r"""
import os, sys
args = sys.argv[1:]
log = os.environ["FAKE_LOG"]
if args[0] == "-e":
    recips = [args[i + 1] for i, a in enumerate(args) if a == "-r"]
    with open(log, "a") as f:
        f.write("age -e " + " ".join(recips) + "\n")
    sys.stdout.buffer.write(b"FAKEAGE\n" + sys.stdin.buffer.read())
elif args[0] == "-d":
    ident = open(args[args.index("-i") + 1]).read().strip()
    if ident != os.environ["FAKE_IDENTITY"]:
        sys.exit("no identity matched")
    data = sys.stdin.buffer.read()
    if not data.startswith(b"FAKEAGE\n"):
        sys.exit("not an age file")
    sys.stdout.buffer.write(data[len(b"FAKEAGE\n"):])
"""

FAKE_KEYGEN = r"""
import os, sys
if sys.stdin.read().strip() != os.environ["FAKE_IDENTITY"]:
    sys.exit("bad identity")
print(os.environ["FAKE_PUB"])
"""

# FAKE_BACKSTAGE: running | stopped | absent
FAKE_DOCKER = r"""
import json, os, sys
args = sys.argv[1:]
state = os.environ.get("FAKE_BACKSTAGE", "running")
if args[0] == "ps":
    filt = " ".join(args)
    if "service=backstage-db" in filt:
        if state == "running" or (state == "stopped" and "-aq" in args):
            print("bsdb")
    elif "service=notify-relay" in filt:
        print("relay")
    elif "service=postgres" in filt:
        pass
elif args[0] == "exec" and args[1] == "bsdb":
    sys.stdout.buffer.write(b"PGDMP fake custom-format dump")
elif args[0] == "exec" and args[2] == "relay":
    with open(os.environ["FAKE_POSTS"], "a") as f:
        f.write(json.dumps(json.load(sys.stdin)) + "\n")
    print("200 ok")
elif args[0] == "inspect":
    print("postgres:17-alpine")
elif args[0] == "run":
    if sys.stdin.buffer.read().startswith(b"PGDMP"):
        print(";\n; Archive created\n;\n215; 1259 16385 TABLE public t backstage")
    else:
        sys.exit("pg_restore: input file is not a valid archive")
else:
    sys.exit(f"fake docker: {args}")
"""


def fake(tmp: Path, name: str, body: str) -> str:
    p = tmp / "bin" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(f"#!{sys.executable}\n{body}")
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def box(tmp_path):
    state = tmp_path / "state"
    for d in ("agents/kit/agent", "browser/b/user-data/Default/Cache", "logs"):
        (state / d).mkdir(parents=True)
    (state / "openclaw.json").write_text("{}")
    (state / "agents/kit/agent/notes.md").write_text("keep me")
    (state / "browser/b/user-data/Default/Cache/blob").write_text("cache")
    (state / "logs/gateway.log").write_text("log")
    db = sqlite3.connect(state / "agents/kit/agent/openclaw-agent.sqlite")
    db.execute("create table t (x)")
    db.execute("insert into t values (42)")
    db.commit()
    db.close()
    secret = tmp_path / "secret-key"
    secret.mkdir()
    (secret / "key").write_bytes(KEY_VALUE)
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "identity-20261013T024500Z.sql.gz").write_bytes(b"old")
    import gzip

    (ident / "identity-20261014T024500Z.sql.gz").write_bytes(
        gzip.compress(
            b"-- dump\n--\n-- PostgreSQL database cluster dump complete\n--\n"
        )
    )
    env_file = tmp_path / "stack.env"
    env_file.write_text(
        f"OTHER_SECRET=dont-read-me\nOFFBOX_BACKUP_AGE_IDENTITY={IDENTITY}\n"
        f"OPENCLAW_CONFIG_DIR={state}\nOPENCLAW_AUTH_PROFILE_SECRET_DIR={secret}\n"
    )
    (tmp_path / "tmp").mkdir()
    env = {
        **os.environ,
        "TMPDIR": str(tmp_path / "tmp"),
        "OFFBOX_BACKUP_STACK_ENV": str(env_file),
        "OFFBOX_BACKUP_REMOTE": str(tmp_path / "remote"),
        "OFFBOX_BACKUP_IDENTITY_DIR": str(ident),
        "OFFBOX_BACKUP_NOW": WEDNESDAY,
        "RCLONE": fake(tmp_path, "rclone", FAKE_RCLONE),
        "AGE": fake(tmp_path, "age", FAKE_AGE),
        "AGE_KEYGEN": fake(tmp_path, "age-keygen", FAKE_KEYGEN),
        "DOCKER": fake(tmp_path, "docker", FAKE_DOCKER),
        "ZSTD": fake(
            tmp_path,
            "zstd",
            "import shutil, sys\nshutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)",
        ),
        "FAKE_LOG": str(tmp_path / "age.log"),
        "FAKE_RCLONE_LOG": str(tmp_path / "rclone.log"),
        "FAKE_POSTS": str(tmp_path / "posts"),
        "FAKE_IDENTITY": IDENTITY,
        "FAKE_PUB": ONBOX_PUB,
    }

    class Box:
        root = tmp_path
        remote = tmp_path / "remote/lifekit"

        def run(self, cmd, **extra):
            return subprocess.run(
                [sys.executable, str(SCRIPT), cmd],
                env={**env, **extra},
                capture_output=True,
                text=True,
                check=False,
            )

        def posts(self):
            p = tmp_path / "posts"
            return (
                [json.loads(ln) for ln in p.read_text().splitlines()]
                if p.exists()
                else []
            )

    Box.state, Box.secret, Box.env_file = state, secret, env_file
    return Box()


def plain(path: Path) -> bytes:
    data = path.read_bytes()
    assert data.startswith(b"FAKEAGE\n"), f"{path.name} was not encrypted"
    return data[len(b"FAKEAGE\n") :]


def test_backup_writes_one_encrypted_set_with_a_manifest(box):
    res = box.run("backup")
    assert res.returncode == 0, res.stderr
    set_dir = box.remote / "daily" / WEDNESDAY
    manifest = json.loads((set_dir / "MANIFEST.json").read_text())
    names = {o["name"] for o in manifest["objects"]}
    assert names == {
        "identity.sql.gz.age",
        "backstage.dump.age",
        "openclaw-secret-key.tar.age",
        "openclaw-sqlite.tar.zst.age",
        "openclaw-state.tar.zst.age",
    }
    for o in manifest["objects"]:
        data = (set_dir / o["name"]).read_bytes()
        assert o["bytes"] == len(data)
        assert o["sha256"] == hashlib.sha256(data).hexdigest()
    assert manifest["recipients"] == [ob.CAPTAIN_RECIPIENT, ONBOX_PUB]
    log = (box.root / "age.log").read_text().splitlines()
    assert log and all(ln == f"age -e {ob.CAPTAIN_RECIPIENT} {ONBOX_PUB}" for ln in log)

    # the newest identity dump, untouched
    assert (
        plain(set_dir / "identity.sql.gz.age")
        == (box.root / "identity/identity-20261014T024500Z.sql.gz").read_bytes()
    )
    # the state archive keeps real state and leaves out caches, logs, live dbs
    tar_path = box.root / "state.tar"
    tar_path.write_bytes(plain(set_dir / "openclaw-state.tar.zst.age"))
    members = tarfile.open(tar_path).getnames()
    assert "./openclaw.json" in members and "./agents/kit/agent/notes.md" in members
    assert not [m for m in members if "Cache" in m or "logs" in m or ".sqlite" in m]
    # the database travels as a backup-API copy in its own archive
    tar_path.write_bytes(plain(set_dir / "openclaw-sqlite.tar.zst.age"))
    assert (
        "./agents/kit/agent/openclaw-agent.sqlite" in tarfile.open(tar_path).getnames()
    )


def test_no_value_reaches_the_output(box):
    res = box.run("backup")
    out = res.stdout + res.stderr
    for value in (KEY_VALUE.decode(), IDENTITY, "dont-read-me"):
        assert value not in out
    assert not list(box.root.glob("remote/**/*.tar")), "no plaintext in the bucket"


def test_not_configured_is_one_line_and_no_upload(box):
    box.env_file.write_text("")
    env = {"OFFBOX_BACKUP_REMOTE": ""}
    for cmd in ("backup", "verify"):
        res = box.run(cmd, **env)
        assert res.returncode == 0, res.stderr
        lines = res.stdout.strip().splitlines()
        assert len(lines) == 1 and "not configured" in lines[0]
        assert "OFFBOX_BACKUP_R2_SECRET_ACCESS_KEY" in lines[0]
    assert not box.remote.exists()
    assert not box.posts()


def test_verify_needs_the_onbox_key(box):
    assert box.run("backup").returncode == 0
    box.env_file.write_text(
        box.env_file.read_text().replace(f"OFFBOX_BACKUP_AGE_IDENTITY={IDENTITY}\n", "")
    )
    res = box.run("verify")
    assert res.returncode == 0
    assert "not configured (missing OFFBOX_BACKUP_AGE_IDENTITY)" in res.stdout
    assert not box.posts()


def test_without_the_onbox_key_backup_encrypts_to_the_captain_only(box):
    box.env_file.write_text(
        box.env_file.read_text().replace(f"OFFBOX_BACKUP_AGE_IDENTITY={IDENTITY}\n", "")
    )
    res = box.run("backup")
    assert res.returncode == 0, res.stderr
    manifest = json.loads(
        (box.remote / "daily" / WEDNESDAY / "MANIFEST.json").read_text()
    )
    assert manifest["recipients"] == [ob.CAPTAIN_RECIPIENT]


def test_verify_passes_and_says_so(box):
    assert box.run("backup").returncode == 0
    res = box.run("verify", OFFBOX_BACKUP_NOW="2026-10-14T05-00-00Z")
    assert res.returncode == 0, res.stderr
    (env,) = box.posts()
    assert env["level"] == "good" and env["subject"] == "offbox-backup"
    assert "pg_restore --list: 1 entries" in env["detail"]
    assert "same hash as the live key" in env["detail"]
    assert "1 databases pass quick_check" in env["detail"]
    assert KEY_VALUE.decode() not in json.dumps(env) + res.stdout
    assert not list((box.root / "tmp").iterdir()), "scratch left behind"


@pytest.mark.parametrize(
    "damage, reason",
    [
        ("tamper", "does not match the manifest"),
        ("rotate", "openclaw-secret-key"),
        ("stale", "old"),
    ],
)
def test_verify_failure_is_an_act_message(box, damage, reason):
    assert box.run("backup").returncode == 0
    now = "2026-10-14T05-00-00Z"
    if damage == "tamper":
        obj = box.remote / "daily" / WEDNESDAY / "openclaw-state.tar.zst.age"
        obj.write_bytes(obj.read_bytes() + b"x")
    elif damage == "rotate":
        (box.secret / "key").write_bytes(b"another key")
    else:
        now = "2026-10-20T05-00-00Z"
    res = box.run("verify", OFFBOX_BACKUP_NOW=now)
    assert res.returncode == 0, res.stderr
    (env,) = box.posts()
    assert env["level"] == "act" and reason in env["headline"]
    assert not list((box.root / "tmp").iterdir()), "scratch left behind"


def test_sunday_goes_to_weekly(box):
    assert box.run("backup", OFFBOX_BACKUP_NOW=SUNDAY).returncode == 0
    assert (box.remote / "weekly" / SUNDAY / "MANIFEST.json").is_file()
    assert not (box.remote / "daily").exists()


def test_retention_keeps_seven_daily_and_drops_incomplete(box):
    daily = box.remote / "daily"
    for day in range(1, 10):
        d = daily / f"2026-10-0{day}T03-30-00Z"
        d.mkdir(parents=True)
        if day != 9:
            (d / "MANIFEST.json").write_text("{}")
    assert box.run("backup").returncode == 0
    kept = sorted(p.name for p in daily.iterdir())
    assert len(kept) == 7 and kept[-1] == WEDNESDAY
    assert "2026-10-09T03-30-00Z" not in kept  # incomplete
    assert kept[0] == "2026-10-03T03-30-00Z"


def test_every_remote_call_stays_under_the_prefix(box):
    for day in range(1, 10):  # more sets than retention keeps
        d = box.remote / "daily" / f"2026-10-0{day}T03-30-00Z"
        d.mkdir(parents=True)
        (d / "MANIFEST.json").write_text("{}")
    assert box.run("backup").returncode == 0
    failed = box.run(
        "backup",
        OFFBOX_BACKUP_NOW="2026-10-14T04-00-00Z",
        FAKE_RCLONE_FAIL="openclaw-state.tar.zst.age",
    )
    assert failed.returncode == 1  # and its partial set was purged
    assert box.run("verify").returncode == 0
    assert box.posts()[-1]["level"] == "good"
    calls = (box.root / "rclone.log").read_text().splitlines()
    assert {c.split()[0] for c in calls} >= {"rcat", "cat", "lsf", "purge"}
    under = f"{box.root}/remote/lifekit/"
    assert all(c.split(" ", 1)[1].startswith(under) for c in calls), calls
    purged = [c.split(" ", 1)[1].removeprefix(under) for c in calls if c[:5] == "purge"]
    assert purged and all(re.fullmatch(r"(daily|weekly)/[0-9TZ-]+", p) for p in purged)


def test_retention_never_reaches_a_sibling_prefix(box):
    """finance-sentry's objects sit at the bucket root under daily/ and
    weekly/; anything not named like a set under lifekit/ is not ours."""
    remote = box.root / "remote"
    siblings = [
        remote / "daily/finance-sentry-20261001T020000Z.dump.age",
        remote / "weekly/finance-sentry-20260927T020000Z.dump.age",
        remote / "finance-sentry/daily/2026-10-01T03-30-00Z/MANIFEST.json",
        box.remote / "daily/not-a-set/keep.txt",
    ]
    for f in siblings:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("not ours")
    for day in range(1, 10):  # more than retention keeps
        d = box.remote / "daily" / f"2026-10-0{day}T03-30-00Z"
        d.mkdir(parents=True)
        (d / "MANIFEST.json").write_text("{}")
    assert box.run("backup").returncode == 0
    assert all(f.read_text() == "not ours" for f in siblings)
    assert len(list((box.remote / "daily").glob("2026-*"))) == 7


@pytest.mark.parametrize("prefix", ["", "daily", "weekly", "a/b", "..", ".", " ", None])
def test_a_missing_or_unsafe_prefix_refuses_to_run(box, monkeypatch, prefix):
    monkeypatch.setattr(ob, "PREFIX", prefix)
    with pytest.raises(ob.Failed, match="refusing to run"):
        ob.Config({"OFFBOX_BACKUP_REMOTE": str(box.root / "remote")})
    env = {
        "OFFBOX_BACKUP_STACK_ENV": str(box.root / "r2.env"),
        "OFFBOX_BACKUP_NOW": WEDNESDAY,
    }
    (box.root / "r2.env").write_text("".join(f"{k}=x\n" for k in ob.R2_KEYS))
    with pytest.raises(ob.Failed, match="refusing to run"):
        ob.Config(env)


def test_r2_paths_sit_under_bucket_slash_prefix(tmp_path):
    (tmp_path / "r2.env").write_text(
        "".join(f"{k}=bucket-or-value\n" for k in ob.R2_KEYS)
    )
    cfg = ob.Config({"OFFBOX_BACKUP_STACK_ENV": str(tmp_path / "r2.env")})
    assert cfg.base == "offbox:bucket-or-value/lifekit"
    assert ob.scoped(cfg, "offbox:bucket-or-value/lifekit/daily/") is not None
    for bad in (
        "offbox:bucket-or-value",
        "offbox:bucket-or-value/",
        "offbox:bucket-or-value/daily/",
        "offbox:bucket-or-value/lifekit",
        "offbox:bucket-or-value/lifekit/",
        "offbox:bucket-or-value/lifekitx/daily",
        "offbox:bucket-or-value/lifekit/../daily",
        "offbox:bucket-or-value/lifekit//daily",
    ):
        with pytest.raises(ob.Failed):
            ob.scoped(cfg, bad)
    # purge takes a whole set and nothing wider, even under the prefix
    for bad in ("lifekit/daily", "lifekit/daily/", "lifekit/daily/not-a-set"):
        with pytest.raises(ob.Failed):
            ob.rclone(cfg, "purge", f"offbox:bucket-or-value/{bad}")


def test_a_failed_store_leaves_no_partial_set(box):
    res = box.run("backup", FAKE_BACKSTAGE="stopped")
    assert res.returncode == 1
    assert "backstage-db exists but is not running" in res.stderr
    res = box.run("backup", FAKE_RCLONE_FAIL="openclaw-sqlite.tar.zst.age")
    assert res.returncode == 1
    assert not (box.remote / "daily" / WEDNESDAY).exists()
    assert not list((box.root / "tmp").iterdir()), "scratch left behind"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_state_file_fails_the_backup(box):
    locked = box.state / "agents/kit/agent/locked.md"
    locked.write_text("secret")
    locked.chmod(0)
    try:
        res = box.run("backup")
    finally:
        locked.chmod(0o644)
    assert res.returncode == 1
    assert "Permission denied" in res.stderr
    assert not (box.remote / "daily" / WEDNESDAY).exists()
    assert not list((box.root / "tmp").iterdir()), "scratch left behind"


def test_verify_reports_a_decrypt_failure_when_age_exits_at_once(box):
    assert box.run("backup").returncode == 0
    big = box.remote / "daily" / WEDNESDAY / "identity.sql.gz.age"
    manifest_path = big.parent / "MANIFEST.json"
    data = b"FAKEAGE\n" + os.urandom(4 * 1024 * 1024)
    big.write_bytes(data)
    manifest = json.loads(manifest_path.read_text())
    for o in manifest["objects"]:
        if o["name"] == big.name:
            o["bytes"], o["sha256"] = len(data), hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    res = box.run(
        "verify",
        OFFBOX_BACKUP_NOW="2026-10-14T05-00-00Z",
        AGE=fake(box.root, "age-exits", "import sys\nsys.exit('no identity matched')"),
    )
    assert res.returncode == 0, res.stderr
    (env,) = box.posts()
    assert env["level"] == "act"
    assert "age could not decrypt it" in env["headline"]
    assert "no identity matched" in env["headline"]


@pytest.mark.parametrize(
    "name, garbage",
    [
        ("openclaw-secret-key.tar.age", b"not a tar archive"),
        ("identity.sql.gz.age", b"\x1f\x8b\x08\x00truncated"),
    ],
)
def test_verify_reports_a_corrupt_object_as_an_act_message(box, name, garbage):
    assert box.run("backup").returncode == 0
    obj = box.remote / "daily" / WEDNESDAY / name
    data = b"FAKEAGE\n" + garbage
    obj.write_bytes(data)
    manifest_path = obj.parent / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    for o in manifest["objects"]:
        if o["name"] == name:
            o["bytes"], o["sha256"] = len(data), hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    res = box.run("verify", OFFBOX_BACKUP_NOW="2026-10-14T05-00-00Z")
    assert res.returncode == 0, res.stderr
    (env,) = box.posts()
    assert env["level"] == "act" and name in env["headline"]
    assert "could not read it" in env["headline"]
    assert "Traceback" not in res.stderr
    assert not list((box.root / "tmp").iterdir()), "scratch left behind"


def test_an_undeployed_backstage_is_recorded_as_skipped(box):
    assert box.run("backup", FAKE_BACKSTAGE="absent").returncode == 0
    manifest = json.loads(
        (box.remote / "daily" / WEDNESDAY / "MANIFEST.json").read_text()
    )
    assert manifest["skipped"] == [{"store": "backstage", "reason": "not deployed"}]


def test_estimate_uploads_nothing(box):
    res = box.run("estimate")
    assert res.returncode == 0, res.stderr
    assert "openclaw-state" in res.stdout and "GB stored" in res.stdout
    assert not box.remote.exists()


def test_env_file_reader_keeps_only_its_keys(tmp_path):
    f = tmp_path / "env"
    f.write_text(
        "A_B=1\nexport OFFBOX_BACKUP_R2_BUCKET='bkt'\nOFFBOX_BACKUP_R2_ENDPOINT=\"e\"\n"
    )
    assert ob.read_env(str(f)) == {
        "OFFBOX_BACKUP_R2_BUCKET": "bkt",
        "OFFBOX_BACKUP_R2_ENDPOINT": "e",
    }


@pytest.mark.parametrize(
    "rel, out",
    [
        ("tools", True),
        ("agents/kit/tools", False),
        ("browser/x/user-data/Default/Cache", True),
        ("a/b/node_modules", True),
        ("agents/kit/agent/codex-home/logs_2.sqlite", True),
        ("agents/kit/agent/openclaw-agent.sqlite", False),
        ("agents/kit/workspace/tmp-123", True),
    ],
)
def test_exclusions_follow_tar_semantics(rel, out):
    assert ob.excluded(rel, ob.STATE_EXCLUDES) is out


def test_captain_recipient_is_the_first_master_recipient():
    rules = yaml.safe_load((REPO / ".sops.yaml").read_text())["creation_rules"]
    (master,) = [r for r in rules if "lifekit\\.env\\.sops" in r["path_regex"]]
    first = master["age"].replace(" ", "").split(",")[0]
    assert first == ob.CAPTAIN_RECIPIENT


def test_installer_writes_both_units_and_enables_them(tmp_path):
    calls = tmp_path / "calls"
    fake_ctl = tmp_path / "systemctl"
    fake_ctl.write_text(f'#!/usr/bin/env bash\necho "$*" >> {calls}\n')
    fake_ctl.chmod(0o755)
    subprocess.run(
        ["bash", str(HERE / "install-offbox-backup.sh")],
        env={
            **os.environ,
            "INSTALL_ROOT": str(tmp_path),
            "SYSTEMCTL": str(fake_ctl),
            "LIFEKIT_USER": "svc",
            "REPO_DIR": "/srv/repo",
        },
        check=True,
    )
    units = tmp_path / "etc/systemd/system"
    for name in ("lifekit-offbox-backup", "lifekit-offbox-verify"):
        svc = (units / f"{name}.service").read_text()
        assert "User=svc" in svc and "/srv/repo/scripts/offbox-backup/" in svc
        assert "__" not in svc
        assert (units / f"{name}.timer").is_file()
    assert calls.read_text().splitlines() == [
        "daemon-reload",
        "enable --now lifekit-offbox-backup.timer",
        "enable --now lifekit-offbox-verify.timer",
    ]
