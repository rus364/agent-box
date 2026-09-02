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
      "claude": {"telegram_channel": false, "statusline": true},
      "notifications": {"enabled": false, "telegram_token": "", "telegram_chat_id": ""},
      "github": {"gh_token": false, "gh_config": false},
      "git": {"identity": true, "full_config": false},
      "providers": {"api_keys": false, "transport": true},
      "vikunja": {"enabled": false},
      "flow_next": {
        "review_backends": {"claude": "codex:gpt-5.6-sol:medium", "codex": "codex:gpt-5.6-sol:medium"},
        "auto_push": false
      }
    }

``flow_next`` and ``vikunja`` are newer than ``version`` 1 but do not bump it:
an old config file without them loads fine and gets the defaults above,
matching the "partial file" schema tolerance the rest of this module already
relies on.
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
    #: Keep Claude's ``statusLine`` pointed at ``agent-box statusline``.
    #:
    #: Defaults on, unlike every other integration here, because it is the one
    #: setting that exposes nothing: no credential leaves the host and nothing
    #: reaches the container that was not already mounted. What it costs is a
    #: line of Claude's own UI, which agent-box gives back the moment the
    #: setting is turned off. A status line someone else configured is never
    #: overwritten -- see ``agent_box.claude_settings.reconcile_statusline``.
    statusline: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"telegram_channel": self.telegram_channel, "statusline": self.statusline}

    @classmethod
    def from_dict(cls, data: Any) -> "ClaudeConfig":
        return cls(
            telegram_channel=_bool(data, "telegram_channel", False),
            statusline=_bool(data, "statusline", True),
        )


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
    """Git configuration exposure, split by how much of the host it reveals.

    ``identity`` seeds *only* ``user.name`` and ``user.email``, read one key
    at a time via ``git config --get``. That is what commit attribution
    actually needs and it is not a secret, so it defaults on.

    ``full_config`` copies the host's entire git configuration instead. A real
    ``~/.gitconfig`` routinely carries far more than identity --
    ``credential.helper`` entries, ``http.<url>.extraHeader`` values holding
    bearer tokens, ``url.insteadOf`` rewrites, and ``include.path``
    directives pulling in further files. Handing all of that to an agent
    container is a deliberate act, so it defaults off.
    """

    identity: bool = True
    full_config: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"identity": self.identity, "full_config": self.full_config}

    @classmethod
    def from_dict(cls, data: Any) -> "GitConfig":
        return cls(identity=_bool(data, "identity", True), full_config=_bool(data, "full_config", False))


@dataclass
class ProvidersConfig:
    """Shell variables forwarded into the container, split by sensitivity.

    ``api_keys`` covers the ones that *are* the credential
    (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``). Having one exported in
    the shell that launched agent-box is not by itself a decision to hand it
    to an agent, so this is off by default: Claude and Codex normally
    authenticate through the mounted ``~/.claude``/``~/.codex`` directories
    and need no key in the environment at all.

    ``transport`` covers endpoint and plumbing settings that select *where*
    and *how* requests go -- base URL, Bedrock/Vertex selectors, proxies, CA
    bundles. Without them a container behind a corporate proxy or on a
    third-party endpoint simply cannot reach the network, so these default on.
    Note that a proxy URL can itself embed credentials
    (``http://user:pass@host``); if that is your situation, turn this off.
    """

    api_keys: bool = False
    transport: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"api_keys": self.api_keys, "transport": self.transport}

    @classmethod
    def from_dict(cls, data: Any) -> "ProvidersConfig":
        return cls(api_keys=_bool(data, "api_keys", False), transport=_bool(data, "transport", True))


@dataclass
class VikunjaConfig:
    """Exposure of the host's Vikunja skill credentials to the container.

    Off by default, like every other host integration here: the skill's
    ``~/.config/vikunja`` directory holds an API token that can read and
    rewrite the user's entire task tracker, and having that file on the host
    is not by itself a decision to hand it to an agent.

    One switch rather than several, because agent-box stores no Vikunja
    settings of its own. Turning this on exposes exactly three things, all
    derived from what the host already has: the config directory read-only
    (URL + token), the import state directory read-write (so a partially
    applied ``vikunja import`` stays resumable across boxes), and the skill's
    own CLI on the container ``PATH``. The server URL is additionally
    rewritten for the container's network namespace -- see
    ``runtime.container_vikunja_url``.
    """

    enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Any) -> "VikunjaConfig":
        return cls(enabled=_bool(data, "enabled", False))


#: Flow-Next review routing is selected by *primary CLI*, not by any quota or
#: cost signal -- agent-box never inspects usage to pick a backend, it just
#: looks up which agent launched. Codex reviewing Codex is intentional for
#: now: Flow-Next has no native executable Claude review backend yet, so the
#: "claude" mapping also points at Codex until upstream adds one. Changing
#: that later is a config edit, not a code change.
DEFAULT_FLOW_REVIEW_BACKENDS: dict[str, str] = {
    "claude": "codex:gpt-5.6-sol:medium",
    "codex": "codex:gpt-5.6-sol:medium",
}
FLOW_REVIEW_BACKEND_AGENTS = ("claude", "codex")


@dataclass
class FlowNextConfig:
    """Deterministic Flow-Next runtime policy, owned by agent-box.

    ``review_backends`` maps the *primary* agent (``claude``/``codex``) to a
    full Flow-Next backend spec (``provider:model:effort``) forwarded to the
    launch as ``FLOW_REVIEW_BACKEND``. Values are opaque strings agent-box
    never parses -- changing the routing later (e.g. once Flow-Next gains a
    native Claude review backend) is a config edit, not a code change.

    ``auto_push`` is the opt-in Flow-Next auto-push reconciler (see
    ``agent_box.flow_push``), applied identically to Claude and Codex
    launches; it defaults off because this must never turn every agent turn
    into a git push.
    """

    review_backends: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_FLOW_REVIEW_BACKENDS))
    auto_push: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"review_backends": dict(self.review_backends), "auto_push": self.auto_push}

    @classmethod
    def from_dict(cls, data: Any) -> "FlowNextConfig":
        backends = dict(DEFAULT_FLOW_REVIEW_BACKENDS)
        raw = data.get("review_backends") if isinstance(data, dict) else None
        if isinstance(raw, dict):
            for agent in FLOW_REVIEW_BACKEND_AGENTS:
                value = raw.get(agent)
                if isinstance(value, str) and value.strip():
                    backends[agent] = value.strip()
        return cls(review_backends=backends, auto_push=_bool(data, "auto_push", False))


@dataclass
class Config:
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    git: GitConfig = field(default_factory=GitConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    vikunja: VikunjaConfig = field(default_factory=VikunjaConfig)
    flow_next: FlowNextConfig = field(default_factory=FlowNextConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "claude": self.claude.to_dict(),
            "notifications": self.notifications.to_dict(),
            "github": self.github.to_dict(),
            "git": self.git.to_dict(),
            "providers": self.providers.to_dict(),
            "vikunja": self.vikunja.to_dict(),
            "flow_next": self.flow_next.to_dict(),
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
            vikunja=VikunjaConfig.from_dict(data.get("vikunja")),
            flow_next=FlowNextConfig.from_dict(data.get("flow_next")),
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
