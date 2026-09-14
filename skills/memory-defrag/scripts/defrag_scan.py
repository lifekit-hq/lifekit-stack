#!/usr/bin/env python3
"""Defrag-candidate scanner for the memory vault (read-only, stdlib-only).

Companion to the vault harness ($VAULT/bin/lint.sh, since 2026-09-14): that lints the
contract; this one surfaces REORGANIZATION candidates for a /defrag session
(decision of record: vault system/proposals.md 2026-07-20-dreaming-as-defragmentation).

Candidate classes:
  - near-duplicates  (title+summary token overlap within/across lessons/ and concepts/)
  - oversized pages  (wiki pages past ~500 lines; the contract says propose a split)
  - orphan sources   (pageType: source cited by zero claims and linked from zero pages)
  - INDEX drift      (flat indexed categories: page missing its hook line in INDEX.md)

The scanner finds candidates; it does not decide. Every finding needs a human
judgment pass before becoming a lane-A fix or lane-B proposal.

Exit 0 = no candidates, 1 = candidates found. Never writes.
"""

import argparse
import itertools
import os
import re
import sys

SKIP_DIRS = {".git", ".obsidian", ".openclaw-wiki"}
DUP_DIRS = ("lessons/", "concepts/")
INDEXED_FLAT = ("lessons/", "domains/", "system/")
OVERSIZE_LINES = 500
DUP_THRESHOLD = 0.35
WIKI_PREFIXES = (
    "domains/",
    "projects/",
    "system/",
    "concepts/",
    "entities/",
    "syntheses/",
    "lessons/",
)
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "how",
    "in",
    "is",
    "it",
    "its",
    "not",
    "of",
    "on",
    "or",
    "over",
    "per",
    "that",
    "the",
    "this",
    "to",
    "use",
    "when",
    "with",
    "vs",
    "via",
}


def find_vault(cli):
    for c in (
        cli,
        os.environ.get("MEMORY_VAULT"),
        os.path.expanduser("~/memory"),
        "/srv/memory",
    ):
        if c and os.path.isfile(os.path.join(os.path.expanduser(c), "README.md")):
            return os.path.expanduser(c)
    sys.exit("vault not found (tried --vault, $MEMORY_VAULT, ~/memory, /srv/memory)")


def walk_md(vault):
    for dp, dns, fns in os.walk(vault):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if fn.endswith(".md"):
                yield os.path.relpath(os.path.join(dp, fn), vault)


def read(vault, rel):
    with open(os.path.join(vault, rel), encoding="utf-8", errors="replace") as f:
        return f.read()


def frontmatter(text):
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    return parts[1] if len(parts) >= 3 else ""


def fm_field(fm, key):
    m = re.search(r"^%s:\s*(.+)$" % re.escape(key), fm, re.M)
    return m.group(1).strip().strip("\"'") if m else ""


def tokens(rel, text):
    fm = frontmatter(text)
    summary = fm_field(fm, "summary") or fm_field(fm, "description")
    title = fm_field(fm, "title") or fm_field(fm, "name")
    base = os.path.splitext(os.path.basename(rel))[0]
    raw = " ".join([base.replace("-", " "), title, summary]).lower()
    return {
        t for t in re.findall(r"[a-z0-9]+", raw) if len(t) > 2 and t not in STOPWORDS
    }


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def scan(vault):
    pages = {rel: read(vault, rel) for rel in walk_md(vault)}
    findings = []

    # --- near-duplicates (lessons/ + concepts/, INDEX files excluded) ---
    dup_pool = {
        rel: tokens(rel, text)
        for rel, text in pages.items()
        if rel.startswith(DUP_DIRS)
        and os.path.basename(rel).lower() not in ("index.md",)
    }
    pairs = []
    for (ra, ta), (rb, tb) in itertools.combinations(sorted(dup_pool.items()), 2):
        score = jaccard(ta, tb)
        if score >= DUP_THRESHOLD:
            pairs.append((score, ra, rb))
    for score, ra, rb in sorted(pairs, reverse=True):
        findings.append(("near-duplicate", "%.2f  %s  <->  %s" % (score, ra, rb)))

    # --- oversized pages (wiki layer only; run-exhaust scaffold excluded) ---
    for rel, text in sorted(pages.items()):
        if not rel.startswith(WIKI_PREFIXES):
            continue
        if "/tasks/" in rel or "/runs/" in rel:
            continue
        n = text.count("\n") + 1
        if n > OVERSIZE_LINES:
            findings.append(
                ("oversized", "%s (%d lines; contract says propose a split)" % (rel, n))
            )

    # --- orphan sources (uncited by claims, unlinked by pages) ---
    cited = set(re.findall(r"sourceId:\s*(\S+)", "\n".join(pages.values())))
    all_links = {
        os.path.basename(t).strip().lower()
        for text in pages.values()
        for t in LINK_RE.findall(text)
    }
    for rel, text in sorted(pages.items()):
        if not rel.startswith("sources/") or os.path.basename(rel) == "README.md":
            continue
        fm = frontmatter(text)
        if fm_field(fm, "pageType") != "source":
            continue
        sid = fm_field(fm, "id")
        base = os.path.splitext(os.path.basename(rel))[0].lower()
        if sid not in cited and base not in all_links:
            findings.append(
                (
                    "orphan-source",
                    "%s (id=%s: zero citing claims, zero inbound links)"
                    % (rel, sid or "?"),
                )
            )

    # --- INDEX drift (flat indexed categories) ---
    for cat in INDEXED_FLAT:
        index_rel = cat + "INDEX.md"
        if index_rel not in pages:
            continue
        indexed = {t.strip().lower() for t in LINK_RE.findall(pages[index_rel])}
        indexed |= {os.path.basename(t).strip().lower() for t in indexed}
        for rel in sorted(pages):
            if os.path.dirname(rel) + "/" != cat:
                continue
            base = os.path.splitext(os.path.basename(rel))[0]
            if base == "INDEX":
                continue
            if base.lower() not in indexed:
                findings.append(
                    ("index-drift", "%s not linked from %s" % (rel, index_rel))
                )

    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--vault", help="vault root (default: $MEMORY_VAULT, ~/memory, /srv/memory)"
    )
    args = ap.parse_args()
    vault = find_vault(args.vault)

    findings = scan(vault)
    by_class = {}
    for cls, msg in findings:
        by_class.setdefault(cls, []).append(msg)

    print("defrag_scan: %s" % vault)
    for cls in ("near-duplicate", "oversized", "orphan-source", "index-drift"):
        msgs = by_class.get(cls, [])
        print("\n[%s] %d candidate(s)" % (cls, len(msgs)))
        for msg in msgs:
            print("  - %s" % msg)

    print("\n%d candidate(s) total" % len(findings))
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
