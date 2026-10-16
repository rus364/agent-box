# Installation, updating and removal

## Requirements

- Linux.
- Python 3.11 or newer. The installed package has no runtime dependencies.
- [uv](https://docs.astral.sh/uv/guides/tools/) or
  [pipx](https://pipx.pypa.io/stable/), which manages the isolated Python environment
  and the command on PATH.
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

**1. Install from Git.** The Python distribution is `agent-box-tool`; the
command and product name remain `agent-box`. Until the public release, use
an account with access to the repository:

```bash
uv tool install 'git+ssh://git@github.com/rus364/agent-box.git'
uv tool update-shell   # if the installer reports its bin directory is not on PATH
```

Or use pipx:

```bash
pipx install 'git+ssh://git@github.com/rus364/agent-box.git'
pipx ensurepath
```

Reopen the shell after changing PATH. Both installers build the package from
Git; running it needs no source checkout. Public PyPI installation is separate
release work and is not available through these instructions.

**2. Configure integrations.**

```bash
agent-box config
```

Save applies the integrations you select; see [Configuration](configuration.md)
and the [integrations](integrations/README.md). Package installation itself does
not edit Claude or Codex settings.

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

Use the installer that owns the package, then refresh the integrations:

```bash
uv tool upgrade agent-box-tool
agent-box config apply
```

With pipx, use `pipx upgrade agent-box-tool` followed by the same
`agent-box config apply`. The installer retains the Git source selected at
installation. An installation pinned to a tag or commit stays pinned; install
from the new reference explicitly to change it.

Running boxes retain an immutable copy of their launcher resources while the
package is replaced. If a new invocation reports a different runtime, end that
workspace's sessions with `agent-box stop` and launch again. Copies are retained
in agent-box state until `agent-box uninstall --purge` removes that state.

Updating the Python package does not rebuild the workspace image. Run
`agent-box build` for changed build inputs, or `agent-box update` to refresh
its tools; see [the image lifecycle](image.md#building-and-refreshing).

## Uninstalling

Clean up integrations before removing the executable:

```bash
agent-box uninstall       # add --purge to delete configuration and state too
uv tool uninstall agent-box-tool
# pipx users: pipx uninstall agent-box-tool
```

`agent-box uninstall` removes its status line and hooks, owned skills and skill
links, rendered credentials and workspace boxes. User-owned integrations stay.
`--purge` also deletes configuration and state, including stored tokens; it asks
first unless `--yes` is supplied.

The installer owns its command and shell PATH setup, which agent-box leaves
untouched. The container image also stays; the cleanup report prints its
explicit removal command.

## Contributor installation

From a clone, install editable so the command uses your working tree:

```bash
uv tool install --editable .
agent-box selftest
python3 tests/run_suite.py
```

After changing shipped integrations, run `agent-box config apply` explicitly.
For development inside a box, use the separate environment described in
[Developing agent-box inside agent-box](design/integrations.md#developing-agent-box-inside-agent-box).
