---
name: job-queue
description: See or clear the jobs waiting for the owner's egress (today the PC), such as queued video evaluations. Use when the owner asks what is waiting, queued or pending for the PC, or asks to cancel a queued job.
metadata: { "openclaw": { "requires": { "bins": ["python3"] } } }
---

# Job queue

Some fetches cannot leave this server, so a skill queues them and they run when the egress answers (today: when the owner's PC is on). One shared queue holds them all; each job belongs to the skill that queued it, and that skill's rules shape its reply. A cron row drains the queue one job at a time on its own. You never run a job by hand.

## Run

```
python3 {baseDir}/jobq.py list
```

One line per job: id, status (`queued`, `running`, `done`, `failed`), when it was queued, attempts, and for a finished job the first line of its result. Finished jobs stay listed for two weeks. Answer the owner plainly: how many wait, and that they run when the PC is on.

```
python3 {baseDir}/jobq.py drop <id>
```

Removes one job, only when the owner asks to cancel it. Never drop a job to "fix" the queue.

`jobq.py next` is the drain cron's check, not for you.
