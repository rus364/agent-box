# agent-box

`agent-box` runs Codex CLI and Claude Code inside the same rootless Podman development container.

The container mounts the host `~/code` tree at the same logical path, preserves agent configuration and authentication, provides the development tools used by the projects in that tree, and can pass through KVM when available.

## Requirements

- Linux
- Python 3.10+
- Podman
- `~/code` must exist (it may be a symlink)

The launcher itself uses only the Python standard library. There are no Python package dependencies.

## Install the launcher

Clone the repository somewhere under `~/code`, then expose the launcher on `PATH`:

```bash
ln -s ~/code/agent-box/agent-box ~/.local/bin/agent-box
```

If the repository lives at another path under `~/code`, adjust the source path accordingly.

## Build

```bash
agent-box build
```

The image is also built automatically on first use.

Image management commands:

```bash
agent-box build    # regular cached build
agent-box update   # refresh frequently updated tools while preserving base layers
agent-box rebuild  # refresh tools and rebuild without cache
```

The default image is `localhost/agent-box:latest`. Override it with `AGENT_BOX_IMAGE`.

## Run Claude Code

```bash
agent-box
agent-box claude
agent-box claude -p "summarize this repository"
```

Claude Code is the default agent. To change the default:

```bash
export AGENT_BOX_AGENT=codex
```

Claude Code is installed from the official `@anthropic-ai/claude-code` npm package. Its `~/.claude` state is mounted from the host so authentication, settings, and history survive disposable container runs.

If `~/.claude.json` already exists on the host, it is mounted as well.

The launcher also forwards common Anthropic/provider variables when they are already set on the host, including `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, Bedrock/Vertex selectors, and common HTTP/TLS proxy variables.

## Run Codex

```bash
agent-box codex
agent-box codex exec "inspect this repository"
```

## Shared agent skills

`~/.agents/skills` is the canonical shared skill directory. Codex discovers skills there directly, while Claude Code discovers personal skills under `~/.claude/skills`.

Before starting a container, `agent-box` exposes each valid shared skill to Claude Code by creating a matching symlink:

```text
~/.agents/skills/systematic-debugging
        -> ~/.claude/skills/systematic-debugging
```

The skill contents are not copied. Both agents therefore use the same `SKILL.md` and supporting files.

Existing Claude-specific skills are preserved. If `~/.claude/skills/<name>` already exists and does not point to the corresponding shared skill, `agent-box` leaves it unchanged and prints a warning instead of overwriting it.

Shared skills may themselves be symlinks to directories elsewhere on the host. `agent-box` mounts those resolved targets read-only into the container so the links remain valid for both Codex and Claude Code.

`agent-box doctor` verifies both the original `~/.agents/skills` links and their Claude Code counterparts.

## Git and GitHub

`git` and `gh` configuration is reseeded from the host on every start into private container state:

```text
~/.gitconfig, ~/.config/git   -> container ~/.config/git   (GIT_CONFIG_GLOBAL)
~/.config/gh                  -> container ~/.config/gh     (GH_CONFIG_DIR)
```

These are directories rather than bind-mounted files on purpose: `git` and `gh` rewrite their configuration through a temporary file plus rename, and renaming over a bind-mounted file fails with `Device or resource busy`. The host remains the source of truth, and configuration written inside the container is discarded at the next start.

`gh auth login` is not meant to be run inside the container — the token lives in the host keyring, and the container is disposable. Authenticate on the host instead:

```bash
gh auth login
```

At startup the launcher reads `gh auth token` on the host and passes it in as `GH_TOKEN`, which authenticates both `gh` and the `gh auth git-credential` helper used by `git` over HTTPS. An already exported `GH_TOKEN`/`GITHUB_TOKEN` is forwarded as-is and takes precedence. Disable the passthrough with:

```bash
AGENT_BOX_GH_TOKEN=0 agent-box codex
```

Only HTTPS remotes are authenticated this way; the container has no SSH keys or agent socket, so `git@github.com:` remotes will fail.

## Usage limits

Usage reporting is built into `agent-box`; there are no separate helper scripts:

```bash
agent-box usage codex
agent-box usage claude
agent-box usage all
```

The combined command prints both subscriptions on one line, making it convenient for direct use in a terminal:

```text
Claude 97/88 · 4h51m/5d23h │ Codex 83/61 · 3h24m/4d11h
```

Both providers use a 60-second cache to avoid unnecessary polling. The commands run directly on the host and do not start a Podman container.

Claude Code's `statusLine` uses the same combined output through:

```bash
agent-box usage all --statusline
```

In status-line mode, Claude usage normally comes from the payload that Claude Code provides on stdin. If the payload is absent or does not contain rate-limit data, `agent-box` falls back to the regular cached/OAuth Claude usage path. Codex usage uses the same cached subscription query as `agent-box usage codex`.

When `agent-box` sees either the old `~/.local/bin/claude-usage --statusline` command or the previous `~/.local/bin/agent-box usage claude --statusline` command in `~/.claude/settings.json`, it migrates it automatically to the combined command while preserving the rest of the settings file.

## Telegram notifications

`agent-box` can send Telegram notifications from both Claude Code and Codex.

Create a bot with BotFather, start a chat with it, then run:

```bash
agent-box notify setup
```

The setup asks for the bot token and Telegram chat ID, sends a test message, and configures Claude Code hooks automatically. The token and chat ID are stored locally in:

```text
~/.config/agent-box/telegram.json
```

The file is created with mode `600`. When notification configuration exists, it is mounted read-only into the agent container.

Claude Code notifications cover permission prompts, idle prompts, completed turns, background work, and stop failures. Notifications use Telegram HTML formatting and status-specific emoji.

Claude Code can emit an `idle_prompt` while the parent turn is idle but background agents are still running. To avoid falsely reporting that human input is required, `agent-box` records the `background_tasks` supplied by Claude Code's `Stop` hook per `session_id`. While that mechanically reported background-task state is active, `idle_prompt` notifications for the same session are suppressed. Permission prompts and failures are never suppressed. The next `Stop` with no background tasks clears the state and sends the normal completed-turn notification. Stale state expires after 30 minutes as a safety fallback.

Typical Claude notifications look like:

```text
🔐 Claude · project
Permission required

⚙️ Claude · project
Background work continues · 1 task

💬 Claude · project
Waiting for input

✅ Claude · project
Turn finished

❌ Claude · project
Stopped with an error
```

Codex receives an `agent-turn-complete` notifier automatically when launched through `agent-box` and uses the same formatted Telegram style.

Useful commands:

```bash
agent-box notify test
agent-box notify status
agent-box notify disable
```

`notify disable` removes the Telegram configuration, background-notification state, and only the Claude hooks installed by `agent-box`; unrelated Claude settings and hooks are preserved.

## Self-tests

The small pure-logic test suite is embedded in the launcher itself:

```bash
agent-box selftest
```

It checks duration formatting, Claude/Codex usage parsing, notification payload handling, Telegram formatting, and background-task state without starting Podman or accessing the network. There is no separate test framework or test file.

## Shell and diagnostics

Open a shell in the same container environment:

```bash
agent-box shell
```

Check both agents, development tools, QEMU, KVM, and shared agent skills:

```bash
agent-box doctor
```

## KVM

When `/dev/kvm` is accessible, it is passed through automatically. Disable passthrough explicitly with:

```bash
AGENT_BOX_KVM=0 agent-box codex
```

ARM64 QEMU remains available through TCG.

## Persistent host state

The launcher uses:

```text
~/.local/share/agent-box/
~/.codex/
~/.claude/
```

Container runs themselves are disposable (`podman run --rm`).

## Migration from codex-box

The launcher, image, hostname, environment variables, and persistent state directory were renamed from `codex-box` to `agent-box`:

```text
codex-box                         -> agent-box
CODEX_BOX_IMAGE                   -> AGENT_BOX_IMAGE
CODEX_BOX_KVM                     -> AGENT_BOX_KVM
localhost/codex-box:latest        -> localhost/agent-box:latest
~/.local/share/codex-box/         -> ~/.local/share/agent-box/
```

The old `codex-box` launcher is intentionally not kept in the repository.
