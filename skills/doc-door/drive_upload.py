#!/usr/bin/env python3
"""drive_upload - file the document door's cited reports into Google Drive with
rclone, under a consent of its own that holds the drive.file scope and nothing
else (docs/runbook.md, "Report copies in Drive (rclone)").

    drive_upload.py status                  what is installed and configured
    drive_upload.py install-rclone          the pinned, sha256-checked rclone binary
    drive_upload.py configure [--force]     write the rclone config (0600) from the
                                            client id, client secret and token that
                                            `rclone authorize` printed; prompts, never echoes
    drive_upload.py put <file> <folder> [--name N]
                                            copy one file to <ROOT>/<folder>/<name>
    drive_upload.py verify [uploader|kit]   prove the token holds only drive.file and
                                            round-trips one test file in its own folder,
                                            and that Kit's connector offers no Drive write

door.py calls put() after a report is accepted. Under drive.file Google shows the
app only the files it created itself, so rclone sees one folder, ROOT, which it
makes on first use, and nothing else in the account: this is the whole reason for
the separate consent, next to the fleet connector (which keeps its own Drive
level, docs/google-mcp-setup.md) and Kit's (read-only Drive).

Nothing here prints the config, the client secret or a token. The rclone config
lives in CONFIG (a directory mount from the host secret location, mode 0600);
rclone is run with a minimal environment, an explicit --config and
--drive-scope drive.file on every call, and no verbose flag (debug logging prints
credentials). Stdlib only.
"""

from __future__ import annotations

import configparser
import getpass
import hashlib
import json
import os
import platform
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

SCOPE = "drive.file"
SCOPE_URL = "https://www.googleapis.com/auth/drive.file"
SECTION = "docdoor"

DOOR_HOME = Path(
    os.environ.get("DOC_DOOR_DIR", Path.home() / ".life-state" / "doc-door")
)
# Same rclone as the off-box backup (scripts/offbox-backup/install-offbox-backup.sh):
# bump the version and both sums together, from <base>/<version>/SHA256SUMS.
RCLONE_VERSION = "v1.75.1"
RCLONE_SUMS = {
    "arm64": "03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9",
    "amd64": "982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab",
}
RCLONE_BASE_URL = os.environ.get(
    "DOC_DOOR_RCLONE_BASE_URL", "https://downloads.rclone.org"
)
RCLONE_USER_AGENT = "lifekit-doc-door-drive-upload/1 (rclone installer)"
RCLONE = os.environ.get("DOC_DOOR_RCLONE", str(DOOR_HOME / "bin" / "rclone"))
CONFIG = Path(
    os.environ.get("DOC_DOOR_RCLONE_CONFIG", "/run/lifekit/doc-door-drive/rclone.conf")
)
# Setting DOC_DOOR_RCLONE_REMOTE swaps Drive for any other rclone remote (a local
# directory, ":local:/tmp/rehearsal/"): the rehearsal path that needs no Google.
REMOTE = os.environ.get("DOC_DOOR_RCLONE_REMOTE", f"{SECTION}:")
REHEARSAL = "DOC_DOOR_RCLONE_REMOTE" in os.environ
ROOT = os.environ.get("DOC_DOOR_RCLONE_FOLDER", "Document door reports")
KIT_MCP_URL = os.environ.get("DOC_DOOR_MCP_URL", "http://google-workspace-mcp:8000/mcp")
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

# A Drive tool that creates, changes, shares or removes something. Kit's
# connector is read-only for Drive, so none of these may be offered by it.
DRIVE_WRITE = re.compile(
    r"^(create|update|delete|manage|set|copy|import|share|move|trash|batch)_"
    r".*(drive|to_google_)|^(get_drive_shareable_link)$"
)


class UploadError(RuntimeError):
    pass


def _clean(text: str, limit: int = 160) -> str:
    return re.sub(r"[^\x20-\x7e]+", " ", text).strip()[:limit]


def configured() -> bool:
    """True once there is something to upload to: a config, or a rehearsal remote."""
    return REHEARSAL or CONFIG.is_file()


def _run(args: list[str], timeout: int = 180, data: bytes | None = None):
    binary = Path(RCLONE)
    if not binary.is_file():
        raise UploadError("rclone is not installed (drive_upload.py install-rclone)")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 - rclone needs one
        "LC_ALL": "C",
    }
    cmd = [
        str(binary),
        "--config",
        str(CONFIG),
        "--drive-scope",
        SCOPE,
        "--retries",
        "2",
        "--low-level-retries",
        "3",
        "--contimeout",
        "20s",
        "--timeout",
        "60s",
        *args,
    ]
    try:
        out = subprocess.run(  # noqa: S603 - fixed binary, argv list, no shell
            cmd, capture_output=True, timeout=timeout, env=env, input=data
        )
    except subprocess.TimeoutExpired as exc:
        raise UploadError("rclone timed out") from exc
    if out.returncode != 0:
        lines = out.stderr.decode(errors="replace").strip().splitlines()
        raise UploadError(
            _clean(lines[-1]) if lines else f"rclone exit {out.returncode}"
        )
    return out.stdout


def _segments(path: str) -> list[str]:
    parts = path.split("/")
    if not path or any(p in ("", ".", "..") or p.startswith("-") for p in parts):
        raise UploadError(f"refusing path {path!r}")
    return parts


def remote_path(*parts: str) -> str:
    return REMOTE + "/".join(p.strip("/") for p in parts)


def put(src: Path, folder: str, name: str) -> str:
    """Copy src to <ROOT>/<folder>/<name>; returns that Drive path. Raises UploadError."""
    _segments(folder)
    _segments(name)
    if "/" in name:
        raise UploadError(f"refusing name {name!r}")
    if not Path(src).is_file():
        raise UploadError("nothing to upload")
    _run(["copyto", str(src), remote_path(ROOT, folder, name)])
    return f"{ROOT}/{folder}/{name}"


# --- the pinned rclone ----------------------------------------------------------------


def _arch() -> str:
    machine = platform.machine()
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}.get(machine)
    if arch is None:
        raise UploadError(f"no pinned rclone for {machine}")
    return arch


def _installed_version(binary: Path) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603
            [str(binary), "version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = out.stdout.splitlines()[0].split() if out.stdout else []
    return first[1] if out.returncode == 0 and len(first) > 1 else None


def install_rclone() -> int:
    binary = Path(RCLONE)
    if _installed_version(binary) == RCLONE_VERSION:
        print(f"rclone {RCLONE_VERSION} already in place")
        return 0
    arch = _arch()
    stem = f"rclone-{RCLONE_VERSION}-linux-{arch}"
    url = f"{RCLONE_BASE_URL}/{RCLONE_VERSION}/{stem}.zip"
    # downloads.rclone.org answers 403 to Python's default user agent.
    req = urllib.request.Request(url, headers={"User-Agent": RCLONE_USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310 - pinned host
        blob = resp.read()
    if hashlib.sha256(blob).hexdigest() != RCLONE_SUMS[arch]:
        print(
            f"{stem}.zip does not match its pinned sha256; nothing installed",
            file=sys.stderr,
        )
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="rclone-zip-"))
    try:
        zpath = tmp / "rclone.zip"
        zpath.write_bytes(blob)
        with zipfile.ZipFile(zpath) as z:
            exe = z.read(f"{stem}/rclone")
    finally:
        for f in tmp.iterdir():
            f.unlink()
        tmp.rmdir()
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.parent.chmod(0o700)
    part = binary.with_name(binary.name + ".partial")
    part.write_bytes(exe)
    part.chmod(0o755)
    part.replace(binary)
    print(f"rclone {RCLONE_VERSION} installed")
    return 0


# --- the config -----------------------------------------------------------------------


def _read_config() -> configparser.SectionProxy:
    cp = configparser.RawConfigParser(interpolation=None)
    cp.read_string(CONFIG.read_text())
    return cp[SECTION]


def _token_json(text: str) -> dict:
    """The JSON object in what `rclone authorize` printed (markers and all)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise UploadError("no token JSON in the pasted text")
    try:
        token = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise UploadError("the pasted token is not valid JSON") from exc
    if not isinstance(token, dict) or not token.get("refresh_token"):
        raise UploadError(
            "the token carries no refresh_token (offline access was not granted)"
        )
    return token


def configure(force: bool, read=None) -> int:
    if CONFIG.exists() and not force:
        print(f"{CONFIG} exists; pass --force to replace it", file=sys.stderr)
        return 1
    if not CONFIG.parent.is_dir() or not os.access(CONFIG.parent, os.W_OK):
        print(
            f"{CONFIG.parent} is missing or not writable for this user: create it on the "
            "host first (docs/runbook.md, 'Report copies in Drive (rclone)')",
            file=sys.stderr,
        )
        return 1
    if read is None:
        tty = sys.stdin.isatty()

        def read(label: str, secret: bool) -> str:
            if tty:
                return (getpass.getpass if secret else input)(f"{label}: ")
            return sys.stdin.readline()

    client_id = read("OAuth client id", False).strip()
    client_secret = read("OAuth client secret", True).strip()
    token = _token_json(read("token JSON from `rclone authorize`", True))
    for label, value in (("client id", client_id), ("client secret", client_secret)):
        if not value or re.search(r"\s", value):
            print(f"the {label} is empty or has whitespace in it", file=sys.stderr)
            return 1
    body = (
        f"[{SECTION}]\ntype = drive\nclient_id = {client_id}\n"
        f"client_secret = {client_secret}\nscope = {SCOPE}\n"
        f"token = {json.dumps(token, separators=(',', ':'))}\n"
    )
    tmp = CONFIG.with_name(CONFIG.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    tmp.chmod(0o600)
    tmp.replace(CONFIG)
    print(f"wrote {CONFIG} (mode 0600, scope {SCOPE}); next: drive_upload.py verify")
    return 0


def status() -> int:
    ver = _installed_version(Path(RCLONE))
    print(f"rclone: {ver or 'not installed'} (pinned {RCLONE_VERSION})")
    if REHEARSAL:
        print(f"remote: {REMOTE} (rehearsal, no Google)")
    elif CONFIG.is_file():
        mode = stat.S_IMODE(CONFIG.stat().st_mode)
        print(f"config: present, mode {mode:04o}; remote {REMOTE}, folder '{ROOT}'")
    else:
        print(
            f"config: absent ({CONFIG}); report copies are off until the one-time "
            "consent is in (docs/runbook.md, 'Report copies in Drive (rclone)')"
        )
    return 0


# --- verify ---------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.failed = False

    def ok(self, msg: str) -> None:
        print(f"PASS  {msg}")

    def info(self, msg: str) -> None:
        print(f"INFO  {msg}")

    def fail(self, msg: str) -> None:
        self.failed = True
        print(f"FAIL  {msg}")


def token_scopes(access_token: str, client_id: str) -> tuple[set[str], str]:
    """(scopes, client the token was issued to) as Google reports them."""
    body = urllib.parse.urlencode({"access_token": access_token}).encode()
    req = urllib.request.Request(TOKENINFO_URL, body)  # noqa: S310 - fixed https URL
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            info = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise UploadError(f"Google rejected the token (HTTP {exc.code})") from exc
    except (OSError, ValueError) as exc:
        raise UploadError(f"tokeninfo unreachable: {_clean(str(exc), 80)}") from exc
    return set(str(info.get("scope", "")).split()), str(
        info.get("azp") or info.get("aud") or ""
    )


def _check_config(rep: Report) -> dict | None:
    if not CONFIG.is_file():
        rep.fail(f"no rclone config at {CONFIG}: the one-time consent is not in yet")
        return None
    mode = stat.S_IMODE(CONFIG.stat().st_mode)
    if mode & 0o077:
        rep.fail(f"config mode is {mode:04o}, want 0600")
    else:
        rep.ok("config mode 0600")
    try:
        sect = _read_config()
        token = json.loads(sect.get("token", ""))
    except (KeyError, ValueError, configparser.Error):
        rep.fail(f"config has no usable [{SECTION}] section with a token")
        return None
    if sect.get("type") != "drive":
        rep.fail(f"remote type is {sect.get('type')!r}, want drive")
    if sect.get("scope") == SCOPE:
        rep.ok(f"config requests scope {SCOPE} only")
    else:
        rep.fail(f"config scope is {sect.get('scope')!r}, want exactly {SCOPE!r}")
    if not token.get("refresh_token"):
        rep.fail("config token has no refresh_token")
    return {"client_id": sect.get("client_id", "")}


def _roundtrip(rep: Report) -> None:
    nonce = secrets.token_hex(8)
    name = f"verify-{nonce}.txt"
    payload = f"document door verify {nonce}\n".encode()
    rel = f"_verify/{name}"
    with tempfile.TemporaryDirectory(prefix="dd-verify-") as tmp:
        src = Path(tmp) / name
        src.write_bytes(payload)
        src.chmod(0o600)
        try:
            _run(["copyto", str(src), remote_path(ROOT, rel)])
            back = _run(["cat", remote_path(ROOT, rel)])
        except UploadError as exc:
            rep.fail(f"create/read-back in '{ROOT}/_verify' failed: {exc}")
            return
    if back != payload:
        rep.fail("the test file read back differently from what was written")
    else:
        rep.ok(
            f"created and read back one test file in its own folder '{ROOT}/_verify'"
        )
    try:
        _run(["deletefile", remote_path(ROOT, rel)])
        rep.ok("removed the test file")
    except UploadError as exc:
        rep.info(f"test file left in place ({exc})")
    try:
        names = [
            ln.strip().rstrip("/")
            for ln in _run(["lsf", REMOTE, "--max-depth", "1"]).decode().splitlines()
            if ln.strip()
        ]
    except UploadError as exc:
        rep.fail(f"could not list the account root: {exc}")
        return
    stray = [n for n in names if n != ROOT]
    if stray:
        rep.fail(
            f"the account root shows {len(stray)} item(s) besides '{ROOT}': "
            "a drive.file token sees only what this client created"
        )
    else:
        rep.ok(f"the account root shows only '{ROOT}'")


def _check_token(rep: Report, client_id: str) -> None:
    try:
        access = json.loads(_read_config()["token"]).get("access_token", "")
        scopes, azp = token_scopes(access, client_id)
    except (UploadError, KeyError, ValueError) as exc:
        rep.fail(f"could not read the token's scope back from Google: {exc}")
        return
    if scopes == {SCOPE_URL}:
        rep.ok(f"Google reports the token's scope as {SCOPE} only")
    else:
        rep.fail(
            f"the token holds {len(scopes)} scope(s), want exactly {SCOPE}: {sorted(scopes)}"
        )
    if client_id and azp and azp != client_id:
        rep.fail(
            "the token was issued to a different OAuth client than the config names"
        )


def verify_uploader(rep: Report) -> None:
    if REHEARSAL:
        rep.info(
            f"REHEARSAL on remote {REMOTE}: no Google access, so the token's scope is "
            "not checked here"
        )
        _roundtrip(rep)
        return
    cfg = _check_config(rep)
    if cfg is None:
        return
    _roundtrip(rep)  # first, so rclone has refreshed and saved the access token
    _check_token(rep, cfg["client_id"])


def kit_tool_names() -> list[str]:
    import door  # the sibling module: its MCP client

    mcp = door.Mcp(KIT_MCP_URL)
    names: list[str] = []
    cursor = None
    for _ in range(20):
        res = mcp._rpc("tools/list", {"cursor": cursor} if cursor else {})  # noqa: SLF001
        names += [t["name"] for t in res.get("tools", [])]
        cursor = res.get("nextCursor")
        if not cursor:
            break
    return names


def verify_kit(rep: Report) -> None:
    try:
        names = kit_tool_names()
    except Exception as exc:  # noqa: BLE001
        rep.fail(f"could not list Kit's connector tools: {_clean(str(exc), 100)}")
        return
    drive = sorted(n for n in names if "drive" in n)
    if not drive:
        rep.fail(
            "Kit's connector offered no Drive tool at all: cannot show its Drive level"
        )
        return
    writes = sorted(n for n in names if DRIVE_WRITE.search(n))
    if writes:
        rep.fail(f"Kit's connector offers Drive write tools: {', '.join(writes)}")
    else:
        rep.ok(
            f"Kit's connector Drive is read-only ({len(drive)} Drive tools, none writes)"
        )


def verify(which: str | None) -> int:
    rep = Report()
    if which in (None, "uploader"):
        verify_uploader(rep)
    if which in (None, "kit"):
        verify_kit(rep)
    print("VERIFY FAILED" if rep.failed else "VERIFY OK")
    return 1 if rep.failed else 0


def main(argv: list[str]) -> int:
    cmd, rest = (argv[1], argv[2:]) if len(argv) > 1 else ("", [])
    if cmd == "status" and not rest:
        return status()
    if cmd == "install-rclone" and not rest:
        return install_rclone()
    if cmd == "configure" and rest in ([], ["--force"]):
        return configure(bool(rest))
    if cmd == "verify" and rest in ([], ["uploader"], ["kit"]):
        return verify(rest[0] if rest else None)
    if cmd == "put" and len(rest) in (2, 4) and (len(rest) == 2 or rest[2] == "--name"):
        src = Path(rest[0])
        try:
            print(put(src, rest[1], rest[3] if len(rest) == 4 else src.name))
        except UploadError as exc:
            print(f"upload failed: {exc}", file=sys.stderr)
            return 1
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
