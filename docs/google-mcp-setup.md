# Google Workspace MCP — one-time setup

There are **two** Workspace connectors, two instances of the same upstream image
([taylorwilsdon/google_workspace_mcp]), each with its own Google account, OAuth
client, refresh token and credentials directory:

| | Kit's connector | The fleet connector |
| --- | --- | --- |
| Compose service | `google-workspace-mcp` | `google-workspace-mcp-fleet` |
| Signs in as | the owner's personal Google account | the fleet Gmail account |
| Reach | Gmail (read and send), Calendar, Docs, Sheets, Tasks and Drive, all read-only except `send_gmail_message` | Drive only; create only (`create_drive_folder`, `create_drive_file`) |
| Cloud project and client | the owner's (`GOOGLE_OAUTH_CLIENT_*`, `USER_GOOGLE_EMAIL`) | the fleet's own (`FLEET_GOOGLE_OAUTH_CLIENT_*`, `FLEET_GOOGLE_EMAIL`) |
| Token store | `/srv/google-workspace-mcp/credentials/` | `/srv/google-workspace-mcp-fleet/credentials/` |
| Internal URL | `http://google-workspace-mcp:8000/mcp/` | `http://google-workspace-mcp-fleet:8000/mcp/` |
| Owner of the account and its revocation | the owner | the owner (see "The fleet connector") |

Sections 1 to 9 below set up **Kit's** connector; "The fleet connector" after them
lists what differs for the second one. Both run single-user, streamable-HTTP on
`:8000` inside the compose network and are consumed by `openclaw-gateway` over
that internal network. Neither port is published to the host.

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

Kit's compose service sets the tool surface with literals, not env values:
`WORKSPACE_MCP_PERMISSIONS` is `gmail:send` plus `readonly` for Drive, Calendar,
Docs, Sheets and Tasks, and `WORKSPACE_MCP_DISABLED_TOOLS` removes the other
write tools the Gmail `send` level pulls in: `draft_gmail_message`,
`manage_gmail_label`, `modify_gmail_message_labels`,
`batch_modify_gmail_message_labels`. The block-by-name list wins over the
permission list, so the only tool that writes anywhere is `send_gmail_message`.
Kit's connector is read-only for Drive on purpose: every Drive write is the fleet
connector's (below). This is the guard that binds: per-agent tool allow and deny
lists are not enforced under the claude-cli runtime. `TOOLS` stays an empty
literal: a non-empty value puts `--tools` on the start command and the server
then ignores permissions (full mode); `WORKSPACE_MCP_TOOLS` is no longer read.
Every call is logged and send pings the owner through the `agent-mail-sent`
Grafana alert
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
   new token (step 7). The sign-in asks only for the scopes the running config
   needs, so it requests `gmail.send` only once the read-only flag is gone: run
   it after the compose change is live. Then check the granted scopes include
   `gmail.send`.

### Drive consent moved to the fleet connector

Until 2026-11-08 Kit's connector held the full Drive level so the document door
could file originals. That moved: Kit's connector is `drive:readonly` and the
document door sends every Drive call to `google-workspace-mcp-fleet`, which is
where the full Drive consent now lives ("The fleet connector"). A refresh token
that Kit's connector already holds with the wider Drive scope keeps working at
the narrower config and no new consent is needed to go down. To be thorough,
revoke the app's access on the owner's Google account third-party access page
and redo step 6 with the narrower flag
(`--permissions gmail:send drive:readonly calendar:readonly docs:readonly sheets:readonly tasks:readonly`).

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

## The fleet connector

`google-workspace-mcp-fleet` gives the fleet its own Google identity instead of
borrowing the owner's consent per scope (owner decision 2026-11-08). It is the
same image and the same flow as steps 1 to 9, with these differences.

**What it is.** A Google account of the fleet's own (the fleet Gmail), a Cloud
project of its own, an OAuth client of type *Desktop app* in that project, and
the consent screen *In production* (step 3) so its refresh token does not
expire after 7 days. Only the Drive API needs enabling. The tool surface is the
literal `drive:full` with the eight other Drive write tools blocked by name
(`update_drive_file`, `manage_drive_access`, `set_drive_file_permissions`,
`get_drive_shareable_link`, `copy_drive_file` and the three `import_to_google_doc`,
`import_to_google_sheets`, `import_to_google_slides` tools, which also create
files; `compose/openclaw/docker-compose.yml`): it can read Drive and create folders and
files, and cannot edit, move, share, copy or delete anything. Files land in the
fleet account's Drive, or in a folder the owner shares with the fleet account
(share it with Editor access; the connector can then create inside it, and the
document door can be pointed at it with `DOC_DOOR_DRIVE_PARENT_ID`, default
`root`, the fleet account's own Drive).

**Who owns it.** The owner owns the account, the Cloud project and the
revocation. Record, once, when the account is first signed in: whether its
recovery phone and 2-Step Verification belong to the owner, and put the answer in
this section (it is not known from the repo). If they do not, move them to the
owner before relying on the account: a fleet identity the owner cannot recover
is not the owner's to revoke.

**Differences from Kit's setup.**

- Secrets are `FLEET_GOOGLE_OAUTH_CLIENT_ID`, `FLEET_GOOGLE_OAUTH_CLIENT_SECRET`
  and `FLEET_GOOGLE_EMAIL` in the master file (step 5, same commands), never the
  `GOOGLE_OAUTH_*` / `USER_GOOGLE_EMAIL` names. The two `oauth-client`
  inventory rows are added to `docs/secrets.md` in the same change that stores
  the values in the master file (`scripts/tests/test_secrets.py` holds the pair).
- On the VPS: `sudo /srv/lifekit-stack/scripts/google-mcp-bootstrap.sh fleet`
  creates `/srv/google-workspace-mcp-fleet/credentials/` (mode 0700, owner
  `lifekit`). Run it before the first sign-in; the compose service starts without
  it and without credentials, and answers `/health`, so the merge is safe to land
  first.
- Step 6 on the PC, signed in as the fleet account, with only Drive asked for:

  ```bash
  export GOOGLE_OAUTH_CLIENT_ID=<the fleet client id>
  export GOOGLE_OAUTH_CLIENT_SECRET=<the fleet client secret>
  export USER_GOOGLE_EMAIL=<the fleet account>
  export OAUTHLIB_INSECURE_TRANSPORT=1
  uvx workspace-mcp --transport streamable-http --single-user \
      --permissions drive:full
  ```

  The sign-in is the one step only a person can take: the fleet account's
  password, any re-verification and 2-Step Verification prompt, and the consent
  click. Nothing here scripts or works around any of it. Check the granted scopes
  include the Drive scope.
- Step 7 copies the token to `/srv/google-workspace-mcp-fleet/credentials/`; step 8
  brings up `google-workspace-mcp-fleet`.
- Step 9 registers a second MCP server, keyed `google-workspace-fleet` in the
  `mcp.servers` object (the live schema is an object keyed by server id), by
  applying the body of
  [`compose/openclaw-gateway/mcp-servers/google-workspace-fleet.json`](../compose/openclaw-gateway/mcp-servers/google-workspace-fleet.json)
  with `openclaw config patch` (host state, an operator step). Its tools then
  appear as `google-workspace-fleet__*`. The document door does not need the
  entry (it speaks MCP to the fleet connector itself); an agent that should call
  fleet Drive tools does.

**Rehearsal, in this order.** Read first: with the token in place, a
`search_drive_files` (or `list_drive_items` on a folder the owner shared) over the
internal URL returns the folder list and writes nothing. Then one write: create a
folder, and a small file in it, inside a folder the owner shared with the fleet
account, and confirm the owner sees both in Drive. A Drive create is logged as
`[create_drive_folder]` / `[create_drive_file]` and raises the warning
`agent-drive-created`; that is the alert working, not a fault.

```bash
docker compose -p openclaw -f /srv/lifekit-stack/compose/openclaw/docker-compose.yml \
    exec openclaw-gateway curl -fsS http://google-workspace-mcp-fleet:8000/health
docker logs --since 10m openclaw-google-workspace-mcp-fleet-1 | grep -E 'create_drive|Invoked|invalid_grant'
```

**Verify Kit's connector is narrowed.** After the deploy,
`docker logs openclaw-google-workspace-mcp-1 | grep -c "Block list"` shows the
four Gmail names and none of the Drive ones, and `create_drive_file` is not
offered by Kit's connector.

**Rollout order.** (1) Merge: the deploy recreates `openclaw-google-workspace-mcp-1`
at `drive:readonly` (Kit's token and Gmail/Calendar reads are unaffected) and
starts the fleet connector without credentials; the gateway itself is not
recreated for this change, since nothing in the gateway's service definition
changes. Until the fleet sign-in is in, the document door reports
`Drive: pending (...)` and retries on its next scan. (2) The owner-side steps
above. (3) The next scan files the pending originals.

## Revoking either connector

Revoking stops the connector at Google, whatever the VPS holds.

- **Kit's:** the owner's Google account, *Security → Third-party apps with
  account access* (or *Manage third-party access*), remove the app; then delete
  the files in `/srv/google-workspace-mcp/credentials/`.
- **The fleet's:** the same page on the fleet account, remove the app; delete the
  files in `/srv/google-workspace-mcp-fleet/credentials/`; for a full stop also
  disable the Drive API or delete the OAuth client in the fleet's Cloud project.
  The owner can do this alone, because the account and the project are the
  owner's. Files the fleet created stay in Drive until the owner deletes them;
  folders the owner shared can be un-shared in Drive.
- Then stop the container (`docker compose -p openclaw ... stop
  google-workspace-mcp-fleet`) or remove its service in a PR. The document door
  keeps reporting `Drive: pending` and never fails a scan.

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

**Token expired.**  If a connector's container logs `invalid_grant` errors (Kit's:
`openclaw-google-workspace-mcp-1`; the fleet's:
`openclaw-google-workspace-mcp-fleet-1`), its refresh token is dead. The usual cause is a consent screen still in *Testing*
status, where Google expires refresh tokens after 7 days: check **Google Auth
Platform → Audience**, choose **Publish app** if it says *Testing*, then
repeat steps 6–7 to regenerate. Gmail tools then answer "ACTION REQUIRED: Google
Authentication Needed" and Kit's needs-you digest shows `mail unavailable:` until the
owner completes the consent in a browser.
