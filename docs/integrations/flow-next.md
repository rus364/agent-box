# Flow-Next

[Flow-Next](https://github.com/obra/superpowers-marketplace) is a plugin that
runs specs, tasks and reviews inside Claude and Codex. If you do not use it,
none of this applies and nothing on this page is configured.

A workspace is treated as Flow-Next when its root contains:

```text
.flow/config.json
```

agent-box then supplies runtime policy the project would otherwise have to
restate in every `AGENTS.md`. Container mode and `--host` mode share the same
code for all of it, so the two cannot drift.

## Review routing

Nothing is routed until you say so.

```bash
agent-box config
```

offers the backends worth choosing between and stores the answer per primary
agent:

```json
{
  "flow_next": {
    "review_backends": {
      "claude": "codex:gpt-5.6-sol:medium"
    }
  }
}
```

A configured agent gets `FLOW_REVIEW_BACKEND` on every launch. An agent with no
entry gets no variable at all, so Flow-Next resolves the backend from
`.flow/config.json` exactly as it would without agent-box.

There is no shipped default, deliberately: a backend spec names a concrete
provider, model and effort level, and a default would silently point every
Flow-Next workspace on every machine at a model you may not have, may not pay
for, or may not want reviewing your code.

An explicit `FLOW_REVIEW_BACKEND` in the shell wins for that one launch:

```bash
FLOW_REVIEW_BACKEND=codex:gpt-5.6-sol:high agent-box claude
```

Which agent is primary is always explicit — `agent-box claude` or
`agent-box codex`. agent-box never picks between them on quota, cost, or any
other heuristic.

## Flowctl

For Flow-Next workspaces agent-box resolves the actual Flowctl installation
separately for each agent and makes bare `flowctl` available for that
invocation. No global symlink is required.

- **Claude**: read from the installed `flow-next` plugin record in
  `<CLAUDE_HOME>/plugins/installed_plugins.json` — the same record Claude Code
  itself uses to decide `$CLAUDE_PLUGIN_ROOT`, so the answer is identical
  without launching Claude to ask.
- **Codex**: `${CODEX_HOME:-~/.codex}/scripts/flowctl`.

Never a filesystem search and never `shutil.which("flowctl")`: upstream
Flow-Next documents that a global `flowctl` on `PATH` is not guaranteed. When it
cannot be determined, auto-push skips rather than guesses.

## Auto-push

Opt-in and fail-closed. At the end of an agent turn agent-box may run:

```bash
git push -u origin HEAD
```

— but only after proving that the current branch, the remote destination, the
remote default branch, the worktree state, the divergence state and the touched
Flow-Next task receipts are all safe, and that Flow-Next itself reports at least
one touched task as authoritatively `done`.

Any ambiguity skips the push. agent-box never force-pushes, rebases, merges,
resets, changes branches, or guesses a destination.

<details>
<summary><strong>What "safe" actually checks</strong></summary>

The reconciler checks the effective push destination rather than assuming
`origin`, queries the remote live instead of trusting a stale `origin/HEAD`, and
rejects `main`/`master`, detached HEADs, dirty worktrees, divergent remote
branches, ambiguous push URLs, unresolved Flowctl, malformed state, and task
completion that exists only in a committed snapshot rather than in Flow-Next's
runtime state.

It also disables `push.followTags` and `push.recurseSubmodules` for the push
subprocess, so a plain branch push cannot silently publish unrelated tags or
submodule changes.

There is no persistent auto-push marker. Every completion event recomputes the
answer from current Git and Flow-Next state.

</details>

Enable it in `agent-box config`. It defaults off because this must never turn
every agent turn into a git push.
