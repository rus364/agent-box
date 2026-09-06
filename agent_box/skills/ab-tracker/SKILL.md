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
- Comments are a human journal only. Branch, base branch and pull request are read from Git and GitHub; no comment can set them, and nothing machine-readable belongs in one.
- The coordinator is the only writer: requirements, lifecycle, dependencies, progress comments, checkboxes, branches and GitHub. Every delegated role - worker, reviewer, scout - reads the tracker and reports back instead. This is a contract, not credential isolation, and it is the same whether the delegate is a native subagent or an agent-box-managed CLI run.
- Required states: Backlog, Ready, Doing, Review, Done. Dependencies block deterministically; a completed sibling unblocks after parent-branch integration, while an external parent's work must first merge into base.
- `audit` reports what mechanically breaks the workflow across the bound subtree: boards, states, tiers, checklists, relations and duplicate titles. It writes nothing, and re-running it is how ab-adopt resumes.
- Use `workflow finish` for completion. Provider-specific imports, boards, bulk edits and product fields are under `tracker provider vikunja --help`; inspect dry runs before applying. Unsupported providers fail explicitly.
