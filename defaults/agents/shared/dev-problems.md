<!-- lifekit:dev-problems:begin -->
## Development-class problems

A development-class problem is a defect in a fleet app or tool, a data source or tool that keeps failing, or a gap in a repo you do not own. You file it and carry on. Denys is never the messenger.

1. File it once with the devclaw intake tool (`file_intake`, over the `devclaw` MCP server). Name the repo when the owner is obvious; when it is not, say so and leave the repo out, and firstmate routes it.
2. Give the title in one line, what failed with the evidence (error text, id, time), and a fingerprint (repo plus failure kind). A repeat adds a count to the open issue, so file it again rather than hold back. If the fix waits on someone outside the fleet, say who.
3. Carry on at once in a degraded way. Never wait for the fix. State the failure once, in one line of your own output for the task in hand.
4. If filing fails, say that in the same line and carry on. Never message Denys about a failure and never ask him to relay it: a chat message about a failure is a defect.
5. This holds on every turn, including scheduled and agent-to-agent ones.
<!-- lifekit:dev-problems:end -->
