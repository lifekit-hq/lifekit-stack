#!/usr/bin/env python3
"""kit_decide.py - the kit-relay `decide` verb's helper (lifekit-stack#727).

The forced command (scripts/kit-relay/kit-relay) runs this under `env -i` with
the request id on argv, the JSON envelope on stdin and these pinned variables:

  FLEET_POLICY      the fleet-policy executable the installer pinned
  FM_HOME           the home the verb acts on (resolved from the pinned list)
  KIT_DECIDE_ACTOR  who files: kit, openclaw or devclaw, set by the key's
                    command= argument, never by the client
  KIT_DECIDE_STATE  where request records live (one per request id)

It turns the envelope into one `fleet-policy decide` argv (no shell), runs it,
and prints one verdict line, `kit-relay-decide.v1`:

  filed          a Tier 2 call held for the owner; `id` is the call's id
  recorded       a self call, answered from the record (kind self)
  refused-ruled  a ruling answers it; `class` is the class to act under
  refused-repeat the owner already answered it; act on that answer
  refused        fleet-policy refused the call as malformed; `message` says why

Exit 0 for filed and recorded, 2 for every refusal (fleet-policy's own code),
65 for an envelope refused here, otherwise fleet-policy's exit code. A request
id seen before returns its recorded verdict without running fleet-policy again
(an `error` verdict is not recorded, so a retry runs again); the same id with a
different body is refused (65).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys

SCHEMA = "kit-relay-decide.v1"
KINDS = ("call", "self")
# Envelope key -> fleet-policy decide flag. Scalars are one string each.
SCALARS = {
    "class": "--class",
    "subject": "--subject",
    "rec": "--rec",
    "boundary": "--boundary",
    "step": "--step",
    "conflict": "--conflict",
    "no_row": "--no-row",
    "title": "--title",
    "reason": "--reason",
    "question": "--question",
    "answer": "--answer",
    "record": "--record",
}
KEYS = {"kind", "rows", "checked", "options", *SCALARS}
# Keys a client might send to pick who it is or where it files: the key and the
# command word decide those, so naming one is refused rather than ignored.
PINNED = {
    "actor": "the key names the actor",
    "home": "the command's <home> word names the home",
    "id": "the request id is the command's argument",
}
MAX_TEXT = 4000
MAX_DETAIL = 2000


class Refused(Exception):
    pass


def text(key, value):
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{key} must be a non-empty string")
    if len(value) > MAX_TEXT:
        raise Refused(f"{key} is over {MAX_TEXT} characters")
    return value


def strings(key, value):
    """A string or a list of strings (rows may also be one comma-joined string)."""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items:
        raise Refused(f"{key} must be a string or a non-empty list of strings")
    return [text(key, v) for v in items]


def argv_of(
    env: dict, fleet_policy: str, actor: str, home: str, rid: str
) -> tuple[list[str], dict]:
    """(argv, normalized fields) for one envelope; Refused when the envelope is not one."""
    if not isinstance(env, dict):
        raise Refused("the envelope must be one JSON object")
    for k in env:
        if k in PINNED:
            raise Refused(f"'{k}' is not the client's to set: {PINNED[k]}")
        if k not in KEYS:
            raise Refused(f"unknown key '{k}' (allowed: {', '.join(sorted(KEYS))})")
    kind = env.get("kind")
    if kind not in KINDS:
        raise Refused(f"kind must be one of {', '.join(KINDS)}")
    fields = {"kind": kind}
    for k in SCALARS:
        if k in env:
            fields[k] = text(k, env[k])
    if kind == "call":
        # boundary alone makes fleet-policy class the call unlisted; a conflict or
        # no-row call without a class is the unlisted class too.
        fields.setdefault("class", "unlisted")
    if "rows" in env:
        fields["rows"] = ",".join(r.strip() for r in strings("rows", env["rows"]))
    if "checked" in env:
        fields["checked"] = strings("checked", env["checked"])
    if "options" in env:
        opts = env["options"]
        if not isinstance(opts, dict) or not opts:
            raise Refused("options must be an object of option id -> label")
        fields["options"] = {
            text("options", i): text("options", label) for i, label in opts.items()
        }
        for i in fields["options"]:
            if "=" in i:
                raise Refused(f"option id '{i}' holds '='")
    argv = [
        fleet_policy,
        "decide",
        f"--kind={kind}",
        f"--actor={actor}",
        f"--home={home}",
    ]
    for k, flag in SCALARS.items():
        if k in fields:
            argv.append(f"{flag}={fields[k]}")
    if "rows" in fields:
        argv.append(f"--rows={fields['rows']}")
    argv += [f"--checked={c}" for c in fields.get("checked", [])]
    argv += [f"--option={i}={label}" for i, label in fields.get("options", {}).items()]
    argv += ["--", rid]
    return argv, fields


def p16_path() -> str:
    """fleet-policy's verdict log at its default state dir (env -i leaves FLEET_POLICY_STATE unset)."""
    return os.path.join(os.path.expanduser("~/.local/state/fleet-policy"), "p16.jsonl")


def size_of(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def p16_line(
    path: str, offset: int, actor: str, rid: str, subject: str | None
) -> dict | None:
    """The last verdict line this run appended: same actor, and this call's id or subject."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read()
    except OSError:
        return None
    hit = None
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("actor") != actor:
            continue
        if e.get("task") == rid or (
            subject is not None and e.get("subject") == subject.strip()
        ):
            hit = e
    return hit


def clip(s: str) -> str:
    s = s.strip()
    return s if len(s) <= MAX_DETAIL else s[: MAX_DETAIL - 3] + "..."


def verdict_of(
    rc: int, kind: str, line: dict | None, out: str, err: str
) -> tuple[dict, int]:
    message = clip(
        "\n".join(x.removeprefix("decide: refused: ") for x in err.strip().splitlines())
    )
    if rc == 0:
        v = {"verdict": "filed" if kind == "call" else "recorded"}
        if line:
            v.update(
                {
                    k: line[k]
                    for k in ("class", "cell", "ruling", "rows")
                    if line.get(k) not in (None, "", [])
                }
            )
        if out.strip():
            v["detail"] = clip(out)
        return v, 0
    if rc == 2 and line and line.get("outcome") in ("refused-ruled", "refused-repeat"):
        v = {
            "verdict": line["outcome"],
            "class": line.get("ruled_by") or line.get("class"),
            "cell": line.get("cell"),
            "ruling": line.get("ruling"),
            "message": message,
        }
        return v, 2
    if rc == 2:
        return {"verdict": "refused", "message": message}, 2
    return {"verdict": "error", "exit": rc, "message": message}, rc or 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--request-id", required=True)
    rid = ap.parse_args().request_id
    actor = os.environ["KIT_DECIDE_ACTOR"]
    home = os.environ["FM_HOME"]
    fleet_policy = os.environ["FLEET_POLICY"]
    state = os.environ["KIT_DECIDE_STATE"]
    body = sys.stdin.buffer.read()
    sha = hashlib.sha256(body).hexdigest()
    head = {
        "schema": SCHEMA,
        "request_id": rid,
        "actor": actor,
        "home": os.path.basename(home),
    }

    os.makedirs(state, mode=0o700, exist_ok=True)
    with open(os.path.join(state, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = os.path.join(state, rid + ".json")
        if os.path.exists(record):
            with open(record) as f:
                rec = json.load(f)
            if rec.get("sha256") != sha:
                print(
                    f"kit-relay: refused: request id {rid} was used for a different envelope",
                    file=sys.stderr,
                )
                return 65
            print(
                json.dumps(
                    {**rec["verdict"], "replay": True},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            return rec["exit"]
        try:
            argv, fields = argv_of(json.loads(body), fleet_policy, actor, home, rid)
        except ValueError as e:  # json.JSONDecodeError, Refused is not one
            print(f"kit-relay: refused: the envelope is not JSON: {e}", file=sys.stderr)
            return 65
        except Refused as e:
            print(f"kit-relay: refused: {e}", file=sys.stderr)
            return 65
        log = p16_path()
        offset = size_of(log)
        r = subprocess.run(argv, capture_output=True, text=True, cwd=home, check=False)
        line = p16_line(log, offset, actor, rid, fields.get("subject"))
        v, code = verdict_of(r.returncode, fields["kind"], line, r.stdout, r.stderr)
        if v["verdict"] in ("filed", "recorded"):
            v["id"] = rid
        verdict = {**head, **v}
        if (
            v["verdict"] != "error"
        ):  # a failed run is retried under the same id, never replayed
            tmp = record + ".tmp"
            with open(tmp, "w") as f:
                json.dump(
                    {"sha256": sha, "exit": code, "verdict": verdict},
                    f,
                    ensure_ascii=False,
                )
            os.replace(tmp, record)
    print(json.dumps(verdict, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
