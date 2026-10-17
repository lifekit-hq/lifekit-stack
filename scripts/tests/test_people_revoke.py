# ruff: noqa: F811
"""people.py revoke and status --notify against fake Logto, Tailscale and notify-relay.

The fake notify-relay is a docker stand-in (the drift-alert transport runs
`docker ps` then `docker exec ... node -e` and feeds the envelope on stdin), so
what these tests read back is exactly the envelope notify-relay would receive.
Nothing here touches a live service.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest
from test_people_tailnet import (  # noqa: F401  (env is a fixture)
    HOST,
    POLICY,
    FakeTailscale,
    block_groups,
    env,
    give_role,
    logto_writes,
    outside_block,
)

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts/people"))
import people  # noqa: E402
import tailnet  # noqa: E402

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$STATE_DIR/calls"
case "$1" in
  ps) [[ -n "${NO_CONTAINER:-}" ]] || echo abc123 ;;
  exec) cat >> "$STATE_DIR/envelopes.jsonl"; echo >> "$STATE_DIR/envelopes.jsonl"
        echo "${POST_OUT:-200 ok}"; exit "${POST_RC:-0}" ;;
esac
"""


@pytest.fixture
def relay(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    state = tmp_path / "relay"
    state.mkdir()

    def envelopes():
        f = state / "envelopes.jsonl"
        return (
            [json.loads(x) for x in f.read_text().split("\n") if x]
            if f.exists()
            else []
        )

    return {"DOCKER": str(docker), "STATE_DIR": str(state)}, envelopes, state


def seed(env, rights=("backstage",), email="friend@example.com"):
    """An invited person: Logto user, roles, tailnet groups, a member row."""
    args = ["invite", email]
    for r in rights:
        args += ["--right", r]
    assert env(*args, "--host", HOST).returncode == 0
    user = next(u for u in env.logto.users if u["primaryEmail"] == email)
    env.ts.users.append(
        {
            "id": "tsu-2",
            "loginName": email,
            "role": "member",
            "status": "active",
            "deviceCount": 1,
        }
    )
    return user


def clean(env):
    """Roles for the two fixture users and synced groups: no drift to start from."""
    give_role(env.logto, "u1", "admin")
    give_role(env.logto, "u2", "backstage")
    assert env("sync").returncode == 0


def role_names(logto, uid):
    ids = set(logto.user_roles.get(uid, []))
    return sorted(r["name"] for r in logto.roles if r["id"] in ids)


def ledger(tmp_path):
    f = tmp_path / "ledger.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()]


# ─── revoke ──────────────────────────────────────────────────────────────────


def test_revoke_cuts_network_and_signin_and_records_it(env, tmp_path):
    user = seed(env, ("backstage", "finance-sentry"))
    proc = env("revoke", "Friend@Example.com")
    assert proc.returncode == 0, proc.stderr
    assert block_groups(env.ts.policy) == {
        "group:finance-sentry": [],
        "group:backstage": [],
    }
    assert outside_block(env.ts.policy) == outside_block(POLICY)
    assert role_names(env.logto, user["id"]) == []
    assert user["isSuspended"] is True
    assert "tailnet user" not in proc.stdout  # never touched without the flag
    assert env.ts.suspended == []
    last = ledger(tmp_path)[-1]
    assert last["action"] == "people-revoke" and last["email"] == "friend@example.com"
    assert last["rights"] == ["finance-sentry", "backstage"] and last["whole"] is True


def test_revoke_ledger_entries_carry_undo_data(env, tmp_path):
    user = seed(env)
    env("revoke", "friend@example.com")
    by_action = {e["action"]: e for e in ledger(tmp_path)}
    assert by_action["remove-role"]["undo"]["method"] == "POST"
    assert by_action["remove-role"]["undo"]["body"]["roleIds"]
    assert by_action["suspend-user"]["undo"]["body"] == {"isSuspended": False}
    assert by_action["suspend-user"]["undo"]["path"].endswith("/is-suspended")
    assert user["isSuspended"] is True


def test_revoke_is_idempotent(env, tmp_path):
    seed(env)
    env("revoke", "friend@example.com")
    writes = len(logto_writes(env.logto)), len(env.ts.acl_writes())
    entries = len(ledger(tmp_path))
    again = env("revoke", "friend@example.com")
    assert again.returncode == 0, again.stderr
    assert "nothing to revoke" in again.stdout
    assert (len(logto_writes(env.logto)), len(env.ts.acl_writes())) == writes
    assert len(ledger(tmp_path)) == entries


def test_revoke_one_right_keeps_the_other_and_does_not_suspend(env):
    user = seed(env, ("backstage", "finance-sentry"))
    proc = env("revoke", "friend@example.com", "--right", "backstage")
    assert proc.returncode == 0, proc.stderr
    assert block_groups(env.ts.policy) == {
        "group:finance-sentry": ["friend@example.com"],
        "group:backstage": [],
    }
    assert role_names(env.logto, user["id"]) == ["finance-sentry"]
    assert not user.get("isSuspended")
    assert "still holds finance-sentry: not suspended" in proc.stdout
    last = env("revoke", "friend@example.com", "--right", "finance-sentry")
    assert last.returncode == 0, last.stderr
    assert user["isSuspended"] is True  # the last right went: suspend


def test_revoke_with_a_platform_role_does_not_suspend(env):
    user = seed(env)
    give_role(env.logto, user["id"], "admin")
    proc = env("revoke", "friend@example.com")
    assert proc.returncode == 0, proc.stderr
    assert "holds platform role admin: not suspended" in proc.stdout
    assert not user.get("isSuspended")
    assert role_names(env.logto, user["id"]) == ["admin"]


def test_revoke_dry_run_writes_nothing(env, tmp_path):
    user = seed(env)
    before = env.ts.policy
    writes = len(logto_writes(env.logto))
    proc = env("revoke", "friend@example.com", "--dry-run", "--suspend-tailnet-user")
    assert proc.returncode == 0, proc.stderr
    assert "tailnet group:backstage: would remove" in proc.stdout
    assert "would remove role backstage" in proc.stdout
    assert "would suspend the user" in proc.stdout
    assert "tailnet user: would suspend" in proc.stdout
    assert env.ts.policy == before and len(logto_writes(env.logto)) == writes
    assert env.ts.suspended == []
    assert role_names(env.logto, user["id"]) == ["backstage"]
    assert ledger(tmp_path)[-1]["action"] != "people-revoke"


def test_revoke_suspends_the_tailnet_user_only_on_request(env):
    seed(env)
    proc = env("revoke", "friend@example.com", "--suspend-tailnet-user")
    assert proc.returncode == 0, proc.stderr
    assert "tailnet user: suspended" in proc.stdout
    assert env.ts.suspended == ["tsu-2"]
    assert env("revoke", "friend@example.com", "--suspend-tailnet-user").returncode == 0
    assert env.ts.suspended == ["tsu-2"]  # already suspended: no second call
    deletes = [r for r in env.ts.requests if "delete" in r[1] or r[0] == "DELETE"]
    assert deletes == []


def test_suspend_tailnet_user_needs_all_rights(env):
    seed(env)
    proc = env(
        "revoke", "friend@example.com", "--right", "backstage", "--suspend-tailnet-user"
    )
    assert proc.returncode == 1 and "drop --right" in proc.stderr
    assert logto_writes(env.logto)[-1][0] != "DELETE"


def test_revoke_unknown_right_or_email_writes_nothing(env):
    seed(env)
    count = len(logto_writes(env.logto))
    bad = env("revoke", "friend@example.com", "--right", "nope")
    assert bad.returncode == 1 and "unknown right" in bad.stderr
    assert env("revoke", "not-an-email").returncode == 1
    assert len(logto_writes(env.logto)) == count and env.ts.acl_writes()[1:] == []


def test_revoke_of_a_person_logto_does_not_know_still_cuts_the_network(env):
    env.ts.policy = POLICY.replace(
        '"group:backstage": [],', '"group:backstage": ["ghost@example.com"],'
    )
    proc = env("revoke", "ghost@example.com")
    assert proc.returncode == 0, proc.stderr
    assert "no user for ghost@example.com" in proc.stdout
    assert block_groups(env.ts.policy)["group:backstage"] == []


def test_revoke_works_on_a_policy_with_a_never_port_grant(env):
    seed(env)
    env.ts.policy = env.ts.policy.replace(
        '"ip": ["tcp:18810", "tcp:3001"]', '"ip": ["tcp:18810", "tcp:3002"]'
    )
    proc = env("revoke", "friend@example.com")
    assert proc.returncode == 0, proc.stderr  # a drifted policy never blocks a revoke
    assert block_groups(env.ts.policy)["group:backstage"] == []


def test_a_tailnet_failure_still_revokes_logto_then_exits_1(env):
    user = seed(env)
    env.ts.validate_error = "test(s) failed"
    proc = env("revoke", "friend@example.com")
    assert proc.returncode == 1
    assert "test(s) failed" in proc.stderr and "run revoke again" in proc.stderr
    assert role_names(env.logto, user["id"]) == [] and user["isSuspended"] is True
    env.ts.validate_error = None
    assert env("revoke", "friend@example.com").returncode == 0
    assert block_groups(env.ts.policy)["group:backstage"] == []


def test_revoke_without_the_oauth_client_fails_after_the_logto_half(env):
    user = seed(env)
    proc = env("revoke", "friend@example.com", tailnet_env=False)
    assert proc.returncode != 0
    assert "tailnet: not configured" in proc.stderr
    assert "run revoke again" in proc.stderr
    assert user["isSuspended"] is True


def test_invite_after_revoke_unsuspends(env):
    user = seed(env)
    env("revoke", "friend@example.com")
    proc = env("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    assert proc.returncode == 0, proc.stderr
    assert "unsuspended" in proc.stdout
    assert user["isSuspended"] is False
    assert role_names(env.logto, user["id"]) == ["backstage"]


def test_invite_refuses_a_user_suspended_outside_revoke(env, tmp_path):
    user = seed(env)
    user["isSuspended"] = True  # an admin suspended them in Logto
    writes, entries = len(logto_writes(env.logto)), len(ledger(tmp_path))
    acl = len(env.ts.acl_writes())
    proc = env("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    assert proc.returncode != 0
    assert "not by people revoke" in proc.stderr
    assert user["isSuspended"] is True
    assert len(logto_writes(env.logto)) == writes
    assert len(ledger(tmp_path)) == entries
    assert len(env.ts.acl_writes()) == acl


def test_invite_refuses_after_an_unrelated_suspend_follows_a_revoke(env):
    user = seed(env)
    env("revoke", "friend@example.com")
    assert (
        env(
            "invite", "friend@example.com", "--right", "backstage", "--host", HOST
        ).returncode
        == 0
    )
    # an admin suspends them again outside people (the ledger last says unsuspend)
    user["isSuspended"] = True
    proc = env("invite", "friend@example.com", "--right", "backstage", "--host", HOST)
    assert proc.returncode != 0
    assert user["isSuspended"] is True


def test_revoke_person_is_a_plain_function_that_validates_first():
    cat = people.load_catalog(people.CATALOG)
    with pytest.raises(people.AdminError, match="unknown right"):
        people.revoke_person(None, None, cat, "a@example.com", ["nope"])
    with pytest.raises(people.AdminError, match="not an email"):
        people.revoke_person(None, None, cat, "nope")
    with pytest.raises(people.AdminError, match="drop --right"):
        people.revoke_person(
            None, None, cat, "a@example.com", ["backstage"], suspend_tailnet=True
        )


# ─── status --notify ─────────────────────────────────────────────────────────


def soon(days):
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days, hours=1)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def drifted(env):
    env.ts.users.append({"loginName": "stranger@example.com", "role": "member"})


def test_notify_posts_drift_and_stays_silent_when_clean(env, relay):
    extra, envelopes, state = relay
    seed(env)
    clean(env)
    clean_run = env("status", "--notify", **extra)
    assert clean_run.returncode == 0, clean_run.stderr
    assert "nothing to alert on" in clean_run.stdout
    assert not (state / "calls").exists() and envelopes() == []
    drifted(env)
    proc = env("status", "--notify", **extra)
    assert proc.returncode == 0, proc.stderr
    (sent,) = envelopes()
    assert sent["level"] == "act" and sent["source"] == "people"
    assert sent["subject"] == "drift" and sent["action"] == "people.py status"
    assert "tailnet member stranger@example.com matches no Logto user" in sent["body"]
    assert len(sent["headline"]) <= 800 and len(sent["action"]) <= 400
    calls = (state / "calls").read_text().splitlines()
    assert calls[0].startswith("ps -q") and calls[1].startswith("exec -i abc123 node")


def test_status_without_notify_posts_nothing(env, relay):
    extra, envelopes, state = relay
    drifted(env)
    assert env("status", **extra).returncode == 0
    assert not (state / "calls").exists()


def test_a_token_within_14_days_alerts_as_wait(env, relay):
    extra, envelopes, _ = relay
    seed(env)
    clean(env)
    env.ts.key["expires"] = soon(10)
    proc = env("status", "--notify", **extra)
    assert proc.returncode == 0, proc.stderr
    assert "tailnet user token: expires" in proc.stdout
    (sent,) = envelopes()
    assert sent["level"] == "wait" and sent["subject"] == "tailnet-token"
    assert "expires on" in sent["headline"] and "in 10 days" in sent["headline"]
    assert "Settings > Keys" in sent["action"]


def test_a_token_beyond_14_days_or_without_expiry_is_quiet(env, relay):
    extra, envelopes, _ = relay
    seed(env)
    clean(env)
    env.ts.key["expires"] = soon(15)
    assert env("status", "--notify", **extra).returncode == 0
    env.ts.key = {"id": "k123"}
    proc = env("status", "--notify", **extra)
    assert "does not expire" in proc.stdout and envelopes() == []


def test_an_expired_or_rejected_token_alerts(env, relay):
    extra, envelopes, _ = relay
    seed(env)
    clean(env)
    env.ts.key["expires"] = soon(-3)
    env("status", "--notify", **extra)
    env.ts.key = {"id": "k123", "revoked": True}
    env("status", "--notify", **extra)
    heads = [e["headline"] for e in envelopes()]
    assert "expired on" in heads[0] and "revoked" in heads[1]


def test_drift_and_token_send_two_envelopes(env, relay):
    extra, envelopes, _ = relay
    seed(env)
    clean(env)
    drifted(env)
    env.ts.key["expires"] = soon(2)
    assert env("status", "--notify", **extra).returncode == 0
    assert [e["subject"] for e in envelopes()] == ["drift", "tailnet-token"]


def test_no_user_token_means_no_expiry_check(env, relay):
    extra, envelopes, _ = relay
    proc = env("status", "--notify", **extra, TAILSCALE_USER_TOKEN="")
    assert proc.returncode == 0 and "user token" not in proc.stdout
    assert [r for r in env.ts.requests if "/keys/" in r[1]] == []


def test_an_unreadable_expiry_is_reported_not_silent(env, relay):
    extra, envelopes, _ = relay
    seed(env)
    clean(env)
    env.ts.key_status = 403
    proc = env("status", "--notify", **extra)
    assert proc.returncode == 0, proc.stderr
    (sent,) = envelopes()
    assert "cannot check TAILSCALE_USER_TOKEN" in sent["headline"]


def test_a_relay_that_refuses_the_envelope_exits_1(env, relay):
    extra, envelopes, _ = relay
    drifted(env)
    proc = env("status", "--notify", **extra, POST_RC="1", POST_OUT="400 bad")
    assert proc.returncode == 1
    assert "alert not delivered" in proc.stderr and "400 bad" in proc.stderr
    gone = env("status", "--notify", **extra, NO_CONTAINER="1")
    assert gone.returncode == 1 and "no running notify-relay" in gone.stderr


def test_notify_takes_no_email(env, relay):
    extra, _, _ = relay
    proc = env("status", "member@example.com", "--notify", **extra)
    assert proc.returncode == 1 and "drop the email" in proc.stderr


def test_notify_works_without_the_tailnet(env, relay):
    extra, envelopes, _ = relay
    proc = env("status", "--notify", tailnet_env=False, **extra)
    assert proc.returncode == 0, proc.stderr
    assert "no product role" in envelopes()[0]["body"]


# ─── the tailnet unit ────────────────────────────────────────────────────────


def test_token_problem_boundaries():
    class T:
        def __init__(self, when):
            self.when = when

        def user_token_expiry(self):
            return self.when

    now = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
    at = lambda d: now + datetime.timedelta(days=d)  # noqa: E731
    assert people.token_problem(T(at(14)), now)[1] is None
    assert (
        "in 13 days"
        in people.token_problem(T(at(14) - datetime.timedelta(seconds=1)), now)[1]
    )
    assert "expired on" in people.token_problem(T(at(0)), now)[1]


def test_user_token_expiry_needs_a_tskey_api_token():
    ts = tailnet.Tailscale({"TAILSCALE_USER_TOKEN": "nonsense"})
    with pytest.raises(tailnet.TailnetError, match="not a tskey-api"):
        ts.user_token_expiry()
