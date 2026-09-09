---
name: ab-pr
description: Open one GitHub PR for a validated parent task after the user requests or accepts PR creation.
---

# ab-pr

Read the parent task and subtasks; confirm work is integrated, accepted and in Review. Inspect the actual complete diff and validation/review evidence. Do not create a PR per microtask.

Use this skill only when `workflow.merge_mode` is `github`. Local mode rejects PR creation without contacting GitHub and lands through ab-land instead.

Write a concise title and body in English, leading with the concrete problem and resulting behavior. Include the tracked parent link, acceptance evidence, validation and material limitations. Use an existing repository template when present. Put multiline content in a temporary file outside the working tree; PR preparation requires a clean task branch.

Run `workflow pr PARENT --title TITLE --body-file FILE [--draft]`. It resolves the parent branch from Git and the base from origin's default branch, then safely pushes only the parent. Existing matching PRs are reused; later commands find the PR on GitHub by head and base, so nothing records its number. Do not force-push, retarget, overwrite a user's PR body or bypass repository policy.

Report the PR link and offer ab-land. GitHub review may use configured Codex/Claude cloud integration; fresh local review is available when cloud review is absent or unavailable.
