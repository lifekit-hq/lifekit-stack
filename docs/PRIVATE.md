# What NEVER belongs in this repo

This is the audit checklist. Before every commit, scan for anything matching this list. The `gitleaks` pre-commit hook catches most of it automatically, but the human pass catches the rest.

## Hard exclusions (gitleaks-enforced)

- Telegram bot tokens (`bot[0-9]+:[A-Za-z0-9_-]+`)
- Anthropic API keys (`sk-ant-[A-Za-z0-9-]+`)
- OpenAI API keys (`sk-[A-Za-z0-9]+`, `sk-proj-[A-Za-z0-9_-]+`)
- AWS / GCP / Azure credentials
- SSH private keys (`BEGIN OPENSSH PRIVATE KEY`, `BEGIN RSA PRIVATE KEY`, etc.)
- Tailscale auth keys (`tskey-auth-[A-Za-z0-9]+`)
- JWT tokens, OAuth refresh tokens
- Plaid client_id / secret, Binance API keys, Monobank tokens, IBKR credentials
- `.env` files with any of the above
- Any file named `auth-profiles.json`, `secret`, `*.pem`, `*.key`

## Hard exclusions (manual audit required)

- **Contents of `~/.life/`** — domains, journal, queue, scout ledger. Never. Personal knowledge layer stays on your machine.
- **The encryption key for OpenClaw auth profiles** (`OPENCLAW_AUTH_PROFILE_SECRET_DIR` contents).
- **Tailscale node identities** (`tailscaled.state`).
- **Generated `wizard.yaml`** — even though it doesn't contain raw secrets, it does contain personal context (your name, timezone, skill selections, Telegram user ID). The template includes `wizard.example.yaml` only.

## Personal information that must be parameterized, not baked in

When auditing a workspace skill before adding it to `skills/`:

- ❌ Personal names (yours, family members, colleagues, friends)
- ❌ Specific addresses, gym names, restaurant names, place names
- ❌ Account numbers, brokerage accounts, exchange handles
- ❌ Phone numbers, email addresses (other than the maintainer's own in `LICENSE`/`CONTRIBUTING.md`)
- ❌ Specific dietary constraints (e.g. "gluten-intolerant" — should be `{{ user.dietary.constraints }}`)
- ❌ Specific work / employer / project details
- ❌ Specific calendar event names from your real calendar
- ❌ Chat IDs, channel IDs, group IDs (other than placeholder `123456789`)
- ❌ Tailscale node names (`{{ host.tailscale_name }}` instead)

Everything personal lives in the user-context YAML the wizard generates locally. Templates read it via `{{ user.* }}` substitutions.

## What IS allowed in this repo

- ✅ `secrets/*.env.sops` — SOPS-encrypted dotenv files (values are ciphertext, names are the inventory in [`secrets.md`](./secrets.md)). Never a decrypted copy, never a `.agekey`.

- ✅ Maintainer's name + email in `LICENSE` and `.github/CONTRIBUTING.md`.
- ✅ Example/placeholder values (`example.com`, `your-bot-name`, `123456789`).
- ✅ Documentation that references the structure of a domain file but not its contents.
- ✅ Skill prompts written for the general case, with `{{ user.* }}` substitutions for any user-specific values.
- ✅ Default values (`defaults.yaml`) per skill — sensible generic defaults so the skill works without user input.

## Pre-commit gate

Every commit runs:

```bash
pre-commit run --all-files
```

Which includes:

- `gitleaks` — secret scanning.
- `ruff` — lint Python.
- `yamllint` — lint YAML.
- `shellcheck` — lint shell scripts.
- `hadolint` — lint Dockerfiles.

CI repeats all of these on push. Any leak that escapes the local hook gets caught here.

## If a leak makes it to a commit

1. **Do not push.** If already pushed: rotate the leaked credential immediately.
2. Force-rewrite history to remove the leaked content (`git filter-repo` or BFG Repo-Cleaner).
3. Open a security advisory if the leak was exposed publicly.
4. Add the leaked pattern to `gitleaks.toml` if it wasn't caught — and write a regression test.

## When in doubt

Default to keeping it out. If a piece of data feels borderline, treat it as personal and parameterize it. The cost of an extra `{{ user.* }}` substitution is zero; the cost of a leak is hours of rotation + a public record of the slip.
