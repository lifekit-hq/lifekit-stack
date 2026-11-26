"""`--check` on every cron-installing ensure-*.sh, and the deploy sweep over them (#678).

Each script's own test module already stubs `docker` with a fake gateway whose
`cron list` prints a state file and whose `cron add`/`edit`/`enable` apply their
flags to it; these tests reuse those fakes. For every declaration:

- drift: on a box without the row (or with a row that differs), `--check` exits 1,
  names the drift, and writes nothing;
- agreement: after a converge, `--check` exits 0 and writes nothing.

`scripts/check-cron-declarations.sh` then runs every `--check`; deploy.sh fails on
its non-zero exit.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_finance_pulse as fp  # noqa: E402
import test_learnclaw_morning as lm  # noqa: E402
import test_leetcode_daily as lc  # noqa: E402
import test_planning_reminder as pr  # noqa: E402
import test_reader_agent as ra  # noqa: E402
import test_today_brief_fleet as tb  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
SWEEP = SCRIPTS / "check-cron-declarations.sh"
WRITES = (["cron", "add"], ["cron", "edit"], ["cron", "enable"], ["cron", "rm"])


def run(script: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True
    )


def writes(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return [c for c in calls if c[:2] in WRITES]


def assert_drift(r: subprocess.CompletedProcess, needle: str) -> None:
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert needle in r.stderr, r.stderr


def assert_agrees(r: subprocess.CompletedProcess, job: str) -> None:
    assert r.returncode == 0, r.stderr
    assert f"ok: cron '{job}'" in r.stdout, r.stdout


# --- the full declarations: one fake each, from the script's own tests -------------

FULL = [
    # (module, script, job name, env for the fake box)
    (pr, SCRIPTS / "ensure-planning-reminder.sh", "planning-reminder", {}),
    (lm, SCRIPTS / "ensure-learnclaw-morning.sh", "learnclaw-morning", {}),
    (lc, SCRIPTS / "ensure-leetcode-daily.sh", "leetcode-daily", {}),
    (
        lc,
        SCRIPTS / "ensure-learnclaw-weekcount.sh",
        "learnclaw-weekcount",
        {"LEARNCLAW_WEEKCOUNT_CHAT": "424242"},
    ),
]


@pytest.mark.parametrize("mod,script,job,extra", FULL, ids=[f[2] for f in FULL])
def test_a_missing_row_is_drift_and_nothing_is_written(
    tmp_path, mod, script, job, extra
):
    state, env = mod.fake_box(tmp_path, [], extra)
    r = run(script, env, "--check")
    assert_drift(r, f"drift: cron '{job}' is declared by {script.name} but not live")
    assert json.loads(state.read_text())["jobs"] == []
    assert writes(tmp_path / "calls.jsonl") == []


@pytest.mark.parametrize("mod,script,job,extra", FULL, ids=[f[2] for f in FULL])
def test_a_converged_row_agrees_and_check_writes_nothing(
    tmp_path, mod, script, job, extra
):
    state, env = mod.fake_box(tmp_path, [], extra)
    assert run(script, env).returncode == 0
    before, logged = state.read_text(), len(writes(tmp_path / "calls.jsonl"))
    assert_agrees(run(script, env, "--check"), job)
    assert state.read_text() == before
    assert len(writes(tmp_path / "calls.jsonl")) == logged


@pytest.mark.parametrize("mod,script,job,extra", FULL, ids=[f[2] for f in FULL])
def test_a_paused_row_is_drift_and_check_leaves_it_paused(
    tmp_path, mod, script, job, extra
):
    state, env = mod.fake_box(tmp_path, [], extra)
    assert run(script, env).returncode == 0
    data = json.loads(state.read_text())
    data["jobs"][0]["enabled"] = False
    state.write_text(json.dumps(data))
    assert_drift(run(script, env, "--check"), "enabled=False, want enabled")
    assert json.loads(state.read_text())["jobs"][0]["enabled"] is False


@pytest.mark.parametrize(
    "field,value,needle",
    [
        ("expr", "0 3 * * 0", "schedule '0 3 * * 0', want"),
        ("tz", "Pacific/Auckland", "schedule.tz='Pacific/Auckland', want"),
    ],
)
@pytest.mark.parametrize("mod,script,job,extra", FULL, ids=[f[2] for f in FULL])
def test_a_hand_moved_schedule_is_drift(
    tmp_path, mod, script, job, extra, field, value, needle
):
    state, env = mod.fake_box(tmp_path, [], extra)
    assert run(script, env).returncode == 0
    data = json.loads(state.read_text())
    data["jobs"][0]["schedule"][field] = value
    state.write_text(json.dumps(data))
    assert_drift(run(script, env, "--check"), needle)


def test_an_agent_turn_leetcode_row_is_drift_not_replaced(tmp_path):
    old = {
        "id": "old1",
        "name": "leetcode-daily",
        "agentId": "learnclaw",
        "payload": {"kind": "agentTurn", "message": "x", "model": lc.MODEL},
    }
    state, env = lc.fake_box(tmp_path, [old])
    r = run(SCRIPTS / "ensure-leetcode-daily.sh", env, "--check")
    assert_drift(r, "payload.kind='agentTurn', want command")
    assert json.loads(state.read_text())["jobs"] == [old]
    assert writes(tmp_path / "calls.jsonl") == []


def test_a_bad_argument_is_a_usage_error_not_drift(tmp_path):
    state, env = pr.fake_box(tmp_path, [])
    r = run(SCRIPTS / "ensure-planning-reminder.sh", env, "--chek")
    assert r.returncode == 2 and "usage:" in r.stderr


# --- the reader's two declarations: doc-door and the needs-you mail pair ------------


@pytest.fixture
def reader_box(tmp_path):
    box = ra.make_box(tmp_path)
    box.put("entry", ra.ENTRY)
    assert box.run().returncode == 0
    return box


@pytest.mark.parametrize(
    "script,job",
    [
        ("ensure-doc-door.sh", "doc-door"),
        ("ensure-needs-you-digest.sh", "needs-you-digest"),
    ],
)
def test_the_reader_jobs_check_drift_then_agreement(reader_box, script, job):
    r = reader_box.script(script, "--check")
    assert r.returncode == 1 and "drift:" in r.stderr, r.stderr
    assert reader_box.calls() == [
        c for c in reader_box.calls() if c[:2] == ["config", "patch"]
    ]
    assert reader_box.script(script).returncode == 0
    before, calls = reader_box.get("jobs"), len(reader_box.calls())
    assert_agrees(reader_box.script(script, "--check"), job)
    assert reader_box.get("jobs") == before and len(reader_box.calls()) == calls


def test_a_doc_door_row_left_on_kit_is_drift(reader_box):
    assert reader_box.script("ensure-doc-door.sh").returncode == 0
    jobs = reader_box.get("jobs")
    (row,) = [j for j in jobs if j["name"] == "doc-door"]
    row["agentId"] = "kit"
    reader_box.put("jobs", jobs)
    r = reader_box.script("ensure-doc-door.sh", "--check")
    assert_drift(r, "agentId='kit', want 'reader'")


def test_a_mail_reader_without_its_gate_is_drift(reader_box):
    # the gate needs door.py in the reader's workspace, which ensure-doc-door.sh installs
    assert reader_box.script("ensure-doc-door.sh").returncode == 0
    assert reader_box.script("ensure-needs-you-digest.sh").returncode == 0
    assert_agrees(
        reader_box.script("ensure-needs-you-digest.sh", "--check"), "needs-you-digest"
    )
    jobs = reader_box.get("jobs")
    (row,) = [j for j in jobs if j["name"] == "needs-you-mail-reader"]
    row.pop("trigger", None)
    reader_box.put("jobs", jobs)
    r = reader_box.script("ensure-needs-you-digest.sh", "--check")
    assert_drift(r, "trigger is not the mail.js gate")


@pytest.mark.parametrize(
    "script,row,job",
    [
        ("ensure-doc-door.sh", "doc-door", "doc-door"),
        ("ensure-needs-you-digest.sh", "needs-you-mail-reader", "needs-you-digest"),
        ("ensure-needs-you-digest.sh", "needs-you-digest", "needs-you-digest"),
    ],
)
def test_a_reader_row_on_the_model_with_its_account_agrees(
    reader_box, script, row, job
):
    # cron_model_account (scripts/lib/cron-model.sh): the same model, account recorded
    assert reader_box.script("ensure-doc-door.sh").returncode == 0
    assert reader_box.script(script).returncode == 0
    jobs = reader_box.get("jobs")
    (live,) = [j for j in jobs if j["name"] == row]
    live["payload"]["model"] = ra.POLICY_MODEL + "@anthropic:api-key"
    reader_box.put("jobs", jobs)
    assert_agrees(reader_box.script(script, "--check"), job)
    live["payload"]["model"] = ra.POLICY_MODEL + "@anthropic:other"
    reader_box.put("jobs", jobs)
    assert reader_box.script(script, "--check").returncode == 1


@pytest.mark.parametrize(
    "field,value,needle",
    [
        ("expr", "0 3 * * 0", "schedule '0 3 * * 0', want"),
        ("tz", "Pacific/Auckland", "schedule.tz='Pacific/Auckland', want"),
    ],
)
@pytest.mark.parametrize(
    "script,row",
    [
        ("ensure-doc-door.sh", "doc-door"),
        ("ensure-needs-you-digest.sh", "needs-you-mail-reader"),
        ("ensure-needs-you-digest.sh", "needs-you-digest"),
    ],
)
def test_a_hand_moved_reader_row_schedule_is_drift(
    reader_box, script, row, field, value, needle
):
    assert reader_box.script("ensure-doc-door.sh").returncode == 0
    assert reader_box.script(script).returncode == 0
    jobs = reader_box.get("jobs")
    (live,) = [j for j in jobs if j["name"] == row]
    live["schedule"][field] = value
    reader_box.put("jobs", jobs)
    assert_drift(reader_box.script(script, "--check"), needle)


def test_the_digest_without_an_owner_chat_id_cannot_check(reader_box, tmp_path):
    env_file = tmp_path / "no-chat.env"
    env_file.write_text("SOMETHING_ELSE=1\n")
    no_chat = {"NEEDS_YOU_ALERT_CHAT": "", "ENV_FILE": str(env_file)}
    r = reader_box.script("ensure-needs-you-digest.sh", "--check", **no_chat)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "no owner chat id for the failure alert" in r.stderr
    assert reader_box.script("ensure-needs-you-digest.sh", **no_chat).returncode == 1


def test_a_refused_door_edit_is_still_drift_under_check(reader_box):
    # converge tolerates the refusal (next deploy retries); --check names the drift
    reader_box.put(
        "jobs",
        [
            {
                "id": "job1",
                "name": "doc-door",
                "agentId": "reader",
                "sessionTarget": "isolated",
                "delivery": {"mode": "none"},
                "payload": {"toolsAllow": ["exec"], "model": ra.POLICY_MODEL},
            }
        ],
    )
    assert reader_box.script("ensure-doc-door.sh", "--check").returncode == 1


# --- the marker editors: finance pulse, today brief, goal check-in -------------------


def test_finance_pulse_check(tmp_path):
    r, state = fp.run(tmp_path, "old checklist")
    assert r.returncode == 0, r.stderr
    edits = len(fp.edits(tmp_path))
    env = fp_env(tmp_path)
    assert_agrees(run(fp.SCRIPT, env, "--check"), "ledger-pulse")
    data = json.loads(state.read_text())
    data["jobs"][1]["payload"]["timeoutSeconds"] = 180
    state.write_text(json.dumps(data))
    assert_drift(run(fp.SCRIPT, env, "--check"), "payload.timeoutSeconds")
    assert len(fp.edits(tmp_path)) == edits


def test_finance_pulse_check_accepts_the_model_with_its_account(tmp_path):
    r, state = fp.run(tmp_path, "x")
    assert r.returncode == 0, r.stderr
    data = json.loads(state.read_text())
    model = data["jobs"][1]["payload"]["model"]
    data["jobs"][1]["payload"]["model"] = model + "@anthropic:api-key"
    state.write_text(json.dumps(data))
    assert_agrees(run(fp.SCRIPT, fp_env(tmp_path), "--check"), "ledger-pulse")


def fp_env(tmp_path):
    return {
        **os.environ,
        "ENV_FILE": str(tmp_path / "stack.env"),
        "FINANCE_PULSE_ALERT_CHAT": "",
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "FAKE_STATE": str(tmp_path / "state.json"),
        "GATE_FILE": str(fp.GATE / "gate.js"),
        "OPENCLAW_CONFIG_DIR": str(tmp_path / "config"),
    }


def tb_env(tmp_path):
    return {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "FAKE_STATE": str(tmp_path / "state.json"),
    }


def test_today_brief_check(tmp_path):
    r, _ = tb.run(tmp_path)  # the prompt lacks the step: the run adds it
    assert r.returncode == 0, r.stderr
    assert_agrees(run(tb.SCRIPT, tb_env(tmp_path), "--check"), "today-brief")
    # a fresh box whose prompt lacks the step: drift, and nothing is edited
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "bin").mkdir()
    (fresh / "bin/docker").write_text(tb.FAKE_DOCKER)
    (fresh / "bin/docker").chmod(0o755)
    (fresh / "state.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job1",
                        "name": "today-brief",
                        "enabled": True,
                        "payload": {"kind": "agentTurn", "message": tb.PROMPT},
                    }
                ]
            }
        )
    )
    r = run(tb.SCRIPT, tb_env(fresh), "--check")
    assert_drift(r, "drift: cron 'today-brief' (job1)")
    assert tb.edits(fresh) == []


def test_today_brief_check_without_the_row_is_drift(tmp_path):
    tb.run(tmp_path, name="something-else")
    r = run(tb.SCRIPT, tb_env(tmp_path), "--check")
    assert_drift(r, "drift: cron 'today-brief' is declared by")


def test_goal_checkin_check(tmp_path):
    state, env = lc.fake_box(tmp_path, [lc.CHECKIN])
    assert_drift(
        run(lc.CHECKIN_SCRIPT, env, "--check"), "does not carry the solved-count block"
    )
    assert writes(tmp_path / "calls.jsonl") == []
    assert run(lc.CHECKIN_SCRIPT, env).returncode == 0
    assert_agrees(run(lc.CHECKIN_SCRIPT, env, "--check"), "goal-weekly-checkin")
    state.write_text(json.dumps({"jobs": []}))
    assert_drift(run(lc.CHECKIN_SCRIPT, env, "--check"), "declared by")


# --- memory-vault-cleanup: a fake of its own (it reads the list with jq) -------------

MVC = SCRIPTS / "ensure-memory-vault-cleanup.sh"


@pytest.fixture
def mvc_env(tmp_path):
    state, env = pr.fake_box(tmp_path, [])
    # pr's fake models add/edit/enable; it does not keep --message or --description,
    # so wrap it with one that does.
    docker = tmp_path / "bin/docker"
    docker.write_text(
        pr.FAKE_DOCKER.replace(
            '    if flag("--session") is not None:',
            '    for f, k in (("--message", "message"),):\n'
            "        if flag(f) is not None:\n"
            "            p[k] = flag(f)\n"
            '    if flag("--description") is not None:\n'
            '        job["description"] = flag("--description")\n'
            '    if "--no-deliver" in cmd:\n'
            '        job["delivery"] = {"mode": "none"}\n'
            '    if flag("--session") is not None:',
            1,
        ).replace(
            '    d = job.setdefault("delivery", {})',
            '    d = job.setdefault("delivery", {"mode": "none"})',
            1,
        )
    )
    return state, env


def test_memory_vault_cleanup_check(tmp_path, mvc_env):
    state, env = mvc_env
    assert_drift(run(MVC, env, "--check"), "drift: cron 'memory-vault-cleanup'")
    assert run(MVC, env).returncode == 0
    assert_agrees(run(MVC, env, "--check"), "memory-vault-cleanup")
    data = json.loads(state.read_text())
    data["jobs"][0]["payload"]["message"] = "an older prompt"
    state.write_text(json.dumps(data))
    logged = len(writes(tmp_path / "calls.jsonl"))
    assert_drift(
        run(MVC, env, "--check"), "payload.message is not the declared message"
    )
    assert len(writes(tmp_path / "calls.jsonl")) == logged
    # the converge puts the declared prompt back, and enables a paused row
    data["jobs"][0]["enabled"] = False
    state.write_text(json.dumps(data))
    assert run(MVC, env).returncode == 0
    assert_agrees(run(MVC, env, "--check"), "memory-vault-cleanup")


# --- the sweep deploy.sh runs --------------------------------------------------------


STUB_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as f:
    f.write(json.dumps(args) + "\n")
mode = os.environ.get("STUB_MODE", "")
if args[:1] == ["ps"]:
    if mode != "no-gateway":
        print("gw1")
    sys.exit(0)
cmd = args[args.index("openclaw") + 1 :] if "openclaw" in args else args
if cmd[:2] == ["cron", "list"]:
    if mode == "list-fails":
        sys.exit("gateway closed the connection")
    print(json.dumps({"jobs": []}))
    sys.exit(0)
if cmd[:2] == ["config", "get"]:
    sys.exit("Config path not found: " + cmd[2])
sys.exit("stub: no such call")
"""


def stub_box(tmp_path: Path, mode: str = "") -> dict:
    """A box the declarations have never touched: an empty cron list, no reader."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(STUB_DOCKER)
    (bindir / "docker").chmod(0o755)
    (tmp_path / "stack.env").write_text("LIFEKIT_TELEGRAM_CHAT=4242\n")
    (tmp_path / "config").mkdir()
    return {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls.jsonl"),
        "STUB_MODE": mode,
        "ENV_FILE": str(tmp_path / "stack.env"),
        "OPENCLAW_CONFIG_DIR": str(tmp_path / "config"),
    }


def stub_writes(tmp_path: Path) -> list[list[str]]:
    calls = tmp_path / "calls.jsonl"
    lines = calls.read_text().splitlines() if calls.exists() else []
    out = []
    for args in map(json.loads, lines):
        cmd = args[args.index("openclaw") + 1 :] if "openclaw" in args else args
        if cmd[:2] in WRITES or cmd[:2] == ["config", "patch"]:
            out.append(args)
    return out


def sweep_table(r: subprocess.CompletedProcess) -> dict[str, str]:
    """The sweep's own report: script name -> ok / drift / error."""
    rows = [ln.split() for ln in r.stdout.splitlines()[1:]]
    return {row[0]: row[1] for row in rows if row and row[0].endswith(".sh")}


def cron_installers() -> list[str]:
    return sorted(
        p.name
        for p in SCRIPTS.glob("ensure-*.sh")
        if re.search(r"openclaw cron (add|edit)\b|cron edit \"\$", p.read_text())
    )


def test_the_sweep_checks_every_cron_installing_script_and_none_writes(tmp_path):
    env = stub_box(tmp_path)
    r = subprocess.run(["bash", str(SWEEP)], env=env, capture_output=True, text=True)
    table = sweep_table(r)
    assert cron_installers(), "no cron-installing script found; fix the pattern"
    assert sorted(table) == cron_installers(), r.stdout
    for name in table:
        c = run(SCRIPTS / name, env, "--check")
        assert c.returncode != 2, (name, c.stderr)
        assert "usage:" not in c.stderr, (name, c.stderr)
    assert stub_writes(tmp_path) == []
    assert [p for p in (tmp_path / "config").rglob("*")] == []


@pytest.mark.parametrize("mode", ["no-gateway", "list-fails"])
def test_a_check_that_cannot_run_exits_3_and_the_sweep_calls_it_error(tmp_path, mode):
    env = stub_box(tmp_path, mode)
    r = subprocess.run(["bash", str(SWEEP)], env=env, capture_output=True, text=True)
    assert r.returncode == 1
    table = sweep_table(r)
    # doc-door declares nothing until the reader agent exists, so it never reads
    # the list on this box.
    skipped = {"ensure-doc-door.sh"} if mode == "list-fails" else set()
    assert {n: s for n, s in table.items() if n not in skipped} == {
        n: "error" for n in cron_installers() if n not in skipped
    }, r.stdout
    for name in sorted(set(table) - skipped):
        c = run(SCRIPTS / name, env, "--check")
        assert c.returncode == 3, (name, c.stdout, c.stderr)
    assert stub_writes(tmp_path) == []


def test_without_a_gateway_a_converge_keeps_its_exit_code(tmp_path):
    env = stub_box(tmp_path, "no-gateway")
    assert run(SCRIPTS / "ensure-planning-reminder.sh", env).returncode == 1
    assert run(SCRIPTS / "ensure-kit-cron-models.sh", env).returncode == 0


def sweep(tmp_path, scripts: dict[str, int]):
    """Run the sweep over stub scripts that exit with the given codes."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    names = []
    for name, rc in scripts.items():
        verdict = {0: "ok: cron 'x' agrees", 1: "drift: cron 'x' differs"}.get(
            rc, "boom"
        )
        (stubs / name).write_text(
            f'#!/usr/bin/env bash\n[ "$1" = --check ] || exit 9\necho "{verdict}"\nexit {rc}\n'
        )
        names.append(os.path.relpath(stubs / name, SCRIPTS))
    return subprocess.run(
        ["bash", str(SWEEP)],
        env={**os.environ, "CRON_DECLARATIONS": " ".join(names)},
        capture_output=True,
        text=True,
    )


def test_the_sweep_passes_when_every_declaration_agrees(tmp_path):
    r = sweep(tmp_path, {"a.sh": 0, "b.sh": 0})
    assert r.returncode == 0, r.stderr
    assert "all 2 cron declarations match live" in r.stdout


@pytest.mark.parametrize("rc,state", [(1, "drift"), (3, "error")])
def test_the_sweep_fails_on_one_drift_or_error_and_still_reports_the_rest(
    tmp_path, rc, state
):
    r = sweep(tmp_path, {"a.sh": 0, "b.sh": rc, "c.sh": 0})
    assert r.returncode == 1
    lines = {
        ln.split()[0].rsplit("/", 1)[-1]: ln.split()[1]
        for ln in r.stdout.splitlines()[1:]
    }
    assert lines == {"a.sh": "ok", "b.sh": state, "c.sh": "ok"}
    assert "1 cron declaration(s) do not match live" in r.stderr
