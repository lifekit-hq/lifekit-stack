#!/usr/bin/env bash
# Memory rotation — mechanism half, runtime state only. logrotate (.jsonl) +
# rotate-state.py (morning-brief inbox archival + trajectory gzip/prune +
# footprint report + heartbeat). NO LLM. The vault rotates its own pages with
# its bin/rotate.sh; nothing here edits /srv/memory.
#
# Runtime state -> /var/lib/lifekit (rotation bookkeeping in rotation/, the
# morning brief's inputs in morning-brief/). The code (this wrapper,
# rotate-state.py, logrotate-memory.conf) lives in the lifekit-stack repo at
# scripts/rotate/, pulled to /srv/lifekit-stack; install-memory-rotate.sh
# installs this wrapper and the units. Override paths via env for other layouts.
set -euo pipefail
STATE="${ROTATE_STATE:-/var/lib/lifekit/rotation}"
BRIEF="${MORNING_BRIEF_DIR:-/var/lib/lifekit/morning-brief}"
AGENTS="${OPENCLAW_AGENTS:-/srv/openclaw/config/agents}"
ROTATE_DIR="${ROTATE_DIR:-/srv/lifekit-stack/scripts/rotate}"
/usr/sbin/logrotate --state "$STATE/logrotate.state" "$ROTATE_DIR/logrotate-memory.conf"
exec /usr/bin/python3 "$ROTATE_DIR/rotate-state.py" --brief-dir "$BRIEF" --state-dir "$STATE" --agents-dir "$AGENTS"
