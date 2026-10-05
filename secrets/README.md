# secrets/

Two SOPS-encrypted dotenv files and nothing else. Key names are plaintext,
values are `ENC[...]`, recipients are in `../.sops.yaml`.

| File | Boundary | Opens with |
| --- | --- | --- |
| `lifekit.env.sops` | master: everything compose interpolates, plus parked secrets | an operator key (captain or firstmate) |
| `lifekit-gateway.env.sops` | gateway: the bot tokens the OpenClaw gateway resolves itself | the gateway key on the box, or an operator key (captain or firstmate) |

Edit with `scripts/secrets/edit.sh <master|gateway>`; the inventory in
`docs/secrets.md` and the sequences in `docs/secrets-runbook.md` are the
rules. `.gitignore` admits only `*.env.sops` here: a decrypted copy, a swap
file or a key can never be committed from this directory.
