# agent-box — notes for agents working on this repository

## What this is

A launcher. `agent-box` runs Claude Code and Codex CLI inside rootless Podman
workspace boxes. Pure Python standard library, no dependencies, no build step.

```text
agent-box              launcher shim -> agent_box.cli:main
agent_box/cli.py       argument parsing and command dispatch
agent_box/config_menu.py stdlib curses configuration menu
agent_box/tracker/     portable task contract and product adapters
agent_box/workflow.py  deterministic workflow boundaries; skills orchestrate
agent_box/runtime.py   container lifecycle, mounts, launches, doctor
agent_box/config.py    the whole config schema; flat dataclasses, no framework
agent_box/paths.py     every host-side path; nothing else may hardcode a home layout
agent_box/pathsetup.py the PATH symlink and the shell rc block, and their refusals
agent_box/selfupdate.py `agent-box upgrade`: git operations on this checkout
agent_box/skills/      skills agent-box ships and installs (owned, checked in)
Containerfile          the workspace image, with optional tool groups
tests/                 unittest, no plugins
```

## Rules that are not negotiable

**Pre-release: no migrations.** There is no installed base. Never add a
compatibility shim, a legacy fallback, a "read the old location if the new one
is missing" branch, or migration prose. Reshape things in place and drop the old
form. `config.py`'s `version` stays `1`.

**Paths come from `paths.py`.** Use `HOST_HOME` (passwd-derived), never
`Path.home()`, `os.path.expanduser`, or `$HOME`. A poisoned environment must not
be able to move a bind mount, a state directory or the container's home.

**Off by default.** Anything that exposes a credential or widens what an agent
can reach starts disabled. The exceptions are documented in the dataclass that
defines them, with the reason.

**Applying is not launching.** Only `agent-box config` (including `config
apply`), `agent-box upgrade` and `agent-box uninstall` may write into
`~/.claude` or `~/.agents`. Launching an
agent reads configuration and changes nothing the user owns. `runtime.apply_config`
is the single place that applies.

**Adding to a user's file is not removing from it.** agent-box may append its
marked block to `~/.bashrc`; it removes one only under `--purge`, and only when
the contents between the markers still match, character for character, the
line it generates. An edited block belongs to the user by then. Every case it
will not touch is reported with a file and a line range instead. The same
asymmetry governs the PATH symlink: a name that is not a symlink into an
agent-box checkout is never replaced.

**agent-box owns its integrations.** An integration configured somewhere else,
that the user has to assemble first, is not an integration. Settings live in
`config.json`; skills are shipped in `agent_box/skills/` and installed by
agent-box; the container gets a rendered, minimal artifact, never `config.json`
itself.

**Derived, not stored.** A fact that Git, GitHub or the tracker already owns is
read back from there. Branch names come from the task's `ab/<id>-` namespace,
the base branch from origin's default, the pull request from a head/base lookup,
the lifecycle from the board's own columns. `.agent-box.json` names the tracker
project and nothing else. Nothing machine-readable is written into a tracker
comment: a comment is a human journal, and an agent allowed to write one must not
thereby reach orchestrator state. A delegate runs as the same user, so anything
in `WORKFLOW_STATE` is writable by it: whatever must not be forgeable travels
with the orchestrator instead, as `workflow prepare`'s handoff token does.

**Secrets travel by name.** `--env=NAME` (Podman inherits the value) for
anything secret; `--env=NAME=VALUE` puts the value in an argv that `/proc`
exposes to other processes. `notify.sync_credentials` and
`runtime.sync_tracker_runtime` are the pattern for rendered credential files.

**Never overwrite what a user owns.** Skill directories carry a marker; ones
without it are the user's. Claude's `statusLine` slot is filled only when empty.
Hook removal only ever touches agent-box's own commands.

## Working in the code

- **Comments explain why, not what.** The existing docstrings state the decision
  and the alternative that was rejected. Match that; do not add narration.
- **Tests are the specification.** Every test's name and docstring says what
  behaviour is being protected and why it would be wrong otherwise. A test that
  restates the implementation is noise.
- **Structural vs per-exec.** Anything in `container_lifetime_args` is part of
  the box's identity: changing it recreates the box. Per-invocation values go
  through `invocation_env`. Choose deliberately.
- **Diagnostics are returned, not printed**, so both the launch path and
  `agent-box config` can render them (`tracker_warnings` is the pattern).

## Commands

```bash
python3 -m unittest discover -s tests    # full suite (no external network)
./agent-box selftest                     # fast, no Podman
uvx ruff check --select E4,E9,F agent_box/ tests/   # what CI lints
./agent-box config show                  # inspect the live config, tokens redacted
```

Test modules import each other by bare name (`from test_runtime import ...`), so
run them through `discover`, not `python3 -m unittest tests.test_browser`.

## Gotchas

- `tests/` must never touch the developer's real `~/.claude` or `~/.agents`.
  Anything that calls `config_interactive` or `apply_config` has to patch
  `runtime.CLAUDE_HOME`, `runtime.CODEX_HOME`, `runtime.AGENT_SKILLS_DIR`, `runtime.MANAGED_SKILLS_SOURCE`
  and the credential paths. This has bitten before.
- The same applies to PATH, and more sharply: `config_interactive` may apply menu-selected PATH changes, and `uninstall_main`
  scans the real PATH for symlinks to remove. Tests must stub the menu/apply
  boundary or use a temporary selected bin directory, and patch
  `pathsetup.path_entries`, `pathsetup.DEFAULT_BIN_DIR`, `pathsetup.rc_file`
  and `pathsetup.checkout_root` — otherwise the suite deletes the developer's
  own `agent-box` symlink or edits their `~/.bashrc`.
- Module-level constants derived from `paths.py` (`AGENTS_DIR`, `TRACKER_RUNTIME_DIR`,
  `CONTAINER_TRACKER_RUNTIME_DIR`) are resolved at import; tests patch them on `runtime`,
  not on `paths`.
- The image label `io.agent-box.tools` records the built tool groups. A missing
  label means *unknown*, not *empty* — never report it as drift.
- `agent_box/skills/` contains short owned `ab-*` skills. Maintain them directly
  and keep their command/role contracts consistent with the Python helpers.
- `.gitignore` ignores `/skills/` at the repo root only — `agent_box/skills/` is
  checked in on purpose.
