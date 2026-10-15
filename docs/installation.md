# Installation, updating and removal

## Requirements

- Linux.
- Python 3.11 or newer. The launcher uses only the standard library: there
  is nothing to build and nothing to install into Python.
- Rootless Podman 5.0 or newer, with a subordinate UID/GID range for your user
  in `/etc/subuid` and `/etc/subgid` (most distributions add one when the user
  is created).
- Git. agent-box asks it for the repository root to decide what to mount, and
  reads it for the status line and the auto-push safety checks.
- `gh` is optional, and only needed for passing a GitHub token into the box.

**Why Podman and not Docker.** `--userns=keep-id` keeps every file an agent
writes owned by you on the host, and rootless Docker has no per-container
equivalent, so agent-box does not fall back to Docker. Podman installs
alongside Docker without conflict — separate storage, configuration and
sockets — as long as you install the `podman` package and not
`podman-docker`, which replaces the `docker` command.

Every command that starts a box checks these requirements first and prints the
exact command that fixes what is missing. Distributions that ship Podman 4.x
(Ubuntu 24.04, Debian 12) need a newer build; see the
[Podman installation guide](https://podman.io/docs/installation).

## Install

**1. Clone it.** Anywhere you like; the checkout is the whole program.

```bash
git clone https://github.com/rus364/agent-box.git ~/.local/src/agent-box
```

**2. Configure it.** Run it by path the first time:

```bash
~/.local/src/agent-box/agent-box config
```

The first question puts `agent-box` on your PATH. The menu offers the writable
directories your PATH already has, or creates `~/.local/bin` and adds it.
While your shell cannot find `agent-box`, the menu opens on that question and
asks once more before saving without it. The symlink and the shell startup
line are two separate questions, both declinable; declining either prints the
exact command to run yourself. The chosen directory is saved, so if the
symlink later disappears, `agent-box config apply` puts it back.

Everything after that is optional; see [Configuration](configuration.md) and
the [integrations](integrations/README.md).

**3. Authenticate the agents on the host.** agent-box mounts your existing
`~/.claude` and `~/.codex` rather than keeping a second credential store, so
log in with `claude` and `codex` before the first launch.

**4. Start a session.** The workspace image is built automatically on first
use, which takes a few minutes once:

```bash
cd ~/code/my-project
agent-box claude          # or: agent-box codex
```

## Shell completion

Add one line to `~/.bashrc`, or to `~/.zshrc` after `compinit`:

```bash
source <(agent-box completion bash)   # zsh: source <(agent-box completion zsh)
```

Candidates are computed on every Tab from the same tables that parse the
command line, so new options and commands complete without regenerating the
script.

## Updating

```bash
agent-box upgrade           # fetch, move to the newest release, apply
agent-box upgrade --check   # report what is available, change nothing
```

Upgrading is a Git operation on the checkout: it moves to the newest release
tag, or fast-forwards the tracking branch while the repository has no tags. It
refuses on a checkout with uncommitted changes or with commits no remote has,
since that is a working copy rather than an installation. Afterwards it
applies your saved configuration, because the skills agent-box ships live in
the checkout that just moved.

The workspace image is not rebuilt: `upgrade` reports when it has drifted from
your configuration and leaves `agent-box build` to you.

## Uninstalling

```bash
agent-box uninstall [--purge] [--yes]
```

Removes what agent-box wrote in places it does not own: its status line and
hooks in Claude's `settings.json`, the skill links in `~/.claude/skills`, the
skills it installed in `~/.agents/skills`, its rendered credential files, the
symlink it put on your PATH, and every workspace box. Anything you wrote
yourself in those places stays.

- **Configuration and state survive** a plain uninstall, so `agent-box config`
  picks up where you left off. `--purge` deletes them too, including stored
  tokens, and asks first unless you pass `--yes`.
- **The PATH line in your shell startup file** stays by default. `--purge`
  removes it only if it still matches, character for character, what
  agent-box wrote; an edited block is reported with its file and line numbers
  for you to decide about.
- **The container image and the checkout** are never removed for you; the
  command prints how.
