---
name: vikunja
description: Default task-management source for projects, subprojects, backlogs, priorities, and next-task requests when the user does not name another tracker. Inspect and safely manage Vikunja projects, task dependencies, boards, comments, and structured imports with the bundled API v2 CLI. An explicitly named source such as GitHub Issues, Jira, Linear, or local .flow tasks overrides Vikunja.
---

# Vikunja

Use `scripts/vikunja.py` (installed as `vikunja`). Prefer numeric IDs, external
IDs, and exact project paths. Ambiguity is an error; never reuse a task by title
alone.

## Default routing

Treat unqualified references to tasks, backlogs, projects, subprojects,
priorities, and "the next task" as Vikunja requests. If the user explicitly
names another source, such as GitHub Issues, Jira, Linear, or local `.flow`
tasks, use that source instead.

## Safety

- Reads are direct. Before a write, run `vikunja status` and inspect the exact
  project/task.
- Individual writes need an explicit write command. Bulk changes and imports
  default to dry-run and require `--apply`.
- Deletes require `--force`. Imports add or update; they never delete.
- Pass multiline Markdown through `--description-file` or `--comment-file`.

## Unclear requests

Use only minimal read-only discovery to resolve vague wording. If more than one
plausible target, action, or intended outcome remains—or the choice would
materially change the result—stop before reading task bodies or making changes
and ask one concise clarifying question. Do not broaden scope or choose the
first match merely to keep moving. If discovery yields one clear candidate,
state the interpretation and proceed.

## Resolve project scope first

Do this before reading tasks when the project reference is approximate or
relational: “under X”, “X's subproject”, “I forgot the name”, or similar.

```bash
vikunja projects search QUERY
vikunja projects children PARENT
vikunja projects children PARENT --recursive
```

Use project titles and paths first; do not read task bodies during discovery.
If one descendant plausibly matches, state the interpretation and continue. If
several remain, compare compact task headings only; ask the user only when the
scope is still ambiguous. Never use task `--recursive` to compensate for an
unresolved project reference: it deliberately merges parent and descendant
work.

## Find tasks by text

`tasks search` matches server-side. Know what it can and cannot do before
trusting an empty result:

- It matches the query as one **contiguous substring** of the title or the
  description. There is no tokenisation and no stemming, so `single node` and
  `топология` miss text that reads `single-node` and `топологии`. Search the
  longest fragment you are sure is literal, then widen.
- **Comments are never searched**, on any backend. Decisions recorded in
  comments are invisible here; read the candidate cards to find them.
- Case-insensitivity is ASCII-only on SQLite and MySQL: `SINGLE-ZONE` matches
  but `Топологи` does not. Lowercase Cyrillic queries. PostgreSQL matches with
  `ILIKE` and does not have this limit.

`--filter` passes a filter expression to the server and combines with the query
and with `--project` as `&&`. Use it to narrow before reading anything:

```bash
vikunja tasks search 'zone' --project 'Root / Child' --recursive
vikunja tasks search 'zone' --filter 'done = false'
vikunja tasks list --filter 'done = false && priority >= 4'
```

Fields include `done`, `priority`, `project_id`, `percent_done`, and the date
fields; operators are `= != > >= < <= like in` combined with `&& || ()`.
`labels` expects label IDs, not titles. See https://vikunja.io/docs/filters.

Search returns whole task objects, descriptions included. Print the matching
lines rather than whole cards unless the card is what you need.

## Choose current work

After resolving the project:

```bash
vikunja tasks next --project 'Root / Child'
vikunja graph --project 'Root / Child'
vikunja graph --project 'Root / Child' --format mermaid
```

`tasks next` and `graph` omit descriptions, comments, and checklist text. An
open task is ready when it has no unfinished `blocked` dependency, including
blockers outside the project. Ready order is: active column (`Doing`,
`In Progress`, `В работе`), higher priority, project index, numeric ID. Report
parallel ready work when it matters; the selected first item is deterministic,
not necessarily a business decision.

Use `--recursive` only when the user wants the whole resolved project subtree.
After choosing a task, run `vikunja tasks get ID`; it returns that card and its
comments. Within the card, the first unchecked Markdown checkbox is the next
local step. Checklist items are not dependency nodes. Task `done` and
`blocking`/`blocked` relations control the project graph.

## Edit tasks and boards

The description is the current goal, scope, and acceptance criteria. Comments
are append-only history: progress, findings, decisions, blockers, and reopen
reasons. Update the description only when the requirement changes; record what
happened as a comment.

Relations, labels, assignees, priority, dates, status, and board column are
first-class fields. Do not duplicate them in description prose. Use direct
commands for one task:

```bash
vikunja tasks update ID [OPTIONS]
vikunja comments add ID --comment-file note.md
vikunja relations add ID blocking OTHER_ID
vikunja board show PROJECT
vikunja board move ID Doing
```

Moving to or from a board's done column also changes task `done`; the CLI
reports this side effect.

## Structured and uncommon operations

Read [references/usage.md](references/usage.md) before structured imports, bulk
updates, deletion, CLI maintenance, or when exact schemas/API contracts are
needed. Routine discovery, next-task selection, inspection, comments, and
single-task edits should not load that reference; use `vikunja --help` for
syntax.

For imports, prefer JSON plans and use `validate` → dry-run → `--apply` →
`verify`. Creating a missing plan root requires explicit user authorization and
`--create-root`. Review reported stale labels/relations separately; import does
not remove them.
