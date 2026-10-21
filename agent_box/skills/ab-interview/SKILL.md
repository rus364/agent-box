---
name: ab-interview
description: Resolve material ambiguity in a tracked task and save implementation-ready acceptance in the tracker.
---

# ab-interview

Before coordinating work, read `agent-box workflow behavior --role coordinator`; follow its effective instructions and startup guidance notice. Assigned delegates use the behavior in their assignment.

Read the task, relevant comments/relations and repository code first. Search older decisions with `tracker search QUERY --include-done`, or libraries, only when they can answer a current uncertainty; follow ab-tracker for focused reads.

Identify unresolved decisions in dependency order. Ask one material question at a time, with 2-4 concrete options and a recommendation when useful. Use the user's language for question text, option explanations and recommendations; literal project labels may stay quoted in the project language. Test uncertain assumptions against facts and concrete scenarios; do not manufacture disagreement or interrogate obvious work.

Assess priority using ab-tracker’s evidence rules when preparing or creating tasks.

Stop when remaining choices can safely be decided during implementation. Update the task's Goal, Context, Acceptance and Validation in the working language (ab-tracker); preserve unrelated text and summarize important decisions in a comment. Explain changed scope and relations. Keep the plan in the tracker, without a parallel repository task database. Maintain verified reference documents as `path#heading` with purpose. Keep the reference section’s heading and location, in the working language; if absent, add one under Context. Missing decisions need clarification, not invented links. After recording an accepted decision, use ab-sync with `workflow sync ID --event plan` to propagate it under configured policy. Already approved work does not need another approval ceremony.
