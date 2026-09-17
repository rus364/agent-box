---
name: ab-plan
description: Plan a tracked deliverable into a useful checklist or sequential parent/subtasks, reusing existing code and libraries.
---

# ab-plan

1. Read the current task, applicable repository guidance, dependencies and relevant decisions. Search other tasks when overlap or missing context is plausible. Use ab-interview only for material unanswered choices.
2. Inspect existing code and reuse suitable implementations. Research current maintained libraries and stable versions from official sources only when the work presents a material dependency choice; record the choice, version/date and constraints. Do not perform dependency research for an exact task with no dependency decision.
3. For a bounded research question that would otherwise need substantial reading, spawn a native read-only scout or run `workflow delegate ID --role scout --brief FILE`; the brief states the goal, the one question, scope and limitations. Keep quick lookups yourself and never launch duplicate scouts.
4. Keep small work as one task with a checklist. For larger work create subtasks in the same project, each understandable and testable in one context. Parent is the deliverable/spec; do not create a project per spec or setup/test pseudo-tasks. Write every description as four second-level sections in the order Goal → Context → Acceptance → Validation (headers in the task's language; Acceptance and Validation as `- [ ]` items).
5. A task tier is optional. Assign tier 1-3 only when an explicit configured route is wanted; never fabricate one to pass readiness. Session pins and role model lists do not change the stored tier, and tier 0 is only for scouts. Add real dependency relations. Ask the user about material unresolved alternatives or conflicting scope, then update each affected task once the decision is made; do not reopen an accepted decision without new evidence.
6. Assess priority using ab-tracker’s evidence rules when preparing or creating tasks. Record acceptance and proportionate validation. Prefer a few behavioral regression tests over tests mirroring functions. Mark fully specified unblocked tasks Ready. One parent branch and one parent PR; subtasks execute and integrate strictly sequentially.
