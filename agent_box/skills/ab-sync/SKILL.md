---
name: ab-sync
description: Coordinate Plan sync launches and recovery; for the coordinator, not the assigned sync executor.
---

# ab-sync

Coordinator only. The assigned sync executor follows its role instructions and uses `agent-box tracker` directly; it never runs this skill or workflow commands.

1. Read `workflow inspect ID` `plan_sync` for settings, execution and the last result. SKIPPED/UPDATED/UNCHANGED need no run; REQUIRED launches its recorded event. Pending/failed runs require step 4. Manual runs use `workflow sync ID`; automatic requests preserve `--event before|after|plan`. Review policy does not control sync. Never assemble task texts/diffs or apply the agent's edits yourself.
2. Recheck execution for each run. Use its first candidate's native profile when available in this session with the configured provider/API, model, effort and speed; otherwise use managed `workflow sync ID`. Preserve the event in either route. Empty models require configuration, never coordinator substitution.
3. Native: `workflow sync ID --native [--event EVENT]` prints `message`. If SKIPPED, stop. Spawn the named profile fresh without parent history, passing only `message`; follow ab-work's native wait policy. Save its final answer unchanged outside the tree and submit `workflow sync ID --result FILE`. If spawn fails before work starts, explain why, reconcile as REQUIRED, then run managed with the same event/settings. Never switch models after possible tracker writes.
4. Recovery: await/stop the old executor, inspect its report and reread possibly changed tasks. Use `workflow sync ID --reconcile FILE` with a coordinator report outside the tree: checked writes, resolved/remaining blockers, then one final `Sync: UPDATED|UNCHANGED|BLOCKED|REQUIRED` line. UPDATED/UNCHANGED means the original scope is complete (include earlier writes); BLOCKED retains unresolved issues; REQUIRED permits another run after checking partial writes. This records recovery without a model call or tracker edits, preserving the original report and HEAD. Never mark an incomplete audit successful or replay uncertain writes.

During adoption, wait until cutover: workflow sync uses the current repository binding, not staging.

The agent searches affected tasks from source decisions; scope is not a full-task audit. It corrects concrete mismatches/references, never informational recaps or Git/workflow bookkeeping. Done tasks are read-only history; superseded text never blocks. Unresolved open-task corrections or inaccessible required sources are BLOCKED; resolve decision conflicts through ab-interview before affected work. Repository, Git and lifecycle are read-only; sync is never code acceptance. A completed after event needs no repeat merely because task text changed.
