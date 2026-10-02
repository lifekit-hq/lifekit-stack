#!/usr/bin/env bash
# host-firewall.sh — the host firewall baseline: one nftables table
# (inet lifekit, scripts/firewall/lifekit-firewall.nft) loaded at boot by
# lifekit-firewall.service. Input policy drop; the tailnet, Tailscale's
# WireGuard port, tcp/80+443 for the edge and essential ICMP stay open; SSH is
# tailnet-only. Published container ports answer only loopback, the tailnet and
# the edge bridge. The ruleset file documents each rule and how it composes
# with Docker's and Tailscale's own iptables-nft chains.
#
# Why its own unit and not Debian's nftables.service: that unit loads
# /etc/nftables.conf, whose stock content starts with `flush ruleset`, and its
# ExecStop is `nft flush ruleset`. Either one wipes Docker's NAT and filter
# tables (every published port and every container's egress) until dockerd is
# restarted. This unit only ever loads or deletes its own table. --check flags
# nftables.service when it is enabled, and an active ufw (its default-deny
# would block the edge ports this table opens).
#
# Modes:
#   (default)  install the ruleset copy and the unit, enable it, and load the
#              ruleset now (root; bootstrap-vps.sh runs it on a fresh box,
#              where the bootstrap's own SSH session survives as an
#              established flow). Idempotent: a reload replaces the table
#              atomically. On a box that is already in service, apply through
#              --trial and --confirm instead.
#   --check    read-only, no root: exit 0 only when the installed ruleset and
#              unit match the repository, the unit is enabled and active, and
#              neither nftables.service nor ufw is enabled; 1 on any
#              mismatch. deploy.sh runs it after `up` as a report-only line.
#   --trial [SECONDS]
#              root, the cutover on a live box: record the current ruleset and
#              iptables state, write a rollback file that restores this table's
#              previous state (none, on a first cutover), schedule that
#              rollback as a transient systemd timer SECONDS from now (default
#              300), then load the repository ruleset live. Nothing is
#              persisted: unconfirmed, the timer restores the previous state,
#              and a reboot does too. Refuses unless the calling SSH session
#              comes over the tailnet (FIREWALL_TRIAL_ANY_SESSION=1 skips that).
#   --confirm  root: cancel the pending rollback, then run the default mode.
#              Refuses when no rollback is pending (it already fired, or no
#              trial ran), so a lapsed trial is never persisted.
#   --rollback root: cancel the pending rollback and apply it now. Refuses
#              when none is pending; a confirmed firewall is turned off with
#              `systemctl disable --now lifekit-firewall.service`, whose stop
#              deletes the table.
#
# Env overrides (mainly for testing — point these at temp paths to dry-run
# without touching the real host):
#   FIREWALL_RULESET    installed ruleset (default /etc/lifekit/firewall.nft)
#   FIREWALL_UNIT_DIR   where the unit goes (default /etc/systemd/system)
#   FIREWALL_STATE_DIR  baselines and the rollback file (default /var/lib/lifekit/firewall)
#   NFT_BIN             nft as the unit and the rollback timer call it (default /usr/sbin/nft)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)"
SOURCE_RULESET="${REPO_DIR}/scripts/firewall/lifekit-firewall.nft"
FIREWALL_RULESET="${FIREWALL_RULESET:-/etc/lifekit/firewall.nft}"
FIREWALL_UNIT_DIR="${FIREWALL_UNIT_DIR:-/etc/systemd/system}"
FIREWALL_STATE_DIR="${FIREWALL_STATE_DIR:-/var/lib/lifekit/firewall}"
NFT_BIN="${NFT_BIN:-/usr/sbin/nft}"
UNIT=lifekit-firewall
ROLLBACK_UNIT=lifekit-firewall-rollback
ROLLBACK_FILE="${FIREWALL_STATE_DIR}/rollback.nft"
TABLE="inet lifekit"

say() { printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

want_ruleset() { cat "${SOURCE_RULESET}"; }

want_service() {
  cat <<EOF
# Managed by lifekit-stack scripts/host-firewall.sh — do not hand-edit.
# Loads and deletes only the nftables table ${TABLE}; never flushes the
# ruleset that Docker and tailscaled share.
[Unit]
Description=lifekit host firewall (nftables table ${TABLE})
DefaultDependencies=no
Wants=network-pre.target
Before=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${NFT_BIN} -f ${FIREWALL_RULESET}
ExecReload=${NFT_BIN} -f ${FIREWALL_RULESET}
ExecStop=-${NFT_BIN} delete table ${TABLE}

[Install]
WantedBy=sysinit.target
EOF
}

MANAGED=(
  "${FIREWALL_RULESET}:want_ruleset"
  "${FIREWALL_UNIT_DIR}/${UNIT}.service:want_service"
)

matches() { [[ -f "$1" ]] && diff -q <("$2") "$1" >/dev/null 2>&1; }

check() {
  local mismatch=0 item path gen state
  for item in "${MANAGED[@]}"; do
    path="${item%:*}" gen="${item##*:}"
    if matches "${path}" "${gen}"; then
      echo "  ${path}: matches"
    elif [[ -f "${path}" ]]; then
      echo "  ${path}: present but does not match the repository content"
      mismatch=1
    else
      echo "  ${path}: absent"
      mismatch=1
    fi
  done
  state="$(systemctl is-enabled "${UNIT}.service" 2>&1 || true)"
  echo "  ${UNIT}.service: ${state}"
  [[ "${state}" == "enabled" ]] || mismatch=1
  state="$(systemctl is-active "${UNIT}.service" 2>&1 || true)"
  echo "  ${UNIT}.service: ${state}"
  [[ "${state}" == "active" ]] || mismatch=1
  state="$(systemctl is-enabled nftables.service 2>&1 || true)"
  if [[ "${state}" == "enabled" ]]; then
    echo "  nftables.service: enabled — its flush-ruleset load and stop would wipe Docker's tables; disable it"
    mismatch=1
  fi
  state="$(systemctl is-active ufw.service 2>&1 || true)"
  if [[ "${state}" == "active" ]]; then
    echo "  ufw.service: active — a second default-deny firewall; this repository's ruleset replaces it"
    mismatch=1
  fi
  return "${mismatch}"
}

install_and_load() {
  local item path gen tmp unit_changed=0
  "${NFT_BIN}" -c -f "${SOURCE_RULESET}"
  for item in "${MANAGED[@]}"; do
    path="${item%:*}" gen="${item##*:}"
    if matches "${path}" "${gen}"; then
      say "${path} already matches, skipping"
      continue
    fi
    install -d -m 755 "$(dirname "${path}")"
    tmp="$(mktemp "${path}.XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    "${gen}" >"$tmp"
    chmod 644 "$tmp"
    mv "$tmp" "${path}"
    trap - EXIT
    say "Wrote ${path}"
    [[ "${path}" != "${FIREWALL_UNIT_DIR}"/* ]] || unit_changed=1
  done
  if [[ "${unit_changed}" == 1 ]]; then
    systemctl daemon-reload
  fi
  systemctl enable "${UNIT}.service"
  # Active: ExecReload replaces the table atomically. Inactive: start loads it.
  systemctl reload-or-restart "${UNIT}.service"
  say "Loaded ${TABLE} from ${FIREWALL_RULESET}; ${UNIT}.service enabled"
}

rollback_pending() {
  [[ "$(systemctl is-active "${ROLLBACK_UNIT}.timer" 2>&1 || true)" == "active" ]]
}

cancel_rollback() {
  systemctl stop "${ROLLBACK_UNIT}.timer" 2>/dev/null || true
}

# The remote host of the session that ran sudo, as utmp records it for the
# controlling tty: an address over SSH, empty on the console, something else
# inside tmux or screen.
session_client() { who -m 2>/dev/null | sed -n 's/.*(\(.*\))[[:space:]]*$/\1/p' | head -n1; }

# 0: a tailnet address (100.64.0.0/10, fd7a:115c:a1e0::/48). 1: another IP
# address. 2: not an address, so nothing is known.
classify_addr() {
  local addr="${1,,}"
  if [[ "${addr}" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
    (( BASH_REMATCH[1] == 100 && BASH_REMATCH[2] >= 64 && BASH_REMATCH[2] <= 127 )) && return 0
    return 1
  fi
  [[ "${addr}" == fd7a:115c:a1e0:* ]] && return 0
  [[ "${addr}" =~ ^[0-9a-f:]+$ && "${addr}" == *:*:* ]] && return 1
  return 2
}

trial() {
  local seconds="${1:-300}" client kind stamp baseline
  if ! [[ "${seconds}" =~ ^[0-9]+$ ]] || (( seconds < 60 )); then
    die "--trial takes a rollback delay in seconds, at least 60 (got '${seconds}')"
  fi

  client="$(session_client)" kind=0
  classify_addr "${client}" || kind=$?
  if [[ "${kind}" == 1 && "${FIREWALL_TRIAL_ANY_SESSION:-}" != 1 ]]; then
    die "this session comes from ${client}, not the tailnet: the ruleset closes public SSH. Reconnect over the tailnet and run again"
  fi
  if [[ "${kind}" == 2 ]]; then
    echo "  (this tty's remote host is '${client}', not an address — make sure you are on the tailnet or the provider console)"
  fi

  rollback_pending && die "a trial is already pending: --confirm or --rollback it first"

  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  baseline="${FIREWALL_STATE_DIR}/baseline-${stamp}"
  install -d -m 700 "${FIREWALL_STATE_DIR}" "${baseline}"
  say "Recording the current state in ${baseline}"
  "${NFT_BIN}" list ruleset >"${baseline}/nft-ruleset.txt"
  local cmd table
  for cmd in iptables ip6tables; do
    for table in filter nat; do
      "${cmd}" -t "${table}" -S >"${baseline}/${cmd}-${table}.txt" 2>&1 ||
        echo "  ${cmd} -t ${table} -S failed, see ${baseline}/${cmd}-${table}.txt"
    done
  done
  ls -l "${baseline}"

  say "Checking ${SOURCE_RULESET}"
  "${NFT_BIN}" -c -f "${SOURCE_RULESET}"

  # Declaring then deleting the table removes whatever this ruleset loads
  # (and is a no-op when nothing was loaded); a previously loaded version
  # follows it, so the rollback lands exactly on the state before the trial.
  {
    printf 'table %s\ndelete table %s\n' "${TABLE}" "${TABLE}"
    "${NFT_BIN}" -s list table inet lifekit 2>/dev/null || true
  } >"${ROLLBACK_FILE}.tmp"
  "${NFT_BIN}" -c -f "${ROLLBACK_FILE}.tmp"
  mv "${ROLLBACK_FILE}.tmp" "${ROLLBACK_FILE}"
  cp "${ROLLBACK_FILE}" "${baseline}/rollback.nft"

  say "Scheduling the automatic rollback in ${seconds}s"
  systemd-run --collect --unit="${ROLLBACK_UNIT}" --on-active="${seconds}s" \
    --timer-property=AccuracySec=1s "${NFT_BIN}" -f "${ROLLBACK_FILE}"

  say "Loading ${TABLE} from ${SOURCE_RULESET} (live, not persisted)"
  if ! "${NFT_BIN}" -f "${SOURCE_RULESET}"; then
    cancel_rollback
    die "nft rejected the ruleset; nothing changed and the rollback is cancelled"
  fi

  cat <<EOF

The trial ruleset is live. The rollback fires at $(date -u -d "+${seconds} seconds" +%H:%M:%SZ) unless confirmed.
Keep this session open. From a NEW terminal, open a NEW SSH session over the
tailnet, run the checks in docs/runbook.md "Host firewall (nftables)", then:

  sudo bash ${REPO_DIR}/scripts/host-firewall.sh --confirm

Roll back now instead: sudo bash ${REPO_DIR}/scripts/host-firewall.sh --rollback
EOF
}

case "${1:-}" in
  --check)
    status=0
    check || status=$?
    exit "${status}"
    ;;
  --trial)
    trial "${2:-}"
    ;;
  --confirm)
    rollback_pending || die "no rollback is pending (it already fired, or no trial ran): start again with --trial"
    cancel_rollback
    say "Rollback cancelled; persisting"
    install_and_load
    rm -f "${ROLLBACK_FILE}"
    ;;
  --rollback)
    rollback_pending || die "no rollback is pending (it already fired, or no trial ran); to turn a persisted firewall off: sudo systemctl disable --now ${UNIT}.service"
    cancel_rollback
    "${NFT_BIN}" -f "${ROLLBACK_FILE}"
    say "Rolled back to the state recorded before the trial"
    ;;
  "")
    install_and_load
    ;;
  *)
    die "unknown mode '$1' (see the header of $0)"
    ;;
esac
