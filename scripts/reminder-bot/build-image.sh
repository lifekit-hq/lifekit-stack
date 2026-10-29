#!/usr/bin/env bash
# scripts/reminder-bot/build-image.sh - build the reminder bot's image on the
# box from the commit pinned in scripts/reminder-bot/PIN.
#
# The bot's repo is private and publishes no image, and the deploy account has
# no access to it, so the image is built by an operator account that does
# (its existing gh login, read-only: one `gh api` tarball fetch at the pinned
# commit) and compose runs the resulting local tag. docs/runbook.md "Reminder
# bot".
#
# Usage:
#   build-image.sh            build reminder-bot:<short sha> (skips if it exists)
#   build-image.sh --check    exit 1 if that tag is absent; builds nothing
#   build-image.sh --tag      print the tag (the short sha) and exit
#
# A pin bump is a PR that edits PIN; build before or right after merging it.
# Environment (tests and rehearsal): REMINDER_BOT_IMAGE (default
# reminder-bot), REMINDER_BOT_REPO (default dsdevq/reminder-bot),
# REMINDER_BOT_SCRATCH (default ~/.cache/fm-scratch, not /tmp).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${REMINDER_BOT_IMAGE:-reminder-bot}"
REPO="${REMINDER_BOT_REPO:-dsdevq/reminder-bot}"
SCRATCH="${REMINDER_BOT_SCRATCH:-${HOME}/.cache/fm-scratch}"

SHA="$(tr -d '[:space:]' <"${HERE}/PIN")"
if [[ ! "${SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "build-image: ${HERE}/PIN must hold one full 40-character commit sha" >&2
  exit 2
fi
TAG="${SHA:0:7}"

case "${1:-}" in
  --tag) echo "${TAG}"; exit 0 ;;
  --check)
    docker image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1 && exit 0
    echo "build-image: ${IMAGE}:${TAG} is absent; build it with: bash scripts/reminder-bot/build-image.sh" >&2
    exit 1 ;;
  "") ;;
  *) echo "usage: build-image.sh [--check|--tag]" >&2; exit 2 ;;
esac

if docker image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1; then
  echo "build-image: ${IMAGE}:${TAG} already exists; nothing to do"
  exit 0
fi

mkdir -p "${SCRATCH}"
BUILD_DIR="$(mktemp -d "${SCRATCH}/reminder-bot-build.XXXXXX")"
trap 'rm -rf "${BUILD_DIR}"' EXIT

gh api "repos/${REPO}/tarball/${SHA}" >"${BUILD_DIR}/src.tar.gz"
mkdir "${BUILD_DIR}/src"
tar -xzf "${BUILD_DIR}/src.tar.gz" -C "${BUILD_DIR}/src" --strip-components=1
docker build --memory 1g -t "${IMAGE}:${TAG}" "${BUILD_DIR}/src"
echo "build-image: built ${IMAGE}:${TAG}"
