<div align="center">

# agent-box

**Run Claude Code and Codex CLI in isolated, reusable Podman workspaces.**

One project. One shared container. Your agents, shell, tools, state, and detachable sessions stay together without giving the container your whole host.

[![CI](https://github.com/rus364/agent-box/actions/workflows/ci.yml/badge.svg)](https://github.com/rus364/agent-box/actions/workflows/ci.yml)
![Linux](https://img.shields.io/badge/platform-Linux-informational)
![Podman](https://img.shields.io/badge/runtime-Podman-892CA0)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![License](https://img.shields.io/badge/license-MIT-blue)

</div>

---

## Why agent-box?

AI coding agents are useful, but running them directly on your workstation gives
them the same filesystem, credentials, tooling and process access that you have.

`agent-box` puts Claude Code and Codex CLI inside a transient rootless Podman
container while keeping the workflow close to local development:

- **Workspace isolation** — only the resolved project root and explicitly requested mounts are exposed.
- **Shared workspace box** — Claude, Codex and `agent-box shell` run in the same container for a project.
- **Same paths as the host** — a project at `/home/you/code/project` is at `/home/you/code/project` inside the box too.
- **Persistent agent state** — authentication, settings and history stay in host-backed `~/.claude` and `~/.codex`.
- **Detachable sessions** — interactive sessions run under tmux and survive a closed terminal or a dropped SSH connection.
- **Nothing exposed by accident** — every credential, every host directory and every optional capability is a switch you turned on.

> [!IMPORTANT]
> A workspace box is one trust boundary. Claude, Codex, shells, hooks and
> daemons inside the same box share the same Unix user and PID namespace.

## Installation

Requirements: Linux, Python 3.11+, Podman and Git. `gh` is optional, and only
for passing a GitHub token into the box. The launcher itself uses only the
Python standard library — there is nothing to build and nothing to install into
Python.

Git is a real dependency, not just how you get the code: agent-box asks it for
the repository root to decide what to mount, and reads it for the status line
and the auto-push safety checks.

**1. Clone it.** Anywhere you like; this is the whole program.

```bash
git clone https://github.com/rus364/agent-box.git ~/.local/src/agent-box
```

**2. Configure it.** Run it by path the first time — its first question is
putting `agent-box` on your PATH:

```bash
~/.local/src/agent-box/agent-box config
```

It offers the writable directories your PATH already has, or creates
`~/.local/bin` and adds it. While your shell cannot find `agent-box`, the menu
opens on that question, says so in the row itself, and asks once more before
saving without it. The symlink and the shell startup line are two separate
questions, both declinable, and declining either prints the exact command to
run yourself. The directory you pick is saved, so if the symlink later
disappears — a `~/.local/bin` rebuilt by another installer, a restored home —
`agent-box config apply` puts it back. Everything after that is optional
integrations, all off by default.

**3. Authenticate the agents on the host.** agent-box mounts your existing
`~/.claude` and `~/.codex` rather than managing a second credential store, so
`claude` and `codex` should be logged in before the first launch.

**4. Run it.** The container image is built automatically on first use, which
takes a few minutes once:

```bash
cd ~/code/my-project
agent-box claude          # or: agent-box codex
```

Then, day to day:

```bash
agent-box shell       # shell in this project's box
agent-box attach      # reattach a detached session
agent-box stop        # end the box and every session in it
agent-box doctor      # verify the environment
agent-box upgrade     # move this checkout to the newest release
```

## How it works

```mermaid
flowchart LR
    Host["Host workspace"] --> Launcher["agent-box"]
    Launcher --> Box["Rootless Podman workspace box"]

    Box --> Claude["Claude Code"]
    Box --> Codex["Codex CLI"]
    Box --> Shell["Shell"]

    Claude --> Tmux["tmux sessions"]
    Codex --> Tmux
    Shell --> Tmux

    State["~/.claude / ~/.codex"] -. host-backed state .-> Box
    Project["Project root\nsame absolute path"] -. explicit bind mount .-> Box
```

The first command in a project creates its box lazily. Later Claude, Codex and
shell commands for the same workspace join that box with `podman exec`. A
different workspace gets a different box. When no useful workload remains, the
box exits and Podman's `--rm` removes it.

## Daily usage

```bash
agent-box                  # default agent: Claude
agent-box claude -p "summarize this repository"
agent-box codex exec "inspect this repository"

export AGENT_BOX_AGENT=codex     # change the default
```

### Detach and reattach

Interactive `claude`, `codex` and `shell` sessions run under tmux inside the
box, so closing the terminal detaches instead of orphaning. Detach on purpose
with `Ctrl-\ d`, come back with `agent-box attach`, and name a session
explicitly when several exist:

```bash
agent-box attach claude-31337
```

A detached session keeps its box alive; `agent-box stop` ends the box and every
session in it.

The mouse wheel scrolls tmux history when the agent does not handle mouse
events itself; mouse-aware TUIs receive the events directly. Press `q` to leave
tmux's scroll mode. This uses agent-box's private tmux configuration.
For an already running server, apply the setting from `agent-box shell` with
`tmux -L agent-box set -g mouse on`; new servers load it automatically.

Only real terminals are wrapped — pipes, redirects, CI and `claude -p` write
straight to stdout. `AGENT_BOX_TMUX=0` disables tmux for one launch.

### Extra mounts

The project root is the only workspace path mounted automatically:

```bash
agent-box --mount ~/code claude
agent-box --mount /mnt/data/specs:ro --mount /srv/generated codex
```

Mounts keep their host absolute paths inside the container, and are read-write
unless you append `:ro`.

### Extra environment variables

```bash
agent-box --env DATABASE_URL=postgres://localhost/dev codex
agent-box --env MY_TOKEN claude
```

`--env NAME=VALUE` sets a variable. `--env NAME` forwards the value that name
already has in your shell *without* putting it in the process list, which is the
form to use for anything secret. Both are repeatable and both win over the lists
stored in `agent-box config`.

The same request works as a shell prefix:

```bash
AGENT_BOX_ENV_DATABASE_URL=postgres://localhost/dev agent-box codex
```

A bare `DATABASE_URL=... agent-box codex` cannot work, and the namespace is why:
inside the process, a variable set by a shell prefix is indistinguishable from
the hundreds inherited from your login profile, so honouring bare names would
mean handing the container your entire host environment.

### Host mode

```bash
agent-box --host claude
agent-box --host codex
agent-box --host attach       # return to a host session in this project
agent-box --host stop         # end this project's agent-box host sessions
```

Runs the installed CLI directly, with your normal host environment and
credentials — no Podman. Tracker commands, task workflow, model routing and
parent-branch push policy are shared with container mode. The
[agent browser](docs/integrations/browser.md) is not: it is a capability of the
workspace image, and agent-box never rewrites your host agent configuration to
fake one.

Interactive host sessions use an installed `tmux` and `bash` with agent-box's private
configuration and one private server per project. They survive a closed
terminal; detach with `Ctrl-\ d` and return with `agent-box --host attach`
(optionally followed by a session name). Each launch receives its own current
environment and working directory. `--host stop` affects only that project's
agent-box host server, leaving other projects and your own tmux servers alone.
Without tmux, the CLI starts directly with a short notice. `AGENT_BOX_TMUX=0`,
pipes, redirects and noninteractive commands such as `claude -p` and `codex exec`
also use direct execution. Agent-box never installs tmux at launch.
Bash is resolved from the caller's PATH before starting tmux; if it is missing,
install it or use `AGENT_BOX_TMUX=0` for direct execution.

| Area | Host responsibility / agent-box behavior |
| --- | --- |
| CLI, tools and authentication | You install and authenticate them; normal host file and network access applies. |
| Main CLI permissions | Native CLI settings and your explicit arguments; no added permission/sandbox bypass. |
| Tracker, workflow, project settings | Shared policy and a per-session configuration snapshot. Managed delegates retain workflow's role-specific policy. |
| Environment and Git/gh | Inherited host environment plus explicit env settings; container credential-forwarding switches do not restrict host access. |
| Skills and owned integrations | Applied by `config` / `config apply`; launching does not rewrite user configuration. Existing host plugins remain under the CLI's configuration. |
| Image, mounts, browser, shell/doctor | Container-only. Host tooling is not provisioned from the image. |

Telegram control and notifications have their own integration contract; see
[Telegram](docs/integrations/telegram.md).

## Configuration

Global overrides are stored in a versioned JSON file:

```text
~/.config/agent-box/config.json
```

Configuration has three layers: built-in `Config()` defaults, global overrides,
then project overrides. Global saves keep only settings that differ from the
program defaults, plus `version: 1`; missing nested settings inherit defaults.
The Python configuration dataclasses define those defaults, with no separate
JSON template. A new project starts without overrides.

```bash
agent-box config        # menu: arrows, Space, Enter; save and apply
agent-box config show   # print current settings, tokens redacted
agent-box config check  # verify the Claude Telegram channel setup
agent-box config apply  # apply the saved file without asking anything
```

`agent-box config` uses Python’s standard-library curses menu, with no extra
packages or installer. It keeps the terminal's default background and remains
usable down to 40 columns by 8 rows. Up and Down move, Space toggles or cycles
the current value, Enter opens a section, choice list, or editor, `?` opens the
complete scrollable help for the selected item, and Escape goes back without
the usual curses delay. Home/End and Page Up/Down navigate long lists. Details
sit beside the list on wide terminals and below it on narrow ones. Editors
support cursor movement, Home, End, Backspace, Delete, insertion, and long
values; secret values remain masked. F1 shows the editor's keyboard help.
In the global editor, **R: Reset to default** restores the selected field or
section to built-in program values and removes its stored override on save.
Reset changes only the draft until you save.

Worker, scout, and reviewer model fields combine local suggestions with a
manual model-ID editor. Codex suggestions come from its local model cache when
usable and otherwise from agent-box's bundled hints; Claude uses bundled stable
aliases and model IDs. These lists are hints rather than availability checks,
and opening the menu never makes a network request. Manual entry and the
explicit disable action stay at the top of the list. Existing custom values
remain editable, Escape restores the previous value, and disabling a candidate
affects only the selected role, tier, and provider.

Cancel writes nothing. Save and apply performs the selected changes, including
optional PATH setup: restoring the `agent-box` symlink in the directory you
chose, installing the skills your integrations need, linking them for Claude,
rendering credential files, and reconciling Claude's `settings.json`.
`agent-box config apply` does the same from the saved file, without any
questions — which is what you want after editing `config.json` by hand.
`agent-box upgrade` applies on its own once it has moved the checkout, so a
newer agent-box never leaves you with its older skills installed.

**Starting an agent applies nothing.** `agent-box claude` and `agent-box codex`
read your configuration but never install a skill, write a symlink or rewrite
`settings.json` on the way past. A launch is safe to run without wondering what
it will change in your home directory, and anything that shows up there is there
because you asked for it.

### Defaults

| Setting | Default | Purpose |
| --- | --- | --- |
| Claude status line | on | Workspace, git state, context and provider usage in one line |
| Git identity | on | `user.name` / `user.email`, for commit attribution |
| Provider transport settings | on | Base URL, proxies, CA bundle — without these a container behind a proxy cannot reach the network |
| Share skills with Claude | on | Link `~/.agents/skills` into `~/.claude/skills` |
| Image tool groups | analysis, kubernetes | What the workspace image is built with |
| Everything else | **off** | Credentials, task tracking, browser, notifications, workflow (auto-push defaults on within opt-in workflow) |

Every switch and what it exposes: **[docs/integrations](docs/integrations/)**.

### Environment overrides

```text
AGENT_BOX_AGENT           Default agent when claude/codex is omitted
AGENT_BOX_IMAGE           Container image name
AGENT_BOX_KVM=0           Disable KVM passthrough
AGENT_BOX_GH_TOKEN=0|1    Withhold / force the GitHub token for one run
AGENT_BOX_WORKSPACE_ROOT  Override the resolved workspace root
AGENT_BOX_STATE_DIR       Override ~/.local/share/agent-box
AGENT_BOX_CONFIG_DIR      Override ~/.config/agent-box
AGENT_BOX_AGENTS_DIR      Override ~/.agents, the shared agent/skill tree
AGENT_BOX_STATUSLINE=0    Leave Claude's statusLine untouched when applying
AGENT_BOX_ENV_<NAME>      Set <NAME> inside the session
AGENT_BOX_TMUX=0          Disable tmux wrapping for one launch
```


## Project configuration

`agent-box config` edits global settings. `agent-box config --project` uses the
same menu to edit `.agent-box.json` at the Git repository or worktree root.
Commands from subdirectories use that root too. Outside Git, the current
directory is used for launch settings. Tracker/workflow operations that need a
bound project still require a complete tracker binding.

```json
{
  "tracker": {
    "provider": "vikunja",
    "project": "12",
    "recursive": true,
    "connections": {"vikunja": {"url": "https://tasks.example.org"}}
  },
  "workflow": {"auto_push": false},
  "browser": {"enabled": true},
  "image": {"tools": {"browser": true}}
}
```

The allowed shared paths are `workflow` except its top-level `enabled`,
`browser.enabled`, `image.tools`, `tracker.provider`, and
`tracker.connections.<provider>.url`. Workflow includes independent worker and reviewer overrides,
models, efforts and tier policy. `tracker.project` and `tracker.recursive` are
project identity fields. Missing fields inherit global settings; nested objects
merge by key and lists replace the whole inherited list. Explicit `false`,
empty lists, and allowed empty strings (such as disabled model candidates)
remain choices. Unknown fields, wrong types and invalid JSON are errors.

The project menu labels values **Inherited** or **Overridden**. **R: Reset to
default** removes the selected field or section's override, so it inherits the
effective global value and follows subsequent global changes. **G: Copy current global**
copies the current effective global value into an explicit project override,
pinning it against subsequent global changes. Both actions work on fields and
sections. The available global baseline comes from the session runtime snapshot
when present, otherwise the host configuration; additional process account
overrides are excluded from copying. A temporary session retains the original
global URLs for the editor, without retaining the replaced global token.
Section copying includes only project-allowed settings. Copying tracker connections
removes project-only accounts absent from the global section; copying the enclosing
tracker section preserves its project ID and recursive choice. Empty menu
placeholders are not configured accounts. Choosing a
value equal to the global or program default still saves an explicit project
override. Project identity fields have no global value to copy; the menu explains
why Copy current global is unavailable there. Copying an empty required URL still requires
editing it before saving. Credentials, environment configuration, integration
enablement and host installation settings remain **Global only** and cannot
be edited here; the label is red when the terminal supports color and keeps
the terminal's default background. **Save project** writes only explicit
project values; **Cancel** discards the draft. Neither applies host integrations
nor configures PATH. Image tool changes still require an explicit image rebuild.

Credentials and environment policy belong outside the repository. Every
agent-box command validates the project file before its command actions.
Recognized credential fields are forbidden even when empty or null; the error
includes `PROJECT_CREDENTIALS_FORBIDDEN`, the file and field path, without its
value. URL credentials, queries and fragments are rejected. This checks the
allowed schema and credential fields, not arbitrary strings for possible
secrets, and does not inspect Git history or control commands outside agent-box.

A complete, nonempty `VIKUNJA_URL` and `VIKUNJA_TOKEN` pair selects the account
for a process/session. One missing or empty variable is an error with no
fallback to the global token. The selected URL must match the project URL,
including for a temporary pair; moving a server requires updating the binding.
A project URL never redirects a global token. `tracker.enabled` remains a
global permission.

Direct tracker/workflow commands read the process environment. Launches also
support the existing global `env.forward`/`env.set`, `--env`, and
`AGENT_BOX_ENV_*` channels. A container selects a temporary account only from
these explicitly forwarded or assigned values; exporting the pair alone does
not select it. `--host` inherits the full process environment as usual.
Prefer forwarding secrets by name:

```bash
# Export VIKUNJA_URL and VIKUNJA_TOKEN securely in your shell first.
agent-box --env VIKUNJA_URL --env VIKUNJA_TOKEN codex
```

`--env NAME=VALUE` can expose a literal in process arguments and shell history;
`env.set` stores its literal in global configuration. These remain user choices.
Temporary account values are never saved in global or project configuration,
and configuration display masks credentials and environment assignments.
Launches use immutable session snapshots in agent-box runtime state. Each
workspace's container mount includes only that workspace's snapshots; host
snapshots are outside container mounts. Sessions sharing one workspace box
can access its snapshots. With tracker and workflow disabled, only a separate
credential-free policy directory is mounted; no account or environment values
are rendered there. Each consumer applies its current project's
overrides to the snapshot's global baseline, so another project/session or a
global apply cannot replace an active session's settings or credentials.
Snapshots remain available for detached sessions and are removed with runtime
state during uninstall.


## The workspace image

A base set everyone gets, plus optional groups you choose in `agent-box config`.

| Always | Included |
| --- | --- |
| Agents | Claude Code, Codex CLI |
| Languages | Python, uv, Node.js 22, Bun, mise |
| Source control | git, git-lfs, gh |
| Shell tooling | tmux, jq, ripgrep, fd, fzf, just, build essentials, network utilities |

| Optional group | Default | Included |
| --- | --- | --- |
| `analysis` | on | shellcheck, sqlite3, jscpd |
| `kubernetes` | on | kubectl, Helm, yq |
| `virtualization` | off | QEMU x86-64 + aarch64, OVMF; also gates `/dev/kvm` passthrough |
| `browser` | off | Playwright MCP + headless Chromium (see [agent browser](docs/integrations/browser.md)) |

Anything else your projects need belongs in the project, through `mise` — a
per-project toolchain is not an image everyone shares.

```bash
agent-box build      # normal cached build
agent-box update     # refresh frequently updated tools
agent-box rebuild    # rebuild without cache
```

The image uses Ubuntu 26.04 LTS. `build` and `update` reuse the local base;
`rebuild` pulls the base and disables layer reuse. `update` refreshes Codex,
Claude, Node.js 22, mise, uv,
Bun and enabled Kubernetes tools. Pinned Playwright/Chromium and jscpd stay
above the refresh boundary and are reused when their inputs are unchanged.
Changing the base, earlier instructions or build arguments can rebuild them.

APT package archives and npm downloads use persistent, builder-owned cache
mounts (`agent-box-apt` and `agent-box-npm`), outside the resulting image and
separate from runtime workspace caches. Installs still check current versions.
`rebuild` bypasses cached layers but retains these download caches; it does not
purge Podman storage. A cold or pruned builder downloads packages again.

Changing a group takes effect on the next `agent-box build`. The image records
what it was built with, so `agent-box config show`, `agent-box doctor` and any
launch will tell you when the running image no longer matches your
configuration — and none of them rebuild it behind your back.

Default image: `localhost/agent-box:latest`, overridable with `AGENT_BOX_IMAGE`.

## Integrations

All off by default and independent of each other. See
**[docs/integrations](docs/integrations/)** for the full set; briefly:

- **[Shared skills](docs/integrations/skills.md)** — one skill tree both agents read, plus three engineering-practice skills agent-box ships.
- **[Tracker](docs/integrations/tracker.md)** — portable task commands, repository setup and a working Vikunja API v2 provider.
- **[Workflow](docs/integrations/workflow.md)** — sequential tracked work, configurable provider/model routing, fresh review and parent GitHub PRs.
- **[Agent browser](docs/integrations/browser.md)** — a headless browser inside the box for agents to drive.
- **[Telegram](docs/integrations/telegram.md)** — Anthropic's official control channel, plus one-way notifications.
- **[Git and GitHub](docs/integrations/git.md)** — what of your Git and GitHub setup reaches a session.

## Usage and status line

```bash
agent-box usage codex
agent-box usage claude
agent-box usage all
agent-box usage all --no-cache
```

```text
97/88 · 4h51m/5d23h │ 83/61 · 3h24m/4d11h
```

Segments are unlabeled: Claude first, Codex second, each in its own muted hue
(terracotta and steel blue; the status line adds violet for context).
Single-provider invocations print plain — the colours exist only to separate
segments from each other.

`--no-cache` always requests live usage. Claude tokens are renewed on demand
only after expiry or HTTP 401, at most once per call; there is no background
refresh. Status-line reads never renew credentials. A busy credential lock or
failed renewal returns an error; usage-aware routing keeps its static-weight
fallback when fresh comparable usage is unavailable.

Claude Code can also use:

```bash
agent-box statusline
```

which renders workspace, git state, context usage and provider limits in one
line. It is enabled by default, but agent-box fills Claude's `statusLine` slot
only when that slot is empty, and removes only its own command when switched off
— a status line you configured yourself is never touched.

That slot lives in `~/.claude/settings.json`, which Claude sessions started
outside agent-box read too, so the line follows you there as well.
`AGENT_BOX_STATUSLINE=0` leaves the file alone while applying, and
`agent-box uninstall` takes the line back out for good.

## Workspace and security model

Workspace resolution is intentionally simple:

1. Inside a Git repository or worktree: its top-level directory.
2. Outside Git: the current directory.
3. `AGENT_BOX_WORKSPACE_ROOT` overrides both, when you deliberately want a broader tree.

Every workspace box runs with:

```text
--userns=keep-id
--cap-drop=all
--security-opt=no-new-privileges
```

The container user mirrors the host user's real passwd identity, so files
created inside the workspace map back to you rather than to root. Only the
workspace root, explicit extra mounts, agent state directories and the
integrations you enabled are exposed.

`$HOME` is never consulted for any of this: paths come from the passwd entry, so
a poisoned environment cannot move a bind mount, a state directory or the
container's home.

## Updating

```bash
agent-box upgrade           # fetch, move to the newest release, apply
agent-box upgrade --check   # report what is available, change nothing
```

Installing agent-box is a `git clone`, so upgrading it is a git operation on
that checkout: it moves to the newest release tag, or fast-forwards the
tracking branch while the repository has no tags yet. It refuses on a checkout
with uncommitted changes or with commits no remote has — that is a working
copy, not an installation, and the alternative to refusing would be stashing
your work. Afterwards it applies your saved configuration, because the skills
agent-box ships live in the checkout that just moved.

Changing the tool groups still needs an explicit `agent-box build`; upgrading
says when the image has drifted and leaves the decision to you.

## Uninstalling

```bash
agent-box uninstall
```

Removes what agent-box wrote in places it does not own: its status line and
hooks in Claude's `settings.json`, the skill links in `~/.claude/skills`, the
skills it installed in `~/.agents/skills`, its rendered credential files, the
symlink it put on your PATH, and every workspace box. Anything you wrote
yourself in those places stays.

The PATH line in your shell startup file is the exception. It is left alone by
default — a stray `export PATH` costs nothing, while deleting the wrong two
lines of a login shell's startup file costs a lot — and `--purge` removes it
only if it still matches, character for character, what agent-box wrote. An
edited block is reported with its file and line numbers for you to decide
about, never guessed at.

Your configuration and state survive, so running `agent-box config` again picks
up where you left off. Add `--purge` to delete those too — that includes stored
tokens, so it asks first unless you pass `--yes`. The container image and this
checkout are never removed for you; the command prints how.

## Diagnostics and tests

```bash
agent-box doctor      # inside a real box: tools, credentials, integrations
agent-box selftest    # fast, no Podman, no network
python3 -m unittest discover -s tests
```

The full suite exercises workspace resolution, identity handling, configuration
gates, mounts, tmux and session lifecycle, notifications, tracker/workflow policy, Git
safety checks, skill installation and container behaviour.

## Troubleshooting

<details>
<summary><strong>Podman is missing</strong></summary>

Install Podman. agent-box intentionally does not fall back to Docker.

</details>

<details>
<summary><strong>Claude or Codex is not authenticated</strong></summary>

Authenticate the corresponding CLI on the host first. agent-box mounts the
host-backed agent state rather than managing a second credential store.

</details>

<details>
<summary><strong><code>gh</code> is unauthenticated inside the box</strong></summary>

That is the default. See [Git and GitHub](docs/integrations/git.md).

</details>

<details>
<summary><strong>The configured workspace root rejects my current directory</strong></summary>

`AGENT_BOX_WORKSPACE_ROOT` is set and the current directory is outside it.
Change directory, or remove the override.

</details>

<details>
<summary><strong>A tool I expected is missing from the box</strong></summary>

Check `agent-box config show` — its group may be off — then `agent-box build`.
Project-specific toolchains belong in the project, declared through `mise`.

</details>

<details>
<summary><strong>A shared skill is missing or points somewhere unexpected</strong></summary>

Run `agent-box doctor`, then `agent-box config apply`. agent-box never
overwrites a skill directory or a link it did not create; it says so and keeps
yours.

</details>

## Known limitations

- Linux + Podman only.
- Workspace containers are transient. Detached tmux sessions survive terminal loss, but not a host reboot or a forced container kill.
- `~/.agents` is exposed as a whole tree, not only `skills/`; it is read-only except for the read-write `skills/` subtree and its external symlink targets.
- The agent browser is amd64/arm64 only, because those are the architectures Playwright publishes Chromium builds for.
- `agent-box update` reuses the pinned browser layer when the local base and earlier build inputs are unchanged. `rebuild` reinstalls it.
- The official Claude Telegram plugin is outside agent-box's control and may need compatibility updates when its internals change.
- There is no remote equivalent of `--mount`, and Telegram cannot create a new workspace box by itself.

## Project scope

agent-box is deliberately a **local workspace runtime**, not an agent platform.

It provides isolated local Claude/Codex execution, workspace lifecycle, mounts
and identity handling, detachable sessions, controlled credential exposure, and
the local integrations above.

It does **not** implement its own cloud scheduler, multi-user control plane,
Telegram bot platform, or Codex remote-control protocol.

## License

MIT — see [LICENSE](LICENSE).

The owned skills are maintained with this repository. See
[workflow design references](docs/workflow-references.md) for provenance and reuse decisions.
