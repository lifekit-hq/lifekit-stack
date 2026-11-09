"""The document door's report copies in Drive: skills/doc-door/drive_upload.py
(rclone under a drive.file-only consent) and its handoff from door.py.

Nothing here touches Google. rclone is replaced by a stub that maps the
`docdoor:` remote to a directory and logs every call; Google's tokeninfo answer
and Kit's connector tool list are monkeypatched. When a real rclone is on the
machine, the rehearsal path (a local remote) also runs against it.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import stat
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_doc_door import (
    GOOD,
    REPO,
    door,
    doc_id,
    drive_upload,
    run,
)
from test_doc_door import env as door_env  # noqa: F401 - the fixture, fetched by name below


@pytest.fixture(name="env")
def _env(request):
    """The door harness of test_doc_door.py (fake mailbox, vault, MarkItDown)."""
    return request.getfixturevalue("door_env")


CLIENT_ID = "cid-123.apps.example"
SECRETS = ("SECRET-XYZ", "ACCESS-TOK", "REFRESH-TOK")
FULL_DRIVE = "https://www.googleapis.com/auth/drive"

STUB = """#!{python}
import json, os, shutil, sys

ROOT, LOG = {root!r}, {log!r}
GLOBAL = {{"--config", "--drive-scope", "--retries", "--low-level-retries", "--contimeout", "--timeout"}}
args, g, rest, i = sys.argv[1:], {{}}, [], 0
while i < len(args):
    if args[i] in GLOBAL:
        g[args[i]] = args[i + 1]
        i += 2
    else:
        rest.append(args[i])
        i += 1
with open(LOG, "a") as f:
    f.write(json.dumps({{"globals": g, "args": rest}}) + "\\n")
if rest[0] == "version":
    print("rclone v1.75.1")
    sys.exit(0)
if os.path.exists(ROOT + ".fail"):
    sys.stderr.write("Failed to copyto: googleapi: Error 403: quota for a@b.example 123456789012\\n")
    sys.exit(1)

def real(p):
    if p.startswith("docdoor:"):
        return os.path.join(ROOT, p[len("docdoor:"):])
    if p.startswith(":local:"):
        return p[len(":local:"):]
    sys.exit("bad remote " + p)

if rest[0] == "copyto":
    dst = real(rest[2])
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(rest[1], dst)
elif rest[0] == "cat":
    sys.stdout.buffer.write(open(real(rest[1]), "rb").read())
elif rest[0] == "deletefile":
    os.remove(real(rest[1]))
elif rest[0] == "lsf":
    d = real(rest[1])
    for n in sorted(os.listdir(d)):
        print(n + ("/" if os.path.isdir(os.path.join(d, n)) else ""))
else:
    sys.exit("unknown command " + rest[0])
"""


def write_config(path: Path, scope: str = "drive.file", mode: int = 0o600) -> None:
    token = json.dumps({"access_token": "ACCESS-TOK", "refresh_token": "REFRESH-TOK"})
    path.write_text(
        f"[docdoor]\ntype = drive\nclient_id = {CLIENT_ID}\nclient_secret = SECRET-XYZ\n"
        f"scope = {scope}\ntoken = {token}\n"
    )
    path.chmod(mode)


@pytest.fixture
def drive(tmp_path, monkeypatch):
    root = tmp_path / "fake-drive"
    root.mkdir()
    log = tmp_path / "rclone.log"
    stub = tmp_path / "rclone"
    stub.write_text(STUB.format(python=sys.executable, root=str(root), log=str(log)))
    stub.chmod(0o755)
    secret = tmp_path / "secret"
    secret.mkdir(mode=0o700)
    cfg = secret / "rclone.conf"
    write_config(cfg)
    monkeypatch.setattr(drive_upload, "RCLONE", str(stub))
    monkeypatch.setattr(drive_upload, "CONFIG", cfg)
    monkeypatch.setattr(drive_upload, "REHEARSAL", False)
    monkeypatch.setattr(drive_upload, "REMOTE", "docdoor:")
    monkeypatch.setattr(
        drive_upload, "token_scopes", lambda tok, cid: ({drive_upload.SCOPE_URL}, cid)
    )

    def calls():
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text().splitlines()]

    return SimpleNamespace(
        root=root,
        cfg=cfg,
        calls=calls,
        fail=lambda: Path(str(root) + ".fail").write_text("x"),
        unfail=lambda: Path(str(root) + ".fail").unlink(),
    )


def copy_dest(d):
    return f"Offer letter [{d[:8]}].md"


def reported_with_copy(capsys):
    run(["scan"])
    d = doc_id()
    capsys.readouterr()
    assert run(["report", d], GOOD) == 0
    return d, capsys.readouterr().out


# --- door -> uploader handoff ----------------------------------------------------------


def test_accepted_report_is_copied_into_the_named_drive_folder(env, drive, capsys):
    d, out = reported_with_copy(capsys)
    path = f"Document door reports/Employment/{copy_dest(d)}"
    assert f"Report copy: filed in {path}" in out
    copied = (drive.root / path).read_text()
    assert copied == (door.HOME / "reports" / f"{d}.md").read_text()
    assert "Facts:" in copied and '"Start date: 4 January 2027" [L3]' in copied
    rec = door.load_state()["docs"][d]
    assert rec["copy"] == "filed" and rec["copy_ref"] == path
    assert not any(s in out for s in SECRETS)


def test_every_rclone_call_names_the_config_and_pins_the_scope(env, drive, capsys):
    reported_with_copy(capsys)
    calls = drive.calls()
    assert calls
    for c in calls:
        assert c["globals"]["--drive-scope"] == "drive.file"
        assert c["globals"]["--config"] == str(drive.cfg)
    assert [c["args"][0] for c in calls] == ["copyto"]


def test_a_copy_is_made_once(env, drive, capsys):
    reported_with_copy(capsys)
    run(["scan"])
    run(["scan"])
    assert [c["args"][0] for c in drive.calls()].count("copyto") == 1


def test_without_the_consent_nothing_is_attempted_and_nothing_is_printed(env, capsys):
    d, out = reported_with_copy(capsys)
    assert "Report copy" not in out
    assert "copy" not in door.load_state()["docs"][d]


def test_a_failed_copy_is_pending_with_a_clean_reason_and_the_scan_retries(
    env, drive, capsys
):
    drive.fail()
    d, out = reported_with_copy(capsys)
    line = [ln for ln in out.splitlines() if ln.startswith("Report copy:")][0]
    assert line.startswith("Report copy: pending (")
    assert "a@b.example" not in line and "123456789012" not in line
    assert door.load_state()["docs"][d]["status"] == "reported"
    assert run(["scan"]) == 0  # a refusal never fails the scan
    assert "No new documents." in capsys.readouterr().out
    drive.unfail()
    run(["scan"])
    assert door.load_state()["docs"][d]["copy"] == "filed"
    run(["scan"])
    assert len(list((drive.root / "Document door reports/Employment").iterdir())) == 1


def test_a_missing_rclone_binary_leaves_the_copy_pending(
    env, drive, capsys, monkeypatch
):
    monkeypatch.setattr(drive_upload, "RCLONE", str(drive.root / "no-such-rclone"))
    d, out = reported_with_copy(capsys)
    assert "Report copy: pending (rclone is not installed" in out
    assert door.load_state()["docs"][d]["copy"] == "pending"


def test_earlier_reports_are_copied_once_the_consent_lands(
    env, capsys, monkeypatch, drive
):
    # A report filed before the consent existed is backfilled by a later scan.
    monkeypatch.setattr(drive_upload, "CONFIG", drive.cfg.parent / "later.conf")
    d, _ = reported_with_copy(capsys)
    assert not drive.calls()
    monkeypatch.setattr(drive_upload, "CONFIG", drive.cfg)
    run(["scan"])
    assert door.load_state()["docs"][d]["copy"] == "filed"


def test_rehearsal_remote_runs_the_whole_handoff_without_google(
    env, drive, capsys, monkeypatch, tmp_path
):
    fake = tmp_path / "rehearsal"
    monkeypatch.setattr(drive_upload, "REHEARSAL", True)
    monkeypatch.setattr(drive_upload, "REMOTE", f":local:{fake}/")
    monkeypatch.setattr(drive_upload, "CONFIG", tmp_path / "absent" / "rclone.conf")
    d, out = reported_with_copy(capsys)
    assert "Report copy: filed in Document door reports/Employment/" in out
    assert (fake / "Document door reports/Employment" / copy_dest(d)).is_file()


def test_the_copy_name_is_safe_and_unique_per_document():
    rec = {"title": "Payslip: Oct/2026 <final>?"}
    name = door.copy_name(rec, "abcdef0123456789")
    assert name == "Payslip- Oct-2026 -final [abcdef01].md"
    assert "/" not in name


# --- the uploader on its own -----------------------------------------------------------


def uploader(*argv):
    return drive_upload.main(["drive_upload.py", *argv])


def test_put_refuses_paths_that_climb_or_nest_the_name(drive, tmp_path):
    f = tmp_path / "r.md"
    f.write_text("x")
    for folder, name in (
        ("../x", "a.md"),
        ("ok", "../a.md"),
        ("ok", "a/b.md"),
        ("-f", "a.md"),
    ):
        with pytest.raises(drive_upload.UploadError):
            drive_upload.put(f, folder, name)
    assert not drive.calls()


def test_put_via_cli_prints_the_drive_path(drive, tmp_path, capsys):
    f = tmp_path / "r.md"
    f.write_text("hello")
    assert uploader("put", str(f), "Tax", "--name", "n.md") == 0
    assert capsys.readouterr().out.strip() == "Document door reports/Tax/n.md"
    assert (drive.root / "Document door reports/Tax/n.md").read_text() == "hello"


def test_status_says_when_the_consent_is_missing(drive, capsys):
    drive.cfg.unlink()
    assert uploader("status") == 0
    assert "config: absent" in capsys.readouterr().out


def test_verify_passes_and_leaves_no_test_file(drive, capsys):
    assert uploader("verify", "uploader") == 0
    out = capsys.readouterr().out
    assert "VERIFY OK" in out and "FAIL" not in out
    assert "scope as drive.file only" in out and "read back one test file" in out
    assert not any(s in out for s in SECRETS) and CLIENT_ID not in out
    verify_dir = drive.root / "Document door reports/_verify"
    assert verify_dir.is_dir() and list(verify_dir.iterdir()) == []
    ops = [c["args"][0] for c in drive.calls()]
    assert ops == ["copyto", "cat", "deletefile", "lsf"]


def test_verify_fails_when_the_token_holds_more_than_drive_file(
    drive, capsys, monkeypatch
):
    monkeypatch.setattr(
        drive_upload,
        "token_scopes",
        lambda tok, cid: ({drive_upload.SCOPE_URL, FULL_DRIVE}, cid),
    )
    assert uploader("verify", "uploader") == 1
    out = capsys.readouterr().out
    assert "FAIL  the token holds 2 scope(s)" in out and "VERIFY FAILED" in out


def test_verify_fails_for_a_token_that_is_only_the_wider_scope(
    drive, capsys, monkeypatch
):
    monkeypatch.setattr(
        drive_upload, "token_scopes", lambda tok, cid: ({FULL_DRIVE}, cid)
    )
    assert uploader("verify", "uploader") == 1


def test_verify_fails_when_the_config_asks_for_a_wider_scope(drive, capsys):
    write_config(drive.cfg, scope="drive")
    assert uploader("verify", "uploader") == 1
    assert "config scope is 'drive'" in capsys.readouterr().out


def test_verify_fails_when_the_config_is_readable_by_others(drive, capsys):
    drive.cfg.chmod(0o644)
    assert uploader("verify", "uploader") == 1
    assert "config mode is 0644, want 0600" in capsys.readouterr().out


def test_verify_fails_without_a_config(drive, capsys):
    drive.cfg.unlink()
    assert uploader("verify", "uploader") == 1
    assert "one-time consent is not in yet" in capsys.readouterr().out


def test_verify_fails_when_the_account_root_shows_more_than_its_own_folder(
    drive, capsys
):
    (drive.root / "somebody-elses-file.pdf").write_text("x")
    assert uploader("verify", "uploader") == 1
    assert "1 item(s) besides 'Document door reports'" in capsys.readouterr().out


def test_verify_fails_when_the_token_belongs_to_another_client(
    drive, capsys, monkeypatch
):
    monkeypatch.setattr(
        drive_upload,
        "token_scopes",
        lambda tok, cid: ({drive_upload.SCOPE_URL}, "other-client"),
    )
    assert uploader("verify", "uploader") == 1


def test_verify_fails_when_google_will_not_confirm_the_scope(
    drive, capsys, monkeypatch
):
    def boom(tok, cid):
        raise drive_upload.UploadError("Google rejected the token (HTTP 400)")

    monkeypatch.setattr(drive_upload, "token_scopes", boom)
    assert uploader("verify", "uploader") == 1
    out = capsys.readouterr().out
    assert "could not read the token's scope back from Google" in out


def test_verify_fails_when_the_round_trip_does_not_work(drive, capsys):
    drive.fail()
    assert uploader("verify", "uploader") == 1
    assert "create/read-back" in capsys.readouterr().out


def test_rehearsal_verify_says_it_did_not_check_the_scope(
    drive, capsys, monkeypatch, tmp_path
):
    def never(tok, cid):
        raise AssertionError("no Google in a rehearsal")

    monkeypatch.setattr(drive_upload, "token_scopes", never)
    monkeypatch.setattr(drive_upload, "REHEARSAL", True)
    monkeypatch.setattr(drive_upload, "REMOTE", f":local:{tmp_path}/rehearsal/")
    assert uploader("verify", "uploader") == 0
    out = capsys.readouterr().out
    assert "REHEARSAL" in out and "not checked" in out and "VERIFY OK" in out


@pytest.mark.parametrize(
    "tools,ok",
    [
        (
            [
                "search_drive_files",
                "list_drive_items",
                "get_drive_file_content",
                "send_gmail_message",
            ],
            True,
        ),
        (["search_drive_files", "create_drive_file"], False),
        (["search_drive_files", "update_drive_file"], False),
        (["search_drive_files", "manage_drive_access"], False),
        (["search_drive_files", "import_to_google_doc"], False),
        (["search_drive_files", "get_drive_shareable_link"], False),
        (["search_gmail_messages"], False),  # no Drive tool: the level cannot be shown
        ([], False),
    ],
)
def test_kit_connector_must_offer_only_drive_reads(tools, ok, capsys, monkeypatch):
    monkeypatch.setattr(drive_upload, "kit_tool_names", lambda: tools)
    assert uploader("verify", "kit") == (0 if ok else 1)


def test_kit_check_reports_an_unreachable_connector(capsys, monkeypatch):
    def down():
        raise OSError("connection refused")

    monkeypatch.setattr(drive_upload, "kit_tool_names", down)
    assert uploader("verify", "kit") == 1
    assert "could not list Kit's connector tools" in capsys.readouterr().out


def test_kit_tool_names_follows_pagination(monkeypatch):
    pages = {None: (["a_drive"], "c1"), "c1": (["b_drive"], None)}

    class Fake:
        def __init__(self, url):
            self.url = url

        def _rpc(self, method, params):
            names, nxt = pages[params.get("cursor")]
            out = {"tools": [{"name": n} for n in names]}
            if nxt:
                out["nextCursor"] = nxt
            return out

    monkeypatch.setattr(door, "Mcp", Fake)
    assert drive_upload.kit_tool_names() == ["a_drive", "b_drive"]


# --- configure -------------------------------------------------------------------------

PASTED = (
    "Paste the following into your remote machine --->\n"
    '{"access_token":"ACCESS-TOK","token_type":"Bearer","refresh_token":"REFRESH-TOK",'
    '"expiry":"2026-11-09T12:00:00Z"}\n<---End paste\n'
)


def configure_with(monkeypatch, tmp_path, *values, force=False, name="rclone.conf"):
    d = tmp_path / "cfgdir"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(drive_upload, "CONFIG", d / name)
    feed = iter(values)
    return drive_upload.configure(
        force, read=lambda label, secret: next(feed)
    ), d / name


def test_configure_writes_a_0600_config_with_drive_file_and_prints_no_secret(
    monkeypatch, tmp_path, capsys
):
    rc, cfg = configure_with(monkeypatch, tmp_path, CLIENT_ID, "SECRET-XYZ", PASTED)
    assert rc == 0
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert not list(cfg.parent.glob("*.tmp"))
    text = cfg.read_text()
    assert "scope = drive.file" in text and "type = drive" in text
    assert (
        json.loads(re.search(r"^token = (.*)$", text, re.M)[1])["refresh_token"]
        == "REFRESH-TOK"
    )
    out = capsys.readouterr()
    assert not any(s in out.out + out.err for s in SECRETS)


def test_configure_refuses_to_replace_without_force(monkeypatch, tmp_path, capsys):
    rc, cfg = configure_with(monkeypatch, tmp_path, CLIENT_ID, "SECRET-XYZ", PASTED)
    before = cfg.read_text()
    rc, _ = configure_with(monkeypatch, tmp_path, "other", "other", PASTED)
    assert rc == 1 and cfg.read_text() == before
    rc, _ = configure_with(
        monkeypatch, tmp_path, "other", "other-secret", PASTED, force=True
    )
    assert rc == 0 and "other-secret" in cfg.read_text()


@pytest.mark.parametrize(
    "token", ["no json here", '{"access_token":"a"}', "{not json}"]
)
def test_configure_rejects_a_token_without_a_refresh_token(
    monkeypatch, tmp_path, token
):
    with pytest.raises(drive_upload.UploadError):
        configure_with(monkeypatch, tmp_path, CLIENT_ID, "SECRET-XYZ", token)
    assert not (tmp_path / "cfgdir" / "rclone.conf").exists()


def test_configure_rejects_an_empty_client(monkeypatch, tmp_path):
    rc, cfg = configure_with(monkeypatch, tmp_path, "", "SECRET-XYZ", PASTED)
    assert rc == 1 and not cfg.exists()


def test_configure_needs_the_host_folder(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(drive_upload, "CONFIG", tmp_path / "nope" / "rclone.conf")
    assert drive_upload.configure(False, read=lambda *a: "x") == 1
    assert "create it on the host first" in capsys.readouterr().err


def test_configure_reads_piped_stdin_lines(monkeypatch, tmp_path):
    d = tmp_path / "cfgdir"
    d.mkdir()
    monkeypatch.setattr(drive_upload, "CONFIG", d / "rclone.conf")
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(f"{CLIENT_ID}\nSECRET-XYZ\n" + PASTED.replace("\n", " ") + "\n"),
    )
    assert uploader("configure") == 0
    assert (d / "rclone.conf").is_file()


# --- the pinned rclone -----------------------------------------------------------------


class _Serve(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(_Serve.body)))
        self.end_headers()
        self.wfile.write(_Serve.body)

    def log_message(self, *a):
        pass


@pytest.fixture
def mirror(monkeypatch, tmp_path):
    arch = drive_upload._arch()
    stem = f"rclone-{drive_upload.RCLONE_VERSION}-linux-{arch}"
    zpath = tmp_path / "mirror.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr(
            f"{stem}/rclone",
            f"#!/bin/sh\necho 'rclone {drive_upload.RCLONE_VERSION}'\n",
        )
    _Serve.body = zpath.read_bytes()
    srv = HTTPServer(("127.0.0.1", 0), _Serve)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import hashlib

    monkeypatch.setattr(
        drive_upload, "RCLONE_BASE_URL", f"http://127.0.0.1:{srv.server_port}"
    )
    monkeypatch.setattr(
        drive_upload, "RCLONE", str(tmp_path / "door" / "bin" / "rclone")
    )
    monkeypatch.setattr(
        drive_upload,
        "RCLONE_SUMS",
        {**drive_upload.RCLONE_SUMS, arch: hashlib.sha256(_Serve.body).hexdigest()},
    )
    yield SimpleNamespace(arch=arch, bin=tmp_path / "door" / "bin" / "rclone")
    srv.shutdown()


def test_install_rclone_checks_the_pin_then_installs_once(mirror, capsys):
    assert uploader("install-rclone") == 0
    assert stat.S_IMODE(mirror.bin.stat().st_mode) == 0o755
    assert stat.S_IMODE(mirror.bin.parent.stat().st_mode) == 0o700
    assert "installed" in capsys.readouterr().out
    assert uploader("install-rclone") == 0
    assert "already in place" in capsys.readouterr().out


def test_install_rclone_refuses_a_download_that_misses_its_sha(
    mirror, monkeypatch, capsys
):
    monkeypatch.setattr(
        drive_upload, "RCLONE_SUMS", {**drive_upload.RCLONE_SUMS, mirror.arch: "0" * 64}
    )
    assert uploader("install-rclone") == 1
    assert "does not match its pinned sha256" in capsys.readouterr().err
    assert not mirror.bin.exists()


def test_the_pin_matches_the_off_box_backups_rclone():
    text = (REPO / "scripts/offbox-backup/install-offbox-backup.sh").read_text()
    assert f'RCLONE_VERSION="{drive_upload.RCLONE_VERSION}"' in text
    for arch, sha in drive_upload.RCLONE_SUMS.items():
        assert re.search(rf"{arch}\) sum=\"{sha}\"", text)


# --- wiring ----------------------------------------------------------------------------


def compose():
    return yaml.safe_load((REPO / "compose/openclaw/docker-compose.yml").read_text())


def test_the_gateway_mounts_the_uploader_config_folder_writable_and_alone():
    vols = compose()["services"]["openclaw-gateway"]["volumes"]
    (mount,) = [
        v
        for v in vols
        if v.endswith("/run/lifekit/doc-door-drive")
        or "/run/lifekit/doc-door-drive:" in v
    ]
    # rw: rclone saves the refreshed access token beside the config; only this
    # folder, never /srv/lifekit-secrets itself.
    assert (
        mount
        == "${LIFEKIT_SECRETS_DIR:-/srv/lifekit-secrets}/doc-door-drive:/run/lifekit/doc-door-drive"
    )
    assert drive_upload.CONFIG == Path("/run/lifekit/doc-door-drive/rclone.conf")


def test_kits_connector_stays_read_only_for_drive_in_the_compose_file():
    env_ = compose()["services"]["google-workspace-mcp"]["environment"]
    perms = env_["WORKSPACE_MCP_PERMISSIONS"].split()
    assert "drive:readonly" in perms and "drive:full" not in perms


@pytest.mark.skipif(shutil.which("rclone") is None, reason="no rclone on this machine")
def test_the_rehearsal_path_works_with_a_real_rclone(
    env, capsys, monkeypatch, tmp_path
):
    fake = tmp_path / "rehearsal"
    monkeypatch.setattr(drive_upload, "RCLONE", shutil.which("rclone"))
    monkeypatch.setattr(drive_upload, "REHEARSAL", True)
    monkeypatch.setattr(drive_upload, "REMOTE", f":local:{fake}/")
    monkeypatch.setattr(drive_upload, "CONFIG", tmp_path / "absent" / "rclone.conf")
    d, out = reported_with_copy(capsys)
    assert "Report copy: filed in Document door reports/Employment/" in out
    assert (fake / "Document door reports/Employment" / copy_dest(d)).is_file()
    assert uploader("verify", "uploader") == 0
