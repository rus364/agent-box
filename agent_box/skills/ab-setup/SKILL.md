---
name: ab-setup
description: Bind a repository to its tracker project and safely install compact workflow instructions.
---

# ab-setup

1. Read repository instructions and `agent-box tracker status`. Resolve the user's provider, server and project. Credentials and provider/model policy belong in global `agent-box config`; never put secrets in Git.
2. Inspect `tracker projects` and `tracker setup --project ID [--recursive]`. The preview reports, per project, the kanban view it resolved and the lifecycle columns it recognized. All five states are mandatory: Backlog, Ready, Doing, Review, Done. Each project needs exactly one kanban view with manually managed buckets; Done follows the view's own done bucket when it has one.
3. Setup never writes view or column IDs. Fix reported problems in the tracker instead: add the missing column, rename an unrecognized one, or remove the duplicate board. With `--recursive`, every project under the root must satisfy this, because all of them take part in the workflow.
4. Run `tracker setup --project ID [--recursive] --apply`. Review `.agent-box.json`, the managed block at the start of `AGENTS.md`, and the `CLAUDE.md` import. Setup preserves outside text and refuses edited blocks. Resolve refusals with the user, never overwrite them.
5. The base branch is origin's default branch, read from Git; there is nothing to configure. Verify `tracker status` and `tracker ready`. Commit the portable binding/instructions when appropriate. On another device, configure the same provider/server globally; project binding remains in the clone. Host and container both use `agent-box tracker`.
