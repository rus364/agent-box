---
name: ab-setup
description: Bind a repository to its tracker project and safely install compact workflow instructions.
---

# ab-setup

1. Read repository instructions and `agent-box tracker status`. Resolve the user's provider, server and project. Credentials and integration permissions belong in global `agent-box config`; `config --project` edits allowed project policy. Never put secrets in Git.
2. Inspect `tracker projects` and `tracker setup --project ID [--recursive|--no-recursive]`. Omitting the scope flag preserves the saved scope for the same provider/server/project; a new binding defaults to root only. The preview reports, per project, the kanban view it resolved and the lifecycle columns it recognized. All five states are mandatory: Backlog, Ready, Doing, Review, Done. Each project needs exactly one kanban view with manually managed buckets; Done follows the view's own done bucket when it has one.
3. Setup never writes view or column IDs. `tracker provider vikunja board init PROJECT [--recursive]` adds the missing lifecycle columns and sets the view's done column; inspect the dry run before `--apply`. It only adds, so an unrecognized title, an ambiguous match or a filter-built board stays reported for the user to rename, disambiguate or rebuild. With `--recursive`, every project under the root must satisfy this, because all of them take part in the workflow.
4. Run `tracker setup --project ID [--recursive|--no-recursive] --apply`. Review `.agent-box.json`, the managed block at the start of `AGENTS.md`, and the `CLAUDE.md` import. Setup preserves project overrides and outside text and refuses edited blocks. Resolve refusals with the user, never overwrite them.
5. Offer ab-adopt when the project already holds tasks: setup binds a repository, it does not bring existing tasks in line with the workflow.
6. The base branch is origin's default branch, read from Git; there is nothing to configure. Verify `tracker status` and `tracker ready`. Commit the portable binding/instructions when appropriate. On another device, configure the same provider/server globally; project binding remains in the clone. Host and container both use `agent-box tracker`.
