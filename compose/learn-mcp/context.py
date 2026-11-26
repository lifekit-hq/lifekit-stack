"""learn-mcp's read side: what `learning_context` returns.

Everything here reads; nothing writes. The learning page is read through the
learning-record skill's own parser (record.py) and the daily problem through
leetcode-daily's own rules (daily.py), so the connector and learnclaw cannot
disagree about what the record says. The LearnHarness mirror is the owner's
LearnHarness record, synced read-only (docs/runbook.md, "learnclaw (the
learning loop)"); this module opens files there for reading and never creates
one (the upstream skill's rule: work read-only).

Env (set by compose/learn-mcp/docker-compose.yml):
  LEARNING_VAULT, LEARNING_PAGE    as record.py
  LEETCODE_RECORD_PY               record.py (daily.py loads it)
  LEARN_MCP_DAILY_PY               daily.py (default: the sibling skill)
  LEARN_MCP_MIRROR                 the LearnHarness data folder (chats/ inside)
  LEETCODE_TODAY                   tests only, as daily.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# Caps keep the reply small: the coach reads it in a side panel.
MAX_GAPS = 12
MAX_RECENT = 5
MAX_SESSIONS = 40
MAX_TEXT = 240
# A right answer given with less than this self-reported confidence is still a gap.
LOW_CONFIDENCE = 50
QUESTION_TOOLS = {"quiz", "probe", "recall", "explain"}


def load_daily():
    path = Path(
        os.environ.get("LEARN_MCP_DAILY_PY") or HERE / "skills/leetcode-daily/daily.py"
    )
    spec = importlib.util.spec_from_file_location("leetcode_daily", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _short(text: Any) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= MAX_TEXT else s[: MAX_TEXT - 1] + "…"


def in_progress_lines(page_text: str) -> list[str]:
    """The list items under the page's `## In-progress` heading, placeholder excluded."""
    out, inside = [], False
    for line in page_text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## In-progress"
            continue
        s = line.strip()
        if inside and s.startswith(("- ", "* ")):
            out.append(s[2:].strip())
    return out


def record_view(daily, day: dt.date) -> dict[str, Any]:
    """Today's problem, the open problems, recent rows, the week and In-progress."""
    record = daily.load_record()
    _, page = record.page_path()
    text = page.read_text()
    rows = record.rows(text.splitlines())
    state = daily.states(record, rows)
    problems = daily.load_list()
    titles = {p["slug"]: p["title"] for p in problems}

    def problem(slug: str) -> dict[str, str]:
        return {
            "slug": slug,
            "title": titles.get(slug, slug),
            "url": daily.URL.format(slug),
        }

    nxt = daily.pick(problems, state)
    offered = daily.offered_on(state, day)
    if day.weekday() in daily.BLOCKED:
        today = {
            "problem": None,
            "note": f"no problem today ({daily.BLOCKED[day.weekday()]})",
        }
    elif offered:
        today = {"problem": problem(offered), "note": "offered today"}
    else:
        today = {
            "problem": None,
            "note": "no problem offered yet today (learnclaw sends it in the morning)",
        }
    today["next_pick"] = problem(nxt["slug"]) if nxt else None

    open_problems = [
        {**problem(slug), "offered": s["date"].isoformat() if s["date"] else None}
        for slug, s in state.items()
        if s["status"] == "in progress"
    ]
    week = io.StringIO()
    with contextlib.redirect_stdout(week):
        daily.cmd_week(_Args(date=day.isoformat()))
    return {
        "todays_problem": today,
        "open_problems": open_problems,
        "week": week.getvalue().splitlines()[0],
        "recent_rows": [
            {k: r[k] for k in ("date", "topic", "artefact", "status", "grilled")}
            for r in rows[-MAX_RECENT:]
        ][::-1],
        "in_progress": in_progress_lines(text),
    }


class _Args:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _answer_gap(details: dict[str, Any]) -> str | None:
    if details.get("kind") not in ("quiz", "explain"):
        return None  # `ask` clarifies preferences; it is not graded
    if details.get("dismissed"):
        return "skipped"
    if details.get("dontKnow"):
        return "don't know"
    if details.get("correct") is False:
        return "wrong"
    conf = details.get("confidence")
    if (
        details.get("correct") is True
        and isinstance(conf, (int, float))
        and conf < LOW_CONFIDENCE
    ):
        return f"right but unsure ({int(conf)}%)"
    return None


def _session_gaps(session: dict[str, Any]) -> list[dict[str, str]]:
    title = _short(session.get("title")) or "untitled session"
    fallback = str(session.get("updatedAt") or "")
    gaps: list[dict[str, str]] = []
    problems: dict[str, str] = {}  # problem call id -> statement, nearest preceding
    for m in session.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if (
                    isinstance(b, dict)
                    and b.get("type") == "toolCall"
                    and b.get("name") == "problem"
                    and isinstance(b.get("arguments"), dict)
                ):
                    problems[str(b.get("id"))] = _short(b["arguments"].get("statement"))
        elif role == "toolResult" and m.get("toolName") in QUESTION_TOOLS:
            d = m.get("details")
            why = _answer_gap(d) if isinstance(d, dict) else None
            if why:
                gaps.append(
                    {
                        "session": title,
                        "when": _iso(d.get("answeredAt") or fallback),
                        "question": _short(d.get("questionText")),
                        "why": why,
                    }
                )
        elif role == "custom" and m.get("customType") == "problem-dismissed":
            d = m.get("details") if isinstance(m.get("details"), dict) else {}
            ref = next(
                (str(v) for k, v in d.items() if k.lower().endswith("callid")), ""
            )
            gaps.append(
                {
                    "session": title,
                    "when": _ms(m.get("timestamp")) or _iso(fallback),
                    "question": problems.get(ref, ""),
                    "why": "practice problem skipped",
                }
            )
    return gaps


def _ms(stamp: Any) -> str:
    if not isinstance(stamp, (int, float)):
        return ""
    return _iso(dt.datetime.fromtimestamp(stamp / 1000, dt.timezone.utc))


def _iso(when: Any) -> str:
    """One UTC form for every timestamp, so the newest-first sort is by time."""
    if isinstance(when, str):
        try:
            when = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            return when
    if not isinstance(when, dt.datetime):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mirror_view(mirror: Path) -> dict[str, Any]:
    """Gaps from the newest sessions, newest first, and the newest session's date."""
    chats = mirror / "chats"
    try:
        files = sorted(chats.glob("*.json")) if chats.is_dir() else None
    except OSError:
        files = None  # unreadable (ownership); the record half still answers
    if files is None:
        return {
            "mirror": "unavailable (no readable chats folder in the LearnHarness mirror)",
            "newest_session": None,
            "gaps": [],
        }
    sessions = []
    for f in files:
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue  # a file mid-sync; the next call reads it
        if isinstance(s, dict):
            sessions.append(s)
    sessions.sort(key=lambda s: str(s.get("updatedAt") or ""), reverse=True)
    gaps: list[dict[str, str]] = []
    for s in sessions[:MAX_SESSIONS]:
        gaps.extend(_session_gaps(s))
    gaps.sort(key=lambda g: g["when"], reverse=True)
    newest = sessions[0] if sessions else None
    return {
        "mirror": f"{len(sessions)} session(s)",
        "newest_session": (
            {
                "title": _short(newest.get("title")),
                "updated": newest.get("updatedAt"),
            }
            if newest
            else None
        ),
        "gaps": gaps[:MAX_GAPS],
    }


def learning_context() -> dict[str, Any]:
    daily = load_daily()
    day = daily.today()
    out: dict[str, Any] = {
        "today": day.isoformat(),
        "weekday": day.strftime("%A"),
    }
    out.update(record_view(daily, day))
    out.update(mirror_view(Path(os.environ.get("LEARN_MCP_MIRROR") or "/mirror")))
    return out
