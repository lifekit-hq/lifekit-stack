#!/usr/bin/env python3
"""Data-flow lint: every durable store has a row in the vault's Data flow table.

The table is `## Data flow` in `system/system-map.md` of the vault (dsdevq/memory),
one row per store (decision 0019: "each agent - we should understand what it writes
and where it writes"). This script discovers stores and matches them against it.

A store is a durable location a deployed process writes and that outlives the process.
Discovery sources:

  1. compose  every named volume and every read-write bind mount. Statically from
              `compose/**/docker-compose*.yml`; live from `docker inspect` mounts.
  2. systemd  every `StateDirectory=` and `ReadWritePaths=` of a unit, and every
              `Environment=` value whose key ends in _DIR, _STATE, _FILE, _FEED or
              _LEDGER and whose value is an absolute path. Statically from
              `scripts/**/*.service`; live from `systemctl show` and user unit files.
  3. roots    (live only) first-level directories under /var/lib/lifekit,
              /var/lib/lifekit-*, ~/.local/state/*, ~/clip-data/*, and each firstmate
              home's state/ and data/.
  4. install  (static only) `install -d` targets in `scripts/**/install-*.sh` and
              `scripts/**/ensure-*.sh`.

Excluded: secrets (/srv/lifekit-secrets, */credentials*, *secret*, *.env, ssh/key
dirs), caches (*/cache*, node_modules), logs (*.log, /var/log), docker anonymous
volumes, test containers (*-test-*, docker's generated two-word names), and paths
that are not data stores at all (/etc, /usr, /opt, /run, /tmp, unit and binary dirs).

Matching: a store matches a row when the row's Path equals the store or contains it
(the store sits under the row's path), after normalising `~` (each admin home),
`<home>` (each firstmate home), `<placeholder>` and `{a,b}` in the row, and container
paths through the mount table. A store whose own path is a parent of documented rows
(a bind of /var/lib/lifekit, say) is covered by those rows. A named volume matches a
row that names it (`<volume>:<path>`), names the container that mounts it
(`<container> / <db>`), or lists its project or first name word before `volumes`
("prometheus, loki, tempo, grafana volumes"). A row nothing discovered matches, whose
path no longer exists on the host, is stale (live mode).

Modes:
  default (CI gate)  static discovery over the repo (sources 1, 2, 4) plus the unit
                     rule: a repo `*.service` must set ProtectSystem=strict plus
                     ReadWritePaths= or StateDirectory=. Exit 1 on an undocumented store
                     or an unsandboxed unit that `data-flow-allowlist.yaml` does not list.
  --live             live discovery on the host (sources 1 to 3) and a markdown section
                     of unmatched stores and stale rows on stdout. Always exit 0.

The table comes from `--map FILE`, else `DATA_FLOW_MAP`, else a read-only shallow clone
of dsdevq/memory main (`DATA_FLOW_VAULT_URL` overrides the URL).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import glob
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ALLOWLIST = Path(__file__).resolve().with_name("data-flow-allowlist.yaml")
VAULT_URL = os.environ.get(
    "DATA_FLOW_VAULT_URL", "https://github.com/dsdevq/memory.git"
)
MAP_PATH = "system/system-map.md"
DEPLOY_ROOT = "/srv/lifekit-stack"  # where relative compose binds resolve on the host
SERVICE_HOMES = ("/home/lifekit", "/root")  # never what `~` or `<home>` stands for
DOCKER = os.environ.get("DOCKER", "docker")
SYSTEMCTL = os.environ.get("SYSTEMCTL", "systemctl")
CMD_TIMEOUT = 30

ENV_SUFFIXES = ("_DIR", "_STATE", "_FILE", "_FEED", "_LEDGER")
NOT_A_STORE = (
    "/etc",
    "/usr",
    "/opt",
    "/run",
    "/var/run",
    "/tmp",
    "/var/tmp",
    "/proc",
    "/sys",
    "/dev",
    "/var/log",
    "/bin",
    "/lib",
)
SECRET_PATTERNS = (
    "/srv/lifekit-secrets",
    "/srv/lifekit-secrets/*",
    "*/credentials*",
    "*secret*",
    "*.env",
    "*/.ssh",
    "*/.ssh/*",
    "*.key",
    "*.pem",
    "*agekey*",
)
CACHE_PATTERNS = ("*cache*", "*/node_modules*")
LOG_PATTERNS = ("*.log", "*.sock")
TEST_CONTAINER = re.compile(r"(^|.*-)test(-.*|$)")
DOCKER_GENERATED = re.compile(r"^[a-z]+_[a-z]+$")
ANON_VOLUME = re.compile(r"^[0-9a-f]{64}$")
VAR = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)


# ---------------------------------------------------------------------------
# data


@dataclass
class Store:
    """One discovered store. `key` is `bind:<abs path>` or `volume:<name>`."""

    kind: str  # bind | volume
    path: str  # absolute host path, or the volume name
    sources: list[str] = field(default_factory=list)  # where it was declared
    project: str = ""  # volume: compose project
    short: str = ""  # volume: name inside the compose file
    containers: set[str] = field(default_factory=set)  # volume: containers mounting it

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.path}"


@dataclass
class Row:
    store: str
    path_cell: str
    line: int
    patterns: list[str] = field(default_factory=list)  # absolute path globs
    volumes: list[str] = field(default_factory=list)  # named volumes (`<vol>:<path>`)
    containers: list[str] = field(default_factory=list)  # `<container> / <db>`
    volume_words: list[str] = field(default_factory=list)  # "a, b volumes"
    remote: bool = False  # another host (`denys-pc:...`): never matched or stale


@dataclass
class Mount:
    source: str  # host path or volume name
    target: str  # container path
    kind: str  # bind | volume


# ---------------------------------------------------------------------------
# exclusions


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def excluded(path: str) -> str:
    """Why a path is not a store ('' when it is one)."""
    low = path.lower()
    if any(fnmatch.fnmatch(low, p) for p in SECRET_PATTERNS):
        return "secret"
    if any(fnmatch.fnmatch(low, p) for p in CACHE_PATTERNS):
        return "cache"
    if any(fnmatch.fnmatch(low, p) for p in LOG_PATTERNS):
        return "log"
    if path.startswith("/") and _under(path, NOT_A_STORE):
        return "not a store"
    return ""


def is_test_container(name: str, compose_project: str = "") -> bool:
    name = name.lstrip("/")
    if TEST_CONTAINER.match(name):
        return True
    return not compose_project and bool(DOCKER_GENERATED.match(name))


# ---------------------------------------------------------------------------
# the table


def parse_table(text: str) -> list[Row]:
    """Rows of the `## Data flow` table; a missing table is an error."""
    lines = text.splitlines()
    try:
        start = next(
            i for i, ln in enumerate(lines) if ln.strip().lower() == "## data flow"
        )
    except StopIteration:
        raise ValueError("no '## Data flow' section in the system map") from None
    rows: list[Row] = []
    header: list[str] | None = None
    for i in range(start + 1, len(lines)):
        ln = lines[i].strip()
        if ln.startswith("## "):
            break
        if not ln.startswith("|"):
            if header is not None and rows:
                break
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        cols = dict(zip(header, cells))
        if "path" not in cols:
            raise ValueError("the Data flow table has no Path column")
        rows.append(make_row(cols.get("store", ""), cols["path"], i + 1))
    if header is None:
        raise ValueError("the '## Data flow' section has no table")
    return rows


def _braces(s: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", s)
    if not m:
        return [s]
    out = []
    for alt in m.group(1).split(","):
        out.extend(_braces(s[: m.start()] + alt.strip() + s[m.end() :]))
    return out


def make_row(store: str, cell: str, line: int) -> Row:
    """One table row; the Path cell's backticked fragments carry the paths.

    `host:~/x` or `host:%APPDATA%` is another machine's path (never matched here);
    `name:/path` and a bare `name` are a named volume; `container / db` names the
    container that holds the store; an unticked "a, b volumes" lists volume words.
    """
    row = Row(store=store, path_cell=cell, line=line)
    ticked = re.findall(r"`([^`]+)`", cell)
    words = re.match(r"^\s*([\w ,.-]+?)\s+volumes\s*$", cell)
    if not ticked and words:
        row.volume_words = [
            w.strip().lower() for w in words.group(1).split(",") if w.strip()
        ]
        return row
    for frag in ticked or [cell]:
        frag = frag.strip()
        m = re.match(r"^([\w.-]+):(.*)$", frag)
        if m and not m.group(2).startswith("//"):
            if m.group(2).startswith(("~", "%")) or "\\" in m.group(2):
                row.remote = True
            else:
                row.volumes.append(m.group(1))
            continue
        m = re.match(r"^([\w.-]+)\s+/\s+([\w.-]+)$", frag)
        if m:
            row.containers.append(m.group(1))
            continue
        if ticked and re.match(r"^[\w.-]+$", frag):
            row.volumes.append(frag)
            continue
        for alt in _braces(frag):
            row.patterns.append(re.sub(r"<(?!home>)[^>]+>", "*", alt))
    return row


def expand_pattern(p: str, homes: list[str], fm_homes: list[str]) -> list[str]:
    """Absolute globs for one row path (`~`, `<home>`, `$HOME`); relative paths drop."""
    if p.startswith("~/") or p == "~":
        bases = homes or ["/home/*"]
        return [b.rstrip("/") + p[1:] for b in bases]
    if p.startswith("<home>"):
        bases = fm_homes or ["/home/*/*", "/home/*"]
        return [b.rstrip("/") + p[len("<home>") :] for b in bases]
    if p.startswith("/"):
        return [p.rstrip("/") or "/"]
    return []


def glob_reaches(path: str, pattern: str) -> bool:
    """fnmatch, except a wildcard never reaches into a service account's home."""
    if not fnmatch.fnmatch(path, pattern):
        return False
    prefix = literal_prefix(pattern)
    return not any(
        _under(path, (h,)) and not _under(prefix, (h,)) for h in SERVICE_HOMES
    )


def literal_prefix(pattern: str) -> str:
    """The directory part of a glob before its first wildcard."""
    idx = min([pattern.find(c) for c in "*?[" if c in pattern] or [len(pattern)])
    head = pattern[:idx]
    if idx < len(pattern):
        head = head.rsplit("/", 1)[0]
    return head.rstrip("/") or "/"


# ---------------------------------------------------------------------------
# matching


@dataclass
class Context:
    homes: list[str] = field(default_factory=list)
    fm_homes: list[str] = field(default_factory=list)
    mounts: list[Mount] = field(default_factory=list)


def row_globs(row: Row, ctx: Context) -> list[str]:
    out: list[str] = []
    for p in row.patterns:
        for g in expand_pattern(p, ctx.homes, ctx.fm_homes):
            out.append(g)
            # a container-side path maps to its host source through the mount table
            for m in ctx.mounts:
                if m.kind == "bind" and (
                    g == m.target or g.startswith(m.target.rstrip("/") + "/")
                ):
                    out.append(m.source.rstrip("/") + g[len(m.target.rstrip("/")) :])
    return out


def _ancestors(path: str) -> list[str]:
    parts = path.rstrip("/").split("/")
    return ["/".join(parts[:i]) or "/" for i in range(len(parts), 0, -1)]


def store_matches_row(
    store: Store, row: Row, ctx: Context, parents: bool = True
) -> bool:
    """`parents=False` drops the store-is-a-parent-of-the-row match (the stale check's view)."""
    if row.remote:
        return False
    if store.kind == "volume":
        names = {store.path, store.short}
        if any(v in names for v in row.volumes):
            return True
        if store.containers & set(row.containers):
            return True
        words = {w.lower() for w in re.split(r"[_-]", store.path)} | {
            store.project.lower()
        }
        if row.volume_words and words & set(row.volume_words):
            return True
        return False
    for g in row_globs(row, ctx):
        for anc in _ancestors(store.path):
            if glob_reaches(anc, g):
                return True  # equal, or the store sits under the row's path
        prefix = literal_prefix(g)
        if prefix == store.path and prefix != g:
            return True  # the store holds a documented glob (an empty inbox is not stale)
        if parents and prefix.startswith(store.path.rstrip("/") + "/"):
            return True  # the store is a parent of a documented row
    return False


def unmatched(stores: list[Store], rows: list[Row], ctx: Context) -> list[Store]:
    return [s for s in stores if not any(store_matches_row(s, r, ctx) for r in rows)]


def _exists(pattern: str) -> bool | None:
    """True/False, or None when the host cannot tell (an unreadable parent)."""
    if any(c in pattern for c in "*?["):
        if any(glob_reaches(p, pattern) for p in glob.glob(pattern)):
            return True
        base = literal_prefix(pattern)
    else:
        if os.path.lexists(pattern):
            return True
        base = os.path.dirname(pattern.rstrip("/")) or "/"
    while base and not os.path.isdir(base):
        if os.path.lexists(base):
            return False
        base = os.path.dirname(base.rstrip("/")) or "/"
        if base == "/":
            break
    return False if os.access(base, os.R_OK | os.X_OK) else None


def stale_rows(
    rows: list[Row],
    stores: list[Store],
    ctx: Context,
    volumes: set[str] | None,
    containers: set[str] | None,
) -> tuple[list[Row], list[Row]]:
    """(stale, unverifiable): rows nothing discovered matches and the host lacks.

    `volumes`/`containers` is None when docker could not list them: a row naming
    one is then unverifiable, not stale.
    """
    stale, unknown = [], []
    for row in rows:
        if row.remote:
            continue
        # A parent store (a bind of all of /var/lib/lifekit) does not discover the
        # rows beneath it: those still need their own path on the host.
        if any(store_matches_row(s, row, ctx, parents=False) for s in stores):
            continue
        if row.volumes:
            if volumes is None:
                unknown.append(row)
            elif not any(v in volumes for v in row.volumes):
                stale.append(row)
            continue
        if row.containers:
            if containers is None:
                unknown.append(row)
            elif not any(c in containers for c in row.containers):
                stale.append(row)
            continue
        globs = row_globs(row, ctx)
        if not globs:
            continue  # descriptive or relative path: nothing to check on the host
        seen = [_exists(g) for g in globs]
        if any(s is True for s in seen):
            continue
        (unknown if any(s is None for s in seen) else stale).append(row)
    return stale, unknown


# ---------------------------------------------------------------------------
# static discovery (the repo)


def _yaml():
    import yaml  # noqa: PLC0415 - only the static gate and the allowlist need it

    class Loader(yaml.SafeLoader):
        pass

    def any_tag(loader, _suffix, node):
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_scalar(node)

    Loader.add_multi_constructor("!", any_tag)
    return yaml, Loader


def subst(value: str, env: dict[str, str] | None = None) -> str:
    """`${VAR:-default}` -> env value or default; an unknown bare `$VAR` stays."""
    env = env or {}

    def rep(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        if name in env:
            return env[name]
        if m.group(1) and m.group(2) is not None:
            return m.group(2)
        return m.group(0)

    for _ in range(8):  # nested defaults; bounded so `X=$X/more` cannot grow forever
        prev, value = value, VAR.sub(rep, value)
        if value == prev:
            break
    return value


def _compose_mounts(svc: dict) -> list[tuple[str, str, bool]]:
    """(source, target, read_only) for each volume entry of a service."""
    out = []
    for v in svc.get("volumes") or []:
        if isinstance(v, str):
            parts = subst(v).split(":")
            if len(parts) == 1:
                out.append(("", parts[0], False))
                continue
            src, dst = parts[0], parts[1]
            mode = parts[2] if len(parts) > 2 else ""
            out.append((src, dst, "ro" in mode.split(",")))
        elif isinstance(v, dict):
            if v.get("type") == "tmpfs":
                continue
            out.append(
                (
                    subst(str(v.get("source") or "")),
                    str(v.get("target") or ""),
                    bool(v.get("read_only")),
                )
            )
    return out


def discover_compose_file(path: Path, repo: Path) -> tuple[list[Store], list[Mount]]:
    yaml, loader = _yaml()
    doc = yaml.load(path.read_text(), Loader=loader) or {}
    project = doc.get("name") or (
        path.parent.name if path.parent != repo / "compose" else "compose"
    )
    declared = doc.get("volumes") or {}
    rel = path.relative_to(repo) if path.is_relative_to(repo) else path
    stores: dict[str, Store] = {}
    mounts: list[Mount] = []
    for name, svc in (doc.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        cname = svc.get("container_name") or f"{project}-{name}-1"
        if is_test_container(cname, project) or TEST_CONTAINER.match(name):
            continue
        for src, dst, ro in _compose_mounts(svc):
            src = subst(src)
            if not src:
                continue  # anonymous volume
            where = f"{rel} services.{name}"
            if src.startswith(("/", ".", "~")):
                if src.startswith("."):
                    src = os.path.normpath(
                        os.path.join(DEPLOY_ROOT, str(rel.parent), src)
                    )
                mounts.append(Mount(src, dst, "bind"))
                if ro or "$" in src or excluded(src) or excluded(dst):
                    continue
                st = stores.setdefault(f"bind:{src}", Store("bind", src))
                st.sources.append(where)
            else:
                spec = declared.get(src) if isinstance(declared, dict) else None
                full = (spec or {}).get("name") if isinstance(spec, dict) else None
                full = subst(str(full)) if full else f"{project}_{src}"
                mounts.append(Mount(full, dst, "volume"))
                if excluded(dst):
                    continue
                st = stores.setdefault(
                    f"volume:{full}", Store("volume", full, project=project, short=src)
                )
                st.sources.append(where)
                st.containers.add(cname)
    return list(stores.values()), mounts


def parse_unit(text: str) -> dict[str, list[str]]:
    """Directives of a unit file (continuation lines joined; repeated keys kept)."""
    out: dict[str, list[str]] = {}
    joined = re.sub(r"\\\n", " ", text)
    for ln in joined.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("#", ";", "[")) or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        out.setdefault(k.strip(), []).append(v.strip())
    return out


def unit_paths(d: dict[str, list[str]]) -> list[tuple[str, str]]:
    """(path, directive) for the store-bearing directives of one parsed unit."""
    out = []
    for v in d.get("StateDirectory", []):
        for name in v.split():
            out.append(("/var/lib/" + name.split(":")[0].strip("/"), "StateDirectory"))
    for v in d.get("ReadWritePaths", []):
        for p in v.split():
            p = p.lstrip("-+")
            if p.startswith("/"):
                out.append((p.rstrip("/"), "ReadWritePaths"))
    for v in d.get("Environment", []):
        try:
            pairs = shlex.split(v)
        except ValueError:
            pairs = v.split()
        for pair in pairs:
            key, _, val = pair.partition("=")
            if key.endswith(ENV_SUFFIXES) and val.startswith("/"):
                for p in val.split():
                    if p.startswith("/"):
                        out.append((p.rstrip("/"), f"Environment {key}"))
    return out


def discover_units(paths: list[Path], repo: Path) -> list[Store]:
    stores: dict[str, Store] = {}
    for path in paths:
        rel = path.relative_to(repo) if path.is_relative_to(repo) else path
        for p, how in unit_paths(parse_unit(path.read_text())):
            if excluded(p) or "__" in p or "@" in p:
                continue
            stores.setdefault(f"bind:{p}", Store("bind", p)).sources.append(
                f"{rel} {how}"
            )
    return list(stores.values())


def _shell_vars(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for m in re.finditer(
        r'^\s*(?:readonly\s+|export\s+|local\s+)?([A-Za-z_][A-Za-z0-9_]*)=("[^"\n]*"|\'[^\'\n]*\'|[^\s;#]+)',
        text,
        re.M,
    ):
        val = m.group(2).strip("\"'")
        env.setdefault(m.group(1), val)
    return env


def discover_installers(paths: list[Path], repo: Path) -> list[Store]:
    stores: dict[str, Store] = {}
    for path in paths:
        text = path.read_text()
        env = _shell_vars(text)
        resolved = {k: v for k, v in env.items() if k != "INSTALL_ROOT"}
        resolved["INSTALL_ROOT"] = ""
        # resolve variables defined from other variables, a few rounds; a variable
        # never substitutes into itself (`X="${X:-/default}"` keeps its default)
        for _ in range(4):
            for k in list(resolved):
                resolved[k] = subst(
                    resolved[k], {o: v for o, v in resolved.items() if o != k}
                )
        joined = re.sub(r"\\\n", " ", text)
        rel = path.relative_to(repo) if path.is_relative_to(repo) else path
        for m in re.finditer(r"(?:^|[\s;&|(])install\s+(-d\b[^\n;&|]*)", joined, re.M):
            try:
                toks = shlex.split(m.group(1), comments=True)
            except ValueError:
                continue
            args, skip = [], False
            for t in toks[1:]:
                if skip:
                    skip = False
                    continue
                if t in ("-m", "-o", "-g", "--mode", "--owner", "--group"):
                    skip = True
                    continue
                if t.startswith("-") or t.startswith("${") and t.endswith("[@]}"):
                    continue
                args.append(t)
            for a in args:
                p = subst(a, resolved)
                if "$(" in p or "`" in p:
                    continue
                p = subst(
                    p.replace("$INSTALL_ROOT", "").replace("${INSTALL_ROOT}", ""),
                    resolved,
                )
                if not p.startswith("/") or "$" in p or "__" in p:
                    continue
                p = os.path.normpath(p)
                if excluded(p):
                    continue
                stores.setdefault(f"bind:{p}", Store("bind", p)).sources.append(
                    f"{rel} install -d"
                )
    return list(stores.values())


def merge(*groups: list[Store]) -> list[Store]:
    out: dict[str, Store] = {}
    for group in groups:
        for s in group:
            have = out.get(s.key)
            if have:
                have.sources += [x for x in s.sources if x not in have.sources]
                have.containers |= s.containers
            else:
                out[s.key] = s
    return sorted(out.values(), key=lambda s: s.key)


def discover_static(repo: Path) -> tuple[list[Store], list[Mount], list[Path]]:
    compose_files = sorted(
        p for p in (repo / "compose").rglob("docker-compose*.y*ml") if p.is_file()
    )
    units = sorted((repo / "scripts").rglob("*.service"))
    installers = sorted(
        {
            p
            for pat in ("install-*.sh", "ensure-*.sh")
            for p in (repo / "scripts").rglob(pat)
        }
    )
    groups, mounts = [], []
    for f in compose_files:
        s, m = discover_compose_file(f, repo)
        groups.append(s)
        mounts += m
    groups.append(discover_units(units, repo))
    groups.append(discover_installers(installers, repo))
    return merge(*groups), mounts, units


def unit_violations(units: list[Path], repo: Path) -> list[str]:
    """Repo units that do not sandbox their writes (rule 2)."""
    bad = []
    for path in units:
        d = parse_unit(path.read_text())
        strict = any(v.strip() == "strict" for v in d.get("ProtectSystem", []))
        declares = any(
            v.strip() for v in d.get("ReadWritePaths", []) + d.get("StateDirectory", [])
        )
        if not (strict and declares):
            bad.append(
                str(path.relative_to(repo) if path.is_relative_to(repo) else path)
            )
    return bad


# ---------------------------------------------------------------------------
# live discovery (the host)


def _run(cmd: list[str]) -> str | None:
    """The command's stdout, or None when it could not run or failed."""
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def discover_live_docker(
    notes: list[str],
) -> tuple[list[Store], list[Mount], set[str] | None, set[str] | None]:
    """Stores, mounts, volume names, container names; a listing docker refused is None."""
    listed = _run([DOCKER, "ps", "-aq"])
    vols = _run([DOCKER, "volume", "ls", "-q"])
    volumes = None if vols is None else set(vols.split())
    ids = (listed or "").split()
    if not ids:
        notes.append(
            "docker: no containers listed (docker unreachable or none present)"
        )
        return [], [], volumes, None if listed is None else set()
    raw = _run([DOCKER, "inspect", *ids])
    try:
        info = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        info = []
    stores: dict[str, Store] = {}
    mounts: list[Mount] = []
    names: set[str] = set()
    for c in info:
        name = (c.get("Name") or "").lstrip("/")
        labels = (c.get("Config") or {}).get("Labels") or {}
        project = labels.get("com.docker.compose.project", "")
        names.add(name)
        if is_test_container(name, project):
            continue
        for m in c.get("Mounts") or []:
            kind = m.get("Type")
            src = m.get("Name") if kind == "volume" else m.get("Source")
            dst = m.get("Destination") or ""
            if not src or kind not in ("bind", "volume"):
                continue
            mounts.append(Mount(src, dst, kind))
            if kind == "volume":
                if ANON_VOLUME.match(src) or excluded(src) or excluded(dst):
                    continue
                st = stores.setdefault(
                    f"volume:{src}", Store("volume", src, project=project)
                )
                st.short = (
                    src[len(project) + 1 :]
                    if project and src.startswith(project + "_")
                    else src
                )
                st.containers.add(name)
                st.sources.append(f"container {name}")
            elif m.get("RW") and not excluded(src) and not excluded(dst):
                stores.setdefault(f"bind:{src}", Store("bind", src)).sources.append(
                    f"container {name}"
                )
    return list(stores.values()), mounts, volumes, names


def _lifekit_units(repo: Path) -> list[str]:
    names = {p.name for p in (repo / "scripts").rglob("*.service")}
    for ln in (
        _run(
            [SYSTEMCTL, "list-unit-files", "--type=service", "--no-legend", "lifekit*"]
        )
        or ""
    ).splitlines():
        if ln.split():
            names.add(ln.split()[0])
    return sorted(names)


def discover_live_systemd(
    repo: Path, notes: list[str]
) -> tuple[list[Store], list[str], list[str]]:
    """Stores from system units, plus the admin homes and firstmate homes they name."""
    units = _lifekit_units(repo)
    out = (
        _run(
            [
                SYSTEMCTL,
                "show",
                "-p",
                "Id,User,LoadState,StateDirectory,ReadWritePaths,Environment",
                *units,
            ]
        )
        or ""
    )
    if not out:
        notes.append("systemd: systemctl show returned nothing")
    stores: dict[str, Store] = {}
    users: set[str] = set()
    fm_homes: set[str] = set()
    for block in out.split("\n\n"):
        d = parse_unit(block)
        uid = (d.get("Id") or [""])[0]
        if (d.get("LoadState") or [""])[0] != "loaded":
            continue
        user = (d.get("User") or [""])[0]
        if user and user not in ("root", "lifekit"):
            users.add(user)
        for v in d.get("Environment", []):
            for pair in shlex.split(v) if v else []:
                key, _, val = pair.partition("=")
                if key.endswith("_HOMES") or key.endswith("FM_HOME"):
                    fm_homes.update(
                        p.rstrip("/") for p in val.split() if p.startswith("/")
                    )
        for p, how in unit_paths(d):
            if not excluded(p):
                stores.setdefault(f"bind:{p}", Store("bind", p)).sources.append(
                    f"{uid} {how}"
                )
    homes = []
    for u in sorted(users):
        try:
            homes.append(pwd.getpwnam(u).pw_dir)
        except KeyError:
            pass
    # user units of the admin accounts (`~/.config/systemd/user`), read as files
    for h in homes:
        udir = Path(h) / ".config/systemd/user"
        try:
            files = sorted(udir.glob("*.service"))
        except OSError:
            files = []
        if not files and not os.access(h, os.X_OK):
            notes.append(f"user units: {udir} not readable by this account")
        for f in files:
            try:
                d = parse_unit(f.read_text())
            except OSError:
                continue
            for p, how in unit_paths(d):
                p = p.replace("%h", h)
                if not excluded(p):
                    stores.setdefault(f"bind:{p}", Store("bind", p)).sources.append(
                        f"user {f.name} {how}"
                    )
    return list(stores.values()), homes, sorted(fm_homes)


def discover_live_roots(
    homes: list[str], fm_homes: list[str], notes: list[str]
) -> list[Store]:
    roots: list[tuple[str, bool]] = [("/var/lib/lifekit", True)]
    roots += [(p, False) for p in sorted(glob.glob("/var/lib/lifekit-*"))]
    for h in homes:
        roots += [(f"{h}/.local/state", True), (f"{h}/clip-data", True)]
    for fm in fm_homes:
        roots += [(f"{fm}/state", False), (f"{fm}/data", False)]
    stores: list[Store] = []
    for root, children in roots:
        if not children:
            if os.path.isdir(root) and not excluded(root):
                stores.append(Store("bind", root, sources=["root listing"]))
            elif not os.path.exists(root) and not os.access(
                os.path.dirname(root), os.X_OK
            ):
                notes.append(f"roots: {root} not readable by this account")
            continue
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name)
        except FileNotFoundError:
            continue
        except OSError:
            notes.append(f"roots: {root} not readable by this account")
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False) and not excluded(e.path):
                stores.append(Store("bind", e.path, sources=[f"root listing {root}"]))
    return stores


# ---------------------------------------------------------------------------
# allowlist


def load_allowlist(path: Path = ALLOWLIST) -> dict[str, dict[str, dict]]:
    """{'stores': {key: entry}, 'units': {path: entry}}; every entry needs remove_by."""
    if not path.exists():
        return {"stores": {}, "units": {}}
    yaml, loader = _yaml()
    doc = yaml.load(path.read_text(), Loader=loader) or {}
    out: dict[str, dict[str, dict]] = {"stores": {}, "units": {}}
    for section, key in (("stores", "store"), ("units", "unit")):
        for entry in doc.get(section) or []:
            if (
                not isinstance(entry, dict)
                or not entry.get(key)
                or not entry.get("remove_by")
            ):
                raise ValueError(
                    f"{path.name}: every {section} entry needs `{key}` and `remove_by`"
                )
            out[section][str(entry[key])] = entry
    return out


def _date(v) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


# ---------------------------------------------------------------------------
# the map


def fetch_map(arg: str | None) -> str:
    src = arg or os.environ.get("DATA_FLOW_MAP")
    if src:
        return Path(src).read_text()
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--branch",
                "main",
                "--filter=blob:none",
                "--no-checkout",
                VAULT_URL,
                tmp,
            ],
            check=True,
            timeout=120,
        )
        return subprocess.run(
            ["git", "-C", tmp, "show", f"HEAD:{MAP_PATH}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout


# ---------------------------------------------------------------------------
# modes


def describe(s: Store) -> str:
    where = "; ".join(s.sources[:3]) + (" ..." if len(s.sources) > 3 else "")
    return f"{s.key}  ({where})"


def gate(repo: Path, map_text: str, allow: dict, today: dt.date) -> int:
    rows = parse_table(map_text)
    stores, mounts, units = discover_static(repo)
    ctx = Context(mounts=mounts)
    missing = unmatched(stores, rows, ctx)
    bad_units = unit_violations(units, repo)
    fail = 0
    print(
        f"data-flow: {len(stores)} store(s) discovered in the repo, {len(rows)} Data flow row(s)"
    )
    for s in missing:
        entry = allow["stores"].get(s.key)
        if entry:
            due = _date(entry["remove_by"])
            tag = "EXPIRED " if due < today else ""
            print(f"  allowlisted {tag}(remove by {due}): {describe(s)}")
        else:
            fail = 1
            print(f"  FAIL no Data flow row: {describe(s)}")
    for u in bad_units:
        entry = allow["units"].get(u)
        if entry:
            due = _date(entry["remove_by"])
            tag = "EXPIRED " if due < today else ""
            print(
                f"  allowlisted {tag}(remove by {due}): unit {u} lacks ProtectSystem=strict + ReadWritePaths=/StateDirectory="
            )
        else:
            fail = 1
            print(
                f"  FAIL unit {u} must set ProtectSystem=strict plus ReadWritePaths= or StateDirectory="
            )
    missing_keys = {s.key for s in missing}
    for key in allow["stores"]:
        if key not in missing_keys:
            print(f"  note: allowlist entry {key} no longer needed - remove it")
    for u in allow["units"]:
        if u not in bad_units:
            print(f"  note: allowlist entry {u} no longer needed - remove it")
    if fail:
        print(
            "data-flow: FAIL - add a row to '## Data flow' in system/system-map.md (dsdevq/memory), "
            "or sandbox the unit (scripts/lint/data-flow.py docstring has the rules)"
        )
    else:
        print("data-flow: ok")
    return fail


def live_report(
    repo: Path, map_text: str, today: dt.date, allow: dict | None = None
) -> str:
    notes: list[str] = []
    rows = parse_table(map_text)
    d_stores, mounts, volumes, containers = discover_live_docker(notes)
    u_stores, homes, fm_homes = discover_live_systemd(repo, notes)
    r_stores = discover_live_roots(homes, fm_homes, notes)
    stores = merge(d_stores, u_stores, r_stores)
    ctx = Context(homes=homes, fm_homes=fm_homes, mounts=mounts)
    return render(
        stores, rows, ctx, volumes, containers, notes, today, allow or {"stores": {}}
    )


def render(stores, rows, ctx, volumes, containers, notes, today, allow) -> str:
    missing = unmatched(stores, rows, ctx)
    stale, unknown = stale_rows(rows, stores, ctx, volumes, containers)
    out = [
        "## Data flow drift",
        "",
        f"Generated {today} on the host by lifekit-stack `scripts/lint/data-flow.py --live` "
        "(sources: docker mounts, systemd units, well-known roots) against `system/system-map.md` "
        f"`## Data flow`: {len(stores)} store(s), {len(rows)} row(s). Soft check: file a row, or retire the store.",
        "",
        f"### Stores with no row ({len(missing)})",
        "",
    ]
    out += [
        f"- `{s.path}` ({s.kind}) - {'; '.join(s.sources[:2])}" for s in missing
    ] or ["- none"]
    out += ["", f"### Stale rows ({len(stale)})", ""]
    out += [
        f"- `{r.store}` (line {r.line}): `{r.path_cell.strip('`')}` - nothing on the host matches"
        for r in stale
    ] or ["- none"]
    if unknown:
        out += ["", f"### Rows this account cannot verify ({len(unknown)})", ""]
        out += [f"- `{r.store}`: {r.path_cell}" for r in unknown]
    if notes:
        out += ["", "### Not scanned", ""] + [f"- {n}" for n in notes]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--live",
        action="store_true",
        help="host drift report (markdown, always exit 0)",
    )
    ap.add_argument(
        "--map", help="system-map.md to read instead of fetching dsdevq/memory main"
    )
    ap.add_argument(
        "--repo", default=str(REPO), help="lifekit-stack checkout (default: this one)"
    )
    ap.add_argument("--allowlist", default=str(ALLOWLIST))
    ap.add_argument("--today", help="YYYY-MM-DD (tests)")
    args = ap.parse_args(argv)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    repo = Path(args.repo).resolve()
    if args.live:
        try:
            print(live_report(repo, fetch_map(args.map), today), end="")
        except Exception as err:  # noqa: BLE001 - the live check must never fail its caller
            print(
                f"## Data flow drift\n\n- check did not run: {type(err).__name__}: {err}\n"
            )
        return 0
    try:
        map_text = fetch_map(args.map)
    except (OSError, subprocess.SubprocessError) as err:
        print(f"data-flow: FAIL - cannot read the system map: {err}", file=sys.stderr)
        return 2
    try:
        allow = load_allowlist(Path(args.allowlist))
        return gate(repo, map_text, allow, today)
    except ValueError as err:
        print(f"data-flow: FAIL - {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
