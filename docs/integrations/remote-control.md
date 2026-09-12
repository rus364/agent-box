# Remote Control

In `agent-box config`, enable `claude.remote_control` or `codex.remote_control`
under Host integrations, then save. Both default to off. The choice applies to
new interactive terminal launches, in containers and with `--host`.

Off uses the ordinary provider command: no private server, profile restrictions
or agent-box Remote Control setup. It does not override native provider settings
or explicit Remote Control commands. Changing either checkbox leaves running
sessions alone. Batch commands and delegated `codex exec` / `claude -p` calls do
not acquire automatic Remote Control.

## Claude

agent-box checks CLI support and first-party claude.ai login, then adds
`--remote-control`. Use the link/QR shown by Claude to connect from its app or web
interface. Account eligibility, managed policy and network availability are
still enforced by Claude. See the [native requirements](https://code.claude.com/docs/en/remote-control).

Resume/continue, explicit `--settings`, third-party endpoints and API-key/setup-token
authentication retain the ordinary launch with a short explanation. Existing
settings, Telegram channels and browser MCP arguments are not rewritten.
Explicit native `--remote-control` arguments remain the user's responsibility.

## Codex

agent-box checks the effective Codex configuration and ChatGPT authentication,
starts a private app-server alongside the terminal, and checks the server's
registered clients. If a paired device is confirmed, no new code is requested.
Otherwise a separated block offers a short-lived manual pairing code for the
Codex app; an unavailable client list does not mean pairing was lost.
Keep the terminal session running while pairing. The code is printed before the terminal UI starts; it remains in the
terminal/tmux scrollback. Restart the session to obtain a new code if it expires.
The terminal connects to that server over a private Unix socket. No public port
is opened and the shared Codex daemon is not started, stopped or reconfigured.

Send the first message in the TUI, then open **that same conversation** on the
phone. Both clients can display its messages. Creating a new conversation on
the phone creates a separate thread; the TUI does not automatically switch to
it. Use `/resume` in the running TUI to select that conversation. The first turn
also makes a new thread available in saved history. agent-box supplies permission
settings to the server only: explicit TUI permission overrides cause Codex
0.154.0 to reject resuming remote tasks. Changing permissions in the TUI can
still trigger that native restriction.

Only **one agent-box Codex Remote Control session per shared `CODEX_HOME`** is
allowed, including across workspace containers and host mode. Another launch
prints a notice and starts normally. Detaching tmux retains the server and its
slot; exiting releases them. A server crash ends its terminal without replaying
the prompt. A normal exit stops the private server process group, including its
npm launcher and a server that exceeds the graceful shutdown timeout. A forced supervisor kill can leave the server holding the slot until
that server exits; no other session automatically kills it. Separately managed
native Remote Control daemons do not participate in this lock.

The following also use the ordinary launch, preserving the original arguments:

- Named profiles (`-p` / `--profile`), custom providers, OSS/local-provider mode.
- Explicit `--remote`, resume/fork, worktree, `--add-dir` and unrecognized CLI options.
- Missing ChatGPT login, unsupported app-server APIs, or startup/pairing failure.

[Named external API launches](external-providers.md) bypass this supervisor and
reject explicit remote/profile/session-reuse options. Their ordinary TUI or
managed CLI process does not acquire the RC slot or change the parent's mode.

The server receives the launch's `-c` integration overrides, model and permission
settings. It uses the same provider home, project directory, filesystem and
network namespace as the terminal, retaining server-side agents, skills, plugins
and MCP configuration. Neither config.toml nor profile files are rewritten.
Codex's remote TUI mode does not load locally configured runtime environments;
turn this checkbox off when that feature is needed. Remote TUI behavior is not
guaranteed to match every embedded CLI feature.

Account policy, service availability and CLI upgrades can introduce further
native restrictions. Known startup failures fall back to the ordinary launch
with a notice.
