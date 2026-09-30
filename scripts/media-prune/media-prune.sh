#!/usr/bin/env bash
# media-prune.sh — prune inbound media (voice notes, photos) older than 7 days.
# OpenClaw keeps every inbound attachment; nothing else bounds that directory.
# Runs daily from media-prune.timer as the lifekit account. MEDIA_DIR and
# MAX_AGE_DAYS exist for tests and one-off dry runs; the defaults are the live values.
set -euo pipefail

DIR="${MEDIA_DIR:-/srv/openclaw/config/media/inbound}"
MAX_AGE_DAYS="${MAX_AGE_DAYS:-7}"

[ -d "$DIR" ] || exit 0
find "$DIR" -type f -mtime +"$MAX_AGE_DAYS" -delete
find "$DIR" -mindepth 1 -type d -empty -delete
