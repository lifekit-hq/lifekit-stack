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
Library**. The compose file's tool surface (Gmail, Drive, Calendar, Docs, Sheets,
Tasks) needs these six:

- [Gmail API](https://console.cloud.google.com/flows/enableapi?apiid=gmail.googleapis.com)
- [Drive API](https://console.cloud.google.com/flows/enableapi?apiid=drive.googleapis.com)
- [Calendar API](https://console.cloud.google.com/flows/enableapi?apiid=calendar-json.googleapis.com)
- [Docs API](https://console.cloud.google.com/flows/enableapi?apiid=docs.googleapis.com)
- [Sheets API](https://console.cloud.google.com/flows/enableapi?apiid=sheets.googleapis.com)
- [Tasks API](https://console.cloud.google.com/flows/enableapi?apiid=tasks.googleapis.com)

(Add Slides / Forms / Chat / People / Custom Search later if you broaden the
permissions.)

The compose service sets the tool surface with literals, not env values:
`WORKSPACE_MCP_PERMISSIONS` is `gmail:send` plus `readonly` for Drive, Calendar,
Docs, Sheets and Tasks, and `WORKSPACE_MCP_DISABLED_TOOLS` removes the other
write tools that `gmail:send` pulls in (`draft_gmail_message`,
`manage_gmail_label`, `modify_gmail_message_labels`,
`batch_modify_gmail_message_labels`). The server then lists 36 tools at 2.1.0 and
the ONLY one that writes anywhere is `send_gmail_message`. This is the guard that
binds: per-agent tool allow and deny lists are not enforced under the
claude-cli runtime. `TOOLS` stays an empty literal: a non-empty value puts
`--tools` on the start command and the server then ignores permissions
(full mode); `WORKSPACE_MCP_TOOLS` is no longer read. Every call is logged and
pings the owner through the `agent-mail-sent` Grafana alert
(`compose/observability/grafana/provisioning/alerting/rules-agent-mail.yml`).
To change the surface, change the literals in `compose/openclaw/docker-compose.yml`
in a reviewed PR. When upstream ships `WORKSPACE_ALLOWED_RECIPIENTS`
([taylorwilsdon/google_workspace_mcp#1078](https://github.com/taylorwilsdon/google_workspace_mcp/pull/1078),
unmerged at 2.1.0), set it so a send can only reach a listed address.

### Widening the consent

Sending needs `gmail.send`, and the Gmail tools that stay registered at the
`gmail:send` level also need `gmail.modify`, `gmail.compose` and `gmail.labels`,
so the token must be issued by a consent that includes all of them; the token
from a read-only consent cannot send. Order matters:

1. Make sure the OAuth app is **In production** (step 3). A token issued while it
   is in *Testing* expires after 7 days.
2. Merge the compose change. The existing read-only token keeps working: mail
   stays readable, and a send is refused until the wider consent below is in.
3. Redo step 6 with the permissions flag shown there, sign in once, and copy the
   new token (step 7). Do this after the merge: the sign-in asks only for the
   scopes the running config needs, so it requests `gmail.send` only once the
   read-only flag is gone. Then check the granted scopes include `gmail.send`.

## 3. Configure the OAuth consent screen

**APIs & Services → OAuth consent screen.**

- **User type:** *External* (unless you're on a Workspace tenant — then
  *Internal* is simpler).
- **App name:** anything, e.g. `lifekit`.
- **User support email** and **Developer contact:** your address.
- **Scopes:** leave empty here; the MCP requests them at runtime.
- **Test users:** add the address you'll set as `USER_GOOGLE_EMAIL`. This is
  what lets you complete consent while the app is in *Testing* status.

**Publish the app to *In production*.** An External app left in *Testing*
status that asks for more than basic scopes (Gmail and Calendar are) gets
refresh tokens that Google expires after 7 days, and the MCP then fails with
`invalid_grant`. In the Cloud console open **Google Auth Platform → Audience**,
choose **Publish app**, and confirm; the status reads *In production*. Ignore
any prompt to submit the app for verification: an unverified app is fine for
personal use. LifeKit's OAuth app is In production and unverified on purpose and
will not be submitted for verification, so its refresh tokens do not expire after
7 days. Google's unverified-app warning on the consent screen is expected (choose
**Advanced**, then continue), and Google verification-failure notices can be
ignored. The privacy policy URL is
<https://github.com/lifekit-hq/.github/blob/main/PRIVACY.md>.

Do this before step 6. A refresh token issued while the app was still in
*Testing* keeps its 7-day life after you publish, so redo step 6 once the
status reads *In production*.

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
```

The tool surface is not an env value; it is pinned in
`compose/openclaw/docker-compose.yml`.

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
    --permissions gmail:send drive:readonly calendar:readonly \
    docs:readonly sheets:readonly tasks:readonly
```

On startup the server prints a `https://accounts.google.com/...` URL. Open it,
sign in as `USER_GOOGLE_EMAIL`, click through the "this app isn't verified"
warning (expected: the app is In production but unverified after step 3), and approve. The
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

**Tool list wrong or server in full mode.**  `WORKSPACE_MCP_PERMISSIONS` is
space-separated (`service:level`), `WORKSPACE_MCP_DISABLED_TOOLS` is
comma-separated. The container log at INFO level shows one
`Block list: disabling tool '<name>'` line per disabled tool; a banner saying
`Mode: Full` means `TOOLS` is non-empty or `WORKSPACE_MCP_TOOLS` leaked back in.

**Token expired.**  If the container logs `invalid_grant` errors, the refresh
token is dead. The usual cause is a consent screen still in *Testing*
status, where Google expires refresh tokens after 7 days: check **Google Auth
Platform → Audience**, choose **Publish app** if it says *Testing*, then
repeat steps 6–7 to regenerate. Gmail tools then answer "ACTION REQUIRED: Google
Authentication Needed" and Kit's needs-you digest shows `mail unavailable:` until the
owner completes the consent in a browser.
