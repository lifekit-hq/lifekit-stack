#!/usr/bin/env python3
"""reply-pusher - tell Kit when firstmate has replied to one of the owner's notes.

Kit's second-mate relay is pull-only: a reply waits in the firstmate home until
Kit next runs `relay.sh replies`. This closes the gap. When a reply lands in a
pinned home, a systemd .path unit starts this oneshot; it finds the new reply,
and, only when the note it answers was sent through Kit (request id `kit-*`),
POSTs one identifiers-only turn to the gateway's `firstmate-reply` inbound hook
(compose/openclaw-gateway/platform.patch.json). Kit's skill then fetches and
says the reply itself.

Hard limits, each pinned by scripts/tests/test_reply_pusher.py:

- It reads a home only through `fm-inbox.sh receipts`; it never opens, writes
  or locks anything inside a firstmate home.
- The hook payload and the on-disk outbox carry identifiers only
  (home, note id, reply cursor). A note or reply body is held in memory long
  enough to read the note's request id and is never persisted, sent or logged.
- It reads exactly two keys from the rendered env at run time
  (OPENCLAW_HOOK_TOKEN, OPENCLAW_HOOK_PATH), keeps them in memory, and never
  logs them or anything derived from them (the hook URL carries the path).
- Delivery is at-least-once with a stable Idempotency-Key (`<home>:<cursor>`),
  so a retry after an ambiguous failure cannot make a second turn.

Modes (first argument):
  run       enqueue new replies per home, then drain the outbox (default)
  baseline  write the starting cursor for every home that has none, at the
            current newest reply, so no history replays; never moves an
            existing cursor

Environment:
  REPLY_PUSHER_HOMES       space-separated absolute paths of the firstmate homes
  STATE_DIRECTORY          state root (systemd sets it); REPLY_PUSHER_STATE as fallback
  REPLY_PUSHER_STACK_ENV   rendered env file (default /srv/lifekit-secrets/stack.env)
  REPLY_PUSHER_GATEWAY     gateway origin (default http://127.0.0.1:18894,
                           the edge's loopback hook route)
  REPLY_PUSHER_TIMEOUT     seconds per POST (default 30)

Exit: 0 all homes clean; 1 some home failed (systemd retries with backoff);
78 not configured or not baselined (retrying cannot help).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# request-id prefix -> hook name under the hooks path. A prefix not listed here
# (dash-, fs-, fleet-, term-, anything else) and a note with no request id are
# skipped: only the owner's own notes sent through Kit are ever pushed.
ROUTES: dict[str, str] = {"kit-": "firstmate-reply"}

STACK_ENV = "/srv/lifekit-secrets/stack.env"
# The edge's `openclaw-hook` entrypoint (compose/edge/traefik/openclaw.yml): the
# gateway's own loopback port is not published in trusted-proxy mode.
DEFAULT_GATEWAY = "http://127.0.0.1:18894"
TOKEN_KEY = "OPENCLAW_HOOK_TOKEN"
PATH_KEY = "OPENCLAW_HOOK_PATH"
ENV_KEYS = (TOKEN_KEY, PATH_KEY)
RECEIPTS_SCHEMA = "fm-inbox-receipts.v1"
ZERO_CURSOR = "000000000000"
MAX_PASSES = 10

HOME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
NOTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CURSOR_RE = re.compile(r"^[0-9]{12}$")
HOME_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
HOOK_PATH_RE = re.compile(r"^/?[A-Za-z0-9._~/-]+$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
GATEWAY_RE = re.compile(r"^http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?$")


class NotConfigured(Exception):
    """Retrying cannot fix this; an operator step is missing."""


class PushFailed(Exception):
    """A POST did not land; the outbox entry stays for the next run."""


def log(message: str) -> None:
    print(f"reply-pusher: {message}", file=sys.stderr, flush=True)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_env(path: str) -> dict[str, str]:
    """Only the two hook keys; the rest of the file is never kept."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        key = key.removeprefix("export ").strip()
        if not sep or key not in ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def hook_url(gateway: str, hook_path: str, hook: str) -> str:
    return f"{gateway}/{hook_path.strip('/')}/{hook}"


def route_for(request_id: str | None) -> str | None:
    """The hook a note's replies go to, or None when they are not pushed."""
    for prefix, hook in ROUTES.items():
        if request_id and request_id.startswith(prefix):
            return hook
    return None


class Home:
    def __init__(self, path: str, state_root: Path) -> None:
        if not HOME_PATH_RE.match(path):
            raise NotConfigured("a home path must be absolute, of [A-Za-z0-9._/-]")
        self.path = path
        self.id = Path(path).name
        if not HOME_RE.match(self.id):
            raise NotConfigured("a home's directory name must match [a-z0-9][a-z0-9-]*")
        self.dir = state_root / self.id
        self.cursor_file = self.dir / "cursor"
        self.outbox = self.dir / "outbox"

    def receipts(self, after: str | None) -> dict[str, Any]:
        cmd = [f"{self.path}/bin/fm-inbox.sh", "receipts"]
        if after:
            cmd += ["--after", after]
        cmd += ["--all-pending", "--all-handled", "--all-replies"]
        # Nothing inherited: the unit's environment is never handed to fm-inbox.
        env = {
            "HOME": os.environ.get("HOME", "/"),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "FM_HOME": self.path,
        }
        proc = subprocess.run(
            cmd, env=env, capture_output=True, check=False, timeout=60
        )
        if proc.returncode != 0:
            # stderr could quote a note; report the exit status only.
            raise PushFailed(f"{self.id}: fm-inbox receipts exit {proc.returncode}")
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise PushFailed(f"{self.id}: fm-inbox receipts is not JSON") from None
        if (
            not isinstance(data, dict)
            or data.get("schema") != RECEIPTS_SCHEMA
            or not all(
                k in data for k in ("pending", "handled", "replies", "reply_cursor")
            )
        ):
            raise PushFailed(f"{self.id}: unexpected fm-inbox receipts shape")
        return data

    def load_cursor(self) -> str:
        try:
            cursor = self.cursor_file.read_text().strip()
        except FileNotFoundError:
            raise NotConfigured(
                f"{self.id}: no cursor; run the installer (baseline) first"
            ) from None
        if not CURSOR_RE.match(cursor):
            raise NotConfigured(f"{self.id}: cursor file is not 12 digits")
        return cursor

    def baseline(self) -> str:
        if self.cursor_file.exists():
            return "kept"
        data = self.receipts(None)
        cursor = data["reply_cursor"] or ZERO_CURSOR
        if not CURSOR_RE.match(cursor):
            raise PushFailed(f"{self.id}: receipts returned no usable reply cursor")
        write_atomic(self.cursor_file, cursor + "\n")
        return "set"

    def enqueue(self) -> int:
        """Outbox first, then the cursor: a crash between them re-enqueues the
        same entries (same file names), never loses one."""
        cursor = self.load_cursor()
        data = self.receipts(cursor)
        request_ids = {
            n.get("id"): n.get("request_id") for n in data["pending"] + data["handled"]
        }
        queued = 0
        newest = cursor
        for reply in data["replies"]:
            note_id, reply_cursor = reply.get("id"), reply.get("cursor")
            if not (isinstance(note_id, str) and isinstance(reply_cursor, str)):
                raise PushFailed(f"{self.id}: malformed reply record")
            if not CURSOR_RE.match(reply_cursor) or reply_cursor <= cursor:
                continue
            newest = max(newest, reply_cursor)
            hook = route_for(request_ids.get(note_id))
            if hook is None:
                continue
            if not NOTE_ID_RE.match(note_id):
                log(
                    f"{self.id}: skipped a reply with an unusable note id at {reply_cursor}"
                )
                continue
            entry = {
                "home": self.id,
                "noteId": note_id,
                "cursor": reply_cursor,
                "hook": hook,
            }
            write_atomic(self.outbox / f"{reply_cursor}.json", json.dumps(entry) + "\n")
            queued += 1
        if newest != cursor:
            write_atomic(self.cursor_file, newest + "\n")
        return queued

    def entries(self) -> list[Path]:
        if not self.outbox.is_dir():
            return []
        return sorted(
            p for p in self.outbox.iterdir() if re.fullmatch(r"[0-9]{12}\.json", p.name)
        )


class Poster:
    """Reads the two secrets once, lazily, and POSTs to the gateway."""

    def __init__(self, gateway: str, stack_env: str, timeout: float) -> None:
        self.gateway = gateway
        self.stack_env = stack_env
        self.timeout = timeout
        self._secrets: tuple[str, str] | None = None
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def secrets(self) -> tuple[str, str]:
        if self._secrets is None:
            env = read_env(self.stack_env)
            token, hook_path = env.get(TOKEN_KEY, ""), env.get(PATH_KEY, "")
            if not TOKEN_RE.match(token):
                raise NotConfigured(
                    f"{TOKEN_KEY} is missing or malformed in the env file"
                )
            if not HOOK_PATH_RE.match(hook_path):
                raise NotConfigured(
                    f"{PATH_KEY} is missing or malformed in the env file"
                )
            self._secrets = (token, hook_path)
        return self._secrets

    def post(self, entry: dict[str, str]) -> None:
        token, hook_path = self.secrets()
        payload = {k: entry[k] for k in ("home", "noteId", "cursor")}
        request = urllib.request.Request(
            hook_url(self.gateway, hook_path, entry["hook"]),
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Idempotency-Key": f"{entry['home']}:{entry['cursor']}",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as err:
            status = err.code
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            # Class only: a URLError's text can carry the URL, which holds the path.
            raise PushFailed(f"transport error ({type(err).__name__})") from None
        if not 200 <= status < 300:
            raise PushFailed(f"gateway answered {status}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def drain(home: Home, poster: Poster) -> int:
    pushed = 0
    for path in home.entries():
        try:
            entry = json.loads(path.read_text())
            valid = (
                HOME_RE.match(entry["home"])
                and entry["home"] == home.id
                and NOTE_ID_RE.match(entry["noteId"])
                and CURSOR_RE.match(entry["cursor"])
                and entry["hook"] in ROUTES.values()
                and path.name == f"{entry['cursor']}.json"
            )
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise NotConfigured(f"{home.id}: outbox entry {path.name} is malformed")
        poster.post(entry)
        path.unlink()
        pushed += 1
        log(f"{home.id}: pushed reply to note {entry['noteId']} at {entry['cursor']}")
    return pushed


def run_home(home: Home, poster: Poster) -> None:
    """Enqueue, then drain, until a pass finds nothing: a reply that lands as a
    pass ends is caught by the next one. A failed enqueue still drains what is
    already queued."""
    for _ in range(MAX_PASSES):
        failure: PushFailed | None = None
        queued = pushed = 0
        try:
            queued = home.enqueue()
        except PushFailed as err:
            failure = err
        try:
            pushed = drain(home, poster)
        except PushFailed as err:
            failure = failure or err
        if failure is not None:
            raise failure
        if queued == 0 and pushed == 0:
            return
    log(f"{home.id}: still finding replies after {MAX_PASSES} passes; will rerun")
    raise PushFailed(f"{home.id}: not settled")


def state_root() -> Path:
    root = os.environ.get("STATE_DIRECTORY") or os.environ.get("REPLY_PUSHER_STATE")
    if not root:
        raise NotConfigured("STATE_DIRECTORY (or REPLY_PUSHER_STATE) is not set")
    return Path(root.split(":")[0])


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "run"
    if mode not in ("run", "baseline"):
        print("usage: reply_pusher.py [run|baseline]", file=sys.stderr)
        return 64
    try:
        paths = os.environ.get("REPLY_PUSHER_HOMES", "").split()
        if not paths:
            raise NotConfigured("REPLY_PUSHER_HOMES is empty")
        root = state_root()
        homes = [Home(p, root) for p in paths]
        if len({h.id for h in homes}) != len(homes):
            raise NotConfigured("two homes share a directory name")
        gateway = os.environ.get("REPLY_PUSHER_GATEWAY", DEFAULT_GATEWAY)
        if not GATEWAY_RE.match(gateway):
            raise NotConfigured("REPLY_PUSHER_GATEWAY must be a loopback http origin")
        try:
            timeout = float(os.environ.get("REPLY_PUSHER_TIMEOUT", "30"))
        except ValueError:
            raise NotConfigured("REPLY_PUSHER_TIMEOUT is not a number") from None
        poster = Poster(
            gateway, os.environ.get("REPLY_PUSHER_STACK_ENV", STACK_ENV), timeout
        )
    except NotConfigured as err:
        log(f"not configured: {err}")
        return 78

    failed = False
    for home in homes:
        try:
            if mode == "baseline":
                log(f"{home.id}: baseline cursor {home.baseline()}")
            else:
                run_home(home, poster)
        except NotConfigured as err:
            log(f"not configured: {err}")
            return 78
        except (PushFailed, subprocess.TimeoutExpired, OSError) as err:
            log(
                f"failed: {err}"
                if isinstance(err, PushFailed)
                else f"{home.id}: failed: {type(err).__name__}"
            )
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
