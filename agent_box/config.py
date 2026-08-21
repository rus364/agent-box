"""The single agent-box configuration file.

One small, versioned JSON document under the normal XDG config location
(``~/.config/agent-box/config.json`` by default, mode 0600 because it may hold
a Telegram bot token). ``agent-box config`` is the interactive front end; this
module is only the schema, load and save.

Design rules this file exists to enforce:

* **Every host integration that exposes credentials is opt-in.** Notably a
  GitHub token is never taken from the host just because ``gh`` happens to be
  authenticated -- ``github.gh_token`` must be turned on deliberately. Same for
  copying the host's ``~/.config/gh`` (which contains that token on disk).
* **Defaults are the safe answer.** A fresh install runs the container with no
  host credentials beyond the agent's own Claude/Codex config directories.
* **No framework.** Flat dataclasses, one version integer, explicit
  ``to_dict``/``from_dict``. Unknown keys are dropped on save rather than
  round-tripped, so the file always matches the shipped schema.

Schema (version 1)::

    {
      "version": 1,
      "claude": {"telegram_channel": false},
      "notifications": {"enabled": false, "telegram_token": "", "telegram_chat_id": ""},
      "github": {"gh_token": false, "gh_config": false},
      "git": {"config": true},
      "providers": {"env": true}
    }
"""
from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .paths import CONFIG_FILE

VERSION = 1
SECRET_MODE = stat.S_IRUSR | stat.S_IWUSR

#: The one Telegram integration agent-box supports for controlling Claude.
#: Anthropic's official plugin owns pairing, access control, permissions and
#: polling; agent-box only passes the flag that activates it.
CLAUDE_TELEGRAM_CHANNEL = "plugin:telegram@claude-plugins-official"
CLAUDE_TELEGRAM_PLUGIN = "telegram@claude-plugins-official"


def _bool(data: Any, key: str, default: bool) -> bool:
    value = data.get(key, default) if isinstance(data, dict) else default
    return bool(value) if isinstance(value, bool) else default


def _str(data: Any, key: str) -> str:
    value = data.get(key) if isinstance(data, dict) else None
    return str(value) if isinstance(value, (str, int)) else ""


@dataclass
class ClaudeConfig:
    #: Start `agent-box claude` with --channels plugin:telegram@claude-plugins-official.
    telegram_channel: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"telegram_channel": self.telegram_channel}

    @classmethod
    def from_dict(cls, data: Any) -> "ClaudeConfig":
        return cls(telegram_channel=_bool(data, "telegram_channel", False))


@dataclass
class NotificationsConfig:
    """Outbound-only Telegram notifications. No polling, no session control."""

    enabled: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.telegram_token and self.telegram_chat_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "telegram_token": self.telegram_token,
            "telegram_chat_id": self.telegram_chat_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "NotificationsConfig":
        return cls(
            enabled=_bool(data, "enabled", False),
            telegram_token=_str(data, "telegram_token"),
            telegram_chat_id=_str(data, "telegram_chat_id"),
        )


@dataclass
class GitHubConfig:
    """Both default to False on purpose.

    ``gh_token`` runs ``gh auth token`` on the host and forwards the result as
    ``GH_TOKEN``; ``gh_config`` copies ``~/.config/gh`` (which stores that same
    token) into the container. An earlier agent-box did the first
    unconditionally whenever ``gh`` was authenticated, which handed every
    container a live GitHub credential nobody asked for.
    """

    gh_token: bool = False
    gh_config: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"gh_token": self.gh_token, "gh_config": self.gh_config}

    @classmethod
    def from_dict(cls, data: Any) -> "GitHubConfig":
        return cls(gh_token=_bool(data, "gh_token", False), gh_config=_bool(data, "gh_config", False))


@dataclass
class GitConfig:
    #: Copy the host's git config (user.name/user.email and friends) in, so
    #: commits made in the container are attributed correctly. Not a secret,
    #: so this one defaults on.
    config: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"config": self.config}

    @classmethod
    def from_dict(cls, data: Any) -> "GitConfig":
        return cls(config=_bool(data, "config", True))


@dataclass
class ProvidersConfig:
    #: Forward provider credential/endpoint variables that are already set in
    #: the invoking shell (ANTHROPIC_API_KEY, proxies, Bedrock/Vertex flags).
    #: Only variables the user exported for this very command are passed, so
    #: this defaults on; turn it off to run the container with none of them.
    env: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"env": self.env}

    @classmethod
    def from_dict(cls, data: Any) -> "ProvidersConfig":
        return cls(env=_bool(data, "env", True))


@dataclass
class Config:
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    git: GitConfig = field(default_factory=GitConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "claude": self.claude.to_dict(),
            "notifications": self.notifications.to_dict(),
            "github": self.github.to_dict(),
            "git": self.git.to_dict(),
            "providers": self.providers.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Config":
        if not isinstance(data, dict):
            return cls()
        return cls(
            claude=ClaudeConfig.from_dict(data.get("claude")),
            notifications=NotificationsConfig.from_dict(data.get("notifications")),
            github=GitHubConfig.from_dict(data.get("github")),
            git=GitConfig.from_dict(data.get("git")),
            providers=ProvidersConfig.from_dict(data.get("providers")),
        )


def load_config(path: Path | None = None) -> Config:
    """The effective configuration; a missing file means all defaults."""
    path = CONFIG_FILE if path is None else path
    if not path.is_file():
        return Config()
    try:
        return Config.from_dict(read_json(path))
    except (OSError, ValueError, TypeError):
        return Config()


def save_config(config: Config, path: Path | None = None) -> None:
    atomic_write_json(CONFIG_FILE if path is None else path, config.to_dict(), SECRET_MODE)
