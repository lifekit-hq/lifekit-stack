#!/usr/bin/python3
"""OpenClaw exec SecretRef resolver backed by one SOPS-encrypted dotenv file.

Speaks the exec provider protocol (OpenClaw docs, gateway/secrets/secretref-contract):
    stdin   {"protocolVersion": 1, "provider": "sops", "ids": ["kit-bot-token", ...]}
    stdout  {"protocolVersion": 1, "values": {...}, "errors": {"<id>": {"code": "NOT_FOUND"}}}

Every id is the dotenv key in lower-kebab form: exec ref ids may not contain
underscores, so ``kit-bot-token`` names ``KIT_BOT_TOKEN`` (docs/secrets.md,
"Naming"). The file is decrypted ONCE per request with the key the gateway
owns, and a value only ever leaves this process on stdout inside the protocol
reply - never on stderr, never in an error message.

Environment (all set by the provider's ``env`` map in
compose/openclaw-gateway/platform.patch.json; the child env is otherwise empty):
    LIFEKIT_SOPS_BIN    absolute path of the sops binary
    LIFEKIT_SOPS_FILE   the encrypted dotenv file (secrets/lifekit-gateway.env.sops)
    SOPS_AGE_KEY_FILE   the gateway's age identity, read by sops itself

Stdlib only, no third-party imports: this file is copied into the gateway
image and also exercised by scripts/tests/test_secrets.py on the host.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


def key_for(ref_id: str) -> str | None:
    """kit-bot-token -> KIT_BOT_TOKEN; anything outside the convention is unknown."""
    if not ID_RE.fullmatch(ref_id):
        return None
    return ref_id.upper().replace("-", "_")


def unquote(value: str) -> str:
    """Strip one pair of matching surrounding quotes, as an env-file parser would."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def decrypt(sops_bin: str, sops_file: str, env: dict[str, str]) -> dict[str, str]:
    result = subprocess.run(
        [
            sops_bin,
            "--decrypt",
            "--input-type",
            "dotenv",
            "--output-type",
            "json",
            sops_file,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # sops' own stderr never carries plaintext; forward it for the audit log.
        sys.stderr.write(result.stderr)
        raise SystemExit(f"sops-resolver: sops exited {result.returncode}")
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise SystemExit("sops-resolver: decrypted payload is not an object")
    return {str(k): str(v) for k, v in data.items()}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as err:
        raise SystemExit(f"sops-resolver: bad request JSON: {err}") from None
    if request.get("protocolVersion") != 1:
        raise SystemExit("sops-resolver: protocolVersion must be 1")
    ids = request.get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise SystemExit("sops-resolver: ids must be a list of strings")

    sops_bin = os.environ.get("LIFEKIT_SOPS_BIN") or "sops"
    sops_file = os.environ.get("LIFEKIT_SOPS_FILE")
    if not sops_file:
        raise SystemExit("sops-resolver: LIFEKIT_SOPS_FILE is not set")
    child_env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "SOPS_AGE_KEY_FILE", "HOME")
    }

    values: dict[str, str] = {}
    errors: dict[str, dict[str, str]] = {}
    secrets = decrypt(sops_bin, sops_file, child_env) if ids else {}
    for ref_id in ids:
        key = key_for(ref_id)
        if key is None or key not in secrets or secrets[key] == "":
            errors[ref_id] = {"code": "NOT_FOUND"}
            continue
        values[ref_id] = unquote(secrets[key])

    sys.stdout.write(
        json.dumps({"protocolVersion": 1, "values": values, "errors": errors})
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
