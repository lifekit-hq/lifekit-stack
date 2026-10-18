#!/usr/bin/env bash
# memory-sync.sh - commit, rebase onto GitHub, push the memory vault (/srv/memory).
# install-memory-sync.sh installs it as /usr/local/bin/memory-sync.sh; memory-sync.timer
# runs it as lifekit every 15 minutes. Vault decision D6, same shape as the PC:
#   commit -> fetch -> rebase origin/main (abort on conflict) -> push HEAD:main.
# No -X theirs/ours and no stash: a real conflict stops the tick with both sides intact.
# Any failure leaves .sync-failed (time, step, exit code); the next clean run removes it.
# log.md and projects/*/log.md merge by union via .gitattributes, which rebase honours.
#
# Commit gate (vault W1), only once the vault ships bin/hooks/pre-commit; without that file
# the tick commits everything, as it did before the gate:
#   - core.hooksPath=bin/hooks is re-set every tick, so the clone cannot silently drop it.
#   - bin/lint.sh --changed names the red files. They stay on disk uncommitted and are listed
#     in .lint-failed; every other change is committed. A tick with no red file removes it.
#     The commit skips the hook: the hook lints every changed file on disk, red ones included,
#     so it would refuse the clean files too - this tick already ran the same check.
#   - Red edits to tracked files block a plain rebase. With any on disk, the rebase runs in a
#     side worktree and the branch moves with checkout, which keeps them; checkout refuses,
#     failing the tick, when the remote changed one of the same files.
#
# The weekly model pass (scripts/ensure-memory-vault-cleanup.sh) commits under its own author.
# While its .model-pass lock is under an hour old the tick does nothing, so the pass's
# half-done edits never land in an auto: commit.
set -euo pipefail
VAULT=${MEMORY_SYNC_DIR:-/srv/memory}
MARKER="$VAULT/.sync-failed"
LINT_MARKER="$VAULT/.lint-failed"
MODEL_LOCK="$VAULT/.model-pass"
cd "$VAULT"
export HOME=${MEMORY_SYNC_HOME:-/home/lifekit}
export GIT_AUTHOR_NAME="Kit (lifekit-vps)"
export GIT_AUTHOR_EMAIL="lifekit-vps@dsdevq.life"
export GIT_COMMITTER_NAME="Kit (lifekit-vps)"
export GIT_COMMITTER_EMAIL="lifekit-vps@dsdevq.life"

if [ -n "$(find "$MODEL_LOCK" -maxdepth 0 -mmin -60 2>/dev/null)" ]; then
  echo "memory-sync: model pass running ($MODEL_LOCK), tick skipped"
  exit 0
fi

STEP=start
SIDE=""
on_exit() {
  local rc=$?
  if [ -n "$SIDE" ]; then
    git worktree remove --force "$SIDE/wt" 2>/dev/null || true
    rm -rf "$SIDE"
    git worktree prune || true
  fi
  [ "$rc" -eq 0 ] || printf "%s step=%s exit=%s\n" "$(date -u +%FT%TZ)" "$STEP" "$rc" > "$MARKER"
}
trap on_exit EXIT

# The markers and the lock are this host's, never vault content.
exclude="$(git rev-parse --git-path info/exclude)"
mkdir -p "$(dirname "$exclude")"
for p in /.sync-failed /.lint-failed /.model-pass; do
  grep -qxF "$p" "$exclude" 2>/dev/null || echo "$p" >> "$exclude"
done

GATE=no
if [ -f bin/hooks/pre-commit ]; then
  GATE=yes
  STEP=hooks
  git config core.hooksPath bin/hooks
fi

STEP=commit
if [ -n "$(git status --porcelain)" ]; then
  red=""
  if [ "$GATE" = yes ]; then
    STEP=lint
    lint_rc=0
    lint_out="$(sh bin/lint.sh --changed --tsv)" || lint_rc=$?
    red="$(printf '%s\n' "$lint_out" | awk -F'\t' '($1 == "high" || $1 == "medium") && $3 != "" {print $3}' | sort -u)"
    if [ "$lint_rc" -ne 0 ] && [ -z "$red" ]; then
      echo "memory-sync: bin/lint.sh --changed failed (exit $lint_rc) without naming a file" >&2
      exit 1
    fi
    STEP=commit
  fi
  git add -A
  if [ -n "$red" ]; then
    while IFS= read -r p; do
      git --literal-pathspecs diff --cached --quiet -- "$p" || git --literal-pathspecs reset --quiet -- "$p"
    done <<< "$red"
    { echo "$(date -u +%FT%TZ) left uncommitted, bin/lint.sh --changed is red on:"; printf '%s\n' "$red"; } > "$LINT_MARKER"
  fi
  if ! git diff --cached --quiet; then
    git commit --quiet --no-verify -m "auto: lifekit-vps sync $(date -u +%FT%TZ)"
  fi
  [ -n "$red" ] || rm -f "$LINT_MARKER"
else
  rm -f "$LINT_MARKER"
fi

STEP=fetch
git fetch --quiet origin

STEP=rebase
if git diff --quiet HEAD --; then
  if ! git rebase --quiet origin/main; then
    git rebase --abort || true
    exit 1
  fi
elif ! git merge-base --is-ancestor origin/main HEAD; then
  SIDE="$(mktemp -d "${TMPDIR:-/tmp}/memory-sync.XXXXXX")"
  git worktree add --quiet --detach "$SIDE/wt" HEAD
  if ! git -C "$SIDE/wt" rebase --quiet origin/main; then
    git -C "$SIDE/wt" rebase --abort || true
    exit 1
  fi
  rebased="$(git -C "$SIDE/wt" rev-parse HEAD)"
  git worktree remove --force "$SIDE/wt"
  rm -rf "$SIDE"
  SIDE=""
  STEP=checkout
  git checkout --quiet -B "$(git symbolic-ref --short HEAD)" "$rebased"
fi

STEP=push
if [ -n "$(git rev-list origin/main..HEAD)" ]; then
  git push --quiet origin HEAD:main
fi
rm -f "$MARKER"
