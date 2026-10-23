# Configuration

## Layers

Settings come from three layers: built-in defaults, your global overrides in
`~/.config/agent-box/config.json`, then the project overrides in the
repository's `.agent-box.json`. Missing settings inherit from the layer
below. A global save keeps only what differs from the defaults; a project file
keeps every value it sets explicitly, even one equal to the global setting. The defaults are defined by the
configuration dataclasses in `agent_box/config.py`; there is no separate JSON
template.

```bash
agent-box config            # global menu: save and apply
agent-box config --project  # the same menu for .agent-box.json
agent-box config show       # effective settings, tokens redacted
agent-box config apply      # apply the saved file without questions
agent-box config check      # verify the Claude Telegram channel setup
agent-box config ai         # set things up together with an agent
```

**Package updates apply automatically.** The first host agent launch after an
update migrates saved settings and refreshes already configured integrations.
Unchanged launches do not reapply them, and rendered sessions do not rewrite
host settings. `agent-box config apply` remains available for manual edits and
retry; `agent-box uninstall` removes owned integrations. See
[compatibility and upgrades](compatibility.md).

## The configuration menu

`agent-box config` is a standard-library curses menu that works down to 40×8
terminals and keeps the terminal's background.

| Key | Action |
| --- | --- |
| Up / Down, Home / End, Page Up / Page Down | Move |
| Space | Toggle or cycle the current value |
| Enter | Open a section, choice list or editor |
| `?` | Full help for the selected item |
| Escape | Go back; in a checkbox list, discard |
| R | Reset the field or section to its default |
| F1 | Keyboard help inside an editor |

Secret values stay masked in editors. **Cancel** writes nothing. **Save and
apply** saves the file and performs the selected changes: installing the skills your integrations need, linking them for Claude,
rendering credential files and reconciling Claude's `settings.json`.
`agent-box config apply` does the same from the saved file, which is what you
want after editing `config.json` by hand. Package upgrades through uv/pipx apply
on the next host launch; PATH and package updates belong to that installer.

**Model lists.** Worker, scout, Plan sync and reviewer models are ordered
candidate lists: add, reorder, change effort or speed, or remove a candidate.
The picker offers Codex, Claude and enabled
[external API](integrations/external-providers.md) models together with manual
model IDs, since any CLI's model may serve any role. The suggestions are hints,
not availability checks.

**Behavior version.** Workflow → Behavior selects `workflow.behavior_version`:
`v1` preserves earlier engineering guidance; `v2` (default) adds impact analysis,
appropriate reuse, maintainability, evidence-based disagreement and investigation
proportionate to risk and effort. Projects may override the global choice.
This setting does not change models, permissions, review frequency or budgets.
`agent-box workflow behavior --role coordinator|worker|reviewer|scout|sync`
reads the effective instructions without a task or tracker connection. New
assignments receive the selected version; existing agent contexts do not reload it.

**Text delivery.** Workflow → Delegation exposes the positive global-only
`workflow.text_max_bytes` (65,536 UTF-8 bytes) and `workflow.text_retention_days`
(7 days). The first limits one complete delivery; the second fixes each new
record's expiry. Oversized notes and managed reports use the configured scout for
one summary; requirements stay exact and diffs are read on demand. See
[text delivery](integrations/workflow.md#text-delivery) for failure, paging and
session-context boundaries.

**Project languages.** Workflow → Project text has two settings:

- **Engineering language**, `workflow.language`: code comments, docstrings,
  documentation, commits, PRs and review replies. Empty defaults to English.
  It also remains the tracker fallback for existing configurations.
- **Tracker language**, `workflow.tracker_language`: task titles, descriptions,
  plans, checklists, journal comments, normalization, adoption and Plan sync.
  Empty inherits `workflow.language`. With both empty, new tasks use your
  interaction language and existing tasks keep their own language.

For Russian tracker work and English engineering, set globally or per project:

```json
{"workflow": {"language": "English", "tracker_language": "Russian"}}
```

Global and project values merge per field before resolving these fallbacks.
Reset a project override to inherit the global value; an explicit empty tracker
override clears the global tracker choice and follows the effective engineering
setting. Each setting accepts one line of at most 40 characters.

`agent-box tracker status` reports the resolved tracker value as `language`.
`agent-box workflow inspect` exposes `configuration.tracker_language` (nullable)
and `configuration.engineering_language` (English by default); the legacy
`configuration.language` retains the configured base value or null.
Generated assignments name the appropriate language for their destination.
Tracker settings also apply without workflow enabled. A rewritten task
description is saved wholly in the tracker language; older comments and
untouched files are not translated. Agents report conflicting repository
language requirements before editing and preserve user-owned instructions.
Conversation with you stays in your language; agent-to-agent handoffs/reports
and agent-box output stay English. These instructions guide agents; they do
not translate text automatically.

**Inside a workspace box** the saved file is not mounted. There
`agent-box config apply` applies the session's rendered configuration — the
shipped skills and the configured agent profiles — and leaves host-only parts
(notifications, hooks, status line, credentials, PATH, Claude's skill links)
alone. The interactive menu refuses in a box.

## Defaults

**Container permissions.** Under **Agents → Claude** and **Agents → Codex**,
**Bypass permissions (container)** sits near Remote Control. Both
`agents.claude.bypass_permissions` and `agents.codex.bypass_permissions`
default to `false`, including when absent from older configuration files.
These global settings apply to new container launches, including external API
sessions. They leave native `--host` permissions and managed workflow role
policy unchanged.

Off leaves the provider's own configuration and your explicit CLI arguments in
effect; it does not select a specific sandbox or approval mode. On adds
`--dangerously-skip-permissions` for Claude sessions, or
`--dangerously-bypass-approvals-and-sandbox` for Codex. Enable either separately
in the menu, or explicitly restore the previous container behavior with:

```bash
agent-box config set --global agents.claude.bypass_permissions=true agents.codex.bypass_permissions=true
agent-box config proposal apply
```

The first command only stages a proposal. Run the second in your own host
terminal, review the changes and confirm to save and apply them.

Codex's remote `/resume` rejects explicit permission overrides, including its
bypass flag. Leave this setting off when resuming through Remote Control, and
avoid explicit permission overrides in the CLI arguments. See
[Remote Control](integrations/remote-control.md#codex) for remaining limits.

| Setting | Default | Purpose |
| --- | --- | --- |
| Default agent | `claude` | What a bare `agent-box` starts |
| Container permission bypass | off for both agents | Keep provider-native permissions unless explicitly enabled |
| Claude status line | on | Workspace, Git state, context and provider usage in one line |
| Git identity | on | `user.name` / `user.email` for commit attribution |
| Provider transport settings | on | Base URL, proxies and CA bundle, so a box behind a proxy reaches the network |
| Claude skill links | on | Link `~/.agents/skills` into `~/.claude/skills` |
| Image tool groups | `analysis`, `kubernetes` | What the [workspace image](image.md) is built with |
| Everything else | off | Credentials, tracker, workflow, browser, k3s, kept nested Podman data, audio, notifications and the other [integrations](integrations/README.md) |

Within the opt-in workflow, auto-push defaults to on.

## Environment variables

| Variable | Effect |
| --- | --- |
| `AGENT_BOX_IMAGE` | Workspace image (default `localhost/agent-box:latest`) |
| `AGENT_BOX_CONFIG_DIR` | Instead of `~/.config/agent-box` |
| `AGENT_BOX_STATE_DIR` | Instead of `~/.local/share/agent-box` |
| `AGENT_BOX_AGENTS_DIR` | Instead of `~/.agents`, the shared agent and skill tree |
| `AGENT_BOX_STATUSLINE=0` | Leave Claude's status line alone when applying |
| `AGENT_BOX_ENV_<NAME>` | Set `<NAME>` inside the session; see [extra environment variables](usage.md#extra-environment-variables) |

Choices for one launch are [session options](usage.md#session-options), not
variables.

## Project configuration

`agent-box config --project` edits `.agent-box.json` at the Git repository or
worktree root; commands from subdirectories use that root too. Outside Git the
current directory is used for launch settings. Tracker and workflow commands
need a complete tracker binding.

The global `agent-box config` menu also has **Project settings**. It opens the
project menu with saved global values. If the global draft has changes, choose
to keep editing or explicitly discard them before switching; save first to
keep those changes. Opening or cancelling the project menu saves nothing.

```json
{
  "tracker": {
    "provider": "github",
    "project": "octo-org/5",
    "recursive": false,
    "connections": {"github": {"url": "https://api.github.com"}}
  },
  "workflow": {"auto_push": false},
  "box": {"browser": {"enabled": true}}
}
```

A project may set:

- `workflow`, except global-only `enabled`, `text_max_bytes` and `text_retention_days` — including each role's
  candidate lists (`models`, and `tiers.N` for workers, Plan sync and
  reviewers), `use_tiers`, `language` and `tracker_language`;
- `box.tools`, `box.k3s`, `box.podman` and `box.persistent_cache`;
- `tracker.provider` and `tracker.connections.<provider>.url`;
- `tracker.project` and `tracker.recursive`, which identify the project and
  exist only here.

Nested objects merge by key and lists replace the inherited list. An explicit
`false`, an empty list or an allowed empty string is a choice, not a gap.
Unknown fields, wrong types and invalid JSON are errors.

**Credentials never go in the repository.** Every agent-box command validates
the project file first and refuses recognized credential fields even when
empty, with `PROJECT_CREDENTIALS_FORBIDDEN`, the file and the field path.
URLs with credentials, queries or fragments are refused too. The token for a
project's tracker comes from your global configuration, whose account must
match the project URL; a project URL never redirects a global token, and
enabling the tracker stays a global decision.

In the project menu values are labelled **Inherited** or **Overridden**.
**R: Inherit global** removes the selected override so the value follows the
global setting again; **G: Copy current global** pins the current global value
as a project override. Credentials, environment, integration switches and host
installation settings are **Global only** there. **Save project** writes only
explicit project values and applies nothing immediately. Image tool changes trigger
a cached build before the next new workspace; stop/relaunch existing workspaces
explicitly, or run `agent-box build` to prepare the image ahead of time.

## Session tracker override

For one session, `--tracker URL` selects another Vikunja server and
`--project ID` another project; together they bind even an unbound repository.
Both need the tracker enabled in `agent-box config`. The token is read by name
from `VIKUNJA_TOKEN` in the launching shell, never from an argument, and
reaches the session only through its private snapshot:

```bash
export VIKUNJA_TOKEN=...
agent-box --tracker https://tasks.example.org --project 12 codex
```

`--tracker` requires Vikunja selected globally or in the project; `--project` alone works with
either provider. Neither changes a configuration file.

**Session snapshots.** Every launch renders an immutable snapshot of its
settings into agent-box's runtime state. A box mounts only its own workspace's
snapshots, and each consumer applies the current project's overrides to the
snapshot's global baseline, so another project, session or a global apply
cannot change an active session's settings or credentials. With tracker and
workflow disabled, only a policy directory is mounted; it holds no tracker or
GitHub account, but the keys of enabled
[external API providers](integrations/external-providers.md) are rendered
there for the sessions of that box.

## Guided setup with `config ai`

`agent-box config ai [claude|codex]` starts an interactive session in this
workspace's box whose first prompt is the shipped `ab-config` skill. The agent
reads the repository and `agent-box config describe`, asks about your goals in
your language, and explains each recommendation: its purpose, consequences,
alternatives, and whether it belongs to the project or your global
configuration. Starting it installs and applies nothing.

The agent changes settings only through checked commands, after showing you
the change:

```bash
agent-box config describe [PATH]    # every setting as JSON, secrets redacted
agent-box config set --project [--dry-run] PATH=JSON... [--reset PATH]...
agent-box config set --global [--dry-run] PATH=JSON... [--reset PATH]...
agent-box config proposal [apply | discard]
```

Project changes are validated as a whole `.agent-box.json` and saved with a
diff. An agent never writes global settings: `config set --global` saves a
proposal in the checkout's `.git/agent-box/`. You review it with
`agent-box config proposal apply` in your own host terminal, which rechecks it
against the saved configuration, shows the changes and applies them only after
you answer yes. `config ai` offers a pending proposal before the session and
again when it ends or detaches. Credentials, tokens, API keys, environment and
installation settings are never proposed this way; set them in
`agent-box config`.

## Networking

**Workspace box → Network** groups workspace and k3s publications, selected
host services and mode. The k3s list remains `box.k3s.ports`. See
[workspace networking](networking.md) for settings, scopes and launch overrides.
