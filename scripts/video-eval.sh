#!/usr/bin/env bash
# video-eval.sh - file and follow video evaluations from the VPS host: the same
# typed job kit files (skills/video-eval/video.py, on the shared job queue),
# for a producer that is not on Telegram, such as firstmate (docs/runbook.md,
# "Video evaluation"). The job runs inside the gateway like any other; its
# answer goes to whoever polls it here, never to the owner's chat.
#
#   video-eval.sh enqueue <link> [--check]   queue a YouTube or Instagram link
#   video-eval.sh backfill FILE...           queue files already downloaded,
#                                            without fetching them again:
#                                            reel-<shortcode>.mp4, yt-<id>.<lang>.vtt,
#                                            or any other video file
#   video-eval.sh status <job id>            queued | running | done | failed,
#                                            and a finished job's three lines
#   video-eval.sh report <job id>            the job's full report
#   video-eval.sh index                      every item judged, one row each
#   video-eval.sh idea [FILE]                file a judged idea's report (FILE,
#                                            else stdin): no job, no fetch;
#                                            always origin scout; prints its
#                                            three lines at once
#   video-eval.sh link [FILE] [--check]      file a judged link's or article's
#                                            report (FILE, else stdin) the same
#                                            way, as VIDEO_EVAL_ORIGIN
#   video-eval.sh note [FILE] [--check]      file a judged text note's report
#                                            (FILE, else stdin) the same way
#   video-eval.sh voice FILE [--check]       queue a voice note (an audio file):
#                                            transcribed, then judged as a note
#
# Completion signal: `status` prints `done` or `failed` on its first line once
# the job is finished, and a done job's last line is `report: <host path>`.
# enqueue prints the job id (video-<key>). A link already judged prints its
# earlier verdict instead, and nothing is queued; voice prints its job id the
# same way. idea, link and note exit 3 with the reasons when the report does
# not follow the rubric.
#
# Run as a user that may use docker (denys, or lifekit). Paths it prints are
# host paths: the gateway's /home/node/.life-state is /var/lib/lifekit here,
# readable by lifekit.
#
# Env overrides:
#   VIDEO_EVAL_ORIGIN    who is asking, kit | firstmate | scout (default: firstmate;
#                        not used by idea, which is always scout)
#   VIDEO_EVAL_AGENT     agent whose workspace has the skill (default: kit)
#   DOCKER               the docker command (default: docker)
set -euo pipefail

usage() {
  sed -n '7,27p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

ORIGIN="${VIDEO_EVAL_ORIGIN:-firstmate}"
AGENT="${VIDEO_EVAL_AGENT:-kit}"
DOCKER="${DOCKER:-docker}"
SCRIPT="/home/node/.openclaw/agents/${AGENT}/workspace/skills/video-eval/video.py"

[ $# -ge 1 ] || usage
cmd="$1"
shift

GATEWAY="$($DOCKER ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "video-eval: no running openclaw-gateway container" >&2
  exit 1
fi

host_paths() { sed 's#/home/node/\.life-state/#/var/lib/lifekit/#g'; }

video() { $DOCKER exec "$GATEWAY" python3 "$SCRIPT" "$@"; }

case "$cmd" in
  enqueue)
    [ $# -ge 1 ] || usage
    video enqueue "$@" --origin "$ORIGIN" | host_paths
    ;;
  backfill)
    [ $# -ge 1 ] || usage
    rc=0
    for f in "$@"; do
      if [ ! -f "$f" ]; then
        echo "$f: no such file" >&2
        rc=1
        continue
      fi
      name="$(basename "$f")"
      printf '%s: ' "$name"
      $DOCKER exec -i "$GATEWAY" python3 "$SCRIPT" ingest "$name" \
        --origin "$ORIGIN" <"$f" | host_paths || rc=1
    done
    exit "$rc"
    ;;
  status | index)
    video "$cmd" "$@" | host_paths
    ;;
  report)
    [ $# -eq 1 ] || usage
    video show "$1"
    ;;
  idea)
    [ $# -le 1 ] || usage
    if [ $# -eq 1 ] && [ ! -f "$1" ]; then
      echo "$1: no such file" >&2
      exit 1
    fi
    $DOCKER exec -i "$GATEWAY" python3 "$SCRIPT" idea \
      <"${1:-/dev/stdin}" | host_paths
    ;;
  link | note | voice)
    check=() file=""
    for a in "$@"; do
      case "$a" in
        --check) check=(--check) ;;
        *) [ -z "$file" ] || usage; file="$a" ;;
      esac
    done
    if [ -n "$file" ] && [ ! -f "$file" ]; then
      echo "$file: no such file" >&2
      exit 1
    fi
    name=()
    if [ "$cmd" = voice ]; then
      [ -n "$file" ] || usage
      name=("$(basename "$file")")
    fi
    $DOCKER exec -i "$GATEWAY" python3 "$SCRIPT" "$cmd" "${name[@]}" \
      --origin "$ORIGIN" "${check[@]}" <"${file:-/dev/stdin}" | host_paths
    ;;
  *) usage ;;
esac
