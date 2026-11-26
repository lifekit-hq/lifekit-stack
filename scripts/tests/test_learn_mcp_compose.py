"""compose/learn-mcp/docker-compose.yml: the study coach's connector stays off
the host's ports, inside the platform contract, and free of secret values; its
image carries the repo's own record.py and daily.py.

Plain YAML, no Docker. The service's behaviour is compose/learn-mcp/tests.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
SERVICE_DIR = REPO / "compose/learn-mcp"
COMPOSE = yaml.safe_load((SERVICE_DIR / "docker-compose.yml").read_text())
SVC = COMPOSE["services"]["learn-mcp"]

_spec = importlib.util.spec_from_file_location(
    "platform_contract", REPO / "scripts/platform-contract.py"
)
contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract)


def test_the_project_is_its_own_and_has_exactly_one_service():
    assert COMPOSE["name"] == "learn-mcp"
    assert list(COMPOSE["services"]) == ["learn-mcp"]


def test_the_service_passes_the_static_contract_check():
    assert contract.static_findings("learn-mcp", SVC) == []


def test_it_is_routed_through_the_edge_and_publishes_no_host_port():
    assert SVC["labels"]["lifekit.contract.ingress"] == "edge"
    assert SVC["labels"]["traefik.enable"] == "true"
    assert "ports" not in SVC
    assert SVC["expose"] == ["8099"]
    assert set(SVC["networks"]) == {"default", "lifekit-shared"}
    assert COMPOSE["networks"]["lifekit-shared"] == {"external": True}


def test_it_is_locked_down_like_every_other_service():
    assert SVC["read_only"] is True
    uid, gid = (int(x) for x in SVC["user"].split(":"))
    assert uid != 0 and gid != 0
    assert SVC["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in SVC["security_opt"]
    assert SVC["deploy"]["resources"]["limits"]["memory"]


def test_the_token_comes_from_the_env_file_by_name_never_as_a_value():
    env = SVC["environment"]
    assert env["LEARN_MCP_TOKEN"] == "${LEARN_MCP_TOKEN:-}"
    for key, value in env.items():
        assert not re.search(r"[0-9a-f]{32}", str(value)), key


def test_the_default_ranges_are_anthropics_published_outbound_ranges():
    assert SVC["environment"]["LEARN_MCP_ALLOW_CIDRS"].endswith(
        ":-160.79.104.0/21,153.61.208.0/20}"
    )


def _mounts():
    """Each short-form mount as (source, target, read_only)."""
    out = []
    for m in SVC["volumes"]:
        parts = m.split(":")
        # the source holds a `${VAR:-default}`, whose `:-` is not a separator
        mode = parts[-1] if parts[-1] in ("ro", "rw") else "rw"
        if parts[-1] in ("ro", "rw"):
            parts = parts[:-1]
        out.append((":".join(parts[:-1]), parts[-1], mode == "ro"))
    return out


def test_the_only_writable_mount_is_the_learning_pages_own_directory():
    mounts = {target: (source, ro) for source, target, ro in _mounts()}
    assert set(mounts) == {"/vault", "/vault/domains", "/mirror"}
    vault_src, vault_ro = mounts["/vault"]
    assert vault_ro is True
    assert mounts["/mirror"][1] is True
    domains_src, domains_ro = mounts["/vault/domains"]
    assert domains_ro is False
    # the writable mount is the read-only vault's own domains/, nothing wider
    assert domains_src == vault_src + "/domains"
    # and it is the directory of the one page record.py is pinned to
    env = SVC["environment"]
    assert env["LEARNING_VAULT"] == "/vault"
    assert str(Path("/vault") / Path(env["LEARNING_PAGE"]).parent) == "/vault/domains"


def test_no_host_socket_or_other_host_path_is_mounted():
    for source, target, _ in _mounts():
        assert "docker.sock" not in source
        assert target.startswith(("/vault", "/mirror"))
    assert SVC["tmpfs"] == ["/tmp"]


def _instructions(dockerfile: str):
    """The Dockerfile as (INSTRUCTION, args) pairs: comments dropped, `\\`
    continuations joined."""
    out, buf = [], ""
    for line in dockerfile.splitlines():
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        word, _, args = (buf + line).strip().partition(" ")
        out.append((word.upper(), args.split()))
        buf = ""
    return out


def _copied_files(instructions):
    """Each COPY as {(build context, source): destination file in the image}."""
    files = {}
    for word, args in instructions:
        if word != "COPY":
            continue
        flags = dict(a[2:].partition("=")[::2] for a in args if a.startswith("--"))
        *sources, dest = [a for a in args if not a.startswith("--")]
        for src in sources:
            target = dest + Path(src).name if dest.endswith("/") else dest
            files[(flags.get("from", "."), src)] = target
    return files


def test_the_image_puts_the_repos_skills_where_the_server_resolves_them():
    # server.py and context.py live in /app and resolve record.py, daily.py
    # (and so problems.json, daily.py's sibling) under HERE / "skills/...".
    assert SVC["build"]["additional_contexts"] == {"skills": "../../skills"}
    files = _copied_files(_instructions((SERVICE_DIR / "Dockerfile").read_text()))
    want = {
        (
            "skills",
            "learning-record/record.py",
        ): "/app/skills/learning-record/record.py",
        ("skills", "leetcode-daily/daily.py"): "/app/skills/leetcode-daily/daily.py",
        (
            "skills",
            "leetcode-daily/problems.json",
        ): "/app/skills/leetcode-daily/problems.json",
        (".", "server.py"): "/app/server.py",
        (".", "context.py"): "/app/context.py",
    }
    for key, dest in want.items():
        assert files.get(key) == dest, key
    contexts = {"skills": REPO / "skills", ".": SERVICE_DIR}
    for ctx, src in want:
        assert (contexts[ctx] / src).is_file(), src


def test_the_image_runs_as_a_non_root_user():
    users = [
        args
        for word, args in _instructions((SERVICE_DIR / "Dockerfile").read_text())
        if word == "USER"
    ]
    assert users, "no USER: the image would run as root"
    uid, _, gid = users[-1][0].partition(":")
    assert uid not in ("0", "root") and gid not in ("0", "root")
