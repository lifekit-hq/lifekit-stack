# Google Workspace MCP — one-time setup

This is the one-time setup for the `google-workspace-mcp` compose service
(upstream: [taylorwilsdon/google_workspace_mcp]). The service runs single-user,
streamable-HTTP on `:8000` inside the compose network, and is consumed by
`openclaw-gateway` over that internal network at
`http://google-workspace-mcp:8000/mcp/`. The port is **not** published to the
host.

Most of this happens once on your PC (the OAuth consent flow needs a browser).
The VPS just receives the resulting refresh token and brings the container up.

[taylorwilsdon/google_workspace_mcp]: https://github.com/taylorwilsdon/google_workspace_mcp

## Architecture recap

```
openclaw-gateway ──► google-workspace-mcp:8000/mcp/ ──► Google APIs
       (compose DNS, no host port)              (refresh token in /srv/...)
```

- **Service DNS name:** `google-workspace-mcp` (compose network).
- **Internal URL:** `http://google-workspace-mcp:8000/mcp/`.
- **Credentials on host:** `/srv/google-workspace-mcp/credentials/` (mode 0700,
  owned by `lifekit`).
- **Credentials inside container:** `/home/app/.google_workspace_mcp/credentials/`.
  This diverges from the upstream README's `~/.google_workspace_mcp/credentials`
  shorthand because the upstream image runs as the non-root user `app` whose
  home is `/home/app`, **not** `/root`. The bind-mount in `docker-compose.yml`
  targets the actual user home.

## 1. Create a Google Cloud project

Open <https://console.cloud.google.com/> → top bar → **New Project**. Name it
something memorable (e.g. `lifekit-workspace-mcp`). Select it.

## 2. Enable the workspace APIs

For each service you intend to use, enable the API in **APIs & Services →
Library**. The default `WORKSPACE_MCP_TOOLS=gmail drive calendar docs sheets tasks`
needs these six:

- [Gmail API](https://console.cloud.google.com/flows/enableapi?apiid=gmail.googleapis.com)
- [Drive API](https://console.cloud.google.com/flows/enableapi?apiid=drive.googleapis.com)
- [Calendar API](https://console.cloud.google.com/flows/enableapi?apiid=calendar-json.googleapis.com)
- [Docs API](https://console.cloud.google.com/flows/enableapi?apiid=docs.googleapis.com)
- [Sheets API](https://console.cloud.google.com/flows/enableapi?apiid=sheets.googleapis.com)
- [Tasks API](https://console.cloud.google.com/flows/enableapi?apiid=tasks.googleapis.com)

(Add Slides / Forms / Chat / People / Custom Search later if you broaden
`WORKSPACE_MCP_TOOLS`.)

The compose service also sets `WORKSPACE_MCP_READ_ONLY: "true"`. In
read-only mode the server drops every tool that needs a write scope (Gmail
send and draft, label and filter changes, every create, update and share), and
`tools/list` shrinks from 76 tools to 35 at 1.21.0. This is the guard that
binds: per-agent tool allow and deny lists are not enforced under the
claude-cli runtime. The start command passes `--tools`, so the server ignores
`WORKSPACE_MCP_PERMISSIONS`. The read-only flag needs no new consent, because
the existing refresh token's scopes are a superset of the read-only ones. To
allow writes again, change the literal in `compose/openclaw/docker-compose.yml`
in a reviewed PR.

## 3. Configure the OAuth consent screen

**APIs & Services → OAuth consent screen.**

- **User type:** *External* (unless you're on a Workspace tenant — then
  *Internal* is simpler).
- **App name:** anything, e.g. `lifekit`.
- **User support email** and **Developer contact:** your address.
- **Scopes:** leave empty here; the MCP requests them at runtime.
- **Test users:** add the address you'll set as `USER_GOOGLE_EMAIL`. This is
  what lets you complete consent while the app is in *Testing* status.

You do **not** need to publish the app. Testing-status consent screens issue
refresh tokens that work for the test users you added.

## 4. Create the OAuth client

**APIs & Services → Credentials → Create Credentials → OAuth client ID.**

- **Application type:** *Desktop app*.
- **Name:** `lifekit-workspace-mcp` (anything).

After creation, click **Download JSON** and note the **Client ID** and
**Client secret** shown in the modal — you'll add them to the master secrets file shortly.

## 5. Stash the client credentials

On your PC, export them in the shell you'll use for the next step:

```bash
export GOOGLE_OAUTH_CLIENT_ID="<paste client id>"
export GOOGLE_OAUTH_CLIENT_SECRET="<paste client secret>"
export USER_GOOGLE_EMAIL="you@example.com"
```

And put the same values into the master secrets file so the container can
refresh the token after expiry (see [`secrets.md`](./secrets.md)). On the VPS:

```bash
scripts/secrets/edit.sh master
sudo bash scripts/secrets/render-stack-env.sh   # -> /srv/lifekit-secrets/stack.env
```

The entries to add:

```bash
GOOGLE_OAUTH_CLIENT_ID=<paste>
GOOGLE_OAUTH_CLIENT_SECRET=<paste>
USER_GOOGLE_EMAIL=you@example.com
WORKSPACE_MCP_TOOLS=gmail drive calendar docs sheets tasks
```

## 6. Run the OAuth consent flow on your PC

The simplest path uses `uvx` from `astral-sh/uv`:

```bash
# One-time install if you don't have uv yet
pipx install uv   # or: curl -LsSf https://astral.sh/uv/install.sh | sh

# Run the server locally so it pops a browser for consent
export GOOGLE_OAUTH_CLIENT_ID=...
export GOOGLE_OAUTH_CLIENT_SECRET=...
export USER_GOOGLE_EMAIL=you@example.com
export OAUTHLIB_INSECURE_TRANSPORT=1   # needed for http://localhost callback
uvx workspace-mcp --transport streamable-http --single-user \
    --tools gmail drive calendar docs sheets tasks
```

On startup the server prints a `https://accounts.google.com/...` URL. Open it,
sign in as `USER_GOOGLE_EMAIL`, click through the "this app isn't verified"
warning (expected for Testing-status consent screens), and approve. The
callback writes the refresh token to:

```
~/.google_workspace_mcp/credentials/
```

Stop the local server (`Ctrl-C`) — the token is now on disk.

## 7. Copy the refresh token to the VPS

First make sure the host dir exists with the right owner/mode. On the VPS:

```bash
sudo /srv/lifekit-stack/scripts/google-mcp-bootstrap.sh
```

Then from your PC:

```bash
scp ~/.google_workspace_mcp/credentials/* \
    lifekit@<vps>:/srv/google-workspace-mcp/credentials/
```

(Replace `<vps>` with your tailnet hostname or IP.)

## 8. Bring the container up

On the VPS:

```bash
docker compose -p openclaw -f /srv/lifekit-stack/compose/openclaw/docker-compose.yml \
    --env-file /srv/lifekit-secrets/stack.env \
    up -d google-workspace-mcp
```

Watch it become healthy:

```bash
docker compose -p openclaw -f /srv/lifekit-stack/compose/openclaw/docker-compose.yml \
    ps google-workspace-mcp
docker compose -p openclaw -f /srv/lifekit-stack/compose/openclaw/docker-compose.yml \
    logs -f --tail=50 google-workspace-mcp
```

## 9. Register the MCP server with openclaw-gateway

Edit `/srv/openclaw/config/openclaw.json` on the VPS and add the snippet from
[`compose/openclaw-gateway/mcp-servers/google-workspace.json`](../compose/openclaw-gateway/mcp-servers/google-workspace.json)
into the `mcp.servers` array. The final entry should look like:

```json
{
  "id": "google-workspace",
  "transport": "streamable-http",
  "url": "http://google-workspace-mcp:8000/mcp/"
}
```

Then restart the gateway so it picks up the new MCP block:

```bash
docker compose -p openclaw -f /srv/lifekit-stack/compose/openclaw/docker-compose.yml \
    --env-file /srv/lifekit-secrets/stack.env \
    restart openclaw-gateway
```

## Troubleshooting

**Container is unhealthy.**  Check the logs — most failures here are credential
file path mismatches. The container expects credentials under
`/home/app/.google_workspace_mcp/credentials/`; verify the bind-mount in
`compose/openclaw/docker-compose.yml` points there and that the host dir is readable by the
`app` user inside the container.

**Verify the MCP server is reachable from inside the gateway.**

```bash
docker compose -p openclaw -f /srv/lifekit-stack/compose/openclaw/docker-compose.yml \
    exec openclaw-gateway curl -fsSv http://google-workspace-mcp:8000/mcp/
```

Any non-5xx response means the gateway can talk to the MCP container over the
compose network. A connection error means the gateway isn't on the same
network as the new service — restart the gateway.

**Verify the gateway exposes the tools.**

```bash
scripts/oclaw tools list | grep -E '^(gmail_|drive_|calendar_)'
```

You should see tools prefixed `gmail_`, `drive_`, `calendar_`, etc. If the
list is empty, the gateway didn't load the new `mcp.servers` entry —
double-check the JSON edit in step 9 and restart `openclaw-gateway`.

**`WORKSPACE_MCP_TOOLS` parsing issue.**  The upstream README documents this
var as comma-separated, but the published Docker image's CMD splits on
whitespace. The default in this stack is space-separated; if you see startup
errors about unrecognised tool names, try comma-separated instead
(`gmail,drive,calendar,docs,sheets,tasks`).

**Token expired.**  Refresh tokens generally don't expire while the OAuth
consent screen stays in *Testing* status — but Google's docs warn they can
be revoked after 7 days for un-verified apps in some configurations. If
the container starts logging `invalid_grant` errors, repeat steps 6–7 to
regenerate.
