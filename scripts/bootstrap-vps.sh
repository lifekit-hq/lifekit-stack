#!/usr/bin/env bash
# bootstrap-vps.sh — one-shot host setup. Run this once on a fresh Hetzner VPS.
#
# Idempotent: re-running it is safe. Each step is "install if missing" or
# "ensure-state" — nothing destructive.
#
# Usage on the VPS as root (after SSHing in fresh):
#
#   curl -fsSL https://raw.githubusercontent.com/lifekit-hq/lifekit-stack/main/scripts/bootstrap-vps.sh | \
#     sudo TAILSCALE_AUTH_KEY=tskey-auth-... TAILSCALE_HOSTNAME=lifekit-vps bash
#
# Or clone the repo first and run:
#   sudo TAILSCALE_AUTH_KEY=... TAILSCALE_HOSTNAME=lifekit-vps ./scripts/bootstrap-vps.sh
#
# Optional: set RUNNER_REG_TOKEN to also install the GitHub Actions
# self-hosted runner. Fetch a fresh 1-hour token with:
#   gh api -X POST /repos/lifekit-hq/lifekit-stack/actions/runners/registration-token --jq .token
#
# After this script: scp your .env to /srv/openclaw/config/.env, then run ./scripts/deploy.sh.

set -euo pipefail

# ─── Required env ─────────────────────────────────────────────────────────────

: "${TAILSCALE_AUTH_KEY:?set TAILSCALE_AUTH_KEY=tskey-auth-... before running}"
: "${TAILSCALE_HOSTNAME:?set TAILSCALE_HOSTNAME=<name> before running}"

LIFEKIT_USER="${LIFEKIT_USER:-lifekit}"
LIFEKIT_UID="${LIFEKIT_UID:-1000}"
ADMIN_USER="${ADMIN_USER:-denys}"          # the sudo human account that also holds the Claude credentials (README "VPS users")
REPO_URL="${REPO_URL:-https://github.com/lifekit-hq/lifekit-stack.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_DIR="${REPO_DIR:-/srv/lifekit-stack}"

# GitHub Actions self-hosted runner — optional. Skip the install block if
# RUNNER_REG_TOKEN is unset (operator can configure manually later).
RUNNER_REG_TOKEN="${RUNNER_REG_TOKEN:-}"
RUNNER_VERSION="${RUNNER_VERSION:-2.334.0}"
RUNNER_NAME="${RUNNER_NAME:-${TAILSCALE_HOSTNAME}-netcup}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,Linux,ARM64,arm64}"
RUNNER_REPO_URL="${RUNNER_REPO_URL:-${REPO_URL%.git}}"
RUNNER_DIR="${RUNNER_DIR:-/home/${LIFEKIT_USER}/actions-runner}"

# ─── Helpers ──────────────────────────────────────────────────────────────────

say() { printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ─── Sanity ───────────────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

# ─── Base packages ────────────────────────────────────────────────────────────

say "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg ufw git rsync sshfs python3 python3-venv gh

# ─── Swap (4G) ────────────────────────────────────────────────────────────────
# The 2026-05-20 cax11 freeze postmortem flagged "zero swap" as one of three
# failure amplifiers. 4G gives the OOM killer breathing room on small VPS
# plans (Hetzner cax11, Netcup VPS 2000 ARM G11). Idempotent: skipped if
# /swapfile is already active.

if swapon --show=NAME --noheadings | grep -qx /swapfile; then
  say "Swap already active on /swapfile, skipping"
else
  say "Creating 4G /swapfile"
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  if ! grep -qE '^/swapfile[[:space:]]' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
fi

# ─── lifekit user ─────────────────────────────────────────────────────────────

if ! id -u "$LIFEKIT_USER" >/dev/null 2>&1; then
  say "Creating user $LIFEKIT_USER (uid $LIFEKIT_UID)"
  useradd --create-home --uid "$LIFEKIT_UID" --shell /bin/bash "$LIFEKIT_USER"
else
  say "User $LIFEKIT_USER already exists, skipping"
fi

# ─── Docker ───────────────────────────────────────────────────────────────────

if have docker; then
  say "Docker already installed: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
else
  say "Installing Docker via get.docker.com"
  curl -fsSL https://get.docker.com | sh
fi

systemctl enable --now docker
usermod -aG docker "$LIFEKIT_USER"

# ─── Tailscale ────────────────────────────────────────────────────────────────

if have tailscale; then
  say "Tailscale already installed: $(tailscale version | head -1)"
else
  say "Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

say "Joining tailnet as $TAILSCALE_HOSTNAME"
tailscale up --authkey="$TAILSCALE_AUTH_KEY" --hostname="$TAILSCALE_HOSTNAME"

# ─── UFW: deny everything public, allow SSH only on tailscale0 ────────────────

say "Configuring UFW (Tailscale-only SSH)"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow in on tailscale0 to any port 22 proto tcp
ufw --force enable

# ─── Bind-mount directories ───────────────────────────────────────────────────

say "Creating /srv/{lifekit-stack,life,openclaw/*} + /var/lib/lifekit"
install -d -o "$LIFEKIT_USER" -g "$LIFEKIT_USER" -m 0750 \
  "$REPO_DIR" \
  /srv/memory \
  /srv/openclaw \
  /srv/openclaw/config \
  /srv/openclaw/workspace \
  /srv/openclaw/secret-key

# Runtime-state dir — split from /srv/memory per proposal
# 2026-05-27-runtime-knowledge-split. Holds orchestrator.sqlite, queue.jsonl,
# .curator-proposed/, .last_consolidation, flat-bucket tasks/, intake_index.json.
install -d -o "$LIFEKIT_USER" -g "$LIFEKIT_USER" -m 0750 \
  /var/lib/lifekit \
  /var/lib/lifekit/tasks \
  /var/lib/lifekit/.curator-proposed

# ─── Clone the stack repo ─────────────────────────────────────────────────────

if [[ -d "$REPO_DIR/.git" ]]; then
  say "Repo already cloned at $REPO_DIR, pulling latest"
  sudo -u "$LIFEKIT_USER" git -C "$REPO_DIR" pull --ff-only
else
  say "Cloning $REPO_URL → $REPO_DIR"
  sudo -u "$LIFEKIT_USER" git clone --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
fi

# (The lifekit-dashboard auto-redeploy timer was retired 2026-08-16 — the
# dashboard deploys from its own repo's workflow now; ecosystem decoupling
# slice 2. Removal from an already-bootstrapped host:
#   systemctl disable --now lifekit-dashboard-redeploy.timer
#   rm -f /etc/systemd/system/lifekit-dashboard-redeploy.{timer,service} \
#         /usr/local/bin/lifekit-dashboard-redeploy.sh && systemctl daemon-reload)

# ─── Docker builder cache GC cap ──────────────────────────────────────────────
# Caps BuildKit's on-box build cache at the daemon level instead of a periodic
# prune job (postmortem: 179 GB of unbounded build cache from on-box GHA
# runner builds). Only writes /etc/docker/daemon.json — it never restarts
# dockerd, so it's safe to run against an already-running Docker; applying
# the change needs a deliberate `systemctl restart docker` (see the notice
# the script prints).

say "Configuring Docker builder cache GC cap"
bash "$REPO_DIR/scripts/docker-builder-gc.sh"

# ─── openclaw-config sync timer ───────────────────────────────────────────────

say "Installing openclaw-config sync script + systemd units"
install -m 755 "$REPO_DIR/scripts/sync/openclaw-config-sync.sh" \
  /usr/local/bin/openclaw-config-sync.sh
install -m 644 "$REPO_DIR/scripts/sync/openclaw-config-sync.service" \
  /etc/systemd/system/openclaw-config-sync.service
install -m 644 "$REPO_DIR/scripts/sync/openclaw-config-sync.timer" \
  /etc/systemd/system/openclaw-config-sync.timer
systemctl daemon-reload
systemctl enable --now openclaw-config-sync.timer

# ─── memory-store rotation timer ──────────────────────────────────────────────
# Monthly rotation of the /srv/memory memory store + OpenClaw agent trajectories
# (logrotate + rotate-extras.py, no LLM). The rotate-extras.py + logrotate-memory.conf
# scripts live in the dsdevq/life repo at /srv/memory/system/ (delivered by memory-sync);
# this only installs the host units + wrapper. Runtime state -> /var/lib/lifekit/rotation.

say "Installing memory-store rotation script + systemd units"
install -d -o "$LIFEKIT_USER" -g "$LIFEKIT_USER" -m 750 /var/lib/lifekit/rotation
install -m 755 "$REPO_DIR/scripts/rotate/memory-rotate.sh" \
  /usr/local/bin/memory-rotate.sh
install -m 644 "$REPO_DIR/scripts/rotate/memory-rotate.service" \
  /etc/systemd/system/memory-rotate.service
install -m 644 "$REPO_DIR/scripts/rotate/memory-rotate.timer" \
  /etc/systemd/system/memory-rotate.timer
systemctl daemon-reload
systemctl enable --now memory-rotate.timer

# ─── Claude quota gauge timer ─────────────────────────────────────────────────
# Every 5 minutes, write the shared Claude account's remaining quota per window
# (quota-axi --json) into node-exporter's textfile collector directory; Grafana's
# claude-quota-* rules alert on it. Runs as ADMIN_USER because that account holds
# the Claude credentials and the nvm-installed quota-axi. The directory must be
# owned by that account and world-readable (node-exporter reads it as nobody).

say "Installing Claude quota gauge script + systemd units"
install -d -o "$ADMIN_USER" -g "$ADMIN_USER" -m 755 /var/lib/node_exporter/textfile
install -m 755 "$REPO_DIR/scripts/quota-gauge/claude-quota-gauge.sh" \
  /usr/local/bin/claude-quota-gauge.sh
sed "s/__ADMIN_USER__/${ADMIN_USER}/" \
  "$REPO_DIR/scripts/quota-gauge/claude-quota-gauge.service" \
  > /etc/systemd/system/claude-quota-gauge.service
install -m 644 "$REPO_DIR/scripts/quota-gauge/claude-quota-gauge.timer" \
  /etc/systemd/system/claude-quota-gauge.timer
systemctl daemon-reload
systemctl enable --now claude-quota-gauge.timer

# ─── GitHub Actions self-hosted runner ────────────────────────────────────────

if [[ -n "$RUNNER_REG_TOKEN" ]]; then
  if systemctl list-units --type=service --no-pager 2>/dev/null | grep -q '^actions.runner.'; then
    say "GitHub Actions runner already installed; skipping (use sudo ./svc.sh uninstall to redo)"
  else
    say "Installing GitHub Actions self-hosted runner $RUNNER_NAME (v$RUNNER_VERSION)"
    mkdir -p "$RUNNER_DIR"
    chown "$LIFEKIT_USER:$LIFEKIT_USER" "$RUNNER_DIR"
    tarball="actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz"
    if [[ ! -f "$RUNNER_DIR/$tarball" ]]; then
      sudo -u "$LIFEKIT_USER" curl -fsSL \
        -o "$RUNNER_DIR/$tarball" \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${tarball}"
    fi
    if [[ ! -x "$RUNNER_DIR/config.sh" ]]; then
      sudo -u "$LIFEKIT_USER" tar -C "$RUNNER_DIR" -xzf "$RUNNER_DIR/$tarball"
    fi
    # libicu76 etc. — bundled .NET runtime needs them.
    bash "$RUNNER_DIR/bin/installdependencies.sh"
    sudo -u "$LIFEKIT_USER" bash -c "cd '$RUNNER_DIR' && ./config.sh \
      --url '$RUNNER_REPO_URL' \
      --token '$RUNNER_REG_TOKEN' \
      --name '$RUNNER_NAME' \
      --labels '$RUNNER_LABELS' \
      --work _work \
      --unattended --replace"
    ( cd "$RUNNER_DIR" && ./svc.sh install "$LIFEKIT_USER" && ./svc.sh start )
  fi
else
  say "RUNNER_REG_TOKEN not set; skipping Actions runner install (fetch a token with: gh api -X POST /repos/<owner>/<repo>/actions/runners/registration-token --jq .token, then re-run this script)"
fi

# ─── Done ─────────────────────────────────────────────────────────────────────

say "Host bootstrap complete."
cat <<EOF

Next steps:
  1. From your laptop, scp your real .env onto the host:
       scp .env $LIFEKIT_USER@$TAILSCALE_HOSTNAME:/srv/openclaw/config/.env
  2. Optional: copy your private workspace skills onto the host:
       rsync -a ~/.openclaw/workspace/skills/ $LIFEKIT_USER@$TAILSCALE_HOSTNAME:/srv/openclaw/workspace/skills/
  3. Run the deploy script (on the host, as $LIFEKIT_USER):
       cd $REPO_DIR && ./scripts/deploy.sh
  4. rsync your ~/memory/ data:
       rsync -a ~/memory/ $LIFEKIT_USER@$TAILSCALE_HOSTNAME:/srv/memory/

EOF
