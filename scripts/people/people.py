#!/usr/bin/env python3
"""People: invite someone and see who has what (Logto and tailnet halves).

    people.py invite EMAIL --right ID [--right ID]... [--host HOST]
                    [--invite-url URL] [--no-tailnet] [--send | --resend]
    people.py status [EMAIL]
    people.py sync [--dry-run]
    people.py revoke EMAIL [--right ID]... [--dry-run] [--suspend-tailnet-user]
    people.py status --notify

invite makes sure the Logto user exists (primary email, no password: they sign
in with Google or an emailed code) and holds one Logto role per right, then
prints the invite email. It sends mail only with --send or --resend (below). Every write
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

--send also mails that email to the invitee through Gmail SMTP
(scripts/people/mailer.py: PEOPLE_SMTP_USER, PEOPLE_SMTP_PASSWORD from the
environment). Without the app password it prints the draft and a "mail not
configured" line and exits 0; an email that would still carry the invite-link
placeholder is not sent either. --resend re-sends it for someone already
invited: it writes nothing to Logto or the policy, requires the Logto user to
hold each given right, mints a fresh tailnet invite link when the email is not
yet a tailnet member (or takes --invite-url), and mails it. A failed send
exits 1 after everything else was done.

status shows each user's Logto roles and suspended flag, and drift: a user
holding no product role from the catalog (platform roles such as admin are
exempt), or a suspended user who still holds one. With the tailnet configured
it adds each user's tailnet membership and groups, and drift between roles and
groups, missing grants, a policy with no tests, and tailnet members no Logto
user matches. With EMAIL it shows that one user; without, every user.

status --notify also posts what it found to notify-relay (docs/message-format.md,
through scripts/ledger-persona/drift-alert.py's transport, so run it on the host):
one `act` envelope for drift (any line of the drift list; silent when there is
none) and one `wait` envelope when TAILSCALE_USER_TOKEN expires within 14 days
or is already rejected (its expiry is read from the token's own key record;
no token set, nothing to check). Run it on a timer (docs/runbook.md "People:
revoke and drift alerts"); exit 1 if the relay did not take the envelope.

revoke removes a person's access: the email leaves the tailnet group of each
right (every catalog right without --right), the Logto role for each is
removed, and the Logto user is suspended when no product role remains and no
platform role (admin) is held. The network cut goes first. Each Logto write has
undo data in the ledger, and a closing `people-revoke` ledger entry records who
lost what. Idempotent: a second run finds nothing to do. --dry-run reads and
prints "would ..." lines, writes nothing. --suspend-tailnet-user (only without
--right) also suspends the Tailscale user (reversible, scope `users`); the
Tailscale user and devices are never deleted. revoke_person() is the plain
function behind it (no printing, no exit); it returns the lines and errors.

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
import datetime
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import mailer
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


def tailnet_invite(
    ts, cat: dict, email: str, rids: list[str], url: str | None, groups: bool = True
):
    """The tailnet half of invite. Returns (invite url or None, already a member).

    groups=False (resend) leaves the policy alone and only handles the link.
    """
    if groups:
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
                f"tailnet: {err}; the user token may be expired: {tailnet.PASTE_HINT}"
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
    if args.resend:
        resend_check(api, email, rights)
    else:
        if ts is not None:
            check_policy(ts.policy()[0], cat)  # refuse a bad policy before any write
        invite_writes(api, ledger, email, rights)
    url, member = args.invite_url, False
    if ts is not None:
        url, member = tailnet_invite(
            ts, cat, email, [i for i, _ in rights], url, groups=not args.resend
        )
    text = render_email(email, rights, host, url, member)
    if args.send or args.resend:
        deliver(email, text, placeholder=not url and not member)
    else:
        admin.say("")
        admin.say("--- invite email (draft; send it yourself) ---")
        admin.say(text)


def invite_writes(api, ledger, email: str, rights: list[tuple[str, dict]]) -> None:
    user, n = admin.ensure_user(api, ledger, email)
    admin.say(
        f"user {user['id']}: " + ("exists" if n is None else f"created, ledger #{n}")
    )
    if user.get("isSuspended"):  # a revoked person invited again can sign in again
        if not suspended_by_revoke(ledger, user["id"], email):
            raise AdminError(
                f"{email} is suspended, but not by people revoke (the ledger has no "
                "revoke behind it): not re-enabling; unsuspend them in Logto first "
                "if that is intended"
            )
        set_suspended(api, ledger, user["id"], False)
        admin.say("user was suspended (revoked earlier): unsuspended")
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


def resend_check(api, email: str, rights: list[tuple[str, dict]]) -> None:
    """A resend is for an existing invite: the user and each role must be there."""
    user = admin.find_user(api, email)
    held = {r["name"] for r in api.list_all(f"/api/users/{user['id']}/roles")}
    missing = [rid for rid, r in rights if r["role"] not in held]
    if missing:
        raise AdminError(
            f"{email} does not hold {', '.join(missing)}: run invite (without "
            "--resend) first"
        )
    admin.say(f"user {user['id']}: exists with the requested rights, nothing written")


def deliver(email: str, text: str, placeholder: bool) -> None:
    """Mail the rendered email, or fall back to printing the draft."""
    cfg = mailer.settings()

    def draft(why: str) -> None:
        admin.say("")
        admin.say("--- invite email (draft; send it yourself) ---")
        admin.say(text)
        admin.say(why)

    if cfg is None:
        draft(
            "mail not configured (set PEOPLE_SMTP_USER and PEOPLE_SMTP_PASSWORD, "
            'docs/runbook.md "Sending the invite email"): not sent'
        )
    elif placeholder:
        draft(
            "no tailnet invite link yet (pass --invite-url <link>): not sent, "
            "the email would carry a placeholder"
        )
    else:
        subject, body = mailer.split_message(text)
        mailer.send(cfg, email, subject, body)
        admin.say(f"invite email sent to {email} from {cfg['user']}")


def suspended_by_revoke(ledger, uid: str, email: str) -> bool:
    """True when the user's latest suspend entry is the one revoke just made.

    revoke appends its closing people-revoke entry right after the suspend, so the
    entry that follows the latest suspend-user must be that revoke; any other
    suspend (or a later unsuspend) is not revoke's to undo.
    """
    entries = ledger.entries()
    last = None
    for i, e in enumerate(entries):
        if e.get("user") == uid and e.get("action") in (
            "suspend-user",
            "unsuspend-user",
        ):
            last = i
    if last is None or entries[last]["action"] != "suspend-user":
        return False
    nxt = entries[last + 1] if last + 1 < len(entries) else {}
    return nxt.get("action") == "people-revoke" and nxt.get("email") == email


def set_suspended(api, ledger, uid: str, suspended: bool) -> int:
    api.call("PATCH", f"/api/users/{uid}/is-suspended", {"isSuspended": suspended})
    return ledger.append(
        {
            "action": "suspend-user" if suspended else "unsuspend-user",
            "user": uid,
            "undo": {
                "method": "PATCH",
                "path": f"/api/users/{uid}/is-suspended",
                "body": {"isSuspended": not suspended},
            },
        }
    )


def tailnet_remove(
    ts, cat: dict, email: str, rids: list[str], dry_run: bool = False
) -> list[str]:
    """Take email out of group:<right> for each right. Returns the groups it left.

    Reads the groups block without check_policy: a policy that has drifted must
    never stop a revoke.
    """
    order = [group_of(r) for r in cat["rights"]]
    drop = {group_of(r) for r in rids}
    left: list[str] = []

    def mutate(text: str) -> str | None:
        have = tailnet.managed_groups(text)
        left[:] = [
            g for g, m in have.items() if g in drop and email in {e.lower() for e in m}
        ]
        if not left or dry_run:
            return None
        merged = {
            g: [e for e in m if e.lower() != email] if g in drop else list(m)
            for g, m in have.items()
        }
        return tailnet.with_groups(text, merged, order)

    ts.update_policy(mutate)
    return left


def revoke_person(
    api,
    ledger,
    cat: dict,
    email: str,
    rids: list[str] | None = None,
    ts=None,
    dry_run: bool = False,
    suspend_tailnet: bool = False,
) -> dict:
    """Cut a person's access. Plain function: no printing, no exit.

    rids None/empty revokes every catalog right. Returns {"email", "rights",
    "log": [lines], "changed": bool, "errors": [lines]}. The tailnet half runs
    first (it is the network cut) and a failure there, or no tailnet client,
    does not stop the Logto half; it comes back in "errors". Bad input (unknown right, bad email,
    --right with the tailnet-user suspend) raises AdminError before any write.
    """
    email = email.strip().lower()
    if "@" not in email or " " in email:
        raise AdminError(f"{email!r} is not an email address")
    whole = not rids
    if suspend_tailnet and not whole:
        raise AdminError("suspending the tailnet user takes all rights: drop --right")
    rights = list(cat["rights"].items()) if whole else pick_rights(cat, rids)
    ids = [rid for rid, _ in rights]
    log: list[str] = []
    errors: list[str] = []
    changed = False

    if ts is None:
        errors.append(
            "tailnet: not configured (set TAILSCALE_OAUTH_CLIENT_ID and "
            "TAILSCALE_OAUTH_CLIENT_SECRET): network access not cut"
        )
    else:
        try:
            left = tailnet_remove(ts, cat, email, ids, dry_run)
            for rid in ids:
                g = group_of(rid)
                log.append(
                    f"tailnet {g}: "
                    + (
                        ("would remove" if dry_run else "removed")
                        if g in left
                        else "not a member"
                    )
                )
            changed |= bool(left) and not dry_run
            if suspend_tailnet:
                member = ts.member(email)
                if member is None:
                    log.append("tailnet user: not a member, nothing to suspend")
                elif member.get("status") == "suspended":
                    log.append("tailnet user: already suspended")
                elif dry_run:
                    log.append("tailnet user: would suspend")
                else:
                    ts.suspend_user(member["id"])
                    changed = True
                    log.append("tailnet user: suspended")
        except tailnet.TailnetError as err:
            errors.append(f"tailnet: {err}")

    user = admin.lookup_user(api, email)
    if user is None:
        log.append(f"logto: no user for {email}, nothing to revoke there")
    else:
        held = {r["name"]: r for r in api.list_all(f"/api/users/{user['id']}/roles")}
        for rid, r in rights:
            role = held.pop(r["role"], None)
            if role is None:
                log.append(f"right {rid}: role {r['role']} not held")
                continue
            if not dry_run:
                api.call("DELETE", f"/api/users/{user['id']}/roles/{role['id']}")
                n = ledger.append(
                    {
                        "action": "remove-role",
                        "user": user["id"],
                        "role": {"id": role["id"], "name": role["name"]},
                        "undo": {
                            "method": "POST",
                            "path": f"/api/users/{user['id']}/roles",
                            "body": {"roleIds": [role["id"]]},
                        },
                    }
                )
                changed = True
                log.append(f"right {rid}: role {r['role']} removed, ledger #{n}")
            else:
                log.append(f"right {rid}: would remove role {r['role']}")
        product = {r["role"] for r in cat["rights"].values()}
        platform = sorted(set(held) - product)
        if set(held) & product:
            log.append(
                "logto: still holds "
                + ", ".join(sorted(set(held) & product))
                + ": not suspended"
            )
        elif platform:
            log.append(
                f"logto: holds platform role {', '.join(platform)}: not suspended"
            )
        elif user.get("isSuspended"):
            log.append("logto: already suspended")
        elif dry_run:
            log.append("logto: would suspend the user (no rights remain)")
        else:
            n = set_suspended(api, ledger, user["id"], True)
            changed = True
            log.append(f"logto: user suspended (no rights remain), ledger #{n}")

    if changed:
        n = ledger.append(
            {"action": "people-revoke", "email": email, "rights": ids, "whole": whole}
        )
        log.append(f"revoke recorded, ledger #{n}")
    elif not errors:
        log.append(
            "nothing to revoke: already done" if not dry_run else "dry run: no writes"
        )
    return {
        "email": email,
        "rights": ids,
        "log": log,
        "changed": changed,
        "errors": errors,
    }


def cmd_revoke(api, ledger, args, cat: dict) -> None:
    ts = tailnet.Tailscale()
    if not ts.configured:
        ts = None
    result = revoke_person(
        api,
        ledger,
        cat,
        args.email,
        args.right,
        ts,
        args.dry_run,
        args.suspend_tailnet_user,
    )
    for line in result["log"]:
        admin.say(line)
    if result["errors"]:
        raise AdminError(
            "; ".join(result["errors"]) + " (the rest was done; run revoke again)"
        )


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


TOKEN_WARN_DAYS = 14
STATUS_ACTION = "people.py status"
TOKEN_ACTION = (
    "generate a new user token (Settings > Keys) for the people SOPS file, or "
    "remove TAILSCALE_USER_TOKEN from it when no invite is pending"
)


def token_problem(ts, now=None) -> tuple[str, str | None]:
    """-> (status line, a problem to alert on or None) for TAILSCALE_USER_TOKEN."""
    now = now or datetime.datetime.now(datetime.UTC)
    try:
        expires = ts.user_token_expiry()
    except tailnet.TokenRejected as err:
        return str(err), f"TAILSCALE_USER_TOKEN is expired or rejected: {err}"
    except tailnet.TailnetError as err:
        return f"expiry unknown ({err})", f"cannot check TAILSCALE_USER_TOKEN: {err}"
    if expires is None:
        return "does not expire", None
    left = expires - now
    day = expires.strftime("%Y-%m-%d")
    if left <= datetime.timedelta(0):
        return f"expired {day}", f"TAILSCALE_USER_TOKEN expired on {day}"
    days = left.days
    line = f"expires {day} (in {days} days)"
    if left < datetime.timedelta(days=TOKEN_WARN_DAYS):
        return line, f"TAILSCALE_USER_TOKEN expires on {day}, in {days} days"
    return line, None


def drift_envelope(findings: list[str]) -> dict:
    n = len(findings)
    return {
        "level": "act",
        "source": "people",
        "subject": "drift",
        "headline": f"{n} access finding{'s' if n != 1 else ''}: roles, tailnet "
        "groups and members disagree",
        "body": "\n".join(f"- {f}" for f in findings),
        "action": STATUS_ACTION,
    }


def token_envelope(problem: str) -> dict:
    return {
        "level": "wait",
        "source": "people",
        "subject": "tailnet-token",
        "headline": problem[:780],
        "action": TOKEN_ACTION,
    }


def notify(envelopes: list[dict]) -> None:
    """POST each envelope to notify-relay through the drift-alert transport."""
    spec = importlib.util.spec_from_file_location(
        "drift_alert", HERE.parent / "ledger-persona" / "drift-alert.py"
    )
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    for env in envelopes:
        try:
            transport.post(env)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as err:
            raise AdminError(f"alert not delivered: {err}") from None
        admin.say(f"alert sent to notify-relay: {env['subject']}")


def cmd_status(api, _ledger, args, cat: dict) -> None:
    if args.notify and args.email:
        raise AdminError("--notify checks everyone: drop the email")
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
    problem = None
    if view is not None and ts.can_invite and not args.email:
        line, problem = token_problem(ts)
        admin.say(f"tailnet user token: {line}")
    drifted = [r for r in rows if r["drift"]]
    findings: list[str] = []
    if drifted or extra:
        admin.say("drift:")
        for row in drifted:
            u = row["user"]
            who = u.get("primaryEmail") or u["id"]
            admin.say(f"  {who}: {'; '.join(row['drift'])}")
            findings.append(f"{who}: {'; '.join(row['drift'])}")
        for line in extra:
            admin.say(f"  tailnet: {line}")
            findings.append(f"tailnet: {line}")
    else:
        admin.say("drift: none")
    if args.notify:
        envelopes = ([drift_envelope(findings)] if findings else []) + (
            [token_envelope(problem)] if problem else []
        )
        if envelopes:
            notify(envelopes)
        else:
            admin.say("nothing to alert on")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="people.py", description="Invite people and see who has what."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser(
        "invite", help="ensure the Logto user and roles, print or send the email"
    )
    i.add_argument("email")
    i.add_argument("--right", action="append", required=True, metavar="ID")
    i.add_argument("--host", help="host the products are served on (or PEOPLE_HOST)")
    i.add_argument("--invite-url", help="the Tailscale invite link for the email")
    i.add_argument(
        "--no-tailnet", action="store_true", help="skip the tailnet step (Logto only)"
    )
    mail = i.add_mutually_exclusive_group()
    mail.add_argument(
        "--send", action="store_true", help="mail the email (draft if mail is unset)"
    )
    mail.add_argument(
        "--resend",
        action="store_true",
        help="re-send for an existing invite; writes nothing to Logto or the policy",
    )
    s = sub.add_parser("status", help="Logto roles, suspended flag and drift")
    s.add_argument("email", nargs="?")
    s.add_argument(
        "--notify",
        action="store_true",
        help="post drift and a nearly expired user token to notify-relay",
    )
    r = sub.add_parser(
        "revoke", help="cut a person's network and sign-in access (idempotent)"
    )
    r.add_argument("email")
    r.add_argument(
        "--right",
        action="append",
        metavar="ID",
        help="revoke only this right (repeatable); default: every right",
    )
    r.add_argument(
        "--dry-run", action="store_true", help="print what would change, write nothing"
    )
    r.add_argument(
        "--suspend-tailnet-user",
        action="store_true",
        help="also suspend the Tailscale user (all rights only; never deletes)",
    )
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
        {
            "invite": cmd_invite,
            "status": cmd_status,
            "sync": cmd_sync,
            "revoke": cmd_revoke,
        }[args.cmd](api, ledger, args, cat)
    except (AdminError, tailnet.TailnetError, mailer.MailError) as err:
        print(f"people: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
