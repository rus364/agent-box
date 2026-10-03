---
name: ab-sync
description: Synchronize live tracker tasks and reference documents using the configured Plan sync agent.
---

# ab-sync

1. Read `workflow inspect ID` `plan_sync` for settings, execution and the last result. For automatic outcomes, SKIPPED/UPDATED/UNCHANGED need no run; REQUIRED launches its recorded event, and pending/failed runs need reconciliation first. Manual runs use `workflow sync ID`; automatic requests preserve `--event before|after|plan`. Review policy does not control sync. Never assemble task texts/diffs or apply the agent's edits yourself.
2. Recheck execution for each run. Use its first candidate's native profile when available in this session with the configured provider/API, model, effort and speed; otherwise use managed `workflow sync ID`. Preserve the event in either route. Empty models require configuration, never coordinator substitution.
3. Native: `workflow sync ID --native [--event EVENT]` prints `message`. If SKIPPED, stop. Spawn the named profile fresh without parent history, passing only `message`; follow ab-work's native wait policy. Save its final answer unchanged outside the tree and submit `workflow sync ID --result FILE`. If spawn fails before work starts, explain why and run managed with the same event and settings. Never switch models after possible tracker writes.

During adoption, wait until cutover: workflow sync uses the current repository binding, not staging.

The agent starts from source decisions and searches affected candidates within scope; scope does not require reading every task. It corrects concrete mismatches and needed references, never adds informational recaps or Git/workflow bookkeeping to descriptions. Repository, Git and lifecycle are read-only. `Sync: UPDATED|UNCHANGED|BLOCKED` is task synchronization, never code acceptance. Error, interrupted run or BLOCKED may follow partial writes: inspect live tasks before retrying; resolve conflicts through ab-interview before starting affected work. A completed after event needs no repeat merely because task text changed.
