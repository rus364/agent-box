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

The workflow and tracker sections are opt-in. Provider connections hold
credentials globally; repository bindings contain only project identity and
project-specific lifecycle policy. Version remains 1 during pre-release.
"""
from __future__ import annotations

import os
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
class SkillsConfig:
    """Claude links expose the shared skills to host Claude sessions as well."""
    claude_links: bool = True

    def to_dict(self):
        return {"claude_links": self.claude_links}

    @classmethod
    def from_dict(cls, data):
        return cls(claude_links=_bool(data, "claude_links", True))


@dataclass
class InstallConfig:
    """Which directory agent-box keeps its own ``agent-box`` symlink in.

    Stored because the symlink is the one part of setup that something else
    can undo: a directory cleaned by a version manager, a home restored from
    backup, a ``~/.local/bin`` rebuilt by another installer. Remembering the
    answer turns "run the menu again and find the PATH question" into
    ``agent-box config apply``, which relinks whatever went missing.

    Empty means agent-box maintains no symlink -- the honest default, since a
    user who declined the question, or who links the shim themselves, must not
    have one appear later. The value is a directory the user picked from
    :func:`agent_box.pathsetup.bin_dir_choices`; the safety rules about what
    may be replaced live in ``install_symlink`` and apply every time.
    """
    bin_dir: str = ""

    def to_dict(self):
        return {"bin_dir": self.bin_dir}

    @classmethod
    def from_dict(cls, data):
        value = data.get("bin_dir") if isinstance(data, dict) else None
        return cls(bin_dir=value if isinstance(value, str) else "")


#: Namespace for the shell-prefix spelling: ``AGENT_BOX_ENV_FOO=bar agent-box
#: codex`` puts ``FOO=bar`` in the container.
#:
#: A prefix is unavoidable. A variable set by a shell prefix
#: (``FOO=bar agent-box codex``) is indistinguishable, inside the process,
#: from one exported in a login profile -- both are simply entries in the
#: environment -- so forwarding bare ``FOO`` would mean forwarding the entire
#: host environment, which is exactly what a workspace box exists not to do.
#: Naming agent-box's own namespace is what makes the intent legible.
ENV_PREFIX = "AGENT_BOX_ENV_"


def environment_overrides(environ: Any = None) -> dict[str, str]:
    """``AGENT_BOX_ENV_FOO=bar`` in the launching shell, as ``{"FOO": "bar"}``."""
    environ = os.environ if environ is None else environ
    overrides = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX):]
        if _is_env_name(name):
            overrides[name] = str(value)
    return overrides


def parse_env_spec(spec: str) -> tuple[str, str | None]:
    """One ``--env`` argument: ``NAME=VALUE`` to set, ``NAME`` to forward.

    Forwarding by bare name is the form for anything secret: the value is
    inherited from the launching shell by name and never enters the Podman
    argv, which ``/proc`` exposes to other processes on the host.
    """
    name, sep, value = spec.partition("=")
    name = name.strip()
    if not _is_env_name(name):
        raise ValueError(
            f"'{spec}' is not a variable name; expected NAME=VALUE or NAME")
    return (name, value if sep else None)


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

    def overlay(
        self, specs: list[tuple[str, str | None]] | None = None,
        environ: Any = None,
    ) -> "EnvConfig":
        """This launch's effective environment policy.

        Merges the stored lists with the two per-invocation channels, in
        increasing order of specificity: the config file, then
        ``AGENT_BOX_ENV_*`` variables in the launching shell, then ``--env``
        flags. Each later source wins for a name it mentions, because each is
        a more deliberate statement about *this* run than the one before it.

        Returns a new value rather than mutating, so the stored configuration
        is never quietly rewritten by a one-off launch.
        """
        forward = list(self.forward)
        assignments = dict(self.set)

        def assign(name: str, value: str) -> None:
            assignments[name] = value
            if name in forward:
                forward.remove(name)

        for name, value in sorted(environment_overrides(environ).items()):
            assign(name, value)
        for name, value in specs or []:
            if value is None:
                assignments.pop(name, None)
                if name not in forward:
                    forward.append(name)
            else:
                assign(name, value)
        return EnvConfig(forward=forward, set=assignments)

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


#: The optional tool groups the image can carry, and whether each is built by
#: default. The base image -- agents, Python, Node, git, shell tooling -- is
#: not a group: it is what every workspace needs and is never optional.
#:
#: On by default where the cost is small and the reach is wide; off where the
#: group is large and most projects never touch it. Anything not listed here
#: belongs in the project, installed through mise, rather than in an image
#: everyone shares.
IMAGE_TOOL_GROUPS: dict[str, bool] = {
    # shellcheck, sqlite3, jscpd -- what an agent reaches for when reading code
    "analysis": True,
    # kubectl, Helm, yq
    "kubernetes": True,
    # QEMU x86-64/aarch64, OVMF; also gates the /dev/kvm passthrough
    "virtualization": False,
    # Playwright MCP + headless Chromium; the browser integration needs it
    "browser": False,
}


#: What each group is, in the words `agent-box config` asks the question in.
IMAGE_TOOL_GROUP_HELP: dict[str, str] = {
    "analysis": "Code analysis (shellcheck, sqlite3, jscpd)",
    "kubernetes": "Kubernetes (kubectl, Helm, yq)",
    "virtualization": "Virtualization (QEMU x86-64/arm64, KVM passthrough; large)",
    "browser": "Headless browser (Playwright MCP + Chromium; large)",
}


@dataclass
class ImageConfig:
    """Which optional tool groups the workspace image is built with.

    Separate from the switches that turn a *capability* on, because these are
    a property of the image rather than of a launch: changing one means
    rebuilding. ``browser`` is the pair to watch -- ``browser.enabled`` wires
    the MCP server into a session, and this group decides whether the image
    has a browser for it to drive at all.

    Unknown group names are dropped on load rather than carried, so the file
    can never ask the build for a group that no longer exists.
    """

    tools: dict[str, bool] = field(
        default_factory=lambda: dict(IMAGE_TOOL_GROUPS))

    def enabled_groups(self) -> list[str]:
        """The groups to build, in a stable order for the image label."""
        return sorted(name for name, on in self.tools.items() if on)

    def to_dict(self) -> dict[str, Any]:
        return {"tools": {name: bool(self.tools.get(name, default))
                          for name, default in IMAGE_TOOL_GROUPS.items()}}

    @classmethod
    def from_dict(cls, data: Any) -> "ImageConfig":
        raw = data.get("tools") if isinstance(data, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        return cls(tools={
            name: _bool(raw, name, default)
            for name, default in IMAGE_TOOL_GROUPS.items()
        })


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


TRACKER_PROVIDERS = ("vikunja", "jira", "linear", "github")
AGENT_PROVIDERS = ("codex", "claude")
AGENT_EFFORTS = {
    "codex": ("default", "low", "medium", "high", "xhigh", "max", "ultra"),
    "claude": ("default", "low", "medium", "high", "xhigh", "max"),
}
WORKFLOW_SKILLS = ("ab-setup", "ab-adopt", "ab-interview", "ab-plan", "ab-work", "ab-review", "ab-pr", "ab-land")
REVIEW_PRESETS = {"thorough": (2, 80), "balanced": (5, 200), "light": (10, 500)}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _number(data, key, default):
    value = _mapping(data).get(key, default)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


@dataclass
class TrackerConnection:
    url: str = ""
    token: str = ""

    @property
    def usable(self):
        return bool(self.url and self.token)

    def to_dict(self):
        return {"url": self.url, "token": self.token}

    @classmethod
    def from_dict(cls, data):
        return cls(url=_str(data, "url"), token=_str(data, "token"))


@dataclass
class TrackerConfig:
    """Disabled until the user explicitly exposes configured tracker accounts."""
    enabled: bool = False
    provider: str = "vikunja"
    connections: dict[str, TrackerConnection] = field(default_factory=dict)

    def connection(self, provider=None):
        return self.connections.get(provider or self.provider, TrackerConnection())

    def to_dict(self):
        return {"enabled": self.enabled, "provider": self.provider,
                "connections": {k: v.to_dict() for k, v in self.connections.items()}}

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        return cls(_bool(data, "enabled", False), _str(data, "provider") or "vikunja",
                   {k: TrackerConnection.from_dict(v) for k, v in
                    _mapping(data.get("connections")).items() if k in TRACKER_PROVIDERS})


def model_tiers(numbers, data=None):
    """Keep role matrices independent while sharing model/effort defaults."""
    data = _mapping(data)
    result = {}
    for number in numbers:
        tier = result[str(number)] = {}
        for provider in AGENT_PROVIDERS:
            spec = _mapping(_mapping(data.get(str(number))).get(provider))
            tier[provider] = {"model": _str(spec, "model"), "effort": _str(spec, "effort") or "medium"}
    return result


@dataclass
class ReviewConfig:
    tiers: dict[str, dict] = field(default_factory=lambda: model_tiers(range(1, 4)))
    policy: str = "auto"
    routing: str = "cross-provider"
    max_files: int = 5
    max_lines: int = 200
    max_rounds: int = 3
    sensitive_paths: list[str] = field(default_factory=lambda: [
        "**/auth/**", "**/security/**", "**/migrations/**", "**/skills/**",
        "**/config*", ".github/**", "**/*schema*", "**/api/**"])
    label: str = "ab:review"
    github_provider: str = "auto"
    github_model: str = ""
    github_effort: str = ""
    wait_seconds: int = 600

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        result = cls()
        result.tiers = model_tiers(range(1, 4), data.get("tiers"))
        for key in ("policy", "routing", "label", "github_provider", "github_model", "github_effort"):
            if key in data and isinstance(data[key], str):
                setattr(result, key, data[key].strip())
        for key in ("max_files", "max_lines", "max_rounds", "wait_seconds"):
            setattr(result, key, _number(data, key, getattr(result, key)))
        if isinstance(data.get("sensitive_paths"), list):
            result.sensitive_paths = [s for s in data["sensitive_paths"] if isinstance(s, str)]
        return result


@dataclass
class WorkflowConfig:
    """Optional workflow policy; only enabled integrations may publish or delegate.

    auto_push defaults on *within this opt-in workflow*, as requested, and
    publishes task branches only with GitHub integration in GitHub mode. Local
    mode publishes the accepted base using available Git transport credentials;
    it neither enables credentials nor publishes task branches.
    Routing counters and usage caches contain no authoritative task state.
    """
    enabled: bool = False
    merge_mode: str = "github"
    routing: str = "current"
    weights: dict[str, float] = field(default_factory=lambda: {"codex": 1, "claude": 1})
    tiers: dict[str, dict] = field(default_factory=lambda: model_tiers(range(4)))
    fallback: bool = True
    reserve_percent: float = 5
    smoothing_hours: float = 24
    scout_limit: int = 3
    auto_push: bool = True
    timeout_seconds: int = 3600
    review: ReviewConfig = field(default_factory=ReviewConfig)
    cleanup_local_branches: bool = False
    cleanup_remote_branches: bool = False

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)

    def validate(self):
        import math
        errors = []
        if self.merge_mode not in ("github", "local"):
            errors.append("workflow.merge_mode must be github or local")
        if self.routing not in ("current", "weighted", "usage-aware"):
            errors.append("workflow.routing must be current, weighted, or usage-aware")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
               for v in self.weights.values()) or not any(self.weights.values()):
            errors.append("workflow.weights must be finite, nonnegative, and not all zero")
        for name, minimum, maximum in (("reserve_percent", 0, 99), ("smoothing_hours", 1, 168),
                                        ("scout_limit", 1, 16), ("timeout_seconds", 30, 86400)):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum or name in ("scout_limit", "timeout_seconds") and not isinstance(value, int):
                errors.append(f"workflow.{name} must be between {minimum} and {maximum}")
        if self.review.routing not in ("cross-provider", "same-provider"):
            errors.append("workflow.review.routing must be cross-provider or same-provider")
        if self.review.policy not in ("off", "auto", "always"):
            errors.append("workflow.review.policy must be off, auto, or always")
        if self.review.github_provider not in ("auto", "local", "codex", "claude"):
            errors.append("workflow.review.github_provider must be auto, local, codex, or claude")
        for name in ("max_files", "max_lines", "max_rounds", "wait_seconds"):
            value = getattr(self.review, name)
            if not isinstance(value, int) or value < 1:
                errors.append(f"workflow.review.{name} must be a positive integer")
        for path, tiers in (("workflow.tiers", self.tiers),
                            ("workflow.review.tiers", self.review.tiers)):
            for tier, models in tiers.items():
                for provider, spec in models.items():
                    if spec.get("effort") not in AGENT_EFFORTS.get(provider, ()):
                        errors.append(f"{path}.{tier}.{provider}: invalid effort")
        return errors

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        result = cls()
        result.enabled = _bool(data, "enabled", False)
        result.merge_mode = _str(data, "merge_mode") or result.merge_mode
        result.routing = _str(data, "routing") or result.routing
        for key in ("fallback", "auto_push", "cleanup_local_branches", "cleanup_remote_branches"):
            setattr(result, key, _bool(data, key, getattr(result, key)))
        for key in ("reserve_percent", "smoothing_hours", "scout_limit", "timeout_seconds"):
            setattr(result, key, _number(data, key, getattr(result, key)))
        weights = _mapping(data.get("weights"))
        result.weights = {p: _number(weights, p, 1) for p in AGENT_PROVIDERS}
        result.tiers = model_tiers(range(4), data.get("tiers"))
        result.review = ReviewConfig.from_dict(data.get("review"))
        return result


@dataclass
class Config:
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    git: GitConfig = field(default_factory=GitConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    env: EnvConfig = field(default_factory=EnvConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    install: InstallConfig = field(default_factory=InstallConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)

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
            "install": self.install.to_dict(),
            "image": self.image.to_dict(),
            "tracker": self.tracker.to_dict(),
            "browser": self.browser.to_dict(),
            "workflow": self.workflow.to_dict(),
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
            install=InstallConfig.from_dict(data.get("install")),
            image=ImageConfig.from_dict(data.get("image")),
            tracker=TrackerConfig.from_dict(data.get("tracker")),
            browser=BrowserConfig.from_dict(data.get("browser")),
            workflow=WorkflowConfig.from_dict(data.get("workflow")),
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


def effective_config() -> Config:
    """Inside a box, read only the rendered capability subset, never host config."""
    import os
    runtime_file = os.environ.get("AGENT_BOX_RUNTIME_CONFIG")
    if not runtime_file:
        return load_config()
    from .errors import AgentBoxError
    try:
        return Config.from_dict(read_json(Path(runtime_file)))
    except (OSError, ValueError, TypeError) as exc:
        raise AgentBoxError(f"Cannot read rendered workflow configuration: {exc}") from None


def effective_config_source() -> dict[str, str]:
    """Identify the source without dumping the credentials it may contain."""
    runtime_file = os.environ.get("AGENT_BOX_RUNTIME_CONFIG")
    if runtime_file:
        return {"kind": "runtime", "path": str(Path(runtime_file))}
    return {"kind": "host" if CONFIG_FILE.is_file() else "defaults", "path": str(CONFIG_FILE)}
