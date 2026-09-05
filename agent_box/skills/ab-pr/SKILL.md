---
name: ab-pr
description: Open one GitHub PR for a validated parent task after the user requests or accepts PR creation.
---

# ab-pr

Read the parent task and subtasks; confirm work is integrated, accepted and in Review. Inspect the actual complete diff and validation/review evidence. Do not create a PR per microtask.

Write a concise title and body in English, leading with the concrete problem and resulting behavior. Include the tracked parent link, acceptance evidence, validation and material limitations. Use an existing repository template when present. Put multiline content in a file.

Run `workflow pr PARENT --title TITLE --body-file FILE [--draft]`. It checks the recorded parent/base, safely pushes only the parent and records the PR in the tracker. Existing matching PRs are reused. Do not force-push, retarget, overwrite a user's PR body or bypass repository policy.

Report the PR link and offer ab-land. GitHub review may use configured Codex/Claude cloud integration; fresh local review is available when cloud review is absent or unavailable.
