---
name: ab-pr
description: Open one GitHub PR for a validated parent task whose configured route lands through GitHub.
---

# ab-pr

Open the pull request the configured route calls for without asking; ask the user only about a blocker or disputed scope. Read the parent task and subtasks; confirm work is integrated, accepted and in Review. Inspect the actual complete diff and validation/review evidence. Do not create a PR per microtask.

Use this skill only when `workflow.merge_mode` is `github`. Local mode rejects PR creation without contacting GitHub and lands through ab-land instead.

Write a concise title and body in English, leading with the concrete problem and resulting behavior. Include the tracked parent link, acceptance evidence, validation and material limitations. Use an existing repository template when present. Put multiline content in a temporary file outside the working tree; PR preparation requires a clean task branch.

Run `workflow pr PARENT --title TITLE --body-file FILE [--draft]`. It resolves the parent branch from Git and the base from origin's default branch, then safely pushes only the parent. Existing matching PRs are reused; later commands find the PR on GitHub by head and base, so nothing records its number. Do not force-push, retarget, overwrite a user's PR body or bypass repository policy.

Report the PR link and continue with ab-land. With `github_provider` codex or claude the parent's final review now belongs to this pull request: run `workflow review PARENT` per ab-review once it exists. Otherwise the parent is reviewed locally with ab-review.
