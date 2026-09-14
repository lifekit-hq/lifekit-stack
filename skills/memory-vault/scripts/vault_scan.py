#!/usr/bin/env python3
"""Contract scanner for the memory vault (structure, links, orphans, frontmatter).

The vault's README.md is the contract; this script mechanizes its lint rules:
  - structure allowlist   (README ```vault-structure fenced block)
  - scope coverage        (every allowlist entry is classified; no path is silently unlinted)
  - broken wikilinks      (>=3x targets are contract violations: write or de-link)
  - orphan wiki pages     (no inbound [[links]]; runtime artifacts excluded)
  - missing frontmatter   (wiki-layer pages)
  - legacy last_updated   (renamed vault-wide to updatedAt)
  - project completeness  (projects/<name>/ needs plan.md, log.md, journal.md)
  - empty folders         (a dir with no files rots the taxonomy)
  - non-kebab / hashed    (filenames are kebab-case; dates only where the date IS the identity)
  - missing category INDEX(an indexed category with >=2 pages needs INDEX.md)
  - runtime in knowledge  (*.jsonl/*.db/*.py/*.log under a knowledge dir — evict + gitignore)

Exit 0 = clean, 1 = findings. Read-only, stdlib-only.
"""

import argparse
import collections
import os
import re
import sys

SKIP_DIRS = {".git", ".obsidian", ".openclaw-wiki"}

# Scope is DERIVED from the README structure allowlist, not from a parallel
# hand-maintained list. Every allowlist entry must land in exactly one class
# below; anything unclassified is a finding, never a silent skip. That
# inversion is the point: "protected" and "unchecked" must not be the same
# state, and a directory nobody classified must not read as clean.
OPAQUE_DIRS = {"state", "scout", "Clippings"}  # runtime surfaces, contract-opaque
GENERATED_DIRS = {"reports", "audits"}  # machine-written; no orphan/frontmatter lint
EVIDENCE_DIRS = {"sources"}  # immutable; cited by claims[], not by wikilinks
WIKI_DIRS = {
    "domains",
    "projects",
    "system",
    "concepts",
    "entities",
    "syntheses",
}
DATA_ROOTS = {"topics.yaml"}  # root non-markdown config
# Link targets that are not markdown pages but are legitimately wikilinked.
LINKABLE_EXT = (".md", ".canvas", ".base")
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


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


def read_allowlist(vault):
    m = re.search(
        r"```vault-structure\n(.*?)```",
        open(os.path.join(vault, "README.md")).read(),
        re.S,
    )
    if not m:
        return None
    return {
        ln.strip().rstrip("/")
        for ln in m.group(1).splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }


def derive_scope(vault, allow):
    """Split the allowlist into scope classes, reporting drift in both directions.

    Returns (wiki_prefixes, wiki_roots, indexed_dirs, findings). A path in the
    allowlist that no class claims is reported rather than skipped; a class that
    names a path the allowlist no longer carries is reported too, so a scope rule
    cannot outlive the directory it describes (that is how `lessons/` lingered
    after the directory was merged into `concepts/`).
    """
    findings = []
    classed = OPAQUE_DIRS | GENERATED_DIRS | EVIDENCE_DIRS | WIKI_DIRS | DATA_ROOTS

    root_md = {e for e in allow if e.endswith(".md")}
    dirs = {e for e in allow if not e.endswith(".md") and e not in DATA_ROOTS}

    for entry in sorted(dirs):
        if entry not in classed:
            findings.append(
                f"scope-unclassified: '{entry}' is allowlisted but no scan class claims it — "
                "classify it (wiki/generated/evidence/opaque) or it goes unlinted silently"
            )
    for entry in sorted(classed - allow):
        findings.append(
            f"scope-stale: scan class names '{entry}' but the allowlist no longer carries it — "
            "drop the rule with the directory"
        )

    # Unclassified dirs default to wiki scope: an unknown path gets MORE lint,
    # never less, so the finding above is the only way it stays quiet.
    wiki_dirs = (WIKI_DIRS | (dirs - classed)) & dirs
    wiki_prefixes = tuple(sorted(d + "/" for d in wiki_dirs))
    indexed = {d for d in wiki_dirs if os.path.isdir(os.path.join(vault, d))}
    return wiki_prefixes, root_md, indexed, findings


def walk_md(vault):
    for dp, dns, fns in os.walk(vault):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if fn.endswith(".md"):
                yield os.path.relpath(os.path.join(dp, fn), vault)


def walk_linkable(vault):
    """Non-markdown wikilink targets (Obsidian canvases, Bases views)."""
    for dp, dns, fns in os.walk(vault):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if fn.endswith(LINKABLE_EXT) and not fn.endswith(".md"):
                yield os.path.relpath(os.path.join(dp, fn), vault)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault")
    args = ap.parse_args()
    vault = find_vault(args.vault)
    findings = []

    # -- structure allowlist
    allow = read_allowlist(vault)
    if allow is None:
        findings.append(
            "structure: README has no ```vault-structure block — freeze not machine-readable"
        )
    else:
        for entry in sorted(os.listdir(vault)):
            if entry in SKIP_DIRS or entry in {".gitignore", ".DS_Store"}:
                continue
            if entry.rstrip("/") not in allow:
                findings.append(
                    f"structure: top-level '{entry}' not in allowlist — needs a graded proposal (contract: structure freeze)"
                )

    if allow is None:
        # No machine-readable freeze: fall back to the wiki dirs we know, and say so.
        wiki_prefixes = tuple(sorted(d + "/" for d in WIKI_DIRS))
        wiki_roots, indexed = set(), set(WIKI_DIRS)
    else:
        wiki_prefixes, wiki_roots, indexed, scope_findings = derive_scope(vault, allow)
        findings += scope_findings

    # -- index pages + links
    pages, paths = {}, set()
    for rel in walk_md(vault):
        paths.add(rel.lower())
        paths.add(rel[:-3].lower())
        pages.setdefault(os.path.basename(rel)[:-3].lower(), rel)
    for rel in walk_linkable(vault):  # canvases/bases are link targets, not pages
        paths.add(rel.lower())
        paths.add(os.path.splitext(rel)[0].lower())
        paths.add(os.path.basename(rel).lower())
        paths.add(os.path.splitext(os.path.basename(rel))[0].lower())

    aliases = {}
    for rel in walk_md(vault):
        text = open(os.path.join(vault, rel), encoding="utf-8", errors="replace").read()
        if text.startswith("---"):
            m = re.search(
                r"(?m)^aliases: \[([^\]]*)\]",
                text.split("---", 2)[1] if "---" in text[3:] else "",
            )
            if m:
                for a in m.group(1).split(","):
                    a = a.strip().strip("\"'").lower()  # YAML list items are quoted
                    if a:
                        aliases[a] = rel

    inbound, broken = collections.Counter(), collections.Counter()
    no_fm, legacy = [], []
    for rel in walk_md(vault):
        is_wiki = rel.startswith(wiki_prefixes) or rel in wiki_roots
        text = open(os.path.join(vault, rel), encoding="utf-8", errors="replace").read()
        if (
            is_wiki
            and rel not in wiki_roots
            and "index" not in os.path.basename(rel).lower()
        ):
            if (
                not text.startswith("---")
                and "/tasks/" not in rel
                and "/runs/" not in rel
            ):
                no_fm.append(rel)
            if re.search(r"(?m)^last_updated:", text):
                legacy.append(rel)
        if not is_wiki:
            continue
        # strip code fences/spans so schema examples like `[[name]]` don't count
        scrub = re.sub(r"```.*?```", "", text, flags=re.S)
        scrub = re.sub(r"`[^`\n]*`", "", scrub)
        for m in LINK_RE.finditer(scrub):
            t = m.group(1).strip()
            key = t.lower()
            base = key.split("/")[-1]
            if base.endswith(".md"):  # links may carry the .md extension / a ../ path
                base = base[:-3]
            if base in pages or key in paths or key + ".md" in paths or key in aliases:
                inbound[pages.get(base, aliases.get(key, key))] += 1
            else:
                broken[t] += 1

    for t, c in broken.most_common():
        if c >= 3:
            findings.append(
                f"broken-link: [[{t}]] referenced {c}x — contract says write the page or de-link"
            )
    for rel in sorted(pages.values()):
        if (
            not rel.startswith(wiki_prefixes)
            or "index" in os.path.basename(rel).lower()
        ):
            continue
        if "/tasks/" in rel or "/runs/" in rel:
            continue
        if inbound.get(rel, 0) == 0:
            findings.append(
                f"orphan: {rel} has no inbound [[links]] — link from an INDEX or related page"
            )
    findings += [f"missing-frontmatter: {r}" for r in no_fm]
    findings += [
        f"legacy-field: {r} still uses last_updated (contract: updatedAt)"
        for r in legacy
    ]

    # -- project completeness
    proj = os.path.join(vault, "projects")
    if os.path.isdir(proj):
        for d in sorted(os.listdir(proj)):
            dd = os.path.join(proj, d)
            if not os.path.isdir(dd):
                continue
            # README rotation policy: a concluded project folds its triad down to
            # plan.md (facts + outcome) + compacted log.md and deletes the rest,
            # so an archived project legitimately has no journal.md.
            archived = False
            try:
                with open(os.path.join(dd, "plan.md"), encoding="utf-8") as fh:
                    head = fh.read(2048)
                archived = bool(re.search(r"^status:\s*archived\s*$", head, re.M))
            except OSError:
                pass
            required = ("plan.md", "log.md") if archived else ("plan.md", "log.md", "journal.md")
            missing = [
                f
                for f in required
                if not os.path.exists(os.path.join(dd, f))
            ]
            if missing:
                findings.append(
                    f"project-incomplete: projects/{d}/ missing {', '.join(missing)}"
                )

    # -- filesystem hygiene (empty dirs, non-kebab/hashed names, missing INDEX, runtime files)
    kebab = re.compile(
        r"(?:\d{4}-\d{2}-\d{2}(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?|[a-z0-9]+(?:-[a-z0-9]+)*)$"
    )
    allcaps = re.compile(r"[A-Z][A-Z0-9_]*$")  # INDEX, README, PLAN, STATUS, …
    hashed = re.compile(
        r"(?=[0-9a-f]*[a-f])[0-9a-f]{8,}"
    )  # bridge-*/uuid opaque tokens
    runtime_ext = (".jsonl", ".db", ".sqlite", ".log", ".py")
    runtime_ok = {
        os.path.join("system", "rotate-extras.py")
    }  # documented mechanism exception

    for dp, dns, fns in os.walk(vault):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dp, vault)
        if rel_dir == ".":
            continue
        top = rel_dir.split(os.sep)[0]
        if (
            not [f for f in fns if not f.startswith(".")]
            and not dns
            and ".gitkeep" not in fns
        ):
            findings.append(f"empty-folder: {rel_dir}/ has no files")
        for fn in fns:
            relf = os.path.join(rel_dir, fn)
            if fn.endswith(".md"):
                stem = fn[:-3]
                if not (kebab.match(stem) or allcaps.match(stem)):
                    findings.append(
                        f"non-kebab: {relf} — kebab-case only (dates where the date is the identity)"
                    )
                elif hashed.search(stem):
                    findings.append(
                        f"hashed-name: {relf} — opaque hash/uuid in filename; give it a human slug"
                    )
            if (
                fn.endswith(runtime_ext)
                and (top + "/") in wiki_prefixes
                and relf not in runtime_ok
                and "/tasks/" not in relf
                and "/runs/"
                not in relf  # runtime-scaffold dirs, excluded like orphan/frontmatter checks
            ):
                findings.append(
                    f"runtime-in-knowledge: {relf} — runtime/code doesn't belong in a knowledge dir (gitignore + evict)"
                )
        if rel_dir == top and top in indexed:
            pages_here = [
                f for f in fns if f.endswith(".md") and f.lower() != "index.md"
            ]
            if len(pages_here) >= 2 and not any(f.lower() == "index.md" for f in fns):
                findings.append(
                    f"missing-index: {top}/ has {len(pages_here)} pages but no INDEX.md"
                )

    total = sum(broken.values())
    print(f"vault: {vault}")
    print(f"wikilinks: broken instances {total}, distinct {len(broken)}")
    if findings:
        print(f"\nFINDINGS ({len(findings)}):")
        for f in findings:
            print(f"  - {f}")
        sys.exit(1)
    print("clean — no contract findings")


if __name__ == "__main__":
    main()
