# Telegram

Two unrelated things share a name here. **Control** is driving a live Claude
session from your phone, and agent-box does not implement it — Anthropic's
official plugin does. **Notifications** are one-way pings when something happens
in a box you walked away from, and those are agent-box's.

Both are off by default.

## Control: the official Claude channel

Remote control is provided by Anthropic's `telegram@claude-plugins-official`
plugin. agent-box implements no Telegram control daemon, pairing protocol,
session router or permission bridge of its own; it only passes the flag that
activates the plugin, and keeps two live sessions from fighting over the same
bot token.

Install and configure the plugin from inside Claude:

```text
agent-box claude
/plugin install telegram@claude-plugins-official
/telegram:configure <BOT_TOKEN>
```

Then enable the channel:

```bash
agent-box config
```

Start a fresh Claude session and pair your account:

```text
/telegram:access pair <code>
/telegram:access policy allowlist
```

`agent-box config` refuses to store an enabled channel before the plugin and its
token exist, because the bootstrap order is necessarily: run Claude, install and
configure the plugin, then come back. Storing `true` early would only produce a
setting every later run has to warn about.

Only one Claude session can own Telegram polling for a bot token at a time.
agent-box tracks that owner and prevents another local session from
accidentally stealing the plugin's poller; a session started while another owns
the channel starts normally, just without Telegram.

Check the whole chain with:

```bash
agent-box config check
```

> [!NOTE]
> Codex remote control is not implemented by agent-box. Use OpenAI's official
> ChatGPT/Codex remote-control experience where supported.

## Notifications

Separate from control, and useful without it: a message when a long run finishes
in a box you are not watching.

```bash
agent-box config      # bot token + chat id
agent-box notify test
```

One-way only. No polling, no `getUpdates`, no callback handling, no way to drive
a session from Telegram.

- **Codex**: turn completed, including the final assistant message.
- **Claude**: waiting for input, permission required, completed, or failed.

When a Claude session already owns the official channel, duplicate *interaction*
notifications are suppressed — the channel is already relaying those to the same
chat — while lifecycle completion and failure signals keep coming.

### Where the credentials go

The bot token lives in `~/.config/agent-box/config.json` (mode 0600) with every
other setting. The container never sees that file: agent-box derives a two-key
file from it —

```text
~/.local/share/agent-box/runtime/notify.json
```

— and it exists only while notifications are actually usable. Turning them off
deletes it. Persistent workspace boxes receive the two values through the
notification-capable exec environment rather than a mount, so they never enter
the box's metadata.

The hooks that deliver Claude's events live in `~/.claude/settings.json` and are
installed by `agent-box config` / `agent-box config apply`, alongside anything you
configured yourself there — agent-box only ever removes its own handlers.
