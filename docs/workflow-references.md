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
| Matt Pocock skills / Grill Me | `3cca18b368ae95cdbdebbff572ccafa662551015` | One decision at a time, dependency order, concrete options, challenge assumptions |
| OpenAI Symphony | `8001b52e3062495a16e520e4ceaf8f9de868c4d0` | Explicit tracker boundary and external task state |

No upstream bootstrap, task database, vendor updater or mandatory microtask
ceremony is shipped. The reference licenses are retained in
[third-party notices](third-party-notices.md) for the workflow adaptations.

## Delegation context evidence (2026-09-09)

The task 335 implementation checked the available tool schema and installed CLI
help separately from prompt-composition tests. No new live provider executions
were needed; tests do not attest external model behavior.

| Interface | Observed launch contract | Limits |
| --- | --- | --- |
| Codex native `spawn_agent` | The exposed schema defaults `fork_turns` to `all`; explicit `none` excludes surrounding conversation. New assignments use a new spawn. | Schema and actual call arguments are evidence, not a receipt of hidden initial context. Profiles only describe routing. |
| Claude native | No native Claude tool was exposed in this session. | Freshness, inheritance defaults and exact routing are unconfirmed here; use managed fallback unless the active tool verifies the equivalent contract. |
| Codex managed | Installed `codex exec --help` exposes a new exec entrypoint, separate resume/fork subcommands, `--ephemeral` and `--ignore-user-config`. The runner uses exec, never resume/fork. | Ephemeral means no persisted session files; it alone does not prove an empty context. JSONL turn counters are optional numeric diagnostics. |
| Claude managed | Installed `claude --help` exposes `-p`, `--no-session-persistence`, and separate continue/resume controls. The runner supplies neither continue nor resume; `--safe-mode` excludes customizations. | `--fork-session` accompanies resume/continue and is not an equivalent fresh-input mechanism. Numeric usage/cost comes from the result envelope when available. |

The shared English assignment and mandatory repository evidence are the input
contract for both backends. Original-language tracker requirements may be labeled
source evidence; parent transcripts, comments and previous-agent history are absent
by default. Complete instructions must be read, including imports; read failures
and truncation are explicit limitations. A new scope/hash or coordinator assertion
does not independently verify context. Missing full-context receipts alone do not
disqualify native. Matched before/after input measurement belongs to task 348;
bytes and words must not be reported as exact tokens.
