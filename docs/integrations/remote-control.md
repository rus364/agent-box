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

- **Use the same conversation.** Send the first message in the terminal, then
  open that conversation on the phone. A conversation started on the phone is a
  separate thread, and resuming it in the terminal is not supported with
  agent-box's permission settings.
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
