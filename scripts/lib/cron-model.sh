# shellcheck shell=bash
# scripts/lib/cron-model.sh - the one model every recurring OpenClaw job and the
# deploy smoke turn run on, read from the repo's config.
#
# Sourced, never executed. The model is agents.defaults.model.primary in
# compose/openclaw-gateway/platform.patch.json, the same file that holds
# modelPolicy.allow, so a job script never names a model of its own: it calls
# cron_model, and scripts/tests/test_cron_model_policy.py fails when the answer
# is not on the allow list or a script spells a model out instead. Kit's own
# chat model is host state (a per-agent entry) and is not read here.

_CRON_MODEL_PATCH="${CRON_MODEL_PATCH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/compose/openclaw-gateway/platform.patch.json}"

# The model a cron job or smoke turn runs on (provider/model).
cron_model() {
  python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))["agents"]["defaults"]
model, allow = d["model"]["primary"], d["modelPolicy"]["allow"]
if model not in allow:
    sys.exit(f"cron model {model} is not in modelPolicy.allow {allow}")
print(model)' "$_CRON_MODEL_PATCH"
}

# The same model with its inference account, provider/model@auth-profile. A cron
# row with no recorded account fails to run, so a job that must name the account
# takes this form (the digest and pulse jobs run on the default account and use
# cron_model). The account is the api-key profile every agent carries
# (docs/secrets.md).
cron_model_account() {
  local model
  model="$(cron_model)" || return 1
  printf '%s@anthropic:api-key\n' "$model"
}
