# Secrets runbook: rotate, purge, rebuild

Companion to [`docs/secrets.md`](./secrets.md) (the inventory and the
boundary rule). Everything here is one of four moves: edit a SOPS file and
merge, render the master file, reload the gateway, or re-key. Nothing is
applied by hand to the running gateway: a merge deploys, the deploy resolves
the gateway refs in a dry run before writing, and reloads them after.

Tools: `sops` (pinned by `compose/openclaw-gateway/install-sops.sh`; the
admin account also has its own), `age-keygen` for minting keys, `git`.

## The four moves

```bash
# 1. edit a value in place (captain key; nothing plaintext touches disk)
scripts/secrets/edit.sh master        # or: gateway
.venv/bin/python -m pytest -q scripts/tests/test_secrets.py
git commit -am "chore(secrets): rotate <NAME>"     # then PR, CI green, merge

# 2. render the master file (after a merged master change; admin account, sudo)
sudo bash scripts/secrets/render-stack-env.sh      # -> /srv/lifekit-secrets/stack.env

# 3. redeploy (recreates the services whose env changed, reloads gateway refs)
gh workflow run ci.yml --ref main                  # or on the box: bash scripts/deploy.sh

# 4. re-key a file after a recipient change in .sops.yaml (captain key)
sops updatekeys secrets/lifekit-gateway.env.sops   # or lifekit.env.sops
```

## Rotation, one sequence per class

| Class | Names | Sequence |
| --- | --- | --- |
| **telegram-bot, gateway file** | `KIT_BOT_TOKEN`, `FABLE_BOT_TOKEN`, `FINANCE_BOT_TOKEN`, `LEARNING_BOT_TOKEN`, `SOCIAL_BOT_TOKEN` | BotFather `/revoke` for the bot, paste the new token with `edit.sh gateway`, merge. The deploy pulls the file and runs `openclaw secrets reload`; no recreate. Verify: `openclaw channels status`. |
| **telegram-bot, master file** | `DEVCLAW_BOT_TOKEN` | BotFather `/revoke`, `edit.sh master`, merge, render, redeploy (recreates gateway, grafana, notify-relay). Verify: `openclaw channels status`, then a Grafana test notification. |
| **api-token** | `OPENCLAW_GATEWAY_TOKEN` | `openssl rand -hex 32` into `edit.sh master`, merge, render, redeploy. Then the pairs: finance-sentry `docker/secrets-edit.sh` (`OPENCLAW_GATEWAY_TOKEN`) and its deploy; lifekit-dashboard `/srv/dashboard/.env` and its deploy. Verify: the prometheus `openclaw` target is up, the dashboard loads. |
| **api-token** | `OPENCLAW_HOOK_TOKEN`, `OPENCLAW_HOOK_PATH` | `edit.sh master` (token: `openssl rand -hex 32`; path: `/hooks-$(openssl rand -hex 8)`), merge, render, redeploy. Then finance-sentry's copy and its deploy. Never remove the token while hooks are enabled (the gateway refuses to start). |
| **mcp-bearer** | `FINANCE_SENTRY_MCP_TOKEN` | generate, `edit.sh master`, merge, render, redeploy; finance-sentry `secrets-edit.sh` (`MCP_TOKEN`) and its deploy. Verify: a finance-agent turn that calls an MCP tool. |
| **mcp-bearer** | `DEVCLAW_MCP_TOKEN` | generate, `edit.sh master`, merge, render, redeploy; `/srv/devclaw/.env` (`DEVCLAW_TOKEN`) and recreate devclaw-mcp; the dashboard's devclaw bearer. |
| **oauth-client** | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | rotate the secret in the Google Cloud console, `edit.sh master`, merge, render, redeploy (recreates google-workspace-mcp). A new client id also needs a new refresh token: `scripts/google-mcp-bootstrap.sh`. |
| **admin-password** | `GRAFANA_ADMIN_PASSWORD` | `docker exec compose-grafana-1 grafana cli admin reset-admin-password '<new>'` (the env value is read at first boot only), then the same value with `edit.sh master`, merge, render, redeploy. Verify: `deploy.sh`'s provisioning reload succeeds. |
| **parked** | `PARKED_BINANCE_API_KEY`, `PARKED_BINANCE_API_SECRET` | captain's call: rotate at Binance, `edit.sh master`. When a consumer appears, drop the prefix and move the row; if none by 2026-12-31, delete both. |
| **gateway age key** | `/srv/lifekit-secrets/gateway/lifekit-gateway.agekey` | `sudo mv` the old key aside, `sudo bash scripts/secrets/init-gateway-key.sh` (prints the new recipient), replace the recipient in `.sops.yaml`, `sops updatekeys secrets/lifekit-gateway.env.sops`, merge (the deploy's dry run proves the new key opens the file), copy the new private key to KeePassXC, delete the old one. Yearly review: first 2027-09-19. |
| **captain age key** | `~/.config/sops/age/keys.txt` | `age-keygen -o keys.new`, add its recipient to both rules in `.sops.yaml`, `sops updatekeys` both files with the OLD key still present, merge, install the new key file (0600) and KeePassXC, then drop the old recipient with a second `updatekeys` and merge. finance-sentry's `docker/.env.sops` uses the same identity: re-key it in the same window. |

Rotation on migration (all telegram-bot and mcp-bearer rows marked "at
migration" in the inventory): those values sat readable by every
exec-capable agent until 2026-09-19. Rotate each once after the cutover,
with the sequence above, and set the "Last rotated" date in the inventory
in the same PR.

## One-time cutover on lifekit-vps (in this order)

1. `sudo bash scripts/secrets/init-gateway-key.sh` on the box; put the
   printed recipient into `.sops.yaml` (gateway rule).
2. `scripts/secrets/import-legacy-env.sh /srv/openclaw/config/.env` as the
   admin account (captain key): writes the two `.sops` files, prints key
   names only. Compare the names with the inventory, commit, PR, merge.
3. `sudo bash scripts/secrets/render-stack-env.sh` (`/srv/lifekit-secrets/stack.env`).
4. The merge deploys: `up -d` recreates the gateway with the mounts, the
   platform patch adds the `sops` provider and the five `botToken` refs after
   a dry run that resolves them, `secrets reload` follows.
5. Verify: `openclaw channels status` shows every account connected;
   `openclaw secrets audit --allow-exec` lists no gateway finding;
   `docker exec compose-openclaw-gateway-1 env | cut -d= -f1 | grep -c BOT_TOKEN`
   prints 1 (`DEVCLAW_BOT_TOKEN`, the master-file one).
6. Purge (below), then rotate on migration (above).
7. Captain: offline copies of both private keys in KeePassXC; Tailscale
   admin console -> Machines -> `lifekit-vps` -> Disable key expiry.

## Purge: residue that still carries values

Host-level and destructive: run as the admin account after step 5 above,
each line on its own, and only after the gateway has been running from the
new files for one deploy. The list is the scout inventory of 2026-09-16
(report `guard-6-gaps-secrets`, section 3.8).

```bash
# the legacy env file and its backups (the gateway's old global dotenv)
sudo rm -v /srv/openclaw/config/.env /srv/openclaw/config/.env.bak*
# config backups OpenClaw itself keeps are refs-only after the cutover and
# are recreated on every write; the hand-made ones from before hold tokens
sudo find /srv/openclaw/config -maxdepth 1 -name 'openclaw.json.pre-*' -o -name 'openclaw.json.clobbered.*' \
  -o -name 'openclaw.json.bak-*' | sudo xargs -r rm -v
# archived per-agent auth files flagged by `openclaw secrets audit`
sudo find /srv/openclaw/config/agents -path '*/agent/auth-*.json.*' -print -delete
# sqlite copies
sudo rm -v /srv/openclaw/config/state/openclaw.sqlite.bak-* /srv/openclaw/config/plugin-state/state.sqlite.bak-*
# the unused compose-dir env and its backups (deploy uses --env-file)
sudo rm -v /srv/lifekit-stack/compose/.env /srv/lifekit-stack/compose/.env.bak*
# the on-box OpenClaw backup tarball, once a copy is off the box
# sudo rm -v /srv/openclaw/backups/*-openclaw-backup.tar.gz
# stale finance-sentry plaintext env (still the env file of finance-sentry-postgres:
# recreate that container from finance-sentry's deploy first)
# sudo rm -v /srv/finance-sentry/docker/.env
# the devclaw env backup and the no-mistakes evidence env files
sudo rm -v /home/lifekit/devclaw.env.bak-*
rm -v ~/.no-mistakes/evidence/*/api-*.env
# stale root Claude login, unused GH secret
sudo rm -v /root/.claude/.credentials.json
gh secret delete TS_AUTHKEY -R lifekit-hq/lifekit-stack
```

Then `openclaw secrets audit --allow-exec` again: the `LEGACY_RESIDUE`
line for archived auth files must be gone. The `PLAINTEXT_FOUND` rows for
`anthropic:manual` / `anthropic:default` are the firstmate item
`openclaw-auth-dedupe-retry`, not this runbook.

## Gate

`scripts/deploy.sh` runs `openclaw secrets audit --allow-exec` after every
deploy as a report. It becomes `--check` with `fail_later` (a red deploy on
any finding) once the audit is clean on the box, which needs the auth-profile
dedupe above; owner firstmate, review 2026-10-15.

CI, on every PR (`scripts/tests/test_secrets.py`): both files are SOPS
dotenv with every value encrypted; their recipients equal `.sops.yaml`;
every name is in the inventory and every inventory row is in its file; the
gateway file holds exactly the names the platform patch references; the
resolver's protocol; and, on the box's runner, the gateway file really
decrypts with the gateway key (values checked for presence, never printed).
The master file is never decrypted in CI, by design: nothing on a service
account can open it. gitleaks (pre-commit and the full-history job) stays
the plaintext backstop.

## Rebuild: a fresh box from the repo plus the master key

Inputs: this repo, the captain age key from KeePassXC, the gateway age key
from KeePassXC (or mint a new one - step 2b), and the usual bootstrap
inputs (`scripts/bootstrap-vps.sh` header).

1. `sudo TAILSCALE_AUTH_KEY=... TAILSCALE_HOSTNAME=lifekit-vps ./scripts/bootstrap-vps.sh`
   (installs sops, creates `/srv/lifekit-secrets{,/gateway}`, clones the repo).
2. Keys.
   - a. Captain: `install -d -m 0700 ~/.config/sops/age && install -m 0600 /dev/stdin ~/.config/sops/age/keys.txt`
     with the key pasted from KeePassXC.
   - b. Gateway: either paste the saved key with
     `sudo install -o lifekit -g lifekit -m 0400 /dev/stdin /srv/lifekit-secrets/gateway/lifekit-gateway.agekey`,
     or mint a new one with `sudo bash scripts/secrets/init-gateway-key.sh`
     and follow the "gateway age key" row above (re-key, merge).
3. `sudo bash scripts/secrets/render-stack-env.sh`.
4. Restore data (vault, OpenClaw state from an off-box backup, `/srv/openclaw/secret-key`).
5. `bash scripts/deploy.sh` as `lifekit`: the platform patch registers the
   provider and the refs on the fresh `openclaw.json`; the dry run proves the
   gateway key opens the file before anything is written.
6. Interactive re-issues, as before: the Claude Code login, the Codex OAuth
   login, the Google Workspace refresh token. Telegram needs nothing: the
   tokens came from the gateway file.

**Dry run, 2026-09-19.** Exercised in a scratch clone with scratch keys
(nothing on the host, no production value): `init`-equivalent keygen,
`.sops.yaml` with the scratch recipients, `import-legacy-env.sh` on a
synthetic legacy env, `pytest scripts/tests/test_secrets.py` green against
those files, `render-stack-env.sh` into a scratch `LIFEKIT_SECRETS_DIR`
(`PARKED_*` absent from the output), and the resolver answering the exact
five ids the platform patch carries with the scratch gateway key.

## Accepted debt

- The gateway age key has no offline copy - accepted by the captain
  2026-09-29; owner: captain; if it is lost, re-mint it with
  `init-gateway-key.sh` and re-encrypt the gateway file with the captain
  key; revisit at the first key rotation.

## Follow-ups (not in this ship)

- Move the `claude-cli:setup-token` auth profile onto a `tokenRef` in the
  gateway file: an `openclaw secrets configure --plan-out` plan for
  `profiles.claude-cli:setup-token.token` (agent `main`), applied on the box.
  SQLite auth rows cannot ride the platform patch.
- `secrets.providers.default = {source: env, allowlist: [...]}` so no env
  name outside the three master-file refs can be pulled into a credential
  path. Add it to the platform patch once the env list is final.
- Give the uptime probe its own bot so the `FINANCE_BOT_TOKEN` copy goes.
