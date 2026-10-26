#!/usr/bin/env python3
"""intake-to-vault - file each accepted video-eval report into the memory vault.

The intake judge (skills/video-eval) leaves one report per item in the private
intake directory. This job is the software half of "take the knowledge from
the video as reference": no model turn, only a mechanical copy through the
vault's own gates, in the vault README's shape for raw evidence.

For each report whose verdict is accept (ROUTED_VERDICTS) it writes:

  1. one immutable source page, sources/<judged date>-video-<key>-<rubric>.md:
     clipper frontmatter, then the report and the transcript it was judged
     on, both verbatim. The vault lint (bin/lint.sh --changed) must pass it,
     else the page is removed again and the run fails;
  2. its index line, by regenerating index.md with bin/index.sh;
  3. one dated line through bin/log.sh, linking the page: on the owning
     project's log when the report's Project: line names a vault project (the
     whole value, or its start followed by a non-name character), else on the
     cross-project log.md as kind `intake`.

A park or a drop is not filed: the vault keeps only what is accepted (vault
CORE.md, "Structured intake"), and the intake index already records every item
judged and why. Each step is checked before it is done, so a run that stopped
half-way is completed by the next one and nothing is written twice: the page
exists (and names the same source), the index lists it, a log (living or
archived) links it. The page is never rewritten. memory-sync commits the page
and the index like any vault edit; bin/log.sh commits its own line.

Like memory-sync and vault-door, it does nothing while the vault's weekly
model pass holds `.model-pass` (under an hour old).

Environment:
  INTAKE_TO_VAULT_VAULT   vault checkout (default /srv/memory)
  INTAKE_TO_VAULT_INTAKE  the intake directory, the gateway's
                          ~/.life-state/intake (default /var/lib/lifekit/intake)

Exit: 0 done, nothing to do, or skipped for the model pass; 1 a report could
not be filed (the journal says which and why; the rest are still filed, and
the next run retries); 78 the vault has no bin/ harness.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROUTED_VERDICTS = ("accept",)
MODEL_LOCK = ".model-pass"
MODEL_LOCK_SECONDS = 60 * 60
LOG_LINE_MAX = (
    400  # the vault's LOG_LINE_MAX (bin/policy.tsv); bin/log.sh refuses longer
)
TITLE_MAX = 100
PROJECT_TEXT_MAX = 60
TIMEOUT = 120

REPORT_NAME = re.compile(r"^(?P<key>[A-Za-z0-9_-]+)\.(?P<stamp>v[0-9]+)\.md$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROJECT_NAME_CHAR = re.compile(r"[a-z0-9-]")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def log(message: str) -> None:
    print(f"intake-to-vault: {message}", file=sys.stderr)


def vault_dir() -> Path:
    return Path(os.environ.get("INTAKE_TO_VAULT_VAULT", "/srv/memory"))


def intake_dir() -> Path:
    return Path(os.environ.get("INTAKE_TO_VAULT_INTAKE", "/var/lib/lifekit/intake"))


def model_pass_running(vault: Path) -> bool:
    try:
        return time.time() - (vault / MODEL_LOCK).stat().st_mtime < MODEL_LOCK_SECONDS
    except OSError:
        return False


# ---------------------------------------------------------------- the report


@dataclass
class Report:
    key: str
    stamp: str
    text: str
    title: str
    source: str
    judged: str
    rubric: str
    mode: str
    gist: str
    verdict: str
    project: str
    next_step: str


def parse_report(path: Path) -> Report:
    """The fields of a report as video.py files it (its `stamp`); ValueError
    names what is missing."""
    m = REPORT_NAME.match(path.name)
    if not m:
        raise ValueError("not a report file name (<key>.v<n>.md)")
    text = path.read_text(encoding="utf-8")
    head: dict[str, str] = {}
    verdict: dict[str, str] = {}
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if not section:
            # Kind: and Evaluated: carry several fields; any other header line is one.
            parts = (
                line.split(" | ")
                if line.startswith(("Kind:", "Evaluated:"))
                else [line]
            )
            for part in parts:
                k, sep, v = part.partition(": ")
                if sep:
                    head.setdefault(k.strip().lower(), v.strip())
        elif section == "verdict":
            k, sep, v = line.partition(":")
            if sep:
                verdict.setdefault(k.strip().lower(), v.strip())
    rubric, _, mode = head.get("rubric", "").partition(" (")
    missing = [
        name
        for name, value in (
            ("Title", head.get("title")),
            ("Source", head.get("source")),
            ("Evaluated", head.get("evaluated")),
            ("Rubric", rubric),
            ("Verdict", verdict.get("verdict")),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"no {', '.join(missing)} in the report")
    if not DATE.match(head["evaluated"]):
        raise ValueError(f"Evaluated {head['evaluated']!r} is not YYYY-MM-DD")
    return Report(
        key=m["key"],
        stamp=m["stamp"],
        text=text,
        title=head["title"],
        source=head["source"],
        judged=head["evaluated"],
        rubric=rubric.strip(),
        mode=mode.rstrip(")").strip(),
        gist=head.get("gist", ""),
        verdict=verdict["verdict"].lower(),
        project=verdict.get("project", ""),
        next_step=verdict.get("next step", ""),
    )


def transcript_of(intake: Path, key: str) -> tuple[str, str]:
    """(the transcript file as video.py saved it, its author) or ("", "") once
    retention has removed it."""
    try:
        text = (intake / "transcripts" / f"{key}.txt").read_text(encoding="utf-8")
    except OSError:
        return "", ""
    author = ""
    for line in text.partition("\n\n")[0].splitlines():
        if line.startswith("Author: "):
            author = line[len("Author: ") :].strip()
    return text, "" if author == "unknown" else author


# ---------------------------------------------------------------- the page


def slug(key: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", key.lower()).strip("-")


def page_candidates(r: Report) -> list[str]:
    """The page's path, then the one used when a different source already holds
    it (two keys that differ only in case or '_' share a slug)."""
    base = f"sources/{r.judged}-video-{slug(r.key)}-{r.stamp}"
    tag = hashlib.sha256(r.key.encode()).hexdigest()[:6]
    return [f"{base}.md", f"{base}-{tag}.md"]


def page_url(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        if line == "---":
            break
        if line.startswith("url: "):
            return line[len("url: ") :].strip()
    return ""


def one_line(text: str) -> str:
    return " ".join(CONTROL.sub(" ", text).split())


def quoted(text: str) -> str:
    """A double-quoted YAML scalar the vault's fm_get reads back unchanged."""
    return '"' + one_line(text).replace("\\", "/").replace('"', "'") + '"'


def written_url(source: str) -> str:
    url = one_line(source)
    return url if re.fullmatch(r"https?://[^\s\"'#]+", url) else quoted(url)


def fenced(text: str) -> str:
    run = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, run + 1)
    return f"{fence}text\n{text.rstrip()}\n{fence}"


def render_page(r: Report, transcript: str, author: str) -> str:
    mode = f", {r.mode} mode" if r.mode else ""
    project = f", project: {one_line(r.project)}" if r.project else ""
    transcript_part = (
        fenced(transcript)
        if transcript.strip()
        else "Not kept: the intake's retention removed the transcript before this page was written."
    )
    return "\n".join(
        [
            "---",
            f"title: {quoted(r.title)}",
            f"url: {written_url(r.source)}",
            f"author: {quoted(author or 'unknown')}",
            f"captured: {r.judged}",
            "type: other",
            f"summary: {quoted(f'Video judged {r.verdict} on the intake rubric: {r.gist or r.title}')}",
            "---",
            "",
            f"# {one_line(r.title)}",
            "",
            f"Video intake judgment, kept verbatim as evidence: the report (rubric {r.rubric}{mode},"
            f" judged {r.judged}, verdict {r.verdict}{project}) and the transcript it was judged on."
            " Written from the finished report by lifekit-stack scripts/intake-to-vault with no model"
            " turn. Never edit it; cite it.",
            "",
            "## Report",
            "",
            fenced(r.text),
            "",
            "## Transcript",
            "",
            transcript_part,
            "",
        ]
    )


def write_new(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


# ---------------------------------------------------------------- the gates


def harness(vault: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(vault / "bin" / script), *args],
        cwd=vault,
        env={**os.environ, "VAULT": str(vault)},
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def lint_refusals(vault: Path, rel: str) -> list[str]:
    """The high and medium findings bin/lint.sh --changed has on one page; a
    lint that did not run is a refusal too."""
    result = harness(vault, "lint.sh", "--changed", "--tsv")
    if result.returncode not in (0, 1):
        return [
            f"bin/lint.sh exited {result.returncode}: {one_line(result.stderr)[:300]}"
        ]
    found = []
    for line in result.stdout.splitlines():
        cells = line.split("\t")
        if len(cells) >= 4 and cells[0] in ("high", "medium") and cells[2] == rel:
            found.append(f"{cells[0]} {cells[1]} - {cells[3]}")
    return found


def owning_project(vault: Path, project: str) -> str | None:
    """The vault project the Project: line names - the whole value, or its start
    followed by a character no project name has - else None."""
    value = project.strip().lower()
    best = None
    for log_md in sorted((vault / "projects").glob("*/log.md")):
        name = log_md.parent.name.lower()
        if value.startswith(name) and not PROJECT_NAME_CHAR.match(
            value[len(name) : len(name) + 1]
        ):
            if best is None or len(name) > len(best):
                best = log_md.parent.name
    return best


def logs_of(vault: Path) -> list[Path]:
    """Every log a page's line can be on: living ones and their archive pages."""
    found = [vault / "log.md", *sorted((vault / "projects").glob("*/log.md"))]
    return found + sorted((vault / "archive").rglob("log-*.md"))


def linked_from_a_log(vault: Path, stem: str) -> bool:
    needle = f"[[{stem}|"
    for path in logs_of(vault):
        try:
            if needle in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


def clip(text: str, limit: int) -> str:
    text = one_line(text.replace("[[", "[").replace("]]", "]"))
    if len(text.encode()) <= limit:
        return text
    return text.encode()[: max(limit - 3, 0)].decode("utf-8", "ignore").rstrip() + "..."


def log_args(r: Report, stem: str, project: str | None) -> list[str]:
    """bin/log.sh's arguments for the page's line, within the vault's cap."""
    link = f" [[{stem}|report]]"
    title = clip(r.title, TITLE_MAX)
    if project:
        args, prefix = ["-p", project], f"## [{r.judged}] "
        head = f"video intake accepted: {title}"
    else:
        args, prefix = ["-k", "intake"], f"## [{r.judged}] intake | "
        target = clip(r.project, PROJECT_TEXT_MAX) or "no project named"
        head = f"video accepted for {target}: {title}"
    room = LOG_LINE_MAX - len((prefix + head + link).encode())
    step = clip(r.next_step, room - len(" - next: ".encode()))
    text = head + (f" - next: {step}" if r.next_step and step else "") + link
    return [*args, text]


# ---------------------------------------------------------------- the run


def file_page(vault: Path, intake: Path, r: Report) -> str:
    """The stem of the report's source page, written now if it is missing.
    Raises RuntimeError when the vault lint refuses it."""
    transcript, author = transcript_of(intake, r.key)
    for rel in page_candidates(r):
        path = vault / rel
        if path.exists() and page_url(path) != written_url(r.source):
            continue
        if not path.exists():
            write_new(path, render_page(r, transcript, author))
            refusals = lint_refusals(vault, rel)
            if refusals:
                path.unlink()
                raise RuntimeError(f"vault lint refused {rel}: {'; '.join(refusals)}")
            log(f"{r.key}.{r.stamp}: wrote {rel}")
        return rel[: -len(".md")]
    raise RuntimeError(f"every page name for {r.key} is held by another source")


def main() -> int:
    vault, intake = vault_dir(), intake_dir()
    if not all(
        (vault / "bin" / s).is_file() for s in ("lint.sh", "index.sh", "log.sh")
    ):
        log(f"no vault harness at {vault}/bin (lint.sh, index.sh, log.sh)")
        return 78
    if model_pass_running(vault):
        log(f"model pass running ({vault / MODEL_LOCK}), run skipped")
        return 0
    reports = sorted((intake / "reports").glob("*.md"))
    failed = 0
    routed: list[tuple[Report, str]] = []
    for path in reports:
        try:
            r = parse_report(path)
        except (OSError, ValueError) as error:
            log(f"{path.name}: unreadable report, not filed: {error}")
            failed += 1
            continue
        if r.verdict not in ROUTED_VERDICTS:
            continue
        try:
            stem = file_page(vault, intake, r)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            log(f"{path.name}: {error}")
            failed += 1
            continue
        routed.append((r, stem))
    if not routed:
        log(f"{len(reports)} report(s), none to file")
        return 1 if failed else 0

    try:
        index = (vault / "index.md").read_text(encoding="utf-8")
    except OSError:
        index = ""
    if any(f"[[{stem}|" not in index for _, stem in routed):
        result = harness(vault, "index.sh")
        if result.returncode != 0:
            log(
                f"bin/index.sh exited {result.returncode}: {one_line(result.stderr)[:300]}"
            )
            return 1
        log(one_line(result.stdout))

    for r, stem in routed:
        if linked_from_a_log(vault, stem):
            continue
        project = owning_project(vault, r.project)
        args = log_args(r, stem, project)
        result = harness(vault, "log.sh", *args)
        if result.returncode != 0:
            # log.sh appends before it commits: a failed commit (memory-sync
            # holding the git lock) still leaves the line for memory-sync.
            if linked_from_a_log(vault, stem):
                log(f"{stem}: line appended, its commit failed; memory-sync commits it")
                continue
            log(f"{stem}: bin/log.sh refused: {one_line(result.stderr)[:300]}")
            failed += 1
            continue
        log(
            f"{stem}: logged on {'projects/' + project + '/log.md' if project else 'log.md'}"
        )
    log(
        f"{len(reports)} report(s), {len(routed)} accepted on file in the vault, {failed} failed"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
