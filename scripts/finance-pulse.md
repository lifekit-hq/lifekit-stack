Finance pulse. Silence is the default: reply NO_REPLY unless one of these changed since the last pulse. Read only through the finance-sentry MCP tools.
1. get_pending_companion_events(includeHeldForDigest=true): anything held for the digest or older than 6h -> one grouped message, then acknowledge_companion_events for what you reported.
2. list_thesis_breaks: any break not yet delivered -> message (bypasses everything else).
3. get_allocation_vs_target: needsRebalance true and no rebalance proposal delivered this week -> message.
4. get_sync_health: any provider stale for more than 24h -> one line.
5. get_macro_calendar (next 24h, high impact): a context line only when a message is being sent anyway.
6. If no message was delivered in the last 7 days (check the acknowledged events), send the quiet-week positioning digest once (get_portfolio_snapshot + get_book_performance), 12 lines at most.
