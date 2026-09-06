---
name: ab-land
description: Bring a parent GitHub PR through feedback, CI and merge, then reconcile its tracker task.
---

# ab-land

Invoking this skill authorizes the normal sequential fix/review/merge workflow, subject to repository policy. Ask the user on ambiguity or disputed scope; unattended runs return needs-human. Never bypass branch protection or force-push.

1. Read the parent task and `workflow land PARENT inspect`. It finds the PR on GitHub by the parent branch and base; several matches stop the command instead of guessing. Confirm PR identity and head, inspect CI, all unresolved threads and comments. Group duplicates by root cause; retain accepted decisions. Use targeted read-only scouts for code/history/CI questions only when useful, within configured scout_limit. No parallel implementation.
2. `workflow land PARENT request-review` uses configured cloud Codex/Claude or recommends local review. Cloud Codex mentions cannot pin model/effort; Claude Action must set those in claude_args. Do not claim a comment controls unsupported settings. Respect wait_seconds; absence of a response is not approval. With fallback enabled use a fresh local ab-review if remote review is unavailable; strict mode reports the failure.
3. Use `tracker claim PARENT --resume` to return the assigned Review task to Doing. Address valid feedback sequentially with a bounded worker or primary for tiny changes. Preserve task-language progress. For disputed/out-of-scope requests ask the user, update affected tasks and dependencies, then respond. Reviewers remain read-only; primary handles comments and thread resolution through gh after verifying the fix.
4. For direct fixes record `workflow writer PARENT --provider ACTUAL` while Doing, then return to Review after acceptance. Commit fixes, run the project's validation with `workflow validate PARENT -- COMMAND ARGS`, push the parent safely, then request a current-head review or run ab-review. For local land acceptance require an actual fresh PASS even if normal auto-policy skipped review: prepare and ingest a configured native review, or run `workflow run PARENT --role reviewer --brief FILE --primary PROVIDER`.
5. Repeat inspect/fix/validate/review at most configured max_rounds and stop on repeated findings without progress. Keep a short iteration summary in the tracker; do not reset the bound by restarting the skill. CI failure, stale review, unaddressed threads, uncertainty or timeout prevents merge.
6. When current-head acceptance, validation and GitHub policy agree, `workflow land PARENT merge [--method squash|merge|rebase]` pins the expected head. A queued merge is not Done. After GitHub confirms merge, the command marks the parent Done; if tracker update fails, retry `workflow land PARENT reconcile`, never merge again.
