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

The image contains Claude Code, Codex CLI, Node 22, **Bun** (required by the official Telegram channel plugin), Python + uv, mise, git/git-lfs/gh, kubectl/helm/yq/jq, QEMU (x86-64 and aarch64), and the usual build and network tooling.

## Run Claude Code or Codex

```bash
cd /any/path/to/your/project
agent-box            # default agent (claude)
agent-box claude
agent-box claude -p "summarize this repository"
agent-box codex
agent-box codex exec "inspect this repository"
agent-box shell      # a shell inside this workspace's shared agent-box container
```

This works from **any** directory — there is no requirement to keep projects under a fixed host directory such as `~/code`.

Claude Code is the default agent; change it with `export AGENT_BOX_AGENT=codex`.

The first command in a project creates its box lazily. Later `claude`, `codex`, and `shell` commands from that project use `podman exec` in the same process namespace, so a shell can see running agents and daemons. A different workspace gets a different box. The box is removed automatically after no useful workloads remain; no normal `up`/`down` lifecycle is required. This also lets a Codex remote-control/app-server process survive after its launcher command exits. Their `~/.claude` and `~/.codex` state directories are still mounted from the host, so authentication, settings, and history persist after the transient box is removed. If `~/.claude.json` exists on the host it is mounted too. For `agent-box codex`, MCP commands under `/usr/lib/chatgpt/resources/` are disabled only for that invocation because those ChatGPT desktop resources are not present in the image; the host Codex configuration is never modified.

### Host mode (`--host`)

```bash
agent-box --host claude
agent-box --host codex
```

Runs the installed host `claude`/`codex` executable directly in the resolved workspace — no Podman, no container hardening, your normal host git/gh credentials. Pass-through arguments and the real exit status work exactly as in container mode. `agent-box claude`/`agent-box codex` (without `--host`) keep meaning containerized execution; primary-agent choice is always explicit either way — agent-box never picks or balances between Claude and Codex for you.

Container mode and host mode resolve Flow-Next detection and the `FLOW_REVIEW_BACKEND`/auto-push policy through the exact same function, so the two cannot drift apart (see below).

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
  "claude":        { "telegram_channel": false },
  "notifications": { "enabled": false, "telegram_token": "", "telegram_chat_id": "" },
  "github":        { "gh_token": false, "gh_config": false },
  "git":           { "identity": true, "full_config": false },
  "providers":     { "api_keys": false, "transport": true },
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
| `notifications.enabled` | **off** | Outbound-only Telegram pings; needs a token and chat id to become active |
| `github.gh_token` | **off** | Permits an exported token, or obtains `gh auth token`, and forwards it as `GH_TOKEN` |
| `github.gh_config` | **off** | Copies the host's `~/.config/gh` (which stores that token) into the container |
| `git.identity` | **on** | Seeds only global `user.name` and `user.email` for commit attribution |
| `git.full_config` | **off** | Copies the complete host Git configuration; opt in only if you accept its credential helpers, headers, includes, and rewrites being exposed |
| `providers.api_keys` | **off** | Forwards exported `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` |
| `providers.transport` | **on** | Forwards endpoint, Bedrock/Vertex, proxy, and CA transport settings (turn off if your proxy URL carries credentials) |
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

Remote control and notifications are separate features. Notifications are strictly **one-directional**: agent-box only sends Telegram messages. There is no polling daemon, no `getUpdates`, no callbacks, and no way to drive a session from a notification. Codex completion reports use Telegram Rich Markdown when available, with a safe plain-message fallback for older or incompatible Bot API deployments.

```bash
agent-box config        # enable, with bot token + chat id
agent-box notify test   # send a test message
```

What gets sent:

- **Codex** — turn completed, with the last assistant message.
- **Claude** — waiting for input, permission required, turn finished, stopped with an error.

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

**Resolving Flowctl.** Flow-Next's CLI is not assumed to be on `PATH` — upstream explicitly documents that a global `flowctl` may fail, since copy-less installs run it from the active plugin/CLI installation instead. agent-box resolves it once, at launch time, per agent: for Claude, from the `flow-next` plugin's own install-path record in `~/.claude/plugins/installed_plugins.json` (the same record Claude Code itself uses to set `$CLAUDE_PLUGIN_ROOT` for a running skill); for Codex, from `${CODEX_HOME:-~/.codex}/scripts/flowctl`. The resolved absolute path is forwarded into the launch as `AGENT_BOX_FLOWCTL`, alongside `AGENT_BOX_FLOW_NEXT_AUTO_PUSH`; if Flowctl can't be resolved, auto-push simply skips rather than guessing at a location.

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

Both providers cache for 60 seconds and run directly on the host (no container). Claude Code's `statusLine` can use `agent-box usage all --statusline`.

## Configuration paths

`agent-box` respects `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_CACHE_HOME`. By default:

```text
~/.config/agent-box/config.json   the one agent-box config file (mode 0600)
~/.local/share/agent-box/         build markers, per-workspace git/gh snapshots, notify state
~/.claude/, ~/.codex/             agent auth and state (unchanged locations)
~/.claude/channels/telegram/      official Telegram plugin state (token, allowlist, inbox)
```

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honored. When `CLAUDE_CONFIG_DIR` is set, the relocated directory is bind-mounted at its host path *and* the resolved value is forwarded into the container, so `claude` there reads the same configuration instead of an empty `$HOME/.claude`.

Workspace containers are transient but shared: the first local agent command creates one, concurrent commands for the same resolved project execute inside it, and it disappears automatically once only agent-box's internal init/keeper processes remain. Host-backed state above persists independently.

## Container hardening

Every workspace box gets `--userns=keep-id --cap-drop=all --security-opt=no-new-privileges`, an isolated container-only cache directory (never your real `~/.cache`), and only the mounts described above. A minimal init plus a small `/proc` keeper keeps the box alive while agent commands run through `podman exec`; its final idle decision takes the same short lifecycle lock as exec admission, so it cannot shut down an admitted session. Once only those internal processes remain, the keeper exits and Podman's `--rm` removes the box. There is exactly one container-level definition of what a box can see (`agent_box.runtime.container_lifetime_args`).

## Self-tests

```bash
agent-box selftest                     # fast, no Podman or network
python3 -m unittest discover -s tests  # full suite
```

The full suite covers config defaults and round-tripping, host-exposure gating (gh token/config, git config, provider env), the Claude channel preflight, `--mount` parsing and overlap planning, workspace resolution, host identity, passwd-anchored `~` expansion under a poisoned `$HOME`, `~/.agents` mount scope, notification payload handling, launch-time legacy-hook migration, event-level official-channel routing, and hook install/remove, usage formatting, the Containerfile's identity-arg validation (executed through a real shell), Flow-Next workspace detection and review-backend routing (container and host mode, including stale-host-env sanitization), `--host` launch behavior, deterministic Flowctl resolution for Claude (from the plugin install-path record) and Codex (`$CODEX_HOME/scripts/flowctl`), the fail-closed remote-default-branch and actual-push-target guard chain — including a *stale locally cached* `origin/HEAD` that disagrees with the remote's current actual default branch, a fetch URL that differs from the effective push URL (including URL rewriting and multiple push URLs), `push.followTags`/`push.recurseSubmodules` neutralization proven against a real submodule and a real annotated tag — including against a hostile *inherited* `GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_COUNT` environment that tries to force them back on or to redirect `origin`'s push URL between preflight and push, plus Flow-Next completion reconciliation (a changed-but-not-done task file never authorizes a push, a `"done"` status sourced from `status_source: "committed"` — or with no `status_source` at all — never authorizes a push either, a done task whose file didn't change in the unpushed range never authorizes a push, a completion followed by further commits — e.g. a receipt commit then a plan-sync commit — still pushes the final HEAD, and two independent workspaces' completions never cross-authorize each other) — and a real rejected `git push` against a bare remote with a rejecting `pre-receive` hook — all against real temporary git repositories and bare remotes; nested-directory and linked-worktree Flow-Next lifecycle handling; mixed-entry Claude hook ownership (agent-box removes only the handler it owns from an entry that also carries a user's own handler, leaving the entry and the user's handler intact); and the generated git credential-helper configuration for container auto-push.

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
- The `statusLine` auto-migration is gone; if yours still points at an old command, set it to `agent-box usage all --statusline`.
- `podman build` on the `Containerfile` requires explicit `USER_NAME`/`USER_UID`/`USER_GID`/`USER_HOME` args — use `agent-box build`, or pass all four yourself.
- The mandatory `~/code` workspace requirement is gone; set `AGENT_BOX_WORKSPACE_ROOT=~/code` if you want the old "one big mounted tree" behavior.
