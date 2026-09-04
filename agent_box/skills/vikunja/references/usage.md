# Vikunja skill reference

## Discovery and configuration

The canonical personal skill is `~/.agents/skills/vikunja`; the Codex skill
catalog points there directly. `~/.claude/skills/vikunja` and
`~/.local/bin/vikunja` are symlinks to the canonical skill and CLI entrypoint.

Configuration precedence:

- Token: `VIKUNJA_TOKEN`, then `~/.config/vikunja/token`.
- URL: `VIKUNJA_URL`, then `url` in `~/.config/vikunja/config.json`.

The token file should be mode `0600`. The CLI never includes the full token in normal or JSON output. Import state lives under `~/.local/state/vikunja/imports/`, outside the skill.

## Core commands

```bash
vikunja [--json] status
vikunja [--json] projects list|tree
vikunja [--json] projects search QUERY
vikunja [--json] projects children ID_OR_PATH [--recursive]
vikunja [--json] projects get ID_OR_PATH
vikunja projects create TITLE [--parent ID_OR_PATH] [--description TEXT | --description-file PATH]
vikunja projects update ID_OR_PATH [--title TITLE] [--description TEXT | --description-file PATH] [--archived true|false]
vikunja projects delete ID_OR_PATH --force

vikunja [--json] tasks list [--project ID_OR_PATH] [--recursive] [--filter EXPR]
vikunja [--json] tasks search QUERY [--project ID_OR_PATH] [--recursive] [--filter EXPR]
vikunja [--json] tasks next --project ID_OR_PATH [--recursive]
vikunja [--json] tasks get ID_OR_EXTERNAL_ID [--field NAME]
vikunja tasks create --project ID_OR_PATH --title TITLE [FIELD OPTIONS]
vikunja tasks update ID [FIELD OPTIONS] [--project ID_OR_PATH]
                        [--append-description TEXT | --append-description-file PATH]
vikunja tasks done|reopen ID
vikunja tasks move ID --project ID_OR_PATH
vikunja tasks assign ID [USERNAME ...]
vikunja tasks delete ID --force
vikunja --json tasks bulk-update changes.json --dry-run|--apply

vikunja [--json] board show ID_OR_PATH [--view ID_OR_TITLE]
vikunja [--json] board buckets ID_OR_PATH [--view ID_OR_TITLE]
vikunja board move TASK_ID_OR_EXTERNAL_ID BUCKET_ID_OR_TITLE [--view ID_OR_TITLE]

vikunja [--json] labels list
vikunja labels add|remove TASK_ID LABEL_ID_OR_EXACT_TITLE
vikunja labels delete LABEL_ID_OR_EXACT_TITLE --force
vikunja [--json] relations list TASK_ID
vikunja relations add|remove TASK_ID KIND OTHER_TASK_ID
vikunja [--json] comments list TASK_ID
vikunja comments add TASK_ID [COMMENT | --comment-file PATH]
vikunja --json snapshot [--project ID_OR_PATH] [--recursive]
vikunja [--json] graph --project ID_OR_PATH [--recursive] [--format human|mermaid]
vikunja --json external resolve EXTERNAL_ID_OR_TASK_ID
```

Global options (`--json`, `--config-dir`, `--state-dir`) precede the command.

### Project hierarchy discovery

`projects children PARENT` returns the parent's direct children as compact
IDs, titles, and full paths. `--recursive` returns every descendant but excludes
the parent itself. Resolution uses the same exact ID/title/path rules as other
project commands; ambiguous titles fail instead of selecting arbitrarily.

Use this before task discovery when the user describes a project relationally
or only remembers its parent. Do not substitute `tasks ... --recursive`: that
merges work from the parent and every descendant rather than resolving which
project the user meant.

### Dependency-aware next work

`tasks next` and `graph` build a read-only execution graph from Vikunja's
first-class `blocking` / `blocked` task relations. Parent/subtask, related, and
precedes/follows relations are not execution blockers. A task is ready when it
is open and all tasks that block it are done; blockers outside the selected
project are honored too.

Ready tasks are ordered deterministically: cards in an active column (`Doing`,
`In Progress`, `In-Progress`, or `В работе`) first, then priority descending,
then project task index and numeric ID ascending. `tasks next` renders the first
ready task plus any parallel ready alternatives. JSON output contains only
compact task summaries under `selected`, `ready`, `blocked`, `done`, and `tasks`,
plus `edges` and counts under `summary`; descriptions, comments, and checklist
text are omitted.

Checklist items remain ordered prose within a task. They are not dependency
nodes and do not affect whether another task is ready. A task's `done` field is
authoritative even if its description still contains unchecked boxes. An open
task with no unchecked boxes remains selected. After choosing a task, use
`tasks get ID` to read only that task's current body and comments and find its
first unchecked local item.

`graph` renders the whole project grouped by Ready, Blocked, and Done. Use
`--format mermaid` for a Mermaid `flowchart TD`; use global `--json` when a
machine-readable graph is more useful. `--recursive` includes child projects in
the set of work being selected.

### Task field options

`tasks create` and `tasks update` accept the same writable scalars, so one
invocation is enough for what used to need several verbs:

`--title`, `--description` / `--description-file`, `--priority 0..5`,
`--done true|false`, `--due-date`, `--start-date`, `--end-date`,
`--percent-done 0..1`, `--hex-color RRGGBB`, `--is-favorite true|false`.
`tasks update` adds `--project` to move in the same call.

Dates take ISO-8601 (`2026-09-10` or `2026-09-10T15:04:05Z`); `-` clears the
field. `--percent-done` is a fraction, so `0.5` means 50%. `tasks done`,
`tasks reopen`, and `tasks move` remain as shorthands.

### Long text

`--description-file`, `--append-description-file`, and `--comment-file` read a
file, or stdin when given `-`. Passing both the inline and the file form of the
same option is an error. Prefer these over quoting multi-line markdown into
argv. `tasks get ID --field description` prints one raw field, which makes a
read-edit-write round trip through a file straightforward.

`--append-description` adds prose to the end of the existing body while keeping
the `Vikunja external ID` footer last, so appending never breaks import
resolution.

### Kanban

`board show` prints the project's kanban board — every column with the cards in
it — from a single `buckets/tasks` request. `board buckets` is the same board
without the cards. `board move` puts one card in one column, resolving the task
by numeric ID or plan ID and the bucket by ID or exact title.

```bash
vikunja board show 'Root / Child'
vikunja board move M01-13 Doing
```

A card's column is **not** `bucket_id`: that field is always `0` on API v2. The
placement lives in the task's expanded `buckets` array, one entry per kanban
view, which `tasks list`, `tasks get`, and `snapshot` now always request. The
human output of `tasks list` shows the column in brackets and `tasks get` names
it under `column`.

Buckets belong to a view, not to a project, so a project with several kanban
views is ambiguous: the command names the views and asks for `--view` rather
than picking one. A view whose `bucket_configuration_mode` is `filter` builds
its columns from filter queries; `board move` refuses it and says so, because a
card only reaches such a column by changing the fields the filter matches.

Moving a card into the view's done bucket marks the task done, and moving it out
un-marks it. `board move` reports that as `"done": true|false` so the side
effect is never silent. Re-sending the column a card already occupies is
reported as `"moved": false` and writes nothing.

### Assignees

`tasks assign ID [USERNAME ...]` sets the complete assignee list; passing no
username unassigns everyone. Usernames must match exactly — an unknown or
ambiguous name is an error rather than a partial assignment.

### Deletion

`tasks delete`, `projects delete`, and `labels delete` each require `--force`.
Without it the command reports what would be removed (task title, project task
and child counts, or how many tasks carry the label) and exits non-zero. Import
and reconciliation never delete anything.

## Structured plan import

A plan is a reconciler, not a dump: it declares the desired state of the tasks
it names and leaves everything else alone. It may cover a subset of a project.

### JSON plans (preferred)

Author plans as JSON — multi-line markdown, lists, and numbers need no quoting
or column bookkeeping. The document is either a bare array of tasks or
`{"tasks": [...]}`.

```json
{"tasks": [
  {"id": "M01-13", "title": "Record the decision",
   "project": "Infrado / 01 — Migration architecture & decisions",
   "description": "Goal\n\n- first bullet\n- second bullet",
   "priority": 5, "labels": ["migration", "decision"],
   "depends_on": ["M00-04"], "parent": "M01-00",
   "due_date": "2026-09-10", "assignees": ["operator"], "bucket": "Doing"}
]}
```

Keys: `id` and `title` and `project` and `description` are required; `priority`,
`labels`, `done`, `kind`, `depends_on`, `source_tasks`, `order`, `due_date`,
`start_date`, `end_date`, `percent_done`, `hex_color`, `assignees`, `bucket`,
and `parent` are optional. Unknown keys are rejected.

### CSV plans

CSV remains supported for spreadsheet workflows and maps onto the same model.
Required columns are `Title`, `Description`, `Project`, `Plan ID`. Optional
columns are `Priority`, `Labels`, `Done`, `Kind`, `Depends On`, `Source Tasks`,
`Order`, `Due Date`, `Start Date`, `End Date`, `Percent Done`, `Hex Color`,
`Assignees`, `Bucket`, `Parent`. Unknown or duplicated columns are rejected.
`Order` may be omitted or repeated; ordering falls back to row order and ties
break on plan ID. In CSV, a blank optional cell leaves the field unmanaged and
`-` clears it; in JSON, an omitted key leaves it unmanaged and `[]` or `null`
clears it.

### Validation and references

Validation is local and needs no Vikunja configuration. It rejects unknown or
duplicated columns and keys, blank required values, invalid or duplicate plan
IDs, invalid priorities, booleans, orders, dates, colors, and percentages,
self-dependency, duplicate dependency edges, and cycles through `depends_on`
and `parent`.

A `depends_on` or `parent` entry that no row defines is an **external
reference**: it must already exist in Vikunja carrying that plan ID. Validation
reports these under `external_references` and the import resolves them,
failing closed with the offending IDs if any is missing. This is what lets a
one-task plan attach to an existing graph.

### Import phases

1. Validate the source and digest it.
2. Require the existing root by default, reuse exact project paths, and create missing descendants. `--create-root` permits creating a missing root only when the user explicitly authorized it.
3. Reuse tasks only by unique external marker, otherwise create them. An unmarked title collision is a conflict.
4. Create/reuse labels and attach expected labels.
5. Set assignees and buckets for the rows that manage them. A row whose card
   already sits in the named column is reported as `reuse` and writes nothing,
   so re-applying a plan never drags a card back out of the column somebody
   moved it to. Dry-run shows the current column alongside the wanted one.
6. Create relations only after every numeric task ID exists, including resolved external references. `depends_on: ["B"]` on task A becomes base task B `blocking` other task A, and Vikunja records the inverse `blocked`. `parent: "P"` becomes `parenttask` posted on the child, with the inverse `subtask` on the parent.
7. Re-read current API state and compare projects, task properties, labels, buckets, IDs, and relation direction.

The source digest selects an atomic state file. Each completed project, task, and relation is recorded immediately, making interruption outcomes inspectable. A short `Vikunja external ID: …` description footer and the state file provide both external-ID-to-task-ID and task-ID-to-external-ID resolution across sessions. A changed source digest cannot reuse an old state file.

### Stale bindings

The importer only adds. Because a removed dependency or label would otherwise
linger silently, both dry-run and apply return a `stale` report listing managed
relations (`blocked`, `parenttask`) and labels that exist in Vikunja but not in
the plan. Nothing is deleted; act on the report with `relations remove` or
`labels remove` when the removal is intended.

The importer will not create a missing root unless `--create-root` is explicit, title-match tasks, or proceed through ambiguous paths/IDs. `--apply` is the only import mutation switch.

### Keep the body prose-only

Relations, labels, priority, dates, assignees, and status are first-class
Vikunja fields and are what the reconciler compares against. Restating them as
text inside the description creates a second copy that drifts and has to be
rewritten by hand on every change. Descriptions should carry prose; the plan
document carries the structure. The `Vikunja external ID` footer is the sole
exception, and the importer maintains it.

## JSON and API behavior

`--json` emits UTF-8 JSON with sorted keys and a trailing newline. Arrays have deterministic ordering where the API entity has a stable ID or project path. Pagination follows `total_pages` and fails if fewer than `total` records arrive; extra server-provided synthetic collections are preserved.

This implementation targets API v2. The installed instance's `/api/v2/openapi.json` is authoritative. Rich text is exchanged as Markdown; partial rich-text writes use `X-Vikunja-Format: markdown`. There is no API v1 fallback.

Exit code `0` means success. User/config/validation/API/conflict failures return `2` with a concise error on stderr. A read-back verification that found drift returns `3`, both for `import verify` and for the `verification` block of `import plan --apply`. Authentication failures are classified separately internally and never include the credential.

## Tests and maintenance

Run:

```bash
cd ~/.agents/skills/vikunja
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/vikunja.py
```

The HTTP tests open an ephemeral localhost listener. Refresh API assumptions against the target instance's OpenAPI document before changing endpoint mechanics.
