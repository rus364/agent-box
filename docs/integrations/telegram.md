# Telegram

Two separate things share the name, and both are off by default:

- **Control** — driving a live Claude session from your phone — is Anthropic's
  official `telegram@claude-plugins-official` plugin. agent-box only starts
  Claude with it and keeps two sessions from fighting over one bot.
- **Notifications** — one-way messages when a Claude or Codex session needs you
  or finishes — are agent-box's own.

For native remote access to Claude or Codex without Telegram, see
[Remote Control](remote-control.md).

## Control: the official Claude channel

1. Install and configure the plugin from inside Claude:

   ```text
   agent-box claude
   /plugin install telegram@claude-plugins-official
   /telegram:configure <BOT_TOKEN>
   ```

2. Enable **Agents → Claude → Telegram channel**
   (`agents.claude.telegram_channel`) in `agent-box config`. The menu refuses
   to save it before the plugin and its token exist, and shows the missing
   prerequisites with setup instructions. Press `?` to read and scroll the
   complete instructions if they do not fit on screen.
3. Start a new Claude session and pair your account:

   ```text
   /telegram:access pair <code>
   /telegram:access policy allowlist
   ```

`agent-box config check` verifies agent-box's side: the setting, the installed
plugin, the bot token and which session owns the channel. Pairing and the
access policy live in the plugin and are not checked.

Only one Claude session can poll a bot at a time. agent-box tracks the owner; a
session started while another owns the channel starts normally, just without
Telegram. `--no-remote-control` also keeps one launch off Telegram.

## Notifications

1. Under **Host → Notifications** in `agent-box config`, enable them and set
   the bot token and chat ID (`host.notifications.*`).
2. Save, then try it: `agent-box notify test`.

| Agent | You are notified when |
| --- | --- |
| Claude | It waits for input, needs a permission, completes or fails |
| Codex | A turn completes, with the final assistant message |

Messages carry the agent's own text, sanitized and length-limited; an oversized
message is replaced by an explicit notice. While a Claude session owns the
Telegram channel, its interaction notifications are suppressed (the channel
already relays them), but completion and failure still arrive. Notifications are
one-way: nothing is read back from Telegram.

`--notify` and `--no-notify` turn notifications on or off for one launch;
turning them on needs them configured first.

**What is installed.** Saving or `agent-box config apply` adds agent-box's
hooks to Claude's `~/.claude/settings.json` and a marked
`notify = ["agent-box", "notify", "codex"]` block at the top of
`~/.codex/config.toml`. This covers `codex`, `agent-box --host codex` and boxes
that load that configuration, provided `agent-box` is on the host's PATH. An
existing different Codex `notify` setting is kept with a warning. Disabling or
uninstalling removes only agent-box's own hooks and its unchanged block.
Managed workflow delegates run Codex without your user configuration and send
no notifications.

**Where the token goes.** The bot token stays in
`~/.config/agent-box/config.json` (mode 0600). Boxes never see that file; they
receive only the token and chat ID, and only while notifications are usable.
