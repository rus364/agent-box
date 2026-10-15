# Task workflow

The workflow turns tracked tasks into reviewed, merged work. A coordinating
agent — your Claude or Codex session — follows short `ab-*` skills that
agent-box ships: it clarifies a task, plans it, implements it on a task branch,
validates and reviews it, and lands it. Every step leaves its state where it
belongs: lifecycle in the [tracker](tracker.md), code and branches in Git and
GitHub.

How the guarantees below are implemented is recorded in the
[workflow design notes](../design/workflow.md).

## Enabling it

1. Set up the [tracker](tracker.md) and bind the repository with `ab-setup`.
2. Enable **Workflow** in `agent-box config`. Saving installs the skills and
   native agent profiles; `config apply` and `upgrade` refresh them.
3. Configure model lists for the roles you want to delegate (optional; see
   [models and routing](#models-and-routing)).

| Skill | Use it to |
| --- | --- |
| `ab-setup` | Bind a repository to its tracker project |
| `ab-adopt`, `ab-normalize` | Bring existing tasks in line with the workflow |
| `ab-interview` | Resolve open decisions and write implementation-ready acceptance |
| `ab-plan` | Turn a deliverable into a checklist or a parent with subtasks |
| `ab-work` | Implement tasks: branch, delegate, validate, review, finish |
| `ab-review` | Get an independent review of a task or the whole parent |
| `ab-sync` | Run Plan sync, which keeps task descriptions current |
| `ab-pr`, `ab-land` | Open the parent's pull request and land it |
| `ab-tracker` | Read and maintain tasks (installed with the tracker alone) |

You drive it in plain language: "interview #14", "plan it", "take the next
task". The skills call `agent-box tracker` and `agent-box workflow` for every
step that has to be checked.

## How work flows

A **parent task** is a deliverable; its **subtasks** are steps small enough to
implement and test in one go. Small work stays one task with a checklist.

1. **Interview** settles open decisions with you and records them in the task.
2. **Plan** creates subtasks with acceptance criteria and dependencies and
   moves them to Ready.
3. **Work** claims the parent and creates its branch `ab/<id>-…` from origin's
   default branch, then takes subtasks one at a time, each on its own branch
   from the parent. For each subtask the coordinator implements (or delegates),
   commits, validates, gets the review the policy asks for, and finishes it: the
   subtask merges into the parent branch and becomes Done.
4. **Parent review.** When all subtasks are Done, the complete parent diff is
   validated and reviewed, and the parent moves to In review.
5. **Landing** merges the parent into the base, by a local fast-forward or a
   GitHub pull request, and marks it Done.

The commands behind those steps:

```bash
agent-box workflow inspect TASK          # branch, acceptance, budget and the next step
agent-box workflow begin TASK            # claim and create or resume the branch
agent-box workflow delegate TASK --role worker --brief brief.md
agent-box workflow validate TASK -- python3 tests/run_suite.py
agent-box workflow review TASK           # apply review policy, run a reviewer
agent-box workflow finish TASK           # subtask: integrate and mark Done; parent: In review
agent-box workflow pr TASK               # GitHub mode: open the parent's pull request
agent-box workflow land TASK merge       # land the parent and mark it Done
```

`workflow inspect` accepts the same references as `tracker get` and always
names one next step, derived from what is recorded for the current head. It is
read-only and works before `begin`, on another branch or with uncommitted
files.

**Acceptance belongs to the code.** Validation, review and your explicit
approval are recorded for the exact head and base commits. A new commit or a
moved base retires them; editing the task description does not. Put every
decision that matters into the task description, because that is what the
reviewer reads — never comments or conversation.

## Roles

| Role | Does | Never |
| --- | --- | --- |
| Coordinator | Your session: owns requirements, lifecycle, branches, commits, pushes and GitHub | Reviews its own work |
| Worker | Changes project files from a brief and answers in Markdown | Commits, switches branches, writes the tracker or GitHub |
| Scout | Answers one research question with facts, sources and uncertainty | Changes anything |
| Plan sync | Updates task descriptions and journal comments after decisions and merges | Changes project files, Git, lifecycle or dependencies |
| Reviewer | Reads the task and diff in fresh context and returns a verdict with findings | Changes anything |

A brief is a compact English file outside the working tree: the goal, scope
and relevant paths, decisions and limitations. Every assignment gets a fresh
agent with no conversation history. The coordinator checks the answer against
the code and commits only what it accepts.

These roles are a contract, not credential isolation: a delegated agent works
in the same workspace with the same credentials.

## Models and routing

Each role has its own ordered **candidate list** under **Workflow** in
`agent-box config`: `workflow.worker`, `workflow.scout`, `workflow.sync` and
`workflow.review`, each with `models`. A candidate names the CLI (`codex` or
`claude`), the model, an effort, a speed and optionally an
[external API provider](external-providers.md#workflow). The lists start
empty.

- **The first available candidate runs**; the rest are fallbacks, used only
  when a CLI is missing, a subscription window is exhausted, or (for
  reviewers) a run hits an authentication, quota or timeout failure.
- **An empty list means "do it yourself"** for workers and scouts: the
  coordinator does the work in its own session. An empty reviewer list means a
  fresh native subagent of the coordinator's session reviews — never the
  coordinator itself. Plan sync needs a configured list.
- **Any CLI may serve any role.** A model of the other CLI than the
  coordinator's runs as a managed `codex exec` or `claude -p` process; a
  candidate of the coordinator's own CLI runs as a native subagent when
  available.
- **Session pins** override the lists for one session:
  `--worker`, `--scout`, `--sync`, `--reviewer` with
  `PROVIDER[@API]:MODEL[:EFFORT]`.
- **Task tiers.** With `workflow.use_tiers` on, a task labelled
  `ab:tier:1`–`3` uses that tier's `tiers.N` list for its worker, Plan sync and
  reviewer, when the list is not empty. Scouts have no tiers.
- **Another vendor reviews first.** `workflow.review.other_vendor_first` puts
  reviewers from a vendor other than the code's authors ahead of the authors'
  own, keeping them as the last fallbacks.
- **Speed.** Each candidate's speed is `inherit` (default), `standard` or
  `fast`. An explicit speed always runs as a managed process, since native
  subagents inherit the session's speed. Fast is accepted only for models known
  to support it, and external API candidates support only `inherit`.
- **Scouts** may run in parallel up to `workflow.scout.limit` (default 3).

A project can replace any role's list in `.agent-box.json`; a project list
replaces the global one whole.

## Review

`workflow.review.policy` decides when a review is required:

- `off` — never.
- `auto` (default) — when a change touches `max_files` files (default 5) or
  `max_lines` lines (default 200), touches a `sensitive_paths` glob, carries
  a review label, or changes a binary file. Changes confined to
  `ignored_paths` do not count, so a documentation-only change with
  `["**/*.md", "docs/**"]` needs no automatic review.
- `always` — every subtask and the complete parent diff.

The same policy applies to subtasks and to the parent's complete diff.
SKIPPED, INCONCLUSIVE and provider errors are not PASS.

- **Verdicts.** The reviewer returns PASS, NEEDS_WORK or INCONCLUSIVE with
  findings by severity. The coordinator verifies each finding, fixes what is
  valid, pushes back with evidence on what is not, and journals the outcome in
  the task.
- **Budget.** `workflow.review.max_rounds` (default 3) counts received
  verdicts per task. A failed run (crash, timeout, no verdict) costs nothing,
  but two failures in a row stop review. Only you can extend either:
  `agent-box workflow review-budget TASK --reason TEXT [--limit TOTAL]`.
- **Incremental review.** `workflow.review.incremental` (`off` by default, or a
  severity) lets a small fix after a round with only minor findings be judged
  by the size of the fix instead of the whole diff. A fix that triggers nothing
  is *accepted by the delta threshold* — enough to finish, never a PASS.
- **Your approval.** Instead of a required review you may accept the current
  head yourself: `agent-box workflow approve TASK --reason TEXT`. It needs
  successful validation of that head and is retired by the next commit.
- **Open checklist items.** An item only you can close, such as a check that
  cannot run in a container, never blocks a round or a subtask's finish; it
  blocks only landing.

### Cloud review in the pull request

With `workflow.review.github_provider` set to `codex` or `claude` in GitHub
merge mode, the parent's final review happens in its pull request:
`workflow review PARENT` posts one `@codex review` or `@claude review` request
for the exact head, and every later run reads the bot's answer back into the
same budget. Subtasks keep their local rounds. Silence is never acceptance:
after `wait_seconds` (default 3600) the round fails without spending an
attempt and you decide what to do. With `auto` or `local`, every round is
local.

## Plan sync

Plan sync is a separate agent that keeps task descriptions in line with
decisions and merged code: it fixes stale statements and references in
affected open tasks, and never touches project files, Git or lifecycle.

| Setting | Behaviour |
| --- | --- |
| `workflow.sync.policy` | `off` (default), `auto` or `always` |
| `events` | `plan` after planning or a decision and `after` integration (default); optionally `before` implementation |
| `paths` | For `auto` after-events: run only when committed changes match (default `docs/**`, `*.md`) |
| `scope` | `project` (default) or the source `parent` tree |
| `references` | Maintain `path#heading` reference sections (default on) |
| `models`, `tiers.N` | Its own candidate list |

`begin`, a subtask's `finish` and the parent's landing return a Plan sync
request, which the coordinator runs through `ab-sync`. A run reports
`UPDATED`, `UNCHANGED`, `BLOCKED` (a decision is needed) or `ERROR`.

```bash
agent-box workflow sync TASK                  # manual, even with the policy off
agent-box workflow sync TASK --event plan     # as the policy decides
```

## Landing

`workflow.merge_mode` chooses how the parent reaches the base branch.

**GitHub** (default). `ab-pr` opens one pull request for the parent;
`ab-land` handles CI, comments and unresolved threads, gets current-head
acceptance and merges with `--match-head-commit`. Branch protection is never
bypassed. The pull request is found by head and base each time, never stored,
and agent-box writes no closing keywords into it. With `workflow.auto_push`
on (default), the parent branch is pushed after each finished subtask.

**Local.** `workflow land PARENT merge` fast-forwards the local base to the
accepted head and, with `auto_push` on, pushes exactly that commit to origin
before marking the parent Done. Task branches are never pushed. There is no
pull request, so every review round is local.

```bash
agent-box workflow land PARENT inspect    # derived refs and blockers
agent-box workflow land PARENT merge      # idempotent: rerun after any failure
agent-box workflow land PARENT cleanup    # retry branch cleanup only
```

**Branch cleanup.** `workflow.cleanup_local_branches` and
`workflow.cleanup_remote_branches` (both off by default) delete the parent's
and its subtasks' branches after landing, and a subtask's own branch when it
finishes. A branch is deleted only when its integration is proven — through
the merged pull request or the local base — and only if it still points where
expected. A cleanup failure never undoes the merge or Done.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `workflow.enabled` | off | Install and allow the workflow (global only) |
| `workflow.merge_mode` | `github` | `github` pull request or `local` fast-forward |
| `workflow.auto_push` | on | Publish the parent (GitHub) or the landed base (local) |
| `workflow.cleanup_local_branches`, `cleanup_remote_branches` | off | Delete integrated task branches |
| `workflow.use_tiers` | off | Let task tiers select model lists |
| `workflow.timeout_seconds` | 3600 | Limit for each managed run |
| `workflow.scout.limit` | 3 | Parallel scouts |
| `workflow.review.policy` | `auto` | `off`, `auto`, `always` |
| `workflow.review.max_files`, `max_lines` | 5, 200 | Auto-review thresholds (inclusive) |
| `workflow.review.sensitive_paths`, `ignored_paths` | — | Globs that always trigger or never count |
| `workflow.review.max_rounds` | 3 | Verdicts per task |
| `workflow.review.incremental` | `off` | Judge small follow-up fixes by their delta |
| `workflow.review.other_vendor_first` | off | Prefer reviewers of another vendor |
| `workflow.review.github_provider` | `auto` | `auto`, `local`, `codex` or `claude` |
| `workflow.review.wait_seconds` | 3600 | How long to wait for a cloud reviewer |

All workflow settings except `enabled` can be overridden per project in
`.agent-box.json`. Session options such as `--review`, `--rounds`, `--merge`
and `--no-push` override them for one session; see
[session options](../usage.md#session-options).

## When something goes wrong

Every workflow command is safe to rerun: it continues from what is recorded
and refuses rather than guesses.

- **A command stopped halfway** (network, tracker, a killed process): run the
  same command again. `finish`, `land merge` and `land cleanup` continue from
  the failed step, and a pull request GitHub already merged is never merged
  again.
- **Review stopped after two failures or used its budget:** decide whether to
  continue; then `workflow review-budget TASK --reason TEXT [--limit TOTAL]`.
- **A review answer could not be applied** (dirty tree, wrong branch, tracker
  error): restore a clean checkout of the task branch and run
  `workflow review-result TASK --round=ROUND --reconcile`; no model runs and
  no attempt is spent.
- **Plan sync ended BLOCKED or ERROR:** it may have written part of its
  changes. Reread the affected tasks, then record what you verified with
  `workflow sync TASK --reconcile REPORT`, or resolve the decision with
  `ab-interview`.
- **A new clone or another machine has no recorded acceptance:** on the task
  branch run `workflow validate`, get the review the policy asks for, then
  rerun the landing command.
- **The base already contains the work** (for example after a manual merge):
  only an independent review or your explicit acceptance of the complete
  implementation can complete the parent; its diff alone is empty.
- **A branch was already pushed from another machine:** `begin` adopts
  `origin/ab/<id>-…` instead of starting over, and stops if local and remote
  disagree.
