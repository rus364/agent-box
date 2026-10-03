---
name: ab-interview
description: Resolve material ambiguity in a tracked task and save implementation-ready acceptance in the tracker.
---

# ab-interview

Read the task, relevant comments/relations and repository code first. Search older decisions or libraries only when they can answer a current uncertainty.

Identify unresolved decisions in dependency order. Ask one material question at a time in the user's language, with 2-4 concrete options and a recommendation when useful. Test uncertain assumptions against facts and concrete scenarios; do not manufacture disagreement or interrogate obvious work.

Assess priority using ab-tracker’s evidence rules when preparing or creating tasks.

Stop when remaining choices can safely be decided during implementation. Update the task's Goal, Context, Acceptance and Validation in its language; preserve unrelated text and summarize important decisions in a comment. Explain changed scope and relations. Keep the plan in the tracker, without a parallel repository task database. Maintain verified reference documents as `path#heading` with purpose. Preserve the reference section’s heading, language and location; if absent, add one in the task’s language under Context. Missing decisions need clarification, not invented links. After recording an accepted decision, use ab-sync with `workflow sync ID --event plan` to propagate it under configured policy. Already approved work does not need another approval ceremony.
