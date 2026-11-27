"""Behaviour of scripts/intake-shape against a fake firstmate home and a fake gh.

What this file pins: an accept creates exactly one shaping item (a repeat adds a
count to it, a repeat of a closed one opens a new item referencing it); the
owner's answer files exactly one issue with the `ready` label and marks the
intake row `handed-off` with the issue as `ref`; an issue missing `## Done when`
is refused the label; a drop files nothing and an unfilable answer reopens the same item. All
fixtures are synthetic.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/intake-shape/intake_shape.py"

spec = importlib.util.spec_from_file_location("intake_shape", SCRIPT)
shape = importlib.util.module_from_spec(spec)
sys.modules["intake_shape"] = shape
spec.loader.exec_module(shape)

HOLD = """#!{py}
import json, os, sys
s = os.environ["STUB_STATE"] + "/hub.json"
d = json.load(open(s)) if os.path.exists(s) else {{}}
a = sys.argv[1:]
if a[0] == "open":
    st = d.get(a[1], {{}}).get("state")
    sys.exit({{"open": 0, "closed": 1, None: 3}}[st])
if a[0] == "hold":
    opt = dict(zip(a[2::2], a[3::2]))
    old = d.get(a[1], {{}})
    d[a[1]] = {{
        "state": "open",
        "title": opt.get("--title", old.get("title")),
        "reason": opt["--reason"],
        "holds": old.get("holds", 0) + 1,
    }}
    json.dump(d, open(s, "w"))
"""
TASKS = """#!{py}
import json, os, sys
s = os.environ["STUB_STATE"] + "/hub.json"
d = json.load(open(s))
a = sys.argv[1:]
if a[0] == "update":
    d[a[1]]["title"] = a[3]
    json.dump(d, open(s, "w"))
if a[0] == "reopen":
    d[a[1]]["state"] = "queued"
    d[a[1]].pop("decision", None)
    json.dump(d, open(s, "w"))
if a[0] == "show":
    text = d[a[1]].get("decision")
    body = "Captain hold set: x" if text is None else (
        "Resolution recorded by fm-captain-hold.\\n\\nCaptain decision:\\n" + text)
    print("task:")
    print("  body: " + json.dumps(body))
"""
GH = """#!{py}
import json, os, sys
s = os.environ["STUB_STATE"] + "/gh.json"
d = json.load(open(s)) if os.path.exists(s) else {{"issues": [], "labels": []}}
a = sys.argv[1:]
def save(): json.dump(d, open(s, "w"))
if a[:2] == ["issue", "list"]:
    print(json.dumps(d["issues"]))
elif a[:2] == ["issue", "create"]:
    opt = dict(zip(a[2::2], a[3::2]))
    url = "https://github.com/%s/issues/%d" % (opt["--repo"], len(d["issues"]) + 1)
    d["issues"].append({{"url": url, "body": opt["--body"], "title": opt["--title"]}})
    save(); print(url)
elif a[:2] == ["issue", "view"]:
    body = next(i["body"] for i in d["issues"] if i["url"] == a[2])
    if os.environ.get("STUB_STRIP_DONE"):
        body = body.replace("## Done when", "## Notes")
    print(json.dumps({{"body": body}}))
elif a[:2] == ["issue", "edit"]:
    d["labels"].append([a[2], a[4]]); save()
"""

# A fake fleet-policy: `classify` answers STUB_FP_CELL for the step; `decide
# --kind self` logs to self-calls.jsonl and p16.jsonl; `decide --kind call`
# logs to calls.jsonl and holds the item through the home's hold script with a
# reason shaped like the real one. STUB_FP_REFUSE makes decide refuse.
FLEET_POLICY = """#!{py}
import argparse, json, os, subprocess, sys
st = os.environ["STUB_STATE"]
a = sys.argv[1:]
def log(name, ent):
    with open(os.path.join(st, name), "a") as f:
        f.write(json.dumps(ent) + "\\n")
if a[0] == "classify":
    print(json.dumps({{"class": a[1], "cell": os.environ.get("STUB_FP_CELL", "allow-and-log"),
                      "rows": ["M2", "M7"], "subject": a[a.index("--subject") + 1]}}))
    sys.exit(0)
p = argparse.ArgumentParser()
p.add_argument("cmd"); p.add_argument("id")
for f in ("kind", "class", "subject", "rows", "question", "answer", "record", "actor", "home",
          "title", "rec", "reason", "boundary", "step", "conflict", "no-row"):
    p.add_argument("--" + f)
p.add_argument("--option", action="append", default=[])
p.add_argument("--checked", action="append", default=[])
o = vars(p.parse_args(a))
o["env_actor"] = os.environ.get("FLEET_POLICY_ACTOR")
if os.environ.get("STUB_FP_REFUSE"):
    print("decide: refused: " + os.environ["STUB_FP_REFUSE"], file=sys.stderr)
    sys.exit(2)
if o["kind"] == "self":
    log("self-calls.jsonl", o)
    log("p16.jsonl", dict(o, outcome="self"))
    sys.exit(0)
why = ("boundary %s: %s" % (o["boundary"], o["step"]) if o["boundary"] else
       "conflict " + o["conflict"].replace(",", " vs ") if o["conflict"] else "no row: " + o["no_row"])
reason = "[Tier 2 class=unlisted subject=%s] rec=%s - %s - checked: %s - %s" % (
    o["subject"], o["rec"], why, "; ".join(o["checked"]), o["reason"])
log("calls.jsonl", o)
log("p16.jsonl", dict(o, outcome="filed"))
sys.exit(subprocess.run([o["home"] + "/bin/fm-captain-hold.sh", "hold", o["id"],
                         "--reason", reason, "--title", o["title"]]).returncode)
"""

REPOS = "lifekit-stack=lifekit-hq/lifekit-stack clip=lifekit-hq/clip"
HEADER = (
    "# Intake index\n\n| date | kind | origin | category | source | title | gist | "
    "verdict | reason | project | recheck | next step or why | rubric | report |\n"
    + "|"
    + " --- |" * 14
    + "\n"
)


def row(
    key, *, verdict="accept", rubric="v8", project="lifekit-stack (deploy)", marks=""
):
    cells = [
        "2026-11-20", "video", "scout", "tool", f"https://youtu.be/{key}",
        f"Title {key}", "gist", verdict, "-", project, "-",
        "port the rubric; add the test", rubric, f"reports/{key}.{rubric}.md",
    ]  # fmt: skip
    return "| " + " | ".join(cells) + marks + " |\n"


class Env:
    def __init__(self, tmp: Path, monkeypatch):
        self.state = tmp / "state"
        self.state.mkdir()
        self.home = tmp / "home"
        (self.home / "bin").mkdir(parents=True)
        self.intake = tmp / "intake"
        self.intake.mkdir()
        bins = tmp / "stubs"
        bins.mkdir()
        for path, text in (
            (self.home / "bin/fm-captain-hold.sh", HOLD),
            (bins / "tasks-axi", TASKS),
            (bins / "gh", GH),
            (bins / "fleet-policy", FLEET_POLICY),
        ):
            path.write_text(text.format(py=sys.executable))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("STUB_STATE", str(self.state))
        monkeypatch.setenv("TASKS_AXI", str(bins / "tasks-axi"))
        monkeypatch.setenv("INTAKE_SHAPE_GH", str(bins / "gh"))
        monkeypatch.setenv("INTAKE_SHAPE_FLEET_POLICY", str(bins / "fleet-policy"))
        # The owner-answer tests need a held item: by default the fake table
        # does not answer the step, so every row is held (no row). The matrix
        # tests set allow-and-log, the real cell of github.issue-open.
        monkeypatch.setenv("STUB_FP_CELL", "stop-and-file")
        monkeypatch.setenv("INTAKE_SHAPE_REPOS", REPOS)
        monkeypatch.setenv("INTAKE_SHAPE_FM_HOME", str(self.home))
        monkeypatch.setenv("INTAKE_SHAPE_INTAKE", str(self.intake))
        (self.intake / ".shape-since").write_text("2026-01-01\n")

    def index(self, *rows: str) -> None:
        (self.intake / "index.md").write_text(HEADER + "".join(rows))

    def append(self, line: str) -> None:
        with open(self.intake / "index.md", "a") as f:
            f.write(line)

    def rows(self):
        return shape.read_rows(self.intake)

    def hub(self) -> dict:
        p = self.state / "hub.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def set_hub(self, item: str, **fields) -> None:
        d = self.hub()
        d[item].update(fields)
        (self.state / "hub.json").write_text(json.dumps(d))

    def gh(self) -> dict:
        p = self.state / "gh.json"
        return json.loads(p.read_text()) if p.exists() else {"issues": [], "labels": []}

    def log(self, name: str) -> list[dict]:
        p = self.state / name
        return (
            [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []
        )

    def report(self, key: str, text: str, rubric: str = "v8") -> None:
        path = self.intake / f"reports/{key}.{rubric}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run(self, cmd="sweep", *extra) -> int:
        return shape.main(["intake_shape.py", cmd, *extra])

    def archive(self, item: str) -> None:
        d = self.hub()
        del d[item]
        (self.state / "hub.json").write_text(json.dumps(d))


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def test_accept_creates_exactly_one_shaping_item(env):
    env.index(row("abc"))
    assert env.run("open") == 0
    assert env.run("open") == 0
    hub = env.hub()
    assert list(hub) == ["shape-abc"]
    assert hub["shape-abc"]["title"] == "Shape: Title abc"
    assert "lifekit-hq/lifekit-stack" in hub["shape-abc"]["reason"]
    assert "port the rubric" in hub["shape-abc"]["reason"]
    [r] = env.rows()
    assert (r["status"], r["ref"]) == ("shaping", "shape-abc")


# ------------------------------------------------ the decision matrix first


@pytest.fixture
def matrix(env, monkeypatch):
    """The real cell of github.issue-open: the matrix answers the step."""
    monkeypatch.setenv("STUB_FP_CELL", "allow-and-log")
    return env


def test_matrix_answered_row_is_filed_with_no_owner_hold(matrix):
    env = matrix
    env.index(row("abc"))
    assert env.run() == 0
    assert env.hub() == {}
    assert env.log("calls.jsonl") == []
    [call] = env.log("self-calls.jsonl")
    assert (call["id"], call["kind"], call["class"]) == (
        "shape-abc",
        "self",
        "github.issue-open",
    )
    assert call["subject"] == "lifekit-hq/lifekit-stack"
    assert call["rows"] == "M4,M2,M7"
    assert "system/decision-matrix.md#M4" in call["record"]
    assert call["actor"] == call["env_actor"] == "intake-shape"
    assert [e["outcome"] for e in env.log("p16.jsonl")] == ["self"]
    gh = env.gh()
    [issue] = gh["issues"]
    assert issue["url"] == "https://github.com/lifekit-hq/lifekit-stack/issues/1"
    for line in (
        "- port the rubric",
        "Owner repo: lifekit-hq/lifekit-stack",
        "Class: dev",
        "Decision: matrix auto-answered",
        "Matrix: M4, M2, M7 (system/decision-matrix.md#M4; "
        "fleet-policy decide --kind self shape-abc)",
    ):
        assert line in issue["body"]
    assert shape.check_issue_body(issue["body"]) == []
    assert gh["labels"] == [[issue["url"], "ready"]]
    [r] = env.rows()
    assert (r["status"], r["ref"]) == ("handed-off", issue["url"])
    assert env.run() == 0
    assert len(env.gh()["issues"]) == 1
    assert len(env.log("self-calls.jsonl")) == 1


def test_owner_repo_named_only_in_the_report_is_answered(matrix):
    env = matrix
    env.index(row("abc", project="goal 1; plain upgrade of a tool already run"))
    env.report(
        "abc",
        "## Sources\nPin: lifekit-stack compose/openclaw-gateway/package.json\n"
        "Blocking PR: https://github.com/lifekit-hq/lifekit-stack/pull/501\n",
    )
    assert env.run() == 0
    assert env.hub() == {}
    [issue] = env.gh()["issues"]
    assert "Owner repo: lifekit-hq/lifekit-stack" in issue["body"]


def test_matrix_repeat_of_filed_work_joins_its_issue(matrix):
    env = matrix
    env.index(row("abc", rubric="v7"))
    assert env.run() == 0
    [issue] = env.gh()["issues"]
    env.append(row("abc", rubric="v8"))
    assert env.run() == 0
    assert len(env.gh()["issues"]) == 1 and env.hub() == {}
    assert len(env.log("self-calls.jsonl")) == 1
    assert [(r["status"], r["ref"]) for r in env.rows()] == [
        ("handed-off", issue["url"])
    ] * 2


def test_dropped_then_filed_repeat_is_not_a_conflict(matrix):
    env = matrix
    env.index(
        row("abc", rubric="v6", marks=" | dropped | shape-abc"),
        row("abc", rubric="v7", marks=" | handed-off | https://x/issues/9"),
    )
    env.append(row("abc", rubric="v8"))
    assert env.run() == 0
    assert env.log("calls.jsonl") == [] and env.hub() == {}
    assert env.rows()[-1]["ref"] == "https://x/issues/9"


def test_boundary_money_is_held_with_the_reason_decide_gave(matrix):
    env = matrix
    env.index(row("abc"))
    env.report(
        "abc",
        "## Cost of adoption\nThe pro plan is a paid plan at $20 per month.\n\n## Verdict\n",
    )
    assert env.run() == 0
    assert env.gh()["issues"] == [] and env.log("self-calls.jsonl") == []
    [call] = env.log("calls.jsonl")
    assert (call["boundary"], call["rows"], call["rec"]) == ("H2", "M4", "approve")
    assert "whose cost names money" in call["step"]
    assert any("$20 per month" in c for c in call["checked"])
    assert call["option"] == ["approve=File it as proposed", "drop=File nothing"]
    item = env.hub()["shape-abc"]
    assert item["reason"].startswith(
        "[Tier 2 class=unlisted subject=shape-abc] rec=approve - boundary H2"
    )
    assert "Intake accepted: Title abc" in item["reason"]
    assert item["title"] == "Shape: Title abc"
    assert env.rows()[0]["status"] == "shaping"


def test_a_cost_that_names_no_charge_is_not_a_boundary(matrix):
    env = matrix
    env.index(row("abc"))
    env.report(
        "abc",
        "## Cost of adoption\nNo new credential; the free tier, no paid plan.\n"
        "One-line change, about 10 minutes.\n",
    )
    assert env.run() == 0
    assert env.hub() == {}
    assert len(env.gh()["issues"]) == 1


def test_money_beside_an_unrelated_negation_is_a_boundary(matrix):
    env = matrix
    env.index(row("abc"), row("def"))
    for key, cost in (
        ("abc", "- Pro plan: $15 per month; the free tier lacks the API."),
        ("def", "No new credential; the API is $10/month."),
    ):
        env.report(key, f"## Cost of adoption\n{cost}\n\n## Verdict\n")
    assert env.run() == 0
    assert env.gh()["issues"] == [] and env.log("self-calls.jsonl") == []
    calls = env.log("calls.jsonl")
    assert sorted(c["id"] for c in calls) == ["shape-abc", "shape-def"]
    assert all(c["boundary"] == "H2" for c in calls)
    assert [(r["status"], r["ref"]) for r in env.rows()] == [
        ("shaping", "shape-abc"),
        ("shaping", "shape-def"),
    ]


def test_repeat_while_the_earlier_answer_is_unprocessed_waits(matrix):
    env = matrix
    env.index(row("abc", rubric="v7", project="goal 1"))
    env.report("abc", "nothing registered here\n", rubric="v7")
    assert env.run() == 0
    assert list(env.hub()) == ["shape-abc"]
    env.set_hub("shape-abc", state="closed", decision="drop")
    env.append(row("abc", rubric="v8"))
    assert env.run("open") == 0
    assert env.gh()["issues"] == [] and env.log("self-calls.jsonl") == []
    assert [(r["status"], r["ref"]) for r in env.rows()] == [
        ("shaping", "shape-abc"),
        ("", ""),
    ]
    assert env.run() == 0
    assert env.rows()[0]["status"] == "dropped"
    assert env.rows()[1]["status"] == ""
    assert env.run() == 0
    assert env.gh()["issues"] == [] and env.log("self-calls.jsonl") == []
    call = env.log("calls.jsonl")[-1]
    assert (call["id"], call["conflict"]) == ("shape-abc-2", "M2,M4")
    assert env.rows()[1]["ref"] == "shape-abc-2"


def test_repeat_of_a_dropped_item_is_a_row_conflict(matrix):
    env = matrix
    env.index(row("abc", rubric="v7", marks=" | dropped | shape-abc"))
    env.append(row("abc", rubric="v8"))
    assert env.run() == 0
    [call] = env.log("calls.jsonl")
    assert (call["id"], call["conflict"], call["rec"]) == (
        "shape-abc-2",
        "M2,M4",
        "drop",
    )
    assert call["rows"] == "M2,M4"
    item = env.hub()["shape-abc-2"]
    assert "conflict M2 vs M4" in item["reason"]
    assert "Repeat of shape-abc" in item["reason"]
    assert env.gh()["issues"] == []


def test_no_registered_repo_is_no_row_and_the_owner_picks_one(matrix):
    env = matrix
    env.index(row("abc", project="goal 1; nothing named"))
    assert env.run() == 0
    [call] = env.log("calls.jsonl")
    assert "no registered project owns" in call["no_row"]
    assert call["rows"] is None
    assert call["option"] == [
        "file-clip=File it in lifekit-hq/clip",
        "file-lifekit-stack=File it in lifekit-hq/lifekit-stack",
        "drop=File nothing",
    ]
    assert call["rec"] == "file-clip"
    assert "repo none registered - name one" in env.hub()["shape-abc"]["reason"]
    env.set_hub(
        "shape-abc",
        state="closed",
        decision="Captain answered this call through dashboard.\nTask: shape-abc\n"
        "Answer: file-clip\nAnswer as shown to the captain: File it in lifekit-hq/clip",
    )
    assert env.run() == 0
    [issue] = env.gh()["issues"]
    assert issue["url"] == "https://github.com/lifekit-hq/clip/issues/1"
    assert "Decision: owner ruled" in issue["body"]
    assert "Matrix:" not in issue["body"]
    assert env.rows()[0]["status"] == "handed-off"


def test_unregistered_project_cell_is_no_row_even_when_the_report_names_one(matrix):
    env = matrix
    env.index(row("abc", project="devclaw (goal 3, console)"))
    env.report("abc", "## Sources\nSee the lifekit-stack runbook.\n")
    assert env.run() == 0
    assert env.gh()["issues"] == [] and env.log("self-calls.jsonl") == []
    [call] = env.log("calls.jsonl")
    assert "no registered project owns" in call["no_row"]
    assert env.rows()[0]["status"] == "shaping"


def test_report_naming_two_projects_is_no_row(matrix):
    env = matrix
    env.index(row("abc", project="goal 1"))
    env.report("abc", "touches lifekit-stack and lifekit-hq/clip\n")
    assert env.run() == 0
    [call] = env.log("calls.jsonl")
    assert call["no_row"] and env.gh()["issues"] == []


def test_no_done_when_is_no_row(matrix):
    env = matrix
    env.index(row("abc").replace("port the rubric; add the test", "-"))
    assert env.run() == 0
    [call] = env.log("calls.jsonl")
    assert "no done-when" in call["no_row"]


def test_refused_decide_holds_nothing_and_retries(matrix, monkeypatch):
    env = matrix
    env.index(row("abc", project="goal 1"))
    monkeypatch.setenv("STUB_FP_REFUSE", "no")
    assert env.run() == 1
    assert env.hub() == {} and env.gh()["issues"] == []
    assert env.rows()[0]["status"] == ""
    monkeypatch.delenv("STUB_FP_REFUSE")
    assert env.run() == 0
    assert list(env.hub()) == ["shape-abc"]


def test_failed_self_call_files_nothing(matrix, monkeypatch):
    env = matrix
    env.index(row("abc"))
    monkeypatch.setenv("STUB_FP_REFUSE", "no")
    assert env.run() == 1
    assert env.gh()["issues"] == [] and env.hub() == {}
    assert env.rows()[0]["status"] == ""


def test_newest_recorded_answer_counts(env):
    answered(
        env,
        "Update\n\nResolution recorded by fm-captain-hold.\n\nCaptain decision:\n"
        "matrix: drop; nothing to file",
    )
    assert env.run("answer") == 0
    assert env.rows()[0]["status"] == "dropped"


def test_main_needs_fleet_policy(env, monkeypatch, capsys):
    monkeypatch.setenv(
        "INTAKE_SHAPE_FLEET_POLICY", str(env.home / "no-such-fleet-policy")
    )
    env.index(row("a"))
    assert env.run() == 2
    assert "fleet-policy not found" in capsys.readouterr().err
    assert env.hub() == {}


def test_park_and_drop_open_nothing(env):
    env.index(row("a", verdict="park"), row("b", verdict="drop"))
    assert env.run() == 0
    assert env.hub() == {}
    assert [r["status"] for r in env.rows()] == ["", ""]


def test_repeat_adds_a_count_to_the_open_item(env):
    env.index(row("abc", rubric="v7"))
    env.run("open")
    env.append(row("abc", rubric="v8"))
    env.run("open")
    env.run("open")
    hub = env.hub()
    assert list(hub) == ["shape-abc"]
    assert hub["shape-abc"]["title"] == "Shape: Title abc (seen 2x)"
    assert [r["ref"] for r in env.rows()] == ["shape-abc", "shape-abc"]


def test_closed_repeat_opens_a_new_item_referencing_it(env):
    env.index(row("abc", rubric="v7"))
    env.run("open")
    env.set_hub("shape-abc", state="closed", decision="approve")
    env.append(row("abc", rubric="v8"))
    env.run("open")
    assert list(env.hub()) == ["shape-abc"]
    assert env.rows()[1]["status"] == ""
    env.run("answer")
    env.run("open")
    hub = env.hub()
    assert sorted(hub) == ["shape-abc", "shape-abc-2"]
    assert hub["shape-abc-2"]["state"] == "open"
    assert "Repeat of shape-abc" in hub["shape-abc-2"]["reason"]


def test_archived_closed_item_still_chains_the_repeat(env):
    env.index(row("abc", rubric="v7"))
    env.run("open")
    env.set_hub("shape-abc", state="closed", decision="approve")
    env.run("answer")
    env.archive("shape-abc")
    env.append(row("abc", rubric="v8"))
    assert env.run("open") == 0
    hub = env.hub()
    assert list(hub) == ["shape-abc-2"]
    assert "Repeat of shape-abc" in hub["shape-abc-2"]["reason"]
    env.set_hub("shape-abc-2", state="closed", decision="drop")
    env.archive("shape-abc-2")
    env.append(row("abc", rubric="v9"))
    env.run("open")
    assert list(env.hub()) == ["shape-abc-3"]


def test_shaping_row_whose_item_was_archived_is_surfaced_not_re_held(env):
    env.index(row("abc"))
    env.run("open")
    env.archive("shape-abc")
    assert env.run() == 1
    assert env.hub() == {}
    assert env.gh()["issues"] == []
    assert env.rows()[0]["status"] == "shaping"


def test_open_item_held_but_row_unmarked_is_joined_not_duplicated(env, monkeypatch):
    def boom(*args):
        raise OSError("disk full")

    env.index(row("abc"))
    with monkeypatch.context() as patched:
        patched.setattr(shape, "mark_rows", boom)
        with pytest.raises(OSError):
            shape.open_items(
                env.intake,
                shape.Hub(str(env.home)),
                "2026-01-01",
                shape.Policy(str(env.home)),
                shape.Gh(),
            )
    assert env.rows()[0]["status"] == ""
    assert env.run("open") == 0
    assert list(env.hub()) == ["shape-abc"]
    assert env.rows()[0]["ref"] == "shape-abc"


def test_rows_before_the_cutoff_open_nothing(env):
    (env.intake / ".shape-since").write_text("2026-11-21\n")
    env.index(row("old"))
    env.append(row("new").replace("2026-11-20", "2026-11-21"))
    assert env.run("open") == 0
    assert list(env.hub()) == ["shape-new"]
    assert [r["status"] for r in env.rows()] == ["", "shaping"]


def test_first_run_records_today_as_the_cutoff(env):
    (env.intake / ".shape-since").unlink()
    today = shape.datetime.now(shape.timezone.utc).strftime("%Y-%m-%d")
    env.index(row("old"), row("new").replace("2026-11-20", today))
    assert env.run("open") == 0
    assert list(env.hub()) == ["shape-new"]
    assert (env.intake / ".shape-since").read_text().strip() == today


def test_since_option_sets_the_cutoff(env):
    (env.intake / ".shape-since").write_text("2099-01-01\n")
    env.index(row("old"))
    assert env.run("open", "--since", "2026-11-01") == 0
    assert list(env.hub()) == ["shape-old"]
    assert (env.intake / ".shape-since").read_text().strip() == "2026-11-01"
    assert env.run("open", "--since", "yesterday") == 2


def test_marking_rewrites_in_place_keeping_the_inode_and_mode(env):
    env.index(row("abc"))
    path = env.intake / "index.md"
    path.chmod(0o640)
    before = path.stat()
    env.run("open")
    after = path.stat()
    assert (after.st_ino, stat.S_IMODE(after.st_mode)) == (
        before.st_ino,
        0o640,
    )
    assert env.rows()[0]["status"] == "shaping"


def answered(env, text, key="abc", **kw):
    env.index(row(key, **kw))
    env.run("open")
    env.set_hub(f"shape-{key}", state="closed", decision=text)


def test_answer_files_one_issue_and_sets_ready(env):
    answered(env, "approve")
    assert env.run("answer") == 0
    assert env.run("answer") == 0
    gh = env.gh()
    [issue] = gh["issues"]
    assert issue["url"] == "https://github.com/lifekit-hq/lifekit-stack/issues/1"
    for line in (
        "## Done when",
        "- port the rubric",
        "- add the test",
        "Owner repo: lifekit-hq/lifekit-stack",
        "Class: dev",
        "Decision: owner ruled",
    ):
        assert line in issue["body"]
    assert gh["labels"] == [[issue["url"], "ready"]]
    [r] = env.rows()
    assert (r["status"], r["ref"]) == ("handed-off", issue["url"])


def test_matrix_answer_is_recorded_as_auto_answered(env):
    answered(env, "matrix: approve")
    env.run("answer")
    assert "Decision: matrix auto-answered" in env.gh()["issues"][0]["body"]


SETTLED = (
    "Captain answered this call through matrix settlement by firstmate "
    "(H4 server.root-installer + M4), not a captain answer.\n"
    "Task: shape-abc\n"
    "Answer: {answer}\n"
    "Answer as shown to the captain: matrix-settled"
)


def test_matrix_settled_drop_files_nothing_and_does_not_ask_again(env):
    answered(env, SETTLED.format(answer="drop: not worth shaping"))
    assert env.run("answer") == 0
    assert env.gh()["issues"] == []
    assert env.rows()[0]["status"] == "dropped"
    assert env.hub()["shape-abc"]["state"] == "closed"


def test_matrix_settled_approve_files_the_proposed_shape(env):
    answered(env, SETTLED.format(answer="approve"))
    assert env.run("answer") == 0
    [issue] = env.gh()["issues"]
    assert "Decision: matrix auto-answered" in issue["body"]
    assert "Owner repo: lifekit-hq/lifekit-stack" in issue["body"]
    assert "- port the rubric" in issue["body"]


def test_answer_overrides_repo_class_and_done_when(env):
    answered(env, "repo: lifekit-hq/clip\nclass: ops\ndone when: one clip ships")
    env.run("answer")
    [issue] = env.gh()["issues"]
    assert "https://github.com/lifekit-hq/clip/issues/1" == issue["url"]
    assert "Class: ops" in issue["body"]
    assert "- one clip ships" in issue["body"]
    assert "port the rubric" not in issue["body"]


def test_crash_after_create_does_not_file_twice(env, monkeypatch):
    answered(env, "approve")
    real = shape.set_ready
    monkeypatch.setattr(shape, "set_ready", lambda gh, url: ["boom"])
    assert env.run("answer") == 1
    monkeypatch.setattr(shape, "set_ready", real)
    assert env.run("answer") == 0
    assert len(env.gh()["issues"]) == 1
    assert env.gh()["labels"][0][1] == "ready"
    assert env.rows()[0]["status"] == "handed-off"


def test_issue_missing_done_when_is_refused_the_label(env, monkeypatch):
    answered(env, "approve")
    monkeypatch.setenv("STUB_STRIP_DONE", "1")
    assert env.run("answer") == 1
    assert len(env.gh()["issues"]) == 1
    assert env.gh()["labels"] == []
    assert env.rows()[0]["status"] == "shaping"


@pytest.mark.parametrize(
    "body, problem",
    [
        ("## Shape\nOwner repo: a/b\nClass: dev\nDecision: owner ruled\n", "Done when"),
        (
            "## Done when\n\n- tbd\n\nOwner repo: a/b\nClass: dev\nDecision: owner ruled\n",
            "placeholder",
        ),
        ("## Done when\n- x\nClass: dev\nDecision: owner ruled\n", "Owner repo"),
        (
            "## Done when\n- x\nOwner repo: a/b\nClass: unclear\nDecision: owner ruled\n",
            "Class",
        ),
        ("## Done when\n- x\nOwner repo: a/b\nClass: dev\n", "Decision"),
    ],
)
def test_check_issue_body_names_what_is_missing(body, problem):
    problems = shape.check_issue_body(body)
    assert len(problems) == 1 and problem in problems[0]


def test_a_complete_body_passes():
    body = "## Done when\n- x\n\n## Shape\nOwner repo: a/b\nClass: ops\nDecision: matrix auto-answered\n"
    assert shape.check_issue_body(body) == []


def test_drop_files_nothing(env):
    answered(env, "drop - not now")
    assert env.run("answer") == 0
    assert env.gh()["issues"] == []
    assert env.rows()[0]["status"] == "dropped"


def test_unreadable_answer_and_unregistered_repo_file_nothing(env):
    answered(env, "let me think about it")
    assert env.run("answer") == 1
    assert env.gh()["issues"] == []
    assert env.rows()[0]["status"] == "shaping"
    assert env.hub()["shape-abc"]["state"] == "open"


def test_refused_answer_reopens_the_same_item_and_waits_for_a_new_one(env):
    answered(env, "approve", project="unknown-thing")
    assert env.run("answer") == 1
    item = env.hub()["shape-abc"]
    assert (item["state"], item["holds"]) == ("open", 2)
    assert "not a registered project" in item["reason"].split("Intake accepted")[0]
    assert "repo none registered - name one" in item["reason"]
    assert list(env.hub()) == ["shape-abc"]
    assert env.rows()[0]["status"] == "shaping"
    for _ in range(2):
        assert env.run("sweep") == 0
    assert env.hub()["shape-abc"]["holds"] == 2
    assert env.gh()["issues"] == []
    env.set_hub("shape-abc", state="closed", decision="repo: lifekit-hq/clip")
    assert env.run("sweep") == 0
    [issue] = env.gh()["issues"]
    assert issue["url"] == "https://github.com/lifekit-hq/clip/issues/1"
    assert env.gh()["labels"] == [[issue["url"], "ready"]]
    [r] = env.rows()
    assert (r["status"], r["ref"]) == ("handed-off", issue["url"])


def test_open_item_waits(env):
    env.index(row("abc"))
    env.run()
    assert env.gh()["issues"] == []
    assert env.rows()[0]["status"] == "shaping"


def test_hand_off_marks_every_row_of_a_repeated_item(env):
    env.index(row("abc", rubric="v7"))
    env.run("open")
    env.append(row("abc", rubric="v8"))
    env.run("open")
    env.set_hub("shape-abc", state="closed", decision="approve")
    env.run("answer")
    assert len(env.gh()["issues"]) == 1
    assert {r["status"] for r in env.rows()} == {"handed-off"}


def test_original_14_column_rows_still_parse_and_video_py_reads_marked_rows(env):
    env.index(row("abc"))
    env.run("open")
    spec = importlib.util.spec_from_file_location(
        "video_eval_video", REPO / "skills/video-eval/video.py"
    )
    video = importlib.util.module_from_spec(spec)
    sys.modules["video_eval_video"] = video
    spec.loader.exec_module(video)
    [r] = video.read_index(env.intake)
    assert (r["status"], r["ref"]) == ("shaping", "shape-abc")


def test_main_needs_a_firstmate_home(env, monkeypatch):
    monkeypatch.setenv("INTAKE_SHAPE_FM_HOME", str(env.home / "missing"))
    assert env.run() == 2


def test_main_needs_tasks_axi(env, monkeypatch, capsys):
    monkeypatch.setenv("TASKS_AXI", str(env.home / "no-such-tasks-axi"))
    env.index(row("a"), row("b"))
    assert env.run() == 2
    err = capsys.readouterr().err
    assert err.count("tasks-axi not found") == 1, err


def run_intake_installer(tmp_path, **extra):
    import subprocess

    root = tmp_path / "root"
    res = subprocess.run(
        ["bash", str(REPO / "scripts/intake-shape/install-intake-shape.sh")],
        env={
            "PATH": "/usr/bin:/bin",
            "INSTALL_ROOT": str(root),
            "SYSTEMCTL": "true",
            "ADMIN_USER": "ops",
            "INTAKE_SHAPE_FM_HOME": "/home/ops/fm",
            "INTAKE_SHAPE_REPOS": "lifekit-stack=lifekit-hq/lifekit-stack",
            "FLEET_POLICY": "/home/ops/.local/bin/fleet-policy",
            **extra,
        },
        capture_output=True,
        text=True,
    )
    return root, res


def intake_unit(root):
    return (root / "etc/systemd/system/lifekit-intake-shape.service").read_text()


def test_installer_writes_tasks_axi_and_path(tmp_path):
    root, res = run_intake_installer(
        tmp_path, TASKS_AXI="/home/ops/.nvm/versions/node/v22/bin/tasks-axi"
    )
    assert res.returncode == 0, res.stderr
    unit = intake_unit(root)
    assert '"TASKS_AXI=/home/ops/.nvm/versions/node/v22/bin/tasks-axi"' in unit
    assert '"PATH=/home/ops/.nvm/versions/node/v22/bin:/usr/local/sbin:' in unit
    assert '"INTAKE_SHAPE_FLEET_POLICY=/home/ops/.local/bin/fleet-policy"' in unit
    assert "__" not in unit


def test_installer_finds_tasks_axi_as_the_admin_account(tmp_path):
    fake_su = tmp_path / "su"
    fake_su.write_text(
        "#!/usr/bin/env bash\n"
        'case "$4" in *"bash -ic"*) echo noise; echo /home/ops/.nvm/bin/tasks-axi;; esac\n'
    )
    fake_su.chmod(0o755)
    root, res = run_intake_installer(tmp_path, SU=str(fake_su))
    assert res.returncode == 0, res.stderr
    unit = intake_unit(root)
    assert '"TASKS_AXI=/home/ops/.nvm/bin/tasks-axi"' in unit
    assert '"PATH=/home/ops/.nvm/bin:' in unit


def test_installer_refuses_without_tasks_axi(tmp_path):
    fake_su = tmp_path / "su"
    fake_su.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_su.chmod(0o755)
    root, res = run_intake_installer(tmp_path, SU=str(fake_su))
    assert res.returncode == 1
    assert "tasks-axi not found for ops" in res.stderr
    assert not (root / "etc/systemd/system/lifekit-intake-shape.service").exists()
    root, res = run_intake_installer(tmp_path, TASKS_AXI="relative/tasks-axi")
    assert res.returncode == 1


def test_installer_finds_fleet_policy_as_the_admin_account(tmp_path):
    fake_su = tmp_path / "su"
    fake_su.write_text(
        "#!/usr/bin/env bash\n"
        'case "$4" in "command -v fleet-policy") echo /home/ops/.local/bin/fleet-policy;;\n'
        '  "command -v tasks-axi") echo /opt/bin/tasks-axi;; esac\n'
    )
    fake_su.chmod(0o755)
    root, res = run_intake_installer(tmp_path, SU=str(fake_su), FLEET_POLICY="")
    assert res.returncode == 0, res.stderr
    assert (
        '"INTAKE_SHAPE_FLEET_POLICY=/home/ops/.local/bin/fleet-policy"'
        in intake_unit(root)
    )


def test_installer_refuses_without_fleet_policy(tmp_path):
    fake_su = tmp_path / "su"
    fake_su.write_text(
        '#!/usr/bin/env bash\ncase "$4" in *tasks-axi*) echo /opt/bin/tasks-axi;; esac\n'
    )
    fake_su.chmod(0o755)
    root, res = run_intake_installer(tmp_path, SU=str(fake_su), FLEET_POLICY="")
    assert res.returncode == 1
    assert "fleet-policy not found for ops" in res.stderr
    assert not (root / "etc/systemd/system/lifekit-intake-shape.service").exists()


def test_installer_renders_the_unit(tmp_path):
    root, res = run_intake_installer(tmp_path, TASKS_AXI="/opt/tools/bin/tasks-axi")
    assert res.returncode == 0, res.stderr
    unit = (root / "etc/systemd/system/lifekit-intake-shape.service").read_text()
    assert "User=ops" in unit and "INTAKE_SHAPE_FM_HOME=/home/ops/fm" in unit
    assert "__" not in unit
    pre = [
        line.split("=", 1)[1]
        for line in unit.splitlines()
        if line.startswith("ExecStartPre=")
    ]
    [acl] = [p for p in pre if "setfacl" in p]
    assert acl.startswith("+"), "must run as root though the service runs as ops"
    argv = shlex.split(acl[1:])
    assert argv[0].endswith("/setfacl") and "-R" in argv
    assert argv[-1] == "/var/lib/lifekit/intake"
    entries = argv[argv.index("-m") + 1].split(",")
    assert {"u:ops:rwX", "m::rwX", "d:u:ops:rwX", "d:m::rwX"} <= set(entries)
    assert (root / "usr/local/libexec/lifekit-intake-shape/intake_shape.py").is_file()


def template_body():
    text = (REPO / ".github/ISSUE_TEMPLATE/shaped-work.md").read_text()
    return text.split("---\n", 2)[2]


def fill(body):
    out = []
    for line in body.splitlines():
        if line.startswith("- <"):
            line = "- the shape step files this issue"
        elif line.startswith("Owner repo:"):
            line = "Owner repo: lifekit-hq/lifekit-stack"
        elif line.startswith("Class:"):
            line = "Class: dev"
        elif line.startswith("Decision:"):
            line = "Decision: owner ruled"
        out.append(line)
    return "\n".join(out)


def test_issue_template_filled_in_passes_and_unfilled_or_incomplete_is_refused():
    body = template_body()
    assert shape.check_issue_body(fill(body)) == []
    assert shape.check_issue_body(body)
    problems = shape.check_issue_body(fill(body).replace("## Done when", "## Notes"))
    assert len(problems) == 1 and "Done when" in problems[0]


@pytest.mark.parametrize("how", ["directory", "mode-000"])
@pytest.mark.parametrize("cmd", ["open", "answer", "sweep"])
def test_unreadable_index_exits_1_with_one_stderr_line(env, how, cmd, capfd):
    if how == "mode-000" and os.geteuid() == 0:
        pytest.skip("root reads mode 000")
    env.index(row("abc"))
    assert env.run("open") == 0
    capfd.readouterr()
    index = env.intake / "index.md"
    saved = index.read_bytes()
    index.unlink()
    if how == "directory":
        index.mkdir()
    else:
        index.write_bytes(saved)
        index.chmod(0)
    hub_before, gh_before = env.hub(), env.gh()
    assert env.run(cmd) == 1
    err = [ln for ln in capfd.readouterr().err.splitlines() if ln.strip()]
    assert err and all(ln.startswith("intake-shape: ") for ln in err)
    assert (env.hub(), env.gh()) == (hub_before, gh_before)


def test_unreadable_since_file_exits_1_instead_of_resetting_the_cutoff(env, capfd):
    since = env.intake / ".shape-since"
    since.unlink()
    since.mkdir()
    env.index()
    assert env.run("open") == 1
    assert "intake-shape:" in capfd.readouterr().err
    assert since.is_dir()


def test_missing_index_is_no_rows_not_a_failure(env):
    assert env.run("sweep") == 0
