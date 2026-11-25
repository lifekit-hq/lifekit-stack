#!/usr/bin/env python3
"""wger.py - Kit's uses of the wger workout manager (docs/runbook.md, "Workout manager (wger)").

  wger.py check                                  # GET /api/v2/version/, then an authenticated profile read
  wger.py routine <gym.json> [--name N] [--start YYYY-MM-DD] [--weeks N] [--replace] [--dry-run]
  wger.py logs --from YYYY-MM-DD --to YYYY-MM-DD [--exercise NAME] [--json]
  wger.py nutrition <plan.json> [--dry-run]
  wger.py profile [--birthdate YYYY-MM-DD] [--height CM] [--weight KG] [--date YYYY-MM-DD] [--dry-run]
  wger.py log-write "<exercises>" [--date YYYY-MM-DD] [--dry-run]
  wger.py log-delete <YYYY-MM-DD> <exercise> [--dry-run]

`routine` writes Kit's routine definition (routines/gym.json, the file Kit keeps in its
workspace) into wger as a routine: one wger day per program day, one slot per exercise, one
slot entry per slot with its sets, rep range and weight. `logs` reads the workout logs back:
date, exercise, sets, reps and weight, grouped the way the skill replies.

Unknown fields in gym.json (steps, notes, deload rules, anything else) are ignored.

`nutrition` writes one nutrition plan from a JSON file ({"name", "kcal", "protein", "carbs",
"fat"}, optional "fiber", "start", "end"): it is created, or updated when wger already has a plan
with that name (wger calls the name the plan's `description`), and left alone when the goals
already match. `profile` sets the profile fields wger exposes (birth date, height in cm) and adds
a weight entry for the day, or corrects that day's entry. `log-write` writes a session as wger
workout logs, one per set, from the same line Kit gives `workout-claw log` ("hack squat 3x10@60kg,
bench press 8,8,6@60kg"): each exercise is linked to the routine covering the date and its slot
entry, and an exercise that already has logs on that date is never written twice (identical sets:
unchanged; the first sets of the line, left by an interrupted run: the rest is written;
any other difference: exit 1, nothing overwritten). workout-claw stays the record of the
session; a wger failure here never touches it.

`log-delete` takes back what `log-write` wrote for one exercise on one date: every workout log
of that exercise that day, then each workout session that held one of those logs and is empty
afterwards. It reports what it removed, says "nothing to remove" when there is nothing (so
running it again is safe), and with --dry-run only says what it would remove. It never touches
another exercise's logs, a session that still holds one, or a session that held none of the
deleted logs (an app-made session with notes stays).

`routine` builds the whole new routine first and deletes the old one (`--replace`) only after
it is complete; a failed build deletes the half-built routine and leaves the old one. Deleting
a wger routine also deletes the workout logs and sessions attached to it (wger 2.7 models:
on_delete=CASCADE), so `--replace` refuses a routine that has any, before writing anything.
Field limits (25-character routine name, 20-character day name, 1-50 sets) and the payload
shapes are wger 2.7's OpenAPI schema (scripts/tests/fixtures/wger-openapi-2.7.0.json).

Environment (set by OpenClaw for the skill's run, never by hand):
    WGER_API_TOKEN  the wger token, injected from the gateway secrets file through the
                    skill's `apiKey` SecretRef (primaryEnv in SKILL.md). Sent only as
                    `Authorization: Token <token>`; never printed, never in an error line.
    WGER_BASE_URL   the wger base URL, no trailing slash needed (default http://wger-nginx-1:8080).

Exit codes: 0 ok, 1 usage/data error, 2 no token, 3 token rejected (revoked or wrong), 4 wger
unreachable or it answered something unexpected. Stdlib only: this file runs in the gateway
image and under scripts/tests/test_wger_skill.py on the host.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://wger-nginx-1:8080"
LANGUAGE_EN = 2  # wger's language id for English
UNIT_REPS, UNIT_KG = 1, 1
# Field limits from wger 2.7's own OpenAPI schema (scripts/tests/fixtures/wger-openapi-2.7.0.json).
ROUTINE_NAME_MAX, DAY_NAME_MAX, SETS_MAX, NUMBER_MAX = 25, 20, 50, 9999
PLAN_NAME_MAX, INT_MAX = (
    80,
    2147483647,
)  # nutrition plan `description`; wger's integer fields
HEIGHT_MIN, HEIGHT_MAX = 140, 230  # userprofile.height (cm)
UNIT_LB, UNIT_BW = 2, 3
WEIGHT_UNITS = {1: "kg", 2: "lb", 3: "bw"}
WEEKDAYS = {
    "mon": "Mon", "monday": "Mon", "tue": "Tue", "tuesday": "Tue", "wed": "Wed",
    "wednesday": "Wed", "thu": "Thu", "thursday": "Thu", "fri": "Fri", "friday": "Fri",
    "sat": "Sat", "saturday": "Sat", "sun": "Sun", "sunday": "Sun",
}  # fmt: skip


class Fail(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class Wger:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self._token = token

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        url = (
            path
            if path.startswith("http")
            else f"{self.base}/api/v2/{path.lstrip('/')}"
        )
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if self._token:
            req.add_header("Authorization", f"Token {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - base URL is operator config
                raw = resp.read()
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise Fail(
                    "wger rejected the token (HTTP %d): it is revoked or wrong - "
                    "docs/runbook.md, 'wger token revoked'" % err.code,
                    3,
                ) from None
            detail = err.read(300).decode("utf-8", "replace").strip()
            raise Fail(
                f"wger answered HTTP {err.code} for {method} {path}: {detail}", 4
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise Fail(f"wger is unreachable at {self.base}: {reason}", 4) from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise Fail(f"wger answered non-JSON for {method} {path}", 4) from None

    def pages(self, path: str, params: dict[str, str]) -> list[dict]:
        """Every row of a paginated list endpoint."""
        url: str | None = f"{path}?{urllib.parse.urlencode({**params, 'limit': 100})}"
        rows: list[dict] = []
        while url:
            page = self.call("GET", url)
            if not isinstance(page, dict) or "results" not in page:
                raise Fail(f"wger answered an unexpected list for {path}", 4)
            rows.extend(page["results"])
            url = page.get("next")
        return rows


def client() -> Wger:
    token = os.environ.get("WGER_API_TOKEN", "").strip()
    if not token:
        raise Fail(
            "WGER_API_TOKEN is not set: the skill's apiKey SecretRef did not reach this run "
            "(docs/runbook.md, 'Kit and wger')",
            2,
        )
    return Wger(os.environ.get("WGER_BASE_URL") or DEFAULT_BASE, token)


# --- exercise names ----------------------------------------------------------


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def name_candidates(name: str) -> list[str]:
    """`bench-press` -> the spellings wger's English translations are likely to use."""
    words = re.sub(r"[-_\s]+", " ", name).strip()
    out = [name, words, words.title(), words.capitalize(), words.lower()]
    for base in list(out):
        if not base.endswith("s"):
            out.append(base + "s")
    seen: list[str] = []
    for c in out:
        if c and c not in seen:
            seen.append(c)
    return seen


# Spellings an owner types that wger does not carry as a name, tried after the name itself and
# the entry's own aliases: slug of the typed name -> wger English names. wger has several curls
# (barbell, dumbbell, SZ-bar, cable, machine); the plain "biceps curl" is the dumbbell one the
# owner's routine uses ("Dumbbell Curl", 1931). log-write prefers the routine's own exercise
# (resolve_for_routine), so this is the fallback for a day the routine does not cover.
BUILTIN_ALIASES = {
    slug(spelling): ["Dumbbell Curl"]
    for spelling in ("bicep curl", "biceps curl", "bicep curls", "biceps curls")
}


def resolve_exercise(w: Wger, ex: dict, cache: dict[str, int]) -> int:
    """The wger exercise (base) id for one program exercise: explicit id, else an exact
    English name match on the exercise's name or aliases. Never a guess."""
    if isinstance(ex.get("wger_exercise"), int):
        return ex["wger_exercise"]
    names = [
        ex["name"],
        *[a for a in ex.get("aliases", []) if isinstance(a, str)],
        *BUILTIN_ALIASES.get(slug(ex["name"]), []),
    ]
    for name in names:
        for cand in name_candidates(name):
            if cand in cache:
                return cache[cand]
            rows = w.pages("exercise-translation/", {"name": cand})
            hit = [
                r
                for r in rows
                if r.get("language") == LANGUAGE_EN
                and slug(r.get("name", "")) == slug(cand)
            ]
            if hit:
                cache[cand] = hit[0]["exercise"]
                return hit[0]["exercise"]
    raise Fail(
        f'no wger exercise named {ex["name"]!r}: add "wger_exercise": <id> to it in gym.json '
        "(the id is on the exercise's wger page) or an alias that matches wger's name",
        1,
    )


def name_key(name: str) -> str:
    """slug with a trailing plural `s` dropped, so `squats` and `Squat` compare equal."""
    return slug(name).removesuffix("s")


def resolve_for_routine(
    w: Wger,
    ex: dict,
    entries: dict[int, int],
    names: dict[int, str],
    cache: dict[str, int],
) -> tuple[int, bool]:
    """(exercise id, is a routine slot). The routine's own exercise when its name matches the
    typed name or one of its aliases; the usual resolution (aliases, wger's names) otherwise."""
    wanted = {
        name_key(n)
        for n in [
            ex["name"],
            *[a for a in ex.get("aliases", []) if isinstance(a, str)],
            *BUILTIN_ALIASES.get(slug(ex["name"]), []),
        ]
    }
    for eid in entries:
        if name_key(exercise_name(w, eid, names)) in wanted:
            return eid, True
    eid = resolve_exercise(w, ex, cache)
    return eid, eid in entries


def exercise_name(w: Wger, ex_id: int, cache: dict[int, str]) -> str:
    if ex_id not in cache:
        rows = [
            r
            for r in w.pages("exercise-translation/", {"exercise": str(ex_id)})
            if r.get("language") == LANGUAGE_EN
        ]
        cache[ex_id] = slug(rows[0]["name"]) if rows else f"exercise-{ex_id}"
    return cache[ex_id]


# --- gym.json ----------------------------------------------------------------


def parse_reps(ex: dict) -> tuple[int, int]:
    raw = next(
        (
            ex[k]
            for k in ("reps", "rep_range", "range", "rep_min")
            if ex.get(k) is not None
        ),
        None,
    )
    if ex.get("rep_min") is not None and ex.get("rep_max") is not None:
        raw = [ex["rep_min"], ex["rep_max"]]
    try:
        if isinstance(raw, bool) or raw is None:
            raise ValueError
        if isinstance(raw, int | float):
            return int(raw), int(raw)
        if isinstance(raw, str):
            nums = [int(n) for n in re.findall(r"\d+", raw)]
        else:
            nums = [int(n) for n in raw]
    except (ValueError, TypeError):
        nums = []
    if len(nums) == 1:
        return nums[0], nums[0]
    if len(nums) == 2 and 0 < nums[0] <= nums[1]:
        return nums[0], nums[1]
    raise Fail(
        f"exercise {ex.get('name')!r}: reps must be a number or a range like 8-12", 1
    )


def load_program(path: str) -> tuple[str, list[dict]]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as err:
        raise Fail(f"cannot read {path}: {err.strerror}", 1) from None
    except json.JSONDecodeError as err:
        raise Fail(f"{path} is not valid JSON: {err}", 1) from None
    days_raw = next(
        (
            doc[k]
            for k in ("days", "split", "program", "schedule")
            if isinstance(doc, dict) and doc.get(k)
        ),
        None,
    )
    if isinstance(days_raw, dict):
        days_raw = [{"day": k, **v} for k, v in days_raw.items() if isinstance(v, dict)]
    if not isinstance(days_raw, list) or not days_raw:
        raise Fail(
            f'{path} has no days: expected a "days" list of {{day, name, exercises}}', 1
        )
    days = []
    for d in days_raw:
        exercises = (
            (d.get("exercises") or d.get("lifts")) if isinstance(d, dict) else None
        )
        if not exercises:
            raise Fail(
                f"day {d.get('day') if isinstance(d, dict) else d!r} has no exercises",
                1,
            )
        parsed = []
        for ex in exercises:
            if not isinstance(ex, dict) or not isinstance(ex.get("name"), str):
                raise Fail('every exercise needs a "name"', 1)
            lo, hi = parse_reps(ex)
            sets = ex.get("sets", ex.get("set_count"))
            if (
                not isinstance(sets, int)
                or isinstance(sets, bool)
                or not 1 <= sets <= SETS_MAX
            ):
                raise Fail(
                    f"exercise {ex['name']!r}: sets must be a whole number 1-{SETS_MAX}",
                    1,
                )
            if hi > NUMBER_MAX:
                raise Fail(f"exercise {ex['name']!r}: reps are above {NUMBER_MAX}", 1)
            weight = ex.get("weight", ex.get("start_weight", ex.get("start_kg")))
            if weight is not None and (
                isinstance(weight, bool)
                or not isinstance(weight, int | float)
                or not 0 <= weight <= NUMBER_MAX
            ):
                raise Fail(
                    f"exercise {ex['name']!r}: weight must be a number in kg, 0-{NUMBER_MAX}",
                    1,
                )
            parsed.append(
                {**ex, "sets": sets, "rep_lo": lo, "rep_hi": hi, "weight": weight}
            )
        label = str(d.get("day", "")).strip()
        label = WEEKDAYS.get(label.lower(), label)
        title = (
            " - ".join(p for p in (label, str(d.get("name", "")).strip()) if p) or "Day"
        )
        if len(title) > DAY_NAME_MAX:
            raise Fail(
                f"day {title!r} is longer than wger's {DAY_NAME_MAX}-character day name: "
                "shorten its day or name in gym.json",
                1,
            )
        days.append({"title": title, "exercises": parsed})
    name = (
        doc.get("name")
        if isinstance(doc.get("name"), str) and doc["name"].strip()
        else "Gym"
    )
    return name.strip(), days


# --- commands ----------------------------------------------------------------


def cmd_check(_: argparse.Namespace) -> int:
    base = os.environ.get("WGER_BASE_URL") or DEFAULT_BASE
    version = Wger(base, "").call("GET", "version/")  # public: no token needed
    print(f"wger {version} at {base.rstrip('/')}")
    profile = client().call("GET", "userprofile/")
    if not isinstance(profile, dict) or "username" not in profile:
        raise Fail("wger answered an unexpected profile", 4)
    print("token: accepted (authenticated profile read ok)")
    return 0


def dec(x: object) -> str:
    """A wger decimal field is a string with at most two decimals: 60 -> "60", 62.5 -> "62.5"."""
    return f"{float(x):.2f}".rstrip("0").rstrip(".")  # type: ignore[arg-type]


def has_history(w: Wger, rid: int) -> bool:
    """True when wger holds workout logs or sessions for the routine: deleting a routine
    cascades to both (wger 2.7 models), so a routine with history is never replaced."""
    for path in ("workoutlog/", "workoutsession/"):
        page = w.call("GET", f"{path}?routine={rid}&limit=1")
        if not isinstance(page, dict) or "count" not in page:
            raise Fail(f"wger answered an unexpected list for {path}", 4)
        if page["count"]:
            return True
    return False


def write_routine(
    w: Wger, name: str, start: dt.date, end: dt.date, days: list[dict]
) -> tuple[int, int]:
    """Build the whole routine; on any failure delete the half-built one and re-raise."""
    rid: int | None = None
    n_ex = 0
    try:
        routine = w.call(
            "POST",
            "routine/",
            {
                "name": name,
                "description": "Written by Kit from gym.json",
                "start": str(start),
                "end": str(end),
                "fit_in_week": False,
            },
        )
        rid = routine["id"]  # type: ignore[index]
        for d_order, day in enumerate(days, 1):
            d = w.call(
                "POST",
                "day/",
                {
                    "routine": rid,
                    "name": day["title"],
                    "order": d_order,
                    "is_rest": False,
                    "type": "custom",
                },
            )
            for s_order, ex in enumerate(day["exercises"], 1):
                slot = w.call("POST", "slot/", {"day": d["id"], "order": s_order})  # type: ignore[index]
                entry = w.call(
                    "POST",
                    "slot-entry/",
                    {
                        "slot": slot["id"],  # type: ignore[index]
                        "exercise": ex["_id"],
                        "type": "normal",
                        "repetition_unit": UNIT_REPS,
                        "weight_unit": UNIT_KG,
                        "order": 1,
                    },
                )
                cfg = {
                    "slot_entry": entry["id"],  # type: ignore[index]
                    "iteration": 1,
                    "operation": "r",
                    "step": "na",
                    "repeat": False,
                }
                w.call("POST", "sets-config/", {**cfg, "value": ex["sets"]})
                w.call(
                    "POST", "repetitions-config/", {**cfg, "value": dec(ex["rep_lo"])}
                )
                if ex["rep_hi"] != ex["rep_lo"]:
                    w.call(
                        "POST",
                        "max-repetitions-config/",
                        {**cfg, "value": dec(ex["rep_hi"])},
                    )
                if ex["weight"] is not None:
                    w.call(
                        "POST", "weight-config/", {**cfg, "value": dec(ex["weight"])}
                    )
                n_ex += 1
    except Exception:
        if rid is not None:
            try:
                w.call("DELETE", f"routine/{rid}/")
            except Fail as undo:
                print(
                    f"wger: the half-built routine (id {rid}) could not be deleted: {undo}",
                    file=sys.stderr,
                )
        raise
    return rid, n_ex


def cmd_routine(args: argparse.Namespace) -> int:
    name, days = load_program(args.file)
    name = args.name or name
    if len(name) > ROUTINE_NAME_MAX:
        raise Fail(
            f"routine name {name!r} is longer than wger's {ROUTINE_NAME_MAX} characters: "
            "pass a shorter --name",
            1,
        )
    w = client()
    ids: dict[str, int] = {}
    # Resolve every exercise first: a name wger lacks stops the run before anything is written.
    missing = []
    for day in days:
        for ex in day["exercises"]:
            try:
                ex["_id"] = resolve_exercise(w, ex, ids)
            except Fail as err:
                if err.code != 1:
                    raise
                missing.append(ex["name"])
    if missing:
        raise Fail(
            "no wger exercise for: "
            + ", ".join(missing)
            + ' - add "wger_exercise": <id> '
            "to each in gym.json (nothing was written)",
            1,
        )
    existing = [r for r in w.pages("routine/", {}) if r.get("name") == name]
    if existing and not args.replace:
        raise Fail(
            f"wger already has a routine named {name!r} (id {existing[0]['id']}): "
            "pass --replace to delete it and write this one, or --name for another",
            1,
        )
    for old in existing:
        if has_history(w, old["id"]):
            raise Fail(
                f"routine {name!r} (id {old['id']}) has workout logs or sessions in wger and "
                "deleting it would delete them: use --name for a new routine "
                "(nothing was written)",
                1,
            )
    start = dt.date.fromisoformat(args.start) if args.start else dt.date.today()
    end = start + dt.timedelta(weeks=args.weeks)
    if args.dry_run:
        print(f"dry-run: routine {name!r} {start}..{end}, {len(days)} days")
        for day in days:
            print(
                f"  {day['title']}: "
                + ", ".join(
                    f"{e['name']} {e['sets']}x{e['rep_lo']}-{e['rep_hi']}"
                    for e in day["exercises"]
                )
            )
        return 0
    rid, n_ex = write_routine(w, name, start, end, days)
    for old in existing:
        try:
            w.call("DELETE", f"routine/{old['id']}/")
        except Fail as err:
            raise Fail(
                f"routine {name!r} written (id {rid}) but the old one (id {old['id']}) "
                f"could not be deleted: {err}",
                err.code,
            ) from None
    print(
        f"routine {name!r} written to wger: id {rid}, {len(days)} days, {n_ex} exercises, {start}..{end}"
    )
    return 0


def fmt_num(x: object) -> str:
    n = float(x)  # type: ignore[arg-type]
    return str(int(n)) if n == int(n) else f"{n:g}"


def fmt_tail(weight: object, unit: str) -> str:
    """`@60kg`, `@bw` for bodyweight, nothing when the log has no weight."""
    if unit == "bw":
        return "@bw"
    return "" if weight is None else f"@{fmt_num(weight)}{unit}"


def cmd_logs(args: argparse.Namespace) -> int:
    for label, v in (("--from", args.date_from), ("--to", args.date_to)):
        try:
            dt.date.fromisoformat(v)
        except ValueError:
            raise Fail(f"{label} must be YYYY-MM-DD", 1) from None
    if args.date_from > args.date_to:
        raise Fail("--from is after --to", 1)
    w = client()
    params = {
        "date__gte": args.date_from,
        "date__lte": args.date_to + "T23:59:59",
        "ordering": "date",
    }
    names: dict[int, str] = {}
    want: int | None = None
    if args.exercise:
        want = resolve_exercise(w, {"name": args.exercise}, {})
        params["exercise"] = str(want)
    rows = []
    for r in w.pages("workoutlog/", params):
        day = str(r.get("date", ""))[:10]
        # The server's filters are best effort; the range and the exercise are enforced here too.
        if not (args.date_from <= day <= args.date_to) or (
            want is not None and r.get("exercise") != want
        ):
            continue
        rows.append(
            {
                "date": day,
                "exercise": exercise_name(w, r["exercise"], names),
                "reps": r.get("repetitions"),
                "weight": r.get("weight"),
                "unit": WEIGHT_UNITS.get(
                    r.get("weight_unit") or 1, f"u{r.get('weight_unit')}"
                ),
            }
        )
    rows.sort(key=lambda r: (r["date"], r["exercise"]))
    grouped: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        grouped.setdefault((r["date"], r["exercise"]), []).append(r)
    out = []
    for (day, ex), sets in grouped.items():
        out.append(
            {
                "date": day,
                "exercise": ex,
                "sets": [
                    {"reps": s["reps"], "weight": s["weight"], "unit": s["unit"]}
                    for s in sets
                ],
            }
        )
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    if not out:
        print(f"no workout logs in wger from {args.date_from} to {args.date_to}")
        return 0
    for g in out:
        sets = g["sets"]
        reps = ",".join(
            fmt_num(s["reps"]) if s["reps"] is not None else "?" for s in sets
        )
        weights = {(s["weight"], s["unit"]) for s in sets}
        if len(weights) == 1:
            line = reps + fmt_tail(*next(iter(weights)))
        else:
            line = ", ".join(
                (fmt_num(s["reps"]) if s["reps"] is not None else "?")
                + fmt_tail(s["weight"], s["unit"])
                for s in sets
            )
        print(f"{g['date']} {g['exercise']}: {line}")
    return 0


# --- nutrition ---------------------------------------------------------------

GOAL_KEYS = {  # wger field -> the keys plan.json may use for it
    "goal_energy": ("kcal", "calories", "goal_energy"),
    "goal_protein": ("protein", "goal_protein"),
    "goal_carbohydrates": ("carbs", "carbohydrates", "goal_carbohydrates"),
    "goal_fat": ("fat", "goal_fat"),
    "goal_fiber": ("fiber", "goal_fiber"),
}


def parse_date(value: object, label: str) -> str:
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise Fail(f"{label} must be YYYY-MM-DD", 1) from None


def load_plan(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as err:
        raise Fail(f"cannot read {path}: {err.strerror}", 1) from None
    except json.JSONDecodeError as err:
        raise Fail(f"{path} is not valid JSON: {err}", 1) from None
    if not isinstance(doc, dict):
        raise Fail(f"{path} must be a JSON object", 1)
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise Fail(f'{path} needs a "name" (the plan\'s name in wger)', 1)
    name = name.strip()
    if len(name) > PLAN_NAME_MAX:
        raise Fail(
            f"plan name {name!r} is longer than wger's {PLAN_NAME_MAX} characters", 1
        )
    plan: dict = {"description": name}
    for field, keys in GOAL_KEYS.items():
        key = next((k for k in keys if doc.get(k) is not None), None)
        if key is None:
            if field == "goal_fiber":
                continue
            raise Fail(f'{path} needs "{keys[0]}" (a whole number)', 1)
        v = doc[key]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= INT_MAX:
            raise Fail(f'"{key}" must be a whole number, got {v!r}', 1)
        plan[field] = v
    for key in ("start", "end"):
        if doc.get(key) is not None:
            plan[key] = parse_date(doc[key], f'"{key}"')
    return plan


def plan_summary(plan: dict) -> str:
    return (
        f"{plan['goal_energy']} kcal, protein {plan['goal_protein']} g, "
        f"carbs {plan['goal_carbohydrates']} g, fat {plan['goal_fat']} g"
    )


def cmd_nutrition(args: argparse.Namespace) -> int:
    plan = load_plan(args.file)
    name = plan["description"]
    w = client()
    rows = [
        r
        for r in w.pages("nutritionplan/", {"description": name})
        if r.get("description") == name
    ]
    if len(rows) > 1:
        raise Fail(
            f"wger has {len(rows)} nutrition plans named {name!r}: delete the extras in the "
            "app, then run this again (nothing was written)",
            1,
        )
    if rows:
        changes = {k: v for k, v in plan.items() if rows[0].get(k) != v}
        if not changes:
            print(f"nutrition plan {name!r} is already as asked: {plan_summary(plan)}")
            return 0
        if args.dry_run:
            print(f"dry-run: would update nutrition plan {name!r}: {sorted(changes)}")
            return 0
        w.call("PATCH", f"nutritionplan/{rows[0]['id']}/", changes)
        print(f"nutrition plan {name!r} updated: {plan_summary(plan)}")
        return 0
    body = {
        "start": dt.date.today().isoformat(),
        "only_logging": False,
        **plan,
    }
    if args.dry_run:
        print(f"dry-run: would create nutrition plan {name!r}: {plan_summary(plan)}")
        return 0
    w.call("POST", "nutritionplan/", body)
    print(f"nutrition plan {name!r} created: {plan_summary(plan)}")
    return 0


# --- profile -----------------------------------------------------------------


def cmd_profile(args: argparse.Namespace) -> int:
    if args.birthdate is None and args.height is None and args.weight is None:
        raise Fail("give at least one of --birthdate, --height, --weight", 1)
    want: dict = {}
    if args.birthdate is not None:
        born = parse_date(args.birthdate, "--birthdate")
        if born >= dt.date.today().isoformat():
            raise Fail("--birthdate must be in the past", 1)
        want["birthdate"] = born
    if args.height is not None:
        if not HEIGHT_MIN <= args.height <= HEIGHT_MAX:
            raise Fail(
                f"--height is in whole cm, {HEIGHT_MIN}-{HEIGHT_MAX} (wger's limits)", 1
            )
        want["height"] = args.height
    day = parse_date(args.date, "--date") if args.date else dt.date.today().isoformat()
    if args.weight is not None and not 0 < args.weight <= 999:
        raise Fail("--weight is in kg, above 0 and at most 999", 1)
    w = client()
    profile = w.call("GET", "userprofile/")
    if not isinstance(profile, dict) or "username" not in profile:
        raise Fail("wger answered an unexpected profile", 4)
    verb = "dry-run: would set" if args.dry_run else "set"
    changes = {k: v for k, v in want.items() if profile.get(k) != v}
    for k, v in want.items():
        if k not in changes:
            print(f"profile {k} is already {v}")
    if changes:
        if not args.dry_run:
            w.call("PATCH", "userprofile/", changes)
        print(f"profile: {verb} " + ", ".join(f"{k} {v}" for k, v in changes.items()))
    if args.weight is not None:
        entries = [
            r
            for r in w.pages(
                "weightentry/",
                {"date__gte": day, "date__lte": day + "T23:59:59", "ordering": "date"},
            )
            if str(r.get("date", ""))[:10] == day
        ]
        if entries and dec(entries[0].get("weight")) == dec(args.weight):
            print(f"weight entry for {day} is already {dec(args.weight)} kg")
        elif entries:
            if not args.dry_run:
                w.call(
                    "PATCH",
                    f"weightentry/{entries[0]['id']}/",
                    {"weight": dec(args.weight)},
                )
            print(
                f"weight entry for {day}: {verb} {dec(args.weight)} kg "
                f"(was {dec(entries[0].get('weight'))})"
            )
        else:
            if not args.dry_run:
                w.call(
                    "POST",
                    "weightentry/",
                    {"date": f"{day}T12:00:00+00:00", "weight": dec(args.weight)},
                )
            print(f"weight entry for {day}: {verb} {dec(args.weight)} kg")
    return 0


# --- log-write ---------------------------------------------------------------

SESSION_PART = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9 _'-]*?)\s+"
    r"(?P<sets>\d+\s*x\s*\d+|\d+(?:\s*,\s*\d+)*)"
    r"(?:\s*@\s*(?:(?P<bw>bw)|(?P<weight>\d+(?:\.\d+)?)\s*(?P<unit>kg|lb)?))?$",
    re.IGNORECASE,
)


def parse_session(line: str) -> list[dict]:
    """workout-claw's syntax: `squat 5x5@100kg, bench press 8,8,6@60kg`. A comma that
    starts a new exercise is the one followed by a letter; commas between digits separate sets."""
    out = []
    for part in re.split(r",\s*(?=[A-Za-z])", line.strip()):
        m = SESSION_PART.match(part.strip())
        if not m:
            raise Fail(
                f"cannot read {part.strip()!r}: write it like 'squat 5x5@100kg' or "
                "'bench press 8,8,6@60kg'",
                1,
            )
        if "x" in m["sets"].lower():
            n, reps = (int(v) for v in re.split(r"\s*x\s*", m["sets"].lower()))
            if not 1 <= n <= SETS_MAX:
                raise Fail(f"{m['name']!r}: sets must be 1-{SETS_MAX}", 1)
            reps_list = [reps] * n
        else:
            reps_list = [int(v) for v in re.split(r"\s*,\s*", m["sets"])]
            if len(reps_list) > SETS_MAX:
                raise Fail(f"{m['name']!r}: at most {SETS_MAX} sets", 1)
        if not all(1 <= r <= NUMBER_MAX for r in reps_list):
            raise Fail(f"{m['name']!r}: reps must be 1-{NUMBER_MAX}", 1)
        weight = float(m["weight"]) if m["weight"] else None
        if weight is not None and weight > NUMBER_MAX:
            raise Fail(f"{m['name']!r}: weight is above {NUMBER_MAX}", 1)
        unit = (
            UNIT_BW
            if m["bw"]
            else UNIT_LB
            if (m["unit"] or "").lower() == "lb"
            else UNIT_KG
        )
        out.append(
            {
                "name": m["name"].strip(),
                "reps": reps_list,
                "weight": weight,
                "unit": unit,
            }
        )
    if not out:
        raise Fail("no exercises: pass the same line given to workout-claw log", 1)
    return out


def routine_entries(w: Wger, day: str) -> tuple[dict | None, dict[int, int]]:
    """The routine whose dates cover `day` (the latest start wins) and its exercise -> slot
    entry map; (None, {}) when no routine covers the day."""
    covering = [
        r
        for r in w.pages("routine/", {})
        if str(r.get("start") or "") <= day <= str(r.get("end") or "9999-12-31")
    ]
    if not covering:
        return None, {}
    routine = max(covering, key=lambda r: str(r.get("start")))
    entries: dict[int, int] = {}
    for d in w.pages("day/", {"routine": str(routine["id"])}):
        for slot in w.pages("slot/", {"day": str(d["id"])}):
            for e in w.pages("slot-entry/", {"slot": str(slot["id"])}):
                entries.setdefault(e["exercise"], e["id"])
    return routine, entries


def set_key(
    reps: object, weight: object, unit: object
) -> tuple[float, float | None, object]:
    return float(reps), None if weight is None else float(weight), unit  # type: ignore[arg-type]


def cmd_log_write(args: argparse.Namespace) -> int:
    day = parse_date(args.date, "--date") if args.date else dt.date.today().isoformat()
    session = parse_session(args.line)
    w = client()
    ids: dict[str, int] = {}
    routine, entries = routine_entries(w, day)
    names: dict[int, str] = {}
    where = f"routine {routine['name']!r}" if routine else "no routine covers the date"
    failed: list[Fail] = []
    for ex in session:
        label = slug(ex["name"])
        try:
            eid, in_routine = resolve_for_routine(w, ex, entries, names, ids)
            have = [
                r
                for r in w.pages(
                    "workoutlog/",
                    {
                        "date__gte": day,
                        "date__lte": day + "T23:59:59",
                        "exercise": str(eid),
                        "ordering": "date",
                    },
                )
                if str(r.get("date", ""))[:10] == day and r.get("exercise") == eid
            ]
            sets = [set_key(r, ex["weight"], ex["unit"]) for r in ex["reps"]]
            held = [
                set_key(r.get("repetitions"), r.get("weight"), r.get("weight_unit"))
                for r in have
            ]
            if held == sets:
                print(f"{day} {label}: wger already has these sets, unchanged")
                continue
            if held != sets[: len(held)]:
                raise Fail(
                    f"wger already has {len(have)} logged set(s) for {label} on {day} that "
                    "differ from this line: nothing was written (change them in the wger app)",
                    1,
                )
            missing = len(sets) - len(held)
            if args.dry_run:
                slot = "a routine slot" if in_routine else "not a routine slot"
                print(
                    f"dry-run: would write {label} {missing} set(s) for {day} to "
                    f"exercise {eid} ({slot}; {where})"
                )
                continue
            for i, reps in enumerate(ex["reps"]):
                if i < len(held):
                    continue
                body = {
                    "exercise": eid,
                    "date": f"{day}T12:{i:02d}:00+00:00",
                    "repetitions": dec(reps),
                    "repetitions_unit": UNIT_REPS,
                    "weight_unit": ex["unit"],
                }
                if ex["weight"] is not None:
                    body["weight"] = dec(ex["weight"])
                if routine and in_routine:
                    body["routine"] = routine["id"]
                    body["slot_entry"] = entries[eid]
                w.call("POST", "workoutlog/", body)
            linked = (
                "in the routine" if routine and in_routine else "not in the routine"
            )
            print(
                f"{day} {label}: {missing} set(s) written to wger "
                f"(exercise {eid}, {linked})"
            )
        except Fail as err:
            if err.code != 1:
                raise
            print(f"wger: {label}: {err}", file=sys.stderr)
            failed.append(err)
    return 1 if failed else 0


def cmd_log_delete(args: argparse.Namespace) -> int:
    day = parse_date(args.date, "date")
    w = client()
    label = slug(args.exercise)
    eid = resolve_exercise(w, {"name": args.exercise}, {})
    logs = [
        r
        for r in w.pages(
            "workoutlog/",
            {
                "date__gte": day,
                "date__lte": day + "T23:59:59",
                "exercise": str(eid),
                "ordering": "date",
            },
        )
        if str(r.get("date", ""))[:10] == day and r.get("exercise") == eid
    ]
    gone = {r["id"] for r in logs}
    empty = []
    for sid in dict.fromkeys(r["session"] for r in logs if r.get("session")):
        held = w.pages("workoutlog/", {"session": str(sid)})
        if all(r["id"] in gone for r in held):
            empty.append(sid)
    if not logs and not empty:
        print(f"{day} {label}: nothing to remove")
        return 0
    verb = "would remove" if args.dry_run else "removed"
    if not args.dry_run:
        for r in logs:
            w.call("DELETE", f"workoutlog/{r['id']}/")
        for sid in empty:
            w.call("DELETE", f"workoutsession/{sid}/")
    print(
        f"{'dry-run: ' if args.dry_run else ''}{day} {label}: {verb} "
        f"{len(logs)} log(s) and {len(empty)} empty workout session(s)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wger.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    r = sub.add_parser("routine")
    r.add_argument("file")
    r.add_argument("--name")
    r.add_argument("--start")
    r.add_argument("--weeks", type=int, default=12)
    r.add_argument("--replace", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_routine)
    lg = sub.add_parser("logs")
    lg.add_argument("--from", dest="date_from", required=True)
    lg.add_argument("--to", dest="date_to", required=True)
    lg.add_argument("--exercise")
    lg.add_argument("--json", action="store_true")
    lg.set_defaults(fn=cmd_logs)
    n = sub.add_parser("nutrition")
    n.add_argument("file")
    n.add_argument("--dry-run", action="store_true")
    n.set_defaults(fn=cmd_nutrition)
    pr = sub.add_parser("profile")
    pr.add_argument("--birthdate")
    pr.add_argument("--height", type=int)
    pr.add_argument("--weight", type=float)
    pr.add_argument("--date", help="the weight entry's day (default: today)")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(fn=cmd_profile)
    lw = sub.add_parser("log-write")
    lw.add_argument(
        "line", help='the line given to workout-claw log, e.g. "squat 5x5@100kg"'
    )
    lw.add_argument("--date")
    lw.add_argument("--dry-run", action="store_true")
    lw.set_defaults(fn=cmd_log_write)
    ld = sub.add_parser("log-delete")
    ld.add_argument("date", help="YYYY-MM-DD, the day the logs were written for")
    ld.add_argument(
        "exercise", help='the exercise as typed to log-write, e.g. "biceps curl"'
    )
    ld.add_argument("--dry-run", action="store_true")
    ld.set_defaults(fn=cmd_log_delete)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Fail as err:
        print(f"wger: {err}", file=sys.stderr)
        return err.code
    except (ValueError, KeyError) as err:
        print(f"wger: unexpected data: {err!r}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
