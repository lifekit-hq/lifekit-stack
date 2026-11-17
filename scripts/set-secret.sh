#!/usr/bin/env bash
# set-secret.sh - put one secret value in place, end to end, from the operator's PC.
#
#   scripts/set-secret.sh gateway ANTHROPIC_API_KEY     # hidden prompt
#   pbpaste | scripts/set-secret.sh gateway ANTHROPIC_API_KEY   # or stdin
#
# <class> is the secrets file: master (secrets/lifekit.env.sops) or gateway
# (secrets/lifekit-gateway.env.sops). NAME must already be a key in it (a new
# name needs an inventory row first: scripts/secrets/edit.sh, docs/secrets.md).
#
# The value comes from a hidden prompt or stdin, never argv, never echoed,
# never written in plaintext anywhere. sops 3.9 `set` takes the value as an
# argument, so it is not used; instead the file is decrypted into a pipe,
# the one line is swapped in bash memory, and the result is re-encrypted from
# a pipe straight into the file. Then: the secrets tests, a branch, a commit,
# a PR with auto-merge on green, and the next steps. Needs bash, sops, git, gh.
#
# Test hooks (not for operators): SET_SECRET_ROOT (repo root),
# SET_SECRET_TEST_CMD (replaces the pytest run).

set -euo pipefail
set +x
umask 077

ROOT="${SET_SECRET_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "${ROOT}"

die() { echo "set-secret: $*" >&2; exit 1; }

# The body is one function, parsed whole before it runs: the branch switch below
# can change or remove this file on disk, and bash must not read it again.
main() {
[[ $# -eq 2 ]] || { echo "usage: $0 <master|gateway> <NAME>   (value from a hidden prompt or stdin)" >&2; exit 2; }
CLASS="$1"
NAME="$2"

case "${CLASS}" in
  master)  FILE=secrets/lifekit.env.sops ;;
  gateway) FILE=secrets/lifekit-gateway.env.sops ;;
  *) echo "usage: $0 <master|gateway> <NAME>" >&2; exit 2 ;;
esac
[[ "${NAME}" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "NAME must look like ANTHROPIC_API_KEY"

for tool in sops git gh; do
  command -v "${tool}" >/dev/null 2>&1 || die "${tool} not on PATH"
done
KEYFILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"
[[ -f "${KEYFILE}" ]] || die "no age key at ${KEYFILE} (restore it from KeePassXC)"
[[ -s "${FILE}" ]] || die "${FILE} does not exist (scripts/secrets/import-legacy-env.sh)"
export SOPS_AGE_KEY_FILE="${KEYFILE}"
[[ -z "$(git status --porcelain)" ]] || die "working tree is not clean; commit or stash first"

# Key names are plaintext in a sops dotenv file: no decryption to check this.
grep -q "^${NAME}=" "${FILE}" || die "${NAME} is not a key in ${FILE} (a new name: scripts/secrets/edit.sh, plus its docs/secrets.md row)"

# --- the value: hidden prompt on a terminal, else stdin ----------------------
if [[ -t 0 ]]; then
  IFS= read -rs -p "Value for ${NAME} (hidden): " VALUE
  echo >&2
else
  VALUE="$(cat)"
fi
[[ -n "${VALUE}" ]] || die "empty value"
if [[ "${VALUE}" == *$'\n'* || "${VALUE}" == *$'\r'* ]]; then
  die "the value has a line break; a dotenv secret is one line"
fi

# --- branch off the current main, then swap the line in memory ---------------
BRANCH="chore/rotate-$(printf '%s' "${NAME}" | tr 'A-Z_' 'a-z-')-$(date +%Y%m%d%H%M%S)"
git fetch --quiet origin main
git switch --quiet -c "${BRANCH}" --no-track origin/main

PLAIN="$(sops -d --input-type dotenv --output-type dotenv "${FILE}")"
NEW=""
FOUND=0
while IFS= read -r line; do
  if [[ "${line%%=*}" == "${NAME}" ]]; then
    line="${NAME}=${VALUE}"
    FOUND=1
  fi
  NEW+="${line}"$'\n'
done <<<"${PLAIN}"
unset PLAIN
[[ "${FOUND}" -eq 1 ]] || die "${NAME} is not a key in ${FILE} on main"

ENC="$(printf '%s' "${NEW}" | sops -e --config "${ROOT}/.sops.yaml" --filename-override "${FILE}" \
  --input-type dotenv --output-type dotenv /dev/stdin)"
unset NEW VALUE
[[ "${ENC}" == *"ENC[AES256_GCM"* ]] || die "re-encryption produced nothing usable; ${FILE} left untouched"
printf '%s\n' "${ENC}" >"${FILE}"
unset ENC

# --- tests, commit, PR, auto-merge -------------------------------------------
if [[ -n "${SET_SECRET_TEST_CMD:-}" ]]; then
  bash -c "${SET_SECRET_TEST_CMD}"
elif [[ -x .venv/bin/python ]]; then
  .venv/bin/python -m pytest -q scripts/tests/test_secrets.py
else
  python3 -m pytest -q scripts/tests/test_secrets.py
fi

git add -- "${FILE}"
git commit --quiet -m "chore(secrets): rotate ${NAME}"
git push --quiet -u origin "${BRANCH}"
PR_URL="$(gh pr create --base main --head "${BRANCH}" \
  --title "chore(secrets): rotate ${NAME}" \
  --body "Rotates ${NAME} in ${FILE}. Only the encrypted file changes; the value is in no description, commit or log.")"
gh pr merge "${PR_URL}" --auto --squash

cat <<EOF

${NAME} is in place on ${BRANCH}; merge is enabled for when CI is green:
  ${PR_URL}

Next, nothing for you to run: the deploy after the merge applies it.
EOF
if [[ "${CLASS}" == gateway ]]; then
  cat <<'EOF'
  - the deploy pulls the file and runs `openclaw secrets reload`
  - then check it on the box: bash scripts/openclaw-auth/auth-mode.sh probe
    (one JSON line: ok | rejected | exhausted | limited | unknown | no-key)
EOF
else
  cat <<'EOF'
  - master file: after the merge, render and redeploy for the consumers of this
    value (docs/secrets-runbook.md, "Rotation, one sequence per class")
EOF
fi
}

main "$@"
exit
