"""scripts/docker-weekly-prune: the prune never removes a :prev rollback image."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "scripts/docker-weekly-prune"
SCRIPT = DIR / "docker-weekly-prune.sh"
INSTALLER = DIR / "install-docker-weekly-prune.sh"

# A docker stand-in that keeps images in a JSON file and implements the one
# filter the script relies on, so the test checks what survives, not just the
# command line. Anything but image ls/inspect/prune and volume prune is a
# test failure: it logs the call and exits 99.
FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
state = os.environ["STATE"]
args = sys.argv[1:]
with open(state + "/calls", "a") as f:
    f.write(" ".join(args) + "\n")
images = json.load(open(state + "/images.json"))
def save():
    json.dump(images, open(state + "/images.json", "w"))
if args[:2] == ["image", "ls"]:
    for i in images:
        for t in i["tags"]:
            if t.endswith(":prev"):
                print(t)
elif args[:2] == ["image", "inspect"]:
    ref = args[-1]
    for i in images:
        if ref in i["tags"]:
            print(i["labels"].get("lifekit.keep", ""))
elif args[:3] == ["image", "prune", "-a"] and "-f" in args:
    flt = args[args.index("--filter") + 1] if "--filter" in args else ""
    key, _, val = flt.removeprefix("label!=").partition("=")
    images[:] = [
        i for i in images
        if i["used"] or (flt and i["labels"].get(key) == val)
    ]
    save()
    print("Total reclaimed space: 1GB")
elif args == ["volume", "prune", "-f"]:
    print("Total reclaimed space: 0B")
else:
    sys.exit(99)
"""


def image(tag, *, keep=False, used=False):
    return {
        "tags": [tag],
        "labels": {"lifekit.keep": "rollback"} if keep else {},
        "used": used,
    }


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)

    def go(images):
        (tmp_path / "images.json").write_text(json.dumps(images))
        (tmp_path / "calls").write_text("")
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "STATE": str(tmp_path),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        left = [
            t
            for i in json.loads((tmp_path / "images.json").read_text())
            for t in i["tags"]
        ]
        calls = (tmp_path / "calls").read_text().splitlines()
        return result, left, calls

    return go


def test_labeled_prev_survives_while_other_unused_images_go(run):
    result, left, calls = run(
        [
            image("lifekit-openclaw:prev", keep=True),
            image("lifekit-openclaw:local", used=True),
            image("old/thing:1"),
        ]
    )

    assert result.returncode == 0, result.stderr
    assert left == ["lifekit-openclaw:prev", "lifekit-openclaw:local"]
    assert "image prune -a -f --filter label!=lifekit.keep=rollback" in calls
    assert "volume prune -f" in calls


def test_only_image_and_anonymous_volume_prunes_are_issued(run):
    _, _, calls = run([image("lifekit-openclaw:prev", keep=True)])

    prunes = [c for c in calls if "prune" in c]
    assert prunes == [
        "image prune -a -f --filter label!=lifekit.keep=rollback",
        "volume prune -f",
    ]
    joined = "\n".join(calls)
    for forbidden in (
        "builder",
        "network",
        "system",
        "--all",
        "restart",
        "stop",
        "kill",
    ):
        assert forbidden not in joined
    assert "volume rm" not in joined


def test_unlabeled_prev_is_refused_not_pruned(run):
    result, left, calls = run([image("lifekit-openclaw:prev"), image("old/thing:1")])

    assert result.returncode == 1
    assert "lifekit-openclaw:prev" in result.stderr
    assert left == ["lifekit-openclaw:prev", "old/thing:1"]
    assert not any(c.startswith("image prune") for c in calls)
    assert "volume prune -f" in calls


def test_gateway_image_carries_the_keep_label():
    dockerfile = (REPO / "compose/openclaw-gateway/Dockerfile").read_text()
    assert 'LABEL lifekit.keep="rollback"' in dockerfile


def test_units_and_installer_wiring():
    for ext in ("service", "timer"):
        assert (DIR / f"lifekit-docker-weekly-prune.{ext}").is_file()
    assert "OnCalendar=Sun " in (DIR / "lifekit-docker-weekly-prune.timer").read_text()
    assert os.access(INSTALLER, os.X_OK) and os.access(SCRIPT, os.X_OK)
    bootstrap = (REPO / "scripts/bootstrap-vps.sh").read_text()
    assert "install-docker-weekly-prune.sh" not in bootstrap


def test_installer_writes_under_install_root_and_enables_timer(tmp_path):
    fake = tmp_path / "systemctl"
    fake.write_text('#!/bin/sh\necho "$*" >> "$STATE/calls"\n')
    fake.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "root"
    subprocess.run(
        ["bash", str(INSTALLER)],
        env={
            **os.environ,
            "INSTALL_ROOT": str(root),
            "SYSTEMCTL": str(fake),
            "STATE": str(state),
        },
        check=True,
        capture_output=True,
    )
    assert os.access(root / "usr/local/bin/lifekit-docker-weekly-prune.sh", os.X_OK)
    units = root / "etc/systemd/system"
    assert (units / "lifekit-docker-weekly-prune.service").is_file()
    assert (units / "lifekit-docker-weekly-prune.timer").is_file()
    assert (state / "calls").read_text().splitlines() == [
        "daemon-reload",
        "enable --now lifekit-docker-weekly-prune.timer",
    ]
