"""Kit's wger client (skills/workout-log/wger.py, docs/runbook.md "Kit and wger").

A local HTTP server stands in for wger 2.7's API: it answers the shapes recorded from the
live read-only calls (version, profile, paginated lists) and records every request, so the
tests can assert what was written and that each call carried `Authorization: Token <token>`.
Every request it receives is checked against wger 2.7.0's own OpenAPI schema
(fixtures/wger-openapi-2.7.0.json, the subset of the live instance's /api/v2/schema the client
uses): a write body with an unknown field, a missing required field, a value outside its enum,
type, length or range, or a query parameter the endpoint does not declare is answered 400 and
fails the test. The same writer was also rehearsed against a throwaway wger/server:2.7.0
(docs/runbook.md, "Kit and wger"). The token below is a made-up placeholder.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

WGER = Path(__file__).resolve().parents[2] / "skills/workout-log/wger.py"
SKILL_MD = WGER.parent / "SKILL.md"
SPEC = json.loads(
    (Path(__file__).parent / "fixtures/wger-openapi-2.7.0.json").read_text()
)
TOKEN = "fake-wger-token-for-tests"
TOKEN_ENV = (
    "WGER_API_TOKEN"  # SKILL.md's primaryEnv; the client reads the token from it
)

EXERCISES = {  # English translation name -> wger exercise (base) id
    "Barbell Row": 101,
    "Squats": 615,
    "Bench Press": 192,
}
NAMES = {v: k for k, v in EXERCISES.items()}
LOGS = [
    # id, exercise, date, reps, weight, weight_unit
    (1, 192, "2026-11-10T18:00:00+00:00", 8, "60.00", 1),
    (2, 192, "2026-11-10T18:04:00+00:00", 8, "60.00", 1),
    (3, 192, "2026-11-10T18:08:00+00:00", 6, "60.00", 1),
    (4, 615, "2026-11-12T18:00:00+00:00", 5, "100.00", 1),
    (5, 615, "2026-11-12T18:05:00+00:00", 5, "100.00", 1),
    (6, 192, "2026-10-01T18:00:00+00:00", 10, "50.00", 1),  # outside the range
    (7, 101, "2026-11-20T18:00:00+00:00", 8, None, 1),  # weight is nullable in wger
    (8, 101, "2026-11-20T18:05:00+00:00", 8, "60.00", 1),
    (9, 101, "2026-11-21T18:00:00+00:00", 10, None, 1),
    (10, 101, "2026-11-21T18:05:00+00:00", 9, None, 1),
]


def _resolve(schema: dict) -> dict:
    while "$ref" in schema:
        schema = SPEC["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema


def validate(schema: dict, value: object, where: str = "body") -> list[str]:
    """The slice of OpenAPI 3.0 the wger writes use: nullable, type, enum, length, range,
    pattern, required, and (as wger's docs warn it silently drops them) unknown fields."""
    schema = _resolve(schema)
    if value is None:
        return (
            [] if schema.get("nullable") or "type" not in schema else [f"{where}: null"]
        )
    errs: list[str] = []
    kind = schema.get("type")
    checks = {
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "object": lambda v: isinstance(v, dict),
    }
    if kind and not checks[kind](value):
        return [f"{where}: {value!r} is not {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: {value!r} not in {schema['enum']}")
    if kind == "string":
        if len(value) > schema.get("maxLength", len(value)):
            errs.append(f"{where}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.match(schema["pattern"], value):
            errs.append(f"{where}: {value!r} does not match {schema['pattern']}")
        if schema.get("format") == "date" and not re.fullmatch(
            r"\d{4}-\d\d-\d\d", value
        ):
            errs.append(f"{where}: {value!r} is not a date")
    if kind == "integer":
        if value > schema.get("maximum", value) or value < schema.get("minimum", value):
            errs.append(f"{where}: {value} outside the allowed range")
    if kind == "object":
        props = schema["properties"]
        for key in value:
            if key not in props or props[key].get("readOnly"):
                errs.append(f"{where}: unknown or read-only field {key!r}")
        errs += [
            f"{where}: {k} is required"
            for k in schema.get("required", [])
            if k not in value
        ]
        for key, sub in props.items():
            if key in value:
                errs += validate(sub, value[key], f"{where}.{key}")
    return errs


def operation(method: str, path: str) -> dict | None:
    """The schema operation for a request path like routine/ or routine/5/."""
    for spec_path, ops in SPEC["paths"].items():
        pattern = re.sub(r"\{id\}", r"[^/]+", spec_path.removeprefix("/api/v2/"))
        if (
            re.fullmatch(pattern, path.removeprefix("/api/v2/"))
            and method.lower() in ops
        ):
            return ops[method.lower()]
    return None


def schema_errors(method: str, path: str, query: dict, body: object) -> list[str]:
    op = operation(method, path)
    if op is None:
        return [f"{method} {path}: not an endpoint wger 2.7 declares"]
    declared = {p["name"] for p in op.get("parameters", []) if p["in"] == "query"}
    errs = [f"query {k!r} is not declared" for k in query if k not in declared]
    if method == "POST":
        body_schema = op["requestBody"]["content"]["application/json"]["schema"]
        errs += validate(body_schema, body)
    return errs


class FakeWger(BaseHTTPRequestHandler):
    log: list[dict] = []
    mode = "ok"  # ok | revoked
    next_id = 1000
    routines: list[dict] = []
    rejected: list[str] = []  # schema violations in what the client sent
    attached: set[int] = set()  # routine ids that hold workout logs or sessions
    fail_on: tuple[str, int] | None = None  # (endpoint, nth POST to it) answered 500
    posted: dict[str, int] = {}

    def _send(self, code: int, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method: str) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        auth = self.headers.get("Authorization")
        type(self).log.append(
            {
                "method": method,
                "path": url.path,
                "query": query,
                "body": body,
                "auth": auth,
            }
        )
        path = url.path.removeprefix("/api/v2/").strip("/")
        problems = schema_errors(method, url.path.removeprefix("/api/v2/"), query, body)
        if problems:
            type(self).rejected += problems
            return self._send(400, {"detail": problems})
        if path == "version":
            return self._send(200, "2.7.0")
        if type(self).mode == "revoked" or auth != f"Token {TOKEN}":
            return self._send(401, {"detail": "Invalid token."})
        if path == "userprofile":
            return self._send(200, {"username": "owner", "weight_unit": "kg"})
        if path == "exercise-translation" and method == "GET":
            if "exercise" in query:
                n = NAMES.get(int(query["exercise"]))
                rows = (
                    [{"exercise": int(query["exercise"]), "name": n, "language": 2}]
                    if n
                    else []
                )
                # wger has the exercise in other languages too; the client wants English
                rows.insert(
                    0,
                    {
                        "exercise": int(query["exercise"]),
                        "name": "Autre",
                        "language": 12,
                    },
                )
            else:
                eid = EXERCISES.get(query.get("name", ""))
                rows = (
                    [{"exercise": eid, "name": query["name"], "language": 2}]
                    if eid
                    else []
                )
                if eid:  # the same name in another language must not match
                    rows.insert(
                        0, {"exercise": 9999, "name": query["name"], "language": 12}
                    )
            return self._send(200, {"count": len(rows), "next": None, "results": rows})
        if path == "routine" and method == "GET":
            rows = type(self).routines
            return self._send(200, {"count": len(rows), "next": None, "results": rows})
        if path.startswith("routine/") and method == "DELETE":
            type(self).routines = [
                r for r in type(self).routines if str(r["id"]) != path.split("/")[1]
            ]
            return self._send(204, None)
        if method == "POST":
            n = type(self).posted[path] = type(self).posted.get(path, 0) + 1
            if type(self).fail_on == (path, n):
                return self._send(500, {"detail": "boom"})
            type(self).next_id += 1
            created = {**body, "id": type(self).next_id}
            if path == "routine":
                type(self).routines.append(created)
            return self._send(201, created)
        if path in ("workoutlog", "workoutsession") and "routine" in query:
            hit = int(query["routine"]) in type(self).attached
            return self._send(
                200, {"count": int(hit), "next": None, "results": [{}] * int(hit)}
            )
        if path == "workoutlog":
            rows = [
                {
                    "id": i,
                    "exercise": e,
                    "date": d,
                    "repetitions": r,
                    "weight": w,
                    "weight_unit": u,
                }
                for (i, e, d, r, w, u) in LOGS
            ]
            if "exercise" in query:
                rows = [r for r in rows if r["exercise"] == int(query["exercise"])]
            # Page the answer so the client has to follow `next`.
            offset = int(query.get("offset", 0))
            page = rows[offset : offset + 4]
            more = offset + 4 < len(rows)
            nxt = (
                f"http://127.0.0.1:{self.server.server_port}/api/v2/workoutlog/?{url.query.split('&offset')[0]}&offset={offset + 4}"
                if more
                else None
            )
            return self._send(200, {"count": len(rows), "next": nxt, "results": page})
        return self._send(404, {"detail": "Not found."})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, *args: object) -> None:  # keep test output quiet
        pass


@pytest.fixture
def wger():
    FakeWger.log, FakeWger.mode, FakeWger.routines, FakeWger.next_id = (
        [],
        "ok",
        [],
        1000,
    )
    FakeWger.rejected, FakeWger.attached = [], set()
    FakeWger.fail_on, FakeWger.posted = None, {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWger)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def run(*args: str, token: str | None = TOKEN):
        env = {"PATH": os.environ["PATH"], "WGER_BASE_URL": base}
        if token is not None:
            env[TOKEN_ENV] = token
        return subprocess.run(
            [sys.executable, str(WGER), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    yield run
    server.shutdown()
    assert FakeWger.rejected == [], "the client sent what wger 2.7's schema rejects"


@pytest.fixture
def gym(tmp_path):
    p = tmp_path / "gym.json"
    p.write_text(
        json.dumps(
            {
                "name": "Gym",
                "days": [
                    {
                        "day": "mon",
                        "name": "back",
                        "exercises": [
                            {
                                "name": "barbell-row",
                                "sets": 3,
                                "reps": "8-12",
                                "weight": 60,
                            }
                        ],
                    },
                    {
                        "day": "wed",
                        "name": "legs",
                        "exercises": [
                            {
                                "name": "squat",
                                "aliases": ["squats"],
                                "sets": 5,
                                "reps": 5,
                            }
                        ],
                    },
                ],
            }
        )
    )
    return p


def writes():
    return [r for r in FakeWger.log if r["method"] in ("POST", "DELETE")]


# --- write a routine ---------------------------------------------------------


def test_routine_is_written_with_days_slots_entries_and_configs(wger, gym):
    r = wger("routine", str(gym), "--start", "2026-11-23", "--weeks", "8")
    assert r.returncode == 0, r.stderr
    assert (
        "routine 'Gym' written to wger" in r.stdout
        and "2 days, 2 exercises" in r.stdout
    )
    posts = [(w["path"].removeprefix("/api/v2/"), w["body"]) for w in writes()]
    by = {}
    for path, body in posts:
        by.setdefault(path.strip("/"), []).append(body)
    assert by["routine"][0]["name"] == "Gym"
    assert (by["routine"][0]["start"], by["routine"][0]["end"]) == (
        "2026-11-23",
        "2027-01-18",
    )
    assert [d["name"] for d in by["day"]] == ["Mon - back", "Wed - legs"]
    assert [d["order"] for d in by["day"]] == [1, 2]
    assert [e["exercise"] for e in by["slot-entry"]] == [
        101,
        615,
    ]  # name and alias resolved
    assert {c["value"] for c in by["sets-config"]} == {3, 5}
    # wger's decimal fields travel as strings (the schema's `type: string, format: decimal`)
    assert [c["value"] for c in by["repetitions-config"]] == ["8", "5"]
    assert [c["value"] for c in by["max-repetitions-config"]] == [
        "12"
    ]  # only the range has a max
    assert [c["value"] for c in by["weight-config"]] == [
        "60"
    ]  # only the weighted exercise
    assert all(
        w["auth"] == f"Token {TOKEN}"
        for w in FakeWger.log
        if w["path"] != "/api/v2/version/"
    )


def test_routine_dry_run_writes_nothing(wger, gym):
    r = wger("routine", str(gym), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "barbell-row 3x8-12" in r.stdout
    assert writes() == []


def test_routine_refuses_an_existing_name_unless_replaced(wger, gym):
    assert wger("routine", str(gym)).returncode == 0
    n = len(writes())
    again = wger("routine", str(gym))
    assert again.returncode == 1 and "already has a routine named 'Gym'" in again.stderr
    assert len(writes()) == n
    replaced = wger("routine", str(gym), "--replace")
    assert replaced.returncode == 0, replaced.stderr
    assert any(w["method"] == "DELETE" for w in writes())
    assert [r["name"] for r in FakeWger.routines] == ["Gym"]


def test_replace_deletes_the_old_routine_only_after_the_new_one_is_built(wger, gym):
    assert wger("routine", str(gym)).returncode == 0
    old = FakeWger.routines[0]["id"]
    FakeWger.log.clear()
    assert wger("routine", str(gym), "--replace").returncode == 0
    methods = [(w["method"], w["path"]) for w in writes()]
    assert methods[-1] == ("DELETE", f"/api/v2/routine/{old}/")
    assert [m for m, _ in methods[:-1]] == ["POST"] * (len(methods) - 1)
    assert [r["id"] for r in FakeWger.routines] != [old]


def test_a_failed_write_deletes_the_half_built_routine(wger, gym):
    FakeWger.fail_on = ("slot-entry", 2)
    r = wger("routine", str(gym))
    assert r.returncode == 4 and "HTTP 500" in r.stderr
    assert FakeWger.routines == []
    assert writes()[-1]["method"] == "DELETE"


def test_a_failed_replace_keeps_the_old_routine(wger, gym):
    assert wger("routine", str(gym)).returncode == 0
    old = [r["id"] for r in FakeWger.routines]
    FakeWger.posted = {}
    FakeWger.fail_on = ("slot-entry", 2)
    r = wger("routine", str(gym), "--replace")
    assert r.returncode == 4 and "HTTP 500" in r.stderr
    assert [r["id"] for r in FakeWger.routines] == old
    assert [w["method"] for w in writes()].count("DELETE") == 1  # the half-built one


def test_replace_refuses_a_routine_with_logs_or_sessions(wger, gym):
    assert wger("routine", str(gym)).returncode == 0
    FakeWger.attached = {FakeWger.routines[0]["id"]}
    FakeWger.log.clear()
    r = wger("routine", str(gym), "--replace")
    assert r.returncode == 1 and "has workout logs or sessions" in r.stderr
    assert "--name" in r.stderr
    assert writes() == [] and len(FakeWger.routines) == 1


def test_a_routine_name_wger_would_reject_stops_the_run_before_any_write(wger, gym):
    r = wger("routine", str(gym), "--name", "x" * 26)
    assert r.returncode == 1 and "25 characters" in r.stderr
    assert writes() == []


@pytest.mark.parametrize(
    "day,exercise,needle",
    [
        ({"day": "Mon", "name": "y" * 20}, {}, "20-character day name"),
        ({}, {"sets": 51}, "sets must be"),
        ({}, {"weight": 10000}, "weight must be"),
        ({}, {"weight": -5}, "weight must be"),
        ({}, {"reps": 10000}, "reps are above"),
    ],
)
def test_values_wger_would_reject_stop_the_run_before_any_write(
    wger, tmp_path, day, exercise, needle
):
    ex = {"name": "barbell-row", "sets": 3, "reps": 8, "weight": 60, **exercise}
    p = tmp_path / "gym.json"
    p.write_text(
        json.dumps({"days": [{"day": "Mon", "name": "a", **day, "exercises": [ex]}]})
    )
    r = wger("routine", str(p))
    assert r.returncode == 1 and needle in r.stderr
    assert writes() == []


def test_routine_with_an_unknown_exercise_writes_nothing(wger, tmp_path):
    p = tmp_path / "gym.json"
    p.write_text(
        json.dumps(
            {
                "days": [
                    {
                        "day": "Fri",
                        "exercises": [
                            {"name": "cable-fly", "sets": 3, "reps": "10-15"}
                        ],
                    }
                ]
            }
        )
    )
    r = wger("routine", str(p))
    assert r.returncode == 1
    assert "cable-fly" in r.stderr and "wger_exercise" in r.stderr
    assert writes() == []


def test_routine_takes_an_explicit_wger_exercise_id(wger, tmp_path):
    p = tmp_path / "gym.json"
    p.write_text(
        json.dumps(
            {
                "days": [
                    {
                        "day": "Fri",
                        "exercises": [
                            {
                                "name": "cable-fly",
                                "wger_exercise": 77,
                                "sets": 3,
                                "reps": [10, 15],
                            }
                        ],
                    }
                ]
            }
        )
    )
    assert wger("routine", str(p)).returncode == 0
    assert [
        w["body"]["exercise"] for w in writes() if w["path"].endswith("/slot-entry/")
    ] == [77]


def test_routine_ignores_fields_it_does_not_know(wger, tmp_path):
    p = tmp_path / "gym.json"
    p.write_text(
        json.dumps(
            {
                "version": 3,
                "deload": {"after": 3, "factor": 0.9},
                "cardio": {"name": "incline-walk"},
                "days": {
                    "Mon": {
                        "name": "back",
                        "muscle": "back",
                        "exercises": [
                            {
                                "name": "barbell-row",
                                "sets": 3,
                                "reps": [8, 12],
                                "step": 2.5,
                                "progression": "double",
                                "note": "x",
                            }
                        ],
                    },
                },
            }
        )
    )
    r = wger("routine", str(p))
    assert r.returncode == 0, r.stderr
    assert [w["body"]["name"] for w in writes() if w["path"].endswith("/day/")] == [
        "Mon - back"
    ]


@pytest.mark.parametrize(
    "doc,needle",
    [
        ({"days": []}, "no days"),
        ({"days": [{"day": "Mon", "exercises": [{"name": "x", "sets": 3}]}]}, "reps"),
        (
            {
                "days": [
                    {"day": "Mon", "exercises": [{"name": "x", "sets": 0, "reps": 5}]}
                ]
            },
            "sets",
        ),
    ],
)
def test_routine_rejects_a_malformed_program(wger, tmp_path, doc, needle):
    p = tmp_path / "gym.json"
    p.write_text(json.dumps(doc))
    r = wger("routine", str(p))
    assert r.returncode == 1 and needle in r.stderr
    assert writes() == []


# --- read the logs back ------------------------------------------------------


def test_logs_group_sets_by_day_and_exercise_within_the_range(wger):
    r = wger("logs", "--from", "2026-11-01", "--to", "2026-11-30")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "2026-11-10 bench-press: 8,8,6@60kg",  # three sets over a paged answer
        "2026-11-12 squats: 5,5@100kg",
        "2026-11-20 barbell-row: 8, 8@60kg",
        "2026-11-21 barbell-row: 10,9",
    ]
    q = [x["query"] for x in FakeWger.log if x["path"] == "/api/v2/workoutlog/"][0]
    assert (q["date__gte"], q["date__lte"]) == ("2026-11-01", "2026-11-30T23:59:59")


def test_logs_filter_by_exercise(wger):
    r = wger(
        "logs",
        "--from",
        "2026-10-01",
        "--to",
        "2026-11-30",
        "--exercise",
        "bench press",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "2026-10-01 bench-press: 10@50kg",
        "2026-11-10 bench-press: 8,8,6@60kg",
    ]


def test_logs_json_carries_reps_weight_and_unit(wger):
    r = wger("logs", "--from", "2026-11-12", "--to", "2026-11-12", "--json")
    assert json.loads(r.stdout) == [
        {
            "date": "2026-11-12",
            "exercise": "squats",
            "sets": [{"reps": 5, "weight": "100.00", "unit": "kg"}] * 2,
        }
    ]


def test_logs_with_a_null_weight_next_to_a_weighted_set_do_not_crash(wger):
    r = wger("logs", "--from", "2026-11-20", "--to", "2026-11-20")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["2026-11-20 barbell-row: 8, 8@60kg"]
    assert json.loads(
        wger("logs", "--from", "2026-11-20", "--to", "2026-11-20", "--json").stdout
    ) == [
        {
            "date": "2026-11-20",
            "exercise": "barbell-row",
            "sets": [
                {"reps": 8, "weight": None, "unit": "kg"},
                {"reps": 8, "weight": "60.00", "unit": "kg"},
            ],
        }
    ]


def test_logs_with_no_weight_on_any_set_print_just_the_reps(wger):
    r = wger("logs", "--from", "2026-11-21", "--to", "2026-11-21")
    assert r.stdout.splitlines() == ["2026-11-21 barbell-row: 10,9"]


def test_logs_with_nothing_in_range_say_so(wger):
    r = wger("logs", "--from", "2025-01-01", "--to", "2025-01-31")
    assert r.returncode == 0 and "no workout logs" in r.stdout


def test_logs_reject_bad_dates(wger):
    assert wger("logs", "--from", "11/01", "--to", "2026-11-30").returncode == 1
    assert wger("logs", "--from", "2026-12-01", "--to", "2026-11-30").returncode == 1


# --- the token paths ---------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "   "])
def test_a_missing_token_stops_before_any_call(wger, gym, token):
    for args in (
        ("logs", "--from", "2026-11-01", "--to", "2026-11-30"),
        ("routine", str(gym)),
        ("check",),
    ):
        r = wger(*args, token=token)
        assert r.returncode == 2, args
        assert "WGER_API_TOKEN is not set" in r.stderr
    assert [x for x in FakeWger.log if x["path"] != "/api/v2/version/"] == []


def test_a_revoked_token_is_named_and_writes_nothing(wger, gym):
    FakeWger.mode = "revoked"
    for args in (
        ("logs", "--from", "2026-11-01", "--to", "2026-11-30"),
        ("routine", str(gym)),
        ("check",),
    ):
        r = wger(*args)
        assert r.returncode == 3, args
        assert "rejected the token" in r.stderr and "revoked" in r.stderr
    assert writes() == []


def test_a_wrong_token_is_rejected_the_same_way(wger):
    r = wger("logs", "--from", "2026-11-01", "--to", "2026-11-30", token="f" * 40)
    assert r.returncode == 3


def test_the_token_never_appears_in_output(wger, gym):
    FakeWger.mode = "ok"
    outs = []
    for args in (
        ("check",),
        ("routine", str(gym)),
        ("logs", "--from", "2026-11-01", "--to", "2026-11-30"),
    ):
        r = wger(*args)
        outs += [r.stdout, r.stderr]
    FakeWger.mode = "revoked"
    r = wger("check")
    outs += [r.stdout, r.stderr]
    assert TOKEN not in "".join(outs)


def test_check_reads_the_version_then_the_profile(wger):
    r = wger("check")
    assert r.returncode == 0, r.stderr
    assert "wger 2.7.0" in r.stdout and "token: accepted" in r.stdout
    assert [x["path"] for x in FakeWger.log] == [
        "/api/v2/version/",
        "/api/v2/userprofile/",
    ]
    assert FakeWger.log[0]["auth"] is None  # the version probe is anonymous


def test_an_unreachable_wger_is_exit_4(tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        TOKEN_ENV: TOKEN,
        "WGER_BASE_URL": "http://127.0.0.1:9",
    }
    r = subprocess.run(
        [
            sys.executable,
            str(WGER),
            "logs",
            "--from",
            "2026-11-01",
            "--to",
            "2026-11-30",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 4 and "unreachable" in r.stderr


# --- the wiring --------------------------------------------------------------


def skill_frontmatter() -> dict:
    """SKILL.md's YAML frontmatter: `key: value` lines, the metadata value being JSON."""
    block = SKILL_MD.read_text().split("---\n")[1]
    fields = dict(line.split(": ", 1) for line in block.strip().splitlines())
    fields["metadata"] = json.loads(fields["metadata"])
    return fields


def test_the_platform_patch_wires_the_token_as_the_skills_api_key():
    patch = json.loads(
        (WGER.parents[2] / "compose/openclaw-gateway/platform.patch.json").read_text()
    )
    skill = skill_frontmatter()
    ref = patch["skills"]["entries"][skill["name"]]["apiKey"]
    assert ref == {"source": "exec", "provider": "sops", "id": "wger-api-token"}
    # OpenClaw hands the apiKey to the skill's run as the env var its primaryEnv names;
    # wger.py reads that same variable.
    assert skill["metadata"]["openclaw"]["primaryEnv"] == TOKEN_ENV


def test_the_schema_check_rejects_what_wger_would_not_accept():
    post = lambda path, body: schema_errors("POST", path, {}, body)  # noqa: E731
    day = {"routine": 1, "name": "Mon", "order": 1, "is_rest": False, "type": "custom"}
    assert post("day/", day) == []
    assert post("day/", {**day, "type": "bogus"})
    assert post("day/", {**day, "extra": 1})
    assert post("day/", {**day, "name": "n" * 21})
    assert post("day/", {"name": "x"})  # routine is required
    cfg = {
        "slot_entry": 1,
        "iteration": 1,
        "value": "8",
        "operation": "r",
        "step": "na",
    }
    assert post("repetitions-config/", cfg) == []
    assert post("repetitions-config/", {**cfg, "value": 8})  # a decimal is a string
    assert post("sets-config/", {**cfg, "value": 51})
    assert schema_errors("GET", "exercise-translation/", {"language": "2"}, None)
