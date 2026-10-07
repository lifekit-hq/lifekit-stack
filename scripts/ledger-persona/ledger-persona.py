#!/usr/bin/env python3
"""Compose the Ledger persona from finance-sentry at a pinned ref, install it
into the Ledger agent's OpenClaw workspace, and report drift.

finance-sentry's agent/ledger/ is the source of truth (its README, "Composition
model"): the OpenClaw persona = persona.core.md + adapters/openclaw.md. This
script fetches those two files at the commit in the PIN file next to it, joins
them, and writes the result to <workspace>/AGENTS.md.

  compose              print the composed persona to stdout
  check                exit 0 when the live AGENTS.md equals the composed
                       persona, 1 (with a unified diff summary) when it
                       differs or is missing. Read-only; never writes.
  install              write the composed persona to the workspace (the old
                       file is kept as AGENTS.md.pre-persona-install). An
                       operator command: deploy.sh never runs it.

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
        if d is None:
            print(f"ledger persona: in sync with finance-sentry@{pin[:7]}")
            return 0
        print(f"ledger persona: DRIFT from finance-sentry@{pin[:7]}\n{d}")
        return 1
    result = install(args.workspace, composed, args.allow_oversize)
    print(f"ledger persona: {result} (finance-sentry@{pin[:7]}, {len(composed)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
