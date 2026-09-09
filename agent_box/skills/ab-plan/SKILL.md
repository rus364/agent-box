---
name: ab-plan
description: Plan a tracked deliverable into a useful checklist or sequential parent/subtasks, reusing existing code and libraries.
---

# ab-plan

1. Read the current task, applicable repository guidance, dependencies and relevant decisions. Search other tasks when overlap or missing context is plausible. Use ab-interview only for material unanswered choices.
2. Inspect existing code and reuse suitable implementations. Research current maintained libraries and stable versions from official sources only when the work presents a material dependency choice; record the choice, version/date and constraints. Do not perform dependency research for an exact task with no dependency decision.
3. When useful, dispatch a bounded read-only scout with `workflow prepare TASK --role scout --tier 0 --primary PROVIDER --brief FILE`. Write one compact UTF-8 English brief with the current goal, acceptance, validation, question and technical context. Follow the returned route/instructions, pass `delegate_handoff` unchanged to a fresh agent, and ingest native output with the returned `report_command`; managed scouting uses `workflow run` with the same brief. Reports are English facts with source locations and uncertainty, never reviewer findings. Do not import conversation, comments or earlier-agent history. Keep within `scout_limit`; use tier 1/2 only when warranted.
4. Keep small work as one task with a checklist. For larger work create subtasks in the same project, each understandable and testable in one context. Parent is the deliverable/spec; do not create a project per spec or setup/test pseudo-tasks.
5. A task tier is optional. Assign tier 1-3 only when an explicit configured route is wanted; never fabricate one to pass readiness. Overrides do not change the stored tier, and tier 0 is only for scouts. Add real dependency relations. Ask the user about material unresolved alternatives or conflicting scope, then update each affected task once the decision is made; do not reopen an accepted decision without new evidence.
6. Record acceptance and proportionate validation. Prefer a few behavioral regression tests over tests mirroring functions. Mark fully specified unblocked tasks Ready. One parent branch and one parent PR; subtasks execute and integrate strictly sequentially.
