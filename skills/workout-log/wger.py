#!/usr/bin/env python3
"""wger.py - Kit's two uses of the wger workout manager (docs/runbook.md, "Workout manager (wger)").

  wger.py check                                  # GET /api/v2/version/, then an authenticated profile read
  wger.py routine <gym.json> [--name N] [--start YYYY-MM-DD] [--weeks N] [--replace] [--dry-run]
  wger.py logs --from YYYY-MM-DD --to YYYY-MM-DD [--exercise NAME] [--json]

`routine` writes Kit's routine definition (routines/gym.json, the file Kit keeps in its
workspace) into wger as a routine: one wger day per program day, one slot per exercise, one
slot entry per slot with its sets, rep range and weight. `logs` reads the workout logs back:
date, exercise, sets, reps and weight, grouped the way the skill replies.

Unknown fields in gym.json (steps, notes, deload rules, anything else) are ignored.

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


def resolve_exercise(w: Wger, ex: dict, cache: dict[str, int]) -> int:
    """The wger exercise (base) id for one program exercise: explicit id, else an exact
    English name match on the exercise's name or aliases. Never a guess."""
    if isinstance(ex.get("wger_exercise"), int):
        return ex["wger_exercise"]
    names = [ex["name"], *[a for a in ex.get("aliases", []) if isinstance(a, str)]]
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
        f"no wger exercise named {ex['name']!r}: add \"wger_exercise\": <id> to it in gym.json "
        "(the id is on the exercise's wger page) or an alias that matches wger's name",
        1,
    )


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
