"""Invariants of compose/openclaw-gateway/platform.patch.json (no Docker, no network).

The file is applied to the production gateway by deploy.sh, so the bounds the
inbound hooks were authorized under are pinned here rather than left to review:
secrets only as ${VAR} references, a dedicated hook token, one allowed agent,
ids-only templates, and no listener beyond the gateway's loopback publish. The
plugin allowlist and the agent-memory and read-only Google Workspace bounds of
the openclaw compose project (lifekit-stack#175) are pinned the same way.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PATCH = json.loads((REPO / "compose/openclaw-gateway/platform.patch.json").read_text())
COMPOSE = (REPO / "compose/openclaw/docker-compose.yml").read_text()
HOOKS = PATCH["hooks"]
ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)
    elif isinstance(node, str):
        yield node


def service_block(name: str) -> str:
    match = re.search(
        rf"^  {name}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", COMPOSE, re.M | re.S
    )
    assert match, f"no {name} service in docker-compose.yml"
    return match.group(1)


def test_hook_token_is_a_dedicated_env_reference():
    ref = ENV_REF.fullmatch(HOOKS["token"])
    assert ref, "hooks.token must be a ${VAR} reference, never a literal"
    assert (
        ref.group(1) != "OPENCLAW_GATEWAY_TOKEN"
    ), "the gateway token must not be reused"


def test_hook_path_is_not_in_git():
    assert ENV_REF.fullmatch(HOOKS["path"]), "a path committed to the repo is guessable"


def test_hooks_reach_only_the_finance_agent():
    assert HOOKS["allowedAgentIds"] == ["finance"]
    assert HOOKS.get("allowRequestSessionKey", False) is False
    assert HOOKS["mappings"], "hooks enabled with no mapping"
    for mapping in HOOKS["mappings"]:
        assert mapping["agentId"] == "finance"
        assert mapping["sessionMode"] == "isolated"
        assert mapping.get("allowUnsafeExternalContent", False) is False
        assert "transform" not in mapping


def test_templates_interpolate_identifiers_only():
    # finance-sentry constitution, Principle VII: the hook carries ids only, so
    # no free-text payload field may be spliced into the agent's prompt.
    for mapping in HOOKS["mappings"]:
        for key in ("messageTemplate", "textTemplate"):
            fields = set(re.findall(r"\{\{\s*([^}\s]+)\s*\}\}", mapping.get(key, "")))
            assert fields <= {"kind", "eventId"}, f"{key} interpolates {fields}"


def test_no_chat_id_literal():
    # docs/PRIVATE.md: chat ids never land in the repo.
    assert not [s for s in strings(PATCH) if re.fullmatch(r"-?\d{5,}", s)]


def test_every_env_reference_reaches_the_gateway_and_the_cli():
    refs = {name for s in strings(PATCH) for name in ENV_REF.findall(s)}
    assert refs, "expected ${VAR} references in the patch"
    for service in ("openclaw-gateway", "openclaw-cli"):
        block = service_block(service)
        for name in refs:
            assert re.search(
                rf"^\s+{name}: \$\{{", block, re.M
            ), f"{service} lacks {name}"


def test_gateway_publishes_loopback_only():
    block = service_block("openclaw-gateway")
    ports = re.search(r"^    ports:\n((?:\s+- .*\n)+)", block, re.M)
    assert ports, "openclaw-gateway has no ports block"
    published = re.findall(r'- "?([^"\n]+)"?', ports.group(1))
    assert published and all(p.startswith("127.0.0.1:") for p in published), published


def test_only_the_finance_agent_gets_a_heartbeat():
    assert PATCH["agents"]["defaults"]["heartbeat"]["every"] == "0m"
    assert set(PATCH["agents"]["entries"]) == {"finance"}
    assert set(PATCH["agents"]["entries"]["finance"]) == {"heartbeat"}


# Model providers something routes to: every agent's model is anthropic/*
# through the claude-cli backend, and the openai auth profile runs on the
# codex agent runtime. Any other provider plugin stays out of the allowlist.
ROUTED_PROVIDERS = {"anthropic", "openai", "codex"}
UNROUTED_PROVIDERS = {"google", "kie", "ollama", "openrouter", "vllm", "xai"}
MEMORY_AGENTS = ("kit", "finance", "devclaw")


def test_plugin_allowlist_keeps_what_the_stack_runs():
    allow = PATCH["plugins"]["allow"]
    assert len(allow) == len(set(allow)), "duplicate ids in plugins.allow"
    required = ROUTED_PROVIDERS | {"telegram", "memory-core"}
    assert required <= set(allow), required - set(allow)
    # An enabled entry left off the allowlist would silently never load.
    enabled = {k for k, v in PATCH["plugins"]["entries"].items() if v.get("enabled")}
    assert enabled <= set(allow), enabled - set(allow)
    assert not UNROUTED_PROVIDERS & set(allow)


def compose_services() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(COMPOSE)["services"]


def volumes(service: str) -> list[str]:
    return compose_services()[service]["volumes"]


def test_agent_memory_lives_in_the_vault():
    for service in ("openclaw-gateway", "openclaw-cli"):
        mounts = volumes(service)
        for agent in MEMORY_AGENTS:
            want = (
                f"${{LIFEKIT_LIFE_DIR:-/srv/memory}}/agents/{agent}/memory:"
                f"/home/node/.openclaw/agents/{agent}/workspace/memory"
            )
            assert want in mounts, f"{service} lacks the {agent} memory mount"
        # MEMORY.md stays in the workspace; a single-file bind does not
        # survive a rename-replace.
        assert not [m for m in mounts if "MEMORY.md" in m]


def test_retired_wiki_vault_mount_is_gone():
    for service in ("openclaw-gateway", "openclaw-cli"):
        assert not [m for m in volumes(service) if "/.openclaw/wiki" in m]


def test_google_workspace_mcp_is_read_only():
    env = compose_services()["google-workspace-mcp"]["environment"]
    # A literal, so widening it is a reviewed compose change, not an env edit.
    assert env["WORKSPACE_MCP_READ_ONLY"] == "true"
    # --tools in the start command makes the server ignore permissions; a
    # permissions key here would only suggest a guard that is not there.
    assert "WORKSPACE_MCP_PERMISSIONS" not in env


def test_finance_hook_asks_for_the_event_app_link_without_inventing_one():
    # finance-sentry#466 N-6: event messages end with the event's appUrl, read
    # from the pulled event (never interpolated: the hook carries ids only).
    for mapping in HOOKS["mappings"]:
        template = mapping["messageTemplate"]
        assert "appUrl" in template
        assert "never build, guess or reuse a link" in template
