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
starts a private app-server alongside the terminal, and prints a short-lived
manual pairing code for the Codex app. Keep the terminal session running while
pairing. The code is printed before the terminal UI starts; it remains in the
terminal/tmux scrollback. Restart the session to obtain a new code if it expires.
The terminal connects to that server over a private Unix socket. No public port
is opened and the shared Codex daemon is not started, stopped or reconfigured.

Only **one agent-box Codex Remote Control session per shared `CODEX_HOME`** is
allowed, including across workspace containers and host mode. Another launch
prints a notice and starts normally. Detaching tmux retains the server and its
slot; exiting releases them. A server crash ends its terminal without replaying
the prompt. A forced supervisor kill can leave the server holding the slot until
that server exits; no other session automatically kills it. Separately managed
native Remote Control daemons do not participate in this lock.

The following also use the ordinary launch, preserving the original arguments:

- Named profiles (`-p` / `--profile`), custom providers, OSS/local-provider mode.
- Explicit `--remote`, resume/fork, worktree, `--add-dir` and unrecognized CLI options.
- Missing ChatGPT login, unsupported app-server APIs, or startup/pairing failure.

This keeps future custom-provider launches and their profiles independent of
Remote Control. Calling a separate DeepSeek agent does not change the parent
session's mode.

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
