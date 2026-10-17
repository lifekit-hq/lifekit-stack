#!/usr/bin/env python3
"""People: invite someone and see who has what (Logto half).

    people.py invite EMAIL --right ID [--right ID]... [--host HOST] [--invite-url URL]
    people.py status [EMAIL]

invite makes sure the Logto user exists (primary email, no password: they sign
in with Google or an emailed code) and holds one Logto role per right, then
prints the invite email for the owner to send. It never sends mail. Every write
goes through scripts/identity/logto-admin.py, so it lands in that script's
ledger with its undo data; invite is idempotent and refuses an unknown right id
before any write. An email needs the host the products are served on, so HOST
(--host or PEOPLE_HOST, a tailnet DNS name) is required; it stays out of the
repo. --invite-url is the Tailscale invite link; without one the email carries
a marked placeholder line for the owner to fill.

status shows each user's Logto roles and suspended flag, and drift: a user
holding no product role from the catalog (platform roles such as admin are
exempt), or a suspended user who still holds one. With EMAIL it shows that
one user; without, every user.

The rights catalog is scripts/people/rights.json (override: PEOPLE_RIGHTS).
Credentials and endpoint are logto-admin.py's: LOGTO_M2M_APP_ID,
LOGTO_M2M_APP_SECRET, LOGTO_ENDPOINT, LOGTO_ADMIN_LEDGER.

Exit codes: 0 done, 1 an API or catalog error, 2 usage.
Tests: scripts/tests/test_people.py, against a fake Management API.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "rights.json"
TEMPLATE = HERE / "invite-email.txt"


def _load_admin():
    spec = importlib.util.spec_from_file_location(
        "logto_admin", HERE.parent / "identity" / "logto-admin.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


admin = _load_admin()
AdminError = admin.AdminError


def load_catalog(path: Path) -> dict:
    try:
        cat = json.loads(path.read_text())
        rights = cat["rights"]
        for rid, r in rights.items():
            r["name"], r["role"], int(r["port"])
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise AdminError(f"rights catalog {path} is unusable: {err!r}") from None
    for key in ("always_ports", "never_ports", "platform_roles"):
        cat.setdefault(key, [])
    for rid, r in rights.items():
        if int(r["port"]) in cat["never_ports"]:
            raise AdminError(f"right {rid!r} is on a never-grant port {r['port']}")
    return cat


def pick_rights(cat: dict, ids: list[str]) -> list[tuple[str, dict]]:
    unknown = [i for i in ids if i not in cat["rights"]]
    if unknown:
        known = ", ".join(sorted(cat["rights"]))
        raise AdminError(f"unknown right {', '.join(unknown)} (known: {known})")
    return [(i, cat["rights"][i]) for i in dict.fromkeys(ids)]


def render_email(
    email: str, rights: list[tuple[str, dict]], host: str, invite_url: str | None
) -> str:
    names = [r["name"] for _, r in rights]
    title = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    lines = []
    for _, r in rights:
        lines.append(f"   - {r['name']}: https://{host}:{r['port']}")
        if r.get("hint"):
            lines.append(f"     {r['hint']}")
    if invite_url:
        invite = (
            f"Open this link and sign in with {email}:\n   {invite_url}\n"
            "   Use that same account for every step below."
        )
    else:
        invite = (
            "Open your Tailscale invite link and sign in with "
            f"{email}:\n   [PASTE THE TAILSCALE INVITE LINK HERE]"
        )
    text = TEMPLATE.read_text()
    for key, value in {
        "products_title": title,
        "invite_step": invite,
        "open_what": "it" if len(rights) == 1 else "each one",
        "products": "\n".join(lines),
        "email": email,
        "short_host": host.split(".")[0],
    }.items():
        text = text.replace("{{ " + key + " }}", value)
    return text


def cmd_invite(api, ledger, args, cat: dict) -> None:
    rights = pick_rights(cat, args.right)
    host = (args.host or os.environ.get("PEOPLE_HOST") or "").strip()
    if not host:
        raise AdminError("no host for the email: pass --host or set PEOPLE_HOST")
    email = args.email.strip().lower()
    if "@" not in email or " " in email:
        raise AdminError(f"{args.email!r} is not an email address")
    user, n = admin.ensure_user(api, ledger, email)
    admin.say(
        f"user {user['id']}: " + ("exists" if n is None else f"created, ledger #{n}")
    )
    for rid, r in rights:
        role, n = admin.ensure_role(
            api, ledger, r["role"], f"{r['name']} access ({rid})"
        )
        if n is not None:
            admin.say(f"role {r['role']}: created ({role['id']}), ledger #{n}")
        n = admin.grant_role(api, ledger, user["id"], role)
        admin.say(
            f"right {rid}: role {r['role']} "
            + ("already held" if n is None else f"assigned, ledger #{n}")
        )
    admin.say("")
    admin.say("--- invite email (draft; send it yourself) ---")
    admin.say(render_email(email, rights, host, args.invite_url))


def user_row(api, user: dict, cat: dict) -> dict:
    roles = sorted(r["name"] for r in api.list_all(f"/api/users/{user['id']}/roles"))
    product = {r["role"] for r in cat["rights"].values()}
    held = [r for r in roles if r in product]
    drift = []
    if not held and not set(roles) & set(cat["platform_roles"]):
        drift.append("no product role")
    if user.get("isSuspended") and held:
        drift.append("suspended but still holds a product role")
    return {
        "user": user,
        "roles": roles,
        "drift": drift,
    }


def cmd_status(api, _ledger, args, cat: dict) -> None:
    if args.email:
        users = [admin.find_user(api, args.email.strip().lower())]
    else:
        users = api.list_all("/api/users")
    rows = [user_row(api, u, cat) for u in users]
    for row in rows:
        u = row["user"]
        admin.say(
            f"{u.get('primaryEmail') or u.get('username') or u['id']}: "
            f"roles [{', '.join(row['roles']) or '-'}]"
            f"{', SUSPENDED' if u.get('isSuspended') else ''}"
        )
    drifted = [r for r in rows if r["drift"]]
    if drifted:
        admin.say("drift:")
        for row in drifted:
            u = row["user"]
            who = u.get("primaryEmail") or u["id"]
            admin.say(f"  {who}: {'; '.join(row['drift'])}")
    else:
        admin.say("drift: none")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="people.py", description="Invite people and see who has what."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser(
        "invite", help="ensure the Logto user and roles, print the email"
    )
    i.add_argument("email")
    i.add_argument("--right", action="append", required=True, metavar="ID")
    i.add_argument("--host", help="host the products are served on (or PEOPLE_HOST)")
    i.add_argument("--invite-url", help="the Tailscale invite link for the email")
    s = sub.add_parser("status", help="Logto roles, suspended flag and drift")
    s.add_argument("email", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    ledger = admin.Ledger(
        Path(
            os.environ.get("LOGTO_ADMIN_LEDGER")
            or Path.home() / ".local/state/lifekit/logto-admin.ledger.jsonl"
        )
    )
    app_id = os.environ.get("LOGTO_M2M_APP_ID", "")
    app_secret = os.environ.get("LOGTO_M2M_APP_SECRET", "")
    if not app_id or not app_secret:
        print(
            "people: LOGTO_M2M_APP_ID and LOGTO_M2M_APP_SECRET must be set",
            file=sys.stderr,
        )
        return 2
    api = admin.Api(
        os.environ.get("LOGTO_ENDPOINT") or "http://127.0.0.1:3001", app_id, app_secret
    )
    try:
        cat = load_catalog(Path(os.environ.get("PEOPLE_RIGHTS") or CATALOG))
        {"invite": cmd_invite, "status": cmd_status}[args.cmd](api, ledger, args, cat)
    except AdminError as err:
        print(f"people: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
