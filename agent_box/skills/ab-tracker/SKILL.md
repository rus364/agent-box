---
name: ab-tracker
description: Read and maintain external tracker tasks, progress, dependencies, and history through agent-box.
---

# ab-tracker

Use `agent-box tracker --help` and command help for exact arguments. Output is JSON by default; no `--json` flag. Never call a provider API directly for common operations.

- `agent-box tracker status` reads account, bound project and lifecycle columns; `agent-box tracker get ID` reads a full task including requirements and comments. Search before selecting an ID; `search`, `ready`, and `next` return compact summaries. Expand only relevant tasks, relations, and history.
- `claim ID` checks readiness and ownership, assigns the API-key user and enters Doing. `--resume` is deliberate same-user recovery. Assignment is a cooperative lock, not distributed exclusion. Recheck ownership before writing.
- `create`, `update`, `tier`, `relation`, `comment`, and `check` maintain tasks. Read `checklist ID` before `check ID INDEX --text EXPECTED`; use `--undo` for contradicted evidence. Files/stdin carry multiline text.
- Use the task's language for descriptions and comments. Preserve unrelated text. First-class fields hold status, priority, assignee, relations and tier; do not duplicate them in Markdown.
- Descriptions contain Goal, Context, Acceptance and Validation (translated to task language). Checkboxes record evidenced progress, not reviewer acceptance. Decisions and failures belong in short comments.
- Comments are a human journal only. Branch, base branch and pull request are read from Git and GitHub; no comment can set them, and nothing machine-readable belongs in one.
- The coordinator is the only writer: requirements, lifecycle, dependencies, progress comments, checkboxes, branches and GitHub. Every delegated role - worker, reviewer, scout - reads the tracker and reports back instead. This is a contract, not credential isolation, and it is the same whether the delegate is a native subagent or an agent-box-managed CLI run.
- Required states: Backlog, Ready, Doing, Review, Done. Dependencies block deterministically; a completed sibling unblocks after parent-branch integration, while an external parent's work must first merge into base.
- `audit --project ID --recursive` reads the complete project tree without saving a binding. Structural errors block adoption; warnings about Backlog preparation, historical progress and duplicate titles stay visible without forcing invented requirements.
- `adopt clone|boards|cutover --receipt PATH` orchestrates native staging adoption. Use `--adoption PATH` before ordinary tracker subcommands to normalize only staging tasks. Source data stays intact until the final root rename; the binding switches last.
- Use `agent-box workflow finish ID` for completion. Provider-specific imports, boards, bulk edits and product fields are under `tracker provider vikunja --help`; inspect dry runs before applying. After adoption, imports with preserved external IDs must select `--project NEW_ROOT_ID` so the backup's markers and checkpoints cannot redirect updates. Unsupported providers fail explicitly.
