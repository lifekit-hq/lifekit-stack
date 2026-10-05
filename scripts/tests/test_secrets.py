"""The secrets discipline of docs/secrets.md, held by CI on every PR.

No secret value is read, printed or compared here. The two SOPS files are
parsed as text (key names and sops metadata are plaintext by design).

This file holds the tests that the two SOPS files, .sops.yaml and
docs/secrets.md can satisfy on their own - structure, recipients, naming,
and the inventory cross-check. The gateway boundary's platform-patch
wiring, the exec resolver and its protocol, and the on-box
decrypt-with-the-real-gateway-key test (ci.yml, self-hosted runner only)
live outside this file, per docs/secrets-runbook.md's CI section.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[2]
SECRETS = REPO / "secrets"
FILES = {
    "master": SECRETS / "lifekit.env.sops",
    "gateway": SECRETS / "lifekit-gateway.env.sops",
}
INVENTORY = REPO / "docs/secrets.md"
SOPS_YAML = REPO / ".sops.yaml"

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
    """Strict operator-set invariant (docs/secrets.md, "Two files, two keys"):
    the master file's recipients are exactly the operator set {captain,
    firstmate}; the gateway file has that same operator set plus exactly one
    gateway-only identity, which is never a master recipient. So either
    operator can always edit either file, and a compromised gateway never
    reaches the master boundary.
    """
    rules = sops_rules()
    assert set(rules) == {
        "master",
        "gateway",
    }, "each file needs exactly one creation rule"
    assert all(
        AGE_RE.match(a) for ages in rules.values() for a in ages
    ), "recipients must be age public keys"
    operators = rules["master"]
    assert len(operators) == 2, "master: captain + firstmate, nothing else"
    assert (
        operators < rules["gateway"]
    ), "both operators must be able to edit the gateway file"
    # master is exactly the operators, so whatever else the gateway file has
    # is gateway-only by construction: never a master recipient
    assert (
        len(rules["gateway"] - operators) == 1
    ), "gateway: the operators + one gateway-only identity, nothing else"


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


# ─── the secrets/ directory ──────────────────────────────────────────────────


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
