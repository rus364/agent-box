"""Telegram configuration: notifications and remote control.

The on-disk format started as a two-key file (``token`` and ``chat_id``) used
only for outbound notifications. This module loads that legacy format
transparently and normalizes it into the current schema, without ever writing
back an old install's file until the user explicitly changes something (so a
stale process reading the same file mid-migration still sees valid JSON).

Current schema (``~/.config/agent-box/telegram.json`` by default, mode 0600)::

    {
      "token": "<bot token>",
      "notify_chat_id": "<chat id used for outbound notifications>",
      "remote_control": {
        "enabled": false,
        "allowed_user_ids": [123456789],
        "workspace_roots": ["~/code", "~/work"]
      }
    }

No secrets other than the bot token live in this file, and the token is never
included in command output or logs.
"""
from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .paths import TELEGRAM_CONFIG

SECRET_MODE = stat.S_IRUSR | stat.S_IWUSR


@dataclass
class RemoteControlConfig:
    enabled: bool = False
    allowed_user_ids: list[int] = field(default_factory=list)
    workspace_roots: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "allowed_user_ids": list(self.allowed_user_ids),
            "workspace_roots": list(self.workspace_roots),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "RemoteControlConfig":
        if not isinstance(data, dict):
            return cls()
        raw_ids = data.get("allowed_user_ids") or []
        ids = [int(x) for x in raw_ids if isinstance(x, (int, str)) and str(x).lstrip("-").isdigit()]
        roots = [str(x) for x in (data.get("workspace_roots") or []) if isinstance(x, str) and x.strip()]
        return cls(enabled=bool(data.get("enabled", False)), allowed_user_ids=ids, workspace_roots=roots)


@dataclass
class TelegramConfig:
    token: str = ""
    notify_chat_id: str = ""
    remote_control: RemoteControlConfig = field(default_factory=RemoteControlConfig)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "notify_chat_id": self.notify_chat_id,
            "remote_control": self.remote_control.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TelegramConfig":
        if not isinstance(data, dict):
            return cls()
        token = str(data.get("token") or "")
        # Legacy schema used "chat_id" for the single notification target.
        notify_chat_id = str(data.get("notify_chat_id") or data.get("chat_id") or "")
        return cls(
            token=token,
            notify_chat_id=notify_chat_id,
            remote_control=RemoteControlConfig.from_dict(data.get("remote_control")),
        )


def load_telegram_config(path: Path = TELEGRAM_CONFIG) -> TelegramConfig | None:
    if not path.is_file():
        return None
    try:
        return TelegramConfig.from_dict(read_json(path))
    except (OSError, ValueError, TypeError):
        return None


def save_telegram_config(config: TelegramConfig, path: Path = TELEGRAM_CONFIG) -> None:
    atomic_write_json(path, config.to_dict(), SECRET_MODE)
