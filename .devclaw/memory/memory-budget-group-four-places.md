# A memory-budget group lives in four places

The list itself is in the repo, not here: `docs/resource-budget.md` "Changing a
budget" names all four edits (the table row plus the `Sum` / `Total` rows and the
opening paragraph's two totals; `GROUP_SELECTORS` in
`scripts/ops-report/weekly.py`; the `lifekit:group_memory_bytes` recording rule
*twice* - its own branch and the burst pool's `unless (...)` list; and the
runbook's production-test project list). Read it there.

What that section does not say:

- Which tests hold it together: `test_every_container_group_has_a_selector`
  (`scripts/ops-report/tests/test_weekly.py`) ties the doc's rows to the
  report's selectors; in `scripts/tests/test_alert_rules.py`,
  `test_memory_group_selectors_match_weekly_report` ties those to the rule's
  named branches and `test_burst_pool_subtracts_every_named_group` (added
  2026-11-06) reads the `unless` list, which the first one's regex cannot see.
  The runbook list has no test, by its nature.
- A fifth place exists, deliberately outside that list: the
  `container-stopped-or-vanished` Grafana rule
  (`compose/observability/grafana/provisioning/alerting/rules.yml`) carries its
  own project allowlist, `project=~"compose|openclaw|docker|devclaw|dashboard|xui"`,
  which already lags the runbook's production list by backstage, identity, edge,
  reminder-bot and basket. Widening it for one newcomer only deepens that drift:
  it is its own change, with its own decision about how much retirement noise
  the rule may make.
