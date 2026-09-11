---
name: ab-tracker
description: Read and maintain external tracker tasks, progress, dependencies, and history through agent-box.
---

# ab-tracker

Use `agent-box tracker --help` and command help for exact arguments. Output is JSON by default; no `--json` flag. Never call a provider API directly for common operations.

- `agent-box tracker status` reads account, binding and lifecycle. Resolve user numbers, exact titles or URLs with `tracker resolve REFERENCE` (quote `#21`); `--title` forces title matching, `--project PROJECT` narrows to one bound project. A unique exact match selects automatically; ambiguity requires the user to choose among the reported projects/tasks. No match means ask for a title/project/URL; never substitute full-text search results.
- `tracker get ID` reads requirements and comments. Pass the returned canonical `id` unchanged to get, mutations and workflow; never resolve it again as a visible number. Before writing, compare number, project and title with user intent. `search`, `ready`, and `next` return compact summaries; text search finds candidates, not exact references. URLs identify their own tasks without adoption redirects.
- `claim ID` checks readiness and ownership, assigns the API-key user and enters Doing. `--resume` is deliberate same-user recovery. Assignment is a cooperative lock, not distributed exclusion. Recheck ownership before writing.
- `create`, `update`, `tier`, `relation`, `comment`, and `check` maintain tasks. Read `checklist ID` before `check ID INDEX --text EXPECTED`; use `--undo` for contradicted evidence. Files/stdin carry multiline text.
- Use the task's language for descriptions and comments. Preserve unrelated text. First-class fields hold status, priority, assignee, relations and tier; do not duplicate them in Markdown.
- Descriptions contain Goal, Context, Acceptance and Validation (translated to task language). Checkboxes record evidenced progress, not reviewer acceptance. Decisions and failures belong in short comments.
- Comments are a human journal only. Branch, base branch and pull request are read from Git and GitHub; no comment can set them, and nothing machine-readable belongs in one.
- The coordinator reads and writes tracker state and alone owns requirements, lifecycle, dependencies, progress comments, checkboxes, branches and GitHub. Delegated workers, reviewers and scouts use their supplied self-contained assignment and repository instructions, without rereading tracker comments or history, and report back. This contract is the same for native subagents and agent-box-managed CLI runs; it is not credential isolation.
- Required states: Backlog, Ready, Doing, Review, Done. Dependencies block deterministically; a completed sibling unblocks after parent-branch integration, while an external parent's work must first merge into base.
- `audit --project ID --recursive` reads the complete project tree without saving a binding. Structural errors block adoption; warnings about Backlog preparation, historical progress and duplicate titles stay visible without forcing invented requirements.
- `adopt clone|boards|cutover --receipt PATH` orchestrates native staging adoption. Use `--adoption PATH` before ordinary tracker subcommands to normalize only staging tasks. Source data stays intact until the final root rename; the binding switches last.
- Use `agent-box workflow finish ID` for completion. Provider-specific imports, boards, bulk edits and product fields are under `tracker provider vikunja --help`; inspect dry runs before applying. After adoption, imports with preserved external IDs must select `--project NEW_ROOT_ID` so the backup's markers and checkpoints cannot redirect updates. Unsupported providers fail explicitly.
