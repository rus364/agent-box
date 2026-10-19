# Installation, updating and removal

## Requirements

- Linux.
- Python 3.11 or newer. The installed package has no runtime dependencies.
- For the PyPI/Git route, [uv](https://docs.astral.sh/uv/guides/tools/) or
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

**1. Install from PyPI.** The Python distribution is `agent-box-tool`; the
command and product name remain `agent-box`:

```bash
uv tool install agent-box-tool
uv tool update-shell   # if the installer reports its bin directory is not on PATH
```

Or use pipx:

```bash
pipx install agent-box-tool
pipx ensurepath
```

Reopen the shell after changing PATH. Running the installed command needs no
source checkout. For example, to install the first release, use `agent-box-tool==1.0.0`.

Public Git installation is also supported, without a GitHub account:

```bash
uv tool install 'git+https://github.com/rus364/agent-box.git@v1.0.0'
# or:
pipx install 'git+https://github.com/rus364/agent-box.git@v1.0.0'
```

Omit `@v1.0.0` only when you deliberately want the development branch.

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

## Arch Linux / AUR

The stable AUR package is `agent-box`; its Python distribution is still
`agent-box-tool`. Install it with an AUR helper, for example:

```bash
yay -S agent-box
```

Or review and build the recipe directly:

```bash
git clone https://aur.archlinux.org/agent-box.git
cd agent-box
makepkg -si
```

The package uses system Python and declares Podman and Git dependencies. pacman
owns `/usr/bin/agent-box`; uv/pipx are not needed for this installation route.
The development test suite runs in release CI, not during AUR installation.
Rootless Podman still needs the user setup described under [Requirements](#requirements).
Run `agent-box config` as your normal user to select integrations. Package
installation/removal scripts never configure integrations or edit users' homes.

To switch an existing uv/pipx installation to AUR, install the Arch package,
then remove only the old tool environment and refresh your shell's command cache:

```bash
uv tool uninstall agent-box-tool  # or: pipx uninstall agent-box-tool
hash -r
command -v agent-box             # should print /usr/bin/agent-box
```

Do not run `agent-box uninstall` during this switch: you are retaining the
application and its integrations. Saved settings and workspace data remain in
place. If a shell alias or another installation still shadows `/usr/bin/agent-box`,
resolve that before using the new installation.

Update through your AUR helper (for example `yay -Syu`), or rebuild the updated
recipe with `makepkg -si`. When Arch changes Python's minor version, rebuild the
AUR package for the new system Python; see [Arch's Python guidance](https://wiki.archlinux.org/title/Python#Module_not_found_after_Python_version_update).
The next host launch reconciles integrations automatically; active workspaces
keep their saved runtime until explicitly stopped and relaunched.

For removal, run `agent-box uninstall` as your normal user first if you want to
remove its integrations, then `sudo pacman -R agent-box`. Configuration and
state remain unless you explicitly choose `agent-box uninstall --purge`.

## Shell completion

Add one line to `~/.bashrc`, or to `~/.zshrc` after `compinit`:

```bash
source <(agent-box completion bash)   # zsh: source <(agent-box completion zsh)
```

Candidates are computed on every Tab from the same tables that parse the
command line, so new options and commands complete without regenerating the
script.

## Updating

Use the installer that owns the package:

```bash
uv tool upgrade agent-box-tool
```

With pipx, use `pipx upgrade agent-box-tool`. The next host agent launch migrates
saved data and refreshes configured integrations automatically. Unchanged launches
do not reapply them. Use `agent-box config apply` for manual configuration edits
or to retry a failed application. See [compatibility](compatibility.md).

For a Git installation the installer retains its selected source. A pinned tag
or commit stays pinned; select the new reference explicitly, for example
`uv tool install --force 'git+https://github.com/rus364/agent-box.git@v1.1.0'`
or `pipx install --force 'git+https://github.com/rus364/agent-box.git@v1.1.0'`
once that version is released. Do not use TestPyPI for normal installations.

Running boxes retain an immutable copy of their launcher resources while the
package is replaced. If a new invocation reports a different runtime, end that
workspace's sessions with `agent-box stop` and launch again. Copies are retained
in agent-box state until `agent-box uninstall --purge` removes that state.

Updating the Python package does not run an image build inside the installer.
Before the next new workspace starts, agent-box automatically builds with cache
if the build inputs changed (or the image predates input labels). Unchanged inputs
reuse the image. A running workspace still needs an explicit stop/relaunch.
Use `agent-box build` to prepare ahead of time, `agent-box update` for fresh tools,
or `agent-box rebuild` for base/OS refresh; see [the image lifecycle](image.md#building-and-refreshing).

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

The next host launch detects changed package contents and refreshes configured
integrations. `agent-box config apply` can apply them explicitly during development.
For development inside a box, use the separate environment described in
[Developing agent-box inside agent-box](design/integrations.md#developing-agent-box-inside-agent-box).
