"""Preflight for Anthropic's official Claude Code Telegram channel plugin.

agent-box does not implement any Telegram control of Claude. It passes one
flag -- ``--channels plugin:telegram@claude-plugins-official`` -- and the
official plugin owns everything behind it: the bot connection, pairing, the
allowlist and access policy, message and question delivery, and polling.

What agent-box *does* own is making sure that flag will actually work before
handing the terminal over to Claude, because a session started with a channel
the plugin cannot serve looks like a working session that silently never
reaches Telegram. Installing or configuring the plugin on the user's behalf is
deliberately not done here: ``claude plugin install`` and ``/telegram:configure``
are the supported mechanisms, they run inside a Claude session, and quietly
writing Claude's plugin state from outside would be guessing at an interface
that is not ours. So this module only reports, and :func:`setup_instructions`
prints the exact commands to run.

Everything inspected lives under ``CLAUDE_HOME``, which agent-box bind-mounts
read-write at its host path -- so a plugin installed once on the host is
already present in every container.
"""
from __future__ import annotations

import os
from pathlib import Path

from .atomicio import read_json
from .config import CLAUDE_TELEGRAM_CHANNEL, CLAUDE_TELEGRAM_PLUGIN
from .paths import CLAUDE_HOME

INSTALLED_PLUGINS = "plugins/installed_plugins.json"
#: Where the plugin's MCP server keeps its token, allowlist and inbox. The
#: server derives this from CLAUDE_CONFIG_DIR, exactly like CLAUDE_HOME does.
CHANNEL_STATE = "channels/telegram"


def plugin_installed(claude_home: Path = CLAUDE_HOME) -> bool:
    try:
        data = read_json(claude_home / INSTALLED_PLUGINS)
    except (OSError, ValueError):
        return False
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return bool(isinstance(plugins, dict) and plugins.get(CLAUDE_TELEGRAM_PLUGIN))


def token_configured(claude_home: Path = CLAUDE_HOME) -> bool:
    """True when the plugin will find a bot token.

    The server reads ``TELEGRAM_BOT_TOKEN`` from the real environment first
    and falls back to its own ``.env``; agent-box does not forward that
    variable into the container, so only the file makes a container session
    work. The environment is still accepted here for a host-side check.
    """
    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        return True
    env_file = claude_home / CHANNEL_STATE / ".env"
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(
        line.strip().startswith("TELEGRAM_BOT_TOKEN=") and line.strip() != "TELEGRAM_BOT_TOKEN="
        for line in text.splitlines()
    )


def missing_requirements(claude_home: Path = CLAUDE_HOME) -> list[str]:
    """Human-readable list of what still has to be set up. Empty means ready."""
    missing = []
    if not plugin_installed(claude_home):
        missing.append(f"the {CLAUDE_TELEGRAM_PLUGIN} plugin is not installed")
    if not token_configured(claude_home):
        missing.append(f"no bot token in {claude_home / CHANNEL_STATE / '.env'}")
    return missing


def setup_instructions(claude_home: Path = CLAUDE_HOME) -> str:
    return "\n".join([
        "Set up the official Telegram channel (once, on this host):",
        "",
        "  1. Create a bot with @BotFather on Telegram and copy its token.",
        "  2. Start a Claude session and install the plugin:",
        "",
        "         agent-box claude",
        f"         /plugin install {CLAUDE_TELEGRAM_PLUGIN}",
        "         /telegram:configure <token>",
        "",
        f"     The token is written to {claude_home / CHANNEL_STATE / '.env'}.",
        "  3. Exit and start again -- agent-box now adds",
        f"     --channels {CLAUDE_TELEGRAM_CHANNEL} for you.",
        "  4. DM the bot, then pair it from the session:",
        "",
        "         /telegram:access pair <code>",
        "         /telegram:access policy allowlist",
    ])


def channel_args(claude_home: Path = CLAUDE_HOME) -> list[str]:
    """The ``--channels`` flag, or a clear error saying what to do first."""
    from .errors import AgentBoxError

    missing = missing_requirements(claude_home)
    if missing:
        raise AgentBoxError(
            "the Claude Telegram channel is enabled in agent-box config but "
            + " and ".join(missing)
            + ".\n\n"
            + setup_instructions(claude_home)
            + "\n\nOr turn it off:  agent-box config"
        )
    return ["--channels", CLAUDE_TELEGRAM_CHANNEL]
