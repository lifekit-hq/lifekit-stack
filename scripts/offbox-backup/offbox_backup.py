#!/usr/bin/env python3
"""Nightly encrypted off-box backup to Cloudflare R2, and its monthly restore check.

Four stores have no copy off this disk (docs/runbook.md "Off-box backup (R2)"):
the identity provider's nightly dumps, the backstage database, the OpenClaw
auth-profile secret key, and the OpenClaw runtime state. One run streams each
one through `age` to the captain's key (plus the on-box restore-check key when
it is configured) and `rclone rcat`s the ciphertext to R2 as one set:

  lifekit/daily/<UTC stamp>/    (Sunday's run goes to lifekit/weekly/)
    identity.sql.gz.age          newest dump the identity-backup timer wrote
    backstage.dump.age           pg_dump -Fc of compose project backstage's db
    openclaw-secret-key.tar.age  tar of the secret-key directory
    openclaw-sqlite.tar.zst.age  consistent copies of the state's *.sqlite
    openclaw-state.tar.zst.age   the state dir minus caches, logs, archives
    MANIFEST.json                written last: a set without one is incomplete

Plaintext never lands on disk except the SQLite copies, taken with SQLite's
backup API into a 0700 scratch directory under the unit's private /tmp and
removed before the run ends. The secret key goes tar -> age through a pipe.
No value read from the env file is ever printed.

  backup    one set, then retention: newest 7 daily and 4 weekly complete
            sets are kept, older and incomplete ones are deleted. Any failed
            store deletes the set being written and exits 1, which fails the
            oneshot; the unit gauge alerts on it.
  verify    fetch the newest complete set, check every object against the
            manifest's sha256, decrypt it with the on-box key, and prove it
            restores: pg_restore --list on the backstage dump, the dump-complete
            footer on the identity dump, a tar listing of the state, PRAGMA
            quick_check on every SQLite copy, and the secret key compared with
            the live one by hash only. The verdict goes to the owner through
            notify-relay (`good` or `act`); exit 1 only when it cannot be sent.
  estimate  produce every store without encrypting or uploading and print the
            compressed sizes and the retention's expected storage and cost.

Until the R2 values are in the env file, backup and verify print one
"not configured" line and exit 0: no partial upload, no alert. verify also
needs OFFBOX_BACKUP_AGE_IDENTITY.

Env overrides, for tests and rehearsals:
  OFFBOX_BACKUP_STACK_ENV  the env file (default /srv/lifekit-secrets/stack.env)
  OFFBOX_BACKUP_REMOTE     an rclone path used instead of R2 (no R2 values needed)
  OFFBOX_BACKUP_IDENTITY_DIR, OFFBOX_BACKUP_NOW (UTC, %Y-%m-%dT%H-%M-%SZ)
  RCLONE, AGE, AGE_KEYGEN, DOCKER, ZSTD, TAR  binaries
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

# The captain's age key: the first master recipient in .sops.yaml (a test
# holds the two together). Always a recipient; it is the off-box restore key.
CAPTAIN_RECIPIENT = "age146hmku2m4667lmx63ysge7szjlyc48a6x7r5zg8ur56wxlq4t3pqsyv390"
R2_KEYS = (
    "OFFBOX_BACKUP_R2_ENDPOINT",
    "OFFBOX_BACKUP_R2_BUCKET",
    "OFFBOX_BACKUP_R2_ACCESS_KEY_ID",
    "OFFBOX_BACKUP_R2_SECRET_ACCESS_KEY",
)
IDENTITY_KEY = "OFFBOX_BACKUP_AGE_IDENTITY"
SETTING_KEYS = ("OPENCLAW_CONFIG_DIR", "OPENCLAW_AUTH_PROFILE_SECRET_DIR")
STACK_ENV = "/srv/lifekit-secrets/stack.env"
# The bucket is finance-sentry's, shared with the same key pair (captain's
# call, 2026-10-14). finance-sentry writes, lists and prunes only daily/ and
# weekly/ at the bucket root; this job only <bucket>/lifekit/. Every remote
# path goes through scoped(), and the prefix is checked before any call.
PREFIX = "lifekit"
PREFIX_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
KEEP = {"daily": 7, "weekly": 4}
MAX_AGE = dt.timedelta(hours=48)
STAMP = "%Y-%m-%dT%H-%M-%SZ"
MANIFEST = "MANIFEST.json"
SET_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d-\d\d-\d\dZ")
CHUNK = 1 << 20
# R2 storage, USD per GB-month past the 10 GB-month free tier; egress is free.
R2_FREE_GB = 10
R2_USD_PER_GB = 0.015

RCLONE = os.environ.get("RCLONE", "rclone")
AGE = os.environ.get("AGE", "age")
AGE_KEYGEN = os.environ.get("AGE_KEYGEN", "age-keygen")
DOCKER = os.environ.get("DOCKER", "docker")
ZSTD = os.environ.get("ZSTD", "zstd")
TAR = os.environ.get("TAR", "tar")

# Left out of the state archive, measured on the reference box (2026-10-14:
# 14 GB on disk, 0.81 GB kept, 0.28 GB zstd-compressed). Everything here is a
# cache, a log, a download, a build output, or an archive of sessions already
# migrated into the SQLite stores. tar --exclude semantics: `*` crosses `/`,
# and a pattern without a leading `./` matches at any depth.
STATE_EXCLUDES = (
    "./browser/*/user-data/OptGuideOnDeviceModel",
    "./browser/*/user-data/OptGuideOnDeviceClassifierModel",
    "./browser/*/user-data/component_crx_cache",
    "./browser/*/user-data/extensions_crx_cache",
    "./browser/*/user-data/Singleton*",
    "*/Cache",
    "*/Code Cache",
    "*/GPUCache",
    "*/GPUPersistentCache",
    "*/DawnGraphiteCache",
    "*/DawnWebGPUCache",
    "*/GraphiteDawnCache",
    "./tools",
    "./npm",
    "./cache",
    "./tmp",
    "./logs",
    "./session-sqlite-migration-runs",
    "./agents/*/session-sqlite-import-archive",
    "./agents/*/tmp",
    "./agents/*/workspace/tmp-*",
    "./agents/*/agent/codex-home/logs_*",
    "./agents/*/agent/codex-home/cache",
    "./agents/*/agent/codex-home/tmp",
    "node_modules",
    "*/bin/Debug",
    "*/bin/Release",
    "*/obj/Debug",
    "*/obj/Release",
    "*.pre-startup-migration-*.bak",
)
# The live databases: copied with the backup API into their own archive, so
# the state tar never holds a torn page.
SQLITE_EXCLUDES = (
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.sqlite-journal",
)
TAR_OK = (0, 1)  # GNU tar: 1 = a file changed or vanished while it was read


class Failed(Exception):
    """A store or a check failed; the message names which, never a value."""


def log(msg: str) -> None:
    print(f"offbox-backup: {msg}", flush=True)


# ─── config ─────────────────────────────────────────────────────────────────


def read_env(path: str) -> dict[str, str]:
    """Only the keys this job uses; the rest of the file is never kept."""
    wanted = {*R2_KEYS, IDENTITY_KEY, *SETTING_KEYS}
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        key = key.removeprefix("export ").strip()
        if not sep or key not in wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


class Config:
    def __init__(self, environ: dict[str, str] | None = None):
        environ = os.environ if environ is None else environ
        env = read_env(environ.get("OFFBOX_BACKUP_STACK_ENV", STACK_ENV))
        self.state_dir = Path(env.get("OPENCLAW_CONFIG_DIR") or "/srv/openclaw/config")
        self.secret_dir = Path(
            env.get("OPENCLAW_AUTH_PROFILE_SECRET_DIR") or "/srv/openclaw/secret-key"
        )
        self.identity_dir = Path(
            environ.get("OFFBOX_BACKUP_IDENTITY_DIR", "/srv/openclaw/backups/identity")
        )
        self.identity = env.get(IDENTITY_KEY, "")
        self.now = (
            dt.datetime.strptime(environ["OFFBOX_BACKUP_NOW"], STAMP).replace(
                tzinfo=dt.timezone.utc
            )
            if environ.get("OFFBOX_BACKUP_NOW")
            else dt.datetime.now(dt.timezone.utc)
        )
        self.rclone_env = dict(os.environ)
        prefix = check_prefix(PREFIX)
        local = environ.get("OFFBOX_BACKUP_REMOTE", "")
        if local:
            self.missing: list[str] = []
            self.base = f"{local.rstrip('/')}/{prefix}"
            return
        self.missing = [k for k in R2_KEYS if not env.get(k)]
        if self.missing:
            return
        bucket = env["OFFBOX_BACKUP_R2_BUCKET"].strip("/")
        if not bucket or "/" in bucket:
            raise Failed("OFFBOX_BACKUP_R2_BUCKET is not a bucket name")
        self.base = f"offbox:{bucket}/{prefix}"
        # rclone's env-only remote: nothing written to a config file, and the
        # keys reach rclone's environment only, never its command line.
        self.rclone_env.update(
            {
                "RCLONE_CONFIG_OFFBOX_TYPE": "s3",
                "RCLONE_CONFIG_OFFBOX_PROVIDER": "Cloudflare",
                "RCLONE_CONFIG_OFFBOX_ENDPOINT": env["OFFBOX_BACKUP_R2_ENDPOINT"],
                "RCLONE_CONFIG_OFFBOX_ACCESS_KEY_ID": env[
                    "OFFBOX_BACKUP_R2_ACCESS_KEY_ID"
                ],
                "RCLONE_CONFIG_OFFBOX_SECRET_ACCESS_KEY": env[
                    "OFFBOX_BACKUP_R2_SECRET_ACCESS_KEY"
                ],
                "RCLONE_CONFIG_OFFBOX_REGION": "auto",
                "RCLONE_CONFIG_OFFBOX_ACL": "private",
                # a bucket-scoped token may not list or create buckets
                "RCLONE_CONFIG_OFFBOX_NO_CHECK_BUCKET": "true",
                "RCLONE_CONFIG_OFFBOX_CHUNK_SIZE": "16M",
            }
        )

    def recipients(self) -> list[str]:
        recips = [CAPTAIN_RECIPIENT]
        if self.identity:
            res = subprocess.run(
                [AGE_KEYGEN, "-y"],
                input=self.identity + "\n",
                capture_output=True,
                text=True,
                check=False,
            )
            pub = res.stdout.strip()
            if res.returncode != 0 or not pub.startswith("age1"):
                raise Failed(f"{IDENTITY_KEY} is not an age identity")
            if pub not in recips:
                recips.append(pub)
        return recips


# ─── rclone ─────────────────────────────────────────────────────────────────


def check_prefix(prefix: str) -> str:
    """One plain path segment, and not finance-sentry's daily/ or weekly/: an
    empty or nested prefix would put the list and the prune at the root of
    the shared bucket."""
    if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix) or prefix in KEEP:
        raise Failed(
            f"refusing to run: the bucket prefix {prefix!r} is not one segment"
        )
    return prefix


def scoped(cfg: Config, path: str) -> str:
    """The one gate every remote path passes: strictly under <base>/, no `..`
    and no empty segment, so no call can name the bucket root or a sibling."""
    check_prefix(cfg.base.rsplit("/", 1)[-1])
    rest = path.removeprefix(cfg.base + "/")
    parts = rest.rstrip("/").split("/")
    if rest == path or not rest or any(p in ("", ".", "..") for p in parts):
        raise Failed(f"refusing a remote path outside {PREFIX}/")
    return path


def set_path(cfg: Config, kind: str, name: str) -> str:
    if kind not in KEEP or not SET_RE.fullmatch(name):
        raise Failed(f"refusing set {kind}/{name}: not <daily|weekly>/<stamp>")
    return scoped(cfg, f"{cfg.base}/{kind}/{name}")


def rclone(cfg: Config, *args: str, check: bool = True) -> str:
    """args end with the remote path; purge takes only a whole set's path."""
    scoped(cfg, args[-1])
    if args[0] == "purge":
        kind, _, name = args[-1].removeprefix(cfg.base + "/").partition("/")
        set_path(cfg, kind, name)
    res = subprocess.run(
        [RCLONE, *args],
        env=cfg.rclone_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and res.returncode != 0:
        raise Failed(f"rclone {args[0]} failed: {res.stderr.strip()[-400:]}")
    return res.stdout


def list_sets(cfg: Config, kind: str) -> list[tuple[str, bool]]:
    """-> [(set name, complete)], oldest first."""
    out = rclone(cfg, "lsf", "--dirs-only", f"{cfg.base}/{kind}/", check=False)
    sets = []
    # only directories named like a set; anything else is not ours to prune
    for name in sorted(
        n.rstrip("/") for n in out.split() if SET_RE.fullmatch(n.rstrip("/"))
    ):
        files = rclone(cfg, "lsf", f"{set_path(cfg, kind, name)}/", check=False).split()
        sets.append((name, MANIFEST in files))
    return sets


# ─── pipelines ──────────────────────────────────────────────────────────────


def spawn(cmds: list[list[str]], stdin) -> list[subprocess.Popen]:
    """Chain cmds stdout->stdin; the last one's stdout is a pipe."""
    procs: list[subprocess.Popen] = []
    for cmd in cmds:
        p = subprocess.Popen(
            cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if procs:
            procs[-1].stdout.close()  # only the next stage holds it now
        procs.append(p)
        stdin = p.stdout
    return procs


def finish(procs: list[subprocess.Popen], ok: dict[int, tuple[int, ...]]) -> None:
    """Wait for every stage; raise naming the first one that failed."""
    errors = []
    for i, p in enumerate(procs):
        err = p.stderr.read().decode(errors="replace") if p.stderr else ""
        rc = p.wait()
        if rc not in ok.get(i, (0,)):
            errors.append(f"{Path(p.args[0]).name} exit {rc}: {err.strip()[-300:]}")
    if errors:
        raise Failed("; ".join(errors))


def pump(src, sink, digest) -> int:
    """Copy src to sink (a file object or None), hashing; -> bytes."""
    n = 0
    while chunk := src.read(CHUNK):
        digest.update(chunk)
        n += len(chunk)
        if sink is not None:
            sink.write(chunk)
    return n


def produce(source: dict) -> tuple[list[list[str]], object]:
    """A store's producer stages and the first stage's stdin."""
    kind = source["kind"]
    if kind == "file":
        return [], open(source["path"], "rb")
    if kind == "cmd":
        return [source["cmd"]], subprocess.DEVNULL
    if kind == "tar":
        tar = [
            TAR,
            "-c",
            "--warning=no-file-changed",
            "--warning=no-file-removed",
            *(f"--exclude={p}" for p in source.get("exclude", ())),
            "-C",
            str(source["dir"]),
            ".",
        ]
        if source.get("zstd"):
            return [tar, [ZSTD, "-q", "-T2", "-6"]], subprocess.DEVNULL
        return [tar], subprocess.DEVNULL
    raise ValueError(kind)


def kill(procs) -> None:
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()


def run_store(source: dict, tail: list[list[str]], sink) -> tuple[int, str]:
    """Run the producer stages + tail (age, or nothing) and copy the output to
    sink (None discards it), hashing as it goes. -> (bytes, sha256)."""
    stages, stdin = produce(source)
    stages = stages + tail
    digest = hashlib.sha256()
    if not stages:  # a plain file with nothing to pipe through
        with stdin:
            return pump(stdin, sink, digest), digest.hexdigest()
    procs = spawn(stages, stdin)
    if stdin is not subprocess.DEVNULL:
        stdin.close()
    try:
        n = pump(procs[-1].stdout, sink, digest)
    except BaseException:
        kill(procs)
        raise
    finally:
        procs[-1].stdout.close()
    finish(procs, {0: TAR_OK} if source["kind"] == "tar" else {})
    return n, digest.hexdigest()


# ─── the stores ─────────────────────────────────────────────────────────────


def find_container(project: str, service: str, running: bool = True) -> str:
    args = [DOCKER, "ps", "-q"] if running else [DOCKER, "ps", "-aq"]
    res = subprocess.run(
        [
            *args,
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise Failed(f"docker ps failed: {res.stderr.strip()[-300:]}")
    ids = res.stdout.split()
    return ids[0] if ids else ""


def excluded(rel: str, patterns) -> bool:
    """tar --exclude matching for a path relative to the archive root."""
    path = "./" + rel
    cands = [path] + [path[i + 1 :] for i, c in enumerate(path) if c == "/"]
    return any(fnmatch.fnmatchcase(c, p) for p in patterns for c in cands)


def snapshot_sqlite(state_dir: Path, dest: Path) -> int:
    """Copy every *.sqlite outside the excluded trees with the backup API."""
    n = 0
    for root, dirs, files in os.walk(state_dir):
        rel_root = os.path.relpath(root, state_dir)
        rel_root = "" if rel_root == "." else rel_root + "/"
        dirs[:] = [d for d in dirs if not excluded(rel_root + d, STATE_EXCLUDES)]
        for f in files:
            rel = rel_root + f
            if not f.endswith(".sqlite") or excluded(rel, STATE_EXCLUDES):
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            src = sqlite3.connect(
                f"file:{state_dir / rel}?mode=ro", uri=True, timeout=30
            )
            try:
                dst = sqlite3.connect(out)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            except sqlite3.Error as exc:
                raise Failed(f"sqlite copy of {rel} failed: {exc}") from exc
            finally:
                src.close()
            n += 1
    return n


def stores(cfg: Config, scratch: Path) -> tuple[list[dict], list[dict]]:
    """-> (stores to write, stores skipped with the reason)."""
    out, skipped = [], []

    dumps = sorted(cfg.identity_dir.glob("identity-*.sql.gz"))
    if dumps:
        out.append(
            {
                "name": "identity.sql.gz.age",
                "store": "identity",
                "kind": "file",
                "path": dumps[-1],
                "source": dumps[-1].name,
            }
        )
    elif find_container("identity", "postgres", running=False):
        raise Failed(f"identity is deployed but {cfg.identity_dir} has no dump")
    else:
        skipped.append({"store": "identity", "reason": "not deployed"})

    ctr = find_container("backstage", "backstage-db")
    if ctr:
        out.append(
            {
                "name": "backstage.dump.age",
                "store": "backstage",
                "kind": "cmd",
                "cmd": [
                    DOCKER,
                    "exec",
                    ctr,
                    "sh",
                    "-c",
                    'exec pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"',
                ],
                "source": "pg_dump -Fc (compose project backstage, backstage-db)",
            }
        )
    elif find_container("backstage", "backstage-db", running=False):
        raise Failed("backstage-db exists but is not running; no dump taken")
    else:
        skipped.append({"store": "backstage", "reason": "not deployed"})

    if not cfg.secret_dir.is_dir():
        raise Failed(f"{cfg.secret_dir} is missing")
    out.append(
        {
            "name": "openclaw-secret-key.tar.age",
            "store": "openclaw-secret-key",
            "kind": "tar",
            "dir": cfg.secret_dir,
            "source": str(cfg.secret_dir),
        }
    )

    if not (cfg.state_dir / "openclaw.json").is_file():
        raise Failed(f"{cfg.state_dir} has no openclaw.json")
    snap = scratch / "sqlite"
    snap.mkdir(mode=0o700)
    count = snapshot_sqlite(cfg.state_dir, snap)
    out.append(
        {
            "name": "openclaw-sqlite.tar.zst.age",
            "store": "openclaw-sqlite",
            "kind": "tar",
            "dir": snap,
            "zstd": True,
            "source": f"{count} SQLite databases under {cfg.state_dir}",
        }
    )
    out.append(
        {
            "name": "openclaw-state.tar.zst.age",
            "store": "openclaw-state",
            "kind": "tar",
            "dir": cfg.state_dir,
            "zstd": True,
            "exclude": STATE_EXCLUDES + SQLITE_EXCLUDES,
            "source": f"{cfg.state_dir} minus the documented exclusions",
        }
    )
    return out, skipped


def scratch_dir() -> Path:
    # The unit's PrivateTmp: on disk (tmp-scratch-policy.sh), private to it.
    return Path(tempfile.mkdtemp(prefix="offbox-"))


# ─── backup ─────────────────────────────────────────────────────────────────


def upload(cfg: Config, source: dict, recipients, path: str) -> dict:
    age = [AGE, "-e"] + [a for r in recipients for a in ("-r", r)]
    rc = subprocess.Popen(
        [RCLONE, "rcat", scoped(cfg, path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=cfg.rclone_env,
    )
    try:
        n, sha = run_store(source, [age], rc.stdin)
    except BaseException:
        kill([rc])  # before closing stdin, so no truncated object is committed
        rc.stdin.close()
        raise
    rc.stdin.close()
    err = rc.stderr.read().decode(errors="replace")
    if rc.wait() != 0:
        raise Failed(f"upload of {source['name']} failed: {err.strip()[-300:]}")
    return {
        "name": source["name"],
        "store": source["store"],
        "source": source["source"],
        "bytes": n,
        "sha256": sha,
    }


def prune(cfg: Config) -> None:
    for kind, keep in KEEP.items():
        sets = list_sets(cfg, kind)
        complete = [name for name, ok in sets if ok]
        drop = [name for name, ok in sets if not ok] + complete[:-keep]
        for name in drop:
            rclone(cfg, "purge", set_path(cfg, kind, name))
            log(f"retired {kind}/{name}")


def backup(cfg: Config) -> int:
    if cfg.missing:
        log(f"not configured (missing {', '.join(cfg.missing)}); nothing uploaded")
        return 0
    recipients = cfg.recipients()
    if len(recipients) == 1:
        log(f"{IDENTITY_KEY} unset: encrypting to the captain key only")
    kind = "weekly" if cfg.now.weekday() == 6 else "daily"
    name = cfg.now.strftime(STAMP)
    dest = set_path(cfg, kind, name)
    scratch = scratch_dir()
    started = False
    try:
        todo, skipped = stores(cfg, scratch)
        objects = []
        started = True
        for source in todo:
            obj = upload(cfg, source, recipients, f"{dest}/{source['name']}")
            objects.append(obj)
            log(f"{kind}/{name}/{obj['name']}: {obj['bytes']} bytes")
        manifest = {
            "version": 1,
            "created": cfg.now.isoformat(),
            "recipients": recipients,
            "objects": objects,
            "skipped": skipped,
        }
        res = subprocess.run(
            [RCLONE, "rcat", scoped(cfg, f"{dest}/{MANIFEST}")],
            input=json.dumps(manifest, indent=2).encode(),
            capture_output=True,
            env=cfg.rclone_env,
            check=False,
        )
        if res.returncode != 0:
            raise Failed(f"manifest upload failed: {res.stderr.decode()[-300:]}")
    except (Failed, OSError, subprocess.SubprocessError) as exc:
        print(f"offbox-backup: {kind}/{name} failed: {exc}", file=sys.stderr)
        if started:
            rclone(cfg, "purge", dest, check=False)
        return 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    total = sum(o["bytes"] for o in objects)
    log(f"{kind}/{name} complete: {len(objects)} objects, {total} bytes")
    for s in skipped:
        log(f"skipped {s['store']}: {s['reason']}")
    try:
        prune(cfg)
    except Failed as exc:
        print(f"offbox-backup: retention failed: {exc}", file=sys.stderr)
        return 1
    return 0


# ─── verify ─────────────────────────────────────────────────────────────────


def tree_digest(entries) -> str:
    """One sha256 over sorted (path, sha256 of content) of regular files."""
    h = hashlib.sha256()
    for rel, sha in sorted(entries):
        h.update(f"{rel}\0{sha}\n".encode())
    return h.hexdigest()


def live_secret_digest(d: Path) -> tuple[str, int]:
    entries = []
    for root, _, files in os.walk(d):
        for f in files:
            p = Path(root) / f
            if p.is_file() and not p.is_symlink():
                entries.append(
                    (
                        p.relative_to(d).as_posix(),
                        hashlib.sha256(p.read_bytes()).hexdigest(),
                    )
                )
    return tree_digest(entries), len(entries)


def archive_secret_digest(stream) -> tuple[str, int]:
    entries = []
    with tarfile.open(fileobj=stream, mode="r|") as tf:
        for m in tf:
            if m.isfile():
                data = tf.extractfile(m).read()
                entries.append(
                    (m.name.removeprefix("./"), hashlib.sha256(data).hexdigest())
                )
    return tree_digest(entries), len(entries)


class Decrypt:
    """rclone cat | (sha256 check) | age -d -i <on-box key via a pipe>.
    `.stdout` is the plaintext; close() checks the hash and every exit."""

    def __init__(self, cfg: Config, path: str, obj: dict):
        self.obj = obj
        self.cat = subprocess.Popen(
            [RCLONE, "cat", scoped(cfg, path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=cfg.rclone_env,
        )
        rfd, wfd = os.pipe()
        os.write(wfd, (cfg.identity + "\n").encode())  # far below a pipe's buffer
        os.close(wfd)
        self.age = subprocess.Popen(
            [AGE, "-d", "-i", f"/dev/fd/{rfd}"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(rfd,),
        )
        os.close(rfd)
        self.stdout = self.age.stdout
        self.digest = hashlib.sha256()
        self.n = 0
        self.age_hung_up = False
        self.feeder = threading.Thread(target=self._feed, daemon=True)
        self.feeder.start()

    def _feed(self) -> None:
        try:
            self.n = pump(self.cat.stdout, self.age.stdin, self.digest)
        except BrokenPipeError:
            self.age_hung_up = True
            self.cat.kill()  # age stopped reading: rclone would block on a full pipe
        finally:
            try:
                self.age.stdin.close()
            except BrokenPipeError:
                pass

    def abort(self) -> None:
        kill([self.cat, self.age])
        self.feeder.join()

    def close(self) -> None:
        # drain whatever the consumer left so age and rclone can exit
        while self.stdout.read(CHUNK):
            pass
        self.feeder.join()
        cat_err = self.cat.stderr.read().decode(errors="replace")
        age_err = self.age.stderr.read().decode(errors="replace")
        name = self.obj["name"]
        if self.age_hung_up and self.age.wait() != 0:
            raise Failed(f"{name}: age could not decrypt it: {age_err.strip()[-300:]}")
        if self.cat.wait() != 0:
            raise Failed(f"{name}: download failed: {cat_err.strip()[-300:]}")
        if self.n != self.obj["bytes"] or self.digest.hexdigest() != self.obj["sha256"]:
            raise Failed(f"{name}: ciphertext does not match the manifest")
        if self.age.wait() != 0:
            raise Failed(f"{name}: age could not decrypt it: {age_err.strip()[-300:]}")


def check_identity(cfg: Config, plain, scratch: Path) -> str | None:
    tail = b""
    with gzip.GzipFile(fileobj=plain) as gz:  # reading to the end checks the CRC
        while chunk := gz.read(CHUNK):
            tail = (tail + chunk)[-4096:]
    if b"PostgreSQL database cluster dump complete" in tail:
        return "gzip intact, pg_dumpall footer present"
    return None


def check_backstage(cfg: Config, plain, scratch: Path) -> str | None:
    image = "postgres:17-alpine"
    ctr = find_container("backstage", "backstage-db")
    if ctr:
        res = subprocess.run(
            [DOCKER, "inspect", "--format", "{{.Config.Image}}", ctr],
            capture_output=True,
            text=True,
            check=False,
        )
        image = res.stdout.strip() or image
    # a throwaway container with no network: it only reads the archive's TOC
    res = subprocess.run(
        [DOCKER, "run", "--rm", "-i", "--network", "none", image]
        + ["pg_restore", "--list"],
        stdin=plain,
        capture_output=True,
        text=True,
        check=False,
    )
    toc = [ln for ln in res.stdout.splitlines() if ln and not ln.startswith(";")]
    if res.returncode == 0 and toc:
        return f"pg_restore --list: {len(toc)} entries"
    return None


def check_secret_key(cfg: Config, plain, scratch: Path) -> str | None:
    got, n = archive_secret_digest(plain)
    want, live_n = live_secret_digest(cfg.secret_dir)
    if got == want and n == live_n:
        return f"{n} files, same hash as the live key"
    return None


def check_state(cfg: Config, plain, scratch: Path) -> str | None:
    procs = spawn([[ZSTD, "-dc"], [TAR, "-t"]], plain)
    members = procs[-1].stdout.read().decode(errors="replace").splitlines()
    finish(procs, {})
    if "./openclaw.json" in members:
        return f"{len(members)} archive members, openclaw.json present"
    return None


def check_sqlite(cfg: Config, plain, scratch: Path) -> str | None:
    out = scratch / "sqlite"
    out.mkdir(mode=0o700)
    try:
        procs = spawn([[ZSTD, "-dc"], [TAR, "-x", "-C", str(out)]], plain)
        procs[-1].stdout.read()
        finish(procs, {})
        dbs = sorted(out.rglob("*.sqlite"))
        for db in dbs:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    return None
            finally:
                con.close()
        return f"{len(dbs)} databases pass quick_check"
    finally:
        shutil.rmtree(out, ignore_errors=True)


CHECKS = {
    "identity": check_identity,
    "backstage": check_backstage,
    "openclaw-secret-key": check_secret_key,
    "openclaw-state": check_state,
    "openclaw-sqlite": check_sqlite,
}


def check_object(cfg: Config, where: str, obj: dict, scratch: Path) -> str:
    check = CHECKS.get(obj["store"])
    if check is None:
        raise Failed(f"{obj['name']}: no restore check for store {obj['store']}")
    dec = Decrypt(cfg, f"{where}/{obj['name']}", obj)
    try:
        result = check(cfg, dec.stdout, scratch)
    except Exception as exc:
        dec.close()  # a download or decryption failure is the real reason
        raise Failed(
            f"{obj['name']}: the restore check could not read it "
            f"({type(exc).__name__}: {str(exc)[-200:]})"
        ) from exc
    except BaseException:
        dec.abort()
        raise
    dec.close()  # raises if the download, the hash or the decryption failed
    if result is None:
        raise Failed(f"{obj['name']}: the restored {obj['store']} does not check out")
    return result


def latest_set(cfg: Config) -> tuple[str, dt.datetime]:
    found = []
    for kind in KEEP:
        for name, ok in list_sets(cfg, kind):
            if ok:
                when = dt.datetime.strptime(name, STAMP).replace(tzinfo=dt.timezone.utc)
                found.append((when, f"{kind}/{name}"))
    if not found:
        raise Failed("no complete backup set in the bucket")
    when, name = max(found)
    return name, when


def verify(cfg: Config) -> tuple[int, dict | None]:
    """-> (exit code, envelope to send or None)."""
    if cfg.missing or not cfg.identity:
        missing = cfg.missing + ([] if cfg.identity else [IDENTITY_KEY])
        log(f"not configured (missing {', '.join(missing)}); nothing checked")
        return 0, None
    scratch = scratch_dir()
    lines, name = [], "?"
    try:
        name, when = latest_set(cfg)
        age = cfg.now - when
        if age > MAX_AGE:
            raise Failed(f"newest set {name} is {age.days}d {age.seconds // 3600}h old")
        where = set_path(cfg, *name.split("/"))
        manifest = json.loads(rclone(cfg, "cat", f"{where}/{MANIFEST}"))
        for obj in manifest["objects"]:
            lines.append(f"{obj['store']}: {check_object(cfg, where, obj, scratch)}")
        for s in manifest.get("skipped", []):
            lines.append(f"{s['store']}: skipped at backup ({s['reason']})")
    except (Failed, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        log(f"restore check of {name} FAILED: {exc}")
        return 0, {
            "level": "act",
            "source": "host",
            "subject": "offbox-backup",
            "headline": f"Off-box restore check failed: {exc}"[:780],
            "body": "The newest R2 backup set did not restore cleanly. "
            "Until it does, the box has no proven off-box copy.",
            "detail": "\n".join(lines),
            "action": "journalctl -u lifekit-offbox-backup -u lifekit-offbox-verify",
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    for ln in lines:
        log(ln)
    log(f"restore check of {name} passed")
    return 0, {
        "level": "good",
        "source": "host",
        "subject": "offbox-backup",
        "headline": f"Off-box restore check passed ({name})",
        "detail": "\n".join(lines),
    }


# notify-relay is internal to the compose network: POST from inside its own
# container, as scripts/ledger-persona/drift-alert.py does.
POST_JS = (
    "let b='';"
    "process.stdin.on('data',d=>b+=d).on('end',async()=>{"
    "const r=await fetch('http://127.0.0.1:'+(process.env.NOTIFY_RELAY_PORT||8090)+'/notify',"
    "{method:'POST',headers:{'content-type':'application/json'},body:b});"
    "console.log(r.status+' '+await r.text());process.exit(r.ok?0:1)})"
)


def post(env: dict) -> None:
    cid = find_container("compose", "notify-relay")
    if not cid:
        raise Failed("no running notify-relay container in compose project compose")
    out = subprocess.run(
        [DOCKER, "exec", "-i", cid, "node", "-e", POST_JS],
        input=json.dumps(env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if out.returncode != 0:
        raise Failed(
            f"notify-relay refused the envelope: {(out.stdout + out.stderr).strip()}"
        )


# ─── estimate ───────────────────────────────────────────────────────────────


def estimate(cfg: Config) -> int:
    scratch = scratch_dir()
    try:
        todo, skipped = stores(cfg, scratch)
        total = 0
        for source in todo:
            n, _ = run_store(source, [], None)
            total += n
            print(f"{source['store']:22} {n / 1e6:10.1f} MB  ({source['source']})")
        for s in skipped:
            print(f"{s['store']:22} skipped: {s['reason']}")
    except Failed as exc:
        print(f"offbox-backup: estimate failed: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    sets = sum(KEEP.values())
    stored = total * sets / 1e9
    cost = max(0.0, stored - R2_FREE_GB) * R2_USD_PER_GB
    print(f"{'one set':22} {total / 1e6:10.1f} MB")
    print(
        f"retention {sets} sets ({KEEP['daily']} daily + {KEEP['weekly']} weekly): "
        f"~{stored:.1f} GB stored, ~${cost:.2f}/month past the "
        f"{R2_FREE_GB} GB free tier (${stored * R2_USD_PER_GB:.2f} if none of it were free)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("command", choices=("backup", "verify", "estimate"))
    args = ap.parse_args(argv)
    os.umask(0o077)
    try:
        cfg = Config()
        if args.command == "backup":
            return backup(cfg)
        if args.command == "estimate":
            return estimate(cfg)
        rc, env = verify(cfg)
    except Failed as exc:
        print(f"offbox-backup: {exc}", file=sys.stderr)
        return 1
    if env is None:
        return rc
    try:
        post(env)
    except (Failed, OSError, subprocess.SubprocessError) as exc:
        print(f"offbox-backup: verdict not delivered: {exc}", file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
