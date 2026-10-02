# Secrets: inventory and boundaries

Every secret the stack uses is listed here, by name, with the boundary it
lives in, who consumes it, who owns it, how often it rotates and when it last
did. `scripts/tests/test_secrets.py` holds this page and the encrypted files
to each other on every PR: a name in a file that is not on this page fails
CI, and so does a row here with no entry in its file. **No secret exists that
is not in this inventory.**

Tooling is SOPS with age, the same pair finance-sentry already uses on this
box. There is no vault server: one host, one operator. A control plane
(OpenBao, Infisical, a hosted secrets manager) is added when a second host or
a second person appears, not before.

## Two files, two keys

| File | Boundary | Recipients | Who decrypts | Holds |
| --- | --- | --- | --- | --- |
| `secrets/lifekit.env.sops` | **master** | captain | the captain (`~/.config/sops/age/keys.txt` on the admin account, offline copy in KeePassXC), and the box only during bootstrap or a render, through that same key | every compose-interpolated secret and setting; parked secrets |
| `secrets/lifekit-gateway.env.sops` | **gateway** | captain + gateway | the OpenClaw gateway's exec resolver, with `/srv/lifekit-secrets/gateway/lifekit-gateway.agekey` (lifekit, 0400); the captain, to edit | only what OpenClaw resolves through a SecretRef and nothing else uses |

Least privilege is structural, not procedural: the gateway key is a
recipient of the gateway file only, so a compromised gateway (or an
exec-capable agent inside it, which runs as the same uid and can read what
the gateway reads) reaches five bot tokens the gateway holds in memory
anyway, and nothing from the master file. The master key never sits on a
service account.

Both files are dotenv: key names are plaintext, values are encrypted, and
git history is the change log (one rotation, one commit). Recipients are in
`.sops.yaml`.

**How each boundary reaches its consumer**

- **master -> compose.** `sudo scripts/secrets/render-stack-env.sh` decrypts
  the master file into `/srv/lifekit-secrets/stack.env` (root:lifekit 0640,
  `PARKED_*` dropped). `scripts/deploy.sh` passes it as `--env-file`; compose
  interpolates the names the services reference and passes nothing else.
  The file is outside every container mount and outside the OpenClaw state
  dir. (Until 2026-09-19 the env file was `/srv/openclaw/config/.env`, which
  the gateway also loaded as its global dotenv - every secret was readable
  from inside any exec-capable agent.)
- **gateway -> OpenClaw.** `compose/openclaw-gateway/platform.patch.json`
  registers an exec SecretRef provider `sops`
  (`/opt/lifekit-secrets/sops-resolver.py`, in the image) and points each
  Telegram account's `botToken` at it. The resolver speaks OpenClaw's exec
  protocol (ids in on stdin, JSON values out on stdout), runs the image's
  pinned `sops` once per request against the file mounted read-only from this
  checkout, with the gateway key mounted read-only from
  `/srv/lifekit-secrets/gateway/`. Every agent's `anthropic:setup-token`
  auth profile points at it too (`claude-oauth-token`); that ref lives in
  each agent's auth store on the box, not in the patch (secrets runbook,
  "Claude service token"). The deploy's platform-patch dry run
  resolves those refs (`--allow-exec`) before writing anything, and
  `openclaw secrets reload` after every deploy picks up rotated values.

## Naming

One convention across both files: `<CONSUMER>_<PURPOSE>` in upper snake
case (`KIT_BOT_TOKEN`, `FINANCE_SENTRY_MCP_TOKEN`). An exec SecretRef id
may not contain an underscore, so the gateway file's keys are referenced in
lower-kebab form: `kit-bot-token` names `KIT_BOT_TOKEN`; the resolver maps one
to the other and nothing else. A secret with no consumer carries the
`PARKED_` prefix, stays in the master file, is never rendered, and is either
given a consumer or deleted at its next review.

## Inventory: secrets

Owner is who can mint a replacement. Rotation is the cadence plus the
trigger; the rows below that sat agent-readable before 2026-09-19 (report
`guard-6-gaps-secrets`, section 3.1) point to the runbook's "Rotation on
migration" paragraph, which records that debt as accepted rather than
carrying a pending trigger here. Last rotated is the date of the last known
new value; "not recorded" is exactly that, and the first rotation after
this inventory sets it.

### gateway - `secrets/lifekit-gateway.env.sops`

| Name | Boundary | Class | Consumers | Owner | Rotation | Last rotated | File |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `KIT_BOT_TOKEN` | gateway | telegram-bot | openclaw-gateway, Telegram account `default` (Kit). Was `TELEGRAM_BOT_TOKEN`. | captain (BotFather) | on exposure; migration debt accepted (runbook: Rotation on migration) | not recorded | `secrets/lifekit-gateway.env.sops` |
| `CAREER_BOT_TOKEN` | gateway | telegram-bot | openclaw-gateway, Telegram account `career`. Was `FABLE_BOT_TOKEN`: the `fable` account is gone, `career` replaced it on the live roster (re-derived at PR time, not carried over from the original design). | captain (BotFather) | on exposure; migration debt accepted (runbook: Rotation on migration) | not recorded | `secrets/lifekit-gateway.env.sops` |
| `FINANCE_BOT_TOKEN` | gateway | telegram-bot | openclaw-gateway, Telegram account `finance`. | captain (BotFather) | on exposure; migration debt accepted (runbook: Rotation on migration) | not recorded | `secrets/lifekit-gateway.env.sops` |
| `LEARNING_BOT_TOKEN` | gateway | telegram-bot | openclaw-gateway, Telegram account `reading` | captain (BotFather) | on exposure; migration debt accepted (runbook: Rotation on migration) | not recorded | `secrets/lifekit-gateway.env.sops` |
| `SOCIAL_BOT_TOKEN` | gateway | telegram-bot | openclaw-gateway, Telegram account `social`. Literal copies in two `openclaw.json` backups from 2026-07 - purged by the runbook. | captain (BotFather) | on exposure; migration debt accepted (runbook: Rotation on migration) | not recorded | `secrets/lifekit-gateway.env.sops` |
| `CLAUDE_OAUTH_TOKEN` | gateway | api-token | openclaw-gateway, through SecretRef id `claude-oauth-token`: an exec/sops `tokenRef` on the `anthropic:setup-token` profile in every agent's own auth store (secrets runbook, "Claude service token"); devclaw reads it too once it resumes. A one-year setup-token from `claude setup-token`. | captain (`claude setup-token`) | yearly, before it expires: the `claude-oauth-token-expiring` alert fires 30 days ahead. Expires 2027-09-30. | 2026-09-30 (created) | `secrets/lifekit-gateway.env.sops` |

### master - `secrets/lifekit.env.sops`

| Name | Boundary | Class | Consumers | Owner | Rotation | Last rotated | File |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `OPENCLAW_GATEWAY_TOKEN` | master | api-token | openclaw-gateway (`gateway.auth.token`, env ref), openclaw-cli, `scripts/deploy.sh`, prometheus (compose secret `openclaw_gateway_token`). Pairs: lifekit-dashboard `/srv/dashboard/.env`, finance-sentry `docker/.env.sops` (`OPENCLAW_GATEWAY_TOKEN`). | captain (`openssl rand -hex 32`) | on exposure; yearly review | not recorded | `secrets/lifekit.env.sops` |
| `OPENCLAW_HOOK_TOKEN` | master | api-token | openclaw-gateway (`hooks.token`, env ref); finance-sentry presents it as the hook caller. | captain (`openssl rand -hex 32`) | on exposure; yearly review | 2026-09-18 (created) | `secrets/lifekit.env.sops` |
| `OPENCLAW_HOOK_PATH` | master | api-token | openclaw-gateway (`hooks.path`, env ref); finance-sentry calls it. A non-guessable path is a credential. | captain | with `OPENCLAW_HOOK_TOKEN` | 2026-09-18 (created) | `secrets/lifekit.env.sops` |
| `DEVCLAW_BOT_TOKEN` | master | telegram-bot | openclaw-gateway (Telegram account `devclaw`, env ref via compose), grafana (alert contact point), notify-relay. Three consumers, so master, not gateway. | captain (BotFather) | on exposure; migration debt accepted (runbook: Rotation on migration) | not recorded | `secrets/lifekit.env.sops` |
| `FINANCE_SENTRY_MCP_TOKEN` | master | mcp-bearer | openclaw-gateway (`mcp.servers.finance-sentry.headers`, env template). Pair: finance-sentry `docker/.env.sops` `MCP_TOKEN`. | captain | on exposure; migration debt accepted (runbook: Rotation on migration; literal in two config backups) | not recorded | `secrets/lifekit.env.sops` |
| `DEVCLAW_MCP_TOKEN` | master | mcp-bearer | openclaw-gateway (`mcp.servers.devclaw.headers`, env template); edge `traefik` (the bearer it adds to signed-in devclaw console requests, `compose/edge/traefik/dynamic.yml`). Pair: `/srv/devclaw/.env` `DEVCLAW_TOKEN`. | captain | on exposure | 2026-09-16 (created) | `secrets/lifekit.env.sops` |
| `GOOGLE_OAUTH_CLIENT_ID` | master | oauth-client | google-workspace-mcp | captain (Google Cloud console) | on exposure | not recorded | `secrets/lifekit.env.sops` |
| `GOOGLE_OAUTH_CLIENT_SECRET` | master | oauth-client | google-workspace-mcp (re-issue the refresh token with `scripts/google-mcp-bootstrap.sh` if the client changes) | captain (Google Cloud console) | on exposure | not recorded | `secrets/lifekit.env.sops` |
| `GRAFANA_ADMIN_PASSWORD` | master | admin-password | grafana (first boot only - later changes need `grafana cli admin reset-admin-password`), `scripts/deploy.sh` provisioning reloads | captain | on exposure | not recorded | `secrets/lifekit.env.sops` |
| `PARKED_BINANCE_API_KEY` | master | parked | none since 2026-07-11 (the Binance MCP was dropped). Was `BINANCE_API_KEY` in the gateway env. Not revoked: the captain rotates it at Binance later. | captain (Binance) | captain's call | not recorded | `secrets/lifekit.env.sops` |
| `PARKED_BINANCE_API_SECRET` | master | parked | none; pair of the key above | captain (Binance) | captain's call | not recorded | `secrets/lifekit.env.sops` |
| `LIFEKIT_EXTERNAL_HEARTBEAT_URL` | master | heartbeat-url | grafana (compose env `HEARTBEAT_URL`; external dead-man heartbeat ping, provisions/deletes the watchdog rule, `docs/runbook.md` "External heartbeat"), `scripts/deploy.sh` (reads it to log enabled/disabled). The URL itself is the credential: anyone who has it can ping (and so silence) the watchdog. | captain | on exposure; migration debt accepted (runbook: Rotation on migration) | not recorded | `secrets/lifekit.env.sops` |
| `LOGTO_DB_PASSWORD` | master | db-password | identity `postgres` (`POSTGRES_PASSWORD`, set on the first boot of an empty volume only) and `logto` (`DB_URL`), compose project `identity`; `scripts/deploy.sh` brings the project up only when it is set. A change after first boot also needs `ALTER ROLE logto PASSWORD ...` inside the database (`docs/runbook.md` "Identity provider (Logto)"). | captain (`openssl rand -hex 32`) | on exposure | not recorded | `secrets/lifekit.env.sops` |

## Inventory: settings carried in the master file

Not secrets, but personal or box-specific (`docs/PRIVATE.md`), so they ride
in the same encrypted file and the same rendered env rather than in git.
`.env.example` documents each one. The test only checks that a name in the
file appears in one of the two inventory tables.

| Name | Boundary | Class | What |
| --- | --- | --- | --- |
| `OPENCLAW_CONFIG_DIR` | master | setting | host path of the OpenClaw state dir |
| `OPENCLAW_WORKSPACE_DIR` | master | setting | host path of the workspace |
| `OPENCLAW_AUTH_PROFILE_SECRET_DIR` | master | setting | host path of the auth-profile key dir |
| `LIFEKIT_LIFE_DIR` | master | setting | host path of the vault |
| `LIFEKIT_STATE_DIR_HOST` | master | setting | host path of runtime state |
| `LIFEKIT_SECRETS_DIR` | master | setting | host path of the secrets dir (default `/srv/lifekit-secrets`) |
| `LIFEKIT_CLAUDE_HOME` | master | setting | host path of the Claude Code login mounted into the gateway |
| `LIFEKIT_CLAUDE_JSON` | master | setting | host path of `.claude.json` |
| `LIFEKIT_GH_CONFIG` | master | setting | host path of the `gh` login mounted into the gateway |
| `LIFEKIT_GITCONFIG` | master | setting | host path of the gitconfig |
| `LIFEKIT_WORKOUT_CLAW_DIR` | master | setting | host path of the workout-claw checkout |
| `OPENCLAW_GATEWAY_PORT` | master | setting | loopback port |
| `OPENCLAW_GATEWAY_BIND` | master | setting | bind mode |
| `OPENCLAW_ALLOW_INSECURE_PRIVATE_WS` | master | setting | CLI/gateway plaintext-WS switch |
| `OPENCLAW_TZ` | master | setting | timezone |
| `OPENCLAW_FINANCE_CHAT` | master | setting | Telegram chat id the finance agent delivers to (personal id) |
| `LIFEKIT_TELEGRAM_CHAT` | master | setting | orchestrator/alert chat id (personal id) |
| `DEVCLAW_CHAT` | master | setting | devclaw alert chat id (personal id) |
| `TELEGRAM_OWNER_USER_ID` | master | setting | the owner's Telegram user id (personal id) |
| `USER_GOOGLE_EMAIL` | master | setting | the Google account of the workspace MCP (personal) |
| `WORKSPACE_MCP_TOOLS` | master | setting | google-workspace-mcp tool list |
| `GRAFANA_ADMIN_USER` | master | setting | Grafana admin login name |
| `GRAFANA_ROOT_URL` | master | setting | Grafana public root URL (tailnet name) |
| `IDENTITY_ENDPOINT` | master | setting | Logto's public URL, the issuer base (tailnet name); optional, deploy derives it |
| `IDENTITY_ADMIN_ENDPOINT` | master | setting | Logto admin console URL (tailnet name); optional, deploy derives it |
| `GRAFANA_OIDC_ENABLED` | master | setting | `true` turns on Grafana's sign-in through Logto; unset is off |
| `GRAFANA_OIDC_ONLY` | master | setting | `true` hides Grafana's password form (needs the three around it); unset is off |
| `GRAFANA_OIDC_CLIENT_ID` | master | setting | client id of the Logto application for Grafana |
| `GRAFANA_OIDC_CLIENT_SECRET` | master | setting | **a secret**: that application's client secret (grafana, `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET`). Listed here, not in the table above, because the inventory test requires every secret row to be in the file and the captain adds this one only when enabling sign-in; move the row up with its rotation columns then (`sops set`, never in clear) |
| `EDGE_OIDC_CLIENT_ID` | master | setting | client id of the Logto application for the tailnet sign-in gate (edge `oauth2-proxy`); `scripts/deploy.sh` brings compose project `edge` up only when all three `EDGE_*` are set |
| `EDGE_OIDC_CLIENT_SECRET` | master | setting | **a secret**: that application's client secret (edge `oauth2-proxy`, `OAUTH2_PROXY_CLIENT_SECRET`). Listed here for the same reason as `GRAFANA_OIDC_CLIENT_SECRET`: the captain adds it only when enabling the gate; move the row up with its rotation columns then (class oauth-client) |
| `EDGE_COOKIE_SECRET` | master | setting | **a secret**: encrypts and signs the sign-in gate's session cookie (edge `oauth2-proxy`, `OAUTH2_PROXY_COOKIE_SECRET`; `openssl rand -hex 16`). Same reason; move it up as class cookie-secret when added |
| `GRAFANA_PORT` | master | setting | loopback port |
| `PROMETHEUS_PORT` | master | setting | loopback port |
| `LOKI_PORT` | master | setting | loopback port |
| `LIFEKIT_GRAFANA_VOLUME` | master | setting | external volume name |
| `LIFEKIT_PROMETHEUS_VOLUME` | master | setting | external volume name |
| `LIFEKIT_LOKI_VOLUME` | master | setting | external volume name |
| `LIFEKIT_TEMPO_VOLUME` | master | setting | volume name |
| `LIFEKIT_OTEL_FILELOG_VOLUME` | master | setting | volume name |
| `LIFEKIT_FINANCE_SENTRY_DASHBOARDS` | master | setting | host path finance-sentry drops dashboards in |
| `DOCKER_GID` | master | setting | docker socket group id |
| `LIFEKIT_DASHBOARD_PORT` | master | setting | dashboard loopback port |
| `LIFEKIT_DASHBOARD_DIR` | master | setting | dashboard checkout path |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | master | setting | optional OTLP endpoint |
| `OTEL_SERVICE_NAME` | master | setting | optional OTLP service name |
| `OPENCLAW_IMAGE` | master | setting | no consumer found in this repo (live env only) |
| `LIFEKIT_WAKE_INTERVAL_SECONDS` | master | setting | no consumer found in this repo (live env only) |
| `LIFEKIT_LOG_LEVEL` | master | setting | no consumer found in this repo (live env only) |
| `NODE_OPTIONS` | master | setting | no consumer found in this repo (live env only) |
| `LIFEKIT_DOCKER_GID` | master | setting | no consumer found in this repo (live env only) |
| `LIFEKIT_DEVCLAW_WORKSPACES` | master | setting | no consumer found in this repo (live env only) |
| `DEVCLAW_GOAL_GRILL` | master | setting | no consumer found in this repo (live env only) |
| `DEVCLAW_RELAY_POLL` | master | setting | no consumer found in this repo (live env only) |
| `DEVCLAW_GOAL_AUTOMERGE` | master | setting | no consumer found in this repo (live env only) |
| `DEVCLAW_NOTIFY_ALTITUDE` | master | setting | no consumer found in this repo (live env only) |
| `DEVCLAW_SELF_ISSUE_MIN_CYCLES` | master | setting | no consumer found in this repo (live env only) |

## Not in these files, and why

| Credential | Why not | Recovery |
| --- | --- | --- |
| The two age private keys | they are the roots; the captain key is on the admin account and in KeePassXC, the gateway key is minted per box (`scripts/secrets/init-gateway-key.sh`) and also copied to KeePassXC | KeePassXC |
| Claude Code refreshing login (`/home/lifekit/.claude`), the Codex OAuth profile, the Google Workspace MCP refresh token | OAuth refresh material rotates on use; OpenClaw excludes OAuth profiles from SecretRefs | interactive login; `scripts/google-mcp-bootstrap.sh` |
| Tailscale node state and auth keys | node identity is per machine; a stored auth key is a standing join credential. Key expiry for `lifekit-vps` is disabled in the admin console instead. | single-use key at rebuild |
| GitHub Actions runner registrations | re-mintable, per registration | fresh registration token |
| OpenClaw internal material (`config-journal-fingerprint.key`, device and pairing records, session SQLite) | runtime-minted state | off-box `openclaw backup` |
| ghcr job logins | job-scoped, must not persist (`scripts/deploy.sh` uses a private empty docker config) | none needed |
| SSH host keys | regenerating is acceptable | update `known_hosts` |
| Kit relay SSH key (`/srv/lifekit-secrets/kit-relay`) | per box, re-mintable; the forced command and `from=` limit it to the inbox | re-run the key steps in `docs/runbook.md`, "Kit's second-mate relay" |
| The GitHub App private key (`RELEASE_APP_PRIVATE_KEY`) | lives in GitHub, re-mintable | app settings |
| finance-sentry's values | their one home is finance-sentry's `docker/.env.sops`, same captain key | that repo |
| devclaw's delivery secrets (`CLAUDE_CODE_OAUTH_TOKEN`, `GH_TOKEN`, `NODE_AUTH_TOKEN`) | GitHub Actions secrets of the devclaw repo; its deploy writes them | `gh secret set` in that repo |
| lifekit-dashboard's env (`/srv/dashboard/.env`) | that repo's deploy owns it; it carries a pair of `OPENCLAW_GATEWAY_TOKEN` | that repo |
| XUI (`/etc/xui/.env`) | not lifekit; ruled out 2026-09-16 | its own backup |
| The admin account's own tool logins (Claude Code, `gh`) | personal operator tooling | re-login |

## Boundary rule, for the next secret

1. Does exactly one consumer use it, is that consumer the OpenClaw gateway,
   and does OpenClaw take it as a SecretRef (`docs/reference/secretref-credential-surface`
   in the image)? Then **gateway**: `scripts/secrets/edit.sh gateway`, a
   `botToken`-style ref in `platform.patch.json`, a row above.
2. Otherwise **master**: `scripts/secrets/edit.sh master`, the consumer reads
   it from the rendered env through compose, a row above.
3. No consumer? `PARKED_` prefix, master, a row above, and a date by which it
   gets a consumer or is deleted.

Never a third file, never a value in `openclaw.json`, never a value in a PR.
