---
name: ab-plan
description: Plan a tracked deliverable into a useful checklist or sequential parent/subtasks, reusing existing code and libraries.
---

# ab-plan

1. Read the task, history, dependencies and repository guidance. Search existing tasks for overlap. Use ab-interview only for material unanswered decisions.
2. Explicitly research currently maintained libraries and their latest stable versions using official docs/releases. Record the versions/date and reuse decision in the task. Inspect repository code first; reuse suitable implementations and remove duplicates instead of introducing another copy. Respect dependency constraints; a stdlib solution can be the right choice.
3. When useful, dispatch bounded read-only scouts via `workflow prepare TASK --role scout --tier 0 --primary PROVIDER`. Compare the available native role, provider, model and effort with the exact allowed candidate, require its native_profile_ready, and ensure the tool enforces the settings; a role name alone proves nothing. If unsupported or unknown, use managed `workflow run TASK --role scout --tier 0 --primary PROVIDER --brief FILE`. Native reports use `workflow report TASK --role scout --tier 0 --provider ACTUAL_PROVIDER --model ACTUAL_MODEL --effort ACTUAL_EFFORT --scope SCOPE --handoff DESCRIPTOR --file FILE`; keep the descriptor with the coordinator and add `--fallback-reason REASON` for fallback, never guess unknown settings. Choose only relevant code, docs, history, library, dependency or gap questions. Parallel scouts must stay within configured scout_limit; report concise facts with source locations and uncertainty. Tier 1/2 is available when deeper analysis is warranted; use the same tier for prepare and report.
4. Keep small work as one task with a checklist. For larger work create subtasks in the same project, each understandable and testable in one context. Parent is the deliverable/spec; do not create a project per spec or setup/test pseudo-tasks.
5. Give each implementation task its own tier 1-3 based on complexity, respecting user changes. Tier 0 is only for scouting. Add real dependency relations; explain overlaps or future-task effects to the user and update all affected tasks after resolution.
6. Record acceptance and proportionate validation. Prefer a few behavioral regression tests over tests mirroring functions. Mark fully specified unblocked tasks Ready. One parent branch and one parent PR; subtasks execute and integrate strictly sequentially.
