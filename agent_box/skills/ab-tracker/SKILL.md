---
name: ab-tracker
description: Read and maintain external tracker tasks, progress, dependencies, and history through agent-box.
---

# ab-tracker

Use `agent-box tracker --help` and command help for exact arguments. JSON is the portable interface; never call a provider API directly for common operations.

- Resolve the bound project with `status`; search before selecting an ID. `get ID` includes requirements and comments; `search`, `ready`, and `next` return compact summaries. Expand only relevant tasks, relations, and history.
- `claim ID` checks readiness and ownership, assigns the API-key user and enters Doing. `--resume` is deliberate same-user recovery. Assignment is a cooperative lock, not distributed exclusion. Recheck ownership before writing.
- `create`, `update`, `tier`, `relation`, `comment`, and `check` maintain tasks. Read `checklist ID` before `check ID INDEX --text EXPECTED`; use `--undo` for contradicted evidence. Files/stdin carry multiline text.
- Use the task's language for descriptions and comments. Preserve unrelated text. First-class fields hold status, priority, assignee, relations and tier; do not duplicate them in Markdown.
- Descriptions contain Goal, Context, Acceptance and Validation (translated to task language). Checkboxes record evidenced progress, not reviewer acceptance. Decisions and failures belong in short comments.
- Orchestrator owns requirements, lifecycle, dependencies, branches and GitHub. A worker may only comment and check existing items on its assigned Doing task. Reviewers and scouts never write tracker data. This is a command/instruction boundary, not credential isolation.
- Required states: Backlog, Ready, Doing, Review, Done. Dependencies block deterministically; a completed sibling unblocks after parent-branch integration, while an external parent's work must first merge into base.
- Use `workflow finish` for completion. Provider-specific imports, boards, bulk edits and product fields are under `tracker provider vikunja --help`; inspect dry runs before applying. Unsupported providers fail explicitly.
