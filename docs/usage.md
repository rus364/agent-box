# Using agent-box

## Starting agents

```bash
agent-box                                  # the default agent
agent-box claude -p "summarize this repository"
agent-box codex exec "inspect this repository"
agent-box shell                            # a shell in this project's box
agent-box doctor                           # check the box and its integrations
```

The default agent is Claude, or Codex if you choose it under **Agents →
Default agent** in `agent-box config` (`agents.default`). It is a global
setting; an explicit `claude` or `codex` always wins. The default agent
receives only options: a prompt needs the agent named, and a word that is not
a command is refused rather than sent as a prompt.

Everything after the agent name reaches the agent unchanged; `--` ends
agent-box options early.

Container launches keep the CLI's own permission settings by default. To
explicitly bypass them, enable **Bypass permissions (container)** separately
under **Agents → Claude** or **Agents → Codex**. See
[container permissions](configuration.md#defaults) for the flags and upgrade
path. Native `--host` launches always retain their own permission settings.

## The workspace box

The first command in a project creates its box from the current settings and
flags. Later `claude`, `codex`, `shell` (including `shell -c ...`) and
`doctor` commands for the same workspace join that box with `podman exec`,
without repeating its creation flags. The box keeps its image, mounts,
devices, k3s cluster and shared access choices; each new process uses its own
workflow and session settings. A different workspace gets a different box.
When no workload remains — no session, background daemon or nested
container — the box exits and Podman removes it.

An explicit option that conflicts with the running box names the conflict and
asks you to end the box first. Matching shared-access flags and already
mounted paths are accepted. Changing saved settings alone does not reconfigure
a running box.

```bash
agent-box stop                  # end the box and every session in it, and stop its k3s cluster
agent-box stop --cluster-data   # also delete the k3s cluster's data
agent-box stop --podman-data    # also delete nested Podman data that box.podman.persistent_data keeps
```

Each project keeps things on the host beyond its box: a k3s cluster, nested
Podman data and agent-box's state directories, all under a workspace id that
is a hash of the project directory. `agent-box workspaces` lists them with
the directory each belongs to, so moving or deleting a project directory does
not leave them behind unseen:

```bash
agent-box workspaces                  # ID, box, nested Podman data, k3s cluster, directory
agent-box workspaces rm PATH|ID...    # delete everything agent-box keeps for these workspaces
agent-box workspaces prune            # the same for every workspace whose directory is gone
```

Both deletions show what they will remove and ask first; `--yes` skips the
question and is required without a terminal. A directory shown as
`(unknown)` was never recorded, because the workspace has not had a box or
host session since agent-box started recording it; `prune` leaves those
alone, and `rm ID` removes one.

How the workspace root is chosen and what the box can reach is described in
[Workspace and security model](security.md).

## Networking

Publish workspace services with `--publish 8080:3000`, reach selected host
services with `--host-port 5432:15432`, or explicitly share the host network
with `--network=host`. See [workspace networking](networking.md) for direction,
loopback defaults, k3s, nested containers and running-session rules.

## Session options

Options before the agent change one launch and are never saved;
`agent-box --help` lists them all. Workflow, tracker and access options apply
over the global and project configuration and travel in the session snapshot,
so nested launches inside the session inherit them; `agent-box config show`
inside the session names them.

```bash
agent-box --reviewer claude:opus:high --review always codex
agent-box --worker codex:gpt-5.5:high --no-push claude
agent-box --host --no-tmux codex exec "run the tests"
```

| Group | Options |
| --- | --- |
| Launch | `--host`, `--workspace PATH`, `--mount PATH[:ro]`, `--env NAME[=VALUE]`, `--no-tmux`, `--no-kvm` |
| Workflow | `--worker SPEC`, `--reviewer SPEC`, `--scout SPEC`, `--sync SPEC`, `--[no-]tiers`, `--review off\|auto\|always`, `--rounds N`, `--merge github\|local`, `--[no-]push`, `--no-workflow` |
| Tracker | `--tracker URL`, `--project ID` ([details](configuration.md#session-tracker-override)) |
| Access | `--[no-]gh-token`, `--[no-]gh-config`, `--[no-]git-config`, `--[no-]registries`, `--[no-]kubeconfigs`, `--[no-]cloud-profiles`, `--[no-]k3s`, `--[no-]links`, `--[no-]api-keys`, `--no-browser`, `--[no-]remote-control`, `--[no-]notify` |

`SPEC` is `PROVIDER[@API]:MODEL[:EFFORT]`, for example `codex:gpt-5.5:high`
or `claude@deepseek:deepseek-v4`. `--worker`, `--reviewer`, `--scout` and
`--sync` pin that model outright: the role's candidate lists and task tiers
choose no other. `--[no-]tiers` decides whether a task's tier selects the
worker, Plan sync and reviewer lists. Workflow options need the workflow
enabled in `agent-box config`. Options that only a container can honour
(mounts, KVM, credential forwarding) are refused with `--host` rather than
ignored.

## Detach and reattach

Interactive `claude`, `codex` and `shell` sessions run under tmux inside the
box, so closing the terminal detaches instead of killing the session. Detach on
purpose with `Ctrl-\ d` and come back with:

```bash
agent-box attach                 # the only session; with several, lists them to name one
agent-box attach claude-31337    # a named session
```

A detached session keeps its box alive. Only real terminals are wrapped:
pipes, redirects and CI write straight to stdout. In the box, a batch command
such as `claude -p` started from a terminal is wrapped too; pass `--no-tmux`
to get its output directly, as for any one launch. A wrapped session that fails reports only its
exit status, because its pane is destroyed with it; rerun with `--no-tmux` to
read the output.

The mouse wheel scrolls tmux history when the agent does not handle mouse
events itself; press `q` to leave scroll mode. agent-box uses its own tmux
configuration and server, never yours.

## Extra mounts

The workspace root is the only project path mounted automatically:

```bash
agent-box --mount ~/code claude
agent-box --mount /mnt/data/specs:ro --mount /srv/generated codex
```

Mounts keep their host absolute paths inside the container and are read-write
unless you append `:ro`.

Containers keep their own `/tmp`; it is not automatically shared with the
host or sidecars. Explicit mounts also reach the session's browser, preserving
their access mode. They do not propagate to the k3s sidecar or its pods.

## Extra environment variables

```bash
agent-box --env DATABASE_URL=postgres://localhost/dev codex
agent-box --env MY_TOKEN claude
```

`--env NAME=VALUE` sets a variable. `--env NAME` forwards the value that name
has in your shell without putting it in the process list; use this form for
anything secret. Both are repeatable and win over the environment lists stored
in `agent-box config`.

The same request works as a shell prefix:

```bash
AGENT_BOX_ENV_DATABASE_URL=postgres://localhost/dev agent-box codex
```

A bare `DATABASE_URL=... agent-box codex` does not reach the box: inside the
launcher a prefixed variable is indistinguishable from the hundreds inherited
from your login profile, and honouring them would hand the container your
whole host environment.

## Host mode

```bash
agent-box --host              # the default agent
agent-box --host claude
agent-box --host attach       # return to a host session in this project
agent-box --host stop         # end this project's agent-box host sessions
```

`--host` runs the installed CLI directly, with your normal host environment
and credentials. Tracker commands, workflow and model routing are shared with
container mode. The optional [agent browser](integrations/browser.md) requires
local Podman and runs in a separate container; disabled host browsing needs no
Podman. The [k3s cluster](integrations/k3s.md) uses the same enabled setting and managed
kubeconfig in both modes; host use requires local rootless Podman and kubectl.

Interactive host sessions run under your installed `tmux` and `bash`, with
agent-box's own configuration and one private tmux server per project. They
survive a closed terminal; detach with `Ctrl-\ d` and return with
`agent-box --host attach [SESSION]`. Without tmux the CLI starts directly with
a short notice; agent-box never installs tmux. `--no-tmux`, pipes, redirects
and non-interactive commands such as `claude -p` also start directly.

| Area | In host mode |
| --- | --- |
| CLI, tools and authentication | You install and authenticate them; normal host file and network access applies. |
| CLI permissions | The CLI's own settings and your arguments; agent-box adds no permission or sandbox bypass. |
| Tracker, workflow, project settings | The same policy, through a per-session configuration snapshot. |
| Environment, Git and `gh` | Inherited from your shell plus explicit `--env`; the container credential switches do not restrict host access. |
| Skills and integrations | Applied by configuration commands and automatically on the first host launch after a package update. |
| Workspace image, extra mounts, `shell`, `doctor` | Container only. |
| k3s | Shared project cluster and kubeconfig; inherit the active project choice. |
| Isolated browser | Separate managed container in both modes; opt-in and local Podman required. |

Telegram control and notifications have their own rules; see
[Telegram](integrations/telegram.md).

## Usage limits and the status line

```bash
agent-box usage                  # enabled usage blocks
agent-box usage claude
agent-box usage all --no-cache   # ask enabled providers
agent-box usage codex --json
```

```text
97/88 · 4h51m/5d23h │ 83/61 · 3h24m/4d11h
```

Segments are unlabelled: Claude first, Codex second, each in its own muted
hue; single-provider output is plain. In `agent-box config`, open
Agents → Claude → Status line to toggle Claude usage, Codex usage and Custom
usage. These switches control both the status line and `usage all` (including
`--json` and `--no-cache`): disabled blocks collect no data, make no API calls
and do not run their custom command. All three start disabled; an empty custom
command adds no block. Explicit `usage claude` and `usage codex` still query the
chosen provider. `--no-cache` requests live usage for the selected providers.
Claude tokens are renewed on demand, only after expiry or HTTP 401 and at most
once per call; there is no background refresh.

`agent-box statusline` renders the workspace, Git state, context usage and
provider limits in one line for Claude Code. It is on by default, but
agent-box fills Claude's `statusLine` slot in `~/.claude/settings.json` only
when that slot is empty, and removes only its own command when you switch it
off. Since Claude sessions outside agent-box read the same file, the line
appears there too. Status-line reads never renew credentials.
`AGENT_BOX_STATUSLINE=0` leaves the file alone while applying, and
`agent-box uninstall` removes the line.
