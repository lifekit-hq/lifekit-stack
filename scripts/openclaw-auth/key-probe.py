#!/usr/bin/python3
"""Live probe of the OpenClaw Anthropic API key, run INSIDE the gateway container.

auth-mode.sh pipes this file into `docker exec -i <gateway> python3 -`. It resolves
the SecretRef `anthropic-api-key` through the same sops resolver the gateway uses
and sends one 1-token Messages API request, so it works while the gateway is up
(`openclaw models status --probe` refuses to run then). The key value stays in
this process: only a one-line JSON verdict is printed.

    {"result": "ok|rejected|exhausted|limited|unknown|no-key", "http": 401, "detail": "..."}

ok         the request was accepted (HTTP 200)
limited    HTTP 429: the key is valid, the rate limit is not
rejected   HTTP 401/403: invalid, revoked or not permitted
exhausted  HTTP 400 naming the credit balance
unknown    anything else (network error, 5xx)
no-key     the resolver has no value for the ref
"""

import json
import os
import subprocess
import urllib.error
import urllib.request

REF = os.environ.get("PROBE_REF", "anthropic-api-key")
MODEL = os.environ.get("PROBE_MODEL", "claude-haiku-4-5")
RESOLVER_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LIFEKIT_SOPS_BIN": "/usr/local/bin/sops",
    "LIFEKIT_SOPS_FILE": "/run/lifekit/secrets/lifekit-gateway.env.sops",
    "SOPS_AGE_KEY_FILE": "/run/lifekit/gateway/lifekit-gateway.agekey",
}


def verdict(result, http=None, detail=""):
    print(json.dumps({"result": result, "http": http, "detail": detail[:160]}))


def resolve_key():
    request = {"protocolVersion": 1, "provider": "sops", "ids": [REF]}
    try:
        proc = subprocess.run(
            ["/opt/lifekit-secrets/sops-resolver.py"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            env=RESOLVER_ENV,
            timeout=30,
            check=False,
        )
        return json.loads(proc.stdout).get("values", {}).get(REF)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def main():
    key = resolve_key()
    if not key:
        return verdict("no-key")
    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return verdict("ok", resp.status)
    except urllib.error.HTTPError as err:
        try:
            error = json.loads(err.read() or b"{}").get("error", {})
        except ValueError:
            error = {}
        message = str(error.get("message", ""))
        if err.code in (401, 403):
            return verdict("rejected", err.code, str(error.get("type", "")))
        if err.code == 429:
            return verdict("limited", err.code, str(error.get("type", "")))
        if err.code == 400 and "credit balance" in message.lower():
            return verdict("exhausted", err.code, "credit balance too low")
        return verdict("unknown", err.code, str(error.get("type", "")))
    except (OSError, ValueError) as err:
        return verdict("unknown", None, type(err).__name__)


main()
