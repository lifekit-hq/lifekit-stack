---
name: learning-record
description: Read and append the artefact record on the vault's learning page, the one record of the learning loop. Use to answer "where was I / where am I" in learning (projections computed now from the record), and to record an artefact the owner reports - started, applied, posted, grilled, done or dropped - as one new row. learnclaw only.
metadata: { "openclaw": { "requires": { "bins": ["python3"] } } }
---

# Learning record

The record is the table under "Artefact record" on `~/memory/domains/learning.md`. One row is one from-scratch artefact; the table is append-only. `record.py` (next to this file) is the only way you write it, and it is your only write anywhere in `~/memory/`.

Run it as `python3 <this skill's dir>/record.py ...`.

## Where was I / where am I
1. `record.py status`. It prints the last artefact row and how long ago it was, each checkpoint still open (due, or missed by how many days), what each in-progress topic still lacks (applied, post, grilled), and the done and dropped topics.
2. Answer from that output and the page's "Current focus" and "Fundamentals track" sections. Never hold a projection in your notes or on the page: it is computed when asked.

## Record an artefact
Only on the owner's words in this conversation: what they built, where it lives, what they read, where they applied it, the post, the grilling. You never write the artefact and you never invent a cell.
1. Build the row. `--topic` and `--artefact` (repo/path, or what exists so far) are required; `--reading`, `--applied`, `--post`, `--grilled` default to `-`; `--status` is `in progress` (default), `done` or `dropped`; `--date` defaults to today, `due YYYY-MM-DD` records a planned artefact with its checkpoint.
2. `done` needs applied, post and grilled all filled; the CLI refuses it otherwise. A step forward on an existing topic is a new row with the same topic carrying everything known so far; a row is never edited.
3. `record.py append --topic "..." --artefact "..." [...]`. Exit 0 prints `recorded: <row>`: say "Recorded" and the row, nothing more. The vault's sync timer commits it; do not commit, push or touch git.
4. Exit 1 means the vault lint refused the row and the page is back as it was: show the finding line and fix the cell it names (a `[[link]]` that resolves to nothing is the usual one). Exit 2 is a refused argument: fix it, or ask the owner for the one missing fact.

## Everything else on the page
"In-progress", "Solid", "Backlog", the focus list and the track decisions are the owner's. A change there is a proposed edit in your reply, never a write.
