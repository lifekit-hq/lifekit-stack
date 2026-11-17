"""scripts/set-secret.sh: one command puts a secret value in place.

Throwaway age key, fixture repo and fixture sops file only; no real secret is
read. `sops` and `gh` are wrappers on PATH that log their argv, so the tests can
assert the value is never in an argument, in any output, or in any file other
than the encrypted one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/set-secret.sh"
VALUE = "sk-ant-TESTVALUE-0123456789abcdef"
OLD = "old-placeholder-value"

pytestmark = pytest.mark.skipif(
    not (shutil.which("sops") and shutil.which("age-keygen") and shutil.which("git")),
    reason="needs sops, age-keygen and git",
)


def run(cmd, cwd, env=None, **kw):
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, **kw)


@pytest.fixture
def box(tmp_path):
    key = tmp_path / "key.txt"
    gen = run(["age-keygen", "-o", str(key)], tmp_path)
    pub = (gen.stderr + gen.stdout).split("Public key:")[1].split()[0]
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], tmp_path)
    work.mkdir()
    (work / "secrets").mkdir()
    (work / ".sops.yaml").write_text(
        "creation_rules:\n"
        "  - path_regex: secrets/lifekit-gateway\\.env\\.sops$\n"
        f"    age: {pub}\n"
        "  - path_regex: secrets/lifekit\\.env\\.sops$\n"
        f"    age: {pub}\n"
    )
    plain = f"ANTHROPIC_API_KEY={OLD}\nOTHER_TOKEN=keep-me\n"
    env = {**os.environ, "SOPS_AGE_KEY_FILE": str(key)}
    for name in ("lifekit-gateway", "lifekit"):
        enc = run(
            [
                "sops",
                "-e",
                "--config",
                str(work / ".sops.yaml"),
                "--filename-override",
                f"secrets/{name}.env.sops",
                "--input-type",
                "dotenv",
                "--output-type",
                "dotenv",
                "/dev/stdin",
            ],
            work,
            env,
            input=plain,
        )
        assert enc.returncode == 0, enc.stderr
        (work / "secrets" / f"{name}.env.sops").write_text(enc.stdout)
    for c in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
        ["remote", "add", "origin", str(bare)],
        ["push", "-q", "origin", "main"],
    ):
        assert run(["git", *c], work).returncode == 0
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "argv.log"
    real_sops = shutil.which("sops")
    (bindir / "sops").write_text(
        f'#!/usr/bin/env bash\nprintf "sops %s\\n" "$*" >>"{log}"\nexec "{real_sops}" "$@"\n'
    )
    (bindir / "gh").write_text(
        f'#!/usr/bin/env bash\nprintf "gh %s\\n" "$*" >>"{log}"\n'
        '[[ "$1 $2" == "pr create" ]] && echo https://example.invalid/pull/1\nexit 0\n'
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for f in bindir.iterdir():
        f.chmod(0o755)
    env.update(
        PATH=f"{bindir}:{os.environ['PATH']}",
        SET_SECRET_ROOT=str(work),
        SET_SECRET_TEST_CMD="true",
        TMPDIR=str(scratch),
    )
    return SimpleBox(tmp_path, work, env, log, scratch)


class SimpleBox:
    def __init__(self, root, work, env, log, scratch):
        self.root, self.work, self.env, self.log, self.scratch = (
            root,
            work,
            env,
            log,
            scratch,
        )

    def decrypt(self, ref_file):
        d = run(
            [
                "sops",
                "-d",
                "--input-type",
                "dotenv",
                "--output-type",
                "dotenv",
                str(ref_file),
            ],
            self.work,
            self.env,
        )
        assert d.returncode == 0, d.stderr
        return d.stdout


def files_with_value(roots, skip):
    hits = []
    for root in roots:
        for p in Path(root).rglob("*"):
            if p.is_file() and p not in skip and VALUE.encode() in p.read_bytes():
                hits.append(p)
    return hits


def test_stdin_puts_value_in_place_and_never_leaks(box):
    p = run(
        [str(SCRIPT), "gateway", "ANTHROPIC_API_KEY"],
        box.work,
        box.env,
        input=VALUE + "\n",
    )
    assert p.returncode == 0, p.stderr
    assert VALUE not in p.stdout + p.stderr
    assert VALUE not in box.log.read_text()
    assert "pr merge" in box.log.read_text() and "--auto" in box.log.read_text()
    assert "openclaw secrets reload" in p.stdout and "auth-mode.sh probe" in p.stdout
    # the value is in the encrypted file on the new branch, and nowhere else
    branch = run(["git", "branch", "--show-current"], box.work).stdout.strip()
    assert branch.startswith("chore/rotate-anthropic-api-key-")
    enc = box.work / "secrets/lifekit-gateway.env.sops"
    assert VALUE not in enc.read_text()
    dec = box.decrypt(enc)
    assert f"ANTHROPIC_API_KEY={VALUE}" in dec and "OTHER_TOKEN=keep-me" in dec
    assert OLD not in dec
    assert files_with_value([box.root], skip=set()) == []
    # not in the commit, nor in git's object store as plaintext
    show = run(["git", "log", "-p", "-1"], box.work).stdout
    assert VALUE not in show
    status = run(["git", "status", "--porcelain"], box.work).stdout
    assert status == ""


def test_master_class_uses_master_file(box):
    p = run(
        [str(SCRIPT), "master", "ANTHROPIC_API_KEY"], box.work, box.env, input=VALUE
    )
    assert p.returncode == 0, p.stderr
    assert f"ANTHROPIC_API_KEY={VALUE}" in box.decrypt(
        box.work / "secrets/lifekit.env.sops"
    )
    assert "auth-mode.sh probe" not in p.stdout


@pytest.mark.parametrize(
    "args,stdin",
    [
        (["bogus", "ANTHROPIC_API_KEY"], VALUE),
        (["gateway", "NOT_A_KEY"], VALUE),
        (["gateway", "ANTHROPIC_API_KEY"], ""),
        (["gateway", "ANTHROPIC_API_KEY"], VALUE + "\nsecond-line"),
    ],
)
def test_refusals_leave_the_file_alone_and_leak_nothing(box, args, stdin):
    enc = box.work / "secrets/lifekit-gateway.env.sops"
    before = enc.read_text()
    p = run([str(SCRIPT), *args], box.work, box.env, input=stdin)
    assert p.returncode != 0
    assert VALUE not in p.stdout + p.stderr
    assert enc.read_text() == before
    assert "pr create" not in (box.log.read_text() if box.log.exists() else "")


def test_dirty_tree_is_refused(box):
    (box.work / "stray.txt").write_text("x")
    p = run(
        [str(SCRIPT), "gateway", "ANTHROPIC_API_KEY"], box.work, box.env, input=VALUE
    )
    assert p.returncode != 0 and "not clean" in p.stderr


def test_failing_tests_stop_before_commit(box):
    env = {**box.env, "SET_SECRET_TEST_CMD": "false"}
    p = run([str(SCRIPT), "gateway", "ANTHROPIC_API_KEY"], box.work, env, input=VALUE)
    assert p.returncode != 0
    assert "pr create" not in box.log.read_text()
    assert VALUE not in p.stdout + p.stderr
