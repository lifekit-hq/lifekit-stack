#!/usr/bin/env python3
"""Compose the Ledger persona from finance-sentry at a pinned ref, install it
into the Ledger agent's OpenClaw workspace, and report drift.

finance-sentry's agent/ledger/ is the source of truth (its README, "Composition
model"): the OpenClaw persona = persona.core.md + adapters/openclaw.md. This
script fetches those two files at the commit in the PIN file next to it, joins
them, and writes the result to <workspace>/AGENTS.md.

finance-sentry's framework primer (served by its MCP server) is Ledger's only
big-picture source. `check` therefore also reports a retired big-picture copy
that is back in the workspace: a path in RETIRED_PATHS, or a STALE_MARKERS hit
in one of SCANNED_FILES. Those files stay host state (Ledger writes its own
memory there), so they are scanned for known markers, never compared byte for
byte, and nothing here writes them.

  compose              print the composed persona to stdout
  check                exit 0 when the live AGENTS.md equals the composed
                       persona and no retired copy is back, 1 (with a
                       `stale:` line per copy, then a unified diff summary)
                       otherwise. Read-only; never writes.
                       Any other failure (fetch, unreadable file, bad pin)
                       exits 2, so drift is never confused with an error.
  install              write the composed persona to the workspace (the old
                       file is kept as AGENTS.md.pre-persona-install).
                       Idempotent; deploy.sh runs it, then `check`, on every
                       deploy.

Options: --workspace DIR (default $LEDGER_WORKSPACE or the finance agent's
workspace), --pin REF (default: the PIN file), --source DIR (read a local
checkout's agent/ledger instead of fetching; for tests and rehearsal).

Fetch is read-only: `gh api` on the GitHub contents API at the pinned SHA, with
gh's existing login. No secret is added. A pin bump is a PR that edits PIN.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = "lifekit-hq/finance-sentry"
BASE = "agent/ledger"
PARTS = ("persona.core.md", "adapters/openclaw.md")
SEPARATOR = "\n\n---\n\n"
DEFAULT_WORKSPACE = "/srv/openclaw/config/agents/finance/workspace"
TARGET = "AGENTS.md"
BACKUP = "AGENTS.md.pre-persona-install"
# OpenClaw's bootstrapMaxChars: the persona is truncated past this.
BOOTSTRAP_MAX_CHARS = 20000
PIN_FILE = Path(__file__).resolve().with_name("PIN")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Big-picture copies retired in favour of the finance-sentry primer. Any of
# these paths existing again is a reintroduced copy.
RETIRED_PATHS = {
    "skills/project-context": "retired project-context skill; the primer owns "
    "the big picture",
    "repos/finance-sentry": "retired repo clone; Ledger reads the server "
    "through MCP and files issues with gh",
}
# Ledger's own memory and the owner's facts: pointers to the primer are fine,
# a restated big picture is not. Daily notes under memory/ are not scanned.
SCANNED_FILES = ("MEMORY.md", "USER.md", "SOUL.md")
STACK = "finance-sentry stack detail, the primer owns it"
STALE_MARKERS = (
    (re.compile(r"ASP\.NET", re.I), STACK),
    (re.compile(r"\bAngular \d+"), STACK),
    (re.compile(r"\bMediatR\b"), STACK),
    (re.compile(r"\bPlaid\b"), "Plaid, not a finance-sentry source any more"),
    (re.compile(r"\bdevclaw\b", re.I), "DevClaw handoff, not Ledger's route"),
    (re.compile(r"\bwiki_(get|search)\b"), "wiki tools, plugin removed 2026-09-29"),
    (re.compile(r"dsdevq/finance-sentry"), "old repo location"),
    (re.compile(r"goes back to Kit"), "Kit relay; Ledger replies on its own"),
)


def read_pin(path: Path = PIN_FILE) -> str:
    pin = path.read_text().strip()
    if not SHA_RE.match(pin):
        raise SystemExit(f"{path}: pin must be a 40-char commit SHA, got {pin!r}")
    return pin


def fetch_part(pin: str, rel: str) -> str:
    out = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{REPO}/contents/{BASE}/{rel}?ref={pin}",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"fetching {rel} at {pin[:7]} failed: {out.stderr.strip()}")
    return out.stdout


def load_parts(pin: str, source: Path | None) -> list[str]:
    if source is None:
        return [fetch_part(pin, rel) for rel in PARTS]
    return [(source / rel).read_text() for rel in PARTS]


def compose(parts: list[str]) -> str:
    """core + exactly one adapter, each trimmed, joined by a rule, one final newline."""
    return SEPARATOR.join(p.strip("\n") for p in parts) + "\n"


def drift(workspace: Path, composed: str) -> str | None:
    """None when the live file equals the composed persona, else a diff summary."""
    live_path = workspace / TARGET
    if not live_path.is_file():
        return f"{live_path} is missing"
    live = live_path.read_text()
    if live == composed:
        return None
    diff = list(
        difflib.unified_diff(
            live.splitlines(),
            composed.splitlines(),
            "live " + TARGET,
            "composed",
            lineterm="",
            n=0,
        )
    )
    shown = diff[:60]
    more = len(diff) - len(shown)
    tail = [f"... {more} more diff lines"] if more > 0 else []
    return "\n".join(shown + tail)


def stale_copies(workspace: Path) -> list[str]:
    """One line per retired big-picture copy found in the workspace."""
    found = [
        f"{rel} exists: {why}"
        for rel, why in RETIRED_PATHS.items()
        if (workspace / rel).exists() or (workspace / rel).is_symlink()
    ]
    for name in SCANNED_FILES:
        path = workspace / name
        if not path.is_file():
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for pattern, why in STALE_MARKERS:
                m = pattern.search(line)
                if m:
                    found.append(f"{name}:{n} {m.group(0)!r}: {why}")
    return found


def install(workspace: Path, composed: str, allow_oversize: bool) -> str:
    if len(composed) > BOOTSTRAP_MAX_CHARS and not allow_oversize:
        raise SystemExit(
            f"composed persona is {len(composed)} chars, over bootstrapMaxChars "
            f"{BOOTSTRAP_MAX_CHARS}; the agent would load it truncated. Trim the "
            "persona in finance-sentry or pass --allow-oversize."
        )
    if not workspace.is_dir():
        raise SystemExit(f"workspace {workspace} is not a directory")
    target = workspace / TARGET
    if target.is_file() and target.read_text() == composed:
        return "unchanged"
    if target.is_file():
        (workspace / BACKUP).write_text(target.read_text())
    tmp = workspace / (TARGET + ".tmp")
    tmp.write_text(composed)
    os.replace(tmp, target)
    return "installed"


def main(argv: list[str] | None = None) -> int:
    """Run the command; every failure that is not drift exits 2."""
    try:
        return run(argv)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise
    except Exception as exc:
        print(f"ledger persona: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("compose", "check", "install"))
    ap.add_argument(
        "--workspace",
        type=Path,
        default=Path(os.environ.get("LEDGER_WORKSPACE", DEFAULT_WORKSPACE)),
    )
    ap.add_argument("--pin", help="commit SHA (default: the PIN file)")
    ap.add_argument(
        "--source", type=Path, help="local agent/ledger dir instead of fetching"
    )
    ap.add_argument("--allow-oversize", action="store_true")
    args = ap.parse_args(argv)

    pin = args.pin or read_pin()
    if not SHA_RE.match(pin):
        raise SystemExit(f"pin must be a 40-char commit SHA, got {pin!r}")
    composed = compose(load_parts(pin, args.source))

    if args.command == "compose":
        sys.stdout.write(composed)
        return 0
    if args.command == "check":
        d = drift(args.workspace, composed)
        stale = stale_copies(args.workspace)
        if d is None and not stale:
            print(
                f"ledger persona: in sync with finance-sentry@{pin[:7]}, "
                "no retired big-picture copy"
            )
            return 0
        heads = [f"DRIFT from finance-sentry@{pin[:7]}"] if d is not None else []
        if stale:
            heads.append(
                f"retired big-picture copy back ({len(stale)} {'hit' if len(stale) == 1 else 'hits'})"
            )
        print("ledger persona: " + "; ".join(heads))
        for line in stale:
            print("stale: " + line)
        if d is not None:
            print(d)
        return 1
    result = install(args.workspace, composed, args.allow_oversize)
    print(f"ledger persona: {result} (finance-sentry@{pin[:7]}, {len(composed)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
