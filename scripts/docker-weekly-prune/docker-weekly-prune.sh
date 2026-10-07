#!/usr/bin/env bash
# docker-weekly-prune.sh — weekly prune of unused Docker images and anonymous
# volumes. Runs as root from lifekit-docker-weekly-prune.timer.
#
#   docker image prune -a -f --filter label!=lifekit.keep=rollback
#   docker volume prune -f
#
# Never build cache (docker-prune-policy.sh and docker-builder-gc.sh own
# that), never networks, never named volumes (`volume prune` without --all
# removes anonymous volumes only), and no daemon or container restart.
#
# Rollback images: `image prune -a` removes every image no container uses,
# which includes lifekit-openclaw:prev. The gateway Dockerfile stamps the
# keep label on the image, so the filter skips it and any image that carries
# the label. :prev is a retag of the image that ran the previous version, so
# it carries the label only once that image was built from a Dockerfile that
# has it. An unlabeled *:prev (the one that predates the label) is therefore
# refused: the image prune is skipped, the volume prune still runs, and the
# unit fails so the journal says why.
#
# Env overrides (tests): DOCKER.
set -euo pipefail

DOCKER="${DOCKER:-docker}"
KEEP_LABEL_KEY=lifekit.keep
KEEP_LABEL="${KEEP_LABEL_KEY}=rollback"

unlabeled_rollbacks() {
  local ref label
  while read -r ref; do
    [[ -n "$ref" ]] || continue
    label="$("$DOCKER" image inspect --format "{{index .Config.Labels \"${KEEP_LABEL_KEY}\"}}" "$ref" 2>/dev/null || true)"
    [[ "$label" == rollback ]] || echo "$ref"
  done < <("$DOCKER" image ls --filter 'reference=*:prev' --format '{{.Repository}}:{{.Tag}}')
}

failed=0
bad="$(unlabeled_rollbacks)"
if [[ -n "$bad" ]]; then
  echo "refusing image prune: rollback image(s) without ${KEEP_LABEL}: ${bad//$'\n'/ }" >&2
  failed=1
else
  echo "docker image prune -a -f --filter label!=${KEEP_LABEL}"
  "$DOCKER" image prune -a -f --filter "label!=${KEEP_LABEL}" 2>&1 | sed 's/^/  [image] /' || failed=1
fi
echo "docker volume prune -f"
"$DOCKER" volume prune -f 2>&1 | sed 's/^/  [volume] /' || failed=1
exit "$failed"
