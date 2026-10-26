"""An isolated agentTurn cron payload must never depend on what an earlier run sent.

Every run is a fresh session that receives only its payload.message, so a rule
like "no message in the last 7 days" or "not yet delivered" can only be answered
by guessing, and the guess is nearly always "nothing was sent": the run sends
again at every slot (the finance pulse's rebalance notice, repeated ~3 times a
day for two weeks). docs/runbook.md, "The payload is stateless", has the rule and
what to write instead: decide from acknowledged events, live tool data, the
run's own clock, or a state file the job itself writes.

This test reads every cron payload the repo declares and fails on phrasing that
asks a run to remember its deliveries. It sees phrasing only; the review
question is "could a run with no memory answer this?".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CHECKLIST = REPO / "scripts/finance-pulse.md"
RUNBOOK = REPO / "docs/runbook.md"

WINDOW = (
    r"\b(?:in|within|over|during|for|since)\s+the\s+"
    r"(?:last|past|previous)\s+(?:\d+|a|an|one|two|three|few|several|seven)\s*"
    r"(?:minutes?|hours?|days?|weeks?|months?)\b"
)
DELIVERY = r"deliver|\bsent\b|\bsends?\b|\bsending\b|\btold\b|notif|messaged|digest"
SENT_VERB = r"(?:sent|told|delivered|messaged|notified|reported|posted|surfaced)"
UNIT = r"(?:message|digest|pulse|run|send|notice|notification|brief|alert|time)"

PATTERNS: dict[str, str] = {
    "delivery tested over a trailing window": "",  # handled by window_hits()
    "already/earlier/yet + sent": rf"\b(?:already|previously|earlier|yet)\b[^.;\n]{{0,15}}\b{SENT_VERB}\b",
    "since the last message/digest/pulse/run": rf"\bsince\s+(?:the|your|its|their)\s+(?:last|previous|prior|latest)\s+{UNIT}\b",
    "delivered this week/day/month": rf"\b{SENT_VERB}\s+(?:this|last)\s+(?:week|month|day)\b",
    "have not yet told/sent": rf"\b(?:have|has|had)\s+(?:not|never)\s+(?:yet\s+)?{SENT_VERB}\b|\bhaven't\s+{SENT_VERB}\b",
}


def declared_payloads() -> dict[str, str]:
    """Label -> the text of each declared cron payload (comments stripped)."""
    out = {str(CHECKLIST.relative_to(REPO)): CHECKLIST.read_text()}
    for script in sorted((REPO / "scripts").glob("ensure-*.sh")):
        text = script.read_text()
        if not re.search(r"openclaw\s+cron\s+(?:add|edit)\b", text):
            continue
        body = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        out[str(script.relative_to(REPO))] = body
    out["docs/runbook.md (--message lines)"] = "\n".join(
        line for line in RUNBOOK.read_text().splitlines() if "--message" in line
    )
    return out


def window_hits(text: str) -> list[str]:
    hits = []
    for m in re.finditer(WINDOW, text, re.IGNORECASE):
        around = text[max(0, m.start() - 70) : m.end() + 70]
        if re.search(DELIVERY, around, re.IGNORECASE):
            hits.append(around.replace("\n", " ").strip())
    return hits


def violations(text: str) -> list[tuple[str, str]]:
    flat = re.sub(r"\s+", " ", text)
    found = [("delivery tested over a trailing window", h) for h in window_hits(flat)]
    for name, pattern in PATTERNS.items():
        if pattern:
            found += [
                (name, m.group(0)) for m in re.finditer(pattern, flat, re.IGNORECASE)
            ]
    return found


def test_the_declared_payloads_are_all_found():
    found = set(declared_payloads())
    for expected in (
        "scripts/finance-pulse.md",
        "scripts/ensure-needs-you-digest.sh",
        "scripts/ensure-memory-vault-cleanup.sh",
        "scripts/ensure-today-brief-fleet.sh",
    ):
        assert (
            expected in found
        ), f"{expected} no longer scanned; fix declared_payloads()"


@pytest.mark.parametrize("label", sorted(declared_payloads()))
def test_payload_does_not_depend_on_earlier_deliveries(label):
    bad = violations(declared_payloads()[label])
    assert not bad, (
        f"{label}: a cron run starts fresh and cannot know what an earlier run sent. "
        "Decide from acknowledged events, live data or the run's own clock instead "
        "(docs/runbook.md, 'The payload is stateless'):\n"
        + "\n".join(f"  [{name}] ...{hit}..." for name, hit in bad)
    )


@pytest.mark.parametrize(
    "phrase",
    [
        "If no message was delivered in the last 7 days, send the digest.",
        "needsRebalance true and no rebalance proposal delivered this week -> message.",
        "any break not yet delivered -> message",
        "skip it if you already sent this notice",
        "unless one of these changed since the last pulse",
        "Read the mail that arrived since the last digest",
        "if you have not told the owner about it",
        "when nothing was sent within the past 3 days, post the summary",
    ],
)
def test_the_check_catches_memory_of_delivery_phrasing(phrase):
    assert violations(phrase), phrase


@pytest.mark.parametrize(
    "phrase",
    [
        "Call search_gmail_messages with query 'in:inbox newer_than:16h'",
        "Compare the two lists as sets; if they are the same, do not call notify-relay.",
        "Read the previous list from audits/needs-denys-state.json",
        "Only on the run that starts on a Sunday at or before 09:22 Europe/Dublin",
        "stale for more than 24h -> one line",
    ],
)
def test_the_check_leaves_stateless_rules_alone(phrase):
    assert not violations(phrase), phrase
