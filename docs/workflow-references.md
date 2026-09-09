# Workflow design references

Research and implementation decisions were checked on 2026-09-05. The code stays
Python standard library only. Existing agent-box Git guards, HTTP transport,
imports, atomic writes, path policy, usage caches and ownership handling were
reused rather than introducing overlapping implementations.

## Libraries and provider interfaces

- [pyvikunja 0.24.0](https://pypi.org/project/pyvikunja/) uses the older API v1
  interface and adds an HTTP client dependency. [vikunja-python
  0.1.2](https://pypi.org/project/vikunja-python/) also targets v1. Neither replaces
  the already working native v2 transport/import code in this repository.
- Python's [curses](https://docs.python.org/3/library/curses.html) supplies the
  configuration menu without a dependency environment or installation utility.
  A Python build without curses can use the JSON file and `config apply`.
- [Vikunja API](https://vikunja.io/docs/api-documentation/) and the connected
  instance's `/api/v2/openapi.json` define task/assignee/bucket endpoints. The live server returns ETags on GET
  but ignores If-Match on task PATCH; the adapter uses explicit optimistic
  snapshot checks and does not advertise atomic conditional updates. Live isolated project tests check actual behavior,
  including Markdown round trips and cooperative claims.
- [Codex subagents](https://developers.openai.com/codex/subagents) and
  [Claude subagents](https://code.claude.com/docs/en/sub-agents) inform generated
  native profiles. Actual installed `codex exec --help`, `claude --help`, and
  `gh pr view/merge --help` define CLI arguments; unsupported selections fail
  rather than silently substituting a different model.
- [Claude GitHub Action usage](https://github.com/anthropics/claude-code-action/blob/main/docs/usage.md)
  documents `claude_args` model/effort settings. Cloud mention interfaces are
  distinguished from exact local CLI model selection.
- Weighted round robin supplies the deterministic static schedule. The
  usage-aware score is a configurable budget-pacing heuristic agreed for this
  workflow; subscription weights are user-supplied capacity estimates.

## Workflow sources

Branch cleanup was checked against Git 2.43.0 in temporary repositories on
2026-09-08. [git-update-ref](https://git-scm.com/docs/git-update-ref) documents
expected-old-OID deletion; [git-push](https://git-scm.com/docs/git-push) documents
the explicit ref/OID lease used only for remote deletion. These comparisons do
not check branch occupancy: [git-branch](https://git-scm.com/docs/git-branch) and
`git worktree list --porcelain` inform the separate worktree guard. Cleanup uses
no merge registry and is limited to the completed parent's branch.

These local reference checkouts informed the design. The shipped skill prose is
maintained here, not periodically patched or fetched from upstream.

| Reference | Inspected revision | Reused ideas |
| --- | --- | --- |
| Flow-Next | `65646d7514726140f89ad9ecf6a13cbc741de99d` | Targeted scouts, concise evidence briefs, independent review, grouping PR feedback, bounded land convergence |
| obra/superpowers | `b36e0829c6d0140e93cfef2ca599b1b07d4a7797` | Clarify material uncertainty, investigate root causes, useful failing regression tests, verify before claiming completion |
| Matt Pocock skills / Grill Me | `3cca18b368ae95cdbdebbff572ccafa662551015` | One decision at a time, dependency order, concrete options, test uncertain assumptions against facts and scenarios |
| OpenAI Symphony | `8001b52e3062495a16e520e4ceaf8f9de868c4d0` | Explicit tracker boundary and external task state |

No upstream bootstrap, task database, vendor updater or mandatory microtask
ceremony is shipped. The reference licenses are retained in
[third-party notices](third-party-notices.md) for the workflow adaptations.

## Delegation context evidence (2026-09-09)

Task 335 checked tool schemas and installed CLI help separately from mocked
launch and prompt-composition tests; those tests do not attest provider behavior.
The coordinator then ran six fresh managed Codex no-op probes in a temporary Git
repository through the current `render_handoff`, `report_schema` and
`build_command` paths, using `gpt-5.6-sol` at medium effort: two sequential
assignments each for worker, scout and reviewer. All six exited zero, returned
their current marker, and emitted `thread.started` with distinct IDs and
`turn.completed`. Each role's second invocation reported its predecessor's marker
absent from initial input. These exposed events and marker reports support
separate executions, not verification of the complete hidden initial context.
Reviewer reports deliberately used `INCONCLUSIVE` as diagnostics, never acceptance;
the first correctly reported the literal sentinel `NONE` present in its question.
Detailed probe paths and results belong in the tracker journal, not committed logs.

| Interface | Observed launch contract | Limits |
| --- | --- | --- |
| Codex native `spawn_agent` | The exposed schema defaults `fork_turns` to `all`; explicit `none` requests no surrounding conversation and is supported by actual call arguments. New assignments use a new spawn. | Fresh creation returned `agent thread limit reached`, so no new native matrix ran. Task 328 remains the preserved negative incident for inherited `fork_turns="all"`. Schema and call arguments do not attest hidden initial context; profiles only describe routing. |
| Claude native | No native Claude tool was exposed in this session. | Freshness, inheritance defaults and exact routing are unconfirmed here; use managed fallback unless the active tool verifies the equivalent contract. |
| Codex managed | Installed `codex exec --help` exposes a new exec entrypoint, separate resume/fork subcommands, `--ephemeral` and `--ignore-user-config`. The runner uses exec, never resume/fork. | Ephemeral means no persisted session files; it alone does not prove an empty context. JSONL turn counters are optional numeric diagnostics. |
| Claude managed | Installed `claude --help` exposes `-p`, `--no-session-persistence`, and separate continue/resume controls. The runner supplies neither continue nor resume; `--safe-mode` excludes customizations. | The actual task 335 review attempt hit quota before review execution; context behavior is unverified and the attempt was not repeated for counters. `--fork-session` accompanies resume/continue, not fresh input. Numeric usage/cost comes from the result envelope when available. |

The shared English assignment and mandatory repository evidence are the input
contract for both backends. Original-language tracker requirements may be labeled
source evidence; parent transcripts, comments and previous-agent history are absent
by default. Complete instructions must be read, including imports; read failures
and truncation are explicit limitations. A new scope/hash or coordinator assertion
does not independently verify context. Missing full-context receipts alone do not
disqualify native. Matched before/after input measurement belongs to task 348;
bytes and words must not be reported as exact tokens.

## Workflow input audit (2026-09-09)

Task 348 audited all nine shipped skills plus repository binding, native profiles,
prepare output and workflow documentation. Route mechanics now come from the
actual `workflow prepare` result instead of being copied into plan/work/review.
An exact task ID no longer requires `tracker ready`; `config show` is diagnostic;
library/version research runs only for a material dependency choice. Task,
repository and applicable rule reads remain mandatory. Interview questions test
uncertain assumptions against facts and concrete scenarios and stop once only
safe implementation choices remain. The adoption and landing recovery rules were
retained because they guard real provider, ref and user-data transitions.

The matched probe used frozen source `e450d5e` and was rerun against `c4e8894`
on 2026-09-09; all After values below were reproduced unchanged. It runs in separate
isolated processes with the same synthetic repository, task, brief, routes and
two-review change/fix sequence. It counted UTF-8 bytes and whitespace-separated
words delivered across the invoked skill, coordinator prepare/result, native
profile, delegate handoff and every mandatory reviewer artifact. Temporary paths
were normalized before counting. Provider execution was mocked only to capture
the actual composed input; this is input measurement, not runtime or token proof.
Repository instructions come from the synthetic fixture, not this checkout's
AGENTS.md. These totals therefore measure the matched workflow paths, not the
complete input of a real delegation or later edits to repository documentation.

| Matched mandatory input | Before bytes / words | After bytes / words |
| --- | ---: | ---: |
| All nine skill files (inventory, not one invocation) | 37,095 / 5,093 | 31,657 / 4,429 |
| Exact-ID untiered direct worker | 9,612 / 1,188 | 7,373 / 864 |
| Tiered managed worker | 8,960 / 1,172 | 7,408 / 920 |
| Tiered native worker | 10,826 / 1,351 | 14,160 / 1,624 |
| Two fresh native review rounds around a fix | 47,492 / 4,029 | 53,718 / 4,510 |

The native rows grew because the old path omitted generated complete worker
handoff data and silently truncated ordinary repository instructions; review now
indexes complete immutable requirements, diff, validation and instruction
artifacts. Removing those bytes would restore the defect. The managed worker and
direct paths shrink because compact skills and prepare task identity remove real
duplication. These byte/word counts are not model tokens. Real successful managed
usage is reported separately when the provider exposes it; unavailable native or
managed usage remains null.

For an exact-ID task with no dependency choice, the documented coordinator CLI
sequence drops from six commands (`get`, `ready`, `config show`, `inspect`,
`begin`, `prepare`) to four (`get`, `inspect`, `begin`, `prepare`). Task selection
still uses `ready`, and configuration diagnosis still uses `config show`. A
prepared native-to-managed review fallback remains two commands but now consumes
one logical attempt instead of two and reuses the same handoff. Two verdicts
around a fix still consume two attempts and two fresh contexts. Re-reading
`workflow inspect` after compaction consumes no attempt.
