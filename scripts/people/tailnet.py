"""The Tailscale half of people.py: policy groups block, users, user invites.

The tailnet policy is a hand-written HuJSON file. This module never round-trips
it through JSON (that would drop the comments): it edits only the lines between
a BEGIN/END people-managed marker pair inside "groups" and leaves every other
byte alone. The block is written once by hand with its markers and the static
grants (docs/runbook.md "People: the tailnet half"); after that only emails move
between the `group:<right>` lines.

Two credentials, both from the environment (never printed, never logged):

    TAILSCALE_OAUTH_CLIENT_ID / TAILSCALE_OAUTH_CLIENT_SECRET
        OAuth client scoped to policy_file and users:read. Runs sync, status
        and the policy edit of invite. Missing: every tailnet step reports
        "not configured" and is skipped.
    TAILSCALE_USER_TOKEN
        A short-lived user-owned API token (tskey-api-...). Used only to create
        a user invite, the one call an OAuth client cannot make. Missing or
        rejected: the caller prints the paste-a-link instruction instead.

TAILSCALE_TAILNET picks the tailnet (default "-", the credential's own).
TAILSCALE_API_URL overrides https://api.tailscale.com (tests, rehearsals).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.tailscale.com"
HUJSON = "application/hujson"

BEGIN_RE = re.compile(r"^([ \t]*)//[ \t]*BEGIN people-managed\b.*$", re.M)
END_RE = re.compile(r"^[ \t]*//[ \t]*END people-managed\b.*$", re.M)
GROUP_LINE_RE = re.compile(r'^"(group:[^"]+)"\s*:\s*\[(.*)\]\s*,?\s*(//.*)?$')
MEMBER_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')

USER_TOKEN_RE = re.compile(r"^tskey-api-([^-\s]+)-")

PASTE_HINT = "create an invite link in the admin console and paste it"


class TailnetError(Exception):
    pass


class TokenRejected(TailnetError):
    """Tailscale answered 401: the credential is expired, revoked or wrong."""


# ─── policy text ─────────────────────────────────────────────────────────────


def strip_hujson(text: str) -> str:
    """HuJSON -> JSON: drop // and /* */ comments and trailing commas."""
    out: list[str] = []
    last = None  # index in out of the last non-space character
    in_str = esc = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
                last = len(out) - 1
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            if c in "}]" and last is not None and out[last] == ",":
                out[last] = " "
            out.append(c)
            if not c.isspace():
                last = len(out) - 1
        i += 1
    return "".join(out)


def parse_policy(text: str) -> dict:
    try:
        policy = json.loads(strip_hujson(text))
    except ValueError as err:
        raise TailnetError(f"the policy is not valid HuJSON: {err}") from None
    if not isinstance(policy, dict):
        raise TailnetError("the policy is not a JSON object")
    return policy


def split_block(text: str) -> tuple[str, str, list[str], str]:
    """-> (head through the BEGIN line, indent, body lines, tail from the END line)."""
    begins, ends = list(BEGIN_RE.finditer(text)), list(END_RE.finditer(text))
    if len(begins) != 1 or len(ends) != 1 or ends[0].start() < begins[0].end():
        raise TailnetError(
            "the policy has no single `// BEGIN people-managed` / "
            "`// END people-managed` marker pair inside groups: do the one-time "
            "operator step in docs/runbook.md first"
        )
    body = text[begins[0].end() + 1 : ends[0].start()]
    return (
        text[: begins[0].end() + 1],
        begins[0].group(1),
        body.splitlines(),
        text[ends[0].start() :],
    )


def read_groups(body: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for n, line in enumerate(body, 1):
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        m = GROUP_LINE_RE.match(s)
        if not m:
            raise TailnetError(
                f"line {n} of the people-managed block is not a "
                '`"group:x": ["email", ...],` line; fix it by hand'
            )
        groups[m.group(1)] = [
            json.loads(f'"{x}"') for x in MEMBER_RE.findall(m.group(2))
        ]
    return groups


def render_groups(groups: dict[str, list[str]], indent: str, order: list[str]) -> str:
    names = [g for g in order if g in groups] + sorted(set(groups) - set(order))
    lines = []
    for name in names:
        members = ", ".join(json.dumps(e) for e in sorted(set(groups[name])))
        lines.append(f'{indent}"{name}": [{members}],\n')
    return "".join(lines)


def managed_groups(text: str) -> dict[str, list[str]]:
    return read_groups(split_block(text)[2])


def with_groups(text: str, groups: dict[str, list[str]], order: list[str]) -> str:
    head, indent, _, tail = split_block(text)
    return head + render_groups(groups, indent, order) + tail


# ─── grants: what the policy lets the managed groups reach ───────────────────


def _covers(spec: str, port: int) -> bool:
    proto, _, ports = spec.rpartition(":") if ":" in spec else ("", "", spec)
    if proto not in ("", "*", "tcp", "udp"):
        return False
    if ports == "*":
        return True
    for part in ports.split(","):
        lo, _, hi = part.partition("-")
        try:
            if int(lo) <= port <= int(hi or lo):
                return True
        except ValueError:
            continue
    return False


def _rules(policy: dict):
    """Yield (src list, port specs) for every grant and legacy acl rule."""
    for g in policy.get("grants") or []:
        if isinstance(g, dict):
            yield list(g.get("src") or []), list(g.get("ip") or [])
    for a in policy.get("acls") or []:
        if isinstance(a, dict):
            yield (
                list(a.get("src") or []),
                [d.rpartition(":")[2] for d in a.get("dst") or [] if ":" in d],
            )


def reaches(policy: dict, group: str, port: int) -> bool:
    return any(
        group in src and any(_covers(spec, port) for spec in specs)
        for src, specs in _rules(policy)
    )


def refuse_never_ports(policy: dict, groups: list[str], never: list[int]) -> None:
    """A managed group must not reach an admin port, whatever the grants say."""
    for src, specs in _rules(policy):
        for group in groups:
            if group not in src:
                continue
            for port in never:
                if any(_covers(spec, port) for spec in specs):
                    raise TailnetError(
                        f"a policy grant gives {group} port {port}, which the "
                        "catalog never grants; not writing"
                    )


# ─── the API ─────────────────────────────────────────────────────────────────


class Tailscale:
    def __init__(self, env=None) -> None:
        env = os.environ if env is None else env
        self.base = (env.get("TAILSCALE_API_URL") or API).rstrip("/")
        self.tailnet = urllib.parse.quote(env.get("TAILSCALE_TAILNET") or "-", safe="-")
        self._id = env.get("TAILSCALE_OAUTH_CLIENT_ID", "")
        self._secret = env.get("TAILSCALE_OAUTH_CLIENT_SECRET", "")
        self._user_token = env.get("TAILSCALE_USER_TOKEN", "")
        self._token: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._id and self._secret)

    @property
    def can_invite(self) -> bool:
        return bool(self._user_token)

    def _send(self, method, path, headers, data=None):
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as err:
            return (
                err.code,
                dict(err.headers or {}),
                err.read().decode(errors="replace"),
            )
        except urllib.error.URLError as err:
            raise TailnetError(f"cannot reach {self.base}: {err.reason}") from None

    @staticmethod
    def _fail(what: str, status: int, body: str) -> TailnetError:
        try:
            msg = json.loads(body).get("message") or ""
        except (ValueError, AttributeError):
            msg = ""
        return TailnetError(f"{what}: HTTP {status}{': ' + msg[:200] if msg else ''}")

    def _bearer(self) -> str:
        if self._token is None:
            form = urllib.parse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": self._id,
                    "client_secret": self._secret,
                }
            ).encode()
            status, _, body = self._send(
                "POST",
                "/api/v2/oauth/token",
                {"Content-Type": "application/x-www-form-urlencoded"},
                form,
            )
            try:
                self._token = json.loads(body)["access_token"]
            except (ValueError, KeyError, TypeError):
                raise self._fail("tailnet token request", status, body) from None
        return self._token

    def _call(self, method, path, what, data=None, headers=None, token=None, ok=(200,)):
        h = {"Authorization": f"Bearer {token or self._bearer()}", **(headers or {})}
        status, resp_headers, body = self._send(method, path, h, data)
        if status not in ok:
            raise self._fail(what, status, body)
        return status, resp_headers, body

    # policy

    def policy(self) -> tuple[str, str]:
        _, headers, body = self._call(
            "GET",
            f"/api/v2/tailnet/{self.tailnet}/acl",
            "read the policy",
            headers={"Accept": HUJSON},
        )
        etag = next((v for k, v in headers.items() if k.lower() == "etag"), "")
        if not etag:
            raise TailnetError("read the policy: no ETag, refusing an unguarded write")
        return body, etag

    def validate(self, text: str) -> None:
        _, _, body = self._call(
            "POST",
            f"/api/v2/tailnet/{self.tailnet}/acl/validate",
            "validate the policy",
            data=text.encode(),
            headers={"Content-Type": HUJSON},
        )
        try:
            out = json.loads(body) if body.strip() else {}
        except ValueError:
            out = {}
        if isinstance(out, dict) and out.get("data"):
            raise TailnetError(f"validate the policy: {out.get('message') or 'failed'}")

    def update_policy(self, mutate) -> bool:
        """Apply mutate(text) -> new text or None. Validate, then POST with If-Match.

        A 412 (someone changed the policy meanwhile) re-reads and retries once.
        Returns whether anything was written.
        """
        for attempt in (0, 1):
            text, etag = self.policy()
            new = mutate(text)
            if new is None or new == text:
                return False
            self.validate(new)
            status, _, body = self._send(
                "POST",
                f"/api/v2/tailnet/{self.tailnet}/acl",
                {
                    "Authorization": f"Bearer {self._bearer()}",
                    "Content-Type": HUJSON,
                    "If-Match": etag,
                },
                new.encode(),
            )
            if status == 200:
                return True
            if status != 412:
                raise self._fail("write the policy", status, body)
            if attempt:
                raise TailnetError(
                    "write the policy: it changed twice while writing; run again"
                )
        return False  # unreachable

    # users and invites

    def users(self) -> list[dict]:
        _, _, body = self._call(
            "GET", f"/api/v2/tailnet/{self.tailnet}/users", "list tailnet users"
        )
        try:
            return json.loads(body)["users"]
        except (ValueError, KeyError, TypeError):
            raise TailnetError("list tailnet users: unexpected response") from None

    def member(self, email: str) -> dict | None:
        return next(
            (u for u in self.users() if (u.get("loginName") or "").lower() == email),
            None,
        )

    def suspend_user(self, user_id: str) -> None:
        """Suspend a tailnet user (reversible; scope `users`). Their devices stop."""
        self._call(
            "POST",
            f"/api/v2/users/{urllib.parse.quote(str(user_id), safe='')}/suspend",
            "suspend the tailnet user",
            ok=(200, 201, 202, 204),
        )

    def user_token_expiry(self) -> datetime.datetime | None:
        """When TAILSCALE_USER_TOKEN expires (None: it does not), read from the key
        record the token's own id names. Raises TokenRejected when it is already
        expired or revoked, TailnetError when the record cannot be read."""
        m = USER_TOKEN_RE.match(self._user_token)
        if not m:
            raise TailnetError("TAILSCALE_USER_TOKEN is not a tskey-api-... token")
        status, _, body = self._send(
            "GET",
            f"/api/v2/tailnet/{self.tailnet}/keys/{urllib.parse.quote(m.group(1))}",
            {"Authorization": f"Bearer {self._user_token}"},
        )
        if status == 401:
            raise TokenRejected("the user token is rejected (expired or revoked)")
        if status != 200:
            raise self._fail("read the user token's expiry", status, body)
        try:
            key = json.loads(body)
            if key.get("revoked") or key.get("invalid"):
                raise TokenRejected("the user token is revoked or invalid")
            expires = key.get("expires")
            if not expires:
                return None
            when = datetime.datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except (ValueError, AttributeError, TypeError):
            raise TailnetError(
                "read the user token's expiry: unexpected response"
            ) from None
        return when if when.tzinfo else when.replace(tzinfo=datetime.UTC)

    def create_invite(self) -> str:
        """A member invite with no email field; returns its inviteUrl."""
        _, _, body = self._call(
            "POST",
            f"/api/v2/tailnet/{self.tailnet}/user-invites",
            "create a user invite",
            data=json.dumps([{"role": "member"}]).encode(),
            headers={"Content-Type": "application/json"},
            token=self._user_token,
            ok=(200, 201),
        )
        try:
            return json.loads(body)[0]["inviteUrl"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise TailnetError(
                "create a user invite: no inviteUrl in the response"
            ) from None
