---
name: leetcode-daily
description: The owner's daily LeetCode problem. Run by the leetcode-daily cron to pick and send one problem (name and link only), and by learnclaw in chat to record a problem done or skipped and to read the weekly solved count. The list, the home-day rule and the no-repeat rule are in daily.py. learnclaw only.
metadata: { "openclaw": { "requires": { "bins": ["python3"] } } }
---

# Daily LeetCode problem

One LeetCode problem a day on home days (not Tuesday, Thursday or Sunday), in learnclaw's own Telegram chat, picked by pattern from `problems.json` next to this file (Track A order, one pattern at a time). Run `python3 <this skill's dir>/daily.py ...`. The state is the artefact record (`learning-record`), one `leetcode: <slug>` row per step; there is no other store.

## The scheduled send
The `leetcode-daily` cron runs `daily.py next --record` and sends what it prints: `SEND`, then the problem name, then its link. Nothing else goes in that message: no pattern name, no hint, no solution. Any other output is `SKIP <reason>` and the run sends nothing.

## When the owner reports on a problem (chat)
Only on the owner's words in this conversation. Never write the solution, not a line, not a starter; a hint is a smaller question.
1. Help the owner understand it after they have attempted it: ask what they tried, follow the weak spot with one question at a time.
2. Have the owner name the pattern and state the time and space complexity themselves. Correct a wrong one with a question, not the answer.
3. Solved by hand: `daily.py done <slug> --pattern "<their words>" --time "<their words>" --space "<their words>"`. Exit 0 prints `recorded: <row>`: say "Recorded", nothing more. A problem solved with help from a solution is not solved by hand: do not record `done`.
4. Skipped: `daily.py skip <slug> [--reason "<their words>"]`. A skipped problem does not come back.
5. The slug is the one in the problem's link (`leetcode.com/problems/<slug>/`). Exit 2 is a refused argument (unknown slug, empty cell): fix it or ask for the one missing fact. Exit 1 is the vault lint refusing the row; the page is back as it was.

## Counts and position
- `daily.py week [--date YYYY-MM-DD]`: problems solved by hand, Monday to Sunday. The first line is `solved: <n> (...)`. Kit's Sunday check-in reads this line; never estimate it.
- `daily.py status`: done, skipped and open counts and the next problem.
- `daily.py check`: validate the list (run after editing `problems.json`).
