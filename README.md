# agent-box

`agent-box` runs Claude Code and Codex CLI in a transient, rootless Podman workspace container built from one development image. One workspace gets one shared box: Claude, Codex, and `agent-box shell` all execute in that same box while useful work remains.

It mounts your current project into the container at the *same absolute path* it has on the host, builds a container user that mirrors your real Unix identity, and preserves agent configuration/authentication across transient workspace boxes.

There are four separate things in here, and it's worth keeping them apart:

| | What it is | Who implements it |
| --- | --- | --- |
| **Local containerized agents** | `agent-box claude` / `agent-box codex` in a hardened shared workspace container | agent-box |
| **Claude Telegram channel** | Control the live Claude session from Telegram | **Anthropic's official plugin** — agent-box only passes the flag |
| **Outbound notifications** | One-way "turn finished / waiting / failed" pings | agent-box |
| **Codex remote control** | Driving Codex from elsewhere | **OpenAI's official ChatGPT/Codex experience**, where supported |

agent-box implements no Telegram bot, no remote-control daemon, and no session routing of its own. Earlier versions did; that whole subsystem has been removed.

## Requirements

- Linux
- Python 3.11+
- Podman

The launcher uses only the Python standard library. There are no Python package dependencies.

## Install

Clone the repository anywhere — it does not need to live inside your projects tree:

```bash
git clone <this repo> ~/.local/src/agent-box
ln -s ~/.local/src/agent-box/agent-box ~/.local/bin/agent-box
```

(Any directory on `PATH` works.)

## Build

```bash
agent-box build    # regular cached build
agent-box update   # refresh frequently updated tools, preserving base layers
agent-box rebuild  # refresh tools and rebuild without cache
```

The image is also built automatically on first use. Build args are derived from your real Unix identity (`pwd.getpwuid(os.getuid())`) — see "Container identity".

The default image is `localhost/agent-box:latest`. Override it with `AGENT_BOX_IMAGE`. If you change host user/UID/GID (or share the image across users), rebuild.

The image contains Claude Code, Codex CLI, Node 22, **Bun** (required by the official Telegram channel plugin), Python + uv, mise, git/git-lfs/gh, kubectl/helm/yq/jq, QEMU (x86-64 and aarch64), tmux (which carries detachable sessions), and the usual build and network tooling.

## Run Claude Code or Codex

```bash
cd /any/path/to/your/project
agent-box            # default agent (claude)
agent-box claude
agent-box claude -p "summarize this repository"
agent-box codex
agent-box codex exec "inspect this repository"
agent-box shell      # a shell inside this workspace's shared agent-box container
agent-box attach     # return to a session whose terminal went away
agent-box stop       # end this workspace's box and every session in it
```

This works from **any** directory — there is no requirement to keep projects under a fixed host directory such as `~/code`.

Claude Code is the default agent; change it with `export AGENT_BOX_AGENT=codex`.

The first command in a project creates its box lazily. Later `claude`, `codex`, and `shell` commands from that project use `podman exec` in the same process namespace, so a shell can see running agents and daemons. A different workspace gets a different box. The box is removed automatically after no useful workloads remain; no normal `up`/`down` lifecycle is required, though a detached session (see below) counts as a workload and `agent-box stop` ends a box you walked away from. This also lets a Codex remote-control/app-server process survive after its launcher command exits. Their `~/.claude` and `~/.codex` state directories are still mounted from the host, so authentication, settings, and history persist after the transient box is removed. If `~/.claude.json` exists on the host it is mounted too. For `agent-box codex`, MCP commands under `/usr/lib/chatgpt/resources/` are disabled only for that invocation because those ChatGPT desktop resources are not present in the image; the host Codex configuration is never modified.

### Detachable sessions

Closing a terminal used to be fatal in a useful sense and useless in every other: a `podman exec` session cannot be re-attached — `podman attach` reaches only the container's main process — so the agent kept running inside the box with nobody on the other end of its pty, and there was no way back to it.

Interactive `claude`, `codex`, and `shell` sessions therefore run under a tmux server **inside** the box. The `podman exec` client then owns nothing but a tmux client, so losing it — a closed window, a dropped ssh connection, a laptop that slept — detaches the session instead of orphaning it:

```bash
agent-box claude     # terminal dies here
agent-box attach     # ...and the same session comes back, still working
```

- `C-\ d` detaches on purpose. The inner prefix is `C-\` rather than `C-b` so a tmux you run on the host keeps its own prefix and no nesting gymnastics are needed.
- `agent-box attach` takes the session back when the box holds exactly one, and lists them when it holds several (`agent-box attach claude-31337`). It always evicts whatever client is still holding the session: after a closed terminal that client is a ghost — conmon keeps its pty open, so tmux never learns nobody is there, and it would otherwise keep the session to itself.
- Every launch is its own session. Two `agent-box claude` commands in one workspace are still two independent Claude sessions, exactly as before.
- A detached session is a live workload, so its box no longer disappears on its own. `agent-box stop` ends the box and everything in it.
- Only real terminals are wrapped. `claude -p '...'`, a pipe, a redirect, CI, and `agent-box doctor` run exactly as they did, straight to your stdout. `AGENT_BOX_TMUX=0` opts a single launch out.
- The exit status is still the agent's own: tmux reports only its own, so the wrapped session records the real one in this workspace's runtime state and the launcher exits with it. A launcher that detached instead exits 0 and says which session is still running.
- Nothing survives a host reboot or `podman kill` — the box is created with `--rm`. What survives that is what always did: `~/.claude` and `~/.codex` are host-mounted, so `claude --continue` picks the conversation back up.
- The official Claude Telegram channel follows the session, not the terminal. Its single-owner record names the box session as well as the launcher, so a detached Claude keeps the bot it is still answering, and a second Claude started meanwhile is told which session holds it (`session claude-31337, /path/to/project`) instead of quietly taking the bot away. The channel is free again as soon as that session really ends.

tmux is part of the image, so an existing installation needs one `agent-box build` before sessions become detachable — the base layer's instruction changed, so an ordinary cached build picks it up.

### Host mode (`--host`)

```bash
agent-box --host claude
agent-box --host codex
```

Runs the installed host `claude`/`codex` executable directly in the resolved workspace — no Podman, no container hardening, your normal host git/gh credentials. Pass-through arguments and the real exit status work exactly as in container mode. `agent-box claude`/`agent-box codex` (without `--host`) keep meaning containerized execution; primary-agent choice is always explicit either way — agent-box never picks or balances between Claude and Codex for you.

Container mode and host mode resolve Flow-Next detection, review routing, Flowctl availability, and auto-push policy through the exact same function, so the two cannot drift apart (see below).

## Configuration

One command, one file:

```bash
agent-box config          # interactive setup
agent-box config show     # current settings, bot token redacted
agent-box config check    # verify the official Claude Telegram channel setup
```

Settings are stored as a small versioned JSON document at `~/.config/agent-box/config.json` (mode 0600, since it may hold a Telegram bot token):

```json
{
  "version": 1,
  "claude":        { "telegram_channel": false, "statusline": true },
  "notifications": { "enabled": false, "telegram_token": "", "telegram_chat_id": "" },
  "github":        { "gh_token": false, "gh_config": false },
  "git":           { "identity": true, "full_config": false },
  "providers":     { "api_keys": false, "transport": true },
  "vikunja":       { "enabled": false },
  "flow_next": {
    "review_backends": { "claude": "codex:gpt-5.6-sol:medium", "codex": "codex:gpt-5.6-sol:medium" },
    "auto_push": false
  }
}
```

### What each setting exposes to the container

| Setting | Default | What it does |
| --- | --- | --- |
| `claude.telegram_channel` | **off** | Adds `--channels plugin:telegram@claude-plugins-official` to `agent-box claude` |
| `claude.statusline` | **on** | Keeps Claude's `statusLine` pointed at `agent-box statusline`; exposes nothing, and never overwrites a status line you configured yourself |
| `notifications.enabled` | **off** | Outbound-only Telegram pings; needs a token and chat id to become active |
| `github.gh_token` | **off** | Permits an exported token, or obtains `gh auth token`, and forwards it as `GH_TOKEN` |
| `github.gh_config` | **off** | Copies the host's `~/.config/gh` (which stores that token) into the container |
| `git.identity` | **on** | Seeds only global `user.name` and `user.email` for commit attribution |
| `git.full_config` | **off** | Copies the complete host Git configuration; opt in only if you accept its credential helpers, headers, includes, and rewrites being exposed |
| `providers.api_keys` | **off** | Forwards exported `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` |
| `providers.transport` | **on** | Forwards endpoint, Bedrock/Vertex, proxy, and CA transport settings (turn off if your proxy URL carries credentials) |
| `vikunja.enabled` | **off** | Exposes the Vikunja skill's host credentials and puts its CLI on the container `PATH` (see below) |
| `flow_next.review_backends` | see below | Flow-Next review backend spec, by primary agent (`claude`/`codex`) |
| `flow_next.auto_push` | **off** | Opt-in Flow-Next auto-push, Claude and Codex (see below) |

**Security-sensitive host integration is explicit.** In particular, being logged in to `gh` on the host is *not* consent to hand a live GitHub credential to every container — `github.gh_token` must be turned on deliberately. (An earlier agent-box forwarded the token whenever `gh` happened to be authenticated. It no longer does.)

Provider values, exported GitHub tokens, and a GitHub token obtained from `gh auth token` are forwarded to Podman by name (`--env=NAME`), never as `--env=NAME=value`; the latter would expose the value in process argv. `AGENT_BOX_GH_TOKEN=0` denies GitHub passthrough for one run and `=1` permits it.

**A shared workspace box is one trust boundary.** Claude, Codex, shells, hooks, and daemons in that box share a PID namespace and Unix user. Per-exec environment forwarding avoids putting secret values in Podman argv, labels, or persistent container metadata, but it does not make those values confidential from other same-user processes in that same box: on normal Linux/Podman setups they can read `/proc/<pid>/environ`. Only enable GitHub, provider, or notification credentials for a workspace whose concurrent processes you trust.

### Environment overrides

Persistent config is the primary mechanism, but a few variables still override for a single command:

```text
AGENT_BOX_AGENT           Default agent when claude/codex is omitted (default: claude)
AGENT_BOX_IMAGE           Container image name (default: localhost/agent-box:latest)
AGENT_BOX_KVM=0           Disable KVM passthrough
AGENT_BOX_GH_TOKEN=0|1    Withhold / force the host GitHub token for this run
AGENT_BOX_WORKSPACE_ROOT  Override the host directory mounted into the container
AGENT_BOX_STATE_DIR       Override ~/.local/share/agent-box
AGENT_BOX_CONFIG_DIR      Override ~/.config/agent-box
```

## Claude Telegram channel (official plugin)

Telegram control of a Claude session is provided entirely by Anthropic's official `telegram@claude-plugins-official` plugin. agent-box does **not** reimplement access control, pairing, permissions, message delivery, questions, or Telegram polling — the plugin owns all of it.

What agent-box does is small and explicit: when `claude.telegram_channel` is on, `agent-box claude` starts the normal interactive Claude Code CLI with

```bash
claude --dangerously-skip-permissions --channels plugin:telegram@claude-plugins-official
```

**The terminal TUI stays the primary interface.** Telegram controls that same live session through the official channel — it is not a second, separate session.

### Setup (once per host)

The plugin is installed and configured through Claude Code's own supported mechanisms, from inside a Claude session. agent-box does not install or configure it silently.

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Start a session and install the plugin:

   ```text
   agent-box claude
   /plugin install telegram@claude-plugins-official
   /telegram:configure 123456789:AAHfiqksKZ8...
   ```

   The token is written to `~/.claude/channels/telegram/.env`.
3. Turn the channel on in agent-box, then start a fresh session:

   ```bash
   agent-box config      # answer yes to "Enable Claude Telegram channel"
   agent-box claude
   ```
4. DM your bot; it replies with a pairing code. Back in the session:

   ```text
   /telegram:access pair <code>
   /telegram:access policy allowlist
   ```

`agent-box config check` reports whether the plugin and token are in place and prints these steps if not.

Because `~/.claude` is bind-mounted read-write at its host path, a plugin installed once is present in every workspace box, and its state (`~/.claude/channels/telegram/`: token, allowlist, inbox) persists across boxes. Bun is in the image because the plugin's MCP server runs on it and `bun install`s its own dependencies on first start; that download is cached in the container-only cache directory.

### What still requires manual setup

- Creating the bot with BotFather and running `/plugin install` + `/telegram:configure` (step 2 above). These are Claude Code slash commands; there is no supported way to do them from outside a session, and agent-box deliberately does not write Claude's plugin state behind its back.
- Pairing and the allowlist policy (step 4).
- On a managed claude.ai Teams/Enterprise account, channels are off unless an administrator enables them. Personal/Console accounts do not need this.

If the channel is enabled in config but the plugin or token is missing, `agent-box claude` prints instructions and starts as a normal local Claude session.

### One active Telegram owner

Telegram permits only one `getUpdates` consumer per bot token. agent-box records one live channel owner using PID, process-start time, and boot ID; stale records are recovered automatically. A second `agent-box claude` prints `Telegram channel already active in another Claude session`, starts normally, and does not receive `--channels`.

The official plugin can still be globally loaded by Claude Code in that second process, even without `--channels`. Its current server loads its persisted `.env` only when `TELEGRAM_BOT_TOKEN` is undefined. agent-box therefore passes an explicit empty `TELEGRAM_BOT_TOKEN` to every non-owner Claude container (including when the channel is disabled). The official plugin exits before polling instead of falling back to the shared token, so it cannot steal or kill the owner's poller. This is isolation around an upstream multi-process limitation, not a second Telegram controller.

## Codex remote control

agent-box has no Telegram or remote-control integration for Codex. Use the official ChatGPT/Codex remote-control experience where it is supported.

`agent-box codex` is purely the local, containerized path: a normal interactive Codex CLI, isolated in the container, with the same mounts and hardening as Claude.

## Outbound notifications

Remote control and notifications are separate features. Notifications are strictly **one-directional**: agent-box only sends Telegram messages. There is no polling daemon, no `getUpdates`, no callbacks, and no way to drive a session from a notification. Completion reports for both agents use Telegram Rich Markdown when available, with a safe plain-message fallback for older or incompatible Bot API deployments.

```bash
agent-box config        # enable, with bot token + chat id
agent-box notify test   # send a test message
```

What gets sent:

- **Codex** — turn completed, with the last assistant message.
- **Claude** — waiting for input, permission required, turn finished (with the last assistant message), stopped with an error.

Both completion reports carry the text the agent actually wrote, rendered as Markdown: Codex supplies it in its notify payload, Claude in the `Stop` hook's `last_assistant_message`. A turn that ends without closing prose still reports plain completion.

Claude's "turn finished" is suppressed while background tasks from that turn are still running, so a long background job doesn't ping you as though it needed input.

**No double notifications.** When a Claude session owns the official channel, it already relays replies and prompts to the paired chat, so agent-box suppresses only its duplicate Claude interaction hooks (permission and waiting-for-input). Its mechanical `Stop`/`StopFailure` hooks still send completion, background-work, and error notifications; the official channel does not reliably replace those lifecycle signals. Codex notifications are unaffected.

The main configuration file is never mounted into a container. When outbound notifications are usable, agent-box forwards the bot token and chat id only to the Claude/Codex exec session that needs them; shell and doctor do not receive them directly. Because a workspace box is a shared same-user PID namespace, this limits accidental exposure in metadata and ordinary shell environment setup, not exposure to another process deliberately inspecting a running agent's `/proc` environment. Disabled notifications mean no notification credential forwarding.

Enabling notifications installs Claude hooks into `~/.claude/settings.json`; disabling removes them. Claude session launch also reconciles those managed hooks, so upgrades migrate the former `~/.local/bin/agent-box notify claude` command without requiring a manual settings edit or rerunning `agent-box config`. Codex is wired up per-run with `-c notify=[...]`.

## Container identity

The container's user, UID, GID, and home directory mirror the **real host identity that built the image** (`agent_box.identity.detect_host_identity`, backed by `pwd.getpwuid`, not `$USER`/`$HOME`, which a shell can set to anything). Another user rebuilding this project on their own machine gets their own identity automatically — nothing in the source names a specific person or machine. Rootless Podman's `--userns=keep-id` maps that identity back to the real host UID/GID, so files created in the container are owned by you on the host.

This matters beyond ownership: because the container user's home is the same path as your real home, host-authored absolute-path symlinks — notably the shared skill symlinks agent-box writes into `~/.claude/skills` — resolve correctly inside the container without any path rewriting.

If the username reported by the OS isn't a legal Linux login name, it's sanitized deterministically rather than replaced with something invented; if even that fails, it falls back to `user<uid>`.

**One home directory, from one source.** The home used for the build identity, the container's runtime `HOME`, every bind mount's host side, and the state/config defaults is always the passwd entry's `pw_dir` (`agent_box.paths.passwd_home`). `$HOME` is never consulted, and neither is anything that consults it internally — `Path.home()` and `Path.expanduser()` both read `$HOME` first, so `~` in a setting like `XDG_DATA_HOME`, `CLAUDE_CONFIG_DIR`, `AGENT_BOX_WORKSPACE_ROOT`, or `--mount` is expanded against the passwd home explicitly. A poisoned `$HOME` therefore cannot relocate any of it, which matters because `--userns=keep-id` maps the *passwd* identity back to the host regardless of what a shell variable says.

Building the image requires that identity to be passed explicitly: the `Containerfile`'s `USER_NAME`/`USER_UID`/`USER_GID`/`USER_HOME` args have no defaults, and a build without them fails immediately with a message saying what to pass. `agent-box build` supplies all four.

## Workspace behavior

`agent-box` resolves a single project root and mounts it into the container **at the same absolute path it has on the host** — there is no fixed `/workspace` remap. The container's working directory mirrors your host working directory exactly, including any subdirectory:

```text
host:      /home/alex/code/infrado
cwd:       /home/alex/code/infrado/sub/dir

container: /home/alex/code/infrado   (mounted at the same path)
workdir:   /home/alex/code/infrado/sub/dir
```

Root resolution:

1. If you're inside a git repository/worktree, its top-level directory is the workspace root.
2. Otherwise, the current directory itself is the workspace root.
3. `AGENT_BOX_WORKSPACE_ROOT=/some/big/tree` overrides both — set it if you deliberately want a larger directory mounted; the current directory must be inside it.

Only the resolved root is mounted; unrelated host directories are never exposed implicitly.

### Mounting additional host paths (`--mount`)

Sometimes an agent needs to see something outside the project root — a sibling repository, a data directory, generated output elsewhere:

```bash
agent-box --mount ~/code claude                                # a parent directory containing the project
agent-box --mount /mnt/data/specs:ro codex                     # an unrelated directory, read-only
agent-box --mount /mnt/data/specs:ro --mount /srv/gen claude   # repeatable
```

Grammar: `agent-box [--mount PATH[:ro]]... [claude|codex|shell|doctor] [provider args...]`. Agent-box's own options must come *before* the subcommand; everything from the subcommand onward is passed through untouched, so `agent-box claude --model opus` is never misparsed, and a hypothetical Claude/Codex flag also spelled `--mount` is never mistaken for agent-box's. Use `--` to stop agent-box's own option parsing early.

Each `--mount` path must be an existing absolute directory; it's mounted at that same path, read-write by default or read-only with `:ro`. Paths (including ones with spaces) are resolved to their canonical, symlink-free form before use, both as the mount source and for overlap checking, so a mount can't be silently widened by a symlink.

Overlapping mounts (including the implicit project-root mount) resolve deterministically rather than being left to Podman's ordering:

- identical paths collapse to one;
- if one mount's path contains another's, only the broader one is kept — but only when they agree on read-only/read-write; a mismatch is rejected as ambiguous instead of silently picking a side.

Additional mounts are part of the workspace box's immutable configuration. If a live box was created with `--mount /foo`, a later command without `--mount` simply joins that existing box. A later command that explicitly requests a different mount set fails clearly instead of recreating the box and interrupting running work. Equivalent mount sets are order-independent.

The same rule applies to the rest of the box definition: the image, agent home/config paths, cache/state layout, and KVM/security settings cannot change underneath a live workspace box. If an invocation requests an incompatible box-level configuration, it fails rather than joining an environment other than the one requested; after an idle or stopped box is removed, the next launch creates one with the new configuration.

## Shared agent skills

`~/.agents/skills` is the canonical shared skill directory. Codex discovers skills there directly, while Claude Code discovers personal skills under `~/.claude/skills`.

Before starting a container, `agent-box` exposes each valid shared skill to Claude Code by creating a matching symlink:

```text
~/.claude/skills/systematic-debugging -> ~/.agents/skills/systematic-debugging
```

Contents are not copied, so both agents use the same `SKILL.md`. Existing Claude-specific skills are preserved: if `~/.claude/skills/<name>` already exists and doesn't point at the shared skill, agent-box leaves it alone and warns.

### What exactly gets mounted from `~/.agents`

If `~/.agents` exists, the **whole tree** is bind-mounted read-only at its own path, and agent-box walks **all of it** — not just `skills/` — for symlinks pointing outside the tree, mounting each distinct target read-only at its own path too.

That scope is deliberate. Since the tree itself is already visible to the container, narrowing the walk to `skills/` would hide nothing; it would just leave every non-skill symlink (a shared agent definition, a command, a prompt fragment) dangling inside a directory the container can already read.

Guarantees:

- Every one of these mounts is **read-only**, external targets included. agent-box exposes your shared agent material; it does not let the container rewrite your library.
- Symlinks pointing back inside `~/.agents` are skipped — the tree mount covers them.
- Each distinct target is mounted once, deterministically.
- A broken symlink warns and is skipped; it never fails the run.

If you keep something under `~/.agents` a container shouldn't read, move it out — the read-only tree mount is unconditional.

## Vikunja skill

The [`vikunja`](https://vikunja.io) skill drives a task tracker over its API v2. Its files travel into the container automatically as part of the `~/.agents` mount above, but a skill's *files* are not a working skill: its CLI has to be on `PATH`, its credentials have to be readable, and the server has to be reachable from inside a container's network namespace. `vikunja.enabled` (default **off**) is the one switch that arranges all three.

Turning it on exposes exactly this, all derived from what the host already has — agent-box stores no Vikunja settings of its own:

| What | Where in the container | Mode |
| --- | --- | --- |
| `~/.config/vikunja` — server URL and API token | `$HOME/.config/vikunja` | read-only |
| `~/.local/state/vikunja` — resumable import journals | `$HOME/.local/state/vikunja` | read-write |
| `~/.agents/skills/vikunja/scripts/vikunja.py` | `$HOME/.local/bin/vikunja` | read-only |

The CLI bind adds no exposure the `~/.agents` mount did not already grant; it only gives the script the name the skill's own documentation tells an agent to invoke (`vikunja tasks get …`, not a path).

**The loopback URL is rewritten.** A self-hosted Vikunja is almost always configured on the host as `http://127.0.0.1:3456`. Inside a container that address is the *container's* own loopback, so the skill would fail to connect with no useful hint. agent-box therefore forwards `VIKUNJA_URL` with only the host part replaced by `host.containers.internal`, which both pasta and slirp4netns write into the container's `/etc/hosts`; scheme, port, path, and any non-loopback host are left exactly as they are. Your host configuration is never modified — the same `~/.config/vikunja/config.json` keeps working outside agent-box.

Unlike a token, a URL is an endpoint rather than a credential, so it is passed by value (`--env=VIKUNJA_URL=…`) the way `GIT_CONFIG_GLOBAL` is. Do not embed basic-auth credentials in it; the skill authenticates with the token file instead.

**The token is the whole tracker.** `~/.config/vikunja/token` can read and rewrite every project and task the account can reach, which is why this defaults off and why the directory is mounted read-only. `agent-box config` shows what the container will actually reach, and says which half is missing when the skill, the token, or the URL is absent:

```text
Vikunja skill access        on (http://host.containers.internal:3456)
```

`agent-box doctor` answers the question that actually matters — whether a call gets through:

```text
vikunja url:         http://host.containers.internal:3456
vikunja token:       present
vikunja:             OK (admin)
```

Import journals are mounted read-write on purpose: a partially applied `vikunja import` is only resumable if its journal outlives the box.

## Git and GitHub

`git` and `gh` configuration directories are always mounted (so both tools work), but only *populated* from the host when you opt in — see the configuration table above. Every workspace box gets its own writable snapshot, seeded as a **copy**, not a bind mount, because `git`/`gh` rewrite config via temp-file-plus-rename, which fails on a bind-mounted file. A snapshot is prepared immediately before that workspace's new box is created and stays stable for its lifetime: later Claude/Codex/shell attaches never replace it, and creating another workspace box cannot alter it. The host stays the source of truth; anything written inside a box is discarded when that workspace's next box is created.

With `github.gh_config` off, the directory is empty and `gh` runs unauthenticated. With `github.gh_token` off (or `AGENT_BOX_GH_TOKEN=0`), no `GH_TOKEN` or `GITHUB_TOKEN` is forwarded.

`gh auth login` is not meant to run inside the container — authenticate on the host. With `github.gh_token` on, agent-box permits an already-exported `GH_TOKEN`/`GITHUB_TOKEN`, or reads `gh auth token` and places it only in the launcher environment before Podman is exec'd. In both cases Podman receives `--env=NAME`, never the token value in argv. Only HTTPS remotes are authenticated this way; the container has no SSH keys or agent socket.

## Flow-Next

agent-box owns deterministic Flow-Next *runtime policy* — model routing and push behavior — so individual projects no longer need to encode it in `AGENTS.md`. Flow-Next itself, its quality gates, and its review pipeline are untouched.

**Detection.** A workspace is Flow-Next when its root has a `.flow/config.json` file. Nothing is invoked and nothing is mutated to check this.

**Review backend routing.** Every Claude or Codex launch (container or `--host`) into a Flow-Next workspace gets `FLOW_REVIEW_BACKEND` set from `flow_next.review_backends`, keyed by the *primary* agent you launched — never by quota, cost, or any inferred signal:

```json
"flow_next": {
  "review_backends": {
    "claude": "codex:gpt-5.6-sol:medium",
    "codex":  "codex:gpt-5.6-sol:medium"
  }
}
```

Codex reviewing Codex is intentional for now: Flow-Next has no native, executable Claude review backend yet, so `claude`'s mapping also points at Codex until upstream adds one. Changing this later is a config edit (`agent-box config`), not a code change. Primary-agent selection stays entirely explicit — `agent-box claude`/`agent-box codex`; agent-box never balances between them.

**Auto-push (opt-in, Claude and Codex).** `flow_next.auto_push` (default **off**) reconciles Flow-Next state with Git at the end of every agent turn — Claude's `Stop` hook, Codex's turn-complete notify event — instead of trying to detect a `flowctl done` invocation while it happens:

1. the workspace is resolved from that turn's `cwd`, the same way the launcher resolves it;
2. nothing runs unless this launch has auto-push enabled and the workspace is Flow-Next (`.flow/config.json` present);
3. Git itself determines whether the current branch has commits `origin` doesn't have yet — the *unpushed range* — using the same fail-closed remote-safety chain as before: `origin` must resolve (via `git remote get-url --push --all`, so URL rewriting is honored) to **exactly one** effective push destination — never zero, never several, and never just assumed to be the same repository `origin` *fetches* from, since a separate `pushurl` can point somewhere else entirely — that destination's **current** default branch must be positively determined by one live, bounded query against it (a locally cached `origin/HEAD` is only ever a hint, never trusted on its own) and must not match the current branch, the branch must be attached (not detached) and not `main`/`master`, the worktree must be clean, and that destination's `refs/heads/<branch>` (from that same live query) must have nothing that isn't a fast-forward ancestor of HEAD;
4. only the paths that changed **within that unpushed range** are inspected for tracked Flow-Next task receipts (`.flow/tasks/<task-id>.md`), and each such task id is mechanically derived from its file path — never from a commit message or prose;
5. for each of those task ids, Flow-Next's own CLI is asked for the authoritative answer — `flowctl show <task-id> --json` — and the push proceeds only if **at least one** of them currently reports both `"status": "done"` **and** `"status_source": "flow-state"`. `status_source` is Flow-Next's own provenance field: `"flow-state"` means its runtime state itself answered, while `"committed"` means the answer only fell back to the tracked task snapshot on disk, which Flow-Next documents can be arbitrarily stale (for example right after a fresh or diff-scoped checkout that never ran Flow-Next's runtime state). A `"done"` status sourced from `"committed"`, or with `status_source` missing entirely, is never trusted — there is no fallback that treats missing provenance as good enough. A task file that changed but isn't authoritatively done, or a task that's done but whose file *didn't* change in this range, never authorizes anything by itself; a turn that legitimately completes several tasks needs only one authoritatively confirmed `done` among the ones it touched.

An undeterminable push destination or default branch, an unresolved Flowctl, a malformed Flowctl response, a non-authoritative `status_source`, or no confirmed completion in range always skips the push; none of these are ever treated as "probably safe" — a missed auto-push just means a manual `git push` later. There is no marker and no persistent state: every turn's completion event recomputes the answer from scratch, so it is safe for any number of independent Claude/Codex sessions running in different workspaces inside the same long-lived container — each is scoped only by its own event's `cwd` — and a later turn that finds nothing newly unpushed simply does nothing.

The literal command is `git push -u origin HEAD` — never `--force`, never another branch, remote, or tag, never a merge/rebase/reset to repair divergence — but safe argv alone doesn't guarantee safe *effective* behavior: `push.followTags`/`push.recurseSubmodules` can silently make a plain push also push unrelated tags or an entirely different (submodule) repository, so the push subprocess neutralizes both by passing `-c push.followTags=false -c push.recurseSubmodules=no` on git's own command line — applied to that one subprocess only, so the user's stored config is never modified. Command-line `-c` is the mechanism specifically because it is both *additive* and *highest-precedence*: it leaves every inherited `GIT_CONFIG_*` entry in place, so the push resolves `remote.origin.url`/`pushurl`/`insteadOf` from byte-identical configuration to the preflight queries (preflight and push therefore cannot disagree about which repository receives the push), while still outranking an inherited `GIT_CONFIG_PARAMETERS` that tries to force the safety settings back on.

**Resolving Flowctl.** Flow-Next's CLI is not assumed to be on `PATH` — upstream explicitly documents that a global `flowctl` may fail, since copy-less installs run it from the active plugin/CLI installation instead. agent-box resolves it once, at launch time, per agent: for Claude, from the `flow-next` plugin's own install-path record in `~/.claude/plugins/installed_plugins.json` (the same record Claude Code itself uses to set `$CLAUDE_PLUGIN_ROOT` for a running skill); for Codex, from `${CODEX_HOME:-~/.codex}/scripts/flowctl`. For every Claude or Codex launch in a Flow-Next workspace, the resolved executable's directory is prepended to that invocation's `PATH`, making the same executable available as bare `flowctl` in both container and `--host` mode. This is invocation-scoped, so concurrent Claude and Codex launches may use different installations without a shared symlink or global switch. Container launches prepend to agent-box's controlled container PATH, never the host PATH; host launches prepend to the inherited host PATH.

The resolved absolute executable is also forwarded as `AGENT_BOX_FLOWCTL` for the auto-push reconciler, independently of whether auto-push is enabled. `AGENT_BOX_FLOW_NEXT_AUTO_PUSH` only decides whether that reconciler runs. If Flowctl cannot be resolved, neither `AGENT_BOX_FLOWCTL` nor a Flowctl PATH entry is invented, and enabled auto-push safely skips.

`StopFailure` never pushes. Container auto-push over HTTPS reuses the existing `github.gh_token` credential forwarding: when it's enabled, the container's isolated git config also gets `credential.https://github.com.helper = !gh auth git-credential` (the same mechanism `gh auth setup-git` configures), so `gh` — already on the container's `PATH` — supplies the push credential from the forwarded `GH_TOKEN`. Host mode is unaffected either way; it always uses the host's own git credential configuration.

## Usage limits

```bash
agent-box usage codex
agent-box usage claude
agent-box usage all
```

The combined command prints both subscriptions on one line:

```text
Claude 97/88 · 4h51m/5d23h │ Codex 83/61 · 3h24m/4d11h
```

Both providers cache for 60 seconds, run directly on the host (no container), and take no arguments and no stdin: `agent-box usage` answers only "how much is left". Anything that depends on a live session — the context segment, the session's own rate limits — belongs to `agent-box statusline` (below), which is the command Claude Code actually calls.

Each segment of the **combined** line gets its own muted 256-colour hue — terracotta for Claude, steel blue for Codex (the status line adds violet for context). `agent-box usage claude` and `agent-box usage codex` print plain: the colours exist to separate segments from each other, and a single-provider invocation has nothing to separate. Set `NO_COLOR` to switch colour off entirely; agent-box never probes for a terminal, because a status-line command never has one.

## Status line

```bash
agent-box statusline
```

The one command to put in Claude Code's `statusLine`. It reads the JSON Claude Code pipes in on stdin and renders the whole bottom line:

```text
➜ [14:23] agent-box git:(main) ✗ │ ctx 31% · 62k/200k │ Claude 97/88 · 4h51m/5d23h │ Codex 83/61 · 3h24m/4d11h
```

`statusLine` accepts exactly one command, so the shell-prompt half (clock, virtualenv, directory, git branch and dirty marker) and the usage half have to be composed somewhere. Doing it here rather than in a wrapper script next to `settings.json` is what keeps it version-controlled, tested, and identical on the host and inside the container, where `agent-box` is mounted onto `PATH`. Every git call is capped at one second, so a slow repository loses its segment instead of stalling a line that redraws constantly.

The `ctx` segment is Claude Code's own accounting: it computes context consumption itself and ships it in the `context_window` field of that same payload, so agent-box neither parses a transcript nor spends session context to show it. The segment is absent before a session's first assistant message.

The Claude numbers come from the same payload when it carries `rate_limits`, which is fresher and cheaper than an HTTP call; the cached/OAuth path from `agent-box usage claude` is the fallback when it does not.

agent-box configures this for you. `claude.statusline` is on by default, and `agent-box claude` reconciles Claude's `statusLine` on every launch, in the same single settings write as the notification hooks:

```json
"statusLine": { "type": "command", "command": "agent-box statusline" }
```

It is the one integration that defaults on, because it exposes nothing: no credential leaves the host and nothing new reaches the container. What it costs is one line of Claude's own UI, which agent-box gives back the moment you turn the setting off in `agent-box config`.

**A status line you configured yourself is never overwritten.** agent-box fills the slot only when it is empty or already holds `agent-box statusline`, and switching the setting off removes only that exact command. `agent-box config show` says which of the three cases you are in. This is deliberately not a migration either: an older `agent-box usage all --statusline` counts as somebody else's line and is left exactly where it is.

## Configuration paths

`agent-box` respects `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_CACHE_HOME`. By default:

```text
~/.config/agent-box/config.json   the one agent-box config file (mode 0600)
~/.local/share/agent-box/         build markers, per-workspace git/gh snapshots, notify state
~/.claude/, ~/.codex/             agent auth and state (unchanged locations)
~/.claude/channels/telegram/      official Telegram plugin state (token, allowlist, inbox)
```

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honored. When `CLAUDE_CONFIG_DIR` is set, the relocated directory is bind-mounted at its host path *and* the resolved value is forwarded into the container, so `claude` there reads the same configuration instead of an empty `$HOME/.claude`.

Workspace containers are transient but shared: the first local agent command creates one, concurrent commands for the same resolved project execute inside it, and it disappears automatically once only agent-box's internal init/keeper processes remain. A tmux server holding a detached session is not one of those internal processes, which is exactly why a session outlives the terminal that started it — and why `agent-box stop` exists. Host-backed state above persists independently.

## Container hardening

Every workspace box gets `--userns=keep-id --cap-drop=all --security-opt=no-new-privileges`, an isolated container-only cache directory (never your real `~/.cache`), and only the mounts described above. A minimal init plus a small `/proc` keeper keeps the box alive while agent commands run through `podman exec`; its final idle decision takes the same short lifecycle lock as exec admission, so it cannot shut down an admitted session. Once only those internal processes remain, the keeper exits and Podman's `--rm` removes the box. There is exactly one container-level definition of what a box can see (`agent_box.runtime.container_lifetime_args`).

## Self-tests

```bash
agent-box selftest                     # fast, no Podman or network
python3 -m unittest discover -s tests  # full suite
```

The full suite covers config defaults and round-tripping, host-exposure gating (gh token/config, git config, provider env, Vikunja credentials), Vikunja loopback-URL rewriting and URL precedence, the Claude channel preflight, `--mount` parsing and overlap planning, workspace resolution, host identity, passwd-anchored `~` expansion under a poisoned `$HOME`, `~/.agents` mount scope, notification payload handling, launch-time legacy-hook migration, event-level official-channel routing, and hook install/remove, usage formatting, the Containerfile's identity-arg validation (executed through a real shell), Flow-Next workspace detection and review-backend routing (container and host mode, including stale-host-env sanitization), `--host` launch behavior, deterministic and invocation-scoped Flowctl availability for Claude (from the plugin install-path record) and Codex (`$CODEX_HOME/scripts/flowctl`), the fail-closed remote-default-branch and actual-push-target guard chain — including a *stale locally cached* `origin/HEAD` that disagrees with the remote's current actual default branch, a fetch URL that differs from the effective push URL (including URL rewriting and multiple push URLs), `push.followTags`/`push.recurseSubmodules` neutralization proven against a real submodule and a real annotated tag — including against a hostile *inherited* `GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_COUNT` environment that tries to force them back on or to redirect `origin`'s push URL between preflight and push, plus Flow-Next completion reconciliation (a changed-but-not-done task file never authorizes a push, a `"done"` status sourced from `status_source: "committed"` — or with no `status_source` at all — never authorizes a push either, a done task whose file didn't change in the unpushed range never authorizes a push, a completion followed by further commits — e.g. a receipt commit then a plan-sync commit — still pushes the final HEAD, and two independent workspaces' completions never cross-authorize each other) — and a real rejected `git push` against a bare remote with a rejecting `pre-receive` hook — all against real temporary git repositories and bare remotes; nested-directory and linked-worktree Flow-Next lifecycle handling; mixed-entry Claude hook ownership (agent-box removes only the handler it owns from an entry that also carries a user's own handler, leaving the entry and the user's handler intact); and the generated git credential-helper configuration for container auto-push.

No credentials are required and no network calls are made.

## Diagnostics

```bash
agent-box doctor
```

Checks both agents, Bun, development tools, QEMU, KVM, shared agent skills, and whether the Telegram channel has a token — all from inside the container.

## KVM

When `/dev/kvm` is accessible it is passed through automatically. Disable with `AGENT_BOX_KVM=0`. ARM64 QEMU remains available through TCG regardless.

## Troubleshooting

- **`podman not found`** — install Podman; agent-box does not fall back to Docker.
- **`current directory is not inside the configured workspace root`** — you set `AGENT_BOX_WORKSPACE_ROOT` and `cd`'d outside it.
- **`--mount` rejects a path** — it must be an existing absolute directory; a relative path, a file, or a not-yet-created directory is rejected rather than silently mounted.
- **`the Claude Telegram channel is enabled ... but`** — run `agent-box config check` and follow the printed steps, or turn the channel off with `agent-box config`.
- **Telegram is silent even though the channel is on** — first make sure another Claude session is not reported as the active owner; then pair the plugin user with `/telegram:access pair <code>`. On managed Teams/Enterprise accounts, channels also need an administrator to enable them.
- **Claude/Codex not authenticated in the container** — authenticate on the host first; the container mounts `~/.claude` and `~/.codex` rather than managing its own credentials.
- **`gh` is unauthenticated in the container** — that's the default. Turn on `github.gh_token` (and optionally `github.gh_config`) in `agent-box config`.
- **Shared skill shows MISSING/MISMATCH in `agent-box doctor`** — a `~/.claude/skills/<name>` entry already exists and doesn't point at the shared skill; remove or rename it if you want it linked.

## Known limitations

- `~/.agents` is mounted read-only in full, and every external symlink target under it too. Deliberate, but it does mean the container can read anything you keep there, not only skills.
- The official Telegram plugin is outside agent-box's control. Its current global-plugin startup behavior requires the empty-token isolation above; a plugin change to its environment precedence or state layout may require an agent-box update.
- `--channels` is verified against Claude Code 2.1.239.
- There is no remote equivalent of `--mount`, and no way to start a container from Telegram. The channel attaches to a session you started locally.

## Migration from older agent-box versions

**The custom Telegram remote-control subsystem has been removed entirely, with no compatibility layer.** If you used it, these are gone: `agent-box telegram serve`, the Telegram daemon, Telegram-created Claude/Codex sessions, session routing/resume/ownership, the custom `AskUserQuestion` bridge, the Codex `requestUserInput` bridge, and remote-control session persistence. Use the official Claude Telegram plugin (above) instead.

- **`~/.config/agent-box/telegram.json` is not read.** Configuration now lives in `~/.config/agent-box/config.json` and there is no migration — run `agent-box config` once to set things up again. Delete the old file.
- **Command changes:** `agent-box notify setup|status|disable` → `agent-box config`. `agent-box telegram *` → gone. `agent-box notify test|claude|codex` still exist (the last two are the hook entry points).
- **The GitHub token is no longer forwarded by default.** It used to travel whenever `gh` was authenticated on the host; now it requires `github.gh_token` in `agent-box config`. Same for copying `~/.config/gh`, which is now `github.gh_config` and also off by default.
- **Legacy notify hooks are cleaned up.** The next normal Claude session removes old `~/.local/bin/agent-box notify claude` entries and installs the PATH-based hook; `agent-box config` does the same.
- The `statusLine` auto-migration is gone, and nothing rewrites an existing command in place. If yours still points at an old one, clear it (or set it to `agent-box statusline` yourself) and agent-box will keep it current from then on.
- **`agent-box usage <provider> --statusline` is gone.** The flag existed back when `agent-box usage all --statusline` was the configured status line; `agent-box statusline` replaced that, and the flag had no callers left. `agent-box usage` now rejects arguments after the provider.
- `podman build` on the `Containerfile` requires explicit `USER_NAME`/`USER_UID`/`USER_GID`/`USER_HOME` args — use `agent-box build`, or pass all four yourself.
- The mandatory `~/code` workspace requirement is gone; set `AGENT_BOX_WORKSPACE_ROOT=~/code` if you want the old "one big mounted tree" behavior.
