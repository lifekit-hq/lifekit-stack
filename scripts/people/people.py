#!/usr/bin/env python3
"""People: invite someone and see who has what (Logto and tailnet halves).

    people.py invite EMAIL --right ID [--right ID]... [--host HOST]
                    [--invite-url URL] [--no-tailnet]
    people.py status [EMAIL]
    people.py sync [--dry-run]

invite makes sure the Logto user exists (primary email, no password: they sign
in with Google or an emailed code) and holds one Logto role per right, then
prints the invite email for the owner to send. It never sends mail. Every write
goes through scripts/identity/logto-admin.py, so it lands in that script's
ledger with its undo data; invite is idempotent and refuses an unknown right id
before any write. An email needs the host the products are served on, so HOST
(--host or PEOPLE_HOST, a tailnet DNS name) is required; it stays out of the
repo. --invite-url is the Tailscale invite link; without one the email carries
a marked placeholder line for the owner to fill.

invite also does the tailnet step when the Tailscale OAuth client is
configured (scripts/people/tailnet.py): it adds the email to group:<right> in
the policy's people-managed groups block (validated, written with If-Match),
and, when the email is not yet a tailnet member, creates a user invite for the
email's link with TAILSCALE_USER_TOKEN, or says to create the link in the admin
console and paste it (--invite-url) when that token is missing or rejected.
An unconfigured tailnet is reported and skipped, never an error.

status shows each user's Logto roles and suspended flag, and drift: a user
holding no product role from the catalog (platform roles such as admin are
exempt), or a suspended user who still holds one. With the tailnet configured
it adds each user's tailnet membership and groups, and drift between roles and
groups, missing grants, a policy with no tests, and tailnet members no Logto
user matches. With EMAIL it shows that one user; without, every user.

sync rebuilds the people-managed groups block from the Logto roles (the source
of truth; a suspended user is in no group). Nothing is written when the groups
already match. --dry-run prints the changes only.

The rights catalog is scripts/people/rights.json (override: PEOPLE_RIGHTS).
Credentials and endpoint are logto-admin.py's: LOGTO_M2M_APP_ID,
LOGTO_M2M_APP_SECRET, LOGTO_ENDPOINT, LOGTO_ADMIN_LEDGER. The tailnet credentials are TAILSCALE_OAUTH_CLIENT_ID /
_SECRET and TAILSCALE_USER_TOKEN, from the environment only (tailnet.py).

Exit codes: 0 done, 1 an API or catalog error, 2 usage.
Tests: scripts/tests/test_people.py and test_people_tailnet.py, against fake
Logto and Tailscale APIs.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import tailnet

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
    email: str,
    rights: list[tuple[str, dict]],
    host: str,
    invite_url: str | None,
    member: bool = False,
) -> str:
    names = [r["name"] for _, r in rights]
    title = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    lines = []
    for _, r in rights:
        lines.append(f"   - {r['name']}: https://{host}:{r['port']}")
        if r.get("hint"):
            lines.append(f"     {r['hint']}")
    if member and not invite_url:
        invite = (
            f"You are already on the tailnet as {email}, so there is no "
            "invite to accept: skip to the next step."
        )
    elif invite_url:
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


NOT_CONFIGURED = (
    "tailnet: not configured (set TAILSCALE_OAUTH_CLIENT_ID and "
    "TAILSCALE_OAUTH_CLIENT_SECRET): skipped"
)


def group_of(rid: str) -> str:
    return f"group:{rid}"


def norm(groups: dict[str, list[str]]) -> dict[str, list[str]]:
    return {g: sorted({e.lower() for e in m}) for g, m in groups.items()}


def check_policy(text: str, cat: dict) -> dict[str, list[str]]:
    """Parse the policy and refuse one whose grants open a never-grant port."""
    policy = tailnet.parse_policy(text)
    tailnet.refuse_never_ports(
        policy, [group_of(r) for r in cat["rights"]], cat["never_ports"]
    )
    return tailnet.managed_groups(text)


def tailnet_add(ts, cat: dict, email: str, rids: list[str]) -> list[str]:
    """Put email in group:<right> for each right. Returns the groups it joined."""
    order = [group_of(r) for r in cat["rights"]]
    joined: list[str] = []

    def mutate(text: str) -> str | None:
        have = check_policy(text, cat)
        merged = {g: list(m) for g, m in have.items()}
        for g in order:
            merged.setdefault(g, [])
        joined.clear()
        for rid in rids:
            if email not in {e.lower() for e in merged[group_of(rid)]}:
                merged[group_of(rid)].append(email)
                joined.append(group_of(rid))
        if norm(merged) == norm(have):
            return None
        return tailnet.with_groups(text, merged, order)

    ts.update_policy(mutate)
    return joined


def tailnet_invite(ts, cat: dict, email: str, rids: list[str], url: str | None):
    """The tailnet half of invite. Returns (invite url or None, already a member)."""
    joined = tailnet_add(ts, cat, email, rids)
    for rid in rids:
        admin.say(
            f"tailnet {group_of(rid)}: "
            + ("added" if group_of(rid) in joined else "already a member")
        )
    try:
        member = ts.member(email)
    except tailnet.TailnetError as err:
        admin.say(f"tailnet: cannot tell if {email} is a member ({err})")
        admin.say(f"tailnet: if they are not on it yet, {tailnet.PASTE_HINT}")
        return url, False
    if member:
        admin.say("tailnet: already a member, no invite needed")
        return url, True
    if url:
        admin.say("tailnet: using the invite link you passed")
    elif not ts.can_invite:
        admin.say(
            "tailnet: no TAILSCALE_USER_TOKEN: create an invite link in the "
            "admin console and paste it (re-run with --invite-url <link> to "
            "render the email with it)"
        )
    else:
        try:
            url = ts.create_invite()
            admin.say(
                "tailnet: invite created (a new one on every run; no email field)"
            )
        except tailnet.TailnetError as err:
            admin.say(
                f"tailnet: {err}; the user token may be expired: "
                f"{tailnet.PASTE_HINT}"
            )
    return url, False


def cmd_invite(api, ledger, args, cat: dict) -> None:
    rights = pick_rights(cat, args.right)
    host = (args.host or os.environ.get("PEOPLE_HOST") or "").strip()
    if not host:
        raise AdminError("no host for the email: pass --host or set PEOPLE_HOST")
    email = args.email.strip().lower()
    if "@" not in email or " " in email:
        raise AdminError(f"{args.email!r} is not an email address")
    ts = None if args.no_tailnet else tailnet.Tailscale()
    if ts is not None and not ts.configured:
        admin.say(NOT_CONFIGURED)
        ts = None
    if ts is not None:
        check_policy(ts.policy()[0], cat)  # refuse a bad policy before any write
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
    url, member = args.invite_url, False
    if ts is not None:
        url, member = tailnet_invite(ts, cat, email, [i for i, _ in rights], url)
    admin.say("")
    admin.say("--- invite email (draft; send it yourself) ---")
    admin.say(render_email(email, rights, host, url, member))


def desired_groups(api, cat: dict) -> dict[str, list[str]]:
    """The groups the Logto roles call for. A suspended user is in none."""
    groups: dict[str, list[str]] = {group_of(r): [] for r in cat["rights"]}
    for u in api.list_all("/api/users"):
        email = (u.get("primaryEmail") or "").strip().lower()
        if not email or u.get("isSuspended"):
            continue
        roles = {r["name"] for r in api.list_all(f"/api/users/{u['id']}/roles")}
        for rid, r in cat["rights"].items():
            if r["role"] in roles:
                groups[group_of(rid)].append(email)
    return norm(groups)


def group_changes(have: dict, want: dict) -> list[str]:
    have = norm(have)
    changes = []
    for g, members in want.items():
        if g not in have:
            changes.append(
                f"{g}: created"
                + (f" with {', '.join(members)}" if members else " (empty)")
            )
        elif have[g] != members:
            add = [e for e in members if e not in have[g]]
            drop = [e for e in have[g] if e not in members]
            changes.append(
                f"{g}: " + " ".join([f"+{e}" for e in add] + [f"-{e}" for e in drop])
            )
    return changes


def cmd_sync(api, _ledger, args, cat: dict) -> None:
    ts = tailnet.Tailscale()
    if not ts.configured:
        admin.say(NOT_CONFIGURED)
        return
    want = desired_groups(api, cat)
    order = list(want)
    changes: list[str] = []
    left_alone: list[str] = []

    def mutate(text: str) -> str | None:
        have = check_policy(text, cat)
        changes[:] = group_changes(have, want)
        left_alone[:] = sorted(set(have) - set(want))
        if not changes or args.dry_run:
            return None
        return tailnet.with_groups(text, {**have, **want}, order)

    wrote = ts.update_policy(mutate)
    for line in changes:
        admin.say(("would change " if args.dry_run else "") + line)
    for g in left_alone:
        admin.say(f"{g}: in the managed block but not in the catalog, left alone")
    if not changes:
        admin.say("tailnet groups already match the Logto roles: nothing written")
    elif wrote:
        admin.say("tailnet policy written")


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


def tailnet_view(ts, cat: dict) -> dict:
    """What status reads from the tailnet; every part may be None where a scope is missing."""
    view: dict = {"groups": None, "policy": None, "users": None, "notes": []}
    try:
        text = ts.policy()[0]
        view["policy"] = tailnet.parse_policy(text)
        view["groups"] = norm(tailnet.managed_groups(text))
    except tailnet.TailnetError as err:
        view["notes"].append(f"policy unavailable: {err}")
    try:
        view["users"] = {(u.get("loginName") or "").lower(): u for u in ts.users()}
    except tailnet.TailnetError as err:
        view["notes"].append(f"users unavailable: {err}")
    return view


def tailnet_line(view: dict, email: str) -> str:
    parts = []
    if view["users"] is not None:
        u = view["users"].get(email)
        if u:
            seen = (u.get("lastSeen") or "")[:10]
            parts.append(
                f"member ({u.get('status') or 'active'}, "
                f"{u.get('deviceCount', 0)} devices"
                + (f", last seen {seen}" if seen else "")
                + ")"
            )
        else:
            parts.append(
                "not a member (invite pending, or accepted under another login)"
            )
    if view["groups"] is not None:
        held = sorted(g for g, m in view["groups"].items() if email in m)
        parts.append(f"groups [{', '.join(held) or '-'}]")
    return ", ".join(parts)


def tailnet_row_drift(view: dict, row: dict, cat: dict) -> list[str]:
    if view["groups"] is None:
        return []
    email = (row["user"].get("primaryEmail") or "").lower()
    if not email:
        return []
    out = []
    live = not row["user"].get("isSuspended")
    for rid, r in cat["rights"].items():
        g = group_of(rid)
        in_group = email in view["groups"].get(g, [])
        has_role = r["role"] in row["roles"] and live
        if has_role and not in_group:
            out.append(f"holds role {r['role']} but is not in {g} (run sync)")
        if in_group and not has_role:
            out.append(f"is in {g} without an active {r['role']} role (run sync)")
    return out


def tailnet_global_drift(view: dict, cat: dict, rows: list[dict], everyone: bool):
    out = []
    policy = view["policy"]
    if policy is not None:
        try:
            tailnet.refuse_never_ports(
                policy, [group_of(r) for r in cat["rights"]], cat["never_ports"]
            )
        except tailnet.TailnetError as err:
            out.append(str(err))
        for rid, r in cat["rights"].items():
            for port in [int(r["port"]), *cat["always_ports"]]:
                if not tailnet.reaches(policy, group_of(rid), port):
                    out.append(f"no grant lets {group_of(rid)} reach tcp:{port}")
        if not policy.get("tests"):
            out.append("the policy has no tests block")
    if everyone and view["users"] is not None:
        known = {(r["user"].get("primaryEmail") or "").lower() for r in rows}
        for login, u in sorted(view["users"].items()):
            if u.get("role") == "member" and login not in known:
                out.append(f"tailnet member {login} matches no Logto user")
    return out


def cmd_status(api, _ledger, args, cat: dict) -> None:
    if args.email:
        users = [admin.find_user(api, args.email.strip().lower())]
    else:
        users = api.list_all("/api/users")
    rows = [user_row(api, u, cat) for u in users]
    ts = tailnet.Tailscale()
    view = tailnet_view(ts, cat) if ts.configured else None
    extra = []
    for row in rows:
        u = row["user"]
        email = (u.get("primaryEmail") or "").lower()
        admin.say(
            f"{u.get('primaryEmail') or u.get('username') or u['id']}: "
            f"roles [{', '.join(row['roles']) or '-'}]"
            f"{', SUSPENDED' if u.get('isSuspended') else ''}"
        )
        if view is not None and email:
            line = tailnet_line(view, email)
            if line:
                admin.say(f"  tailnet: {line}")
            row["drift"] += tailnet_row_drift(view, row, cat)
    if view is None:
        admin.say(NOT_CONFIGURED)
    else:
        for note in view["notes"]:
            admin.say(f"tailnet: {note}")
        extra = tailnet_global_drift(view, cat, rows, not args.email)
    drifted = [r for r in rows if r["drift"]]
    if drifted or extra:
        admin.say("drift:")
        for row in drifted:
            u = row["user"]
            who = u.get("primaryEmail") or u["id"]
            admin.say(f"  {who}: {'; '.join(row['drift'])}")
        for line in extra:
            admin.say(f"  tailnet: {line}")
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
    i.add_argument(
        "--no-tailnet", action="store_true", help="skip the tailnet step (Logto only)"
    )
    s = sub.add_parser("status", help="Logto roles, suspended flag and drift")
    s.add_argument("email", nargs="?")
    y = sub.add_parser(
        "sync", help="rebuild the tailnet groups block from the Logto roles"
    )
    y.add_argument(
        "--dry-run", action="store_true", help="print the changes, write nothing"
    )
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
        {"invite": cmd_invite, "status": cmd_status, "sync": cmd_sync}[args.cmd](
            api, ledger, args, cat
        )
    except (AdminError, tailnet.TailnetError) as err:
        print(f"people: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
