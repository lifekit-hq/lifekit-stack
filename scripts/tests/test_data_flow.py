"""The data-flow lint (scripts/lint/data-flow.py) and its live drift hook.

What this file pins: the static gate fails a compose file, unit or installer that
introduces a store with no row in the vault's `## Data flow` table and passes once
the row exists; a unit without ProtectSystem=strict plus ReadWritePaths= or
StateDirectory= fails unless the allowlist lists it with a removal date; secrets
paths are never reported; a row nothing matches is reported stale; and the vault
audit runner appends the drift section without ever failing the timer. Fixtures are
synthetic: a throwaway repo tree and a hand-written system map.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/lint/data-flow.py"
ALLOWLIST = REPO / "scripts/lint/data-flow-allowlist.yaml"
RUNNER = REPO / "scripts/memory-audit/run-vault-audit.py"
TODAY = dt.date(2026, 11, 26)

spec = importlib.util.spec_from_file_location("data_flow", SCRIPT)
df = importlib.util.module_from_spec(spec)
sys.modules["data_flow"] = df
spec.loader.exec_module(df)

COMPOSE = """\
services:
  app:
    image: example/app:1
    volumes:
      - app_data:/var/lib/app
      - ${APP_STATE_DIR:-/var/lib/lifekit/app-state}:/state
      - ./config:/etc/app:ro
      - /srv/lifekit-secrets/app:/run/secrets/app
      - /srv/app/credentials:/creds
      - /cache-only
  app-test-runner:
    image: example/app:1
    volumes:
      - /var/lib/lifekit/test-scratch:/scratch
volumes:
  app_data:
"""

UNIT_SANDBOXED = """\
[Service]
User=lifekit
Environment="APP_FEED_FILE=/var/lib/lifekit-feed/feed.jsonl" "APP_NAME=x" "APP_TOKEN_DIR=/srv/app/secret-key"
ProtectSystem=strict
StateDirectory=lifekit-app
ReadWritePaths=/var/lib/lifekit/app-state
"""

UNIT_OPEN = """\
[Service]
User=lifekit
ExecStart=/usr/local/bin/app
"""

INSTALLER = """\
#!/usr/bin/env bash
STATE_DIR="${STATE_DIR:-/var/lib/lifekit/app-installed}"
UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
install -d -m 755 "$UNIT_DIR"
install -d -o lifekit -g lifekit -m 750 \\
  "$INSTALL_ROOT$STATE_DIR"
"""

HEADER = """\
# System map

## Data flow

| Store | Path | Format | Writer | Readers | Exposure | Owning repo |
|---|---|---|---|---|---|---|
"""

ROWS = [
    "| app-db | `example_app_data:/var/lib/app` | sqlite | app | app | private | example |",
    "| app-state | `/var/lib/lifekit/{app-state,app-installed}` | json | app | app | file | example |",
    "| app-svc | `/var/lib/lifekit-app` | json | app | app | private | example |",
    "| app-feed | `/var/lib/lifekit-feed/feed.jsonl` | jsonl | app | publisher | publisher | example |",
]


def make_repo(tmp_path: Path, open_unit: bool = False) -> Path:
    repo = tmp_path / "repo"
    (repo / "compose/example").mkdir(parents=True, exist_ok=True)
    (repo / "compose/example/docker-compose.yml").write_text(COMPOSE)
    (repo / "scripts/app").mkdir(parents=True, exist_ok=True)
    (repo / "scripts/app/lifekit-app.service").write_text(UNIT_SANDBOXED)
    (repo / "scripts/app/install-app.sh").write_text(INSTALLER)
    if open_unit:
        (repo / "scripts/app/lifekit-open.service").write_text(UNIT_OPEN)
    return repo


def map_with(rows: list[str]) -> str:
    return HEADER + "\n".join(rows) + "\n\n## Next section\n"


def run_gate(
    repo: Path, map_text: str, tmp_path: Path, allowlist: str = ""
) -> subprocess.CompletedProcess:
    map_file = tmp_path / "system-map.md"
    map_file.write_text(map_text)
    allow = tmp_path / "allow.yaml"
    allow.write_text(allowlist)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--map",
            str(map_file),
            "--allowlist",
            str(allow),
            "--today",
            str(TODAY),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def discovered_keys(repo: Path) -> set[str]:
    stores, _, _ = df.discover_static(repo)
    return {s.key for s in stores}


def test_discovery_reads_compose_systemd_and_installers_and_skips_what_is_not_a_store(
    tmp_path,
):
    keys = discovered_keys(make_repo(tmp_path))
    assert (
        keys
        == {
            "volume:example_app_data",  # named volume
            "bind:/var/lib/lifekit/app-state",  # rw bind (default substituted) and ReadWritePaths=
            "bind:/var/lib/lifekit-app",  # StateDirectory=
            "bind:/var/lib/lifekit-feed/feed.jsonl",  # *_FILE environment path
            "bind:/var/lib/lifekit/app-installed",  # install -d target through its variable
        }
    )


def test_secrets_paths_are_never_reported(tmp_path):
    repo = make_repo(tmp_path)
    keys = discovered_keys(repo)
    assert not any("secret" in k or "credentials" in k for k in keys)
    result = run_gate(repo, map_with([]), tmp_path)
    assert "secret" not in result.stdout and "credentials" not in result.stdout
    for path in (
        "/srv/lifekit-secrets/gw",
        "/srv/app/credentials.json",
        "/etc/lifekit/scout.env",
        "/x/secret-key",
    ):
        assert df.excluded(path) == "secret"


def test_caches_logs_anonymous_volumes_and_test_containers_are_excluded(tmp_path):
    keys = discovered_keys(make_repo(tmp_path))
    assert not any("cache" in k or "test-scratch" in k for k in keys)
    assert df.excluded("/var/lib/app/cache/x") == "cache"
    assert df.excluded("/var/lib/app/run.log") == "log"
    assert df.is_test_container("basket-test-pg-1")
    assert df.is_test_container(
        "youthful_wilbur"
    )  # docker's generated name, no compose project
    assert not df.is_test_container("wger_web", "wger")


def test_an_undocumented_store_fails_the_gate(tmp_path):
    repo = make_repo(tmp_path)
    result = run_gate(repo, map_with(ROWS[1:]), tmp_path)
    assert result.returncode == 1, result.stdout
    assert "FAIL no Data flow row: volume:example_app_data" in result.stdout
    assert result.stdout.count("FAIL") == 2  # the store line and the summary line


def test_a_documented_repo_passes(tmp_path):
    result = run_gate(make_repo(tmp_path), map_with(ROWS), tmp_path)
    assert result.returncode == 0, result.stdout
    assert "data-flow: ok" in result.stdout


def test_a_row_covers_paths_under_it_and_a_parent_bind_of_documented_rows(tmp_path):
    ctx = df.Context()
    row = df.make_row("root", "`/var/lib/lifekit/intake`", 1)
    assert df.store_matches_row(
        df.Store("bind", "/var/lib/lifekit/intake/reports"), row, ctx
    )
    assert df.store_matches_row(df.Store("bind", "/var/lib/lifekit"), row, ctx)
    assert not df.store_matches_row(
        df.Store("bind", "/var/lib/lifekit/intake-old"), row, ctx
    )


def test_home_and_tilde_rows_normalise_through_the_known_homes():
    ctx = df.Context(homes=["/home/admin"], fm_homes=["/home/admin/fleet"])
    tilde = df.make_row("scout", "`~/.local/state/lifekit-scout`", 1)
    home = df.make_row("status", "`<home>/state/<task>.status`", 2)
    assert df.store_matches_row(
        df.Store("bind", "/home/admin/.local/state/lifekit-scout"), tilde, ctx
    )
    assert df.store_matches_row(
        df.Store("bind", "/home/admin/fleet/state/t1.status"), home, ctx
    )
    assert df.store_matches_row(df.Store("bind", "/home/admin/fleet/state"), home, ctx)
    assert not df.store_matches_row(
        df.Store("bind", "/home/other/.local/state/lifekit-scout"), tilde, ctx
    )


def test_without_known_homes_tilde_and_home_rows_never_cover_the_service_homes(
    tmp_path,
):
    repo = make_repo(tmp_path)
    (repo / "compose/gateway").mkdir(parents=True)
    (repo / "compose/gateway/docker-compose.yml").write_text(
        "services:\n  gw:\n    image: example/gw:1\n    volumes:\n"
        "      - /home/lifekit/.claude:/home/node/.claude\n"
    )
    admin_rows = ROWS + [
        "| sessions | `~/.claude` | jsonl | Claude Code | quota-share | private | example |"
    ]
    result = run_gate(repo, map_with(admin_rows), tmp_path)
    assert result.returncode == 1, result.stdout
    assert "FAIL no Data flow row: bind:/home/lifekit/.claude" in result.stdout

    own_row = "| gw-claude | `/home/lifekit/.claude` | json | gw | gw | private | example |"
    result = run_gate(repo, map_with(admin_rows + [own_row]), tmp_path)
    assert result.returncode == 0, result.stdout

    ctx = df.Context()
    home = df.make_row("status", "`<home>/state`", 1)
    assert not df.store_matches_row(
        df.Store("bind", "/home/lifekit/fleet/state"), home, ctx
    )
    assert df.store_matches_row(df.Store("bind", "/home/admin/fleet/state"), home, ctx)


def test_container_paths_normalise_through_the_mount_table():
    ctx = df.Context(
        mounts=[df.Mount("/var/lib/lifekit", "/home/node/.life-state", "bind")]
    )
    row = df.make_row("jobq", "`/home/node/.life-state/job-queue`", 1)
    assert df.store_matches_row(
        df.Store("bind", "/var/lib/lifekit/job-queue"), row, ctx
    )


def test_volume_rows_match_by_name_container_or_listed_words():
    ctx = df.Context()
    vol = df.Store(
        "volume",
        "wger_postgres_data",
        project="wger",
        short="postgres_data",
        containers={"wger-db-1"},
    )
    assert df.store_matches_row(vol, df.make_row("wger", "`wger-db-1 / wger`", 1), ctx)
    obs = df.Store(
        "volume", "compose_grafana_data", project="compose", short="grafana_data"
    )
    assert df.store_matches_row(
        obs, df.make_row("o", "prometheus, loki, tempo, grafana volumes", 1), ctx
    )
    rem = df.Store(
        "volume",
        "reminder-bot_reminder_data",
        project="reminder-bot",
        short="reminder_data",
    )
    assert df.store_matches_row(
        rem, df.make_row("r", "`reminder-bot_reminder_data:/data/reminders.db`", 1), ctx
    )
    other = df.make_row("pc", "`otherhost:~/clip-pc`", 1)
    assert other.remote and not df.store_matches_row(
        df.Store("bind", "/home/x/clip-pc"), other, ctx
    )


def test_a_new_unit_without_the_write_sandbox_fails(tmp_path):
    result = run_gate(make_repo(tmp_path, open_unit=True), map_with(ROWS), tmp_path)
    assert result.returncode == 1
    assert (
        "FAIL unit scripts/app/lifekit-open.service must set ProtectSystem=strict"
        in result.stdout
    )


def test_an_allowlisted_unit_passes_but_is_printed_with_its_removal_date(tmp_path):
    allow = "units:\n  - unit: scripts/app/lifekit-open.service\n    remove_by: 2026-12-31\n"
    result = run_gate(
        make_repo(tmp_path, open_unit=True), map_with(ROWS), tmp_path, allow
    )
    assert result.returncode == 0, result.stdout
    assert (
        "allowlisted (remove by 2026-12-31): unit scripts/app/lifekit-open.service"
        in result.stdout
    )
    expired = allow.replace("2026-12-31", "2026-11-01")
    result = run_gate(
        make_repo(tmp_path, open_unit=True), map_with(ROWS), tmp_path, expired
    )
    assert "allowlisted EXPIRED (remove by 2026-11-01)" in result.stdout


def test_an_allowlist_entry_without_a_removal_date_is_refused(tmp_path):
    allow = "units:\n  - unit: scripts/app/lifekit-open.service\n"
    result = run_gate(
        make_repo(tmp_path, open_unit=True), map_with(ROWS), tmp_path, allow
    )
    assert result.returncode == 2
    assert "remove_by" in result.stderr


def test_a_map_without_the_table_is_an_error_not_a_pass(tmp_path):
    result = run_gate(make_repo(tmp_path), "# System map\n\nno table here\n", tmp_path)
    assert result.returncode == 2
    assert "Data flow" in result.stderr


def test_a_stale_row_is_reported(tmp_path):
    live = tmp_path / "var-lib"
    (live / "kept").mkdir(parents=True)
    rows = df.parse_table(
        map_with(
            [
                f"| kept | `{live}/kept` | json | a | a | file | x |",
                f"| gone | `{live}/needs-you` | json | a | a | file | x |",
                "| gone-vol | `old_volume:/data` | sqlite | a | a | private | x |",
                "| pc | `otherhost:~/thing` | json | a | a | file | x |",
            ]
        )
    )
    stores = [
        df.Store("bind", f"{live}/kept"),
        df.Store("bind", f"{live}/undocumented"),
    ]
    text = df.render(
        stores, rows, df.Context(), set(), set(), [], TODAY, {"stores": {}}
    )
    assert "### Stores with no row (1)" in text and f"`{live}/undocumented`" in text
    assert "### Stale rows (2)" in text
    assert "`gone`" in text and "`gone-vol`" in text
    assert "`kept`" not in text and "`pc`" not in text


def test_a_parent_bind_does_not_hide_a_stale_row_under_it(tmp_path):
    # On the host the gateway binds all of /var/lib/lifekit: that parent store must
    # not count as discovering every row beneath it, or none of them can go stale.
    live = tmp_path / "var-lib-lifekit"
    (live / "kept").mkdir(parents=True)
    rows = df.parse_table(
        map_with(
            [
                f"| kept | `{live}/kept` | json | a | a | file | x |",
                f"| gone | `{live}/retired-store` | json | a | a | file | x |",
            ]
        )
    )
    stores = [df.Store("bind", str(live))]
    text = df.render(
        stores, rows, df.Context(), set(), set(), [], TODAY, {"stores": {}}
    )
    assert "### Stores with no row (0)" in text  # the parent is still documented
    assert "### Stale rows (1)" in text
    assert "`gone`" in text and "`kept`" not in text


def test_rows_naming_volumes_or_containers_are_unverifiable_when_docker_is_unreachable(
    monkeypatch,
):
    # Unreadable sources are reported, not guessed: a docker that refuses to list
    # anything is no evidence that a volume or a container is gone.
    monkeypatch.setattr(df, "DOCKER", "false")
    notes: list[str] = []
    stores, mounts, volumes, containers = df.discover_live_docker(notes)
    rows = df.parse_table(
        map_with(
            [
                "| vol | `old_volume:/data` | sqlite | a | a | private | x |",
                "| db | `dashboard-db / dashboard` | pg | a | a | private | x |",
            ]
        )
    )
    text = df.render(
        stores, rows, df.Context(mounts=mounts), volumes, containers, notes,
        TODAY, {"stores": {}},
    )
    assert "### Stale rows (0)" in text
    assert "### Rows this account cannot verify (2)" in text
    assert "docker unreachable" in text


def test_the_live_report_never_fails_its_caller(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--live", "--map", str(tmp_path / "missing.md")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("## Data flow drift")
    assert "check did not run" in result.stdout


def test_the_repo_allowlist_is_well_formed_and_every_entry_has_a_date():
    allow = df.load_allowlist(ALLOWLIST)
    for section in ("stores", "units"):
        for key, entry in allow[section].items():
            assert isinstance(df._date(entry["remove_by"]), dt.date), key
    for unit in allow["units"]:
        assert (REPO / unit).is_file(), unit


def test_the_repo_units_outside_the_allowlist_are_sandboxed():
    _, _, units = df.discover_static(REPO)
    allowed = set(df.load_allowlist(ALLOWLIST)["units"])
    assert set(df.unit_violations(units, REPO)) <= allowed


# --- the vault audit runner -------------------------------------------------

FAKE_DOCKER = """#!/usr/bin/env bash
case "$1" in
  ps) echo abc123 ;;
  exec) cat > /dev/null; echo '200 {"ok":true}' ;;
esac
"""


@pytest.mark.parametrize(
    "drift_body",
    ["print('## Data flow drift\\n\\n- `/x` (bind)')", "raise SystemExit(3)"],
)
def test_the_vault_audit_appends_the_drift_section_and_never_fails_on_it(
    tmp_path, drift_body
):
    vault = tmp_path / "vault"
    (vault / "audits").mkdir(parents=True)
    (vault / "audits/latest.md").write_text("# Vault audit - today\n")
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text("echo 'Vault audit | 0 high'\n")
    drift = tmp_path / "drift.py"
    drift.write_text(drift_body + "\n")
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(RUNNER)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "WRAPPER": str(wrapper),
            "DOCKER": str(docker),
            "DATA_FLOW": str(drift),
            "MEMORY_VAULT": str(vault),
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = (vault / "audits/latest.md").read_text()
    assert report.startswith("# Vault audit - today\n")
    assert "## Data flow drift" in report
    if "SystemExit" in drift_body:
        assert "check did not run: exit 3" in report
