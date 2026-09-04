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
      "env": {"forward": [], "set": {}},
      "skills": {"claude_links": true},
      "vikunja": {"enabled": false, "url": "", "token": ""},
      "browser": {"enabled": false},
      "flow_next": {"review_backends": {}, "auto_push": false}
    }

``version`` stays at 1 while the schema keeps changing, because agent-box has
not been released: there is no installed base to migrate, so a section that
grows or changes shape is simply the new shape, and a file written by an
older working copy loads through the same "partial file" tolerance every
``from_dict`` here already provides.
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
    return str(value).strip() if isinstance(value, (str, int)) else ""


def _is_env_name(name: str) -> bool:
    """A POSIX-shaped variable name, so a malformed entry cannot inject argv.

    Everything here ends up as a ``--env`` flag. A name carrying ``=`` or
    whitespace would either split into something else entirely or silently
    define a different variable, so a bad entry is dropped on load rather
    than carried to Podman.
    """
    return bool(name) and not name[0].isdigit() and all(
        c.isalnum() or c == "_" for c in name)


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
    """The whole Vikunja integration: the switch, the server, the token.

    Off by default, like every other integration that reaches outside the
    box: the token can read and rewrite the user's entire task tracker.

    agent-box owns these two values rather than reading a configuration file
    the skill also owns. That is what makes the integration installable
    instead of merely detectable -- ``agent-box config`` asks for a URL and a
    token, the skill itself is installed from the copy shipped in this
    package (see ``runtime.install_managed_skills``), and nothing has to
    exist on the host beforehand. The container never sees this file: the
    host renders a two-file credential directory from it
    (``runtime.sync_vikunja_credentials``) and mounts that read-only, the
    same way notifications derive their own credential file.

    ``url`` is stored exactly as the *host* reaches the server. The rewrite
    that makes it reachable from inside a container happens at render time --
    see ``runtime.container_vikunja_url`` -- so the value here stays the one
    the user typed and keeps working if they later stop using containers.
    """

    enabled: bool = False
    url: str = ""
    token: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.url and self.token)

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "url": self.url, "token": self.token}

    @classmethod
    def from_dict(cls, data: Any) -> "VikunjaConfig":
        return cls(
            enabled=_bool(data, "enabled", False),
            url=_str(data, "url"),
            token=_str(data, "token"),
        )


@dataclass
class SkillsConfig:
    """How the shared agent skill tree is wired up.

    ``claude_links`` mirrors ``~/.agents/skills`` into ``~/.claude/skills``
    with symlinks so Claude sees the same ``SKILL.md`` files Codex reads
    directly. It defaults on because sharing skills between the two agents is
    the point of the tree, but it is a switch rather than an assumption: the
    links land in the user's real Claude configuration, where they also
    affect Claude sessions started outside agent-box, and someone who manages
    that directory themselves must be able to say no.
    """

    claude_links: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"claude_links": self.claude_links}

    @classmethod
    def from_dict(cls, data: Any) -> "SkillsConfig":
        return cls(claude_links=_bool(data, "claude_links", True))


@dataclass
class EnvConfig:
    """Arbitrary environment variables the user wants inside the box.

    The fixed ``providers`` lists above cover the variables agent-box knows
    the *agents* need. This one exists because a project may need anything at
    all -- a registry endpoint, a database URL, a license key -- and having
    to patch agent-box to pass one through would be absurd.

    Two mechanisms, split by where the value lives, because the difference is
    a security one. ``forward`` names variables inherited from the shell that
    launched the session and is applied as ``--env=NAME``, so the value never
    enters the Podman argv, which is world-readable through ``/proc``; that
    is the form to use for anything secret. ``set`` carries literal
    ``NAME=VALUE`` pairs written into this config file and into the argv,
    which is right for a timezone or a feature flag and wrong for a token.

    Applied per launch rather than baked into the box, so changing a value in
    the shell takes effect on the next ``agent-box claude`` without
    recreating the workspace container.
    """

    forward: list[str] = field(default_factory=list)
    set: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"forward": list(self.forward), "set": dict(self.set)}

    @classmethod
    def from_dict(cls, data: Any) -> "EnvConfig":
        raw_forward = data.get("forward") if isinstance(data, dict) else None
        forward = [
            name.strip() for name in raw_forward
            if isinstance(name, str) and _is_env_name(name.strip())
        ] if isinstance(raw_forward, list) else []
        raw_set = data.get("set") if isinstance(data, dict) else None
        assignments = {
            name.strip(): str(value) for name, value in raw_set.items()
            if isinstance(name, str) and _is_env_name(name.strip())
            and isinstance(value, (str, int, float, bool))
        } if isinstance(raw_set, dict) else {}
        return cls(forward=forward, set=assignments)


@dataclass
class BrowserConfig:
    """An agent-controlled headless browser inside the workspace container.

    Off by default, like every other capability that widens what an agent can
    reach. Turning it on makes a Playwright MCP server available to
    containerized Claude and Codex sessions, which drives a headless Chromium
    baked into the image.

    This exposes no host state at all -- there is no host browser profile, no
    host cookie jar and no credential of any kind involved, and the switch
    therefore holds nothing secret. What it does mean is that pages the agent
    opens land *inside the workspace box*: the browser, the MCP server, the
    agent, the mounted project and every other enabled integration are one
    trust boundary, so anything the browser fetches is reachable by everything
    else in that box. That is the decision this switch is asking about.

    Deliberately one boolean and nothing else. Each MCP process gets an
    isolated, non-persistent profile and a session-private output directory
    derived from the workspace's runtime state (see
    ``agent_box.runtime.browser_session``), so there is no profile path,
    cookie file or credential for a user to configure -- and no place for one
    to be stored.
    """

    enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Any) -> "BrowserConfig":
        return cls(enabled=_bool(data, "enabled", False))


#: Flow-Next review routing is selected by *primary CLI*, not by any quota or
#: cost signal -- agent-box never inspects usage to pick a backend, it just
#: looks up which agent launched.
#:
#: Empty by default, deliberately. A backend spec names a concrete provider,
#: model and effort level, and shipping one as a default would silently point
#: every Flow-Next workspace on every machine at a model the user may not
#: have, may not pay for, or may not want reviewing their code. With no entry
#: agent-box simply sets no ``FLOW_REVIEW_BACKEND`` and Flow-Next falls back
#: to its own ``.flow/config.json`` and defaults, which is the correct
#: behavior for a launcher that was never told what to prefer.
DEFAULT_FLOW_REVIEW_BACKENDS: dict[str, str] = {}
FLOW_REVIEW_BACKEND_AGENTS = ("claude", "codex")

#: Offered by ``agent-box config`` when Flow-Next routing is being set up.
#: A menu rather than free text because a mistyped model name fails only
#: later, inside a review Flow-Next has already started. Not a default: an
#: empty answer stays empty.
FLOW_REVIEW_BACKEND_CHOICES = (
    "codex:gpt-5.6-sol:medium",
    "codex:gpt-5.6-sol:high",
)


@dataclass
class FlowNextConfig:
    """Deterministic Flow-Next runtime policy, owned by agent-box.

    ``review_backends`` maps the *primary* agent (``claude``/``codex``) to a
    full Flow-Next backend spec (``provider:model:effort``) forwarded to the
    launch as ``FLOW_REVIEW_BACKEND``. Values are opaque strings agent-box
    never parses, and an agent with no entry gets no variable at all, so
    Flow-Next keeps its own resolution. Changing the routing later is a
    config edit, not a code change.

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
                else:
                    backends.pop(agent, None)
        return cls(review_backends=backends, auto_push=_bool(data, "auto_push", False))


@dataclass
class Config:
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    git: GitConfig = field(default_factory=GitConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    env: EnvConfig = field(default_factory=EnvConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    vikunja: VikunjaConfig = field(default_factory=VikunjaConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    flow_next: FlowNextConfig = field(default_factory=FlowNextConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "claude": self.claude.to_dict(),
            "notifications": self.notifications.to_dict(),
            "github": self.github.to_dict(),
            "git": self.git.to_dict(),
            "providers": self.providers.to_dict(),
            "env": self.env.to_dict(),
            "skills": self.skills.to_dict(),
            "vikunja": self.vikunja.to_dict(),
            "browser": self.browser.to_dict(),
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
            env=EnvConfig.from_dict(data.get("env")),
            skills=SkillsConfig.from_dict(data.get("skills")),
            vikunja=VikunjaConfig.from_dict(data.get("vikunja")),
            browser=BrowserConfig.from_dict(data.get("browser")),
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
