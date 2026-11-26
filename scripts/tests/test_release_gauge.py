"""Behaviour of scripts/release-gauge/lifekit-release-gauge.sh against a fake gh."""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/release-gauge/lifekit-release-gauge.sh"
SLUG = "lifekit-hq/finance-sentry"
RUN_URL = "https://github.com/lifekit-hq/finance-sentry/actions/runs/38209202428"
PR_URL = "https://github.com/lifekit-hq/finance-sentry/pull/851"
STARTED = "2026-10-12T05:43:38Z"

# Fake gh: answers `gh api .../runs` from STATE_DIR/<owner>_<name>.runs.json and
# `gh pr list` from STATE_DIR/<owner>_<name>.prs.json, applying --jq with the
# real jq; STATE_DIR/_broken fails every call like an expired login. Every
# call is logged to STATE_DIR/_calls.
FAKE = r"""#!/usr/bin/env bash
echo "$*" >> "$STATE_DIR/_calls"
[[ -f "$STATE_DIR/_broken" ]] && { echo "HTTP 401: Bad credentials" >&2; exit 1; }
jqexpr=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ "${args[i]}" == --jq ]] && jqexpr="${args[i+1]}"
  [[ "${args[i]}" == -R ]] && repo="${args[i+1]}"
done
if [[ "$1" == api ]]; then
  repo="$(sed -E 's#^repos/([^/]+/[^/]+)/.*#\1#' <<< "$2")"
  file="$STATE_DIR/${repo/\//_}.runs.json"
  default='{"workflow_runs": []}'
else
  [[ "$*" == *'--label autorelease: pending'* && "$*" == *'--state open'* ]] || exit 3
  file="$STATE_DIR/${repo/\//_}.prs.json"
  default='[]'
fi
if [[ -f "$file" ]]; then jq -r "$jqexpr" "$file"; else jq -r "$jqexpr" <<< "$default"; fi
"""


MANIFEST = "scripts/release-gauge/components.txt"


class Stack:
    """A throwaway deploy checkout: commits with a fixed committer time, an
    origin/main ref, and a stamp directory beside it."""

    def __init__(self, root: Path):
        self.repo, self.stamps = root / "stack", root / "installed"
        self.repo.mkdir()
        self.stamps.mkdir()
        self.git("init", "-q", "-b", "main")

    def git(self, *args, when=None):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        if when is not None:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{when} +0000"
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit(self, when, **files):
        for rel, text in files.items():
            path = self.repo / rel.replace("__", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"at {when}", when=when)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        return self.git("rev-parse", "HEAD")

    def stamp(self, component, commit):
        (self.stamps / component).write_text(
            json.dumps({"commit": commit, "at": "2026-11-26T00:00:00Z"}) + "\n"
        )

    def env(self):
        return {
            "RELEASE_STACK_REPO": str(self.repo),
            "LIFEKIT_INSTALLED_DIR": str(self.stamps),
        }


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    s.commit(
        1_700_000_000, **{"scripts__release-gauge__components.txt": "a scripts/a\n"}
    )
    return s


@pytest.fixture
def gauge(tmp_path, stack):
    state, out, bin_dir = tmp_path / "state", tmp_path / "out", tmp_path / "bin"
    for d in (state, out, bin_dir):
        d.mkdir()
    fake = bin_dir / "gh"
    fake.write_text(FAKE)
    fake.chmod(0o755)

    def runs(repo, conclusion, started=STARTED, url=RUN_URL):
        rows = (
            []
            if conclusion is None
            else [
                {"conclusion": conclusion, "run_started_at": started, "html_url": url}
            ]
        )
        (state / f"{repo.replace('/', '_')}.runs.json").write_text(
            json.dumps({"workflow_runs": rows})
        )

    def prs(repo, *rows):
        (state / f"{repo.replace('/', '_')}.prs.json").write_text(
            json.dumps([{"url": u, "createdAt": c} for u, c in rows])
        )

    def run(repos=SLUG, expect=0):
        env = {k: v for k, v in os.environ.items() if k != "RELEASE_REPOS"}
        env |= {"PATH": f"{bin_dir}:{env['PATH']}", "STATE_DIR": str(state)}
        env |= stack.env()
        if repos is not None:
            env["RELEASE_REPOS"] = repos
        proc = subprocess.run(
            ["bash", str(SCRIPT), str(out)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == expect, proc.stderr
        if expect:
            return proc
        parsed = {}
        for line in (out / "weekly_release.prom").read_text().splitlines():
            if line and not line.startswith("#"):
                name, value = line.rsplit(" ", 1)
                parsed[name] = int(value)
        return parsed

    return runs, prs, run, state, out


def blocked(repo=SLUG, pr="", run=""):
    return f'weekly_release_blocked{{repo="{repo}",pr_url="{pr}",run_url="{run}"}}'


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_failed_run_with_its_release_pr_still_open_is_blocked(gauge, conclusion):
    runs, prs, run, *_ = gauge
    runs(SLUG, conclusion)
    prs(SLUG, (PR_URL, "2026-10-05T09:20:00Z"))
    assert run() == {blocked(pr=PR_URL, run=RUN_URL): 1}


def test_successful_run_is_not_blocked_and_never_reads_prs(gauge):
    # A merged release and the "no pending release PR" no-op both end green.
    runs, prs, run, state, _ = gauge
    runs(SLUG, "success")
    prs(SLUG, (PR_URL, "2026-10-05T09:20:00Z"))
    assert run() == {blocked(): 0}
    assert "pr list" not in (state / "_calls").read_text()


def test_failed_run_without_an_open_release_pr_is_not_blocked(gauge):
    runs, prs, run, *_ = gauge
    runs(SLUG, "failure")
    prs(SLUG)
    assert run() == {blocked(): 0}


def test_release_pr_opened_after_the_failed_run_is_not_blamed_on_it(gauge):
    runs, prs, run, *_ = gauge
    runs(SLUG, "failure")
    prs(SLUG, (PR_URL, "2026-10-13T10:00:00Z"))
    assert run() == {blocked(): 0}


def test_repo_without_any_completed_run_is_not_blocked(gauge):
    runs, _, run, *_ = gauge
    runs(SLUG, None)
    assert run() == {blocked(): 0}


def test_every_repo_gets_a_series(gauge):
    runs, prs, run, *_ = gauge
    other = "lifekit-hq/devclaw"
    runs(SLUG, "failure")
    prs(SLUG, (PR_URL, "2026-10-05T09:20:00Z"))
    runs(other, "success")
    assert run(repos=f"{SLUG} {other}") == {
        blocked(pr=PR_URL, run=RUN_URL): 1,
        blocked(repo=other): 0,
    }


def test_default_repos_are_the_four_weekly_release_repos(gauge):
    *_, run, _, _ = gauge
    assert {name.split('"')[1] for name in run(repos=None)} == {
        "lifekit-hq/finance-sentry",
        "lifekit-hq/lifekit-stack",
        "lifekit-hq/lifekit-dashboard",
        "lifekit-hq/devclaw",
    }


def test_read_failure_keeps_the_previous_file(gauge):
    runs, prs, run, state, out = gauge
    runs(SLUG, "failure")
    prs(SLUG, (PR_URL, "2026-10-05T09:20:00Z"))
    run()
    before = (out / "weekly_release.prom").read_text()
    (state / "_broken").write_text("")
    proc = run(expect=1)
    assert "Bad credentials" in proc.stderr
    assert (out / "weekly_release.prom").read_text() == before
    assert sorted(p.name for p in out.iterdir()) == [
        "lifekit_installed.prom",
        "weekly_release.prom",
    ]


def test_unexpected_url_fails_instead_of_writing_a_broken_label(gauge):
    runs, prs, run, _, out = gauge
    runs(SLUG, "failure", url='https://x/"} 1')
    prs(SLUG, (PR_URL, "2026-10-05T09:20:00Z"))
    run(expect=1)
    assert not (out / "weekly_release.prom").exists()


def _unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string((REPO / "scripts/release-gauge" / name).read_text())
    return parser


def test_service_runs_the_gauge_as_the_admin_user_into_the_textfile_dir():
    service = _unit("lifekit-release-gauge.service")
    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["User"] == "__ADMIN_USER__"
    assert service["Service"]["ExecStart"].split() == [
        "/usr/local/bin/lifekit-release-gauge.sh",
        "/var/lib/node_exporter/textfile",
    ]
    assert os.access(SCRIPT, os.X_OK)


def test_timer_runs_every_five_minutes():
    # textfile-collector-stale judges every 5-minute gauge against 15 minutes.
    timer = _unit("lifekit-release-gauge.timer")
    assert timer["Timer"]["OnUnitActiveSec"] == "5min"


# --- installed components behind main (issue 689) ----------------------------


def drift(gauge, expect=0):
    """Run the gauge and parse lifekit_installed.prom into {(metric, component): value}."""
    *_, run, _, out = gauge
    proc = run(expect=expect)
    if expect:
        return proc
    parsed = {}
    for line in (out / "lifekit_installed.prom").read_text().splitlines():
        if line and not line.startswith("#"):
            m = re.fullmatch(r'(\w+)\{component="([^"]+)"\} (-?\d+)', line)
            assert m, line
            parsed[(m[1], m[2])] = int(m[3])
    return parsed


T0, T1 = 1_700_000_000, 1_700_086_400


def world(stack):
    """Components a, b, c, d; a change to a's path lands on main after T0."""
    first = stack.commit(
        T0 + 10,
        **{
            "scripts__release-gauge__components.txt": "# owned paths\n"
            "a scripts/a\nb scripts/b skills/b\nc scripts/c\nd scripts/d\n",
            "scripts__a__x.sh": "1\n",
            "scripts__b__y.sh": "1\n",
            "skills__b__SKILL.md": "1\n",
            "scripts__c__z.sh": "1\n",
            "scripts__d__w.sh": "1\n",
        },
    )
    later = stack.commit(T1, **{"scripts__a__x.sh": "2\n"})
    stack.commit(T1 + 60, **{"scripts__a__x.sh": "3\n"})
    return first, later


def test_a_stale_stamp_is_behind_since_the_oldest_change_it_lacks(gauge, stack):
    first, _ = world(stack)
    stack.stamp("a", first)
    got = drift(gauge)
    assert got[("lifekit_component_behind", "a")] == 1
    assert got[("lifekit_component_behind_since_seconds", "a")] == T1


def test_a_stamp_at_or_after_the_last_change_to_its_paths_is_current(gauge, stack):
    first, later = world(stack)
    stack.stamp("b", first)  # only a's paths changed since
    stack.stamp("a", stack.git("rev-parse", "HEAD"))
    got = drift(gauge)
    assert got[("lifekit_component_behind", "a")] == 0
    assert got[("lifekit_component_behind", "b")] == 0
    assert ("lifekit_component_behind_since_seconds", "a") not in got
    assert ("lifekit_component_behind_since_seconds", "b") not in got


def test_a_component_without_a_stamp_is_unknown_not_current(gauge, stack):
    world(stack)
    stack.stamp("d", "f" * 40)  # a commit the checkout does not have
    got = drift(gauge)
    assert got[("lifekit_component_behind", "c")] == -1
    assert got[("lifekit_component_behind", "d")] == -1
    assert ("lifekit_component_behind_since_seconds", "c") not in got


def test_the_manifest_is_read_at_origin_main_not_the_working_tree(gauge, stack):
    first, _ = world(stack)
    stack.stamp("a", first)
    (stack.repo / MANIFEST).write_text("other scripts/a\n")
    assert ("lifekit_component_behind", "a") in drift(gauge)


def test_an_unreadable_manifest_keeps_the_previous_file_and_fails(gauge, stack):
    *_, out = gauge
    first, _ = world(stack)
    stack.stamp("a", first)
    before = drift(gauge)
    stack.commit(T1 + 120, **{"scripts__release-gauge__components.txt": "Bad Name x\n"})
    proc = drift(gauge, expect=1)
    assert "components.txt" in proc.stderr
    assert (out / "weekly_release.prom").exists()  # the release half still ran
    text = (out / "lifekit_installed.prom").read_text()
    assert 'lifekit_component_behind{component="a"} 1' in text
    assert before[("lifekit_component_behind", "a")] == 1


# --- the stamp and who writes it ------------------------------------------------

STAMP = REPO / "scripts/lib/install-stamp.sh"
NOT_DEPLOYING = {"ensure-ops-door.sh"}  # checks the ops door; installs nothing
STAMP_CALL = re.compile(r"lib/install-stamp\.sh\"? ([a-z0-9-]+)")


def installers():
    return sorted(
        p
        for p in [
            *REPO.glob("scripts/ensure-*.sh"),
            *REPO.glob("scripts/*/install-*.sh"),
        ]
        if p.name not in NOT_DEPLOYING and p != STAMP
    )


def manifest():
    rows = {}
    for line in (REPO / MANIFEST).read_text().splitlines():
        line = line.split("#", 1)[0].split()
        if line:
            rows[line[0]] = line[1:]
    return rows


def test_every_installer_stamps_a_component_the_manifest_lists():
    listed = manifest()
    stamped = {}
    for script in installers():
        names = set(STAMP_CALL.findall(script.read_text()))
        assert (
            len(names) == 1
        ), f"{script.relative_to(REPO)} stamps {names or 'nothing'}"
        (name,) = names
        assert name in listed, f"{name} ({script.name}) is not in {MANIFEST}"
        stamped[name] = script
    assert set(listed) == set(stamped), "a manifest row no installer stamps"


def test_every_manifest_path_exists_and_each_row_owns_one():
    for name, paths in manifest().items():
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
        assert paths, name
        for path in paths:
            assert (REPO / path).exists(), f"{name}: {path}"


def run_stamp(tmp_path, *args, **env):
    clean = {k: v for k, v in os.environ.items() if k != "LIFEKIT_INSTALLED_DIR"}
    return subprocess.run(
        ["bash", str(STAMP), *args],
        env={**clean, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_stamp_records_this_checkouts_head_under_install_root(tmp_path):
    proc = run_stamp(tmp_path, "fleet-publisher", INSTALL_ROOT=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    stamp = json.loads(
        (tmp_path / "var/lib/lifekit/installed/fleet-publisher").read_text()
    )
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert stamp["commit"] == head
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp["at"])
    assert [p.name for p in (tmp_path / "var/lib/lifekit/installed").iterdir()] == [
        "fleet-publisher"
    ]


@pytest.mark.parametrize("name", ["", "Fleet", "../x", "-x"])
def test_the_stamp_refuses_a_bad_component_name(tmp_path, name):
    assert (
        run_stamp(tmp_path, name, LIFEKIT_INSTALLED_DIR=str(tmp_path)).returncode == 2
    )
    assert not list(tmp_path.iterdir())


def run_fleet_installer(tmp_path, systemctl_exit):
    fake = tmp_path / "systemctl"
    fake.write_text(f"#!/usr/bin/env bash\nexit {systemctl_exit}\n")
    fake.chmod(0o755)
    root = tmp_path / "root"
    env = {k: v for k, v in os.environ.items() if k != "LIFEKIT_INSTALLED_DIR"}
    proc = subprocess.run(
        ["bash", str(REPO / "scripts/fleet-publisher/install-fleet-publisher.sh")],
        env={
            **env,
            "FM_HOMES": "/srv/fleet-home",
            "TASKS_AXI": "/opt/tools/bin/tasks-axi",
            "INSTALL_ROOT": str(root),
            "SYSTEMCTL": str(fake),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return proc, root / "var/lib/lifekit/installed/fleet-publisher"


def test_an_installer_stamps_on_success(tmp_path):
    proc, stamp = run_fleet_installer(tmp_path, 0)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(stamp.read_text())["commit"]


def test_an_installer_that_fails_writes_no_stamp(tmp_path):
    proc, stamp = run_fleet_installer(tmp_path, 1)
    assert proc.returncode != 0
    assert not stamp.exists()


def test_a_rerun_at_the_same_commit_keeps_the_stamp_and_is_silent(tmp_path):
    first = run_stamp(tmp_path, "scout", LIFEKIT_INSTALLED_DIR=str(tmp_path))
    assert first.returncode == 0 and first.stdout
    stamp = tmp_path / "scout"
    stamp.write_text(stamp.read_text().replace('"at": "', '"at": "1999'))
    kept = stamp.read_text()
    again = run_stamp(tmp_path, "scout", LIFEKIT_INSTALLED_DIR=str(tmp_path))
    assert (again.returncode, again.stdout) == (0, "")
    assert stamp.read_text() == kept
    stamp.write_text(kept.replace(json.loads(kept)["commit"], "0" * 40))
    assert run_stamp(tmp_path, "scout", LIFEKIT_INSTALLED_DIR=str(tmp_path)).stdout
    assert json.loads(stamp.read_text())["commit"] != "0" * 40
