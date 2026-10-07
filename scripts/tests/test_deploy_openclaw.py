"""The deploy split: scripts/deploy-openclaw.sh and the deploy.sh that calls it.

deploy.sh sources deploy-openclaw.sh and calls its OpenClaw phases between
its own platform phases. These tests run deploy.sh end to end against a
scratch copy of the repo (phase bodies and platform helper scripts replaced by
trace-logging stubs) to pin the call order, and source deploy-openclaw.sh
under `set -euo pipefail` to pin the build phase's return status (a phase
whose last command is a false `[[ ]] && ...` would return 1 and trip
deploy.sh's set -e), which compose project and file each phase drives, and
the one-time cutover of the OpenClaw services out of the platform project.
The same end-to-end run pins the gated `identity` and `edge` projects:
declared on every deploy, brought up only once their secrets are rendered
(and, for `edge`, once `identity` is up).
`docker` is a stub on PATH throughout; no test runs a real docker command,
and every host path the phases touch points into the test's tmp dir.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
OPENCLAW = REPO / "scripts/deploy-openclaw.sh"
LIB = REPO / "scripts/lib/deploy-common.sh"
OPENCLAW_SERVICES = ("openclaw-cli", "openclaw-gateway", "google-workspace-mcp")

# LEGACY_CONTAINERS: "<service>=<id> ..." - the containers `docker ps` finds
# for that service in the platform project ("proj", the name `config` gives),
# and only there.
#
# The image store is modelled the way the containerd snapshotter behaves
# (the VPS's): $IMG_STATE holds one file per tag with the image id in it, and
# an id is addressable by `docker tag` only while some tag still points at it
# - even when a container runs it. `compose build` moves lifekit-openclaw:local
# to $NEW_IMAGE. The gateway container (`gw1`, absent when $NO_GATEWAY is set)
# runs $RUN_IMAGE; lifekit-openclaw:local starts on it, or on $NEW_IMAGE when
# $RUN_UNADDRESSABLE is set (the box after the 2026-10-07 failure), and
# lifekit-openclaw:prev starts on $PREV_IMAGE when that is set. With
# $NO_PROJECT_GATEWAY only the platform project's legacy gateway exists.
DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CALL_LOG"
S="$IMG_STATE"
mkdir -p "$S"
key() { printf '%s' "$1" | tr '/:' '__'; }
if [ ! -e "$S/.init" ]; then
  if [ -n "$RUN_UNADDRESSABLE" ]; then echo "$NEW_IMAGE" > "$S/$(key lifekit-openclaw:local)"
  else echo "$RUN_IMAGE" > "$S/$(key lifekit-openclaw:local)"; fi
  [ -z "$PREV_IMAGE" ] || echo "$PREV_IMAGE" > "$S/$(key lifekit-openclaw:prev)"
  touch "$S/.init"
fi
resolve() {
  if [ -f "$S/$(key "$1")" ]; then cat "$S/$(key "$1")"; return 0; fi
  want="${1#sha256:}"
  for f in "$S"/*; do
    [ -f "$f" ] || continue
    id="$(cat "$f")"
    case "$want" in "$id"*) echo "$id"; return 0 ;; esac
  done
  return 1
}
case "$1" in
  tag)
    id="$(resolve "$2")" || { echo "Error response from daemon: No such image: $2" >&2; exit 1; }
    echo "$id" > "$S/$(key "$3")"
    exit 0 ;;
  rmi)
    id="$(resolve "$2")" || { echo "Error: No such image: $2" >&2; exit 1; }
    others="$(grep -l -x "$id" "$S"/* 2>/dev/null | grep -v -F "/$(key "$2")" || true)"
    if [ "$id" = "$RUN_IMAGE" ] && [ -z "$others" ] && [ -z "$NO_GATEWAY" ]; then
      echo "conflict: unable to delete $2 (must be forced) - container gw1 is using its referenced image" >&2
      exit 1
    fi
    rm -f "$S/$(key "$2")"
    exit 0 ;;
  images)
    resolve "$3" || true
    exit 0 ;;
  inspect)
    case " $* " in
      *" {{.Image}} "*) echo "sha256:${RUN_IMAGE}000000000000"; exit 0 ;;
      *" {{json .RepoTags}} "*)
        tags=""
        for f in "$S"/*; do
          [ -f "$f" ] || continue
          [ "$(cat "$f")" = "$3" ] && tags="$tags\\"$(basename "$f")\\","
        done
        echo "[${tags%,}]"; exit 0 ;;
    esac ;;
esac
case " $* " in
  *" build openclaw-gateway "*) echo "$NEW_IMAGE" > "$S/$(key lifekit-openclaw:local)" ;;
  *" ps -a -q openclaw-gateway "*) [ -n "$NO_GATEWAY$NO_PROJECT_GATEWAY" ] || echo gw1 ;;
  *" ps -a -q --filter ancestor="*)
    [ -n "$NO_GATEWAY" ] || [ -n "${*##*ancestor=$RUN_IMAGE*}" ] || echo gw1 ;;
  *" lifekit-openclaw:prev --version "*)
    [ -z "$PREV_VER" ] || echo "OpenClaw $PREV_VER (old)" ;;
  *" config --format json "*) echo '{"name":"proj"}' ;;
  " ps -a -q "*"label=com.docker.compose.project=proj "*)
    for pair in $LEGACY_CONTAINERS; do
      case " $* " in *" label=com.docker.compose.service=${pair%%=*} "*) echo "${pair#*=}" ;; esac
    done ;;
  *" network inspect "*) echo 172.30.0.1 ;;
  *" exec -T openclaw-gateway openclaw --version "*) echo "OpenClaw $RUNNING_VER (abc)" ;;
  *" lifekit-openclaw:local --version "*) echo "OpenClaw $BUILT_VER (def)" ;;
  *" plugins list --json "*)
    if [ -e "$PLUGINS_FIXED" ]; then v="$BUILT_VER"; else v="2026.6.8"; fi
    echo '[{"id":"codex","enabled":true,"origin":"global","version":"'"$v"'"}]' ;;
  *" plugins update "*) [ -n "$PLUGINS_STUCK" ] || touch "$PLUGINS_FIXED" ;;
esac
exit 0
"""

LOGGING_SH = """#!/bin/sh
echo "%s" >> "$DOCKER_CALL_LOG"
"""

PHASES = ["prepare", "modules", "build", "up", "configure", "post_up", "smoke"]

# Platform steps and OpenClaw phases in the order deploy.sh ran them before the
# split; `openclaw:<phase>` marks a phase call, the rest are platform steps.
DEPLOY_ORDER = [
    "openclaw:prepare",
    "embed-origin",
    "openclaw:modules",
    "render-heartbeat",
    "contract-static",
    "contract-static",
    "contract-static",
    "contract-static",
    "openclaw:build",
    "compose-up",
    "openclaw:up",
    "openclaw:configure",
    "prometheus-reload",
    "grafana-alerting-reload",
    "grafana-datasources-reload",
    "openclaw:post_up",
    "compose-ps",
    "compose-ps",
    "openclaw:smoke",
    "contract-enforce",
    "builder-gc",
    "tmp-scratch",
]


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)
    env_file = tmp_path / "stack.env"
    env_file.write_text("OPENCLAW_GATEWAY_TOKEN=t\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    call_log = tmp_path / "docker-calls.log"
    call_log.write_text("")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMPDIR": str(scratch),
        "DOCKER_CALL_LOG": str(call_log),
        "REPO_DIR": str(REPO),
        "ENV_FILE": str(env_file),
        # Absent: the uid-1000 ownership check then finds nothing to refuse.
        "OPENCLAW_CONFIG_DIR": str(tmp_path / "no-config"),
        "OPENCLAW_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "LIFEKIT_LIFE_DIR": str(tmp_path / "memory"),
        "LIFEKIT_STATE_DIR_HOST": str(tmp_path / "state"),
        "LEGACY_CONTAINERS": "",
        "RUNNING_VER": "2026.9.4",
        "BUILT_VER": "2026.9.5",
        "IMG_STATE": str(tmp_path / "images"),
        "RUN_IMAGE": "f6e77bf93da9",
        "NEW_IMAGE": "5ab1c2d3e4f5",
        "PREV_IMAGE": "",
        "PREV_VER": "",
        "NO_GATEWAY": "",
        "NO_PROJECT_GATEWAY": "",
        "RUN_UNADDRESSABLE": "",
        "PLUGINS_FIXED": str(tmp_path / "plugins-fixed"),
        "PLUGINS_STUCK": "",
    }


def stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def marker(line: str) -> str | None:
    if line.startswith(("openclaw:", "contract-")) or line in {
        "embed-origin",
        "render-heartbeat",
        "builder-gc",
        "tmp-scratch",
    }:
        return line
    if "up -d --build" in line:
        return "compose-up"
    if "kill -s HUP prometheus" in line:
        return "prometheus-reload"
    if line.endswith(" ps"):
        return "compose-ps"
    if "alerting/reload" in line:
        return "grafana-alerting-reload"
    if "datasources/reload" in line:
        return "grafana-datasources-reload"
    return None


def full_deploy(
    env, tmp_path, env_text="LIFEKIT_TELEGRAM_CHAT=123\n", identity=None, edge=None
):
    """Run deploy.sh end to end in a scratch repo; return (result, trace, repo).

    `identity` is the body of a stub scripts/deploy-identity.sh, which the
    deploy calls only once LOGTO_DB_PASSWORD is in the env file; `edge` the
    body of a stub scripts/deploy-edge.sh, called only once identity is up
    and the edge's three secrets are in it.
    """
    fake = tmp_path / "repo"
    (fake / "scripts/lib").mkdir(parents=True)
    shutil.copy(REPO / "scripts/deploy.sh", fake / "scripts/deploy.sh")
    shutil.copy(LIB, fake / "scripts/lib/deploy-common.sh")
    shutil.copy(PATHS_LIB, fake / "scripts/lib/openclaw-paths.sh")
    stub(
        fake / "scripts/deploy-openclaw.sh",
        "".join(
            f'openclaw_phase_{p}() {{ echo "openclaw:{p}" >> "$DOCKER_CALL_LOG"; }}\n'
            for p in PHASES
        ),
    )
    stub(fake / "scripts/deploy-embed-origin.sh", LOGGING_SH % "embed-origin")
    stub(fake / "scripts/deploy-grafana-oidc.sh", "#!/bin/sh\nexit 0\n")
    stub(fake / "scripts/render-heartbeat.sh", LOGGING_SH % "render-heartbeat")
    stub(fake / "scripts/docker-builder-gc.sh", LOGGING_SH % "builder-gc")
    stub(fake / "scripts/tmp-scratch-policy.sh", LOGGING_SH % "tmp-scratch")
    stub(
        fake / "scripts/platform-contract.py",
        "import os, sys\n"
        'open(os.environ["DOCKER_CALL_LOG"], "a").write("contract-" + sys.argv[1].lstrip("-") + "\\n")\n',
    )
    alert_dir = fake / "compose/observability/grafana/provisioning/alerting"
    alert_dir.mkdir(parents=True)
    (alert_dir / "contact-points.yml.tmpl").write_text("chat: __TELEGRAM_CHAT_ID__\n")
    (fake / "compose/docker-compose.yml").write_text("")
    bin_dir = tmp_path / "bin"
    stub(bin_dir / "git", '#!/bin/sh\necho "  HEAD branch: main"\n')
    stub(bin_dir / "curl", '#!/bin/sh\necho "curl $*" >> "$DOCKER_CALL_LOG"\n')
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    if identity is not None:
        stub(fake / "scripts/deploy-identity.sh", identity)
    if edge is not None:
        stub(fake / "scripts/deploy-edge.sh", edge)
    Path(env["ENV_FILE"]).write_text(env_text)

    r = subprocess.run(
        ["bash", str(fake / "scripts/deploy.sh")],
        env={
            **env,
            "REPO_DIR": str(fake),
            "LIFEKIT_DEPLOY_REEXEC": "1",
            "LIFEKIT_STATE_DIR_HOST": str(state_dir),
            "DOCKER_GID": "999",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return r, Path(env["DOCKER_CALL_LOG"]).read_text().splitlines(), fake


def test_deploy_runs_each_openclaw_phase_at_its_point_in_the_platform_sequence(
    env, tmp_path
):
    r, trace, fake = full_deploy(env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "✓ deploy complete." in r.stdout
    assert [m for m in map(marker, trace) if m] == DEPLOY_ORDER
    # The platform project no longer names, starts or manages OpenClaw's
    # services, and is never told to remove orphans.
    platform = [
        line for line in trace if f"-f {fake}/compose/docker-compose.yml" in line
    ]
    assert platform
    assert not [
        line
        for line in platform
        if any(svc in line.split() for svc in OPENCLAW_SERVICES)
        or "--remove-orphans" in line
    ]
    openclaw = [line for line in trace if " -p openclaw " in f" {line} "]
    assert openclaw == [
        f"compose -p openclaw --env-file {env['ENV_FILE']} "
        f"-f {fake}/compose/openclaw/docker-compose.yml {args}"
        for args in ("--profile * config --format json", "ps")
    ]
    # No LOGTO_DB_PASSWORD rendered yet: the identity project is declared
    # (static check) but never brought up, and the deploy stays green.
    identity = [line for line in trace if " -p identity " in f" {line} "]
    assert identity == [
        f"compose -p identity --env-file {env['ENV_FILE']} "
        f"-f {fake}/compose/identity/docker-compose.yml config --format json"
    ]
    assert "LOGTO_DB_PASSWORD is not in" in r.stderr
    # Nor the edge secrets: declared, never brought up.
    edge = [line for line in trace if " -p edge " in f" {line} "]
    assert edge == [
        f"compose -p edge --env-file {env['ENV_FILE']} "
        f"-f {fake}/compose/edge/docker-compose.yml config --format json"
    ]
    assert "(0/3); project edge not brought up" in r.stderr


def test_identity_oidc_network_is_created_before_the_platform_up(env, tmp_path):
    r, trace, _ = full_deploy(env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    create = trace.index("network inspect identity-oidc")
    up = next(i for i, line in enumerate(trace) if "up -d --build" in line)
    assert create < up


IDENTITY_STUB = """#!/bin/sh
echo "identity-$1" >> "$DOCKER_CALL_LOG"
if [ "$1" = endpoints ] && [ -z "$NO_ENDPOINTS" ]; then
  echo IDENTITY_ENDPOINT=https://box.example.ts.net:3001
  echo IDENTITY_ADMIN_ENDPOINT=https://box.example.ts.net:3002
fi
[ "$1" != check ] || [ -z "$CHECK_DRIFT" ]
"""


@pytest.mark.parametrize("drift", [False, True])
def test_identity_comes_up_once_its_password_is_rendered(env, tmp_path, drift):
    if drift:
        env["CHECK_DRIFT"] = "1"
    r, trace, fake = full_deploy(
        env,
        tmp_path,
        env_text="LIFEKIT_TELEGRAM_CHAT=123\nLOGTO_DB_PASSWORD=abc\n",
        identity=IDENTITY_STUB,
    )
    # Serve or sign-in-mode drift is report-only: a red line, never a red deploy.
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("✗ identity provider: not yet converged" in r.stderr) == drift
    prefix = (
        f"compose -p identity --env-file {env['ENV_FILE']} "
        f"-f {fake}/compose/identity/docker-compose.yml "
    )
    steps = [
        line[len(prefix) :]
        if line.startswith(prefix)
        else ("up -d --build" if "up -d --build" in line else line)
        for line in trace
        if line.startswith(prefix)
        or line.startswith(("identity-", "contract-enforce"))
        or "up -d --build" in line
    ]
    assert steps == [
        "config --format json",
        "up -d --build",
        "identity-endpoints",
        "up -d --wait --wait-timeout 300",
        "ps",
        "contract-enforce",
        "identity-check",
    ]
    assert "issuer https://box.example.ts.net:3001/oidc" in r.stdout


def test_identity_without_an_issuer_fails_the_deploy_and_stays_down(env, tmp_path):
    env["NO_ENDPOINTS"] = "1"
    r, trace, fake = full_deploy(
        env,
        tmp_path,
        env_text="LIFEKIT_TELEGRAM_CHAT=123\nLOGTO_DB_PASSWORD=abc\n",
        identity=IDENTITY_STUB,
    )
    assert r.returncode != 0
    assert "issuer URL undecidable" in r.stderr
    assert not [line for line in trace if " -p identity " in line and " up " in line]
    assert "identity-check" not in trace


EDGE_STUB = """#!/bin/sh
echo "edge-$1 IDENTITY_ENDPOINT=$IDENTITY_ENDPOINT" >> "$DOCKER_CALL_LOG"
if [ "$1" = domains ] && [ -z "$NO_DOMAINS" ]; then
  echo "EDGE_REDIRECT_DOMAINS=box.example.ts.net:*"
fi
[ "$1" != check ] || [ -z "$EDGE_DRIFT" ]
"""

EDGE_SECRETS = (
    "EDGE_OIDC_CLIENT_ID=id\nEDGE_OIDC_CLIENT_SECRET=s\nEDGE_COOKIE_SECRET='c'\n"
)


def edge_steps(trace, env, fake):
    prefix = (
        f"compose -p edge --env-file {env['ENV_FILE']} "
        f"-f {fake}/compose/edge/docker-compose.yml "
    )
    return [
        line[len(prefix) :] if line.startswith(prefix) else line
        for line in trace
        if line.startswith((prefix, "edge-", "identity-", "contract-enforce"))
    ]


@pytest.mark.parametrize("drift", [False, True])
def test_edge_comes_up_after_identity_once_its_secrets_are_rendered(
    env, tmp_path, drift
):
    if drift:
        env["EDGE_DRIFT"] = "1"
    r, trace, fake = full_deploy(
        env,
        tmp_path,
        env_text="LIFEKIT_TELEGRAM_CHAT=123\nLOGTO_DB_PASSWORD=abc\n" + EDGE_SECRETS,
        identity=IDENTITY_STUB,
        edge=EDGE_STUB,
    )
    # Serve not yet pointed at the gate is report-only: a red line, never a
    # red deploy.
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("✗ sign-in gate: not yet converged" in r.stderr) == drift
    issuer = "IDENTITY_ENDPOINT=https://box.example.ts.net:3001"
    assert edge_steps(trace, env, fake) == [
        "config --format json",
        "identity-endpoints",
        f"edge-domains {issuer}",
        "up -d --wait --wait-timeout 120",
        "ps",
        "contract-enforce",
        "identity-check",
        f"edge-check {issuer}",
    ]
    assert "redirects limited to box.example.ts.net:*" in r.stdout


@pytest.mark.parametrize(
    ("env_text", "expect"),
    [
        # Secrets rendered, but identity is not (no LOGTO_DB_PASSWORD).
        (EDGE_SECRETS, "the identity project is not up"),
        # Identity up, one edge secret missing.
        (
            "LOGTO_DB_PASSWORD=abc\nEDGE_OIDC_CLIENT_ID=id\nEDGE_COOKIE_SECRET=c\n"
            "EDGE_OIDC_CLIENT_SECRET=\n",
            "(2/3); project edge not brought up",
        ),
    ],
)
def test_edge_stays_down_until_identity_and_all_its_secrets(
    env, tmp_path, env_text, expect
):
    r, trace, fake = full_deploy(
        env,
        tmp_path,
        env_text="LIFEKIT_TELEGRAM_CHAT=123\n" + env_text,
        identity=IDENTITY_STUB,
        edge=EDGE_STUB,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert expect in r.stderr
    assert [
        s for s in edge_steps(trace, env, fake) if s.startswith(("edge-", "up "))
    ] == []


def test_edge_without_redirect_domains_fails_the_deploy_and_stays_down(env, tmp_path):
    env["NO_DOMAINS"] = "1"
    r, trace, fake = full_deploy(
        env,
        tmp_path,
        env_text="LIFEKIT_TELEGRAM_CHAT=123\nLOGTO_DB_PASSWORD=abc\n" + EDGE_SECRETS,
        identity=IDENTITY_STUB,
        edge=EDGE_STUB,
    )
    assert r.returncode != 0
    assert "redirect domains undecidable" in r.stderr
    steps = edge_steps(trace, env, fake)
    assert "up -d --wait --wait-timeout 120" not in steps
    assert not [s for s in steps if s.startswith("edge-check")]


def image_tags(env):
    """The stub image store's tags: {"repo:tag": image id}."""
    state = Path(env["IMG_STATE"])
    return {
        f.name.replace("lifekit-openclaw_", "lifekit-openclaw:"): f.read_text().strip()
        for f in state.iterdir()
        if f.is_file() and not f.name.startswith(".")
    }


def build_phase(env, extra_env=None):
    script = f"""
set -euo pipefail
source {LIB}
source {OPENCLAW}
rc=0
openclaw_phase_build || rc=$?
echo "rc=$rc"
echo "failures=${{#DEPLOY_FAILURES[@]}}"
for f in "${{DEPLOY_FAILURES[@]}}"; do echo "failure: $f"; done
"""
    return subprocess.run(
        ["bash", "-c", script],
        env={**env, **(extra_env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_on_version_bump_with_repinned_plugins_returns_zero_and_queues_nothing(
    env,
):
    r = build_phase(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0" in r.stdout.splitlines()
    assert "failures=0" in r.stdout.splitlines()
    assert "plugin codex is 2026.6.8, core is 2026.9.5; updating" in r.stderr
    calls = Path(env["DOCKER_CALL_LOG"]).read_text()
    assert "stop openclaw-cli openclaw-gateway" in calls
    assert "doctor --fix --non-interactive" in calls


def test_build_with_plugins_still_off_core_queues_a_failure_without_aborting(env):
    r = build_phase(env, {"PLUGINS_STUCK": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0" in r.stdout.splitlines()
    assert (
        "failure: plugins still off core 2026.9.5 after update: codex 2026.6.8"
        in r.stdout
    )


def test_build_keeps_the_running_image_as_prev_across_the_build_that_moves_local(env):
    # The 2026-10-07 failure: `compose build` re-tags :local to the new image,
    # and under the containerd store the running image is then unaddressable
    # by id. The pin tag taken before the build is what :prev is promoted from.
    r = build_phase(env)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()
    pin = next(
        i
        for i, c in enumerate(calls)
        if c == "tag sha256:f6e77bf93da9000000000000 lifekit-openclaw:pin"
    )
    build = next(
        i for i, c in enumerate(calls) if c.endswith(" build openclaw-gateway")
    )
    promote = calls.index("tag lifekit-openclaw:pin lifekit-openclaw:prev")
    stop = next(
        i for i, c in enumerate(calls) if "stop openclaw-cli openclaw-gateway" in c
    )
    assert pin < build < promote < stop
    assert image_tags(env) == {
        "lifekit-openclaw:local": env["NEW_IMAGE"],
        "lifekit-openclaw:prev": env["RUN_IMAGE"],
    }


def test_build_promotion_drops_the_image_the_old_prev_pointed_to(env):
    r = build_phase(env, {"PREV_IMAGE": "0ld0ld0ld0ld"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert image_tags(env)["lifekit-openclaw:prev"] == env["RUN_IMAGE"]
    assert "rmi 0ld0ld0ld0ld" in Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()


def test_build_without_a_version_change_leaves_no_pin_and_prev_alone(env):
    # A same-version rebuild that yields the same image id: :local still tags
    # it, so the pin goes. :prev is never retagged.
    r = build_phase(
        env,
        {
            "RUNNING_VER": "2026.9.5",
            "PREV_IMAGE": "0ld0ld0ld0ld",
            "NEW_IMAGE": "f6e77bf93da9",
        },
    )
    assert r.returncode == 0, r.stdout + r.stderr
    tags = image_tags(env)
    assert "lifekit-openclaw:pin" not in tags
    assert tags["lifekit-openclaw:prev"] == "0ld0ld0ld0ld"
    assert "lifekit-openclaw:prev" not in " ".join(
        c
        for c in Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()
        if c.startswith("tag ")
    )


def test_build_without_a_version_change_cannot_untag_the_image_a_container_uses(env):
    # A same-version rebuild with a new image id: the pin is the running
    # image's last tag, docker refuses to drop it, and the deploy goes on.
    # The next deploy removes it once the recreated gateway has moved off.
    r = build_phase(env, {"RUNNING_VER": "2026.9.5", "PREV_IMAGE": "0ld0ld0ld0ld"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0" in r.stdout.splitlines()
    tags = image_tags(env)
    assert tags["lifekit-openclaw:pin"] == env["RUN_IMAGE"]
    assert tags["lifekit-openclaw:prev"] == "0ld0ld0ld0ld"


def test_build_clears_a_stale_pin_before_pinning_again(env):
    state = Path(env["IMG_STATE"])
    state.mkdir()
    (state / "lifekit-openclaw_pin").write_text("57a1e57a1e57")
    r = build_phase(env)
    assert r.returncode == 0, r.stdout + r.stderr
    tags = image_tags(env)
    assert "lifekit-openclaw:pin" not in tags
    assert tags["lifekit-openclaw:prev"] == env["RUN_IMAGE"]


def test_build_stops_before_the_gateway_when_the_running_image_cannot_be_pinned(env):
    # The box after 2026-10-07: :local already holds the new build, so the
    # running image has no tag and `docker tag` cannot reach it. No rollback
    # image means no swap.
    r = build_phase(env, {"RUN_UNADDRESSABLE": "1"})
    assert r.returncode == 1, r.stdout + r.stderr  # `exit 1` leaves the shell
    assert "cannot be pinned" in r.stderr
    assert "gateway was not touched" in r.stderr
    calls = Path(env["DOCKER_CALL_LOG"]).read_text()
    assert " stop " not in calls
    assert "doctor --fix" not in calls
    assert "lifekit-openclaw:prev" not in " ".join(
        c for c in calls.splitlines() if c.startswith("tag ")
    )


def test_build_accepts_an_existing_prev_of_the_running_version_when_unpinnable(env):
    # The one-time recovery: the operator rebuilt the running version and
    # tagged it :prev; the deploy keeps it and carries on with the swap.
    r = build_phase(
        env,
        {
            "RUN_UNADDRESSABLE": "1",
            "PREV_IMAGE": "9a9a9a9a9a9a",
            "PREV_VER": "2026.9.4",
        },
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0" in r.stdout.splitlines()
    assert "keeping existing :prev (OpenClaw 2026.9.4)" in r.stdout
    assert image_tags(env)["lifekit-openclaw:prev"] == "9a9a9a9a9a9a"
    assert "doctor --fix --non-interactive" in Path(env["DOCKER_CALL_LOG"]).read_text()


def test_build_refuses_an_unpinnable_image_when_prev_is_another_version(env):
    r = build_phase(
        env,
        {
            "RUN_UNADDRESSABLE": "1",
            "PREV_IMAGE": "9a9a9a9a9a9a",
            "PREV_VER": "2026.9.3",
        },
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "prev is 2026.9.3, not 2026.9.4" in r.stderr
    assert "doctor --fix" not in Path(env["DOCKER_CALL_LOG"]).read_text()


def test_build_with_no_gateway_container_stops_on_a_version_change(env):
    # RUNNING_VER cannot come from nowhere in production; the stub reports one
    # regardless, which is what a bump with an unreadable container looks like.
    r = build_phase(env, {"NO_GATEWAY": "1"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "cannot be pinned" in r.stderr


def test_build_with_unchanged_version_migrates_nothing(env):
    r = build_phase(env, {"RUNNING_VER": "2026.9.5"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0" in r.stdout.splitlines()
    assert "failures=0" in r.stdout.splitlines()
    calls = Path(env["DOCKER_CALL_LOG"]).read_text()
    assert "doctor --fix" not in calls
    assert " stop " not in calls


def test_sourcing_defines_the_phases_and_runs_nothing(env):
    # Sourcing the lib a second time (deploy-openclaw.sh does) must not reset
    # a failure deploy.sh already queued.
    script = f"""
set -euo pipefail
source {LIB}
fail_later "queued before" 2>/dev/null
source {OPENCLAW}
declare -F | awk '{{print $3}}' | grep '^openclaw_phase_'
echo "failures=${{#DEPLOY_FAILURES[@]}}"
"""
    r = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr
    *defined, failures = r.stdout.split()
    assert sorted(defined) == sorted(f"openclaw_phase_{p}" for p in PHASES)
    assert failures == "failures=1"
    assert Path(env["DOCKER_CALL_LOG"]).read_text() == ""


def run_phases(env, phases, extra_env=None, tmp_path=None):
    script = f"""
set -euo pipefail
source {LIB}
source {OPENCLAW}
""" + "".join(f"openclaw_phase_{p}\n" for p in phases)
    return subprocess.run(
        ["bash", "-c", script],
        env={**env, **(extra_env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_openclaw_phase_drives_the_openclaw_project_and_file(env, tmp_path):
    # post_up waits 30s for Telegram; the stub makes it instant.
    stub(Path(env["PATH"].split(":")[0]) / "sleep", "#!/bin/sh\nexit 0\n")
    r = run_phases(env, [p for p in PHASES if p != "modules"])
    assert r.returncode == 0, r.stdout + r.stderr
    compose = [
        line
        for line in Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()
        if line.startswith("compose ")
    ]
    prefix = (
        f"compose -p openclaw --env-file {env['ENV_FILE']} "
        f"-f {REPO}/compose/openclaw/docker-compose.yml "
    )
    # The one platform call reads the platform project's name for the cutover
    # lookup (the build phase's version bump runs the cutover too).
    platform_name = (
        f"compose --env-file {env['ENV_FILE']} "
        f"-f {REPO}/compose/docker-compose.yml config --format json"
    )
    assert [line for line in compose if not line.startswith(prefix)] == [platform_name]
    ran = {line[len(prefix) :] for line in compose if line.startswith(prefix)}
    for args in (
        "build openclaw-gateway",
        "up -d",
        "exec -T openclaw-gateway openclaw secrets reload",
        "--profile cli run --rm -T openclaw-cli channels status",
        "--profile cli stop openclaw-cli openclaw-gateway",
    ):
        assert args in ran, args
    # onboard + the trustedProxies write go to the openclaw project too.
    assert any(
        a.endswith(
            "onboard --non-interactive --accept-risk --flow quickstart "
            "--mode local --auth-choice skip --gateway-auth token "
            "--gateway-token-ref-env OPENCLAW_GATEWAY_TOKEN "
            "--gateway-bind loopback --gateway-port 18789"
        )
        for a in ran
    )
    assert any("config set gateway.trustedProxies" in a for a in ran)
    assert (
        "network inspect openclaw_default --format {{(index .IPAM.Config 0).Gateway}}"
        in Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()
    )
    # Every path the phases wrote to is inside the test's tmp dir.
    assert (tmp_path / "workspace/memory-audit").is_dir()


def cutover_calls(env):
    return [
        line
        for line in Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()
        if line.split()[0] in {"stop", "rm", "ps"}
        or line.startswith("compose -p openclaw ")
        and " up " in f" {line} "
    ]


def test_cutover_removes_only_the_old_openclaw_containers_before_the_new_up(env):
    legacy = (
        "openclaw-cli=c1 openclaw-cli=c2 openclaw-gateway=g1 google-workspace-mcp=m1"
    )
    r = run_phases(env, ["up"], {"LEGACY_CONTAINERS": legacy})
    assert r.returncode == 0, r.stdout + r.stderr
    calls = cutover_calls(env)
    lookups = [c for c in calls if c.startswith("ps ")]
    assert lookups == [
        "ps -a -q --filter label=com.docker.compose.project=proj "
        f"--filter label=com.docker.compose.service={svc}"
        for svc in OPENCLAW_SERVICES
    ]
    assert [c for c in calls if not c.startswith("ps ")] == [
        "stop c1 c2",
        "rm c1 c2",
        "stop g1",
        "rm g1",
        "stop m1",
        "rm m1",
        f"compose -p openclaw --env-file {env['ENV_FILE']} "
        f"-f {REPO}/compose/openclaw/docker-compose.yml up -d",
    ]


def test_cutover_is_a_no_op_once_the_old_containers_are_gone(env):
    r = run_phases(env, ["up"])
    assert r.returncode == 0, r.stdout + r.stderr
    calls = cutover_calls(env)
    assert not [c for c in calls if c.split()[0] in {"stop", "rm"}]
    assert calls[-1].endswith("compose/openclaw/docker-compose.yml up -d")
    assert "cutover" not in r.stdout


def test_cutover_never_touches_the_openclaw_projects_own_containers(env, tmp_path):
    # A platform project that is itself named `openclaw` has no legacy copies.
    bin_dir = Path(env["PATH"].split(":")[0])
    (bin_dir / "docker").write_text(
        DOCKER_STUB.replace('{"name":"proj"}', '{"name":"openclaw"}')
    )
    r = run_phases(env, ["up"], {"LEGACY_CONTAINERS": "openclaw-gateway=g1"})
    assert r.returncode == 0, r.stdout + r.stderr
    calls = cutover_calls(env)
    assert not [c for c in calls if c.split()[0] in {"stop", "rm", "ps"}]


def test_version_bump_on_the_cutover_deploy_reads_and_stops_the_old_gateway(env):
    # The deploy that moves OpenClaw also bumps it: the running version comes
    # from the platform project's gateway, which is stopped before migrating.
    stub_path = Path(env["PATH"].split(":")[0]) / "docker"
    stub_path.write_text(
        DOCKER_STUB.replace(
            '  *" exec -T openclaw-gateway openclaw --version "*) echo "OpenClaw $RUNNING_VER (abc)" ;;\n',
            '  " exec g1 openclaw --version ") echo "OpenClaw $RUNNING_VER (abc)" ;;\n',
        )
    )
    r = build_phase(
        env,
        {"LEGACY_CONTAINERS": "openclaw-gateway=g1", "NO_PROJECT_GATEWAY": "1"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    calls = Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()
    assert "inspect --format {{.Image}} g1" in calls
    assert "tag lifekit-openclaw:pin lifekit-openclaw:prev" in calls
    assert image_tags(env)["lifekit-openclaw:prev"] == env["RUN_IMAGE"]
    assert calls.index("stop g1") < next(
        i for i, c in enumerate(calls) if "doctor --fix" in c
    )


# ─── the OpenClaw gate: scripts/lib/openclaw-paths.sh ────────────────────────

PATHS_LIB = REPO / "scripts/lib/openclaw-paths.sh"
GATE_REF = "refs/lifekit/last-deployed"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("compose/openclaw-gateway/Dockerfile", True),
        ("compose/openclaw-gateway/platform.patch.json", True),
        ("compose/openclaw/docker-compose.yml", True),
        ("defaults/modules.yaml", True),
        ("skills/notes/SKILL.md", True),
        ("platform.patch.json", True),
        ("elsewhere/platform.patch.json", True),
        ("scripts/deploy-openclaw.sh", True),
        ("scripts/deploy-trusted-proxies.sh", True),
        ("scripts/memory-audit/run.sh", True),
        ("secrets/lifekit-gateway.env.sops", True),
        ("secrets/lifekit.env.sops", False),
        ("compose/docker-compose.yml", False),
        ("compose/observability/prometheus/prometheus.yml", False),
        ("compose/notify-relay/server.js", False),
        ("scripts/deploy.sh", False),
        ("scripts/memory-audit-cron.sh", False),
        ("docs/runbook.md", False),
        ("README.md", False),
        (".github/workflows/ci.yml", False),
    ],
)
def test_openclaw_path_matches(path, expected):
    r = subprocess.run(
        ["bash", "-c", f"source {PATHS_LIB}; openclaw_path_matches '{path}'"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (r.returncode == 0) is expected


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    ).stdout.strip()


def commit(repo: Path, rel: str) -> str:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f.read_text() + "x\n" if f.exists() else "x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", rel)
    return git(repo, "rev-parse", "HEAD")


def decide(repo: Path, mode: str | None) -> tuple[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "LIFEKIT_DEPLOY_OPENCLAW"}
    if mode is not None:
        env["LIFEKIT_DEPLOY_OPENCLAW"] = mode
    r = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; source {PATHS_LIB}; openclaw_gate_decide; echo "$OPENCLAW_GATE_RUN|$OPENCLAW_GATE_REASON"',
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    run, reason = r.stdout.strip().split("|", 1)
    return run, reason


@pytest.fixture
def gate_repo(tmp_path):
    repo = tmp_path / "gate-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    base = commit(repo, "README.md")
    git(repo, "update-ref", GATE_REF, base)
    return repo


def test_gate_default_mode_runs_everything(gate_repo):
    commit(gate_repo, "docs/runbook.md")
    assert decide(gate_repo, None)[0] == "1"
    assert decide(gate_repo, "always")[0] == "1"


def test_gate_auto_skips_when_only_platform_paths_changed(gate_repo):
    commit(gate_repo, "compose/observability/prometheus/prometheus.yml")
    commit(gate_repo, "docs/runbook.md")
    run, reason = decide(gate_repo, "auto")
    assert run == "0"
    assert "no OpenClaw path changed" in reason


def test_gate_auto_runs_when_any_commit_in_the_range_touched_openclaw(gate_repo):
    # A superseded run never advanced the ref: its OpenClaw change is still in
    # the range even though the newest commit is platform-only.
    commit(gate_repo, "skills/notes/SKILL.md")
    commit(gate_repo, "docs/runbook.md")
    run, reason = decide(gate_repo, "auto")
    assert run == "1"
    assert "skills/notes/SKILL.md" in reason


def test_gate_auto_runs_when_nothing_is_recorded(gate_repo):
    git(gate_repo, "update-ref", "-d", GATE_REF)
    run, reason = decide(gate_repo, "auto")
    assert run == "1"
    assert "no record" in reason


def test_gate_auto_runs_when_last_deployed_is_not_an_ancestor(gate_repo):
    git(gate_repo, "checkout", "-q", "-b", "other")
    git(gate_repo, "update-ref", GATE_REF, commit(gate_repo, "docs/other.md"))
    git(gate_repo, "checkout", "-q", "main")
    commit(gate_repo, "docs/runbook.md")
    run, reason = decide(gate_repo, "auto")
    assert run == "1"
    assert "not an ancestor" in reason


def test_gate_auto_runs_when_the_recorded_commit_is_missing(gate_repo):
    ref = gate_repo / ".git" / GATE_REF
    ref.write_text("0" * 39 + "1\n")
    assert decide(gate_repo, "auto")[0] == "1"


def test_gate_unknown_mode_runs_everything(gate_repo):
    run, reason = decide(gate_repo, "bogus")
    assert run == "1"
    assert "unknown" in reason


# ─── deploy.sh with the gate on / off ────────────────────────────────────────

PLATFORM_ORDER = [m for m in DEPLOY_ORDER if not m.startswith("openclaw:")]


def run_gated_deploy(env, tmp_path, mode, touched):
    """deploy.sh against a scratch git repo whose last commit touched `touched`."""
    fake = tmp_path / "repo"
    (fake / "scripts/lib").mkdir(parents=True)
    shutil.copy(REPO / "scripts/deploy.sh", fake / "scripts/deploy.sh")
    shutil.copy(LIB, fake / "scripts/lib/deploy-common.sh")
    shutil.copy(PATHS_LIB, fake / "scripts/lib/openclaw-paths.sh")
    stub(
        fake / "scripts/deploy-openclaw.sh",
        "".join(
            f'openclaw_phase_{p}() {{ echo "openclaw:{p}" >> "$DOCKER_CALL_LOG"; }}\n'
            for p in PHASES
        ),
    )
    stub(fake / "scripts/deploy-embed-origin.sh", LOGGING_SH % "embed-origin")
    stub(fake / "scripts/deploy-grafana-oidc.sh", "#!/bin/sh\nexit 0\n")
    stub(fake / "scripts/render-heartbeat.sh", LOGGING_SH % "render-heartbeat")
    stub(fake / "scripts/docker-builder-gc.sh", LOGGING_SH % "builder-gc")
    stub(fake / "scripts/tmp-scratch-policy.sh", LOGGING_SH % "tmp-scratch")
    stub(
        fake / "scripts/platform-contract.py",
        "import os, sys\n"
        'open(os.environ["DOCKER_CALL_LOG"], "a").write("contract-" + sys.argv[1].lstrip("-") + "\\n")\n',
    )
    alert_dir = fake / "compose/observability/grafana/provisioning/alerting"
    alert_dir.mkdir(parents=True)
    (alert_dir / "contact-points.yml.tmpl").write_text("chat: __TELEGRAM_CHAT_ID__\n")
    (fake / "compose/docker-compose.yml").write_text("")
    (fake / ".gitignore").write_text("contact-points.yml\n")
    git(fake, "init", "-q", "-b", "main")
    base = commit(fake, "README.md")
    git(fake, "update-ref", GATE_REF, base)
    commit(fake, touched)
    head = git(fake, "rev-parse", "HEAD")
    bin_dir = tmp_path / "bin"
    real_git = shutil.which("git")
    # fetch/reset/remote would need a real origin; everything else is real git.
    stub(
        bin_dir / "git",
        f"""#!/bin/sh
case "$1" in
  remote) echo "  HEAD branch: main" ;;
  fetch|reset) ;;
  *) exec {real_git} "$@" ;;
esac
""",
    )
    stub(bin_dir / "curl", '#!/bin/sh\necho "curl $*" >> "$DOCKER_CALL_LOG"\n')
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    Path(env["ENV_FILE"]).write_text("LIFEKIT_TELEGRAM_CHAT=123\n")
    r = subprocess.run(
        ["bash", str(fake / "scripts/deploy.sh")],
        env={
            **env,
            "REPO_DIR": str(fake),
            "LIFEKIT_DEPLOY_REEXEC": "1",
            "LIFEKIT_DEPLOY_OPENCLAW": mode,
            "LIFEKIT_STATE_DIR_HOST": str(state_dir),
            "DOCKER_GID": "999",
        },
        cwd=fake,
        capture_output=True,
        text=True,
        check=False,
    )
    return r, fake, head


def test_gate_off_skips_every_openclaw_phase_and_leaves_the_gateway_out_of_up(
    env, tmp_path
):
    r, fake, head = run_gated_deploy(
        env, tmp_path, "auto", "compose/observability/prometheus/prometheus.yml"
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OpenClaw phases: SKIPPED" in r.stdout
    trace = Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()
    assert [m for m in map(marker, trace) if m] == PLATFORM_ORDER
    assert not [line for line in trace if line.startswith("openclaw:")]
    # The platform project defines no OpenClaw service, so its plain `up` cannot
    # touch the gateway, and nothing brings the openclaw project up.
    up = [line for line in trace if " up " in line]
    assert len(up) == 1 and up[0].endswith(
        f"-f {fake}/compose/docker-compose.yml up -d --build"
    )
    assert git(fake, "rev-parse", GATE_REF) == head


def test_gate_on_runs_every_openclaw_phase_in_order(env, tmp_path):
    r, fake, head = run_gated_deploy(env, tmp_path, "auto", "defaults/modules.yaml")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OpenClaw phases: RUN (defaults/modules.yaml changed" in r.stdout
    trace = Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()
    assert [m for m in map(marker, trace) if m] == DEPLOY_ORDER
    # Every service is named implicitly: no service list on the up.
    assert [line for line in trace if "up -d --build" in line][0].endswith(
        "up -d --build"
    )
    assert git(fake, "rev-parse", GATE_REF) == head


def test_gate_always_runs_every_phase_even_for_a_platform_only_change(env, tmp_path):
    r, _, _ = run_gated_deploy(env, tmp_path, "always", "docs/runbook.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OpenClaw phases: RUN (mode 'always'" in r.stdout
    trace = Path(env["DOCKER_CALL_LOG"]).read_text().splitlines()
    assert [m for m in map(marker, trace) if m] == DEPLOY_ORDER
