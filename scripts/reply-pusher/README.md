# reply-pusher

Tells Kit, at once, that firstmate replied to one of the owner's `kit-` notes.

- `reply_pusher.py` - `run` (default) enqueues and POSTs; `baseline` sets each home's cursor to its newest reply.
- `lifekit-reply-pusher.path` - fires on each pinned home's reply drop (`<home>/state/inbox/.replies`).
- `lifekit-reply-pusher.service` - the oneshot the path starts; `Restart=on-failure` with back-off is the retry.
- `install-reply-pusher.sh` - renders and installs the units, baselines, enables. Operator step; `bootstrap-vps.sh` calls it.

Reads a home only through `fm-inbox.sh receipts`. Posts identifiers only (`home`, `noteId`, `cursor`) to
the `firstmate-reply` hook mapping, with `Idempotency-Key: <home>:<cursor>`. Reads only the two hook keys
from the rendered env, at run time. Runbook: `docs/runbook.md` "Reply pusher".
