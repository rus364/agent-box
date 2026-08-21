# agent-box

`agent-box` runs Codex CLI and Claude Code inside disposable rootless Podman containers built from one development image.

It mounts your current project into the container at the *same absolute path* it has on the host, builds a container user that mirrors your real Unix identity, and preserves agent configuration/authentication across disposable runs. It also has optional Telegram integration: outbound notifications, and an opt-in bidirectional remote-control mode that runs Claude Code or Codex sessions in containers of their own, under Telegram's control.

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
agent-box shell      # a plain shell in an identically configured container
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

**One home directory, from one source.** The home used for the build identity, for the container's runtime `HOME`, for every bind mount's host side, and for the state/config defaults is always the passwd entry's `pw_dir` (`agent_box.paths.passwd_home`). `$HOME` is never consulted, and neither is anything that consults it internally — `Path.home()` and `Path.expanduser()` both read `$HOME` first, so `~` in a setting like `XDG_DATA_HOME` or `CLAUDE_CONFIG_DIR` is expanded against the passwd home explicitly. A poisoned or merely inherited-from-elsewhere `$HOME` therefore cannot relocate any of it, which matters because `--userns=keep-id` maps the *passwd* identity back to the host regardless of what a shell variable says.

Building the image requires that identity to be passed explicitly: the `Containerfile`'s `USER_NAME`/`USER_UID`/`USER_GID`/`USER_HOME` args have no defaults, and a build without them fails immediately with a message saying what to pass. `agent-box build` supplies all four.

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

### What exactly gets mounted from `~/.agents`

If `~/.agents` exists, the **whole tree** is bind-mounted read-only at its own path, and `agent-box` then walks **all of it** — not just `skills/` — looking for symlinks that point outside the tree, and mounts each distinct target read-only at its own path too.

That scope is deliberate. Since the tree itself is already visible to the container, narrowing the walk to `skills/` would not hide anything; it would just leave every non-skill symlink (a shared agent definition, a command, a prompt fragment) dangling inside a directory the container can already read. `sync_claude_agent_skills` only cares about `skills/` because that is the part Claude Code consumes by convention — a different question from what has to resolve.

The guarantees that come with that scope:

- Every one of these mounts is **read-only**, external targets included. agent-box exposes your shared agent material to the container; it does not let the container rewrite your library.
- Symlinks pointing back inside `~/.agents` are skipped — the whole-tree mount already covers them.
- Each distinct target is mounted once, in a deterministic order.
- A broken symlink prints a warning and is skipped; it never fails the run.

If you keep something under `~/.agents` that you do not want a container to read, move it out — the read-only tree mount is unconditional.

`agent-box doctor` verifies both the `~/.agents/skills` links and their Claude Code counterparts, and prints the Claude config and Codex home directories it actually inspected (honoring `CLAUDE_CONFIG_DIR` / `CODEX_HOME`).

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

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honored for the agents' own directories. When `CLAUDE_CONFIG_DIR` is set, the relocated directory is bind-mounted at its host path *and* the resolved value is forwarded into the container as `CLAUDE_CONFIG_DIR`, so `claude` inside the container reads the same configuration instead of an empty `$HOME/.claude`.

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

If you also configure remote control and a live session exists for the same project, notifications include a pointer to it: `/session <id>`, which the bot accepts as a command.

That pointer works from inside the container, where the hooks actually run. The Telegram state directory (`sessions.json`, pending-question callback tokens, the `getUpdates` offset) is deliberately **not** mounted into any container, so container-side code cannot read it — an earlier version tried to, and the pointer was therefore always empty (and named `/sessions <id>`, a command the bot never accepted). Instead the daemon publishes a minimal index at `~/.local/share/agent-box/notify/sessions-index.json` (mode `0600`) containing only session id, provider, and project path for sessions that are live right now. No callback tokens, no polling offset, no chat or Telegram user IDs.

## Telegram remote control

Remote control turns the same Telegram bot into a bidirectional interface: you can start Claude Code or Codex sessions from Telegram, chat with them, and answer their structured questions with inline buttons.

**Sessions run in their own disposable containers, built from the same image and configured by the same authoritative policy as normal `agent-box claude`/`codex` usage.** Every run — local or remote — is a separate `podman run --rm`; nothing is ever shared between two sessions, and no container outlives its session. What *is* shared is the policy: a single function (`agent_box.runtime.base_podman_args`) decides the mounts, environment, and hardening for both, so there is exactly one place that determines what a container can see, rather than the daemon re-approximating a second answer.

The Telegram daemon itself runs on the host — it has to, to hold the long-poll connection and manage subprocesses — but every agent process it starts is `podman run` under the hood. A machine with Python, Podman, and the built `agent-box` image — but **no host-installed `claude` or `codex` binary at all** — can still be Telegram-remote-controlled.

The one deliberate policy difference for remote-control containers: outbound Telegram notification hooks are switched off in them (`AGENT_BOX_SUPPRESS_NOTIFY`). See "Notifications and remote control don't double up" below.

**It is disabled by default and requires explicit setup.** Configure it after `agent-box notify setup` has stored a bot token:

```bash
agent-box telegram roots add ~/code          # an allowed project root (repeatable; stored as a canonical absolute path)
                                             # "~" expands against the passwd home, never $HOME
agent-box telegram allow add <your_telegram_user_id>
agent-box telegram enable
agent-box telegram serve                     # foreground long-polling daemon
```

Run `agent-box telegram serve` as a long-lived process (e.g. a systemd `--user` service, tmux/screen session, or similar). It uses `getUpdates` long polling — no webhook, no public HTTP endpoint, no inbound port.

### Notifications and remote control don't double up

If you use both features, a remote-control session's events go **only** to the chat that owns it.

The Claude notification hooks live in `~/.claude/settings.json`, which every container mounts, so without this they would also fire for Telegram-driven sessions and post that session's permission prompts and turn completions to the single global `notify_chat_id`. That is a different audience: remote-control sessions are owner-scoped, and their owner never chose the notification chat — may not even be in it. So machine-mode containers get `AGENT_BOX_SUPPRESS_NOTIFY=1` and `agent_box.notify` stops before reading the config or contacting the API.

Local `agent-box claude` / `agent-box codex` runs never set the flag, so ordinary notification behavior is unchanged. Suppression only affects *sending*: per-session background-task bookkeeping still updates normally.

### Security model

- Remote control is off until you explicitly enable it, independent of notification setup.
- Only Telegram user IDs on the configured allowlist get any response; unknown users receive no information, not even an error.
- Projects are chosen from buttons built from configured, canonicalized `workspace_roots` only — a Telegram user can never type or select an arbitrary host path. A symlink inside an allowed root that points outside every configured root is not listed as a selectable project, and is rejected if selected anyway.
- **Project discovery is one level deep**, by design: `/new` lists each configured root itself plus its immediate subdirectories, and stops there. The list is rendered as inline buttons (one row each), so an unbounded recursive walk would be both unusable in Telegram and a needless filesystem scan on every `/new`. To expose a project nested deeper, add its parent directory to `workspace_roots`. Depth limits the *listing* only — containment checks still accept any path genuinely inside a configured root.
- **The local `--mount` CLI option described above is never exposed to Telegram.** There is no remote equivalent, no `/mount` command, and no way for a Telegram message or callback to add an arbitrary host path to a session's mounts. If a deployment genuinely needs extra host paths available remotely, that would be a host-administrator-configured, non-Telegram-writable setting — not implemented here because it isn't needed yet.
- There is no `/shell` command, no generic "run this host command" action, and callback data never carries raw agent arguments — every button maps to a short opaque token validated server-side against the session/user/request it belongs to.
- **Every session has an owner, and only the owner can act on it.** Listing, opening, routing plain text to, interrupting, stopping, resuming, and answering questions all require `owner_user_id` to match. Being on the allowlist authorizes running *your own* sessions in a chat — there is no shared-session policy in agent-box, so a second allow-listed user in the same group chat gets their own independent sessions rather than access to yours. Active-session routing is keyed by `(chat_id, user_id, message_thread_id)`, so two people in one group chat never collide.
- **A project is re-authorized on every resume**, against `workspace_roots` as configured *now* and against the project's *current* canonical path. A stored path records a past decision, not a standing grant: if a root was removed or narrowed, the project deleted, or the path replaced by a symlink escaping the root, `Resume` refuses and says so.
- Answering an already-claimed, already-delivered, already-recorded, or expired question fails closed. A callback is only ever reported as successful to the user *after* the provider actually confirms delivery; a delivery failure — including a write/transport exception mid-delivery — is reported honestly and never leaves the question stuck in a claimed state, but the token is also not silently reusable.
- When one provider request carries several questions, answering one of them is reported as **recorded**, not delivered: nothing reaches the agent until every question in that request has an answer and the single aggregate response is actually submitted. Only then are the earlier recorded answers marked delivered.
- The container itself keeps its existing hardening (`--cap-drop=all`, `--security-opt=no-new-privileges`) for every session, local and remote alike.
- `telegram.json` (token, allowlist, workspace roots) stays mode `0600`. Session/pending-question state files are also written `0600` and never contain the bot token or agent credentials.
- Logging never includes the bot token, provider credentials, or full prompt payloads.

### Using it from Telegram

```text
/start        main menu (Sessions, New session, Usage, Refresh)
/sessions     list your sessions
/session <id> open a specific session (the pointer notifications print)
/new          create a session: pick Claude or Codex, then a project
/status       quick summary
/usage        Claude + Codex usage line
```

Multiple sessions in the same chat are supported, and each one belongs to the user who created it: `/sessions` lists only your own, and every other command refuses to touch someone else's.

Opening a session makes it *your* active session for that chat (or that forum topic) — your plain text messages route there until you open a different one. Routing candidates are always scoped to your own sessions in the current chat and the current topic, so a message in a topic never lands in a main-chat session and vice versa. Within those candidates the order is: the session you explicitly opened, else the single *live* one. A forum topic that has hosted several sessions over time routes to the one that is actually running now — never to the oldest record that happens to match the topic. With two live sessions and no explicit selection, you're asked to pick rather than the bot guessing.

Each session has `Open`, `Interrupt`, `Stop`, and (once stopped/disconnected) `Resume` buttons — only actions the provider actually confirmed are reported as having happened.

### Structured questions

When a provider asks a structured question (Codex's `item/tool/requestUserInput`, Claude's `AskUserQuestion`), it's rendered as inline buttons. The buttons follow what that specific question actually permits, rather than flattening every question into one shape:

| Question says | You get |
|---|---|
| a list of options | one button per option; tapping one answers immediately |
| `multiSelect: true` (Claude) | checkbox toggles (☐/☑) plus an explicit **Submit**; nothing is sent until you submit, and submitting with nothing selected is refused rather than sending an empty answer |
| free text is allowed (Claude always; Codex when the question sets `isOther`) | an **Other...** button; your next plain message in that chat/topic goes back as *that question's answer*, not as a new turn |
| no options at all | just **Other...** |

The free-text path matters for correctness, not just convenience: both providers block on an outstanding request, so an answer typed as an ordinary turn would leave that request unanswered. A question that does not permit free text gets no Other button, and tapping a stale one is refused.

If a request carries several questions you get one message per question, and the bot says plainly when an answer is only *recorded* pending the rest — nothing reaches the agent until every question in that request is answered and the single aggregate response is submitted. This holds for every answer shape, including multi-select submits and free text.

If the provider withdraws a question (Codex `serverRequest/resolved`, Claude `control_cancel_request`, or the session ending), its buttons are retired and any armed Other prompt is disarmed, instead of silently failing later.

### Session persistence and daemon restarts

Sessions are recorded in `~/.local/share/agent-box/telegram/sessions.json` (JSON, atomic writes, no secrets): local session ID, provider, provider session/thread ID, host project path, chat ID, owning Telegram user ID, status, and timestamps. The `getUpdates` offset is persisted separately so a restart doesn't replay (or silently drop) Telegram updates.

On daemon startup, every session whose stored status implies a live process is marked `disconnected` rather than left as `running` — a fresh process has no provider attached to anything yet, and persisted metadata is not proof of a live one. What happens next depends on the provider (see below): `Resume` either reconnects the real underlying session or reports honestly that it can't.

### Claude vs. Codex capabilities

Both adapters use each tool's supported machine interface — Codex's `app-server` JSON-RPC protocol (verified against the schema the installed `codex app-server generate-json-schema --out <dir>` emits, including the `initialize` → `initialized` → `thread/start` handshake order and the `capabilities.experimentalApi` opt-in that `item/tool/requestUserInput` requires) and Claude Code's `--input-format stream-json --output-format stream-json` plus its `--permission-prompt-tool stdio` control protocol. Neither scrapes terminal output, drives a TUI, or reads a provider's private session files.

| | Codex | Claude Code |
|---|---|---|
| Structured questions → buttons | Yes (`item/tool/requestUserInput`, gated on `capabilities.experimentalApi`); a single request can carry multiple questions and is answered with exactly one aggregate JSON-RPC response once all of them have an answer | Yes (`AskUserQuestion` via the Agent SDK's `canUseTool` mechanism — a `can_use_tool` control request answered with `behavior: "allow"` and the selections in `updatedInput.answers`); every sub-question of a multi-question call is exposed, and all of them are returned in one control response |
| Free-text ("Other") answers | Per question, from the schema's `isOther` (default false) | Always — `AskUserQuestion` never restricts the user to the listed options |
| Multi-select questions | Not in the protocol; `request_user_input` has no multi-select field | Yes, from the question's `multiSelect`; selections are comma-joined into the single value the `answers` map takes |
| Interrupt a running turn | Yes (`turn/interrupt`) | Not supported over stream-json input; reported honestly as unsupported rather than faking success |
| Resume after daemon restart | Real (live-verified): `thread/resume` rejoins the existing thread by ID; the same local session record is kept. Only works once the thread has completed at least one turn — Codex doesn't persist a resumable rollout before that | Real (live-verified), but a different guarantee: `--resume <session-id>` starts a **new** process that continues the same session's history (Claude Code's own CLI help documents `--session-id` and `--resume` as separate mechanisms, and `--fork-session`'s help text confirms plain `--resume` reuses the original ID) — not a rebind of a still-running process, since Claude Code has no supported way to do that |

Both adapters' core flow -- starting a session, sending a turn, receiving text back, and resuming -- has been verified live through a real built image with real Claude/Codex credentials (not just against fixtures/fake executables): a Codex thread survived `thread/resume` with the same thread ID after a real turn, and a Claude session survived `--resume <session-id>` with the model correctly recalling something only told to the earlier process. One real nuance found this way: Codex only persists a resumable "rollout" for a thread once it has run at least one turn -- resuming a thread that was started but never given a turn fails with a real, honestly-propagated `ProviderError` ("no rollout found for thread id ..."), not a silent success.

#### How Claude's structured questions actually work

`AskUserQuestion` is a **built-in** tool: the CLI executes it itself, so a client cannot answer it by sending a `tool_result` block on stdin for its `tool_use` id. An earlier revision of this adapter did exactly that. It looked correct only because the test suite's fake `claude` executable had been written to accept it — against a real Claude Code install it did nothing. That fake, and the claim it supported, are gone.

The supported mechanism is the Agent SDK's `canUseTool` callback. The SDK implements it over the CLI's own control protocol: it launches the CLI with `--permission-prompt-tool stdio` and answers a `can_use_tool` control request. `AskUserQuestion` declares `requiresUserInteraction`, so the CLI routes it to that permission surface with `requires_user_interaction: true`, and the user's selections come back as the tool's `updatedInput` — the `AskUserQuestion` input schema carries an `answers` map, described by the CLI itself as "User answers collected by the permission component", keyed by question text. This adapter speaks that protocol directly rather than adding the SDK as a second in-container execution path; the wire format is the SDK's.

**This round trip is live-verified** against the installed Claude Code (2.1.239) with real credentials: a real turn emitted the `can_use_tool` request for `AskUserQuestion`, the response above was accepted, and the model received `"Pick a colour"="Red"` and answered accordingly. The test suite reproduces the same exchange against a fake `claude` that refuses to ask at all unless `--permission-prompt-tool stdio` is present.

The Codex handshake itself — `initialize` with `capabilities.experimentalApi: true`, `initialized`, then `thread/start` — *was* run against the installed `codex app-server` (0.148.0), which accepted the capability and returned a real thread. Codex's `item/tool/requestUserInput` round trip is **not** live-verified — the model does not reliably call that tool on demand from a scripted, non-interactive smoke test. It is exercised against a fake `codex app-server` that enforces the real handshake and the `experimentalApi` capability (a client that does not declare it gets no question at all), plus fixtures generated from the installed CLI's own JSON schema. The request/response shapes match `ToolRequestUserInputParams`/`ToolRequestUserInputResponse` from that schema.

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

- Codex's `item/tool/requestUserInput` round trip is exercised only against a fake `codex app-server` (which does enforce the real handshake, the `experimentalApi` capability, and the schema's request/response shapes) — the model does not reliably call that tool on demand from a scripted smoke test, so the end-to-end path has not been seen against a live Codex. Claude's `AskUserQuestion` round trip *has* been live-verified; see "Claude vs. Codex capabilities" above.
- The Claude adapter's structured questions depend on `--permission-prompt-tool stdio` and the `can_use_tool` control request. That is the same wire the Agent SDK's `canUseTool` uses, but it is not a frozen public API: a future Claude Code release could change it, in which case questions stop appearing (they are not silently mis-answered). It is verified against 2.1.239 only.
- Multi-select and free-text answering are exercised against the fakes and unit-tested end to end through the bot, but not observed against a live model actually emitting a `multiSelect` question or a user picking "Other" in a real Telegram chat.
- `~/.agents` is mounted read-only in full, and every external symlink target under it is mounted read-only too. This is deliberate (see "What exactly gets mounted from `~/.agents`"), but it does mean the container can read anything you keep there, not only skills.
- Any other tool that reaches the permission surface in a Telegram session is **denied**, not auto-approved. Sessions run with `--permission-mode bypassPermissions`, so this should not normally happen; if a future Claude Code version routes more tools there, those tool calls will fail rather than run unattended.
- A failed answer-delivery attempt is reported honestly and the token is safely consumed (no replay), but the original message's buttons are not automatically re-issued with a fresh token — you'd need the agent to re-ask, or restart the session.
- There is no remote (Telegram-driven) equivalent of `--mount`; extra host paths for remote sessions would need a host-administrator-configured setting, which doesn't exist yet because nothing has needed it.
- KVM/QEMU passthrough applies to interactive local runs only, not to Telegram machine-mode sessions.

## Migration from older agent-box versions

- The container's internal user used to be a fixed, neutral `agentbox` account; it now mirrors your real Unix identity, and the project workspace used to be remapped to a fixed `/workspace` inside the container; it's now mounted at the same absolute path it has on the host. Nothing to do on your side beyond rebuilding the image (`agent-box rebuild`); host-side auth/config directories are unaffected since they're bind-mounted by content.
- The mandatory `~/code` workspace requirement is gone. If you relied on it implicitly, set `AGENT_BOX_WORKSPACE_ROOT=~/code` to keep the old "one big mounted tree" behavior, or simply `cd` into individual projects as needed.
- `telegram.json`'s old two-key notification-only schema (`token`, `chat_id`) is read transparently; nothing breaks, and remote control stays off until you explicitly configure and enable it.
- Telegram remote-control sessions used to run the `claude`/`codex` CLI directly on the host; they now run in disposable containers from the same image, under the same runtime policy as everything else. If you were relying on a host-installed `claude`/`codex` for remote control specifically, it's no longer required (the built image is).
- **`podman build` on the `Containerfile` now requires explicit identity args.** The `USER_NAME`/`USER_UID`/`USER_GID`/`USER_HOME` build args no longer default to an invented `agent`/`1000`/`1000` identity, because that produced an image whose user and home matched no host, failing confusingly at runtime instead of at build time. Use `agent-box build` (which derives all four from the host passwd entry), or pass all four `--build-arg` values yourself.
- **`$HOME` no longer influences any host path.** The build identity, the container's `HOME`, every bind mount source, the state/config directory defaults, `AGENT_BOX_WORKSPACE_ROOT`, `--mount`, `agent-box telegram roots add`, and Telegram project discovery all come from the passwd entry (`pwd.getpwuid`); `~` anywhere in a setting expands against that same home. If you were deliberately pointing `$HOME` somewhere else to relocate agent-box state, use `AGENT_BOX_STATE_DIR`/`AGENT_BOX_CONFIG_DIR`/`XDG_*` instead.
- **`CLAUDE_CONFIG_DIR` is now forwarded into the container**, and honored consistently on the host. If you relocate Claude Code's config directory, the container is told about it (and the directory is bind-mounted at its host path) instead of `claude` inside the container falling back to an empty `$HOME/.claude`. `agent-box usage claude` reads credentials from the relocated directory, `agent-box doctor` inspects it, and the skills sync writes there — all from the one normalized value in `agent_box.paths`, rather than each re-parsing the variable. `CODEX_HOME` works the same way for `agent-box usage codex`.
- **Telegram remote-control sessions no longer trigger the outbound notification hooks.** Their events go to the owner's chat via the bot only. Local runs are unchanged.

### Migration from codex-box (pre-agent-box)

The launcher, image, hostname, environment variables, and persistent state directory were renamed from `codex-box` to `agent-box`:

```text
codex-box                         -> agent-box
CODEX_BOX_IMAGE                   -> AGENT_BOX_IMAGE
CODEX_BOX_KVM                     -> AGENT_BOX_KVM
localhost/codex-box:latest        -> localhost/agent-box:latest
~/.local/share/codex-box/         -> ~/.local/share/agent-box/
```
