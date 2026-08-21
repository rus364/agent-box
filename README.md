# agent-box

`agent-box` runs Codex CLI and Claude Code inside the same rootless Podman development container.

It mounts your current project into the container, preserves agent configuration and authentication across disposable runs, provides development tools, and can pass through KVM when available. It also has optional Telegram integration: outbound notifications, and an opt-in bidirectional remote-control mode that lets you drive Claude Code or Codex sessions from Telegram.

## Requirements

- Linux
- Python 3.10+
- Podman

The launcher itself uses only the Python standard library. There are no Python package dependencies, on the host or for the Telegram remote-control daemon.

## Install

Clone the repository anywhere — it does not need to live inside your projects tree:

```bash
git clone <this repo> ~/.local/src/agent-box
```

Then put the launcher on `PATH`:

```bash
ln -s ~/.local/src/agent-box/agent-box ~/.local/bin/agent-box
```

(Any directory on `PATH` works; adjust to your setup. If `~/.local/bin` isn't on `PATH` yet, add it.)

## Build

```bash
agent-box build
```

The image is also built automatically on first use.

```bash
agent-box build    # regular cached build
agent-box update   # refresh frequently updated tools while preserving base layers
agent-box rebuild  # refresh tools and rebuild without cache
```

The default image is `localhost/agent-box:latest`. Override it with `AGENT_BOX_IMAGE`.

## Run Claude Code or Codex

```bash
cd /any/path/to/your/project
agent-box            # default agent (claude)
agent-box claude
agent-box claude -p "summarize this repository"
agent-box codex
agent-box codex exec "inspect this repository"
agent-box shell      # a plain shell in the same container environment
```

This works from **any** directory — there is no requirement to keep projects under a fixed host directory such as `~/code`.

Claude Code is the default agent. To change it:

```bash
export AGENT_BOX_AGENT=codex
```

Claude Code is installed from `@anthropic-ai/claude-code`, Codex from `@openai/codex`. Their `~/.claude` and `~/.codex` state directories are mounted from the host so authentication, settings, and history survive disposable container runs. If `~/.claude.json` exists on the host it is mounted too.

Common Anthropic/provider environment variables are forwarded when already set on the host: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, Bedrock/Vertex selectors, and common HTTP/TLS proxy variables.

## Workspace behavior

`agent-box` resolves a single workspace root for the current invocation and mounts it at `/workspace` inside the container, preserving your working directory relative to that root:

1. If you're inside a git repository/worktree, its top-level directory is the workspace root.
2. Otherwise, the current directory itself is the workspace root.
3. `AGENT_BOX_WORKSPACE_ROOT=/some/big/tree` overrides both of the above — set it if you deliberately want a larger directory (e.g. a legacy `~/code` tree containing many projects) mounted, and the current directory must be inside it.

Only the resolved root is exposed to the container; arbitrary unrelated host directories are never mounted implicitly.

The container's internal user is a fixed, neutral `agentbox` (home `/home/agentbox`) — not derived from your host username — so the image and mounted state stay portable across machines. Host UID/GID mapping (`--userns=keep-id`) still applies, so files created in the container are owned by you on the host.

## Shared agent skills

`~/.agents/skills` is the canonical shared skill directory. Codex discovers skills there directly, while Claude Code discovers personal skills under `~/.claude/skills`.

Before starting a container, `agent-box` exposes each valid shared skill to Claude Code by creating a matching symlink:

```text
~/.claude/skills/systematic-debugging -> ~/.agents/skills/systematic-debugging
```

The skill contents are not copied. Both agents therefore use the same `SKILL.md` and supporting files. Existing Claude-specific skills are preserved: if `~/.claude/skills/<name>` already exists and does not point at the corresponding shared skill, `agent-box` leaves it unchanged and prints a warning.

Shared skills may themselves be symlinks to directories elsewhere on the host; `agent-box` mounts those resolved targets read-only (at their real host path) so the links stay valid for both agents inside the container.

`agent-box doctor` verifies both the `~/.agents/skills` links and their Claude Code counterparts.

## Git and GitHub

`git` and `gh` configuration is reseeded from the host on every start into private container state (a copy, not a bind mount, since `git`/`gh` rewrite config via temp-file-plus-rename, which fails on a bind-mounted file). The host remains the source of truth; anything written inside the container is discarded at the next start.

`gh auth login` is not meant to run inside the container — authenticate on the host instead:

```bash
gh auth login
```

At startup, `agent-box` reads `gh auth token` on the host and passes it in as `GH_TOKEN`, authenticating both `gh` and the `gh auth git-credential` helper used by `git` over HTTPS. An already-exported `GH_TOKEN`/`GITHUB_TOKEN` takes precedence. Disable the passthrough with `AGENT_BOX_GH_TOKEN=0`. Only HTTPS remotes are authenticated this way; the container has no SSH keys or agent socket.

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

Both providers cache for 60 seconds and run directly on the host (no container). Claude Code's `statusLine` can use the same combined output via `agent-box usage all --statusline`.

## Configuration paths

`agent-box` respects `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_CACHE_HOME`. By default:

```text
~/.config/agent-box/telegram.json    Telegram bot token + settings (mode 0600)
~/.local/share/agent-box/            build markers, seeded git/gh config, notify state, Telegram session state
~/.codex/, ~/.claude/                agent auth and state (unchanged locations)
```

Override the state/config roots directly with `AGENT_BOX_STATE_DIR` / `AGENT_BOX_CONFIG_DIR` if needed. Container runs themselves are disposable (`podman run --rm`); only these host paths persist.

## Telegram notifications

`agent-box` can send Telegram notifications from both Claude Code and Codex — this is separate from, and does not require, remote control (below).

Create a bot with BotFather, start a chat with it, then run:

```bash
agent-box notify setup
```

This asks for the bot token and a chat ID, sends a test message, and installs Claude Code hooks automatically.

```bash
agent-box notify test
agent-box notify status
agent-box notify disable
```

`notify disable` removes the Telegram configuration, background-notification state, and only the hooks `agent-box` installed; unrelated Claude settings/hooks are preserved.

Claude Code notifications cover permission prompts, idle prompts, completed turns, background work, and stop failures. Codex receives an `agent-turn-complete` notifier automatically when launched through `agent-box`. Claude Code can emit an `idle_prompt` while background agents are still running; `agent-box` tracks per-session background-task state from the `Stop` hook and suppresses the false "waiting for input" notification while background work is active (permission prompts and failures are never suppressed; stale state expires after 30 minutes as a safety net).

If you later configure remote control, notifications also link back to the relevant Telegram session where one is registered.

## Telegram remote control

Remote control turns the same Telegram bot into a bidirectional interface: you can start Claude Code or Codex sessions from Telegram, chat with them, and answer their structured questions with inline buttons.

**It is disabled by default and requires explicit setup.** Configure it after `agent-box notify setup` has stored a bot token:

```bash
agent-box telegram roots add ~/code          # an allowed project root (repeatable)
agent-box telegram allow add <your_telegram_user_id>
agent-box telegram enable
agent-box telegram serve                     # foreground long-polling daemon
```

Run `agent-box telegram serve` as a long-lived process (e.g. a systemd `--user` service, tmux/screen session, or similar). It uses `getUpdates` long polling — no webhook, no public HTTP endpoint, no inbound port.

### Security model

- Remote control is off until you explicitly enable it, independent of notification setup.
- Only Telegram user IDs on the configured allowlist get any response; unknown users receive no information, not even an error.
- Projects are chosen from buttons built from configured `workspace_roots` only — a Telegram user can never type or select an arbitrary host path.
- There is no `/shell` command, no generic "run this host command" action, and callback data never carries raw agent arguments — every button maps to a short opaque token validated server-side against the session/user/request it belongs to.
- Answering an already-resolved or expired question fails closed; buttons are invalidated after use.
- The container itself keeps its existing hardening (`--cap-drop=all`, `--security-opt=no-new-privileges`) for normal `agent-box claude`/`codex` runs. Remote-controlled sessions run the agent CLI directly on the host (see "Design note" below) rather than inside that container, because the daemon needs direct control of the agent process's stdio; the security boundary there is the allowlist plus configured project roots plus the agent's own permission mode, not container isolation.
- `telegram.json` (token, allowlist, workspace roots) stays mode `0600`. Session/pending-question state files are also written `0600` and never contain the bot token or agent credentials.
- Logging never includes the bot token, provider credentials, or full prompt payloads.

### Using it from Telegram

```text
/start      main menu (Sessions, New session, Usage, Refresh)
/sessions   list your sessions
/new        create a session: pick Claude or Codex, then a project
/status     quick summary
/usage      Claude + Codex usage line
```

Once a session exists, plain messages you send are delivered to it as the next turn. Each session has `Open`, `Interrupt`, `Stop`, and (once stopped) `Resume` buttons — only actions the provider actually confirmed are reported as having happened.

When a provider asks a structured question with options (Codex's `request_user_input`, Claude's `AskUserQuestion`), it's rendered as inline buttons; tapping one submits the corresponding structured answer.

### Session persistence

Sessions are recorded in `~/.local/share/agent-box/telegram/sessions.json` (JSON, atomic writes, no secrets): local session ID, provider, provider session/thread ID, host project path, chat ID, status, and timestamps. This survives a daemon restart. Whether a session can actually be *resumed* after a restart depends on the provider — see below.

### Claude vs. Codex capabilities

Both adapters use each tool's supported machine interface — Codex's `app-server` JSON-RPC protocol (verified against the schema the installed `codex app-server generate-json-schema` emits) and Claude Code's `--input-format stream-json --output-format stream-json`. Neither scrapes terminal output, drives a TUI, or reads a provider's private session files.

| | Codex | Claude Code |
|---|---|---|
| Structured questions → buttons | Yes (`item/tool/requestUserInput`) | Best-effort (`AskUserQuestion` tool call answered via a `tool_result` message) |
| Interrupt a running turn | Yes (`turn/interrupt`) | Not currently supported over stream-json input; reported honestly as unsupported rather than faking success |
| Resume after daemon restart | Yes, via `thread/resume` | Only via `--resume`, which starts a **new** process — Claude session identity is bound to the process that opened it with `--session-id`, there is no supported way to rebind a running session to a fresh process |

For Claude, if a question can't be confirmed as delivered as a structured `tool_result`, or you simply prefer to answer in your own words, plain free text sent to the session is always a safe fallback — it's delivered as the next ordinary user turn, which Claude will still treat as an answer.

## Self-tests

```bash
agent-box selftest
```

A fast, dependency-free smoke test (usage formatting, notification payload handling, background-task state, config migration, workspace resolution) with no Podman/network access. The full test suite lives under `tests/` and covers the rest (session registry, callback validation, project allowlisting, Telegram HTML escaping, and Codex/Claude protocol event translation against recorded fixture payloads):

```bash
python3 -m unittest discover -s tests
```

No real Telegram, Anthropic, or OpenAI credentials are required; network calls are mocked.

## Diagnostics

```bash
agent-box doctor
```

Checks both agents, development tools, QEMU, KVM, and shared agent skills inside the container.

## KVM

When `/dev/kvm` is accessible, it is passed through automatically. Disable explicitly with `AGENT_BOX_KVM=0`. ARM64 QEMU remains available through TCG regardless.

## Troubleshooting

- **`podman not found`** — install Podman; `agent-box` does not fall back to Docker.
- **`current directory is not inside the configured workspace root`** — you set `AGENT_BOX_WORKSPACE_ROOT` and `cd`'d outside it; `cd` back inside, or unset the override.
- **Claude/Codex not authenticated inside the container** — authenticate on the host first (`claude` / `codex login` outside `agent-box`, or `gh auth login` for GitHub); the container mounts `~/.claude`, `~/.codex`, and GitHub config from the host rather than managing its own credentials.
- **Shared skill shows as MISSING/MISMATCH in `agent-box doctor`** — a `~/.claude/skills/<name>` entry already exists and doesn't point at the shared skill; remove or rename it if you want it linked, otherwise it's left alone intentionally.
- **Telegram remote control does nothing** — check `agent-box telegram status`: it needs a configured token, `remote control: enabled`, at least one allowed user ID, and at least one workspace root.

## Migration from older agent-box versions

- The container's internal user changed from a host-username-derived account to the neutral `agentbox` (`/home/agentbox`). Nothing to do on your side; existing host-side auth/config directories are unaffected since they're bind-mounted by content, not by container username.
- The mandatory `~/code` workspace requirement is gone. If you relied on it implicitly, set `AGENT_BOX_WORKSPACE_ROOT=~/code` to keep the old "one big mounted tree" behavior, or simply `cd` into individual projects as needed.
- `telegram.json`'s old two-key notification-only schema (`token`, `chat_id`) is read transparently; nothing breaks, and remote control stays off until you explicitly configure and enable it.

### Migration from codex-box (pre-agent-box)

The launcher, image, hostname, environment variables, and persistent state directory were renamed from `codex-box` to `agent-box`:

```text
codex-box                         -> agent-box
CODEX_BOX_IMAGE                   -> AGENT_BOX_IMAGE
CODEX_BOX_KVM                     -> AGENT_BOX_KVM
localhost/codex-box:latest        -> localhost/agent-box:latest
~/.local/share/codex-box/         -> ~/.local/share/agent-box/
```
