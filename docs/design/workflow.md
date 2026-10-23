# Workflow design notes

For contributors. The user guide is [the workflow integration](../integrations/workflow.md);
this page records how its guarantees are kept and why. Observed CLI
interfaces are in the [native launch contract](native-launch.md), sources and
reuse decisions in [workflow design references](workflow-references.md).

## Branches and base

- Each task owns the branch namespace `ab/<task id>-*`, so the branch is found
  in Git, never recorded. A local branch wins; otherwise a branch already pushed
  as `origin/ab/<id>-*` is adopted with a tracking branch, so another machine
  continues the work instead of forking the base again. Two branches for one
  task, a local and remote name that disagree, or a local branch whose remote
  moved ahead stop the command instead of choosing.
- The base is origin's default branch, asked of the remote itself, because
  `refs/remotes/origin/HEAD` is only as fresh as the last fetch and a renamed
  default would keep receiving work at its old name. The local ref is the
  offline answer.
- The coordinator creates the parent branch from the base and each subtask
  branch from the parent, and integrates each subtask before the next. No
  parallel workers and no branch creation by delegated agents.
- `workflow begin CHILD` records the initial head only for a new branch claimed
  from Ready. A child still at that head can finish without commits after
  validation and the configured review. Resuming In progress or In review work,
  or adopting a remote branch, neither creates nor resets this evidence. Lost
  acceptance of an integrated change cannot be replaced by validating its
  now-empty diff: preserve the branches and restore the original acceptance or
  reconstruct the change. Missing creation evidence also prevents claiming that
  an integrated child began without commits.
- Child finish saves acceptance before integration and publication, so
  `workflow finish CHILD` can be retried after a failed Done write; it rechecks
  the accepted head, base and current review policy. In GitHub mode auto-push
  publishes the current parent tip containing the accepted child before Done.

## Pause and resume

Pause and resume reuse Git namespaces and tracker ownership, with no persisted
pause-state schema or machine-readable tracker comments. Pause deliberately
does not change lifecycle: blocking/deferral requires a separate explicit user
decision. Its optional checkpoint publication freezes the push URL and object
IDs, uses non-force explicit refs, and verifies live remote heads before
switching. Failures may leave a published checkpoint or a switched checkout;
neither case erases task refs, and retries read current Git/tracker state.
Resume fetches missing remote objects without applying ambient fetch mappings,
creates only branches backed by verified existing commits, and invokes begin
with existing-only branch switching. It never recreates lost work from base.

## Roles and delegation

- The contract is the same for a native subagent and a managed `codex exec` /
  `claude -p`. It is a contract boundary, not credential isolation: a native
  delegation inherits the coordinator's workspace, `.git` and credentials, so
  neither backend gets private restrictions. Workers and scouts are only
  launched, never tracked.
- `delegate` runs the role's first available candidate once as a fresh process
  and prints its Markdown answer on stdout; the route, token usage and the path
  of a private copy of the answer follow on stderr as one JSON line. The prompt
  asks for `## Done`, `## Evidence` and `## Limitations`; nothing parses the
  answer. Workflow records nothing about the run, so editing the task or
  committing while a delegate works affects no later command.
- A managed worker needs the task branch checked out and holds the local work
  lock, so validation cannot run under it. A worker failure other than an
  unavailable CLI or model never falls back to another candidate, because files
  may already have changed.
- Managed scouts overlap up to `workflow.scout.limit`, counted by
  per-repository slot locks that a finished or killed process releases; the
  coordinator counts its native scouts itself.
- With workflow enabled, interactive `agent-box codex` and `--host codex`
  launches derive Codex's native capacity from the effective scout limit:
  V1 receives that many subagent slots and V2 one more for the coordinator (a
  limit of 6 sets `agents.max_concurrent_threads_per_session=6` and
  `features.multi_agent_v2.max_concurrent_threads_per_session=7`). These are
  launch-time overrides; `~/.codex/config.toml` is not written. Batch, managed
  and service commands (including `app-server`) and already running or
  independently launched sessions keep their own settings.
- Claude managed runs use `--safe-mode`, which keeps the user's hooks, skills,
  MCP servers and agents out of the context and also disables `CLAUDE.md`
  discovery. The repository's `AGENTS.md` and `CLAUDE.md` therefore travel
  completely in the prompt, with instructions to read nested files and
  `@path` imports. Read errors and invalid UTF-8 fail explicitly; text is never
  truncated. Symlinked instruction files are excluded with a stated limitation.
- Provider errors, including Claude's exit-zero error envelopes, are never
  findings or verdicts.

## Plan sync

- The executor reads Git, documents and the live tracker itself, starting from
  the source decisions and finding candidates by changed paths, anchors, domain
  terms and relations. Scope bounds discovery; it does not read every task.
- It writes only for a concrete stale assertion, a decision changing scope or
  acceptance, or a missing or stale reference an affected task needs. No
  recaps, commit hashes, sync bookkeeping or copies of workflow instructions; no
  correction means no write. It preserves requirements, progress marks and an
  existing reference section's heading and location, uses the resolved tracker
  language for edited descriptions, and verifies
  saved text. Open tasks in any state are editable; only Done tasks are
  historical evidence. Lifecycle, assignees, dependencies and progress marks
  never change.
- Auto after-events ignore changes confined to an intact, checksum-verified
  managed `workflow` block in the root `AGENTS.md`, comparing both committed
  versions; missing files or damaged blocks cannot qualify. Missing source
  commit evidence triggers inspection rather than a silent skip.
- Permissions: Codex uses a read-only filesystem profile with network; managed
  Claude gets Read/Grep/Glob and Bash for the tracker CLI and read-only Git,
  without Edit/Write; native Claude inherits session permissions, so its
  no-edit rule is a contract. Writes use `update --revision REV` and reread.
- Managed executors receive `AGENT_BOX_SYNC_EXECUTOR=1` in their own
  environment; `workflow sync` refuses such callers before configuration,
  tracker access or locks. This diagnoses accidental recursion, not isolation.
  The coordinator passes a task reference and scope, never assembled texts.
- No provider fallback after possible writes; a failure before launch may use
  the next candidate. `--reconcile FILE` records the coordinator's recovery
  without a model call or edits, keeping the original report, diagnostics,
  event and head beside it. Pending, ERROR and BLOCKED results cannot be
  silently overwritten by a new run. Repeating a completed after event at the
  same head requests nothing.

## Review rounds

- **Backend choice per round.** A local round uses the first candidate's
  native profile when it is available with the configured model, effort and
  speed, otherwise the managed command. An earlier thread-limit error is not
  proof native launching is still unavailable. An empty reviewer list allows
  only an independent native reviewer, never self-review. A native result is
  taken on trust: its model and fresh context are the coordinator's word. Only
  the latest round can be answered, and a new round removes the input of an
  unanswered native one.
- **Answer format.** Exactly one `Verdict:` line and findings as
  `- [SEVERITY] path:line — text`; workflow reads only the verdict and
  severities. A JSON schema was rejected: external APIs ignored it, and a valid
  verdict wrapped in narration then cost a round.
- **Input package.** Base and head, a diff shortstat and a manifest (path and
  SHA-256) of temporary read-only files: `task.md`, `parent.md` for a subtask
  or one `subtask-N.md` per subtask for a parent, the full diff, the changed
  paths, recorded validation, repository instructions and the brief as
  `author-report.md`. Checkmarks are cleared; comments, history and earlier
  verdicts are never included. Each description is headed with the task's
  number, ID and URL. The prompt states the round's own boundary — read-only,
  unable to run the suite — so recorded validation is read as the only run.
  Artifacts are removed once the result is recorded or the managed run fails;
  oversized index metadata fails closed.
- **What the reviewer judges.** Only what the repository shows. Requirements
  about tracker state or operator actions are closed by the coordinator and
  listed as limitations, never findings or INCONCLUSIVE. A reviewer that
  judged tracker claims from the brief was rejected: the same change passed or
  stayed INCONCLUSIVE depending on the brief's wording. Complexity findings
  need a concrete present cost and a simpler behaviour-preserving alternative;
  style and hypothetical futures do not block PASS.
- **Acceptance belongs to code.** Validation, review and human acceptance are
  recorded for the head and base commits, and the pair is stored with them: it
  still names the original diff after the base fast-forwards past the head,
  where Git can no longer derive the fork point. A new commit or moved base
  retires all three; task edits retire nothing. Evidence is a disposable local
  cache; another machine may need to validate and review again.
- **Next step.** `workflow inspect` derives it from what is recorded for this
  head, not from the shape of the cycle. `scope` is null until the expected
  task branch is clean.
- **Paths.** `sensitive_paths` and `ignored_paths` are `fnmatch` globs on the
  repository-relative path, also tried with a leading `/`; `*` matches across
  `/`. A rename leaves the counts only when both names are ignored; a path in
  both lists counts and triggers review. Ignoring never changes `always`,
  labels, scope, validation or the diff a reviewer reads.
- **Incremental economy.** With `review.incremental` set, the triggers run on
  the delta from the task's own latest applied round (PASS, or NEEDS_WORK with
  no finding above the level) to the current head. A delta that triggers
  nothing is accepted by that round's threshold, never a PASS. The delta always
  starts at the reviewed head, so small steps add up. A parent's first review
  is always of its whole diff; subtask rounds never shorten it. Stale,
  superseded or INCONCLUSIVE rounds, expired results, non-descending history
  and already-integrated parents use the whole diff.
- **Budget and failures.** `max_rounds` counts received verdicts. Known
  launch constraints are checked first and open no round. Failures (killed
  process, provider failure or timeout, an unanswered native reviewer, no
  verdict line) spend no attempt; every round start counts as a failure until
  its verdict clears it, so an abandoned run still counts. Two in a row stop
  review until the user decides. Budget state is keyed by checkout, tracker
  provider, server, project and task, survives scope changes and restarts, and
  never resets on its own; `review-budget --limit TOTAL` sets an absolute
  ceiling and always clears the failure count.
- **Locks.** Every new round retires earlier PASS and human acceptance under
  the budget lock, even if it later fails. Result ingestion accepts only the
  latest pending round, so late or replayed results cannot restore acceptance.
  Finish and landing consume freshly reread evidence under that lock.
- **Human acceptance** (`approve`) records head, actor and reason; it is a
  coordinator contract, not authentication of the human.
- **Usage counters.** Codex counters, including cache-write input and
  reasoning output tokens, come from JSONL; Claude's from the result envelope.
  A CLI's own cost figure is not kept: for an external API it is an estimate,
  not the bill.

## Model routing

- A candidate is unavailable when its CLI is missing or, for a subscription
  model, a known usage window is exhausted with a future reset. A failed live
  refresh keeps cached exhaustion evidence and exits nonzero; a failure without
  such evidence excludes nothing. Provider APIs may lag regardless of cache.
- A managed reviewer advances to the next candidate only after observed
  unavailability, authentication, quota or `timeout_seconds` in the same round.
  Unknown errors, unsupported settings and answers without a verdict stop the
  run. Speed, cost, preference, silence before timeout and verdicts never
  justify switching. An unsupported model is an error, not a reason to guess.
- `other_vendor_first` orders candidates by vendor: an external API name, else
  the CLI. Workflow does not track commit authorship, so the coordinator names
  authors with `--author`; without it inspect reports `authors-required` and a
  managed review refuses. Pins, cloud reviewers and empty lists are not
  reordered.
- `config apply` installs native profiles with exact model and effort per CLI
  and role (`ab-scout`, `ab-worker`, `ab-sync`, `ab-reviewer`, and
  `ab-worker-N`, `ab-sync-N`, `ab-reviewer-N` for nonempty tiers), each from
  that CLI's first built-in-API candidate. Profiles agent-box no longer defines
  are removed while unedited. A profile is a convenience, not proof of the live
  tool's capabilities. Effort `default` omits the override; it is not a literal
  provider value.

## Local landing

- `land merge` verifies that the local base is an ancestor of the validated
  head before switching branches, then fast-forwards to that exact commit.
- Publication pushes only the accepted base head to the unique effective
  origin, after checking the live default branch and fast-forward ancestry,
  with force, mirror, tag and submodule side pushes disabled. Unknown or
  diverged remote heads and server rejection stop completion. Afterwards only
  the remote-tracking ref selected by origin's fetch refspec is updated, using
  its earlier value as a compare-and-swap guard; fetch and push URLs must match
  after rewrites, and unsuitable mappings are skipped. `publication.tracking`
  reports `updated`, `skipped` or `failed`; a tracking failure never repeats
  the push, blocks Done or touches another branch.
- The command is idempotent: a failure keeps the merge and branch and leaves
  the task In review, and rerunning continues from the failed step. A head the
  base already contains is not merged twice; anything but the accepted head is
  never published. A retry proves acceptance from the two recorded commits, so
  the current policy applies to the original diff rather than the empty one.
  When the base already contains the head, only an independent PASS or explicit
  human acceptance of the complete implementation can cover it; unclear scope
  is INCONCLUSIVE.

## GitHub landing

- Pull requests are found by head branch and base, never stored: an open one
  wins, a single merged one answers a repeated merge, several stop the command.
- Completion from a merged PR requires its merged head to equal the branch's
  current head, since squash and rebase merges rewrite commits and head
  equality is the only honest test.
- A merged PR proves integration, not acceptance. A human `APPROVED` on the
  current head counts only when observed before the merge, read from the
  reviews endpoint, which alone reports the reviewed commit and whether the
  account is a person: an app's approval is not acceptance, or a cloud
  reviewer could accept work by pressing a button instead of answering.
- Branch protection is never bypassed; merge-queue entry is not completion.

## Branch cleanup

- Candidates come from each task's namespace in Git (and live origin refs for
  remote cleanup), never guessed from titles. Ambiguous names, symbolic refs,
  the base, the current branch and branches used by any worktree — including
  the remote copy of a locally checked-out branch — are preserved with a
  diagnostic. Cleanup never switches branches.
- Unfinished descendants get read-only discovery: `preserved`, `absent`, or
  `error` with candidates. Hierarchy read failures appear in
  `cleanup.descendants_error`.
- **Proof.** GitHub mode needs one exact merged PR with merge timestamp and
  commit; the parent tip must equal the merged head and descendant heads must be
  its ancestors, so squashing the parent does not erase their proof. A missing
  parent ref can recover proof from a unique merged PR in its namespace. Local
  mode needs every head to be an ancestor of the local base. Remote deletion
  also needs the head in the live published base; an unpublished merge or
  missing object preserves the remote branch. Done alone is never merge
  evidence.
- **Deletion** compares the expected head: `git update-ref --no-deref -d REF
  EXPECTED` locally and an exact `--force-with-lease=REF:EXPECTED` with one
  deletion refspec remotely. The push URL must be unique and unchanged by
  chained rewrites; tag following, submodule and mirror pushes are disabled for
  that command. Worktree occupancy is rechecked just before deletion.

## Cloud review in the pull request

- On a head with no request, `workflow review PARENT` publishes one comment
  asking `@codex review` or `@claude review` for that exact commit; later runs
  read the answer back. A request in the thread counts only when its text is
  one this checkout published for this scope and reviewer, word for word,
  because anyone may comment. It offers no example verdict, since examples get
  copied back. A round whose own request is not listed yet publishes no second
  one; the wait budget ends it if the request never appears.
- The request quotes the task descriptions and the brief, because the bot reads
  only this comment and a PR body is the author's summary, not the
  requirements. Quoted mentions are defused, so a card naming `@claude` cannot
  start a second, paid review. A request over GitHub's comment limit is refused
  before publishing.
- Pins the bot cannot honour are refused first: `github_model`/`github_effort`
  for Codex, and for Claude any value not already in the `claude_args` of the
  workflow step that uses the Claude Action.
- **Claude's answer**: a `claude[bot]` comment ending in `Reviewed-Commit`,
  `Reviewed-Base` and one `Verdict:` line, findings in the reviewer form.
  Progress checklist lines are not findings. An answer echoing no base, or
  about this head without a single verdict, ends the round at once.
- **Codex's answer**: a COMMENTED review bound to the commit with priority
  badges (P0 → CRITICAL … P3 → LOW), otherwise a comment naming the reviewed
  commit; nothing attached means PASS. Only what it attached after the request
  counts; its prose and 👍 reaction are never parsed. Because Codex names only
  the commit, a head already asked about another base cannot be asked again.
- Only answers published after the request and naming this commit count.
  Silence is never acceptance; past `wait_seconds` the round fails without
  spending an attempt.
- Live verification on 2026-09-17 read both bots' answers on a throwaway pull
  request and recorded a verdict end to end. The `Reviewed-Base` line was added
  later and has not been observed live; publishing and the timeout are covered
  by tests only, because the bots answer for real money.

## Saved results and failure artifacts

- Completed native and managed reviews save a private immutable receipt before
  input cleanup or applicability checks, so `review-result --reconcile` can
  apply it later without a model call. Applied rounds never apply twice;
  changed code, a moved base or a newer round keep the answer but cannot accept
  work, even after old refs are restored. A missing first budget-index write is
  recovered by task and round identity after checking the receipt's `.seal`; an
  unavailable budget stays unknown and blocks application. A receipt is
  operational evidence, not an unforgeable credential.
- Receipts live in `WORKFLOW_STATE/review-results` (directories 0700, files
  0400), at most 20 per checkout, 32 MiB total, 8 MiB each and seven days.
  Pruning removes only verified owned files.
- Raw managed reviewer streams go to
  `review-results/<checkout>/streams/<task>/<round>/<run>`, read-only and cut to
  8 MiB once finished, removed when the task is Done and otherwise bounded like
  receipts. A failed removal is reported as `review_streams_error` and leaves
  the task Done.
- Any failed managed run, workers and scouts included, writes
  `WORKFLOW_STATE/provider-failures/<id>.json` (0700/0600) with safe metadata and
  base64 prefixes: up to 1 MiB each of stdout and the final result file and
  64 KiB of stderr, from responses limited to 4 MiB. Prompts and commands are
  not kept. At most 20 artifacts (about 55 MiB); older than seven days or
  excess files are removed when the next one is saved.

## Model speed

- `standard` and `fast` always use managed CLIs. Codex 0.155.1 overwrites a
  native child's service tier with the root session's after applying its
  profile, and Claude native profiles inherit speed, so a profile cannot pin
  speed. `config apply` installs native profiles only for `inherit`.
- Managed Codex passes `service_tier="default"` with `features.fast_mode=false`
  for standard (Codex omits the tier on the wire) and `service_tier="fast"` with
  `features.fast_mode=true` for fast, keeping `--ignore-user-config`; Claude
  passes `--settings '{"fastMode":false|true}'`.
- Fast is accepted only for known models: Codex `gpt-6-astra`, `gpt-5.6-sol`,
  `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.6`, `gpt-5.5`, `gpt-5.4`; Claude
  `claude-opus-5`, `claude-opus-4-8`. Unknown models are refused to avoid a
  silent switch; Claude Fast on third-party clouds is unsupported. External
  API candidates support `inherit` only.
- Sources checked 2026-09-22:
  [Codex schema](https://learn.chatgpt.com/docs/config-schema.json),
  [Codex 0.155.1 native spawn](https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/core/src/agent/control/spawn.rs),
  [Codex speed](https://learn.chatgpt.com/docs/agent-configuration/speed),
  [Claude Fast mode](https://code.claude.com/docs/en/fast-mode). Tests prove
  configuration and request construction, not entitlement, billing or
  delivered acceleration.
