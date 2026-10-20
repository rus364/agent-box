# Remote Control

Remote Control lets you follow and drive a new interactive Claude or Codex
session from the provider's own app or web interface. Enable
**Agents → Claude → Remote control** or **Agents → Codex → Remote control**
(`agents.claude.remote_control`, `agents.codex.remote_control`) in
`agent-box config`; both are off by default and global only.

The setting applies to new interactive terminal launches, in boxes and with
`--host`. Running sessions, batch commands and workflow delegates
(`claude -p`, `codex exec`) are unaffected, and `--[no-]remote-control`
overrides it for one launch. Off means the ordinary provider command with no
agent-box involvement. Whenever a launch cannot use Remote Control, it starts
normally with a short notice.

## Claude

agent-box checks CLI support and a first-party claude.ai login, then starts
Claude with `--remote-control`; connect through the link or QR code Claude
shows. Account eligibility, policy and network access are Claude's own; see
the [Claude requirements](https://code.claude.com/docs/en/remote-control).

Resume or continue, explicit `--settings`, third-party endpoints and API-key or
setup-token authentication use the ordinary launch. Existing settings,
Telegram channels and browser arguments are not rewritten.

## Codex

agent-box checks the effective Codex configuration and ChatGPT login, starts a
private app-server for the session and connects the terminal to it. If a paired
device is already registered, nothing is asked; otherwise a short-lived pairing
code is printed before the terminal UI starts (it stays in the scrollback).
Keep the session running while pairing, and restart it for a new code. No
public port is opened and the shared Codex daemon is not touched.

Before pairing, the launcher waits for the remote transport to connect, allowing
transient failures to recover within a 30-second retry window; Ctrl-C cancels startup.
If it cannot connect,
the notice identifies the local-only fallback: that session has no phone access.
Check network/VPN access to ChatGPT, then start a new session. A working terminal
alone does not prove Remote Control connected; host and container routes can both
be affected by a router's selective VPN rules or TLS handling.

- **Use the same conversation.** Send the first message in the terminal, then
  open that conversation on the phone. A conversation started on the phone is a
  separate thread. Codex rejects remote `/resume` when the terminal has explicit
  permission overrides, including **Bypass permissions (container)** or user
  sandbox/approval arguments. Bypass is off by default; leave it off for this
  path. Removing the override does not by itself prove authenticated remote
  resume or phone acceptance for your installed Codex version.
- **One session per Codex home.** Only one agent-box Codex Remote Control
  session may run per shared `CODEX_HOME`, across boxes and host mode; another
  launch starts normally with a notice. Detaching keeps the slot, exiting
  releases it.
- **Ordinary launch instead** for named profiles (`-p`/`--profile`), custom or
  local providers, explicit `--remote`, resume or fork, worktrees, `--add-dir`,
  unrecognized options, a missing ChatGPT login, or a server that fails to
  start or pair. [External API launches](external-providers.md) never use it.
- Codex's remote terminal mode does not load locally configured runtime
  environments; turn Remote Control off when you need them.

Implementation details are in the
[integration design notes](../design/integrations.md#remote-control).
