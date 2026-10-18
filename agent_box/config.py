"""The single agent-box configuration file.

One small, versioned JSON document under the normal XDG config location
(``~/.config/agent-box/config.json`` by default, mode 0600 because it may hold
a Telegram bot token). ``agent-box config`` is the interactive front end; this
module is only the schema, load and save.

Design rules this file exists to enforce:

* **Every host integration that exposes credentials is opt-in.** Notably a
  GitHub token is never taken from the host just because ``gh`` happens to be
  authenticated -- ``credentials.github.token`` must be turned on deliberately. Same for
  copying the host's ``~/.config/gh`` (which contains that token on disk).
* **Defaults are the safe answer.** A fresh install runs the container with no
  host credentials beyond the agent's own Claude/Codex config directories.
* **No framework.** Flat dataclasses, one version integer, explicit
  ``to_dict``/``from_dict``. Unknown keys are dropped on save rather than
  round-tripped, so the file always matches the shipped schema.

The workflow and tracker sections are opt-in. Provider connections hold
credentials globally; project_config validates the explicit repository subset
for launch and workflow policy alongside tracker identity. Stored schema versions
are independent of package SemVer; migrations run before parsing known fields.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .atomicio import read_json
from . import migrations, networking
from .paths import CONFIG_FILE
from .external_config import API_EFFORTS, ExternalProvider, parse_connections, valid_name

VERSION = migrations.GLOBAL_CONFIG.version
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

    Usage switches and the command also feed ``agent-box usage all``.
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
    #: Usage collection is opt-in because provider reads use account credentials
    #: and the custom command may contact an external API.
    claude_usage: bool = False
    codex_usage: bool = False
    custom_usage: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "command": self.command,
                "claude_usage": self.claude_usage, "codex_usage": self.codex_usage,
                "custom_usage": self.custom_usage}

    @classmethod
    def from_dict(cls, data: Any) -> "StatuslineConfig":
        return cls(enabled=_bool(data, "enabled", True), command=_str(data, "command"),
                   claude_usage=_bool(data, "claude_usage", False),
                   codex_usage=_bool(data, "codex_usage", False),
                   custom_usage=_bool(data, "custom_usage", False))


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
class NativeAgentWaitConfig:
    """Cache-aware cadence for root coordinators waiting on native agents."""

    enabled: bool = True
    interval_seconds: int = 28 * 60

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "interval_seconds": self.interval_seconds}

    @classmethod
    def from_dict(cls, data: Any) -> "NativeAgentWaitConfig":
        result = cls()
        result.enabled = _bool(data, "enabled", result.enabled)
        result.interval_seconds = _number(data, "interval_seconds", result.interval_seconds)
        return result

    def validate(self) -> list[str]:
        value = self.interval_seconds
        if not isinstance(value, int) or not 10 <= value <= 3600:
            return ["agents.codex.native_agent_wait.interval_seconds must be between 10 and 3600"]
        return []


@dataclass
class CodexConfig:
    #: Remote access is opt-in; a session never switches the shared daemon.
    remote_control: bool = False
    native_agent_wait: NativeAgentWaitConfig = field(default_factory=NativeAgentWaitConfig)

    def to_dict(self) -> dict[str, Any]:
        return {"remote_control": self.remote_control,
                "native_agent_wait": self.native_agent_wait.to_dict()}

    @classmethod
    def from_dict(cls, data: Any) -> "CodexConfig":
        return cls(
            remote_control=_bool(data, "remote_control", False),
            native_agent_wait=NativeAgentWaitConfig.from_dict(_mapping(data).get("native_agent_wait")),
        )

    def validate(self) -> list[str]:
        return self.native_agent_wait.validate()


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

    ``token`` runs ``gh auth token`` on the host and forwards the result as
    ``GH_TOKEN``; ``config`` copies ``~/.config/gh`` (which stores that same
    token) into the container. An earlier agent-box did the first
    unconditionally whenever ``gh`` was authenticated, which handed every
    container a live GitHub credential nobody asked for.
    """

    token: bool = False
    config: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"token": self.token, "config": self.config}

    @classmethod
    def from_dict(cls, data: Any) -> "GitHubConfig":
        return cls(token=_bool(data, "token", False), config=_bool(data, "config", False))


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


def _names(data, key, *, exact=False):
    """Distinct non-empty strings in their saved order: hosts trimmed and without whitespace, or ``exact``."""
    value = _mapping(data).get(key)
    names = [item if exact else item.strip() for item in value if isinstance(item, str)] if isinstance(value, list) else []
    return list(dict.fromkeys(name for name in names if name and (exact or not any(c.isspace() for c in name))))


@dataclass
class RegistriesConfig:
    """Container registry and Helm repository logins forwarded from the host.

    Off and empty by default: a forwarded login reaches every private image
    and chart behind it. The lists hold what the user ticked among the logins
    ``agent-box config`` found on the host; ``enabled`` is kept apart from them
    so ``--[no-]registries`` can turn forwarding on or off for one session
    without losing the choice. Entries are copied from the files the host's
    tools already wrote, never obtained by running a login command on the
    host: agent-box does not run the user's credential tooling.
    """

    enabled: bool = False
    #: Registry hosts; each one filters the podman and Helm OCI auth files.
    hosts: list[str] = field(default_factory=list)
    #: Helm repository names from ``repositories.yaml``.
    helm_repositories: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.hosts or self.helm_repositories)

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "hosts": list(self.hosts), "helm_repositories": list(self.helm_repositories)}

    @classmethod
    def from_dict(cls, data: Any) -> "RegistriesConfig":
        return cls(enabled=_bool(data, "enabled", False), hosts=_names(data, "hosts"),
                   # A Helm repository name is matched exactly, as Helm writes it; a host has no spaces.
                   helm_repositories=_names(data, "helm_repositories", exact=True))


@dataclass
class KubernetesConfig:
    """Kubeconfig files from the host's ``~/.kube`` forwarded into the box.

    Off and empty by default: a kubeconfig usually reaches a real cluster,
    often a production one. ``files`` holds what the user ticked among the
    kubeconfigs ``agent-box config`` found; ``enabled`` is kept apart so
    ``--[no-]kubeconfigs`` can switch forwarding for one session without
    losing the choice. Whole files are copied unchanged and never merged:
    the same context and user names recur across files, and a merged
    ``KUBECONFIG`` would silently resolve one to the wrong cluster.
    """

    enabled: bool = False
    #: File names in ``~/.kube``.
    files: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.files)

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "files": list(self.files)}

    @classmethod
    def from_dict(cls, data: Any) -> "KubernetesConfig":
        # A name that leaves ~/.kube could copy any file the user can read into the box.
        return cls(enabled=_bool(data, "enabled", False),
                   files=[name for name in _names(data, "files", exact=True) if "/" not in name and name not in (".", "..")])


def _profile_names(data, key):
    # A name is also a file name under credentials/; one that leaves it could copy any file.
    return [name for name in _names(data, key, exact=True) if "/" not in name and name not in (".", "..")]


@dataclass
class CloudConfig:
    """Cloud CLI profiles forwarded from the host, one list per provider.

    Off and empty by default: unlike a kubeconfig, which reaches one
    cluster, a profile carries everything its account may do in the cloud,
    and a federated one a refresh token valid for a month. ``enabled`` is
    kept apart from the choices so ``--[no-]cloud-profiles`` can switch all
    providers for one session without losing them; a new provider adds a
    list here, not a flag.
    """

    enabled: bool = False
    #: Profile names in the host's yc ``config.yaml``.
    yc: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.yc)

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "yc": {"profiles": list(self.yc)}}

    @classmethod
    def from_dict(cls, data: Any) -> "CloudConfig":
        return cls(enabled=_bool(data, "enabled", False), yc=_profile_names(_mapping(data).get("yc"), "profiles"))


@dataclass
class HostEnvConfig:
    """Agent provider variables forwarded from the launching shell, split by sensitivity.

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
    def from_dict(cls, data: Any) -> "HostEnvConfig":
        return cls(api_keys=_bool(data, "api_keys", False), transport=_bool(data, "transport", True))


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
TOOL_GROUPS: dict[str, bool] = {
    # shellcheck, sqlite3, jscpd -- what an agent reaches for when reading code
    "analysis": True,
    # kubectl, Helm, yq
    "kubernetes": True,
    # QEMU x86-64/aarch64, OVMF; also gates the /dev/kvm passthrough
    "virtualization": False,
    # Nested rootless Podman; gates ID capabilities, TUN and namespace setup
    "podman": False,
    # skopeo alone: registry work needs no namespace permissions, unlike Podman
    "skopeo": False,
    # Playwright MCP + headless Chromium, wired into every box session
    "browser": False,
    # Yandex Cloud CLI; what forwarded yc profiles and exec kubeconfigs run
    "yc": False,
}


#: What each group is, in the words `agent-box config` asks the question in.
TOOL_GROUP_HELP: dict[str, str] = {
    "analysis": "Code analysis (shellcheck, sqlite3, jscpd)",
    "kubernetes": "Kubernetes (kubectl, Helm, yq)",
    "virtualization": "Virtualization (QEMU x86-64/arm64, KVM passthrough; large)",
    "podman": "Nested Podman (rootless builds/containers; extra namespace permissions)",
    "skopeo": "skopeo (inspect, copy and list tags in container registries without Podman)",
    "browser": ("Agent browser (Playwright MCP + headless Chromium; large). Isolated in-memory "
                "profile, same trust boundary as agents; unavailable in --host mode"),
    "yc": "Yandex Cloud CLI (yc)",
}


K3S_LIFETIMES = ("persistent", "box")
#: A ``rancher/k3s`` release tag; the image spells k3s's ``+`` as ``-``.
K3S_TAG_PATTERN = re.compile(r"v\d+\.\d+\.\d+(-rc\d+)?-k3s\d+")
K3S_READY_TIMEOUT = (30, 1800)


def _k3s_url_problem(url: Any) -> str:
    from urllib.parse import urlsplit
    if not isinstance(url, str):
        return "must be a string"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 -- raises on a port that is not a number in range
    except ValueError:
        return "is not a URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "must be an http(s) URL"
    if parts.username is not None or parts.password is not None:
        return "must not carry credentials"
    return ""


@dataclass
class K3sConfig:
    """A per-workspace k3s cluster in a sidecar next to the workspace box.

    Not an image group: the cluster runs in its own ``rancher/k3s`` container
    in the host's rootless Podman, so the box itself gains no privilege. The
    alternatives -- k3s inside the box, or nested Podman with delegated
    cgroups -- both widen what the box where the agent lives may do.

    Off by default: the agent gets cluster-admin in a cluster whose privileged
    pods are root in the sidecar's user namespace. That reaches only the
    sidecar's own filesystem and volume, never the host's files, but it is a
    wider kernel surface than an unprivileged box.

    ``lifetime`` ``persistent`` keeps the cluster until ``agent-box stop`` so
    deployments survive between sessions; ``box`` stops it with the box.
    ``persistent_data`` false deletes the cluster's volume whenever the sidecar
    stops. An empty ``version`` is the release agent-box pins by digest.

    Everything else shapes only the sidecar, which is why a project may set
    it too: ``ports`` publish on host loopback and never on the LAN, the
    ``manifests`` directory stays inside the project and is mounted read-only,
    and registry mirrors carry no credentials. There is no CNI choice: Cilium
    needs BPF privileges no rootless container can hold.
    """

    enabled: bool = False
    lifetime: str = "persistent"
    persistent_data: bool = True
    traefik: bool = False
    metrics_server: bool = False
    servicelb: bool = True
    local_storage: bool = True
    network_policy: bool = True
    version: str = ""
    cpus: str = ""
    memory: str = ""
    ports: list = field(default_factory=list)
    manifests: str = ""
    registry_mirrors: dict = field(default_factory=dict)
    extra_args: list = field(default_factory=list)
    ready_timeout: int = 180

    def to_dict(self) -> dict[str, Any]:
        import copy
        return {"enabled": self.enabled, "lifetime": self.lifetime,
                "persistent_data": self.persistent_data, "traefik": self.traefik,
                "metrics_server": self.metrics_server, "servicelb": self.servicelb,
                "local_storage": self.local_storage, "network_policy": self.network_policy,
                "version": self.version, "cpus": self.cpus, "memory": self.memory,
                "ports": list(self.ports), "manifests": self.manifests,
                "registry_mirrors": copy.deepcopy(self.registry_mirrors),
                "extra_args": list(self.extra_args), "ready_timeout": self.ready_timeout}

    @classmethod
    def from_dict(cls, data: Any) -> "K3sConfig":
        default = cls()
        return cls(
            enabled=_bool(data, "enabled", default.enabled),
            lifetime=_str(data, "lifetime") if "lifetime" in _mapping(data) else default.lifetime,
            persistent_data=_bool(data, "persistent_data", default.persistent_data),
            traefik=_bool(data, "traefik", default.traefik),
            metrics_server=_bool(data, "metrics_server", default.metrics_server),
            servicelb=_bool(data, "servicelb", default.servicelb),
            local_storage=_bool(data, "local_storage", default.local_storage),
            network_policy=_bool(data, "network_policy", default.network_policy),
            version=_str(data, "version"),
            cpus=_str(data, "cpus"),
            memory=_str(data, "memory"),
            manifests=_str(data, "manifests"),
            # Kept as found, so validate() can name a malformed entry instead
            # of the loader silently dropping what the user wrote.
            ports=_copied(data, "ports", list),
            registry_mirrors=_copied(data, "registry_mirrors", dict),
            extra_args=_copied(data, "extra_args", list),
            ready_timeout=_number(data, "ready_timeout", default.ready_timeout),
        )

    def validate(self) -> list[str]:
        errors = []
        if self.lifetime not in K3S_LIFETIMES:
            errors.append(f"box.k3s.lifetime must be one of {', '.join(K3S_LIFETIMES)}")
        if self.version and not K3S_TAG_PATTERN.fullmatch(self.version):
            errors.append("box.k3s.version must be empty or a rancher/k3s tag such as v1.33.5-k3s1")
        if self.cpus and not (re.fullmatch(r"\d+(\.\d+)?", self.cpus) and float(self.cpus) > 0):
            errors.append("box.k3s.cpus must be empty or a positive number such as 2 or 1.5")
        if self.memory and not (re.fullmatch(r"\d+[bkmg]?", self.memory, re.I) and int(re.sub(r"\D", "", self.memory))):
            errors.append("box.k3s.memory must be empty or a size such as 4g or 512m")
        try:
            networking.ports(self.ports)
        except ValueError as exc:
            errors.append(f"box.k3s.ports: {exc}")
        manifests = PurePosixPath(self.manifests)
        if self.manifests and (manifests.is_absolute() or ".." in manifests.parts or "\\" in self.manifests):
            errors.append("box.k3s.manifests must be a path inside the project, without '..'")
        mirrors = self.registry_mirrors
        if not isinstance(mirrors, dict) or any(
                not isinstance(registry, str) or not re.fullmatch(r"[A-Za-z0-9.:*-]+", registry)
                or not isinstance(endpoints, list) or not endpoints
                or any(_k3s_url_problem(url) for url in endpoints)
                for registry, endpoints in mirrors.items()):
            errors.append("box.k3s.registry_mirrors must map registry names to nonempty lists of "
                          "http(s) mirror URLs without credentials")
        if not isinstance(self.extra_args, list) or any(
                not isinstance(arg, str) or not arg.startswith("--") or "\n" in arg for arg in self.extra_args):
            errors.append("box.k3s.extra_args must be a list of k3s server flags, each starting with --")
        low, high = K3S_READY_TIMEOUT
        if not isinstance(self.ready_timeout, int) or not low <= self.ready_timeout <= high:
            errors.append(f"box.k3s.ready_timeout must be between {low} and {high} seconds")
        return errors


@dataclass
class PodmanConfig:
    """How long nested Podman's storage outlives the box; the tool group itself is ``tools.podman``.

    Images, build cache, containers and volumes of nested Podman live in the
    outer volume ``agent-box-podman-<workspace id>``. ``persistent_data``
    false deletes it once the workspace's box is gone, so a scratch project
    leaves nothing behind; true keeps pulls and build cache between boxes
    until ``agent-box stop --podman-data``.

    Off by default because kept data piles up unseen: one host had gathered
    7.5 GB in 19 such volumes, several for directories that no longer existed.
    """

    persistent_data: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"persistent_data": self.persistent_data}

    @classmethod
    def from_dict(cls, data: Any) -> "PodmanConfig":
        return cls(persistent_data=_bool(data, "persistent_data", cls().persistent_data))


@dataclass
class ToolsConfig:
    """Which optional tool groups the workspace image is built with.

    A property of the image rather than of a launch: changing a group means
    rebuilding. ``browser`` also wires the Playwright MCP server into every
    Claude and Codex session in the box. A separate switch for that server was
    rejected: with Chromium in the box an agent can drive it through Playwright
    from a shell anyway, so the switch isolated nothing.

    The browser exposes no host state -- no host profile, cookie jar or
    credential. Pages the agent opens land *inside the workspace box*, one
    trust boundary with the agent, the mounted project and every other enabled
    integration. Each MCP process gets an isolated, non-persistent profile and
    a session-private output directory (``agent_box.runtime.browser_session``),
    so there is nothing further for a user to configure.

    Unknown group names are dropped on load rather than carried, so the file
    can never ask the build for a group that no longer exists.

    The k3s cluster is a sibling section of the box, not a group: it builds
    nothing into the image, so it never reaches the build args, the image
    label or the drift report.
    """

    groups: dict[str, bool] = field(default_factory=lambda: dict(TOOL_GROUPS))
    #: ``--no-browser`` for one session: the image keeps Chromium, the session
    #: gets no MCP server. Never saved; the launch snapshot re-applies it.
    browser_off: bool = False

    def enabled_groups(self) -> list[str]:
        """The groups to build, in a stable order for the image label."""
        return sorted(name for name, on in self.groups.items() if on)

    def browser(self) -> bool:
        """Whether this session wires the browser MCP server."""
        return self.groups["browser"] and not self.browser_off

    def to_dict(self) -> dict[str, Any]:
        return {name: bool(self.groups.get(name, default)) for name, default in TOOL_GROUPS.items()}

    @classmethod
    def from_dict(cls, data: Any) -> "ToolsConfig":
        return cls(groups={name: _bool(data, name, default) for name, default in TOOL_GROUPS.items()})


TRACKER_PROVIDERS = ("vikunja", "jira", "github")
#: Providers with an adapter; the rest are reserved names that tracker commands refuse.
IMPLEMENTED_TRACKERS = ("vikunja", "jira", "github")
AGENT_PROVIDERS = ("codex", "claude")
AGENT_EFFORTS = {
    "codex": ("default", "low", "medium", "high", "xhigh", "max", "ultra"),
    "claude": API_EFFORTS['claude'],
}
WORKFLOW_SKILLS = ("ab-setup", "ab-adopt", "ab-normalize", "ab-interview", "ab-plan", "ab-work", "ab-sync", "ab-review", "ab-pr", "ab-land")
REVIEW_PRESETS = {"thorough": (2, 80), "balanced": (5, 200), "light": (10, 500)}
# Off, or the highest finding severity a NEEDS_WORK round may carry for its delta to count.
REVIEW_INCREMENTAL = ("off", "LOW", "MEDIUM", "HIGH", "CRITICAL")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _copied(data, key, kind):
    """A list or object setting as found; absent means the empty default."""
    import copy
    value = _mapping(data).get(key, kind())
    return copy.deepcopy(value)


def _number(data, key, default):
    value = _mapping(data).get(key, default)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


@dataclass
class TrackerConnection:
    url: str = ""
    token: str = ""
    #: Jira Cloud uses email/API-token Basic auth; empty selects Server/DC PAT.
    username: str = ""

    @property
    def usable(self):
        return bool(self.url and self.token)

    def to_dict(self):
        return {"url": self.url, "token": self.token, **({"username": self.username} if self.username else {})}

    @classmethod
    def from_dict(cls, data):
        return cls(url=_str(data, "url"), token=_str(data, "token"), username=_str(data, "username"))


@dataclass
class TrackerConfig:
    """Disabled until the user explicitly exposes configured tracker accounts."""
    enabled: bool = False
    provider: str = ""
    connections: dict[str, TrackerConnection] = field(default_factory=dict)

    def connection(self, provider=None):
        """GitHub's API address is known, so only Enterprise Server needs one saved."""
        provider = provider or self.provider
        connection = self.connections.get(provider, TrackerConnection())
        if provider == "github" and not connection.url:
            from .tracker.github_api import DEFAULT_API
            connection = TrackerConnection(DEFAULT_API, connection.token)
        return connection

    def to_dict(self):
        return {"enabled": self.enabled, "provider": self.provider,
                "connections": {k: v.to_dict() for k, v in self.connections.items()}}

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        return cls(_bool(data, "enabled", False), _str(data, "provider"),
                   {k: TrackerConnection.from_dict(v) for k, v in
                    _mapping(data.get("connections")).items() if k in TRACKER_PROVIDERS})


MODEL_ENTRY = {"provider": str, "model": str, "effort": str, "speed": str, "api_provider": str}
#: Task tiers select worker, sync and reviewer lists; scouts have none.
TIERS = range(1, 4)
TIERED_ROLES = ("worker", "reviewer", "sync")


def model_entry(data):
    """One candidate: the CLI that runs it, and the model it runs.

    The provider is stored rather than derived from the model name, because an
    external API model (DeepSeek through Claude's CLI) names no provider.
    """
    data = _mapping(data)
    # Kept as written, not coerced: a wrong type must reach validate() at its path.
    return {"provider": data.get("provider", ""), "model": data.get("model", ""),
            "effort": data.get("effort", "medium"), "speed": data.get("speed", "inherit"),
            "api_provider": data.get("api_provider", "")}


def vendor(spec):
    """Whose model a candidate runs: its external API by name, else its CLI's own subscription.

    An aggregator is one vendor whatever its model prefixes say: a name
    convention would have to be guessed per provider, and the configured API
    name is what the user chose.
    """
    return spec.get("api_provider") or spec.get("provider")


def candidate_label(spec):
    """Candidate identity and explicit speed; CLI pins accept the identity alone."""
    api = f"@{spec['api_provider']}" if spec.get("api_provider") else ""
    return f"{spec['provider']}{api}:{spec['model']}:{spec['effort']}" + f" speed={spec.get('speed', 'inherit')}"


def model_list(data=None):
    """Candidates in preference order; a role runs the first one available.

    A malformed entry becomes an empty candidate that validation rejects at its
    index; dropping it would silently shorten the list, or empty it and let
    untiered work inherit the session instead.
    """
    if data is None:
        return []
    return [model_entry(item) for item in data] if isinstance(data, list) else data


def model_tiers(data=None):
    """Known tiers as lists; a wrong container or unknown tier stays for validate() to name."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return data
    known = {str(number) for number in TIERS}
    return {**{tier: model_list(data.get(tier)) for tier in sorted(known)},
            **{tier: value for tier, value in data.items() if tier not in known}}


@dataclass
class WorkerConfig:
    models: list[dict] = field(default_factory=list)
    tiers: dict[str, list] = field(default_factory=model_tiers)

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        return cls(model_list(data.get("models")), model_tiers(data.get("tiers")))


@dataclass
class ScoutConfig:
    limit: int = 3
    models: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        return cls(_number(data, "limit", 3), model_list(data.get("models")))


@dataclass
class ReviewConfig:
    policy: str = "auto"
    #: Off by default: the list order already states the preference, and on it
    #: every reviewer selection needs the coordinator to name the code's authors.
    other_vendor_first: bool = False
    models: list[dict] = field(default_factory=list)
    tiers: dict[str, list] = field(default_factory=model_tiers)
    max_files: int = 5
    max_lines: int = 200
    max_rounds: int = 3
    sensitive_paths: list[str] = field(default_factory=lambda: [
        "**/auth/**", "**/security/**", "**/migrations/**", "**/skills/**",
        "**/config*", ".github/**", "**/*schema*", "**/api/**", "**/AGENTS.md"])
    ignored_paths: list[str] = field(default_factory=list)
    incremental: str = "off"
    label: str = "ab:review"
    github_provider: str = "auto"
    github_model: str = ""
    github_effort: str = ""
    wait_seconds: int = 3600

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        result = cls()
        result.models = model_list(data.get("models"))
        result.tiers = model_tiers(data.get("tiers"))
        result.other_vendor_first = _bool(data, "other_vendor_first", False)
        for key in ("policy", "incremental", "label", "github_provider", "github_model", "github_effort"):
            if key in data and isinstance(data[key], str):
                setattr(result, key, data[key].strip())
        for key in ("max_files", "max_lines", "max_rounds", "wait_seconds"):
            setattr(result, key, _number(data, key, getattr(result, key)))
        for key in ("sensitive_paths", "ignored_paths"):
            if isinstance(data.get(key), list):
                setattr(result, key, [s for s in data[key] if isinstance(s, str)])
        return result


@dataclass
class PlanSyncConfig:
    # Tracker-writing delegation is opt-in independently of code review.
    policy: str = "off"
    events: list[str] = field(default_factory=lambda: ["plan", "after"])
    scope: str = "project"
    references: bool = True
    paths: list[str] = field(default_factory=lambda: ["docs/**", "*.md"])
    models: list[dict] = field(default_factory=list)
    tiers: dict[str, list] = field(default_factory=model_tiers)

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        result = cls()
        for key in ("policy", "scope"):
            if key in data:
                setattr(result, key, data[key])
        for key in ("events", "paths"):
            if key in data:
                setattr(result, key, data[key])
        result.references = _bool(data, "references", True)
        result.models = model_list(data.get("models"))
        result.tiers = model_tiers(data.get("tiers"))
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
    #: Off by default: each role runs its own list regardless of the task tier.
    #: The tiers stay configured and labelled.
    use_tiers: bool = False
    scout: ScoutConfig = field(default_factory=ScoutConfig)
    auto_push: bool = True
    #: Checkpoints can publish unfinished code in either delivery mode; explicit opt-in only.
    pause_push: bool = False
    timeout_seconds: int = 3600
    review: ReviewConfig = field(default_factory=ReviewConfig)
    sync: PlanSyncConfig = field(default_factory=PlanSyncConfig)
    cleanup_local_branches: bool = False
    cleanup_remote_branches: bool = False
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    #: The working language of project text in every area at once (tracker,
    #: GitHub, commits, repository text); empty keeps each text's own rule.
    #: Per-area switches were rejected as configuration nobody asked to vary.
    #: It applies with the tracker alone too, since ab-tracker reads it.
    language: str = ""
    #: Launch-only candidate lists pinned by --worker/--reviewer/--scout/--sync, keyed by role.
    #: Never serialized: a session re-derives them from its snapshot's launch
    #: flags, so no configuration file can hold one.
    pins: dict[str, list] = field(default_factory=dict)

    def to_dict(self):
        from dataclasses import asdict
        data = asdict(self)
        data.pop("pins")
        return data

    def role(self, role):
        return {"scout": self.scout, "worker": self.worker, "reviewer": self.review, "sync": self.sync}[role]

    def effective_tier(self, tier):
        """The task tier that selects models, or None while tiers are switched off."""
        return tier if self.use_tiers else None

    def models_for(self, role, tier, authors=()):
        """``(path, candidates)``: a session pin, else a nonempty tier list, else the role's own list.

        ``tier`` is already effective; scouts have no tier lists. ``authors``
        are the vendors that wrote the reviewed code: with ``other_vendor_first``
        a reviewer list puts every other vendor ahead of them, keeping the list
        order within both groups, so the authors' own models stay the last
        fallbacks rather than being dropped. A pin names its model outright and
        is never reordered.

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
        if role in TIERED_ROLES and tier is not None and isinstance(settings.tiers, dict) and settings.tiers.get(str(tier)):
            path, models = f"workflow.{section}.tiers.{tier}", settings.tiers[str(tier)]
        else:
            path, models = f"workflow.{section}.models", settings.models
        if role == "reviewer" and authors and self.review.other_vendor_first and isinstance(models, list):
            models = sorted(models, key=lambda spec: vendor(spec) in authors)
        return path, models

    def validate(self):
        import math
        errors = []
        if self.merge_mode not in ("github", "local"):
            errors.append("workflow.merge_mode must be github or local")
        errors += language_problems(self.language)
        value = self.timeout_seconds
        if not isinstance(value, int) or not math.isfinite(value) or not 30 <= value <= 86400:
            errors.append("workflow.timeout_seconds must be between 30 and 86400")
        if not isinstance(self.scout.limit, int) or not 1 <= self.scout.limit <= 16:
            errors.append("workflow.scout.limit must be between 1 and 16")
        if self.sync.policy not in ("off", "auto", "always"):
            errors.append("workflow.sync.policy must be off, auto, or always")
        if self.sync.scope not in ("parent", "project"):
            errors.append("workflow.sync.scope must be parent or project")
        if (not isinstance(self.sync.events, list) or not self.sync.events
                or any(event not in ("before", "after", "plan") for event in self.sync.events)):
            errors.append("workflow.sync.events must list before, after, or plan")
        if not isinstance(self.sync.paths, list) or any(not isinstance(path, str) or not path for path in self.sync.paths):
            errors.append("workflow.sync.paths must be a list of nonempty glob patterns")
        if self.review.policy not in ("off", "auto", "always"):
            errors.append("workflow.review.policy must be off, auto, or always")
        if self.review.incremental not in REVIEW_INCREMENTAL:
            errors.append("workflow.review.incremental must be off, LOW, MEDIUM, HIGH, or CRITICAL")
        if self.review.github_provider not in ("auto", "local", "codex", "claude"):
            errors.append("workflow.review.github_provider must be auto, local, codex, or claude")
        for name in ("max_files", "max_lines", "max_rounds", "wait_seconds"):
            value = getattr(self.review, name)
            if not isinstance(value, int) or value < 1:
                errors.append(f"workflow.review.{name} must be a positive integer")
        from .speed import configuration_error
        for role in ("scout", *TIERED_ROLES):
            section = "review" if role == "reviewer" else role
            settings = self.role(role)
            if not isinstance(settings.models, list):
                errors.append(f"workflow.{section}.models must be a list of candidates")
            if role not in TIERED_ROLES:
                continue
            if not isinstance(settings.tiers, dict):
                errors.append(f"workflow.{section}.tiers must be an object keyed by tier")
                continue
            for tier, models in settings.tiers.items():
                if tier not in {str(number) for number in TIERS}:
                    errors.append(f"workflow.{section}.tiers.{tier} is not a task tier; use 1, 2, 3")
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
        for role in ("scout", *TIERED_ROLES):
            section = "review" if role == "reviewer" else role
            settings = self.role(role)
            groups.append((f"workflow.{section}.models", settings.models))
            if role in TIERED_ROLES and isinstance(settings.tiers, dict):
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
        for key in ("use_tiers", "auto_push", "pause_push", "cleanup_local_branches", "cleanup_remote_branches"):
            setattr(result, key, _bool(data, key, getattr(result, key)))
        result.timeout_seconds = _number(data, "timeout_seconds", result.timeout_seconds)
        result.scout = ScoutConfig.from_dict(data.get("scout"))
        result.review = ReviewConfig.from_dict(data.get("review"))
        result.sync = PlanSyncConfig.from_dict(data.get("sync"))
        result.worker = WorkerConfig.from_dict(data.get("worker"))
        # Only spaces are trimmed: a line break must reach validate(), not vanish into an empty value.
        language = data.get("language")
        result.language = language.strip(" ") if isinstance(language, str) else ""
        return result


@dataclass
class AgentsConfig:
    """Which agent starts, how each CLI runs, and which APIs it may reach."""

    #: The agent a bare ``agent-box`` starts; an explicit ``claude``/``codex`` wins.
    #: Global only: which TUI to open is the user's habit, not a repository's policy.
    default: str = "claude"
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    host_env: HostEnvConfig = field(default_factory=HostEnvConfig)
    external_apis: dict[str, ExternalProvider] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "default": self.default,
            "claude": self.claude.to_dict(),
            "codex": self.codex.to_dict(),
            "host_env": self.host_env.to_dict(),
            "external_apis": {name: spec.to_dict() for name, spec in self.external_apis.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AgentsConfig":
        data = _mapping(data)
        agent = _str(data, "default")
        return cls(
            default=agent if agent in LAUNCH_AGENTS else "claude",
            claude=ClaudeConfig.from_dict(data.get("claude")),
            codex=CodexConfig.from_dict(data.get("codex")),
            host_env=HostEnvConfig.from_dict(data.get("host_env")),
            external_apis=parse_connections(data.get("external_apis")),
        )


@dataclass
class LinksConfig:
    """Host access is opt-in; displaying links needs no host capability.

    The socket is part of the shared box, so toggling enabled recreates it.
    Browser opening is separately opt-in and never used on a remote host.
    """
    enabled: bool = False
    open_browser: bool = False
    ssh_destination: str = ""
    coder_workspace: str = ""

    def to_dict(self):
        return {"enabled": self.enabled, "open_browser": self.open_browser,
                "ssh_destination": self.ssh_destination, "coder_workspace": self.coder_workspace}

    @classmethod
    def from_dict(cls, data):
        return cls(_bool(data, "enabled", False), _bool(data, "open_browser", False),
                   _str(data, "ssh_destination"), _str(data, "coder_workspace"))

    def validate(self):
        errors = []
        for name in ("ssh_destination", "coder_workspace"):
            value = getattr(self, name)
            if value and (value.startswith("-") or any(ord(c) < 33 or ord(c) == 127 for c in value)):
                errors.append(f"box.links.{name} must be one destination without options or whitespace")
        return errors


@dataclass
class NetworkConfig:
    """Host access/publication is opt-in and belongs to the shared container."""
    mode: str = "default"
    ports: list = field(default_factory=list)
    host_ports: list = field(default_factory=list)

    def to_dict(self):
        return {"mode": self.mode, "ports": list(self.ports), "host_ports": list(self.host_ports)}

    @classmethod
    def from_dict(cls, data):
        data = _mapping(data)
        return cls((_str(data, "mode") if "mode" in data else "default"), _copied(data, "ports", list),
                   _copied(data, "host_ports", list))


@dataclass
class BoxConfig:
    """What the workspace box is built with and runs beside it."""

    tools: ToolsConfig = field(default_factory=ToolsConfig)
    k3s: K3sConfig = field(default_factory=K3sConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    podman: PodmanConfig = field(default_factory=PodmanConfig)
    #: The host's sound server inside workspace boxes.
    #:
    #: Off by default: turning it on lets everything in the box -- agents,
    #: their tools, the project's own code -- record from the host microphone
    #: and play through its speakers. The PulseAudio socket grants both
    #: directions and control of the server, so there is deliberately one
    #: switch rather than a microphone/speaker pair that the protocol could not
    #: actually separate.
    #:
    #: The socket, not ``/dev/snd``: raw ALSA devices would contend with the
    #: host's PipeWire or PulseAudio for exclusive access, while the socket
    #: works with either server. Structural, because a bind mount is part of
    #: the box.
    audio: bool = False
    links: LinksConfig = field(default_factory=LinksConfig)
    env: EnvConfig = field(default_factory=EnvConfig)

    def to_dict(self) -> dict[str, Any]:
        return {"tools": self.tools.to_dict(), "k3s": self.k3s.to_dict(), "network": self.network.to_dict(), "podman": self.podman.to_dict(),
                "audio": self.audio, "links": self.links.to_dict(), "env": self.env.to_dict()}

    @classmethod
    def from_dict(cls, data: Any) -> "BoxConfig":
        data = _mapping(data)
        return cls(tools=ToolsConfig.from_dict(data.get("tools")), k3s=K3sConfig.from_dict(data.get("k3s")),
                   network=NetworkConfig.from_dict(data.get("network")),
                   podman=PodmanConfig.from_dict(data.get("podman")), audio=_bool(data, "audio", False), links=LinksConfig.from_dict(data.get("links")),
                   env=EnvConfig.from_dict(data.get("env")))

    def validate(self) -> list[str]:
        return self.k3s.validate() + self.links.validate() + networking.problems(self.network, self.k3s)


@dataclass
class CredentialsConfig:
    """Host credentials and identity the box's own tools may use; all opt-in but Git identity."""

    git: GitConfig = field(default_factory=GitConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    registries: RegistriesConfig = field(default_factory=RegistriesConfig)
    kubernetes: KubernetesConfig = field(default_factory=KubernetesConfig)
    cloud: CloudConfig = field(default_factory=CloudConfig)

    def to_dict(self) -> dict[str, Any]:
        return {"git": self.git.to_dict(), "github": self.github.to_dict(), "registries": self.registries.to_dict(),
                "kubernetes": self.kubernetes.to_dict(), "cloud": self.cloud.to_dict()}

    def needed_tools(self) -> dict[str, str]:
        """Image tool groups the active forwarding needs, and what needs each.

        Forwarded configuration without its tool is a box that looks set up
        and is not; the menu ticks these groups when forwarding is turned on
        and launch warns when they are missing anyway.
        """
        needed = {}
        if self.kubernetes.active:
            needed["kubernetes"] = "Kubernetes configs"
        if self.cloud.active:
            needed["yc"] = "Cloud CLI profiles (Yandex Cloud)"
        return needed

    @classmethod
    def from_dict(cls, data: Any) -> "CredentialsConfig":
        data = _mapping(data)
        return cls(git=GitConfig.from_dict(data.get("git")), github=GitHubConfig.from_dict(data.get("github")),
                   registries=RegistriesConfig.from_dict(data.get("registries")),
                   kubernetes=KubernetesConfig.from_dict(data.get("kubernetes")),
                   cloud=CloudConfig.from_dict(data.get("cloud")))


@dataclass
class HostConfig:
    """What agent-box itself does on the host: notifications and skill links."""

    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    #: Link the shared skills into Claude's skill directory, so host Claude
    #: sessions see them as well.
    claude_skill_links: bool = True
    def to_dict(self) -> dict[str, Any]:
        return {"notifications": self.notifications.to_dict(),
                "claude_skill_links": self.claude_skill_links}

    @classmethod
    def from_dict(cls, data: Any) -> "HostConfig":
        data = _mapping(data)
        return cls(notifications=NotificationsConfig.from_dict(data.get("notifications")),
                   claude_skill_links=_bool(data, "claude_skill_links", True))


#: Long enough for any language name, short enough that a value cannot carry instructions into a prompt.
LANGUAGE_LIMIT = 40


def language_problems(language: str) -> list[str]:
    """Why a working language cannot be put into a generated prompt as is.

    The value is free text so any language can be named in its own words; a
    list of codes would need a catalogue agent-box has no other use for. One
    short line is the only shape that substitutes into a prompt without
    breaking it or adding a rule of its own.
    """
    import unicodedata
    # Zl and Zp are Unicode's line and paragraph separators, which a prompt reader also breaks on.
    if any(unicodedata.category(character)[0] == "C" or unicodedata.category(character) in ("Zl", "Zp")
           for character in language):
        return ["workflow.language must be one line without control characters"]
    if len(language) > LANGUAGE_LIMIT:
        return [f"workflow.language must be at most {LANGUAGE_LIMIT} characters, a language name such as English"]
    return []


@dataclass
class Config:
    """The whole file, one section per menu entry at the root."""

    agents: AgentsConfig = field(default_factory=AgentsConfig)
    box: BoxConfig = field(default_factory=BoxConfig)
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    host: HostConfig = field(default_factory=HostConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "agents": self.agents.to_dict(),
            "box": self.box.to_dict(),
            "credentials": self.credentials.to_dict(),
            "tracker": self.tracker.to_dict(),
            "workflow": self.workflow.to_dict(),
            "host": self.host.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Config":
        if not isinstance(data, dict):
            return cls()
        return cls(
            agents=AgentsConfig.from_dict(data.get("agents")),
            box=BoxConfig.from_dict(data.get("box")),
            credentials=CredentialsConfig.from_dict(data.get("credentials")),
            tracker=TrackerConfig.from_dict(data.get("tracker")),
            workflow=WorkflowConfig.from_dict(data.get("workflow")),
            host=HostConfig.from_dict(data.get("host")),
        )


def load_config(path: Path | None = None) -> Config:
    """The saved configuration; only a missing file means all defaults.

    A file that exists but cannot be read stops here: read as defaults, it
    would let the menu save them over it and `config apply` unapply integrations.
    """
    from .errors import AgentBoxError
    path = CONFIG_FILE if path is None else path
    try:
        data = read_json(path)
    except FileNotFoundError:
        return Config()
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        raise AgentBoxError(f"Cannot read {path}: {reason}. Fix the file, or delete it "
                            "to start from defaults") from None
    if not isinstance(data, dict):
        raise AgentBoxError(f"{path} is not a JSON object. Fix the file, or delete it "
                            "to start from defaults")
    return Config.from_dict(migrations.GLOBAL_CONFIG.prepare(data))


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
    migrations.write(CONFIG_FILE if path is None else path, {"version": VERSION, **data},
                     migrations.GLOBAL_CONFIG, mode=SECRET_MODE)


def migrate_config(path: Path | None = None) -> bool:
    """Persist during explicit apply or the first launch of an updated package."""
    def validate(data):
        config = Config.from_dict(data)
        errors = config.workflow.validate() + config.box.validate()
        if errors:
            from .errors import AgentBoxError
            raise AgentBoxError(errors[0])
    return migrations.upgrade(CONFIG_FILE if path is None else path,
                              migrations.GLOBAL_CONFIG, validate=validate, mode=SECRET_MODE)


def session_account(config):
    """The tracker account a session connects with: the global one, or --tracker's.

    The project configuration can name a URL but never a token, so the account
    comes from the configuration beneath the project overlay.
    """
    return getattr(config, "_project_base", config).tracker


def apply_box_policy(config, policy):
    """A session's shared capabilities belong to its box, even after a project edit."""
    import copy
    policy = migrations.BOX_POLICY.prepare(policy)
    shared = Config.from_dict(policy)
    config.box.tools.groups = copy.deepcopy(shared.box.tools.groups)
    config.box.k3s = shared.box.k3s
    config.box.network = shared.box.network
    config.box.podman = shared.box.podman
    config.box.audio = shared.box.audio
    config.box.links = shared.box.links
    config.credentials.git = shared.credentials.git
    config.credentials.github.config = shared.credentials.github.config
    config.credentials.registries = shared.credentials.registries
    config.credentials.kubernetes = shared.credentials.kubernetes
    config.credentials.cloud = shared.credentials.cloud
    if not policy["workflow_mount"]:
        config.workflow.enabled = config.tracker.enabled = False
    config._box_policy = copy.deepcopy(policy)
    return config


def effective_config(base=None, *, include_project=True) -> Config:
    """Global (or snapshot) < project < this session's launch flags."""
    from . import launch_options, project_config
    runtime_file = os.environ.get("AGENT_BOX_RUNTIME_CONFIG")
    launch = {}
    box_policy = getattr(base, "_box_policy", None)
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
                if "network" in rendered.get("box", {}):
                    # A running namespace cannot follow later project edits.
                    base._network_policy = NetworkConfig.from_dict(rendered["box"]["network"])
                # Only agent-box's own messages are shown: other exceptions may quote values.
                errors = base.workflow.validate() + base.agents.codex.validate()
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
            box_policy = rendered.get("box_policy")
            if box_policy is not None:
                apply_box_policy(base, box_policy)
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
    if hasattr(base, "_network_policy"):
        result.box.network = copy.deepcopy(base._network_policy)
    if box_policy is not None:
        apply_box_policy(result, box_policy)
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
