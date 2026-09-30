# Quickstart

This walks you through deploying `lifekit-stack` to a fresh Debian/Ubuntu VPS in about 15 minutes. The prose below stays provider-neutral; the [Reference walkthrough](#reference-walkthrough) at the bottom has the exact copy-pasteable commands for the tested combination (host, mesh VPN, chat transport).

> **Status:** wizard is under active development. Some steps below are aspirational until v0.1.0 ships. See the [main README](../README.md) for current status.

## Before you start

You'll need:

1. **A VPS** running Debian or Ubuntu 24.04 with at least 2GB RAM. Other Debian-family providers should work but aren't actively tested in v0.x. See the [Reference walkthrough](#reference-walkthrough) for the exact host/plan the maintainer runs.
2. **A mesh-VPN account** with an unattended-join auth key. Ephemeral or reusable, your call.
3. **A chat-transport bot** that supports long-polling (or webhook) delivery, plus your own user ID for the owner allowlist. The [Reference walkthrough](#reference-walkthrough) has the concrete bot-creation steps for the tested transport.
4. **Anthropic auth** — either:
   - A working [Claude CLI](https://docs.claude.com/claude-code) login on your laptop (preferred — no API key needed), or
   - An Anthropic API key.
5. **A laptop with [`pipx`](https://pipx.pypa.io/) installed.**

Optional but recommended:

- A domain name — not needed in v0.x (long-polling is the default), but if you ever want webhook mode it's required.
- OpenAI API key — only if you want Codex CLI integration for autonomous code builds.

## Step 1 — Install the wizard

```bash
pipx install lifekit
lifekit --version    # should print 0.1.0 or newer
```

## Step 2 — Clone the template

```bash
git clone https://github.com/lifekit-hq/lifekit-stack.git
cd lifekit-stack
```

## Step 3 — Run the wizard

```bash
lifekit init-stack
```

The wizard will:

1. **Check prerequisites.** Verifies Docker is available on the target, the mesh-VPN auth key is valid, the chat-transport bot token works, Anthropic auth resolves.
2. **Collect identity.** Your name, email, timezone, preferred language.
3. **Collect tokens.** Chat transport, Anthropic, optional OpenAI, optional finance providers.
4. **Let you select skills.** Multi-select from the bundled skills in `skills/`. Sensible defaults; opt out of anything you don't want.
5. **Optionally bootstrap `~/.life/` domains.** If you have a `~/.claude/CLAUDE.md` describing yourself, the wizard can draft your domain files from it (reuse of existing `lifekit onboard`).
6. **Render templates.** Generates real config files from the Jinja2 templates in `compose/` and `skills/` using the values you provided.
7. **Provision the VPS.** Runs `scripts/bootstrap-vps.sh` on the host (installs Docker, the mesh-VPN client, UFW, creates directories). The script is idempotent — re-running it is safe.
8. **Start the stack.** Brings up the Docker Compose stack (`OpenClaw gateway` + `lifekit-curator` + your selected skills).
9. **Verify.** Runs `openclaw doctor`, `openclaw health`, sends a synthetic message over the chat transport, confirms round-trip.

If anything fails, the wizard surfaces the error and tells you what to try next.

## Step 4 — First message

Open your chat client, find your bot, and say hello. The bot should respond. Try:

- `/help` — see what your Kit can do.
- `Hi Kit, what's on my calendar today?` — if you wired calendar.
- `Log: I went for a 5km run` — capture into `queue.jsonl`.

## Step 5 — Mount `~/.life/` on your laptop (optional but recommended)

If you want to read/edit your knowledge layer from your laptop:

```bash
# On the laptop
sudo apt install sshfs
mkdir -p ~/.life
sshfs lifekit@<your-vps-mesh-name>:/srv/life ~/.life
```

For auto-mount on boot, see [`docs/runbook.md`](./runbook.md#sshfs-auto-mount).

## Updating

Pull the template and re-run the wizard:

```bash
cd lifekit-stack
git pull
lifekit init-stack --target <your-vps-mesh-name>
```

The wizard reads `wizard.yaml` from the target (saved on first run), reapplies templates, and restarts only services touched by the changeset. Idempotent.

## Troubleshooting

See [`docs/runbook.md`](./runbook.md) for common issues — failed mesh-VPN joins, OpenClaw not starting, chat-transport silence, queue.jsonl growing without drain, etc.

## Getting help

- [GitHub Issues](https://github.com/lifekit-hq/lifekit-stack/issues) — bugs and feature requests.
- [GitHub Discussions](https://github.com/lifekit-hq/lifekit-stack/discussions) — questions and ideas.
- [OpenClaw docs](https://docs.openclaw.ai/) — for runtime-specific questions.

## Reference walkthrough

The exact provider/tooling combination the maintainer runs and tests against. Each of these is a swap-point via the adapter ports in [`docs/architecture.md`](./architecture.md#2-adapter-pattern-for-every-replaceable-component); the rest of the stack doesn't know or care which provider sits behind the port.

### Host — arm64 VPS

An arm64 VPS (10 vCPU / 15.6 GiB RAM), Debian 13. Provision it through your provider's console or CLI, with your SSH key, then run `scripts/bootstrap-vps.sh` on it.

### Mesh VPN — Tailscale

[Tailscale](https://tailscale.com/), with an [auth key](https://login.tailscale.com/admin/settings/keys) (ephemeral or reusable, your call). The wizard / `bootstrap-vps.sh` accepts the key via `TAILSCALE_AUTH_KEY=tskey-auth-...` and joins the host to your tailnet headlessly.

### Chat transport — Telegram

Telegram long-polling — the gateway dials out to Telegram, no inbound webhook needed.

- DM [@BotFather](https://t.me/BotFather), send `/newbot`, choose a name. Save the token.
- DM [@userinfobot](https://t.me/userinfobot) to get your own numeric Telegram user ID. Save it — this becomes the owner allowlist.

That's the full tested path; everything else in this document is intentionally written so a different host / mesh VPN / chat transport can drop in without doc churn.
