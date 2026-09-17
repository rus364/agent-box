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
credentials globally; project_config validates the explicit repository subset
for launch and workflow policy alongside tracker identity. Version remains 1 during pre-release.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .paths import CONFIG_FILE
from .external_config import API_EFFORTS, ExternalProvider, parse_connections, valid_name

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
class StatuslineConfig:
    """Claude's status line: whether agent-box keeps the slot, and its extra segment.

    Both describe the same line, so they share a section; the command also
    feeds ``agent-box usage all``, which prints the same segments.
    """
    #: Keep Claude's ``statusLine`` pointed at ``agent-box statusline``.
    #:
    #: Defaults on, unlike every other integration here, because it is the one
    #: setting that exposes nothing: no credential leaves the host and nothing
    #: reaches the container that was not already mounted. What it costs is a
    #: line of Claude's own UI, which agent-box gives back the moment the
    #: setting is turned off. A status line someone else configured is never
    #: overwritten -- see ``agent_box.claude_settings.reconcile_statusline``.
    enabled: bool = True
    #: Shell command whose first output line follows the Claude and Codex usage
    #: segments. Empty shows nothing extra; set, it runs wherever the line is
    #: drawn, which for Claude's status line is inside the workspace box.
    command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "command": self.command}

    @classmethod
    def from_dict(cls, data: Any) -> "StatuslineConfig":
        return cls(enabled=_bool(data, "enabled", True), command=_str(data, "command"))


@dataclass
class ClaudeConfig:
    #: Remote access is opt-in and applies to new interactive launches only.
    remote_control: bool = False
    #: Start `agent-box claude` with --channels plugin:telegram@claude-plugins-official.
    telegram_channel: bool = False
    statusline: StatuslineConfig = field(default_factory=StatuslineConfig)

    def to_dict(self) -> dict[str, Any]:
        return {"telegram_channel": self.telegram_channel, "remote_control": self.remote_control,
                "statusline": self.statusline.to_dict()}

    @classmethod
    def from_dict(cls, data: Any) -> "ClaudeConfig":
        return cls(
            remote_control=_bool(data, "remote_control", False),
            telegram_channel=_bool(data, "telegram_channel", False),
            statusline=StatuslineConfig.from_dict(_mapping(data).get("statusline")),
        )


@dataclass
class CodexConfig:
    #: Remote access is opt-in; a session never switches the shared daemon.
    remote_control: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"remote_control": self.remote_control}

    @classmethod
    def from_dict(cls, data: Any) -> "CodexConfig":
        return cls(remote_control=_bool(data, "remote_control", False))


LAUNCH_AGENTS = ("claude", "codex")


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
    # Nested rootless Podman; gates ID capabilities, TUN and namespace setup
    "podman": False,
}


#: What each group is, in the words `agent-box config` asks the question in.
IMAGE_TOOL_GROUP_HELP: dict[str, str] = {
    "analysis": "Code analysis (shellcheck, sqlite3, jscpd)",
    "kubernetes": "Kubernetes (kubectl, Helm, yq)",
    "virtualization": "Virtualization (QEMU x86-64/arm64, KVM passthrough; large)",
    "browser": "Headless browser (Playwright MCP + Chromium; large)",
    "podman": "Nested Podman (rootless builds/containers; extra namespace permissions)",
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


@dataclass
class AudioConfig:
    """The host's sound server inside workspace boxes.

    Off by default: turning it on lets everything in the box -- agents, their
    tools, the project's own code -- record from the host microphone and play
    through its speakers. The PulseAudio socket grants both directions and
    control of the server, so there is deliberately one switch rather than a
    microphone/speaker pair that the protocol could not actually separate.

    The socket, not ``/dev/snd``: raw ALSA devices would contend with the
    host's PipeWire or PulseAudio for exclusive access, while the socket works
    with either server. Structural, because a bind mount is part of the box.
    """

    enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Any) -> "AudioConfig":
        return cls(enabled=_bool(data, "enabled", False))


TRACKER_PROVIDERS = ("vikunja", "jira", "linear", "github")
AGENT_PROVIDERS = ("codex", "claude")
AGENT_EFFORTS = {
    "codex": ("default", "low", "medium", "high", "xhigh", "max", "ultra"),
    "claude": API_EFFORTS['claude'],
}
WORKFLOW_SKILLS = ("ab-setup", "ab-adopt", "ab-normalize", "ab-interview", "ab-plan", "ab-work", "ab-review", "ab-pr", "ab-land")
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


MODEL_ENTRY = {"provider": str, "model": str, "effort": str, "fast": bool, "api_provider": str}
#: Tiers each role runs at; tier 0 is reserved for scouts.
ROLE_TIERS = {"scout": range(4), "worker": range(1, 4), "reviewer": range(1, 4)}


def model_entry(data):
    """One candidate: the CLI that runs it, and the model it runs.

    The provider is stored rather than derived from the model name, because an
    external API model (DeepSeek through Claude's CLI) names no provider.
    """
    data = _mapping(data)
    # Kept as written, not coerced: a wrong type must reach validate() at its path.
    return {"provider": data.get("provider", ""), "model": data.get("model", ""),
            "effort": data.get("effort", "medium"), "fast": data.get("fast", False),
            "api_provider": data.get("api_provider", "")}


def candidate_label(spec):
    """``PROVIDER[@API]:MODEL:EFFORT``, the spelling --worker/--scout/--reviewer accept."""
    api = f"@{spec['api_provider']}" if spec.get("api_provider") else ""
    return f"{spec['provider']}{api}:{spec['model']}:{spec['effort']}" + (" fast" if spec.get("fast") is True else "")


def model_list(data=None):
    """Candidates in preference order; a role runs the first one available.

    A malformed entry becomes an empty candidate that validation rejects at its
    index; dropping it would silently shorten the list, or empty it and let
    untiered work inherit the session instead.
    """
    if data is None:
        return []
    return [model_entry(item) for item in data] if isinstance(data, list) else data


def model_tiers(numbers, data=None):
    """Known tiers as lists; a wrong container or unknown tier stays for validate() to name."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return data
    known = {str(number) for number in numbers}
    return {**{tier: model_list(data.get(tier)) for tier in sorted(known)},
            **{tier: value for tier, value in data.items() if tier not in known}}


@dataclass
class WorkerConfig:
    execution: str = "auto"
    models: list[dict] = field(default_factory=list)
    tiers: dict[str, list] = field(default_factory=lambda: model_tiers(ROLE_TIERS["worker"]))

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        return cls(data.get("execution", "auto"), model_list(data.get("models")),
                   model_tiers(ROLE_TIERS["worker"], data.get("tiers")))


@dataclass
class ScoutConfig:
    limit: int = 3
    models: list[dict] = field(default_factory=list)
    tiers: dict[str, list] = field(default_factory=lambda: model_tiers(ROLE_TIERS["scout"]))

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        return cls(_number(data, "limit", 3), model_list(data.get("models")),
                   model_tiers(ROLE_TIERS["scout"], data.get("tiers")))


@dataclass
class ReviewConfig:
    policy: str = "auto"
    routing: str = "cross-provider"
    models: list[dict] = field(default_factory=list)
    tiers: dict[str, list] = field(default_factory=lambda: model_tiers(ROLE_TIERS["reviewer"]))
    max_files: int = 5
    max_lines: int = 200
    max_rounds: int = 3
    sensitive_paths: list[str] = field(default_factory=lambda: [
        "**/auth/**", "**/security/**", "**/migrations/**", "**/skills/**",
        "**/config*", ".github/**", "**/*schema*", "**/api/**"])
    ignored_paths: list[str] = field(default_factory=list)
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
        result.models = model_list(data.get("models"))
        result.tiers = model_tiers(ROLE_TIERS["reviewer"], data.get("tiers"))
        for key in ("policy", "routing", "label", "github_provider", "github_model", "github_effort"):
            if key in data and isinstance(data[key], str):
                setattr(result, key, data[key].strip())
        for key in ("max_files", "max_lines", "max_rounds", "wait_seconds"):
            setattr(result, key, _number(data, key, getattr(result, key)))
        for key in ("sensitive_paths", "ignored_paths"):
            if isinstance(data.get(key), list):
                setattr(result, key, [s for s in data[key] if isinstance(s, str)])
        return result


@dataclass
class WorkflowConfig:
    """Optional workflow policy; only enabled integrations may publish or delegate.

    auto_push defaults on *within this opt-in workflow*, as requested, and
    publishes task branches only with GitHub integration in GitHub mode. Local
    mode publishes the accepted base using available Git transport credentials;
    it neither enables credentials nor publishes task branches.
    """
    enabled: bool = False
    merge_mode: str = "github"
    scout: ScoutConfig = field(default_factory=ScoutConfig)
    auto_push: bool = True
    timeout_seconds: int = 3600
    review: ReviewConfig = field(default_factory=ReviewConfig)
    cleanup_local_branches: bool = False
    cleanup_remote_branches: bool = False
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    #: Launch-only candidate lists pinned by --worker/--reviewer/--scout, keyed by role.
    #: Never serialized: a session re-derives them from its snapshot's launch
    #: flags, so no configuration file can hold one.
    pins: dict[str, list] = field(default_factory=dict)

    def to_dict(self):
        from dataclasses import asdict
        data = asdict(self)
        data.pop("pins")
        return data

    def role(self, role):
        return {"scout": self.scout, "worker": self.worker, "reviewer": self.review}[role]

    def models_for(self, role, tier):
        """``(path, candidates)``: a session pin, else a nonempty tier list, else the role's own list.

        The candidate's provider decides only how it runs, never whether the
        coordinator may use it: a Claude session runs a Codex scout as a
        managed CLI. Per-provider slots filtered by the session provider were
        the rejected shape, since a configured model then vanished whenever
        the other CLI coordinated.
        """
        if role in self.pins:
            return f"--{role}", self.pins[role]
        section = "review" if role == "reviewer" else role
        settings = self.role(role)
        # Wrong containers are validate()'s errors; unvalidated readers such as profile sync must not crash.
        if tier is not None and isinstance(settings.tiers, dict) and settings.tiers.get(str(tier)):
            return f"workflow.{section}.tiers.{tier}", settings.tiers[str(tier)]
        return f"workflow.{section}.models", settings.models

    def validate(self):
        import math
        errors = []
        if self.merge_mode not in ("github", "local"):
            errors.append("workflow.merge_mode must be github or local")
        if self.worker.execution not in ("auto", "direct", "delegated"):
            errors.append("workflow.worker.execution must be auto, direct, or delegated")
        value = self.timeout_seconds
        if not isinstance(value, int) or not math.isfinite(value) or not 30 <= value <= 86400:
            errors.append("workflow.timeout_seconds must be between 30 and 86400")
        if not isinstance(self.scout.limit, int) or not 1 <= self.scout.limit <= 16:
            errors.append("workflow.scout.limit must be between 1 and 16")
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
        from .fast_mode import configuration_error
        for role, tiers in ROLE_TIERS.items():
            section = "review" if role == "reviewer" else role
            settings = self.role(role)
            if not isinstance(settings.models, list):
                errors.append(f"workflow.{section}.models must be a list of candidates")
            if not isinstance(settings.tiers, dict):
                errors.append(f"workflow.{section}.tiers must be an object keyed by tier")
                continue
            for tier, models in settings.tiers.items():
                if tier not in {str(number) for number in tiers}:
                    errors.append(f"workflow.{section}.tiers.{tier} is not a {role} tier; use "
                                  + ", ".join(str(number) for number in tiers))
                elif not isinstance(models, list):
                    errors.append(f"workflow.{section}.tiers.{tier} must be a list of candidates")
        for path, spec in self.candidates():
            provider = spec.get("provider")
            if provider not in AGENT_PROVIDERS:
                errors.append(f"{path}: provider must be codex or claude")
                continue
            if not isinstance(spec.get("model"), str) or not spec["model"].strip():
                errors.append(f"{path}: model must be a nonempty string")
            if spec.get("api_provider", "") != "" and not valid_name(spec.get("api_provider")):
                errors.append(f"{path}: invalid API provider name")
            efforts = API_EFFORTS if spec.get('api_provider') else AGENT_EFFORTS
            if spec.get("effort") not in efforts.get(provider, ()):
                errors.append(f"{path}: invalid effort")
            error = configuration_error(provider, spec)
            if error:
                errors.append(f"{path}: {error}")
        return errors

    def candidates(self):
        """Every configured candidate with its dotted path, including session pins."""
        groups = []
        for role in ROLE_TIERS:
            section = "review" if role == "reviewer" else role
            settings = self.role(role)
            groups.append((f"workflow.{section}.models", settings.models))
            if isinstance(settings.tiers, dict):
                groups.extend((f"workflow.{section}.tiers.{tier}", models) for tier, models in settings.tiers.items())
        groups.extend((f"--{role}", models) for role, models in self.pins.items())
        # Wrong containers are reported by validate() by themselves, not entry by entry.
        return [(f"{path}.{index}", spec) for path, models in groups if isinstance(models, list)
                for index, spec in enumerate(models)]

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        result = cls()
        result.enabled = _bool(data, "enabled", False)
        result.merge_mode = _str(data, "merge_mode") or result.merge_mode
        for key in ("auto_push", "cleanup_local_branches", "cleanup_remote_branches"):
            setattr(result, key, _bool(data, key, getattr(result, key)))
        result.timeout_seconds = _number(data, "timeout_seconds", result.timeout_seconds)
        result.scout = ScoutConfig.from_dict(data.get("scout"))
        result.review = ReviewConfig.from_dict(data.get("review"))
        result.worker = WorkerConfig.from_dict(data.get("worker"))
        return result


@dataclass
class Config:
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    git: GitConfig = field(default_factory=GitConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    external_providers: dict[str, ExternalProvider] = field(default_factory=dict)
    env: EnvConfig = field(default_factory=EnvConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    install: InstallConfig = field(default_factory=InstallConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    #: The agent a bare ``agent-box`` starts; an explicit ``claude``/``codex`` wins.
    #: Global only: which TUI to open is the user's habit, not a repository's policy.
    default_agent: str = "claude"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "default_agent": self.default_agent,
            "claude": self.claude.to_dict(),
            "codex": self.codex.to_dict(),
            "notifications": self.notifications.to_dict(),
            "github": self.github.to_dict(),
            "git": self.git.to_dict(),
            "providers": self.providers.to_dict(),
            "external_providers": {name: spec.to_dict() for name, spec in self.external_providers.items()},
            "env": self.env.to_dict(),
            "skills": self.skills.to_dict(),
            "install": self.install.to_dict(),
            "image": self.image.to_dict(),
            "tracker": self.tracker.to_dict(),
            "browser": self.browser.to_dict(),
            "audio": self.audio.to_dict(),
            "workflow": self.workflow.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Config":
        if not isinstance(data, dict):
            return cls()
        agent = _str(data, "default_agent")
        return cls(
            default_agent=agent if agent in LAUNCH_AGENTS else "claude",
            claude=ClaudeConfig.from_dict(data.get("claude")),
            codex=CodexConfig.from_dict(data.get("codex")),
            notifications=NotificationsConfig.from_dict(data.get("notifications")),
            github=GitHubConfig.from_dict(data.get("github")),
            git=GitConfig.from_dict(data.get("git")),
            providers=ProvidersConfig.from_dict(data.get("providers")),
            external_providers=parse_connections(data.get("external_providers")),
            env=EnvConfig.from_dict(data.get("env")),
            skills=SkillsConfig.from_dict(data.get("skills")),
            install=InstallConfig.from_dict(data.get("install")),
            image=ImageConfig.from_dict(data.get("image")),
            tracker=TrackerConfig.from_dict(data.get("tracker")),
            browser=BrowserConfig.from_dict(data.get("browser")),
            audio=AudioConfig.from_dict(data.get("audio")),
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
    """Persist global choices; missing settings follow the program defaults."""
    def overrides(values, defaults):
        result = {}
        for key, value in values.items():
            if key in defaults and value == defaults[key]:
                continue
            result[key] = (overrides(value, defaults[key])
                           if isinstance(value, dict) and isinstance(defaults.get(key), dict) else value)
        return result

    data = overrides(config.to_dict(), Config().to_dict())
    atomic_write_json(CONFIG_FILE if path is None else path, {"version": VERSION, **data}, SECRET_MODE)


def session_account(config):
    """The tracker account a session connects with: the global one, or --tracker's.

    The project configuration can name a URL but never a token, so the account
    comes from the configuration beneath the project overlay.
    """
    return getattr(config, "_project_base", config).tracker


def effective_config(base=None, *, include_project=True) -> Config:
    """Global (or snapshot) < project < this session's launch flags."""
    from . import launch_options, project_config
    runtime_file = os.environ.get("AGENT_BOX_RUNTIME_CONFIG")
    launch = {}
    if base is None:
        if not runtime_file:
            base = load_config()
        else:
            from .errors import AgentBoxError
            try:
                rendered = read_json(Path(runtime_file))
            except (OSError, ValueError):
                raise AgentBoxError("Cannot read rendered workflow configuration") from None
            # The snapshot is rendered once at launch; after a schema change in the
            # checkout it no longer loads or validates although nothing the user saved is wrong.
            try:
                base = Config.from_dict(rendered)
                # Only agent-box's own messages are shown: other exceptions may quote values.
                errors = base.workflow.validate()
            except AgentBoxError as exc:
                errors = [str(exc)]
            except (ValueError, TypeError, AttributeError):
                errors = ["the snapshot has an unexpected shape"]
            if errors:
                raise AgentBoxError(
                    "This session's configuration snapshot was rendered at launch for an older "
                    f"agent-box configuration schema ({'; '.join(errors)}). Restart the agent-box "
                    "session to render a current snapshot") from None
            if "global_tracker_urls" in rendered:
                base._global_tracker_urls = rendered["global_tracker_urls"]
            launch = rendered.get("launch") if isinstance(rendered.get("launch"), dict) else {}
    import copy
    base = copy.deepcopy(base)
    # The editor needs the original URLs, but a session must not retain an
    # unused global token just to remember where those URLs came from.
    source = getattr(base, "_project_base", base)
    base._global_tracker_urls = getattr(source, "_global_tracker_urls", {
        name: connection.url for name, connection in source.tracker.connections.items()})
    if not include_project:
        # A nested launch starts from this session's flags and adds its own.
        base._launch = launch
        return base
    result = project_config.apply(base, project_config.load())
    return launch_options.apply(result, launch) if launch else result


def effective_config_source() -> dict[str, str]:
    """Identify the source without dumping the credentials it may contain."""
    runtime_file = os.environ.get("AGENT_BOX_RUNTIME_CONFIG")
    if runtime_file:
        return {"kind": "runtime", "path": str(Path(runtime_file))}
    return {"kind": "host" if CONFIG_FILE.is_file() else "defaults", "path": str(CONFIG_FILE)}


def session_snapshot_only() -> bool:
    """Whether the only configuration readable here is a rendered session snapshot.

    A workspace box does not mount the saved file; the snapshot carries only
    the sections that cross into a box (tracker, workflow), so reading the
    missing path as "no integrations" is how the 2026-09-13 wipe happened.
    """
    return bool(os.environ.get("AGENT_BOX_RUNTIME_CONFIG")) and not CONFIG_FILE.is_file()
