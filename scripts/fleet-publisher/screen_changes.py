#!/usr/bin/env python3
"""screen-changes - the dashboard's "Screen changes" feed, for fleet-publisher.sh.

Writes OUT_DIR/screen-changes.json (schema fleet-screen-changes.v1, contract:
README.md "Screen changes feed" and the dashboard's docs/SCREEN-CHANGES.md) and
copies the before/after PNGs it lists to OUT_DIR/screen-changes/<id>/<file>:
one item per open pull request, in an allowlisted repo, whose diff adds,
changes or removes a screen-baseline image (`*-linux.png`), newest first, at
most 20 items and 40 screens each.

Usage: screen_changes.py --out-dir <dir> --state-dir <dir> --now <epoch>
         --home-id <id>

Env: FLEET_SCREEN_REPOS (space-separated owner/name allowlist; default
lifekit-hq/lifekit-dashboard: only these repos are read, and only paths that
look like a baseline image are fetched, at the PR's own merge base and head;
empty skips), FLEET_GH (default gh; empty skips), CREDENTIAL_RE (an item whose
text matches it is withheld). Exit 3 means nothing was read, so the previous
feed stays; any other failure also leaves the previous feed and its images
as they were.

gh is read-only here (`pr list`, `api` GETs). What was fetched for a head
commit is cached in --state-dir, so a quiet minute is one `pr list` per repo.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

SCHEMA = "fleet-screen-changes.v1"
DEFAULT_REPOS = "lifekit-hq/lifekit-dashboard"
MAX_ITEMS = 20
MAX_SCREENS = 40
PR_LIMIT = 50
TITLE_MAX = 160
IMAGE_MAX_BYTES = 4 * 1024 * 1024
BUDGET_SECONDS = 40
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CACHE_NAME = "screen-changes-cache.json"
FEED_NAME = "screen-changes.json"
IMAGE_DIR = "screen-changes"

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.png$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# A baseline is `<name>-<project>-linux.png`; the screen is its name.
BASELINE_RE = re.compile(
    r"^(?:.*/)?([A-Za-z0-9][A-Za-z0-9._-]*)-[A-Za-z0-9._]+-linux\.png$"
)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
STATUS = {
    "added": "added",
    "removed": "removed",
    "modified": "changed",
    "changed": "changed",
    "renamed": "changed",
}


class Skip(Exception):
    """Nothing is read (gh disabled): the previous feed stays."""


def run(cmd: list[str], timeout: float) -> bytes:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=True).stdout


def one_line(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(CONTROL_RE.sub(" ", value).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def epoch(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return int(moment.timestamp())


class Reader:
    """gh, with one shared time budget for the whole run."""

    def __init__(self) -> None:
        self.gh = os.environ.get("FLEET_GH", "gh")
        self.deadline = time.monotonic() + BUDGET_SECONDS

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def call(self, *args: str) -> bytes:
        left = self.left()
        if left < 1:
            raise TimeoutError("time budget ran out")
        return run([self.gh, *args], timeout=min(20, left))

    def pulls(self, repo: str) -> list[dict[str, Any]]:
        out = self.call(
            "pr", "list", "-R", repo, "--state", "open", "--limit", str(PR_LIMIT),
            "--json", "number,title,headRefOid,baseRefOid,updatedAt",
        )  # fmt: skip
        rows = json.loads(out)
        if not isinstance(rows, list):
            raise ValueError("pr list is not a list")
        return [r for r in rows if isinstance(r, dict)]

    def compare(self, repo: str, base: str, head: str) -> dict[str, Any]:
        doc = json.loads(self.call("api", f"repos/{repo}/compare/{base}...{head}"))
        if not isinstance(doc, dict):
            raise ValueError("compare is not an object")
        return doc

    def image(self, repo: str, path: str, sha: str) -> bytes | None:
        """The PNG at path@sha, None when it is not a small PNG."""
        quoted = urllib.parse.quote(path, safe="/")
        data = self.call(
            "api", "-H", "Accept: application/vnd.github.raw+json",
            f"repos/{repo}/contents/{quoted}?ref={sha}",
        )  # fmt: skip
        if data.startswith(PNG_MAGIC) and len(data) <= IMAGE_MAX_BYTES:
            return data
        return None


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o644)
    tmp.replace(path)


def screens_of(
    reader: Reader, repo: str, item_dir: Path, compare: dict[str, Any], head: str
) -> tuple[list[dict[str, Any]], bool]:
    """The pull request's screens, with their images copied into item_dir; and
    whether every wanted image was read (so the result may be cached)."""
    merge_base = (compare.get("merge_base_commit") or {}).get("sha")
    files = compare.get("files")
    if not (isinstance(merge_base, str) and SHA_RE.match(merge_base)):
        raise ValueError("compare has no merge base")
    if not isinstance(files, list):
        raise ValueError("compare has no files")
    screens: list[dict[str, Any]] = []
    names: set[str] = set()
    complete = True
    for entry in files:
        if len(screens) >= MAX_SCREENS:
            break
        if not isinstance(entry, dict):
            continue
        path, status = entry.get("filename"), STATUS.get(str(entry.get("status")))
        match = BASELINE_RE.match(path) if isinstance(path, str) else None
        if not (match and status) or match.group(1) in names:
            continue
        name = match.group(1)
        before_path = entry.get("previous_filename") or path
        if not (isinstance(before_path, str) and BASELINE_RE.match(before_path)):
            continue
        before_file, after_file = f"before-{name}.png", f"after-{name}.png"
        if not (FILE_RE.match(before_file) and FILE_RE.match(after_file)):
            continue
        try:
            before = (
                None
                if status == "added"
                else reader.image(repo, before_path, merge_base)
            )
            after = None if status == "removed" else reader.image(repo, path, head)
        except (OSError, ValueError, subprocess.SubprocessError, TimeoutError):
            complete = False
            continue
        # A screen without the images its status needs is skipped.
        if (status != "added" and before is None) or (
            status != "removed" and after is None
        ):
            continue
        names.add(name)
        if before is not None:
            write_atomic(item_dir / before_file, before)
        if after is not None:
            write_atomic(item_dir / after_file, after)
        screens.append(
            {
                "name": name,
                "status": status,
                "before": before_file if before is not None else None,
                "after": after_file if after is not None else None,
            }
        )
    return screens, complete


def usable(item_dir: Path, screens: Any) -> bool:
    """A cached screen list whose images are all still on disk."""
    if not isinstance(screens, list):
        return False
    return all(
        isinstance(s, dict)
        and all(
            s.get(k) is None
            or (isinstance(s.get(k), str) and (item_dir / s[k]).is_file())
            for k in ("before", "after")
        )
        for s in screens
    )


def build(args: argparse.Namespace) -> dict[str, Any] | None:
    reader = Reader()
    if not reader.gh:
        raise Skip
    repos = os.environ.get("FLEET_SCREEN_REPOS", DEFAULT_REPOS).split()
    if not repos:
        raise Skip
    if not all(REPO_RE.match(r) for r in repos):
        raise ValueError("FLEET_SCREEN_REPOS holds a name that is not owner/name")
    credential = re.compile(os.environ.get("CREDENTIAL_RE") or r"(?!)")
    out_dir = Path(args.out_dir)
    cache_path = Path(args.state_dir) / CACHE_NAME
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    cache = cache if isinstance(cache, dict) else {}
    new_cache: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for repo in repos:
        for pr in reader.pulls(repo):
            number, head, base = (
                pr.get("number"),
                pr.get("headRefOid"),
                pr.get("baseRefOid"),
            )
            if not (
                isinstance(number, int)
                and not isinstance(number, bool)
                and isinstance(head, str)
                and SHA_RE.match(head)
                and isinstance(base, str)
                and SHA_RE.match(base)
            ):
                continue
            key = f"{repo}#{number}"
            item_id = f"{repo.split('/')[1]}-{number}"
            title = one_line(pr.get("title"), TITLE_MAX)
            if item_id in seen or not SLUG_RE.match(item_id) or not title:
                continue
            item_dir = out_dir / IMAGE_DIR / item_id
            hit = cache.get(key)
            if (
                isinstance(hit, dict)
                and hit.get("head") == head
                and isinstance(hit.get("ts"), int)
                and usable(item_dir, hit.get("screens"))
            ):
                screens, ts = hit["screens"], hit["ts"]
                new_cache[key] = hit
            else:
                try:
                    compare = reader.compare(repo, base, head)
                    screens, complete = screens_of(
                        reader, repo, item_dir, compare, head
                    )
                except (OSError, ValueError, subprocess.SubprocessError, TimeoutError):
                    # Retried next run; this one goes on with the other pull requests.
                    continue
                commits = compare.get("commits")
                last = commits[-1] if isinstance(commits, list) and commits else {}
                when = (((last or {}).get("commit") or {}).get("committer") or {}).get(
                    "date"
                )
                ts = epoch(when) or epoch(pr.get("updatedAt")) or args.now
                if complete:
                    new_cache[key] = {"head": head, "ts": ts, "screens": screens}
            if not screens:
                continue
            item = {
                "id": item_id,
                "home_id": args.home_id,
                "repo": repo.split("/")[1],
                "pr": number,
                "title": title,
                "url": f"https://github.com/{repo}/pull/{number}",
                "ts": ts,
                "screens": screens,
            }
            if credential.search(json.dumps(item, ensure_ascii=False)):
                continue
            seen.add(item_id)
            items.append(item)
    items.sort(key=lambda i: (-i["ts"], i["id"]))
    items = items[:MAX_ITEMS]
    feed = {"schema": SCHEMA, "generated_epoch": args.now, "items": items}

    write_atomic(
        out_dir / FEED_NAME,
        json.dumps(feed, ensure_ascii=False, separators=(",", ":")).encode() + b"\n",
    )
    prune(out_dir / IMAGE_DIR, items)
    save_cache(cache_path, new_cache)
    return feed


def prune(root: Path, items: list[dict[str, Any]]) -> None:
    """Drop the images of items that left the feed (and any file it does not list)."""
    listed = {
        i["id"]: {s[k] for s in i["screens"] for k in ("before", "after") if s[k]}
        for i in items
    }
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_dir() and child.name not in listed:
            shutil.rmtree(child, ignore_errors=True)
        elif child.is_dir():
            for f in child.iterdir():
                if f.name not in listed[child.name]:
                    f.unlink(missing_ok=True)


def save_cache(path: Path, doc: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, sort_keys=True))
        tmp.replace(path)
    except OSError as exc:
        print(f"screen-changes: cache not saved: {exc}", file=sys.stderr)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--now", type=int, required=True)
    parser.add_argument("--home-id", required=True)
    args = parser.parse_args(argv)
    if not SLUG_RE.match(args.home_id):
        print("screen-changes: --home-id is not a slug", file=sys.stderr)
        return 2
    try:
        build(args)
    except Skip:
        return 3
    except (OSError, ValueError, subprocess.SubprocessError, TimeoutError) as exc:
        print(f"screen-changes: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
