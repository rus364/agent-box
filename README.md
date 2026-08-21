# agent-box

`agent-box` runs Codex CLI and Claude Code inside the same rootless Podman development container.

It mounts your current project into the container at the *same absolute path* it has on the host, builds a container user that mirrors your real Unix identity, and preserves agent configuration/authentication across disposable runs. It also has optional Telegram integration: outbound notifications, and an opt-in bidirectional remote-control mode that runs Claude Code or Codex sessions *inside the same container* under Telegram's control.

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

(Any directory on `PATH` works; adjust to your setup.)

## Build

```bash
agent-box build
```

The image is also built automatically on first use. Build args are derived from your real Unix identity (`pwd.getpwuid(os.getuid())`) — see "Container identity" below.

```bash
agent-box build    # regular cached build
agent-box update   # refresh frequently updated tools while preserving base layers
agent-box rebuild  # refresh tools and rebuild without cache
```

The default image is `localhost/agent-box:latest`. Override it with `AGENT_BOX_IMAGE`. If you change host user/UID/GID (or share the image across users), rebuild.

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

## Container identity

The container's user, UID, GID, and home directory mirror the **real host identity that built the image** (`agent_box.identity.detect_host_identity`, backed by `pwd.getpwuid`, not `$USER`/`$HOME`, which a shell can set to anything). Another user rebuilding this project on their own machine gets their own identity automatically — nothing in the source names a specific person or machine. Rootless Podman's `--userns=keep-id` maps that identity back to the real host UID/GID, so files created in the container are owned by you on the host, same as before.

This matters beyond ownership: because the container user's home is the same path as your real home, host-authored absolute-path symlinks — notably the shared skill symlinks agent-box itself writes into `~/.claude/skills` (see below) — resolve correctly inside the container without any path rewriting.

If the username reported by the OS isn't a legal Linux login name (e.g. it contains characters `useradd` rejects), it's sanitized deterministically (lowercased, invalid characters replaced) rather than making something up; if even that fails, it falls back to `user<uid>`.

## Workspace behavior

`agent-box` resolves a single project root for the current invocation and mounts it into the container **at the same absolute path it has on the host** — there is no internal normalization path like a fixed `/workspace`. The container's working directory mirrors your host working directory exactly, including any subdirectory:

```text
host:      /home/alex/code/infrado
cwd:       /home/alex/code/infrado/sub/dir

container: /home/alex/code/infrado   (mounted at the same path)
workdir:   /home/alex/code/infrado/sub/dir
```

Root resolution:

1. If you're inside a git repository/worktree, its top-level directory is the workspace root.
2. Otherwise, the current directory itself is the workspace root.
3. `AGENT_BOX_WORKSPACE_ROOT=/some/big/tree` overrides both of the above — set it if you deliberately want a larger directory (e.g. a legacy `~/code` tree containing many projects) mounted; the current directory must be inside it.

Only the resolved root is mounted; arbitrary unrelated host directories are never exposed implicitly.

### Mounting additional host paths (`--mount`)

Sometimes an agent needs to see something outside the resolved project root — a sibling repository, a data directory, generated output elsewhere on disk. Pass it explicitly:

```bash
agent-box --mount ~/code claude                        # a parent directory containing the project
agent-box --mount /mnt/data/specs:ro codex               # an unrelated directory, read-only
agent-box --mount /mnt/data/specs:ro --mount /srv/gen claude   # repeatable
```

Grammar: `agent-box [--mount PATH[:ro]]... [claude|codex|shell|doctor] [provider args...]`. Agent-box's own options must come *before* the subcommand; everything from the subcommand onward is passed through untouched, so `agent-box claude --model opus` is never misparsed, and a hypothetical Claude/Codex flag also spelled `--mount` is never mistaken for agent-box's. Use `--` to stop agent-box's own option parsing early if you ever need to.

Each `--mount` path must be an existing absolute directory; it's mounted at that same path, read-write by default or read-only with `:ro`. Paths (including ones containing spaces) are resolved to their canonical, symlink-free form before being used, both as the actual mount source and for overlap checking, so a mount request can't be silently widened by a symlink.

Overlapping mounts (including the implicit project-root mount) are resolved deterministically rather than left to Podman's own ordering:
- identical paths collapse to one;
- if one mount's path contains another's, only the broader one is kept — but only when they agree on read-only/read-write; a mismatch (e.g. a read-only child under a read-write parent) is rejected as ambiguous instead of silently picking a side.

This local `--mount` option is a *local CLI feature only*. It is never available to Telegram — see "Security model" below.

## Shared agent skills

`~/.agents/skills` is the canonical shared skill directory. Codex discovers skills there directly, while Claude Code discovers personal skills under `~/.claude/skills`.

Before starting a container, `agent-box` exposes each valid shared skill to Claude Code by creating a matching symlink:

```text
~/.claude/skills/systematic-debugging -> ~/.agents/skills/systematic-debugging
```

The skill contents are not copied. Both agents therefore use the same `SKILL.md` and supporting files. Existing Claude-specific skills are preserved: if `~/.claude/skills/<name>` already exists and does not point at the corresponding shared skill, `agent-box` leaves it unchanged and prints a warning.

Shared skills may themselves be symlinks to directories elsewhere on the host; because the container's home mirrors the host's (see "Container identity"), those symlinks resolve correctly inside the container without rewriting, and `agent-box` mounts their real targets read-only.

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

If you also configure remote control and a matching session exists, notifications include a pointer to it.

## Telegram remote control

Remote control turns the same Telegram bot into a bidirectional interface: you can start Claude Code or Codex sessions from Telegram, chat with them, and answer their structured questions with inline buttons.

**Sessions run inside the same Podman container as normal `agent-box claude`/`codex` usage** — the Telegram daemon itself runs on the host (it needs to, to hold the long-poll connection and manage subprocesses), but every actual agent process it starts is `podman run` under the hood, sharing the exact mount/environment/hardening model `agent_box.runtime` uses for interactive use. A machine with Python, Podman, and the built `agent-box` image — but **no host-installed `claude` or `codex` binary at all** — can still be Telegram-remote-controlled.

**It is disabled by default and requires explicit setup.** Configure it after `agent-box notify setup` has stored a bot token:

```bash
agent-box telegram roots add ~/code          # an allowed project root (repeatable; stored as a canonical absolute path)
agent-box telegram allow add <your_telegram_user_id>
agent-box telegram enable
agent-box telegram serve                     # foreground long-polling daemon
```

Run `agent-box telegram serve` as a long-lived process (e.g. a systemd `--user` service, tmux/screen session, or similar). It uses `getUpdates` long polling — no webhook, no public HTTP endpoint, no inbound port.

### Security model

- Remote control is off until you explicitly enable it, independent of notification setup.
- Only Telegram user IDs on the configured allowlist get any response; unknown users receive no information, not even an error.
- Projects are chosen from buttons built from configured, canonicalized `workspace_roots` only — a Telegram user can never type or select an arbitrary host path. A symlink inside an allowed root that points outside every configured root is not listed as a selectable project, and is rejected if selected anyway.
- **The local `--mount` CLI option described above is never exposed to Telegram.** There is no remote equivalent, no `/mount` command, and no way for a Telegram message or callback to add an arbitrary host path to a session's mounts. If a deployment genuinely needs extra host paths available remotely, that would be a host-administrator-configured, non-Telegram-writable setting — not implemented here because it isn't needed yet.
- There is no `/shell` command, no generic "run this host command" action, and callback data never carries raw agent arguments — every button maps to a short opaque token validated server-side against the session/user/request it belongs to.
- Answering an already-claimed, already-delivered, or expired question fails closed. A callback is only ever reported as successful to the user *after* the provider actually confirms the answer was accepted; a delivery failure is reported honestly and the token is not silently reusable, but it is also not silently claimed as success.
- The container itself keeps its existing hardening (`--cap-drop=all`, `--security-opt=no-new-privileges`) for every session, local and remote alike.
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

Multiple sessions in the same chat are supported. Opening a session from `/sessions` makes it the chat's *active* session — plain text messages route there until you open a different one. If a session was created inside a Telegram forum topic, messages sent in that topic route to it directly regardless of which session is otherwise active. With no active session and more than one live session in the chat, you're asked to pick one explicitly rather than the bot guessing.

Each session has `Open`, `Interrupt`, `Stop`, and (once stopped/disconnected) `Resume` buttons — only actions the provider actually confirmed are reported as having happened.

When a provider asks a structured question with options (Codex's `request_user_input`, Claude's `AskUserQuestion`), it's rendered as inline buttons; tapping one submits the corresponding structured answer.

### Session persistence and daemon restarts

Sessions are recorded in `~/.local/share/agent-box/telegram/sessions.json` (JSON, atomic writes, no secrets): local session ID, provider, provider session/thread ID, host project path, chat ID, owning Telegram user ID, status, and timestamps. The `getUpdates` offset is persisted separately so a restart doesn't replay (or silently drop) Telegram updates.

On daemon startup, every session whose stored status implies a live process is marked `disconnected` rather than left as `running` — a fresh process has no provider attached to anything yet, and persisted metadata is not proof of a live one. What happens next depends on the provider (see below): `Resume` either reconnects the real underlying session or reports honestly that it can't.

### Claude vs. Codex capabilities

Both adapters use each tool's supported machine interface — Codex's `app-server` JSON-RPC protocol (verified against the schema the installed `codex app-server generate-json-schema` emits, including the `initialize` → `initialized` → `thread/start` handshake order) and Claude Code's `--input-format stream-json --output-format stream-json`. Neither scrapes terminal output, drives a TUI, or reads a provider's private session files.

| | Codex | Claude Code |
|---|---|---|
| Structured questions → buttons | Yes (`item/tool/requestUserInput`); a single request can carry multiple questions and is answered with exactly one aggregate JSON-RPC response once all of them have an answer | Best-effort (`AskUserQuestion` tool call answered via a `tool_result` message); only the first sub-question of a multi-question call is exposed as buttons |
| Interrupt a running turn | Yes (`turn/interrupt`) | Not supported over stream-json input; reported honestly as unsupported rather than faking success |
| Resume after daemon restart | Real (live-verified): `thread/resume` rejoins the existing thread by ID; the same local session record is kept. Only works once the thread has completed at least one turn — Codex doesn't persist a resumable rollout before that | Real (live-verified), but a different guarantee: `--resume <session-id>` starts a **new** process that continues the same session's history (Claude Code's own CLI help documents `--session-id` and `--resume` as separate mechanisms, and `--fork-session`'s help text confirms plain `--resume` reuses the original ID) — not a rebind of a still-running process, since Claude Code has no supported way to do that |

Both adapters' core flow -- starting a session, sending a turn, receiving text back, and resuming -- has been verified live through a real built image with real Claude/Codex credentials (not just against fixtures/fake executables): a Codex thread survived `thread/resume` with the same thread ID after a real turn, and a Claude session survived `--resume <session-id>` with the model correctly recalling something only told to the earlier process. One real nuance found this way: Codex only persists a resumable "rollout" for a thread once it has run at least one turn -- resuming a thread that was started but never given a turn fails with a real, honestly-propagated `ProviderError` ("no rollout found for thread id ..."), not a silent success.

A Python/TypeScript Agent SDK also exists as a possible alternative to driving the `claude` CLI's stream-json interface; it was considered but not adopted here, since it would mean running a second, SDK-based execution path inside the container alongside the CLI-based one used for local interactive sessions, which is a much larger change than this pass's scope. The documented stream-json CLI mechanism used here was live-verified for the parts that matter most (starting a session, turns, resume); if a future revision finds the SDK gives materially better structured-question guarantees, the adapter is isolated enough (`agent_box/telegram/providers/claude.py`) to swap without touching the rest of the system.

What was *not* live-verified: the `AskUserQuestion`/`request_user_input` structured-question round trip, since neither model reliably calls those tools on demand from a scripted, non-interactive smoke test. That path is exercised against a fake `claude`/`codex app-server` executable in the test suite instead (proving the actual subprocess/JSON-RPC wiring), plus hand-authored fixtures for the message-parsing logic itself. As a safety net, every Claude question is also answerable by plain free text: if you type an answer instead of tapping a button, it's delivered as an ordinary next user turn, which Claude will still receive as an answer.

## Self-tests

```bash
agent-box selftest
```

A fast, dependency-free smoke test (usage formatting, notification payload handling, background-task state, config migration, workspace resolution, host identity detection, mount planning) with no Podman/network access. The full test suite lives under `tests/` and covers the rest — session registry, callback claim/deliver/fail gating, project allowlisting (including symlink-escape rejection), Telegram HTML escaping, `--mount` parsing and overlap planning, and Codex/Claude protocol behavior both against recorded fixture payloads *and* against fake `claude`/`codex app-server` executables that exercise the real subprocess argv/stdin/stdout wiring:

```bash
python3 -m unittest discover -s tests
```

No real Telegram, Anthropic, or OpenAI credentials are required; network calls are mocked, and the fake-executable tests spawn real (but fake) subprocesses instead of talking to any real service.

## Diagnostics

```bash
agent-box doctor
```

Checks both agents, development tools, QEMU, KVM, and shared agent skills inside the container.

## KVM

When `/dev/kvm` is accessible, it is passed through automatically for interactive local runs. Disable explicitly with `AGENT_BOX_KVM=0`. ARM64 QEMU remains available through TCG regardless. (Telegram remote-control sessions don't request KVM — they aren't interactive VM/terminal sessions.)

## Troubleshooting

- **`podman not found`** — install Podman; `agent-box` does not fall back to Docker.
- **`current directory is not inside the configured workspace root`** — you set `AGENT_BOX_WORKSPACE_ROOT` and `cd`'d outside it; `cd` back inside, or unset the override.
- **`--mount` rejects a path** — the path must be an existing absolute directory; a relative path, a file, or a not-yet-created directory is rejected rather than silently mounted (or silently ignored).
- **Claude/Codex not authenticated inside the container** — authenticate on the host first (`claude` / `codex login` outside `agent-box`, or `gh auth login` for GitHub); the container mounts `~/.claude`, `~/.codex`, and GitHub config from the host rather than managing its own credentials.
- **Shared skill shows as MISSING/MISMATCH in `agent-box doctor`** — a `~/.claude/skills/<name>` entry already exists and doesn't point at the shared skill; remove or rename it if you want it linked, otherwise it's left alone intentionally.
- **Telegram remote control does nothing** — check `agent-box telegram status`: it needs a configured token, `remote control: enabled`, at least one allowed user ID, and at least one workspace root.
- **`agent-box image is not built`** from a Telegram session — run `agent-box build` on the host; the daemon doesn't auto-build for you.

## Known limitations

- Claude's structured-question round trip (`AskUserQuestion` → `tool_result`) is implemented against the documented stream-json mechanism but not verified against a live Claude Code session in this development environment; see "Claude vs. Codex capabilities" above.
- A failed answer-delivery attempt is reported honestly and the token is safely consumed (no replay), but the original message's buttons are not automatically re-issued with a fresh token — you'd need the agent to re-ask, or restart the session.
- There is no remote (Telegram-driven) equivalent of `--mount`; extra host paths for remote sessions would need a host-administrator-configured setting, which doesn't exist yet because nothing has needed it.
- KVM/QEMU passthrough applies to interactive local runs only, not to Telegram machine-mode sessions.

## Migration from older agent-box versions

- The container's internal user used to be a fixed, neutral `agentbox` account; it now mirrors your real Unix identity, and the project workspace used to be remapped to a fixed `/workspace` inside the container; it's now mounted at the same absolute path it has on the host. Nothing to do on your side beyond rebuilding the image (`agent-box rebuild`); host-side auth/config directories are unaffected since they're bind-mounted by content.
- The mandatory `~/code` workspace requirement is gone. If you relied on it implicitly, set `AGENT_BOX_WORKSPACE_ROOT=~/code` to keep the old "one big mounted tree" behavior, or simply `cd` into individual projects as needed.
- `telegram.json`'s old two-key notification-only schema (`token`, `chat_id`) is read transparently; nothing breaks, and remote control stays off until you explicitly configure and enable it.
- Telegram remote-control sessions used to run the `claude`/`codex` CLI directly on the host; they now run inside the same Podman container as everything else. If you were relying on a host-installed `claude`/`codex` for remote control specifically, it's no longer required (the built image is).

### Migration from codex-box (pre-agent-box)

The launcher, image, hostname, environment variables, and persistent state directory were renamed from `codex-box` to `agent-box`:

```text
codex-box                         -> agent-box
CODEX_BOX_IMAGE                   -> AGENT_BOX_IMAGE
CODEX_BOX_KVM                     -> AGENT_BOX_KVM
localhost/codex-box:latest        -> localhost/agent-box:latest
~/.local/share/codex-box/         -> ~/.local/share/agent-box/
```
