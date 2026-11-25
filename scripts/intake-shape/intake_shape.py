#!/usr/bin/env python3
"""intake-shape - the shape gate between an accepted idea and a build hand-off.

An `accept` (or `adopt`) verdict at the intake door used to end in a vault page
(scripts/intake-to-vault). This is the step after it, with no model turn:

  open    every accepted intake row dated on or after the cutoff (below) and
          not yet shaping gets a SHAPING ITEM: a
          captain-held task (fm-captain-hold.sh hold) carrying the proposed
          shape and the options, shown on the Calls page. One item per source
          (the intake key): a repeat adds a count to the open item's title, a
          repeat of a closed item opens a new item that references it.
  answer  once the owner's answer is recorded on the item, the step files ONE
          GitHub issue in the owning repo with a `## Done when` section, the
          owner repo, the class and a record of who ruled, then sets the
          `ready` label. The label is set only here, and only on an issue
          that passes `check_issue_body`. The intake row is marked
          `handed-off` with the issue as `ref`; from then on the issue owns
          the item and the intake index keeps only the reference.

`sweep` runs both. Each step is checked first, so a run that stopped half-way
is completed by the next one and nothing is filed twice.

An intake row gets two trailing cells, `status` and `ref`, that the original
14-column rows lack: `shaping <item id>` while the item waits, then
`handed-off <issue url>`, or `dropped` with the item id when the answer drops it.
An answer that cannot be filed (unreadable, no registered repo, a failed check)
does not end the item: it is reopened under its hold with the reason in its
text and the rows stay `shaping`, so the owner answers the same item again. These marks are the only thing this step writes to the
intake index (rewritten in place under video.py's lock, so the file keeps its
owner and mode); no other store is added. A row still `shaping` whose hub item
is gone (closed and archived before the answer was read) is logged and fails
the run, never re-held.

Only rows dated on or after the cutoff open items, so the ideas accepted before
this step existed are never asked about. The first run records its UTC date in
`.shape-since` in the intake directory; `--since YYYY-MM-DD` sets it.

The owner's answer is free text. `approve` (or yes/ok/ship) files the proposed
shape; `drop` (or no/park/reject) files nothing; any answer may carry
`repo: <owner/name>`, `class: <dev|ops|finance|life>` and `done when: <text>`
lines that override the proposal. An answer that starts with `matrix:` is
recorded as auto-answered by the decision matrix, any other as the owner's.

Environment:
  INTAKE_SHAPE_FM_HOME   the firstmate home holding the captain hold (required)
  INTAKE_SHAPE_INTAKE    the intake directory (default /var/lib/lifekit/intake)
  INTAKE_SHAPE_REPOS     space-separated `<project>=<owner/repo>` pairs: the
                         registered projects an issue may be filed in
  INTAKE_SHAPE_GH        the gh command (default `gh`)
  TASKS_AXI              the tasks-axi command (default `tasks-axi`)

Usage: intake_shape.py [open|answer|sweep] [--since YYYY-MM-DD]

Exit: 0 done or nothing to do; 1 something could not be done (the journal says
what; the next run retries); 2 bad usage or configuration (no firstmate home,
or no tasks-axi on TASKS_AXI/PATH: one journal line, not one per item).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROUTED_VERDICTS = ("adopt", "accept")
COLUMNS = (
    "date",
    "kind",
    "origin",
    "category",
    "source",
    "title",
    "gist",
    "verdict",
    "reason",
    "project",
    "recheck",
    "next step or why",
    "rubric",
    "report",
)
MARKS = ("status", "ref")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
CELL_LIMIT = 300
CLASSES = ("dev", "ops", "finance", "life")
READY = "ready"
DONE_WHEN = "## Done when"
APPROVE = ("approve", "approved", "yes", "ok", "ship", "go")
REJECT = ("drop", "dropped", "no", "park", "reject", "rejected")
PLACEHOLDERS = ("", "-", "n/a", "na", "none", "tbd", "todo")
TIMEOUT = 60
REASON_MAX = 900


def log(message: str) -> None:
    print(f"intake-shape: {message}", file=sys.stderr)


def one_line(text: str) -> str:
    return " ".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text).split())


def cell(value: str) -> str:
    """The index's own cell cleaning (skills/video-eval/video.py `_cell`)."""
    v = " ".join(str(value).replace("|", "/").split())
    return v if len(v) <= CELL_LIMIT else v[: CELL_LIMIT - 3] + "..."


def slug(key: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", key.lower()).strip("-")


# ---------------------------------------------------------------- the index


@dataclass
class Row:
    cells: dict[str, str]

    def __getitem__(self, name: str) -> str:
        return self.cells.get(name, "")

    @property
    def key(self) -> str:
        """The item's fingerprint: its report's key, from the `report` cell
        (`reports/<key>.<stamp>.md`), else the source URL."""
        name = Path(self["report"]).name
        m = re.match(r"^(?P<key>[A-Za-z0-9_-]+)\.v[0-9]+\.md$", name)
        return m["key"] if m else self["source"]


def parse_row(line: str) -> Row | None:
    if not line.startswith("| ") or line.startswith("| date |"):
        return None
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if set(cells) == {"---"} or len(cells) not in (len(COLUMNS), len(COLUMNS) + 2):
        return None
    return Row(dict(zip(COLUMNS + MARKS, cells)))


def read_rows(intake: Path) -> list[Row]:
    """The index's rows; no index yet is no rows, an unreadable one raises."""
    try:
        text = (intake / "index.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [r for r in map(parse_row, text.splitlines()) if r]


@contextlib.contextmanager
def index_lock(intake: Path):
    with open(intake / ".index.lock", "a") as f:
        with contextlib.suppress(OSError):
            os.chmod(f.fileno(), 0o600)
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def render_row(row: Row) -> str:
    names = COLUMNS + MARKS if (row["status"] or row["ref"]) else COLUMNS
    return "| " + " | ".join(cell(row[n]) for n in names) + " |"


def mark_rows(intake: Path, updates: dict[tuple[str, str], tuple[str, str]]) -> None:
    """Set (status, ref) on the rows named by (source, rubric), rewriting the
    file in place under the index lock video.py appends under, so its owner and
    mode (the gateway's appends keep working) survive."""
    with index_lock(intake), open(intake / "index.md", "r+", encoding="utf-8") as f:
        out = []
        for line in f.read().splitlines():
            row = parse_row(line)
            hit = row and updates.get((row["source"], row["rubric"]))
            if row and hit:
                row.cells["status"], row.cells["ref"] = hit
                line = render_row(row)
            out.append(line)
        f.seek(0)
        f.write("\n".join(out) + "\n")
        f.truncate()


# ---------------------------------------------------------------- the hub


class Hub:
    """The firstmate home's captain-hold primitive, the one the Calls page
    shows. Every call is a subprocess so the home's own gates apply."""

    def __init__(self, home: str):
        self.home = home
        self.hold_cmd = str(Path(home) / "bin" / "fm-captain-hold.sh")
        self.tasks = shlex.split(os.environ.get("TASKS_AXI", "tasks-axi"))

    def run(self, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            cwd=self.home,
            env={**os.environ, "FM_HOME": self.home},
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )

    def state(self, item: str) -> str:
        """open (held for the captain), closed, or absent."""
        res = self.run([self.hold_cmd, "open", item, "--distinguish-absent"])
        states = {0: "open", 1: "closed", 3: "absent"}
        if res.returncode not in states:
            raise RuntimeError(
                f"fm-captain-hold open {item} exit {res.returncode}: {one_line(res.stderr)[:200]}"
            )
        return states[res.returncode]

    def hold(self, item: str, title: str | None, reason: str) -> None:
        argv = [self.hold_cmd, "hold", item, "--reason", reason]
        if title:
            argv += ["--title", title]
        res = self.run(argv)
        if res.returncode != 0:
            raise RuntimeError(
                f"fm-captain-hold hold {item} exit {res.returncode}: {one_line(res.stderr)[:200]}"
            )

    def reopen(self, item: str, reason: str) -> None:
        """Put a closed item back under its hold with a new reason."""
        res = self.run([*self.tasks, "reopen", item])
        if res.returncode != 0:
            raise RuntimeError(
                f"tasks-axi reopen {item} exit {res.returncode}: {one_line(res.stderr)[:200]}"
            )
        self.hold(item, None, reason)

    def retitle(self, item: str, title: str) -> None:
        res = self.run([*self.tasks, "update", item, "--title", title])
        if res.returncode != 0:
            raise RuntimeError(
                f"tasks-axi update {item} exit {res.returncode}: {one_line(res.stderr)[:200]}"
            )

    def decision(self, item: str) -> str | None:
        """The owner's recorded words on a closed item, else None."""
        res = self.run([*self.tasks, "show", item, "--full"])
        if res.returncode != 0:
            raise RuntimeError(
                f"tasks-axi show {item} exit {res.returncode}: {one_line(res.stderr)[:200]}"
            )
        raw = next(
            (
                ln[len("  body: ") :]
                for ln in res.stdout.splitlines()
                if ln.startswith("  body: ")
            ),
            "",
        )
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        m = re.search(r"^Captain decision:\n(.*?)(?:\n\n|\Z)", body, re.M | re.S)
        return m[1].strip() if m else None


# ---------------------------------------------------------------- the item


def projects() -> dict[str, str]:
    """The registered projects an issue may be filed in."""
    out = {}
    for pair in os.environ.get("INTAKE_SHAPE_REPOS", "").split():
        name, _, repo = pair.partition("=")
        if name and re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
            out[name.lower()] = repo
    return out


def proposed_repo(project: str) -> str:
    """The registered repo the report's Project: names - its whole value or
    its start followed by a character no project name has - else ''."""
    value = project.strip().lower()
    best = ""
    for name in projects():
        if value.startswith(name) and not re.match(
            r"[a-z0-9-]", value[len(name) : len(name) + 1]
        ):
            if len(name) > len(best):
                best = name
    return projects().get(best, "")


def item_title(row: Row, count: int) -> str:
    seen = f" (seen {count}x)" if count > 1 else ""
    return one_line(f"Shape: {row['title']}{seen}")[:150]


def item_reason(row: Row, previous: str | None) -> str:
    repo = proposed_repo(row["project"]) or "none registered - name one"
    done = one_line(row["next step or why"]) or "none given - name one"
    again = f" Repeat of {previous}, already answered." if previous else ""
    text = (
        f"Intake accepted: {row['title']} ({row['source']}).{again} Proposed shape: "
        f"repo {repo}, class dev, done when: {done}. Options: approve to file it "
        "as proposed; drop to file nothing; or answer with `repo: <owner/name>`, "
        "`class: <dev|ops|finance|life>`, `done when: <text>` lines to change it. "
        "Prefix `matrix:` when the decision matrix answers."
    )
    return one_line(text)[:REASON_MAX]


def item_id(base: str, n: int) -> str:
    return base if n == 1 else f"{base}-{n}"


def next_item(
    hub: Hub, base: str, key: str, marked: list[Row]
) -> tuple[str, str | None, bool]:
    """(the item an arriving accept belongs to, the closed item it repeats,
    whether it must be created). How many items this source has had comes from
    the intake rows already marked for it (the hub archives closed items) or
    the hub's own chain, whichever is longer."""
    seen = len({r["ref"] for r in marked if r.key == key})
    while hub.state(item_id(base, seen + 1)) != "absent":
        seen += 1
    if seen == 0:
        return base, None, True
    last = item_id(base, seen)
    if hub.state(last) == "open":
        return last, None, False
    return item_id(base, seen + 1), last, True


def since_cutoff(intake: Path, override: str | None) -> str:
    """The first date (UTC YYYY-MM-DD) whose accepted rows open a shaping item:
    `--since`, else the date the first run recorded in `.shape-since`."""
    path = intake / ".shape-since"
    if override is None:
        with contextlib.suppress(FileNotFoundError):
            recorded = path.read_text(encoding="utf-8").strip()
            if DATE.fullmatch(recorded):
                return recorded
        override = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path.write_text(override + "\n", encoding="utf-8")
    return override


def open_items(intake: Path, hub: Hub, since: str) -> int:
    """Give every accepted row dated on or after `since` a shaping item;
    returns the failures. The item is held before its rows are marked, so a
    run that stopped between the two finds the open item and joins it."""
    failed = 0
    rows = read_rows(intake)
    marked = [r for r in rows if r["status"]]
    groups: dict[str, list[Row]] = {}
    for r in rows:
        if (
            r["verdict"].lower() in ROUTED_VERDICTS
            and not r["status"]
            and DATE.fullmatch(r["date"])
            and r["date"] >= since
        ):
            groups.setdefault(f"shape-{slug(r.key)}", []).append(r)
    marks: dict[tuple[str, str], tuple[str, str]] = {}
    for base, members in groups.items():
        try:
            item, repeat, create = next_item(hub, base, members[0].key, marked)
            seen = len(members) + sum(
                1 for r in marked if r["status"] == "shaping" and r["ref"] == item
            )
            title = item_title(members[0], seen)
            if create:
                hub.hold(item, title, item_reason(members[0], repeat))
                log(f"{item}: shaping item opened for {members[0].key}")
            else:
                hub.retitle(item, title)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            log(f"{base}: {error}")
            failed += 1
            continue
        for m in members:
            marks[(m["source"], m["rubric"])] = ("shaping", item)
    if marks:
        mark_rows(intake, marks)
    return failed


# ---------------------------------------------------------------- the issue


@dataclass
class Shape:
    repo: str
    cls: str
    done_when: str
    ruled: str  # "matrix auto-answered" | "owner ruled"


def parse_answer(text: str, row: Row) -> Shape | str | None:
    """The shape the owner's answer files; the string `drop` when it files
    nothing; None when the answer cannot be read as either."""
    ruled = "owner ruled"
    body = text.strip()
    if body.lower().startswith("matrix:"):
        ruled, body = "matrix auto-answered", body[len("matrix:") :].strip()
    first = re.split(r"[\s,.:;!]+", body.lower(), maxsplit=1)[0]
    repo = proposed_repo(row["project"])
    cls = "dev"
    done = one_line(row["next step or why"])
    overrides = 0
    for line in body.splitlines():
        m = re.match(r"^\s*(repo|class|done[ -]when)\s*:\s*(.+?)\s*$", line, re.I)
        if not m:
            continue
        overrides += 1
        field, value = m[1].lower(), m[2]
        if field == "repo":
            repo = value
        elif field == "class":
            cls = value.lower()
        else:
            done = value
    if overrides == 0:
        if first in REJECT:
            return "drop"
        if first not in APPROVE:
            return None
    return Shape(repo, cls, done, ruled)


def issue_body(shape: Shape, row: Row, item: str) -> str:
    return "\n".join(
        [
            f"<!-- intake-shape: {item} -->",
            f"Intake: {row['title']} ({row['source']})",
            "",
            DONE_WHEN,
            "",
            *[
                f"- {p.strip()}"
                for p in re.split(r";\s+", shape.done_when)
                if p.strip()
            ],
            "",
            "## Shape",
            "",
            f"Owner repo: {shape.repo}",
            f"Class: {shape.cls}",
            f"Decision: {shape.ruled}",
            "",
        ]
    )


def section(body: str, heading: str) -> str:
    m = re.search(
        rf"^{re.escape(heading)}[ \t]*\n(.*?)(?=^## |^(?:Owner repo|Class|Decision):|\Z)",
        body,
        re.M | re.S,
    )
    return m[1].strip() if m else ""


def check_issue_body(body: str) -> list[str]:
    """What keeps an issue from the `ready` label; empty means it may have it."""
    problems = []
    done = section(body, DONE_WHEN)
    if not done:
        problems.append(f"no `{DONE_WHEN}` section with content")
    elif all(
        re.sub(r"^[-*\s]+", "", ln).strip().lower() in PLACEHOLDERS
        for ln in done.splitlines()
    ):
        problems.append(f"`{DONE_WHEN}` holds only a placeholder")
    repo = re.search(r"^Owner repo:\s*(\S+)\s*$", body, re.M)
    if not repo or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo[1]):
        problems.append("no `Owner repo: <owner/name>` line")
    cls = re.search(r"^Class:\s*(\S+)\s*$", body, re.M)
    if not cls or cls[1] not in CLASSES:
        problems.append(f"no `Class:` line naming one of {', '.join(CLASSES)}")
    if not re.search(
        r"^Decision:\s*(matrix auto-answered|owner ruled)\s*$", body, re.M
    ):
        problems.append("no `Decision:` line (matrix auto-answered | owner ruled)")
    return problems


class Gh:
    def __init__(self) -> None:
        self.cmd = shlex.split(os.environ.get("INTAKE_SHAPE_GH", "gh"))

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*self.cmd, *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )

    def need(self, *args: str) -> str:
        res = self.run(*args)
        if res.returncode != 0:
            raise RuntimeError(
                f"gh {args[0]} {args[1]} exit {res.returncode}: {one_line(res.stderr)[:200]}"
            )
        return res.stdout

    def find(self, repo: str, item: str) -> str | None:
        out = self.need(
            "issue", "list", "--repo", repo, "--state", "all", "--search",
            f'"intake-shape: {item}" in:body', "--json", "url,body",
        )  # fmt: skip
        for found in json.loads(out or "[]"):
            if f"<!-- intake-shape: {item} -->" in found.get("body", ""):
                return found["url"]
        return None

    def create(self, repo: str, title: str, body: str) -> str:
        out = self.need(
            "issue", "create", "--repo", repo, "--title", title, "--body", body
        )
        url = out.strip().splitlines()[-1] if out.strip() else ""
        if not re.fullmatch(r"https://\S+/issues/\d+", url):
            raise RuntimeError(f"gh issue create printed no issue url: {out[:120]!r}")
        return url

    def body(self, url: str) -> str:
        out = self.need("issue", "view", url, "--json", "body")
        return json.loads(out)["body"]

    def label(self, url: str) -> None:
        repo = re.sub(r"/issues/\d+$", "", url.split("https://github.com/", 1)[-1])
        self.run(
            "label", "create", READY, "--repo", repo, "--force",
            "--description", "Shaped: done-when, owner repo and class are on the issue",
        )  # fmt: skip
        self.need("issue", "edit", url, "--add-label", READY)


def set_ready(gh: Gh, url: str) -> list[str]:
    """Label an issue `ready` - the only place the label is ever set - after
    its own body passes check_issue_body; returns the refusals."""
    problems = check_issue_body(gh.body(url))
    if not problems:
        gh.label(url)
    return problems


def ask_again(hub: Hub, item: str, row: Row, why: str) -> None:
    """An answer that files nothing does not end the item: the same item goes
    back under its hold, its text saying why, and the rows stay `shaping`. A
    reopened item is no longer closed, so the next run does not read the
    refused answer again; the owner's next answer is a new one."""
    reason = one_line(f"Not filed: {why}. Answer this item again. ")
    hub.reopen(item, (reason + item_reason(row, None))[:REASON_MAX])
    log(f"{item}: not filed ({why}); item reopened for a new answer")


def answer_items(intake: Path, hub: Hub, gh: Gh) -> int:
    """File the issue for every shaping item the owner has answered."""
    failed = 0
    items: dict[str, list[Row]] = {}
    for r in read_rows(intake):
        if r["status"] == "shaping":
            items.setdefault(r["ref"], []).append(r)
    for item, members in sorted(items.items()):
        row = members[0]
        keys = [(m["source"], m["rubric"]) for m in members]
        try:
            state = hub.state(item)
            if state == "absent":
                log(f"{item}: shaping row without its hub item, answer lost")
                failed += 1
            if state != "closed":
                continue
            text = hub.decision(item)
            if text is None:
                ask_again(hub, item, row, "the item closed without a recorded answer")
                failed += 1
                continue
            shape = parse_answer(text, row)
            if shape == "drop":
                mark_rows(intake, dict.fromkeys(keys, ("dropped", item)))
                log(f"{item}: dropped by the answer")
                continue
            if shape is None:
                ask_again(
                    hub,
                    item,
                    row,
                    "the answer reads as neither approve, drop nor a shape",
                )
                failed += 1
                continue
            body = issue_body(shape, row, item)
            problems = check_issue_body(body)
            if shape.repo not in projects().values():
                problems.append(
                    f"{shape.repo or 'no repo'} is not a registered project"
                )
            if problems:
                ask_again(hub, item, row, "; ".join(problems))
                failed += 1
                continue
            url = gh.find(shape.repo, item) or gh.create(
                shape.repo, one_line(row["title"])[:120], body
            )
            problems = set_ready(gh, url)
            if problems:
                log(f"{item}: {url} refused the label: {'; '.join(problems)}")
                failed += 1
                continue
            mark_rows(intake, dict.fromkeys(keys, ("handed-off", url)))
            log(f"{item}: handed off as {url}")
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
            log(f"{item}: {error}")
            failed += 1
    return failed


# ---------------------------------------------------------------- the run


def main(argv: list[str]) -> int:
    args, since = argv[1:], None
    if "--since" in args:
        i = args.index("--since")
        since = args[i + 1] if i + 1 < len(args) else ""
        del args[i : i + 2]
        if not DATE.fullmatch(since):
            print("usage: --since YYYY-MM-DD", file=sys.stderr)
            return 2
    command = args[0] if args else "sweep"
    if command not in ("open", "answer", "sweep") or len(args) > 1:
        print(
            "usage: intake_shape.py [open|answer|sweep] [--since YYYY-MM-DD]",
            file=sys.stderr,
        )
        return 2
    home = os.environ.get("INTAKE_SHAPE_FM_HOME", "")
    if not home or not (Path(home) / "bin" / "fm-captain-hold.sh").is_file():
        log("INTAKE_SHAPE_FM_HOME does not name a firstmate home")
        return 2
    hub = Hub(home)
    if not hub.tasks or not shutil.which(hub.tasks[0]):
        log("tasks-axi not found: set TASKS_AXI or put it on PATH (reinstall the unit)")
        return 2
    intake = Path(os.environ.get("INTAKE_SHAPE_INTAKE", "/var/lib/lifekit/intake"))
    failed = 0
    if command in ("open", "sweep"):
        try:
            failed += open_items(intake, hub, since_cutoff(intake, since))
        except OSError as error:
            log(f"{intake}: {error}")
            failed += 1
    if command in ("answer", "sweep"):
        try:
            failed += answer_items(intake, hub, Gh())
        except OSError as error:
            log(f"{intake}: {error}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
