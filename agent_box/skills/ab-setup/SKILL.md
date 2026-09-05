---
name: ab-setup
description: Bind a repository to its tracker project and safely install compact workflow instructions.
---

# ab-setup

1. Read repository instructions and `agent-box tracker status`. Resolve the user's provider, server and project. Credentials and provider/model policy belong in global `agent-box config`; never put secrets in Git.
2. Inspect `tracker projects` and `tracker setup --project ID [--view ID]`. Select a manual kanban view. All five states are mandatory: Backlog, Ready, Doing, Review, Done. Propose a mapping from existing columns; if a state is missing, identify it and ask the user to add it. Do not silently create or omit workflow states.
3. Resolve the actual base branch from Git/GitHub. Save a JSON mapping from required state names to column IDs, then run `tracker setup --project ID --view ID --states-file FILE --base-branch BRANCH --apply`.
4. Review `.agent-box.json`, the managed block at the start of `AGENTS.md`, and the `CLAUDE.md` import. Setup preserves outside text and refuses edited blocks. Resolve refusals with the user, never overwrite them.
5. Verify `tracker status` and `tracker ready`. Commit the portable binding/instructions when appropriate. On another device, configure the same provider/server globally; project binding remains in the clone. Host and container both use `agent-box tracker`.
