"""Invariants of compose/openclaw-gateway/platform.patch.json (no Docker, no network).

The file is applied to the production gateway by deploy.sh, so the bounds the
inbound hooks were authorized under are pinned here rather than left to review:
secrets only as ${VAR} references, a dedicated hook token, the allowed agents,
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


def test_code_mode_is_authored_on_the_quickjs_executor():
    # Absent tools.codeMode means "auto" on the node:vm executor, which is not a
    # security boundary; Kit reads untrusted mail, so the executor is pinned to
    # the hardened QuickJS one and the activation is authored so a version bump
    # cannot silently change the tool surface.
    assert PATCH["tools"]["codeMode"] == {"enabled": "auto", "executor": "quickjs"}


# What each hook mapping may do. The agents are the only ones hooks reach
# (hooks.allowedAgentIds), the session mode is what the mapping was authorized
# under, and the fields are the only payload values its template may splice in.
MAPPING_BOUNDS = {
    "finance-sentry-companion": {
        "agent": "finance",
        "mode": "isolated",
        "fields": {"kind", "eventId"},
    },
    "firstmate-reply": {
        "agent": "kit",
        "mode": "isolated",
        "fields": {"home", "noteId"},
    },
}


def test_hooks_reach_only_the_authorized_agents():
    assert HOOKS.get("allowRequestSessionKey", False) is False
    assert HOOKS["mappings"], "hooks enabled with no mapping"
    assert {m["id"] for m in HOOKS["mappings"]} == set(MAPPING_BOUNDS)
    assert sorted(HOOKS["allowedAgentIds"]) == sorted(
        {b["agent"] for b in MAPPING_BOUNDS.values()}
    )
    for mapping in HOOKS["mappings"]:
        bounds = MAPPING_BOUNDS[mapping["id"]]
        assert mapping["agentId"] == bounds["agent"]
        assert mapping["sessionMode"] == bounds["mode"]
        assert mapping.get("allowUnsafeExternalContent", False) is False
        assert "transform" not in mapping


def test_persistent_mappings_use_a_static_session_key_of_their_own_agent():
    # A persistent turn lands in a long-lived conversation, so its key is fixed
    # in the file: never derived from the payload, always the mapping's agent.
    for mapping in HOOKS["mappings"]:
        if mapping["sessionMode"] != "persistent":
            assert "sessionKey" not in mapping
            continue
        key = mapping["sessionKey"]
        assert "{{" not in key
        assert key.startswith(f"agent:{mapping['agentId']}:")


def test_templates_interpolate_identifiers_only():
    # finance-sentry constitution, Principle VII: the hook carries ids only, so
    # no free-text payload field may be spliced into the agent's prompt. The
    # same holds for a firstmate reply: never its body.
    for mapping in HOOKS["mappings"]:
        allowed = MAPPING_BOUNDS[mapping["id"]]["fields"]
        for key in ("messageTemplate", "textTemplate", "sessionKey", "to"):
            fields = set(re.findall(r"\{\{\s*([^}\s]+)\s*\}\}", mapping.get(key, "")))
            assert fields <= allowed, f"{key} interpolates {fields}"


def test_firstmate_reply_delivers_to_the_owners_kit_dm():
    # An isolated session has no last route and neither does kit's persistent
    # main one (the owner's DM runs in its own per-peer session), so the route
    # is explicit: a Telegram target read from the env, never "last" and never a
    # chat id in the file (docs/PRIVATE.md).
    mapping = next(m for m in HOOKS["mappings"] if m["id"] == "firstmate-reply")
    assert mapping["match"] == {"path": "firstmate-reply"}
    assert mapping["agentId"] == "kit"
    assert mapping["sessionMode"] == "isolated"
    assert "sessionKey" not in mapping
    assert mapping["deliver"] is True
    assert mapping["channel"] == "telegram"
    assert mapping["to"] == "${OPENCLAW_KIT_CHAT}"


def test_kit_chat_has_no_fallback_to_another_chat():
    # Unlike OPENCLAW_FINANCE_CHAT, an unset OPENCLAW_KIT_CHAT must stay empty in
    # the container so the deploy's dry run refuses the patch, rather than quietly
    # routing the reply to some other chat.
    for service in ("openclaw-gateway", "openclaw-cli"):
        assert re.search(
            r"^\s+OPENCLAW_KIT_CHAT: \$\{OPENCLAW_KIT_CHAT:-\}$",
            service_block(service),
            re.M,
        ), f"{service} must pass OPENCLAW_KIT_CHAT through with an empty default"


def test_kit_chat_is_a_documented_master_setting():
    secrets = (REPO / "docs/secrets.md").read_text()
    assert re.search(r"^\| `OPENCLAW_KIT_CHAT` \| master \| setting \|", secrets, re.M)
    assert "OPENCLAW_KIT_CHAT" in (REPO / ".env.example").read_text()


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
MEMORY_AGENTS = ("kit", "finance")


def test_plugin_allowlist_keeps_what_the_stack_runs():
    allow = PATCH["plugins"]["allow"]
    assert len(allow) == len(set(allow)), "duplicate ids in plugins.allow"
    required = ROUTED_PROVIDERS | {"telegram", "memory-core"}
    assert required <= set(allow), required - set(allow)
    # An enabled entry left off the allowlist would silently never load.
    enabled = {k for k, v in PATCH["plugins"]["entries"].items() if v.get("enabled")}
    assert enabled <= set(allow), enabled - set(allow)
    assert not UNROUTED_PROVIDERS & set(allow)


def test_dreaming_stays_off():
    # The vault is the one memory store: memory-core's dreaming would promote
    # agent notes into MEMORY.md, which holds a pointer only.
    dreaming = PATCH["plugins"]["entries"]["memory-core"]["config"]["dreaming"]
    assert dreaming == {"enabled": False}


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


def test_retired_devclaw_agent_wiring_is_gone():
    # The devclaw OpenClaw agent is retired: no memory mount, and no gateway
    # env for its Telegram account or MCP server.
    for service in ("openclaw-gateway", "openclaw-cli"):
        assert not [m for m in volumes(service) if "agents/devclaw" in m]
        env = compose_services()[service].get("environment", {})
        assert not [k for k in env if k.startswith("DEVCLAW_")]


def test_workshop_approval_policy_stays_retired():
    # OpenClaw 2026.10.5 retired Skill Workshop proposals and removed
    # skills.workshop.approvalPolicy from the schema; the dry run rejects it.
    assert "skills" not in PATCH


def test_bot_tokens_are_sops_refs_or_dropped():
    # A disabled account keeps no ref (null deletes it): a Telegram channel
    # restart stops disabled accounts too, and an unresolved ref there turns
    # the restart into a gateway restart (runbook, "Telegram bot tokens").
    accounts = PATCH["channels"]["telegram"]["accounts"]
    assert accounts["default"]["botToken"] is not None
    for name, account in accounts.items():
        token = account["botToken"]
        assert token is None or (
            token["source"] == "exec" and token["provider"] == "sops"
        ), name


def test_google_workspace_mcp_exposes_send_as_its_only_write_tool():
    svc = compose_services()["google-workspace-mcp"]
    env = svc["environment"]
    # A non-empty TOOLS puts --tools in the start command, and the server then
    # runs in full mode and ignores permissions. It must stay an empty literal.
    assert env["TOOLS"] == ""
    # --read-only / --tools are mutually exclusive with permissions; neither
    # key may come back alongside it.
    assert "WORKSPACE_MCP_READ_ONLY" not in env
    assert "WORKSPACE_MCP_TOOLS" not in env
    # Literals, so widening the consent is a reviewed compose change, not an
    # env-file edit.
    assert env["WORKSPACE_MCP_PERMISSIONS"] == (
        "gmail:send drive:readonly calendar:readonly "
        "docs:readonly sheets:readonly tasks:readonly"
    )
    # The other write tools that gmail:send pulls in (cumulative levels).
    assert env["WORKSPACE_MCP_DISABLED_TOOLS"].split(",") == [
        "draft_gmail_message",
        "manage_gmail_label",
        "modify_gmail_message_labels",
        "batch_modify_gmail_message_labels",
    ]
    # 2.x binds loopback by default; the gateway reaches it over the network.
    assert env["WORKSPACE_MCP_HOST"] == "0.0.0.0"
    assert env["WORKSPACE_MCP_SESSION_IDLE_TIMEOUT"] == "0"
    for key in (
        "TOOLS",
        "WORKSPACE_MCP_PERMISSIONS",
        "WORKSPACE_MCP_DISABLED_TOOLS",
        "WORKSPACE_MCP_HOST",
        "WORKSPACE_MCP_SESSION_IDLE_TIMEOUT",
    ):
        assert "${" not in str(env[key])
    assert svc["image"] == "ghcr.io/taylorwilsdon/google_workspace_mcp:2.1.0"
