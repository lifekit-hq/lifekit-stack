"""The secrets discipline of docs/secrets.md, held by CI on every PR.

No secret value is read, printed or compared here. The two SOPS files are
parsed as text (key names and sops metadata are plaintext by design); the one
decryption test runs the resolver against the gateway file with the gateway
key when this host has it (the box's self-hosted runner) and checks presence
only. The master file is never decrypted: nothing on a service account can.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SECRETS = REPO / "secrets"
FILES = {
    "master": SECRETS / "lifekit.env.sops",
    "gateway": SECRETS / "lifekit-gateway.env.sops",
}
INVENTORY = REPO / "docs/secrets.md"
SOPS_YAML = REPO / ".sops.yaml"
PATCH = json.loads((REPO / "compose/openclaw-gateway/platform.patch.json").read_text())
RESOLVER = REPO / "compose/openclaw-gateway/sops-resolver.py"
DOCKERFILE = (REPO / "compose/openclaw-gateway/Dockerfile").read_text()
COMPOSE = (REPO / "compose/docker-compose.yml").read_text()

NAME_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")  # <CONSUMER>_<PURPOSE>
ENC_RE = re.compile(
    r"^ENC\[AES256_GCM,data:[^,]*,iv:[^,]+,tag:[^,]+,type:(str|comment)\]$"
)
AGE_RE = re.compile(r"^age1[a-z0-9]{58}$")
ROW_RE = re.compile(
    r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*(master|gateway)\s*\|\s*([a-z-]+)\s*\|"
)


# ─── parsers ─────────────────────────────────────────────────────────────────


def parse_sops_dotenv(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """-> (entries, sops metadata). Never returns anything decrypted."""
    entries, meta = {}, {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        (meta if key.startswith("sops_") else entries)[key] = value
    return entries, meta


def recipients_of(meta: dict[str, str]) -> set[str]:
    return {
        v
        for k, v in meta.items()
        if re.fullmatch(r"sops_age__list_\d+__map_recipient", k)
    }


def inventory() -> dict[str, dict[str, str]]:
    rows = {}
    for line in INVENTORY.read_text().splitlines():
        m = ROW_RE.match(line)
        if m:
            assert (
                m.group(1) not in rows
            ), f"{m.group(1)} listed twice in docs/secrets.md"
            rows[m.group(1)] = {"boundary": m.group(2), "class": m.group(3)}
    return rows


def sops_rules() -> dict[str, set[str]]:
    """creation_rules of .sops.yaml as {boundary: recipients} (no yaml module on the runner)."""
    text = SOPS_YAML.read_text()
    rules = {}
    for block in re.split(r"\n\s*-\s+path_regex:", text)[1:]:
        regex = block.split("\n", 1)[0].strip()
        ages = set(re.findall(r"age1[a-z0-9]{58}", block))
        for boundary, path in FILES.items():
            if re.search(regex, path.relative_to(REPO).as_posix()):
                rules[boundary] = ages
    return rules


def key_for(ref_id: str) -> str:
    return ref_id.upper().replace("-", "_")


def patch_refs() -> dict[str, str]:
    """{ref id: config path} for every exec ref on the sops provider."""
    found = {}

    def walk(node, path):
        if isinstance(node, dict):
            if (
                node.get("source") == "exec" and "id" in node
            ):  # a ref, not the provider block
                assert node.get("provider") == "sops", f"{path}: unknown exec provider"
                found[node["id"]] = path
                return
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(PATCH, "")
    return found


def require(boundary: str) -> Path:
    path = FILES[boundary]
    assert path.is_file(), (
        f"{path.relative_to(REPO)} is missing: run scripts/secrets/import-legacy-env.sh "
        "(docs/secrets-runbook.md, cutover) - no secret may live outside the two files"
    )
    return path


# ─── the files ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("boundary", list(FILES))
def test_file_is_sops_encrypted_dotenv(boundary):
    entries, meta = parse_sops_dotenv(require(boundary))
    assert entries, "an empty secret file is not a boundary"
    assert "sops_mac" in meta and ENC_RE.match(
        meta["sops_mac"]
    ), "no MAC: not a SOPS file"
    assert meta.get("sops_version"), "no sops_version"
    for key, value in entries.items():
        assert value == "" or ENC_RE.match(value), f"{key} is not encrypted"


@pytest.mark.parametrize("boundary", list(FILES))
def test_file_recipients_match_sops_yaml(boundary):
    _, meta = parse_sops_dotenv(require(boundary))
    assert (
        recipients_of(meta) == sops_rules()[boundary]
    ), f"{boundary}: re-key with sops updatekeys"


def test_sops_yaml_boundaries():
    rules = sops_rules()
    assert set(rules) == {
        "master",
        "gateway",
    }, "each file needs exactly one creation rule"
    assert all(
        AGE_RE.match(a) for ages in rules.values() for a in ages
    ), "recipients must be age public keys"
    assert len(rules["master"]) == 1, "the master file is the captain's alone"
    assert (
        rules["master"] < rules["gateway"]
    ), "the captain must be able to edit the gateway file"
    gateway_only = rules["gateway"] - rules["master"]
    assert len(gateway_only) == 1, "exactly one gateway identity"
    assert (
        not gateway_only & rules["master"]
    ), "the gateway key must not open the master file"


@pytest.mark.parametrize("boundary", list(FILES))
def test_names_follow_the_convention(boundary):
    entries, _ = parse_sops_dotenv(require(boundary))
    bad = [k for k in entries if not NAME_RE.match(k)]
    assert not bad, f"{boundary}: not <CONSUMER>_<PURPOSE>: {bad}"
    if boundary == "gateway":
        assert not [
            k for k in entries if k.startswith("PARKED_")
        ], "parked secrets live in the master file"


# ─── inventory <-> files ─────────────────────────────────────────────────────


@pytest.mark.parametrize("boundary", list(FILES))
def test_every_entry_is_inventoried_and_every_row_exists(boundary):
    entries, _ = parse_sops_dotenv(require(boundary))
    rows = {name for name, row in inventory().items() if row["boundary"] == boundary}
    # settings are optional (compose has defaults); secrets are not
    secret_rows = {
        name
        for name, row in inventory().items()
        if row["boundary"] == boundary and row["class"] != "setting"
    }
    assert (
        set(entries) <= rows
    ), f"{boundary}: not in docs/secrets.md: {sorted(set(entries) - rows)}"
    assert secret_rows <= set(
        entries
    ), f"{boundary}: inventoried but absent: {sorted(secret_rows - set(entries))}"


def test_inventory_rows_point_at_their_file():
    for line in INVENTORY.read_text().splitlines():
        m = ROW_RE.match(line)
        if m and m.group(3) != "setting":
            assert (
                FILES[m.group(2)].name in line
            ), f"{m.group(1)}: File column disagrees with Boundary"


def test_parked_secrets_have_no_other_class():
    for name, row in inventory().items():
        assert name.startswith("PARKED_") == (row["class"] == "parked"), name


# ─── platform patch <-> gateway file <-> image ──────────────────────────────


def test_gateway_file_holds_exactly_what_the_patch_resolves():
    entries, _ = parse_sops_dotenv(require("gateway"))
    refs = {key_for(i) for i in patch_refs()}
    assert refs == set(entries), (
        f"gateway file and platform.patch.json disagree: file-only {sorted(set(entries) - refs)}, "
        f"patch-only {sorted(refs - set(entries))}"
    )


def test_patch_refs_are_bot_tokens_only():
    for ref_id, path in patch_refs().items():
        assert re.fullmatch(
            r"[a-z0-9][a-z0-9-]*", ref_id
        ), f"{ref_id}: exec ids take no underscore"
        assert path.startswith("channels.telegram.accounts.") and path.endswith(
            ".botToken"
        ), path
    accounts = PATCH["channels"]["telegram"]["accounts"]
    for name, account in accounts.items():
        assert set(account) == {
            "botToken"
        }, f"account {name}: only botToken is platform state"


def test_provider_matches_image_and_mounts():
    provider = PATCH["secrets"]["providers"]["sops"]
    assert provider["source"] == "exec" and provider.get("jsonOnly") is True
    assert (
        "passEnv" not in provider
    ), "the gateway's PATH starts with an agent-writable dir; never inherit"
    env = provider["env"]
    assert (
        f"COPY --chown=node:node --chmod=0500 sops-resolver.py {provider['command']}"
        in DOCKERFILE
    )
    assert provider["trustedDirs"] == [str(Path(provider["command"]).parent)]
    assert (
        env["LIFEKIT_SOPS_BIN"] == "/usr/local/bin/sops"
        and "install-sops.sh /usr/local/bin" in DOCKERFILE
    )
    for service in ("openclaw-gateway", "openclaw-cli"):
        block = re.search(
            rf"^  {service}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", COMPOSE, re.M | re.S
        ).group(1)
        assert "- ../secrets:/run/lifekit/secrets:ro" in block, service
        assert "}/gateway:/run/lifekit/gateway:ro" in block, service
        assert ":/run/lifekit/gateway" in block and "/srv/lifekit-secrets}" in block
    assert env["LIFEKIT_SOPS_FILE"] == f"/run/lifekit/secrets/{FILES['gateway'].name}"
    assert env["SOPS_AGE_KEY_FILE"].startswith("/run/lifekit/gateway/")
    assert provider["timeoutMs"] <= 120000


def test_secrets_dir_admits_only_encrypted_files():
    tracked = subprocess.run(
        ["git", "ls-files", "secrets"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for path in tracked:
        assert (
            path.endswith(".env.sops") or path == "secrets/README.md"
        ), f"{path} does not belong in secrets/"
    gitignore = (REPO / ".gitignore").read_text()
    assert "secrets/*\n!secrets/*.env.sops" in gitignore


# ─── the resolver's protocol (stub sops, no keys) ───────────────────────────


def run_resolver(request: dict, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RESOLVER)],
        input=json.dumps(request),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def stub_sops(tmp_path):
    """A fake sops that prints a fixed JSON payload: the protocol is what is under test."""
    payload = {
        "KIT_BOT_TOKEN": "111:aaa",
        "FABLE_BOT_TOKEN": '"quoted"',
        "EMPTY_ONE": "",
    }
    stub = tmp_path / "sops"
    stub.write_text(
        "#!/bin/sh\n[ \"$1\" = --decrypt ] || exit 9\ncat <<'EOF'\n"
        + json.dumps(payload)
        + "\nEOF\n"
    )
    stub.chmod(0o755)
    return {
        "PATH": "/usr/bin:/bin",
        "LIFEKIT_SOPS_BIN": str(stub),
        "LIFEKIT_SOPS_FILE": str(tmp_path / "x.env.sops"),
        "SOPS_AGE_KEY_FILE": str(tmp_path / "k"),
    }


def test_resolver_maps_ids_to_keys_and_unquotes(stub_sops):
    out = run_resolver(
        {
            "protocolVersion": 1,
            "provider": "sops",
            "ids": ["kit-bot-token", "fable-bot-token"],
        },
        stub_sops,
    )
    assert out.returncode == 0, out.stderr
    reply = json.loads(out.stdout)
    assert reply == {
        "protocolVersion": 1,
        "values": {"kit-bot-token": "111:aaa", "fable-bot-token": "quoted"},
        "errors": {},
    }
    assert out.stderr == ""


def test_resolver_reports_not_found_without_leaking(stub_sops):
    ids = ["nope-bot-token", "empty-one", "KIT_BOT_TOKEN", "../etc/passwd"]
    out = run_resolver(
        {"protocolVersion": 1, "provider": "sops", "ids": ids}, stub_sops
    )
    assert out.returncode == 0, out.stderr
    reply = json.loads(out.stdout)
    assert reply["values"] == {}
    assert reply["errors"] == {i: {"code": "NOT_FOUND"} for i in ids}
    assert "111:aaa" not in out.stdout + out.stderr


def test_resolver_rejects_bad_protocol(stub_sops):
    out = run_resolver({"protocolVersion": 2, "ids": ["kit-bot-token"]}, stub_sops)
    assert out.returncode != 0 and out.stdout == ""


def test_resolver_fails_closed_when_sops_fails(stub_sops, tmp_path):
    broken = tmp_path / "sops-broken"
    broken.write_text(
        "#!/bin/sh\necho 'age: no identity matched any of the recipients' >&2\nexit 128\n"
    )
    broken.chmod(0o755)
    out = run_resolver(
        {"protocolVersion": 1, "ids": ["kit-bot-token"]},
        {**stub_sops, "LIFEKIT_SOPS_BIN": str(broken)},
    )
    assert out.returncode != 0 and out.stdout == ""
    assert "no identity matched" in out.stderr


# ─── the real gateway file, with the real gateway key (box runner only) ─────


def test_gateway_file_decrypts_with_the_gateway_key():
    key = Path(
        os.environ.get(
            "LIFEKIT_GATEWAY_AGE_KEY_FILE",
            "/srv/lifekit-secrets/gateway/lifekit-gateway.agekey",
        )
    )
    sops = shutil.which("sops")
    if not sops or not os.access(key, os.R_OK):
        pytest.skip(
            "needs sops on PATH and a readable gateway age key (the box's runner has both)"
        )
    ids = sorted(patch_refs())
    out = run_resolver(
        {"protocolVersion": 1, "provider": "sops", "ids": ids},
        {
            "PATH": os.environ["PATH"],
            "LIFEKIT_SOPS_BIN": sops,
            "LIFEKIT_SOPS_FILE": str(require("gateway")),
            "SOPS_AGE_KEY_FILE": str(key),
        },
    )
    assert out.returncode == 0, out.stderr  # sops' stderr never carries plaintext
    reply = json.loads(out.stdout)
    assert reply["errors"] == {}, sorted(reply["errors"])
    assert set(reply["values"]) == set(ids)
    assert all(
        reply["values"].values()
    ), "an empty bot token would leave that account cold"
