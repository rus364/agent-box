"""Podman image/workspace lifecycle: the single authoritative place that
builds image, persistent workspace-container, and ``podman exec`` commands.

Every local command -- ``agent-box claude``/``codex``/``shell``/``doctor`` --
goes through :func:`container_lifetime_args`, so there is exactly one
definition of what a container can see. What that is depends on
``agent-box config``: host credentials are exposed only where the user turned
the corresponding switch on (see :mod:`agent_box.config`).
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import tomllib
import urllib.parse

from . import personal_browser
from . import channel as channel_module
from . import claude_settings
from . import notify as notify_module
from . import k3s, browser, host_k3s
from . import host_podman, nested_podman, runtime_bundle
from . import cloud, kubeconfigs, registries, links
from .atomicio import atomic_write_bytes, atomic_write_json, atomic_write_text, read_json
from .config import ENV_PREFIX, IMPLEMENTED_TRACKERS, WORKFLOW_SKILLS, Config, apply_box_policy, load_config, effective_config, session_account
from . import image_build, migrations, networking
from .errors import AgentBoxError
from .external_config import has_toml_spelling, toml_string
from .identity import HostIdentity, detect_host_identity
from .launch_options import LaunchOptions
from .mounts import MountSpec, host_tmp_mounts, plan_mounts
from .notify import SUPPRESS_NOTIFY_ENV
from .paths import (
    AGENT_SKILLS_DIR,
    AGENTS_DIR,
    BUILD_STATE,
    CLAUDE_HOME,
    DEFAULT_CLAUDE_HOME,
    DEFAULT_CLAUDE_SIDECAR,
    CODEX_HOME,
    CODEX_PLUGIN_CACHE,
    CONFIG_ROOT,
    CONTAINER_LAUNCHER_ROOT,
    CONTAINER_LAUNCHER,
    PACKAGE_ROOT,
    HOST_LAUNCHER,
    CONTAINER_NOTIFY_CREDENTIALS,
    CONTAINER_TRACKER_RUNTIME_DIR,
    CONTAINER_POLICY_RUNTIME_DIR,
    CONTAINER_WORKFLOW_STATE,
    HOST_HOME,
    HOST_RUNTIME_DIR,
    HOST_SESSIONS_ROOT,
    HOST_SESSION_RUNNER,
    MANAGED_SKILLS_SOURCE,
    NOTIFY_CREDENTIALS,
    NOTIFY_STATE_ROOT,
    STATE_ROOT,
    TOOLS_REFRESH_FILE,
    TRACKER_RUNTIME_DIR,
    POLICY_RUNTIME_DIR,
    WORKFLOW_STATE,
    XDG_CACHE_HOME,
)
from .sessions import host_tmux_env, tmux_argv, tmux_session_exists, tmux_sessions
from .workspace import Workspace, resolve_workspace

IMAGE = os.environ.get("AGENT_BOX_IMAGE", "localhost/agent-box:latest")
CHATGPT_DESKTOP_RESOURCES = "/usr/lib/chatgpt/resources/"
WORKSPACE_CONTAINER_SCHEMA = "4"
WORKSPACE_RUNTIME_LABEL = "io.agent-box.runtime"
WORKSPACE_CONTAINER_POLICY_LABEL = "io.agent-box.policy"
WORKSPACE_CONTAINER_LABEL = "io.agent-box.managed"
WORKSPACE_CONTAINER_WORKSPACE_LABEL = "io.agent-box.workspace"
WORKSPACE_CONTAINER_BASE_CONFIG_LABEL = "io.agent-box.base-config-hash"
WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL = "io.agent-box.extra-mounts-hash"
WORKSPACE_CONTAINER_SCHEMA_LABEL = "io.agent-box.schema"
#: The workspace id that owns a nested Podman volume, as ``io.agent-box.k3s`` for a cluster.
NESTED_PODMAN_LABEL = "io.agent-box.podman"
WORKSPACE_PATH_LABEL = host_podman.WORKSPACE_PATH_LABEL
#: The project directory of a workspace, beside the rest of its state.
WORKSPACE_PATH_FILE = "path"
KEEPER_PATH = CONTAINER_LAUNCHER_ROOT / "agent_box/keeper.sh"
#: Only these run as a person's live session; ``doctor`` is a diagnostic
#: script whose output belongs on the caller's stdout.
TMUX_AGENTS = frozenset({"claude", "codex", "shell"})
CONTAINER_RUNTIME_PATH = (
    f"{HOST_HOME}/.local/share/mise/shims:{HOST_HOME}/.cargo/bin:/usr/local/bin:{HOST_HOME}/.local/bin:"
    "/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
)


def warn(message: str) -> None:
    print(f"agent-box: warning: {message}", file=sys.stderr)


def _uses_chatgpt_desktop_resources(server: object) -> bool:
    if not isinstance(server, dict):
        return False
    values = [server.get("command")]
    args = server.get("args")
    if isinstance(args, list):
        values.extend(args)
    env = server.get("env")
    if isinstance(env, dict):
        values.extend(env.values())
    return any(
        isinstance(value, str) and CHATGPT_DESKTOP_RESOURCES in value
        for value in values
    )


#: One ``-c`` config path segment Codex accepts without quoting.
CODEX_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def codex_disable(path: list[str]) -> str:
    """One ``-c`` override setting the last ``path`` segment to false.

    Codex splits a ``-c`` key on dots itself and never unquotes a segment, so
    a path carrying a character TOML forbids in a bare key cannot be spelled
    the obvious way: ``mcp_servers."node_repl".enabled=false`` sends Codex
    looking for a server named ``"node_repl"`` and aborts the launch with
    "invalid transport", while ``plugins."id@market"…`` is ignored outright.
    Such a path is written as an inline table instead, which Codex merges into
    the table it already has and which agent-box's own launch validation can
    parse as TOML -- a value it must parse, because the plugin ids it filters
    always carry an ``@``.
    """
    if all(CODEX_BARE_KEY.fullmatch(part) for part in path):
        return ".".join(path) + "=false"
    table = "false"
    for part in reversed(path[1:]):
        key = part if CODEX_BARE_KEY.fullmatch(part) else toml_string(part)
        table = f"{{{key}={table}}}"
    root = path[0] if CODEX_BARE_KEY.fullmatch(path[0]) else toml_string(path[0])
    return f"{root}={table}"


def _plugin_mcp_overrides(config: dict) -> list[str]:
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return []

    overrides = []
    for plugin_id, settings in plugins.items():
        if not isinstance(plugin_id, str) or not isinstance(settings, dict):
            continue
        if settings.get("enabled") is not True:
            continue
        name, separator, marketplace = plugin_id.rpartition("@")
        if not separator or not name or not marketplace:
            continue
        plugin_cache = CODEX_PLUGIN_CACHE / marketplace / name
        for manifest_path in sorted(plugin_cache.glob("*/.codex-plugin/plugin.json")):
            plugin_root = manifest_path.parent.parent
            try:
                manifest = json.loads(manifest_path.read_text())
                mcp_config_name = manifest.get("mcpServers")
                if not isinstance(mcp_config_name, str):
                    continue
                mcp_config_path = (plugin_root / mcp_config_name).resolve()
                if not mcp_config_path.is_relative_to(plugin_root.resolve()):
                    continue
                mcp_config = json.loads(mcp_config_path.read_text())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                warn(f"could not read Codex plugin MCP configuration for '{plugin_id}'")
                continue
            plugin_servers = mcp_config.get("mcpServers")
            if not isinstance(plugin_servers, dict):
                continue
            for server_name, server in plugin_servers.items():
                if not isinstance(server_name, str) or not _uses_chatgpt_desktop_resources(server):
                    continue
                if not has_toml_spelling(plugin_id) or not has_toml_spelling(server_name):
                    warn(f"could not disable desktop MCP of plugin '{plugin_id}': "
                         f"'{server_name}' has no TOML spelling")
                    continue
                overrides.append(codex_disable(
                    ["plugins", plugin_id, "mcp_servers", server_name, "enabled"]
                ))
    return overrides


def codex_runtime_overrides() -> list[str]:
    """Disable MCPs that only the host ChatGPT desktop application provides.

    ``CODEX_HOME`` remains the normal bind-mounted home, so both the user's
    explicit MCPs and MCPs contributed by enabled plugins must be inspected.
    These ephemeral command-line settings stop Codex in the container from
    trying to execute desktop resources without changing either source.
    """
    config_path = CODEX_HOME / "config.toml"
    if not config_path.is_file():
        return []
    try:
        data = tomllib.loads(config_path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        warn(f"could not parse Codex config at {config_path}; not filtering MCP servers")
        return []
    configured_servers = data.get("mcp_servers")
    servers = configured_servers if isinstance(configured_servers, dict) else {}

    overrides = []
    for name in sorted(servers):
        server = servers[name]
        if not isinstance(name, str):
            continue
        if not _uses_chatgpt_desktop_resources(server):
            continue
        overrides.append(codex_disable(["mcp_servers", name, "enabled"]))
    overrides.extend(_plugin_mcp_overrides(data))
    return sorted(set(overrides))


def codex_workflow_overrides(rest: list[str], config: Config) -> list[str]:
    """Apply coordinator capacity and waits without changing the user's Codex config."""
    if not config.workflow.enabled or not host_interactive_invocation("codex", rest):
        return []
    limit = config.workflow.scout.limit
    # V2 counts the coordinator; its explicit feature setting takes precedence
    # over agents.*. Set both from the same policy for either native tool surface.
    overrides = [f"agents.max_concurrent_threads_per_session={limit}",
                 f"features.multi_agent_v2.max_concurrent_threads_per_session={limit + 1}"]
    policy = config.agents.codex.native_agent_wait
    if policy.enabled and not policy.validate():
        overrides.append(f"features.multi_agent_v2.default_wait_timeout_ms={policy.interval_seconds * 1000}")
    return overrides


def codex_command(
    rest: list[str], config: Config,
    browser_session_dir: Path | None = None,
) -> list[str]:
    """Build the Codex command, including all container-only config overrides.

    ``browser_session_dir`` adds the container-native Playwright MCP server
    for this invocation only (see :func:`browser_codex_overrides`). It is
    additive: the ChatGPT desktop MCP filtering below is unaffected, and both
    kinds of override reach Codex through the same ephemeral ``-c`` mechanism
    without ever writing to the user's ``~/.codex/config.toml``.
    """
    command = ["codex"]
    for override in browser_codex_overrides(browser_session_dir):
        command += ["-c", override]
    for override in codex_runtime_overrides():
        command += ["-c", override]
    for override in codex_workflow_overrides(rest, config):
        command += ["-c", override]
    return [*command, "--dangerously-bypass-approvals-and-sandbox", *rest]


def ensure_state_dirs() -> None:
    for path in (
        BUILD_STATE, STATE_ROOT / "cache", STATE_ROOT / "uv", STATE_ROOT / "mise",
        STATE_ROOT / "mise-config", STATE_ROOT / "cargo", STATE_ROOT / "rustup", NOTIFY_STATE_ROOT,
        STATE_ROOT / "runtime/workspaces", CODEX_HOME, CLAUDE_HOME,
        XDG_CACHE_HOME / "claude-code", XDG_CACHE_HOME / "claude-cli-nodejs",
    ):
        path.mkdir(parents=True, exist_ok=True)


def ensure_refresh_token() -> None:
    if not TOOLS_REFRESH_FILE.is_file() or TOOLS_REFRESH_FILE.stat().st_size == 0:
        atomic_write_text(TOOLS_REFRESH_FILE, "initial\n")


#: Label the image carries, naming the optional tool groups it was built with.
IMAGE_TOOLS_LABEL = "io.agent-box.tools"
#: Labels recording the subordinate ID ranges the image's nested Podman delegates.
IMAGE_ID_RANGE_LABELS = {"uid": "io.agent-box.podman-uid-ranges", "gid": "io.agent-box.podman-gid-ranges"}


def tool_build_args(config: Config) -> list[str]:
    """``--build-arg`` flags selecting the image's optional tool groups.

    Every group is passed explicitly, including the ones that are off, so the
    build never depends on the Containerfile's own defaults agreeing with the
    configuration -- and so turning a group off is as much a decision the
    build layer sees as turning one on.
    """
    args: list[str] = []
    for name, enabled in sorted(config.box.tools.groups.items()):
        args += ["--build-arg", f"TOOLS_{name.upper()}={'1' if enabled else '0'}"]
    args += ["--build-arg", f"AGENT_BOX_TOOLS={','.join(config.box.tools.enabled_groups())}"]
    return args


def runtime_config_base() -> Config:
    """Nested launches use the session snapshot; the host config is not mounted."""
    if os.environ.get("AGENT_BOX_RUNTIME_CONFIG"):
        return effective_config(include_project=False)
    return load_config()


def image_build_arguments(config: Config, identity: HostIdentity) -> list[str]:
    return [
        "--build-arg", f"USER_NAME={identity.name}",
        "--build-arg", f"USER_UID={identity.uid}",
        "--build-arg", f"USER_GID={identity.gid}",
        "--build-arg", f"USER_HOME={identity.home}",
        "--build-arg", f"TOOLS_REFRESH={TOOLS_REFRESH_FILE.read_text().strip()}",
        *tool_build_args(config), *nested_podman_build_args(config, identity),
    ]


def build_image(script_dir: Path, refresh: bool, no_cache: bool,
                identity: HostIdentity | None = None,
                config: Config | None = None) -> int:
    ensure_state_dirs()
    identity = identity or detect_host_identity()
    config = effective_config(runtime_config_base()) if config is None else config
    with image_build.lock(BUILD_STATE):
        ensure_refresh_token()
        if refresh:
            now = datetime.now(timezone.utc)
            token = now.strftime("%Y%m%dT%H%M%S.") + f"{time.time_ns() % 1_000_000_000:09d}Z\n"
            atomic_write_text(TOOLS_REFRESH_FILE, token)
        arguments = image_build_arguments(config, identity)
        with image_build.context(script_dir, BUILD_STATE, arguments) as (directory, fingerprint, iidfile):
            rc, _ = image_build.build(IMAGE, directory, fingerprint, iidfile, arguments, no_cache)
    if rc == 0 and config.box.k3s.enabled and not inside_box():
        k3s.pull(config.box.k3s)
    return rc


def ensure_workspace_image(script_dir: Path, config: Config) -> str:
    """Recheck under the build lock; return an ID immune to later tag changes."""
    with image_build.lock(BUILD_STATE):
        ensure_refresh_token()
        arguments = image_build_arguments(config, detect_host_identity())
        with image_build.context(script_dir, BUILD_STATE, arguments) as (directory, fingerprint, iidfile):
            current = image_build.current(IMAGE)
            if current is not None and current[1] == fingerprint:
                return current[0]
            reason = "missing" if current is None else "older build inputs or no input label"
            print(f"agent-box: workspace image {reason}; building with layer cache...", file=sys.stderr)
            rc, identity = image_build.build(IMAGE, directory, fingerprint, iidfile, arguments, False)
            if rc:
                raise AgentBoxError("required workspace image build failed; previous image retained; "
                                    "fix the build error and retry the launch or run agent-box build")
            return identity


def nested_podman_ranges(identity: HostIdentity) -> dict[str, list[tuple[int, int]]]:
    """The subordinate UID and GID ranges this Podman can delegate to a box."""
    try:
        mappings = nested_podman.host_info()["idMappings"]
        return {kind: nested_podman.subid_ranges(mappings[kind + "map"], getattr(identity, kind))
                for kind in ("uid", "gid")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AgentBoxError(f"Cannot configure nested Podman IDs: {exc}") from exc


def nested_podman_build_args(config: Config, identity: HostIdentity) -> list[str]:
    if not config.box.tools.groups["podman"]:
        return []
    return [arg for kind, ranges in nested_podman_ranges(identity).items()
            for arg in ("--build-arg", f"PODMAN_{kind.upper()}_RANGES={json.dumps(ranges, separators=(',', ':'))}")]


def nested_podman_drift(config: Config) -> list[str]:
    """An image whose nested Podman delegates ranges this Podman no longer has.

    The image writes its builder's ranges into /etc/subuid and /etc/subgid,
    and Podman in the box can map only what the box itself was given: an
    image built on another host, a level up, or before the host's subordinate
    IDs changed creates a box whose Podman fails on newuidmap. Reported like
    tool drift and never rebuilt here. An unlabelled image is unknown, and one
    built without the group is tool drift's to report.
    """
    if not config.box.tools.groups["podman"]:
        return []
    try:
        built = {kind: [tuple(item) for item in json.loads(image_label(label) or "null") or []]
                 for kind, label in IMAGE_ID_RANGE_LABELS.items()}
    except (ValueError, TypeError):
        return []
    if not all(built.values()):
        return []
    try:
        current = nested_podman_ranges(detect_host_identity())
    except AgentBoxError:
        return []  # Creating the box reports this Podman's own problem.
    if built == current:
        return []
    return ["the image's nested Podman was built for other subordinate ID ranges than this Podman "
            "can delegate, so Podman in the box cannot start -- run 'agent-box build'"]


def nested_podman_volume(workspace_id: str) -> str:
    return f"agent-box-podman-{workspace_id}"


def _nested_podman_volume_owner(workspace_id: str) -> str | None:
    """The volume's owner label: ``None`` when it is absent, ``""`` when nobody's."""
    volume = nested_podman_volume(workspace_id)
    try:
        exists = subprocess.run(["podman", "volume", "exists", volume], text=True, capture_output=True)
        # Only 1 means absent: a failing Podman must not pass for deleted data.
        if exists.returncode == 1:
            return None
        if exists.returncode:
            raise AgentBoxError(f"cannot inspect the Podman volume {volume}: "
                                f"{exists.stderr.strip() or f'exit {exists.returncode}'}")
        result = subprocess.run(["podman", "volume", "inspect", "--format",
                                 f'{{{{index .Labels "{NESTED_PODMAN_LABEL}"}}}}', volume],
                                text=True, capture_output=True)
    except OSError as exc:
        raise AgentBoxError(f"cannot inspect the Podman volume {volume}: {exc}") from exc
    if result.returncode:
        raise AgentBoxError(f"cannot inspect the Podman volume {volume}: {result.stderr.strip()}")
    owner = result.stdout.strip()
    return "" if owner == "<no value>" else owner


def _require_own_nested_podman_volume(workspace_id: str, owner: str) -> None:
    if owner != workspace_id:
        volume = nested_podman_volume(workspace_id)
        raise AgentBoxError(f"the Podman volume {volume} exists but agent-box did not create it for this "
                            f"workspace; agent-box neither uses nor deletes it -- inspect it, then "
                            f"'podman volume rm {volume}'")


def ensure_nested_podman_volume(workspace_id: str, workspace_path: str) -> None:
    """Create the workspace's nested Podman volume with the labels that name its owner and project.

    Explicit rather than left to ``--mount``, which creates an unlabelled
    volume: the labels let ``uninstall`` and ``workspaces`` find every such
    volume and say which project it belongs to, since the workspace id is a
    hash that cannot be turned back into a path.
    """
    owner = _nested_podman_volume_owner(workspace_id)
    if owner is None:
        command = ["podman", "volume", "create", f"--label={NESTED_PODMAN_LABEL}={workspace_id}",
                   f"--label={WORKSPACE_PATH_LABEL}={workspace_path}",
                   nested_podman_volume(workspace_id)]
        try:
            created = subprocess.run(command, text=True, capture_output=True)
        except OSError as exc:
            raise AgentBoxError(f"cannot create the Podman volume: {exc}") from exc
        if not created.returncode:
            return
        # Another launch may have created it first; anything else is an error.
        owner = _nested_podman_volume_owner(workspace_id)
        if owner is None:
            raise AgentBoxError(f"cannot create the Podman volume: {created.stderr.strip()}")
    _require_own_nested_podman_volume(workspace_id, owner)


def remove_nested_podman_volume(workspace_id: str) -> bool:
    """Delete the workspace's nested Podman data; whether there was any."""
    owner = _nested_podman_volume_owner(workspace_id)
    if owner is None:
        return False
    _require_own_nested_podman_volume(workspace_id, owner)
    volume = nested_podman_volume(workspace_id)
    result = subprocess.run(["podman", "volume", "rm", volume], text=True, capture_output=True)
    if result.returncode:
        raise AgentBoxError(f"cannot delete the Podman volume {volume}: {result.stderr.strip()}")
    return True


def nested_podman_workspaces() -> list[str]:
    """Workspace ids with a nested Podman volume agent-box created, for uninstall."""
    result = subprocess.run(["podman", "volume", "ls", f"--filter=label={NESTED_PODMAN_LABEL}",
                             "--format={{.Name}}"], text=True, capture_output=True)
    if result.returncode:
        raise AgentBoxError(f"cannot list Podman volumes: {result.stderr.strip()}")
    return sorted(match.group(1) for line in result.stdout.splitlines()
                  if (match := re.fullmatch(r"agent-box-podman-([0-9a-f]{16})", line.strip())))


def nested_podman_args(config: Config, workspace: Workspace) -> list[str]:
    if not config.box.tools.groups["podman"]:
        return []
    try:
        if not os.access("/dev/net/tun", os.R_OK | os.W_OK):
            raise ValueError("/dev/net/tun must be readable and writable for userspace networking")
        host = nested_podman.host_info()
        profile = nested_podman.seccomp_profile(read_json(Path(host["security"]["seccompProfilePath"])))
        text = json.dumps(profile, sort_keys=True) + "\n"
        # A changed host profile changes the workspace identity. Never overwrite
        # a profile an existing container was created with.
        digest = hashlib.sha256(text.encode()).hexdigest()
        path = STATE_ROOT / "runtime/nested-podman" / f"seccomp-{digest}.json"
        if not path.exists():
            atomic_write_text(path, text)
        return ["--cap-add=SETUID", "--cap-add=SETGID", "--device=/dev/net/tun",
                "--security-opt=unmask=/proc/*", f"--security-opt=seccomp={path}",
                f"--mount=type=volume,source={nested_podman_volume(workspace_identity(workspace))},"
                f"target={HOST_HOME}/.local/share/containers"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AgentBoxError(f"Cannot configure nested Podman: {exc}") from exc


def image_label(name: str, image: str | None = None) -> str | None:
    """A label of the built image, or ``None`` when it is absent or cannot be read."""
    command = ["podman", "image", "inspect", "--format",
               f"{{{{index .Labels \"{name}\"}}}}", image or IMAGE]
    try:
        result = subprocess.run(command, text=True, capture_output=True)
    except OSError:
        return None
    if result.returncode:
        return None
    value = result.stdout.strip()
    return None if value in ("", "<no value>") else value


def image_tool_groups(image: str | None = None) -> list[str] | None:
    """The groups the built image carries, or ``None`` when it cannot be read.

    ``None`` is not "no groups": an image that predates the label, or a
    Podman that cannot be asked, must not be reported as missing everything.
    """
    value = image_label(IMAGE_TOOLS_LABEL, image)
    if value is None:
        return None
    return sorted(part for part in value.split(",") if part)


def image_tool_drift(config: Config) -> tuple[list[str], list[str]]:
    """Groups the configuration wants but the image lacks, and the reverse.

    Read rather than acted on: rebuilding an image is slow and destroys
    nothing the user asked to keep, so agent-box reports the mismatch and
    lets them choose the moment.
    """
    built = image_tool_groups()
    if built is None:
        return ([], [])
    wanted = config.box.tools.enabled_groups()
    return ([g for g in wanted if g not in built], [g for g in built if g not in wanted])


def image_exists() -> bool:
    """Return a definite image-exists answer without hiding storage failures."""
    command = ["podman", "image", "exists", IMAGE]
    try:
        result = subprocess.run(command, text=True, capture_output=True)
    except OSError as exc:
        raise AgentBoxError(f"could not determine image state: {' '.join(command)} failed: {exc}") from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic from podman"
    raise AgentBoxError(
        f"could not determine image state: {' '.join(command)} "
        f"failed with exit {result.returncode}: {detail}"
    )


#: Marks a skill directory as one agent-box installed and therefore owns.
#: Its ``digest`` is the fingerprint of the shipped copy that produced it, so
#: an install can tell "already current" from "shipped copy changed" without
#: comparing trees, and a directory with no marker is recognised as the
#: user's own and never touched.
MANAGED_SKILL_MARKER = ".agent-box-managed.json"


def skill_tree_digest(root: Path) -> str:
    """A content fingerprint of one skill directory.

    Covers relative paths and file bytes, so a renamed or deleted file
    changes the answer as surely as an edited one. The marker file itself is
    excluded, because it carries the digest.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.name == MANAGED_SKILL_MARKER or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            digest.update(b"d\0" + relative.encode() + b"\0")
            continue
        digest.update(b"f\0" + relative.encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def shipped_skill(name: str) -> Path | None:
    """The copy of *name* that ships inside this package, if it exists."""
    source = MANAGED_SKILLS_SOURCE / name
    return source if (source / "SKILL.md").is_file() else None


def managed_skill_digest(target: Path) -> str | None:
    """The digest recorded in *target*'s marker, or ``None`` when unmanaged."""
    try:
        data = migrations.read(target / MANAGED_SKILL_MARKER, migrations.SKILL_MARKER)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    digest = data.get("digest") if isinstance(data, dict) else None
    return digest if isinstance(digest, str) else None


def install_managed_skill(name: str) -> None:
    """Install or refresh one shipped skill in the shared agent tree.

    agent-box installs the skills its integrations need rather than waiting
    for the user to produce them: an integration that can be switched on but
    then reports a missing file is not an integration, it is a checklist.

    Ownership is explicit and one-directional. A directory carrying the
    managed marker belongs to agent-box and is replaced whenever the shipped
    copy differs, so upgrading agent-box upgrades the skill. A directory
    *without* the marker is the user's own -- hand-written, or a symlink into
    a checkout they maintain -- and is left exactly where it is, with a
    warning, because silently overwriting someone's working tree to install a
    convenience is never the right trade.
    """
    source = shipped_skill(name)
    if source is None:
        warn(f"no skill named '{name}' ships with agent-box")
        return
    target = AGENT_SKILLS_DIR / name
    digest = skill_tree_digest(source)
    recorded = None
    if target.exists() or target.is_symlink():
        recorded = managed_skill_digest(target)
        if recorded is None or target.is_symlink() or skill_tree_digest(target) != recorded:
            warn(f"skill '{name}' already exists and is not managed by agent-box; keeping {target}")
            return
        if recorded == digest:
            return
    target.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a complete, marked tree. A failed copy must not leave an
    # unmarked target that the next upgrade would mistake for user content.
    with tempfile.TemporaryDirectory(prefix=".agent-box-stage-", dir=target.parent) as staging:
        prepared = Path(staging) / name
        shutil.copytree(source, prepared, ignore=shutil.ignore_patterns("__pycache__"))
        atomic_write_json(prepared / MANAGED_SKILL_MARKER,
                          {"format_version": migrations.SKILL_MARKER.version,
                           "skill": name, "digest": digest}, 0o644)
        if target.exists() or target.is_symlink():
            if (recorded is None or target.is_symlink()
                    or managed_skill_digest(target) != recorded or skill_tree_digest(target) != recorded):
                warn(f"skill '{name}' changed during installation; keeping {target}")
                return
            # Retire the whole old tree before cleanup: a partial rmtree must
            # never leave an unrecognizable installed skill behind on retry.
            os.replace(target, Path(staging) / "retired")
        os.replace(prepared, target)


def remove_managed_skill(name: str) -> None:
    """Remove a skill agent-box installed; never one the user owns."""
    target = AGENT_SKILLS_DIR / name
    recorded = managed_skill_digest(target)
    if recorded is not None and not target.is_symlink() and skill_tree_digest(target) == recorded:
        with tempfile.TemporaryDirectory(prefix=".agent-box-retired-", dir=target.parent) as retired:
            os.replace(target, Path(retired) / name)


def managed_skills(config: Config) -> set[str]:
    """The shipped skills the current configuration asks for."""
    wanted: set[str] = set()
    if config.tracker.enabled or config.workflow.enabled:
        wanted.add("ab-tracker")
    if config.workflow.enabled:
        wanted.update(WORKFLOW_SKILLS)
    return wanted


def sync_managed_skills(config: Config) -> None:
    """Bring the shared tree in line with what the configuration enables."""
    wanted = managed_skills(config)
    for name in sorted(wanted):
        install_managed_skill(name)
    if AGENT_SKILLS_DIR.is_dir():
        for target in sorted(AGENT_SKILLS_DIR.iterdir()):
            if target.name not in wanted and managed_skill_digest(target) is not None:
                remove_managed_skill(target.name)


def claude_skill_links(config: Config) -> None:
    """Mirror the shared skill tree into Claude's own skills directory.

    Claude reads ``~/.claude/skills`` while Codex reads ``~/.agents/skills``
    directly, so without these symlinks the two agents would see different
    skills. The links land in the user's real Claude configuration, which
    Claude sessions started outside agent-box also read -- hence the switch,
    and hence the care below.

    A name already occupied by something that is not one of these links is
    left alone with a warning: it is the user's, and a shared skill is not
    worth overwriting a real directory for. Turning the switch off removes
    only the links agent-box created, leaving anything else untouched.
    """
    source, target_root = AGENT_SKILLS_DIR, CLAUDE_HOME / "skills"

    def points_into_tree(link: Path) -> bool:
        return link.is_symlink() and os.readlink(link).startswith(str(source) + os.sep)

    if not config.host.claude_skill_links:
        if target_root.is_dir():
            for link in target_root.iterdir():
                if points_into_tree(link):
                    link.unlink(missing_ok=True)
        return
    if not source.is_dir():
        return
    target_root.mkdir(parents=True, exist_ok=True)
    for skill in source.iterdir():
        if not (skill / "SKILL.md").is_file():
            continue
        target = target_root / skill.name
        if target.is_symlink():
            try:
                if target.resolve(strict=True) == skill.resolve(strict=True):
                    continue
            except FileNotFoundError:
                pass
            warn(f"Claude skill '{skill.name}' already points elsewhere; keeping {target}")
        elif target.exists():
            warn(f"Claude skill '{skill.name}' already exists; keeping {target}")
        else:
            target.symlink_to(skill)
    for link in target_root.iterdir():
        if points_into_tree(link) and not link.exists():
            link.unlink(missing_ok=True)


def seed_state_dir(target: Path, *sources: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    for source in sources:
        if not source.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(source, target / "config")


GIT_IDENTITY_KEYS = ("user.name", "user.email")

#: Scoped to github.com only -- GH_TOKEN is a GitHub credential, not a
#: generic one, so this must not apply to any other remote host.
GH_CREDENTIAL_URL = "https://github.com"
#: Delegates to the GitHub CLI's own credential support (the same mechanism
#: `gh auth setup-git` configures). `gh` picks up GH_TOKEN/GITHUB_TOKEN from
#: its own process environment -- which is exactly how the token already
#: reaches the container (see `github_env`) -- so this line never embeds the
#: token value itself; it only tells git which program to ask. The
#: container's image always has `gh` on PATH (see docs/image.md).
GH_CREDENTIAL_HELPER = "!gh auth git-credential"


def _set_git_config(config_file: Path, key: str, value: str) -> None:
    """Set ``key`` to exactly one value, even if the file (e.g. a copied-in
    full host gitconfig) already defines it multiple times -- plain ``git
    config --file ... key value`` refuses to overwrite an ambiguous
    multi-valued key outright, so this always uses ``--replace-all``."""
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.touch(exist_ok=True)
    subprocess.run(
        ["git", "config", "--file", str(config_file), "--replace-all", key, value], check=False,
    )


def seed_git_config(target: Path, config: Config) -> None:
    """Populate the container's git config directory.

    Three outcomes for the bulk of it, by design:

    * ``full_config`` on -- copy the host's git configuration wholesale. A
      real ``~/.gitconfig`` commonly carries ``credential.helper`` entries,
      ``http.<url>.extraHeader`` bearer tokens, ``url.insteadOf`` rewrites and
      ``include.path`` directives, so this is opt-in.
    * ``identity`` on (the default) -- write a minimal config holding only
      ``user.name`` and ``user.email``, read one key at a time from the host so
      nothing else can ride along. That is what commit attribution needs and
      it reveals nothing beyond a name and address the commits would carry
      anyway.
    * both off -- an empty directory; git falls back to its own defaults.

    Independently of those three, when GitHub token passthrough is enabled
    (``github_enabled``), a
    ``credential.https://github.com.helper`` entry is added so a plain
    ``git push`` over HTTPS actually has a usable credential path -- without
    this, forwarding ``GH_TOKEN`` into the container environment alone does
    nothing, because plain git never consults it on its own.
    """
    if config.credentials.git.full_config:
        seed_state_dir(target, HOST_HOME / ".config/git", HOST_HOME / ".gitconfig")
    else:
        seed_state_dir(target)
        if config.credentials.git.identity:
            lines = []
            for key in GIT_IDENTITY_KEYS:
                result = subprocess.run(
                    ["git", "config", "--global", "--get", key], text=True, capture_output=True,
                )
                value = result.stdout.strip() if result.returncode == 0 else ""
                if value:
                    lines.append(f"\t{key.split('.', 1)[1]} = {value}")
            if lines:
                atomic_write_text(target / "config", "[user]\n" + "\n".join(lines) + "\n")
    if github_enabled(config):
        _set_git_config(target / "config", f"credential.{GH_CREDENTIAL_URL}.helper", GH_CREDENTIAL_HELPER)
        # SSH credentials never enter the box. Route the standard GitHub SSH
        # forms through the HTTPS helper in this disposable snapshot only.
        for prefix in ("git@github.com:", "ssh://git@github.com/"):
            subprocess.run(
                ["git", "config", "--file", str(target / "config"), "--add",
                 "url.https://github.com/.insteadOf", prefix], check=True,
            )


@dataclass(frozen=True)
class WorkspaceRuntimeState:
    """Mutable bind sources owned by one resolved workspace box."""

    root: Path
    git: Path
    gh: Path


def workspace_state_root(workspace_id: str) -> Path:
    return STATE_ROOT / "runtime/workspaces" / workspace_id


def workspace_state_dirs(workspace_id: str) -> list[Path]:
    """Every directory agent-box keeps for one workspace, its own state root first."""
    return [workspace_state_root(workspace_id), TRACKER_RUNTIME_DIR / "workspaces" / workspace_id,
            POLICY_RUNTIME_DIR / "workspaces" / workspace_id, registries.runtime_dir(workspace_id),
            kubeconfigs.runtime_dir(workspace_id), cloud.runtime_dir(workspace_id).parent,
            HOST_SESSIONS_ROOT / workspace_id, links.directory(workspace_id)]


def record_workspace_path(workspace_id: str, path: str) -> None:
    """Name the project beside its state, for ``agent-box workspaces``; the id alone cannot."""
    root = workspace_state_root(workspace_id)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Bytes, as the path itself is: a directory name need not be UTF-8.
    atomic_write_bytes(root / WORKSPACE_PATH_FILE, os.fsencode(path) + b"\n", 0o600)


def workspace_runtime_state(workspace: Workspace) -> WorkspaceRuntimeState:
    root = workspace_state_root(workspace_identity(workspace))
    return WorkspaceRuntimeState(root=root, git=root / "git", gh=root / "gh")


def legacy_runtime_state(workspace: Workspace | None = None) -> WorkspaceRuntimeState:
    """State used only by the retained disposable argv compatibility path."""
    return WorkspaceRuntimeState(
        root=workspace_runtime_state(workspace).root if workspace is not None else STATE_ROOT,
        git=STATE_ROOT / "git",
        gh=STATE_ROOT / "gh",
    )


def prepare_container_lifetime_state(
    config: Config, *, mount_notifications: bool, runtime_state: WorkspaceRuntimeState | None = None,
) -> None:
    """Seed bind-mount sources immediately before creating a new box.

    ``git`` and ``gh`` are directory bind mounts.  Reseeding them replaces
    their root inodes, so it is safe only when no workspace container is using
    them.  Callers for persistent boxes therefore invoke this only from the
    new-container branch under the workspace transition lock.  The retained
    disposable argv helper prepares on every run because its box has no
    surviving mounts.
    """
    runtime_state = runtime_state or legacy_runtime_state()
    ensure_state_dirs()
    seed_git_config(runtime_state.git, config)
    seed_state_dir(
        runtime_state.gh,
        *((HOST_HOME / ".config/gh",) if config.credentials.github.config else ()),
    )
    if mount_notifications:
        notify_module.sync_credentials(config, NOTIFY_CREDENTIALS)
    tracker_mounts(config, runtime_state)[0][0].mkdir(parents=True, exist_ok=True)
    for source, _, _ in [*registries.mounts(config, runtime_state.root.name),
                         *kubeconfigs.mounts(config, runtime_state.root.name),
                         *cloud.mounts(config, runtime_state.root.name)]:
        source.mkdir(mode=0o700, parents=True, exist_ok=True)
    if config.box.links.enabled:
        links.prepare(runtime_state.root.name)
    else:
        # The name can be reused before the old broker's liveness check.
        links.stop(runtime_state.root.name)
    if config.tracker.enabled or config.workflow.enabled:
        WORKFLOW_STATE.mkdir(parents=True, exist_ok=True)
        for problem in tracker_warnings(config):
            warn(problem)


def agent_tree_mounts() -> list[tuple[Path, Path, bool]]:
    """Mount ``~/.agents`` safely while letting agents maintain their skills.

    Scope, decided deliberately: the **whole** ``~/.agents`` tree is walked
    recursively, not just ``~/.agents/skills``.

    The tree itself is bind-mounted read-only at its own path, then ``skills/``
    is overlaid read-write. This lets skill-authoring agents create and update
    their own skills without also granting write access to shared agent
    definitions, commands, or prompt fragments elsewhere in the tree.

    Every symlink in the tree is followed so it resolves in the container.
    External targets linked from ``skills/`` are read-write; targets linked
    only from elsewhere stay read-only. Symlinks pointing back inside
    ``~/.agents`` are skipped (the whole-tree mounts already cover them), and
    a broken symlink is warned about and skipped rather than failing the run.
    """
    agents = AGENTS_DIR
    if not agents.is_dir():
        return []
    skills = AGENT_SKILLS_DIR
    mounts: list[tuple[Path, Path, bool]] = [(agents, agents, True)]
    if skills.is_dir() and not skills.is_symlink():
        mounts.append((skills, skills, False))
    external_modes: dict[Path, bool] = {}
    for link in sorted(agents.rglob("*")):
        if not link.is_symlink():
            continue
        try:
            host_target = link.resolve(strict=True)
        except OSError:
            warn(f"broken agent symlink: {link}")
            continue
        if agents in host_target.parents or host_target == agents:
            continue  # already covered by the whole-tree mounts above
        read_only = not link.is_relative_to(skills)
        # The same target can be shared by skill and non-skill links. One
        # writable skill reference must keep that target writable regardless
        # of which link sorts first.
        external_modes[host_target] = external_modes.get(host_target, True) and read_only
    for host_target, read_only in sorted(
        external_modes.items(), key=lambda item: (len(item[0].parts), str(item[0]))
    ):
        mounts.append((host_target, host_target, read_only))
    return mounts


#: Podman's stable name for the host as seen from inside a container. Both
#: pasta and slirp4netns write it into the container's /etc/hosts, so it does
#: not depend on which rootless network backend is in use.
CONTAINER_HOST_ALIAS = "host.containers.internal"
#: Podman's magic ``--add-host`` target: the host's address as the container
#: sees it, resolved by Podman at creation time rather than guessed here.
CONTAINER_HOST_GATEWAY = "host-gateway"
#: Host names that mean "this machine" to the host but would mean "this
#: container" inside one. Compared lower-cased; an IPv6 literal has already
#: had its brackets stripped by ``urlsplit``.
LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def is_ip_literal(host: str) -> bool:
    """True when *host* is an address rather than a name to be resolved."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_host_local(host: str) -> bool:
    """True when *host* names an address this machine itself answers on.

    Binding is the test rather than an inventory of interface addresses: the
    kernel lets a socket bind only to an address the local stack owns, which
    is precisely the question being asked, and it needs neither a subprocess
    nor a dependency. The candidate addresses come from the host's own
    resolver, so a LAN name that DNS points at this machine is recognised the
    same way a bare address literal is.

    Any resolution or binding failure answers "not local", which leaves the
    URL untouched: an address agent-box cannot reason about is better handed
    to the skill verbatim than redirected on a guess.
    """
    try:
        candidates = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, socktype, proto, _canonname, sockaddr in candidates:
        try:
            with socket.socket(family, socktype, proto) as probe:
                probe.bind(sockaddr)
        except OSError:
            continue
        return True
    return False


def container_tracker_url(raw: str) -> str:
    """Rewrite a Vikunja address that has no name worth preserving.

    A self-hosted tracker is almost always configured on the host as
    ``http://127.0.0.1:3456``. Inside a container that address is the
    *container's* own loopback, so the skill would fail to connect with no
    hint as to why. The same trap closes on the host's own LAN address:
    Podman's rootless networking gives the container that address too, so
    forwarding it verbatim still addresses the container. Rewriting only the
    host part -- scheme, port and path are preserved -- turns the host's own
    setting into a working one without asking the user to maintain a second
    URL, and without touching the host configuration the skill also uses
    outside agent-box.

    Only an address with no name to preserve is rewritten here: ``localhost``
    or an IP literal belonging to this machine. A DNS name that resolves to
    the host keeps its URL and is redirected by :func:`tracker_host_args`
    instead, because rewriting it would break TLS certificate validation and
    reverse-proxy virtual-host routing. A genuinely remote Vikunja -- a LAN
    box next door or a public domain -- is returned unchanged.
    """
    url = raw.strip()
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        # A malformed authority (e.g. a non-numeric port). Not agent-box's to
        # repair: hand it through and let the skill report it.
        return url
    if host is None:
        return url
    if host.lower() not in LOOPBACK_HOSTNAMES and not (
            is_ip_literal(host) and is_host_local(host)):
        return url
    netloc = CONTAINER_HOST_ALIAS if port is None else f"{CONTAINER_HOST_ALIAS}:{port}"
    if parts.username:
        credentials = parts.username
        if parts.password:
            credentials = f"{credentials}:{parts.password}"
        netloc = f"{credentials}@{netloc}"
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


def sync_tracker_runtime(config: Config, directory: Path | None = None, *, policy=False, container=True) -> None:
    """Render only explicitly enabled capabilities; directory mounts see atomic updates."""
    launch = getattr(config, "_launch", {})
    cluster_policy = getattr(config, "_cluster_policy", None)
    if cluster_policy is not None:
        launch = {key: value for key, value in launch.items() if key != "k3s"}
    browser_enabled = config.box.browser.active()
    network = config.box.network.to_dict()
    isolated_network = container and config.box.network.mode != "host"
    config = getattr(config, "_project_base", config)
    directory = TRACKER_RUNTIME_DIR if directory is None else directory
    if not policy and not (config.tracker.enabled or config.workflow.enabled):
        (directory / "config.json").unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass
        return
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    tracker = config.tracker.to_dict()
    if not config.tracker.enabled:
        tracker["connections"] = {}
    external = {name: spec.to_dict() for name, spec in config.agents.external_apis.items() if spec.enabled}
    if isolated_network:
        for spec in external.values():
            for cli in ("codex", "claude"):
                spec[cli]["url"] = container_tracker_url(spec[cli]["url"])
    rendered = {
        "tracker": tracker,
        "tracker_urls": {name: container_tracker_url(connection.url) if isolated_network else connection.url
                         for name, connection in config.tracker.connections.items()
                         if config.tracker.enabled},
        "global_tracker_urls": getattr(config, "_global_tracker_urls", {
            name: connection.url for name, connection in config.tracker.connections.items()}),
        "workflow": config.workflow.to_dict(),
        # Claude's status line is drawn inside the box, where the saved file is not
        # mounted; whether agent-box owns the host's statusLine slot stays host-only.
        "agents": {
            "claude": {"remote_control": config.agents.claude.remote_control,
                       "statusline": {key: value for key, value in config.agents.claude.statusline.to_dict().items()
                                      if key != "enabled"}},
            "codex": config.agents.codex.to_dict(),
            "external_apis": external,
        },
        "credentials": {"github": config.credentials.github.to_dict(),
                        "registries": config.credentials.registries.to_dict(),
                        "kubernetes": config.credentials.kubernetes.to_dict(),
                        "cloud": config.credentials.cloud.to_dict()},
        # A nested box inherits the outer box's sound socket at the same path.
        "box": {"tools": config.box.tools.to_dict(), "browser": config.box.browser.to_dict(), "k3s": config.box.k3s.to_dict(),
                "network": network,
                "podman": config.box.podman.to_dict(),
                "audio": config.box.audio, "host_tmp": config.box.host_tmp, "links": config.box.links.to_dict()},
        "container": container,
        # Re-applied over the project configuration inside the session.
        "launch": launch,
    }
    if hasattr(config, "_box_policy"):
        rendered["box_policy"] = copy.deepcopy(config._box_policy)
        # A nested launch may explicitly replace its inherited outer network.
        rendered["box_policy"]["box"]["network"] = network
        # Browser containers are per-session capabilities, independent of the box image.
        rendered["box_policy"]["box"]["browser"] = {"enabled": browser_enabled}
        rendered["box"]["browser"] = {"enabled": browser_enabled}
    if cluster_policy is not None:
        rendered["cluster_policy"] = copy.deepcopy(cluster_policy)
    atomic_write_json(directory / "config.json", rendered, 0o600)


def session_runtime(config, *, container=True, runtime_state=None):
    """Snapshots survive detached sessions; later launches never replace their file."""
    import uuid
    if container:
        if runtime_state is None:
            raise ValueError("Container snapshots require workspace runtime state")
        root = tracker_mounts(config, runtime_state)[0][0]
    else:
        root = TRACKER_RUNTIME_DIR if config.tracker.enabled or config.workflow.enabled else POLICY_RUNTIME_DIR
    directory = root / "sessions" / uuid.uuid4().hex
    sync_tracker_runtime(config, directory, policy=True, container=container)
    path = directory / "config.json"
    return path


def requested_network(saved, options):
    network = copy.deepcopy(saved)
    if options.network is not None:
        network.mode = options.network
    if options.publish is not None:
        network.ports = list(options.publish)
    if options.host_ports is not None:
        network.host_ports = list(options.host_ports)
    return network


def launch_config(options=None, *, box_policy=None):
    """Saved settings, then the project, then this launch's flags."""
    from . import launch_options
    options = options or launch_options.LaunchOptions()
    base = runtime_config_base()
    overrides = {**getattr(base, "_launch", {}), **options.overrides}
    policy = base.box.env.overlay(options.env)
    config = effective_config(base)
    if box_policy is not None:
        browser_enabled = config.box.browser.active()
        if not box_policy["workflow_mount"]:
            from .launch_options import WORKFLOW_KEYS
            required = next((key for key in overrides if key in WORKFLOW_KEYS | {"tracker", "project"}), None)
            if required:
                raise AgentBoxError(f"--{required} needs a workflow mount absent from the running workspace box; "
                                    "end it with agent-box stop and rerun")
        apply_box_policy(config, box_policy)
        apply_box_policy(config._project_base, box_policy)
        config.box.browser.off = not browser_enabled
    launch_options.apply(config, overrides)
    config.box.network = requested_network(config.box.network, options)
    # The snapshot renders from the layer beneath the project overlay. Only
    # --no-workflow's withdrawal of the tracker and the --tracker account belong
    # there; any other flag would reach a project copy made inside the session.
    if "workflow" in overrides:
        launch_options.apply(config._project_base, {"workflow": overrides["workflow"]})
    if "tracker" in options.overrides:
        launch_options.bind_tracker_account(config, overrides["tracker"])
    if options.overrides.get("notify"):
        if os.environ.get("AGENT_BOX_RUNTIME_CONFIG"):
            raise AgentBoxError("--notify applies to launches from the host; a session cannot turn notifications on")
        if not config.host.notifications.usable:
            raise AgentBoxError("--notify needs Telegram notifications enabled and set up in agent-box config")
    config.box.env = policy
    if config.tracker.enabled:
        from .project_config import load
        from .repo_binding import server_identity
        provider = config.tracker.provider
        project_url = overrides.get("tracker") or load().get("tracker", {}).get("connections", {}).get(provider, {}).get("url")
        account = session_account(config).connection(provider)
        if account.url and project_url and server_identity(account.url) != server_identity(project_url):
            raise AgentBoxError("Repository binding targets a different tracker server; pass --tracker or update the project binding")
    return config


def tracker_mounts(config: Config, runtime_state: WorkspaceRuntimeState) -> list[tuple[Path, Path, bool]]:
    # The workspace root is mounted even with integrations disabled. Keep
    # credentials outside it, and never mount host or other workspace sessions.
    workspace = runtime_state.root.name
    policy = getattr(config, "_box_policy", None)
    if policy is not None:
        source, destination, read_only = policy["session_mount"]
        mounts = [(Path(source), Path(destination), read_only)]
        if policy["workflow_mount"]:
            mounts.append((WORKFLOW_STATE, CONTAINER_WORKFLOW_STATE, False))
        return mounts
    if not (config.tracker.enabled or config.workflow.enabled):
        return [(POLICY_RUNTIME_DIR / "workspaces" / workspace, CONTAINER_POLICY_RUNTIME_DIR, True)]
    return [(TRACKER_RUNTIME_DIR / "workspaces" / workspace, CONTAINER_TRACKER_RUNTIME_DIR, True),
            (WORKFLOW_STATE, CONTAINER_WORKFLOW_STATE, False)]


def tracker_host_alias(raw: str) -> str:
    """The tracker hostname that must be pointed back at the host, or ``""``.

    Only a DNS name qualifies. Loopback names and address literals are
    already handled by :func:`container_tracker_url`, and a name that
    resolves anywhere but this machine is reachable from the container as it
    stands.
    """
    url = raw.strip()
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port
        host = parts.hostname
    except ValueError:
        return ""
    if not host or host.lower() in LOOPBACK_HOSTNAMES or is_ip_literal(host):
        return ""
    return host if is_host_local(host) else ""


def tracker_host_args(config: Config) -> list[str]:
    """``--add-host=NAME:host-gateway`` for a host-local tracker hostname.

    A self-hosted tracker is often addressed by a LAN name such as
    ``pm.example.org`` that DNS resolves to the host's own address. Podman's
    rootless networking hands the container that same address, so the
    forwarded URL names the container itself and the connection is refused
    with nothing to suggest why. Mapping the name to the host gateway fixes
    the destination while leaving the name in the URL intact, so SNI,
    certificate validation and reverse-proxy virtual-host routing all still
    see the name the server was configured for -- none of which survives a
    URL rewrite.

    Structural rather than per-exec: the mapping belongs to the same
    container lifetime as the mounts it addresses, and changing it therefore
    rebuilds the box rather than silently diverging from the running one.
    """
    if config.box.network.mode == "host":
        return []
    urls = [connection.url for connection in config.tracker.connections.values()] if config.tracker.enabled else []
    urls += [getattr(spec, cli).url for spec in config.agents.external_apis.values() if spec.enabled
             for cli in ("codex", "claude")]
    hosts = {tracker_host_alias(url) for url in urls}
    return [f"--add-host={host}:{CONTAINER_HOST_GATEWAY}" for host in sorted(hosts) if host]



def tracker_warnings(config: Config) -> list[str]:
    from .external_config import validate_connections
    problems = validate_connections(config)
    problems += config.workflow.validate() + config.agents.codex.validate() if config.workflow.enabled else []
    if config.tracker.enabled:
        if not config.tracker.provider:
            problems.append("No tracker provider selected; choose one with agent-box config or in the project configuration")
        else:
            if not config.tracker.connection().usable:
                problems.append("Tracker is enabled but its default connection needs a server URL and token")
            if config.tracker.provider not in IMPLEMENTED_TRACKERS:
                problems.append(f"Tracker provider {config.tracker.provider} is not implemented; choose "
                                + " or ".join(name.title() for name in IMPLEMENTED_TRACKERS))
    if config.workflow.enabled and not config.tracker.enabled:
        problems.append("Workflow needs an enabled tracker connection before tracked work")
    return problems


def browser_session_id(agent: str) -> str:
    """Do not reuse stale browser sockets/artifacts when the OS recycles a PID."""
    return f"{agent}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def browser_session_root(runtime_state: WorkspaceRuntimeState) -> Path:
    """Where every browser session of one workspace keeps its state."""
    return runtime_state.root / "browser"


BROWSER_MCP_SERVER_NAME = "agent_box_browser"
CLAUDE_SKILL_LINKS_ENV = "AGENT_BOX_CLAUDE_SKILL_LINKS"
IMAGE_TOOLS_ENV = "AGENT_BOX_TOOLS"
BROWSER_ENABLED_ENV = "AGENT_BOX_BROWSER"
BROWSER_SESSION_GRACE_SECONDS = 60

def browser_mcp_args(session_dir: Path) -> list[str]:
    """The workspace speaks MCP through a private host-owned stdio bridge."""
    return ["python3", str(session_dir / "client.py"), str(session_dir / "mcp.sock")]


def browser_codex_overrides(session_dir: Path | None, server_command: list[str] | None = None, *, server_name=BROWSER_MCP_SERVER_NAME) -> list[str]:
    """``-c`` overrides registering the browser MCP for one Codex launch.

    An inline TOML table under ``mcp_servers.<name>``, which Codex merges into
    the servers from ``~/.codex/config.toml`` rather than replacing them --
    verified against codex-cli 0.152.1 with ``codex ... mcp list``. The user's
    host-backed ``~/.codex/config.toml`` is never written to; this is the same
    ephemeral mechanism :func:`codex_runtime_overrides` already uses to
    disable ChatGPT desktop MCP servers, and the two compose.
    """
    if session_dir is None:
        return []
    command, *args = server_command or browser_mcp_args(session_dir)
    table = (
        f"command={toml_string(command)}, "
        f"args=[{', '.join(toml_string(arg) for arg in args)}]"
    )
    return [f"mcp_servers.{server_name}={{{table}}}"]


def browser_claude_args(session_dir: Path | None, server_command: list[str] | None = None, *, server_name=BROWSER_MCP_SERVER_NAME) -> list[str]:
    """``--mcp-config`` for one Claude launch, after writing the file.

    Claude Code has no ``-c``-style inline override, but ``--mcp-config``
    takes a JSON file that applies to that invocation only; the user's
    host-backed Claude configuration is not touched. ``--strict-mcp-config``
    is deliberately *not* passed, so the servers the user already configured
    stay loaded alongside this one -- verified against Claude Code 2.1.258.

    The file is written under the workspace runtime state, which is already
    bind-mounted into the box at the same absolute path, so the path is valid
    on both sides without any new mount.
    """
    if session_dir is None:
        return []
    command, *args = server_command or browser_mcp_args(session_dir)
    config_path = session_dir / "mcp.json"
    atomic_write_json(config_path, {
        "mcpServers": {server_name: {"command": command, "args": args}},
    })
    return ["--mcp-config", str(config_path)]


#: A just-created session directory can only belong to a launch that is still
#: starting up -- one that has not created the workspace box yet, so the "no
#: box is running" test below would wrongly call it stale. Anything genuinely
#: abandoned is older than this by a wide margin.
BROWSER_SESSION_GRACE_SECONDS = 60


def remove_browser_sessions(runtime_state: WorkspaceRuntimeState) -> None:
    """Drop every browser session of one workspace. Callers must know the box
    is gone, which is what makes this unconditional."""
    if browser_session_root(runtime_state).exists():
        browser.stop(runtime_state.root.name, "workspace")
        shutil.rmtree(browser_session_root(runtime_state), ignore_errors=True)


def prune_stale_browser_sessions(runtime_state: WorkspaceRuntimeState, keep: str) -> None:
    """Drop browser state left behind by launches that are long gone.

    Only ever called while the workspace has no running container, under the
    workspace transition lock: with no box there is no agent session, so
    nothing can still be using these directories. That makes the rule exact
    rather than a timeout guess, and it is why a *detached* tmux session --
    which keeps its box alive -- can never have its browser state deleted out
    from under it.

    ``keep`` and the grace period cover the one gap in that argument: a
    concurrent launch that has created its own directory but has not yet
    created the box.
    """
    root = browser_session_root(runtime_state)
    if not root.is_dir():
        return
    cutoff = time.time() - BROWSER_SESSION_GRACE_SECONDS
    for entry in root.iterdir():
        if entry.name == keep:
            continue
        try:
            recent = entry.stat().st_mtime > cutoff
        except OSError:
            continue
        if not recent:
            shutil.rmtree(entry, ignore_errors=True)


def browser_session(
    config: Config, workspace: Workspace, runtime_state: WorkspaceRuntimeState, agent: str,
) -> Path | None:
    """This launch's private browser state directory, or ``None`` when off.

    Per launch, not per workspace: two concurrent Claude/Codex sessions in one
    box get separate directories, so neither the (in-memory) profile nor the
    output directory is ever shared by accident.

    A failure to prepare the directory is reported and degrades to "no
    browser" rather than aborting: a browser is an extra capability, and
    losing it is not a reason to refuse to start the agent.
    """
    if not config.box.browser.active() or agent not in {"claude", "codex"}:
        return None
    session_id = browser_session_id(agent)
    try:
        with workspace_transition_lock(workspace):
            current = inspect_workspace_container(workspace_container_name(workspace))
            if current is None or current.get("State", {}).get("Status") != "running":
                prune_stale_browser_sessions(runtime_state, keep=session_id)
            session_dir = browser_session_root(runtime_state) / session_id
            (session_dir / "output").mkdir(parents=True, exist_ok=True)
    except (AgentBoxError, OSError) as exc:
        warn(f"browser integration is on but its session state could not be prepared: {exc}")
        return None
    return session_dir


def build_mounts(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    config: Config | None = None, mount_notifications: bool = True,
    runtime_state: WorkspaceRuntimeState | None = None,
) -> list[str]:
    """Build ``--mount`` flags.

    Everything the agent should see is mounted at the *same absolute path*
    it has on the host (see workspace.py's module docstring) -- there is no
    container-specific home or workspace root to translate to, because the
    container's user/home mirror the host's (agent_box.identity).

    This is deliberately a pure rendering step.  Call
    :func:`prepare_container_lifetime_state` before creating a new container
    so every bind source exists and mutable Git/GH source directories have
    been seeded without replacing roots of a live box.
    """
    from .mounts import MountSpec, plan_mounts

    mount_workspace_runtime_state = runtime_state is not None
    runtime_state = runtime_state or legacy_runtime_state(workspace)
    # The project root and any explicit --mount paths can legitimately
    # overlap (a --mount of a parent directory containing the project is
    # the documented way to expose sibling repos); plan_mounts collapses
    # that deterministically instead of issuing two overlapping podman
    # --mount flags for the same tree.
    project_mounts = plan_mounts([MountSpec(workspace.host_root, False, "project root"), *extra_mounts,
                                 *host_tmp_mounts(config is not None and config.box.host_tmp)])

    pairs: list[tuple[Path, Path, bool]] = [
        # Agent homes intentionally remain host-backed and shared: they hold
        # the user's provider authentication and durable agent settings. Git
        # and gh below are different because their copied, writable config is
        # a snapshot owned by one workspace box lifetime.
        (CODEX_HOME, CODEX_HOME, False),
        (CLAUDE_HOME, CLAUDE_HOME, False),
        # Shared container-only cache: intentionally global so tool downloads
        # survive short-lived boxes without exposing the host's real ~/.cache.
        (STATE_ROOT / "cache", XDG_CACHE_HOME, False),
        (XDG_CACHE_HOME / "claude-code", XDG_CACHE_HOME / "claude-code", False),
        (XDG_CACHE_HOME / "claude-cli-nodejs", XDG_CACHE_HOME / "claude-cli-nodejs", False),
        # Shared tool caches/state. uv/mise own their concurrency handling;
        # Rust gets standard in-container homes backed by agent-box-private
        # persistent storage rather than the host's real ~/.cargo/~/.rustup.
        (STATE_ROOT / "uv", HOST_HOME / ".local/share/uv", False),
        (STATE_ROOT / "mise", HOST_HOME / ".local/share/mise", False),
        # `mise use -g` records its choice in the global config, not the data
        # directory; an installed tool without that choice is a dead shim. It
        # is the box's own, never the host's: the host's mise may name tools
        # and backends the image does not have.
        (STATE_ROOT / "mise-config", HOST_HOME / ".config/mise", False),
        (STATE_ROOT / "cargo", HOST_HOME / ".cargo", False),
        (STATE_ROOT / "rustup", HOST_HOME / ".rustup", False),
        # Notification hook delivery state is intentionally host-global so
        # duplicate suppression and rate tracking work across agent sessions.
        (NOTIFY_STATE_ROOT, HOST_HOME / ".local/share/agent-box/notify", False),
        (script_dir / "container-launcher", CONTAINER_LAUNCHER, True),
        (script_dir, CONTAINER_LAUNCHER_ROOT / "agent_box", True),
    ]
    if mount_workspace_runtime_state:
        # The keeper needs only this workspace's lifecycle lock and snapshots,
        # never another workspace's writable runtime state.
        pairs.append((runtime_state.root, runtime_state.root, False))
        if config is not None and config.box.k3s.enabled:
            # Read-only over the writable runtime root: the sidecar mounts
            # registries.yaml from here, and the box must not rewrite it.
            pairs.append((k3s_state_dir(runtime_state), k3s_state_dir(runtime_state), True))
    # The container never sees config.json. The legacy disposable argv helper
    # may request a derived two-key credential file; persistent workspace
    # boxes deliberately pass ``mount_notifications=False`` and use per-exec
    # forwarding instead, because a shared bind mount would expose it to every
    # concurrent process in that box.
    if mount_notifications and NOTIFY_CREDENTIALS.is_file():
        pairs.append((NOTIFY_CREDENTIALS, CONTAINER_NOTIFY_CREDENTIALS, True))
    # Only default-home preferences live outside the mounted Claude tree.
    # A relocated home keeps its preferences inside that tree and must not
    # gain access to unrelated default or sibling sidecars.
    if CLAUDE_HOME == DEFAULT_CLAUDE_HOME and DEFAULT_CLAUDE_SIDECAR.is_file():
        pairs.append((DEFAULT_CLAUDE_SIDECAR, DEFAULT_CLAUDE_SIDECAR, False))

    # Both directories are always mounted so git and gh work inside the
    # container. Their source contents are prepared separately at box
    # creation, never while merely calculating an attach spec.
    pairs += [
        (runtime_state.git, HOST_HOME / ".config/git", False),
        (runtime_state.gh, HOST_HOME / ".config/gh", False),
    ]

    pairs += agent_tree_mounts()
    pairs += tracker_mounts(config, runtime_state)
    pairs += registries.mounts(config, runtime_state.root.name)
    pairs += kubeconfigs.mounts(config, runtime_state.root.name)
    pairs += cloud.mounts(config, runtime_state.root.name)
    if config.box.links.enabled:
        root = links.directory(runtime_state.root.name)
        pairs.append((root, root, True))

    out = []
    seen_paths: set[Path] = set()
    for spec in project_mounts:
        seen_paths.add(spec.path)
        flag = f"type=bind,src={spec.path},dst={spec.path}" + (",ro=true" if spec.read_only else "")
        out += ["--mount", flag]
    for src, dst, ro in pairs:
        if dst in seen_paths:
            continue
        seen_paths.add(dst)
        flag = f"type=bind,src={src},dst={dst}" + (",ro=true" if ro else "")
        out += ["--mount", flag]
    return out


def kvm_args(config: Config, enabled: bool = True) -> list[str]:
    """``/dev/kvm`` passthrough, when there is something in the box to use it.

    Tied to the ``virtualization`` tool group: without QEMU in the image the
    device is a bind with no consumer, and handing a container a device it has
    no use for is not a neutral default. ``--no-kvm`` withholds it for a launch.
    """
    if not config.box.tools.groups["virtualization"]:
        return []
    if not enabled or not Path("/dev/kvm").exists():
        return []
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        warn("/dev/kvm exists but current user cannot access it; KVM disabled")
        return []
    args = ["--device=/dev/kvm"]
    try:
        runtime = subprocess.check_output(["podman", "info", "--format", "{{.Host.OCIRuntime.Name}}"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        runtime = ""
    if runtime == "crun":
        args.append("--group-add=keep-groups")
    else:
        warn(f"OCI runtime is {runtime or 'unknown'}; /dev/kvm will be passed through without keep-groups")
    return args


PULSE_DIR = HOST_RUNTIME_DIR / "pulse"


def audio_args(config: Config) -> list[str]:
    """The host's PulseAudio socket, when the audio switch asks for it.

    The directory is mounted rather than the socket file: a restarted sound
    server replaces the socket inode, which a file bind would keep pointing at.
    Read-only, because a client only connects: the kernel exempts sockets from
    the read-only mount check, while the box can no longer unlink or replace
    the host's socket. PipeWire's pulse server and PulseAudio both accept the
    same-uid client that ``--userns=keep-id`` maps back, so no cookie is
    involved. A host without the socket gets no mount; :func:`audio_warnings`
    says so.
    """
    socket = PULSE_DIR / "native"
    if not config.box.audio or not socket.is_socket():
        return []
    return ["--mount", f"type=bind,src={PULSE_DIR},dst={PULSE_DIR},ro=true", f"--env=PULSE_SERVER=unix:{socket}"]


def audio_warnings(config: Config) -> list[str]:
    socket = PULSE_DIR / "native"
    if config.box.audio and not socket.is_socket():
        return [f"Audio is enabled but {socket} is not a sound-server socket; workspace boxes start without sound"]
    return []


def github_enabled(config: Config) -> bool:
    """Whether a GitHub credential may reach the container at all.

    Being logged in to ``gh`` on the host is not consent, and neither is
    having ``GH_TOKEN`` exported in the launching shell -- an agent container
    is a different trust boundary from your interactive shell, and a variable
    that happens to be in the environment is not a decision about it. So the
    config switch gates *both* the host-derived token and any already-exported
    one; ``--[no-]gh-token`` sets that switch for one launch.
    """
    return config.credentials.github.token


def github_env(config: Config) -> list[str]:
    """``--env`` flags carrying a GitHub token, or nothing at all.

    A token read from ``gh auth token`` is placed in this short-lived launch
    process's environment, then forwarded by *name*. Podman inherits that
    environment when it is exec'd, while its argv contains only
    ``--env=GH_TOKEN``. This is deliberately different from putting the token
    after ``=`` in argv, which exposes it through process inspection.
    """
    if not github_enabled(config):
        return []
    existing = [n for n in ("GH_TOKEN", "GITHUB_TOKEN") if os.environ.get(n)]
    if existing:
        # Already in the environment and now permitted: forward by name, so
        # the value never appears in the container's argv.
        return [f"--env={name}" for name in existing]
    if not shutil.which("gh"):
        warn("GitHub token passthrough is enabled but 'gh' is not installed on the host")
        return []
    result = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True)
    token = result.stdout.strip() if result.returncode == 0 else ""
    if not token:
        warn("GitHub token passthrough is enabled but the host has no token; run 'gh auth login'")
        return []
    os.environ["GH_TOKEN"] = token
    return ["--env=GH_TOKEN"]


#: Variables that *are* a provider credential. Off by default: Claude and
#: Codex authenticate through the mounted ~/.claude / ~/.codex directories,
#: so a container normally needs none of these.
PROVIDER_API_KEY_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
#: Endpoint and plumbing settings -- where requests go and how. Without them
#: a container behind a proxy or on a third-party endpoint cannot reach the
#: network at all, so these default on.
PROVIDER_TRANSPORT_ENV = (
    "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "HTTP_PROXY", "HTTPS_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
)


def user_env(config: Config) -> list[str]:
    """``--env`` flags for the variables the user asked agent-box to pass.

    Two spellings, matching the two halves of ``config.box.env``. A forwarded
    name becomes ``--env=NAME``, which tells Podman to inherit the value from
    the client process, so a secret never lands in an argv that ``/proc``
    exposes to other processes on the host; a name with no value in the
    current environment is skipped rather than defined as empty, because an
    empty variable and an absent one mean different things to most programs.
    An explicit assignment becomes ``--env=NAME=VALUE`` and does appear in
    the argv, which is why ``agent-box config`` steers secrets to the other
    list.

    An explicit assignment wins over a forward of the same name: it is the
    more specific statement, and letting the shell silently override a value
    written in the config file would make the file a suggestion.
    """
    assignments = config.box.env.set
    names = [
        name for name in config.box.env.forward
        if name not in assignments and os.environ.get(name)
    ]
    return [
        *(f"--env={name}" for name in names),
        *(f"--env={name}={value}" for name, value in sorted(assignments.items())),
    ]


def provider_env(config: Config) -> list[str]:
    names = (
        *(PROVIDER_API_KEY_ENV if config.agents.host_env.api_keys else ()),
        *(PROVIDER_TRANSPORT_ENV if config.agents.host_env.transport else ()),
    )
    # --env=NAME (not NAME=value) so a secret never lands in the argv, which
    # is world-readable via /proc on most systems.
    return [f"--env={name}" for name in names if os.environ.get(name)]


def k3s_state_dir(runtime_state: WorkspaceRuntimeState) -> Path:
    """The kubeconfig and registries.yaml of this workspace's k3s sidecar."""
    return runtime_state.root / "k3s"


def network_arguments(config, api_port=None):
    errors = networking.problems(config.box.network, config.box.k3s)
    try:
        if errors:
            raise ValueError(errors[0])
        return networking.arguments(config.box.network, api_port)
    except ValueError as exc:
        raise AgentBoxError(str(exc)) from None


def prepare_network(config):
    if config.box.network.host_ports:
        try:
            backend = nested_podman.host_info().get("rootlessNetworkCmd")
        except ValueError as exc:
            raise AgentBoxError(str(exc)) from None
        if backend != "pasta":
            raise AgentBoxError("selected host-service forwarding requires host Podman's pasta backend; "
                                "clear --host-port with --no-host-ports or configure pasta")


def k3s_box_args(
    config: Config, workspace: Workspace, runtime_state: WorkspaceRuntimeState | None,
) -> list[str]:
    """The box's route to its cluster: pasta's loopback forward and the kubeconfig.

    The API is published on host loopback, which Podman's host alias does
    not reach, and publishing it on the LAN would expose a cluster-admin
    endpoint. ``-T`` forwards the box's own 127.0.0.1:PORT to the host's.
    Structural: the port is fixed when the sidecar is created, so a
    recreated sidecar recreates the box too.
    """
    if not config.box.k3s.enabled:
        return network_arguments(config)
    runtime_state = runtime_state or workspace_runtime_state(workspace)
    port = k3s.api_port(workspace_identity(workspace))
    return [*network_arguments(config, int(port)),
            f"--env=KUBECONFIG={k3s.kubeconfig_path(k3s_state_dir(runtime_state))}"]


def prepare_k3s(config: Config, workspace: Workspace, runtime_state: WorkspaceRuntimeState) -> None:
    """Bring this workspace's cluster up, or release one a new box no longer needs.

    Before the box is specified, because the sidecar's published port is
    part of the box's identity; under the transition lock, so two launches
    do not create the sidecar twice.
    """
    with workspace_transition_lock(workspace):
        host_k3s.verify(config, runtime_state.root)
        if config.box.k3s.enabled:
            workspace_id, state_dir = workspace_identity(workspace), k3s_state_dir(runtime_state)
            box = inspect_workspace_container(workspace_container_name(workspace))
            box_running = (box or {}).get("State", {}).get("Status") == "running"
            # Only the creation path calls this: a box found now won the
            # race and owns its cluster policy, even when its hash matches.
            if box_running:
                raise AgentBoxError("a workspace box appeared while this launch was starting; run the command again")
            if not box_running:
                # A box-bound cluster whose box ended on its own is noticed
                # only now, and the new box must not inherit it.
                retained = host_k3s.load(runtime_state.root)
                if retained and not retained["owners"]:
                    host_k3s.cleanup(runtime_state.root, retained)
                else:
                    release_k3s(workspace, config, runtime_state)
            k3s.ensure(workspace_id, config.box.k3s, state_dir, workspace.host_root, host_tmp=config.box.host_tmp)
        else:
            release_k3s(workspace, config, runtime_state)


def release_k3s(workspace: Workspace, config: Config | None = None,
                runtime_state: WorkspaceRuntimeState | None = None) -> None:
    """Stop the sidecar once no box uses it, unless the configuration keeps it.

    ``persistent`` keeps a running cluster between boxes. A cluster the
    configuration no longer asks for, or one bound to its box, stops as soon
    as agent-box sees the box gone. A box that ended on its own is only
    noticed by the next agent-box command for the workspace.
    """
    runtime_state = runtime_state or workspace_runtime_state(workspace)
    state_dir = k3s_state_dir(runtime_state)
    # Creating a sidecar creates this directory and deleting its data removes
    # it: without one no sidecar exists here, and neither the configuration
    # nor Podman need be asked.
    if not state_dir.is_dir():
        return
    retained = host_k3s.load(runtime_state.root)
    if retained and retained["owners"]:
        return
    recorded = (Config.from_dict({"box": retained["box"]}) if retained else
                last_box_config(runtime_state, config))
    if recorded is None:
        return
    settings = recorded.box.k3s
    if config is not None and not config.box.k3s.enabled:
        # Only an explicit new disabled environment withdraws access. An
        # attach/cleanup command's changed defaults cannot erase retained data.
        settings = replace(settings, enabled=False)
    if settings.enabled and settings.lifetime != "box":
        return
    if not settings.enabled:
        # Switching the cluster off is not asking for its data to go.
        settings = replace(settings, persistent_data=True)
    # An exited box, as a reboot leaves one despite --rm, uses no cluster.
    box = inspect_workspace_container(workspace_container_name(workspace))
    if (box or {}).get("State", {}).get("Status") == "running":
        return
    if k3s.stop(workspace_identity(workspace), settings, state_dir):
        print(f"agent-box: stopped the k3s cluster {k3s.name(workspace_identity(workspace))}", file=sys.stderr)


def last_box_config(runtime_state: WorkspaceRuntimeState, current: Config | None = None) -> Config | None:
    """The configuration the workspace's latest box was created with; ``None`` when unreadable.

    Data a box used goes or stays by the policy that box was created with, as
    every session in it did: a setting changed since, or read by a command
    that never joined the box, must not delete what the box was told to keep,
    nor keep what it was told to discard. Without any box record ``current``
    (by default the current configuration) applies.
    """
    config = current or effective_config(runtime_config_base())
    policies = sorted(runtime_state.root.glob("box-*.json"))
    if not policies:
        return config
    try:
        return apply_box_policy(copy.deepcopy(config), read_json(policies[-1]))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, AgentBoxError):
        return None


def release_nested_podman(workspace: Workspace, config: Config | None = None) -> None:
    """Delete disposable nested Podman data once the workspace has no box.

    ``config`` is the gone box's own; without it the box's record decides,
    and an unreadable record keeps the data. An exited box that is still
    present keeps its volume mounted; the next box's creation decides for it.
    """
    config = config or last_box_config(workspace_runtime_state(workspace))
    if config is None or config.box.podman.persistent_data:
        return
    workspace_id = workspace_identity(workspace)
    # The volume first: most workspaces have none, and then Podman is asked once.
    if _nested_podman_volume_owner(workspace_id) is None:
        return
    if inspect_workspace_container(workspace_container_name(workspace)) is not None:
        return
    if remove_nested_podman_volume(workspace_id):
        print(f"agent-box: deleted the nested Podman data {nested_podman_volume(workspace_id)}", file=sys.stderr)


def confirm_k3s(spec: WorkspaceContainerSpec) -> None:
    """Refuse to start a box whose cluster moved after this launch prepared it.

    The sidecar is prepared under one hold of the transition lock and the box
    is created under a later one, so an ``agent-box stop`` in between would
    leave a box forwarding to no cluster. Checked under the creation lock
    instead of holding one lock from preparation to creation, which would
    put the whole launch path, session prompts included, inside it.
    """
    config = spec.creation_config
    if not config.box.k3s.enabled:
        return
    sidecar = k3s.inspect(spec.workspace_id)
    running = (sidecar or {}).get("State", {}).get("Status") == "running"
    expected = network_arguments(config, int(k3s.api_port(spec.workspace_id))) if running else []
    if not running or any(arg not in spec.create_argv for arg in expected):
        raise AgentBoxError("the k3s cluster was stopped or recreated while this launch was starting; "
                            "run the command again")


def inside_box() -> bool:
    """Podman marks every container it starts; agent-box runs in one only as a nested launch."""
    return Path("/run/.containerenv").is_file()


#: Host-side capabilities a box started inside a workspace box does without.
NESTED_WITHHELD = {
    "k3s": "box.k3s is on, but a box started inside a workspace box gets no cluster of its own; "
           "the outer box has it",
    "links": "box.links is on, but a box started inside a workspace box gets no host link broker; "
             "its links are only displayed",
}


def withhold_from_nested_box(config: Config) -> list[str]:
    """A box started inside a workspace box gets no k3s cluster and no link broker.

    The session snapshot carries the outer box's settings, but both live on
    the host. A sidecar cannot start in nested Podman (crun may not mount
    sysfs there), and a cluster inside the box was rejected when k3s was
    designed; sharing the outer one would need pasta, which the image lacks.
    A nested broker would run inside the outer box, with no host desktop, and
    its callback listeners would sit on the outer box's loopback, not the
    workstation's. Turned off in the snapshot layer and in the inherited box
    policy too, which the snapshot carries and a nested session reapplies.
    """
    if not inside_box():
        return []
    warnings = [message for name, message in NESTED_WITHHELD.items() if getattr(config.box, name).enabled]
    # Every layer, even where a project switched one off: the snapshot
    # renders from the layer beneath the project, and replays the session's
    # launch flags over the box policy.
    for layer in (config, getattr(config, "_project_base", config)):
        policy = getattr(layer, "_box_policy", None)
        for name in NESTED_WITHHELD:
            setattr(layer.box, name, replace(getattr(layer.box, name), enabled=False))
            if policy is not None:
                policy["box"][name]["enabled"] = False
        for name in NESTED_WITHHELD:
            if getattr(layer, "_launch", {}).get(name):
                layer._launch[name] = False
    return warnings


def nested_launch_options(options):
    """Host-only capabilities are withheld before nested box admission.

    Turned off before a running nested box is admitted, whose policy already
    records these capabilities off and would otherwise refuse the flag as a conflict.
    """
    withheld = [name for name in NESTED_WITHHELD if options.overrides.get(name)] if inside_box() else []
    if not withheld:
        return options, []
    return (replace(options, overrides={**options.overrides, **dict.fromkeys(withheld, False)}),
            [NESTED_WITHHELD[name] for name in withheld])


def k3s_warnings(config: Config) -> list[str]:
    if config.box.k3s.enabled and not config.box.tools.groups["kubernetes"]:
        return ["box.k3s is on but the kubernetes tool group is off: the box has no kubectl or helm"]
    return []


def tool_warnings(config: Config) -> list[str]:
    """Forwarded configuration whose tool group is off; one built but missing is the image drift warning's.

    The menu ticks the group when forwarding is turned on, so this is what
    ``config set`` or a one-session flag left behind.
    """
    return [f"{needer} need the {group} tool group, which is off: turn it on in agent-box config "
            f"(Workspace box > Image tools) and run 'agent-box build'"
            for group, needer in config.credentials.needed_tools().items() if not config.box.tools.groups[group]]


def container_lifetime_args(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    config: Config | None = None, mount_notifications: bool = False,
    runtime_state: WorkspaceRuntimeState | None = None, kvm: bool = True,
) -> list[str]:
    """Container-level arguments shared by workspace creation and legacy argv tests.

    ``HOST_HOME`` is the passwd-derived home (``paths.passwd_home``), the
    same value ``agent_box.identity`` builds the image's user with and the
    same value ``build_mounts`` binds host state from. ``$HOME`` is never
    consulted, so a poisoned ``$HOME`` cannot move the container's home,
    PATH, or any bind mount.
    """
    config = effective_config(runtime_config_base()) if config is None else config
    home = HOST_HOME
    args = [
        "--userns=keep-id", *kvm_args(config, kvm), *audio_args(config),
        "--cap-drop=all", "--security-opt=no-new-privileges", "--hostname=agent-box", "--tz=local",
        *nested_podman_args(config, workspace),
        f"--env=HOME={home}",
        f"--env=PATH={CONTAINER_RUNTIME_PATH}",
        f"--env=CODEX_HOME={CODEX_HOME}",
        f"--env=GIT_CONFIG_GLOBAL={home}/.config/git/config",
        f"--env=GH_CONFIG_DIR={home}/.config/gh",
        *registries.container_env(config),
        *kubeconfigs.container_env(config),
        f"--env=XDG_CACHE_HOME={XDG_CACHE_HOME}",
        f"--env=UV_CACHE_DIR={XDG_CACHE_HOME}/uv",
        # The official Telegram channel plugin runs its MCP server under Bun
        # and installs its dependencies on first start; keep that download in
        # the container-only cache so it survives across disposable runs.
        f"--env=BUN_INSTALL_CACHE_DIR={XDG_CACHE_HOME}/bun",
        f"--env=MISE_DATA_DIR={home}/.local/share/mise",
        # `mise trust` records live in the state directory; in the box's own
        # home they died with it and every project tool shim failed as
        # untrusted. Inside the persistent data mount, because a mount at
        # ~/.local/state/mise would have Podman create ~/.local/state
        # root-owned on any image without it, locking every other tool out.
        f"--env=MISE_STATE_DIR={home}/.local/share/mise/state",
        "--env=DISABLE_AUTOUPDATER=1",
        # Notification credentials are deliberately *not* a container mount.
        # A workspace box is shared by shell/Claude/Codex, while notification
        # access is an agent-invocation capability.  The hooks receive their
        # two credentials through podman exec instead (see notification_env).
        *build_mounts(workspace, extra_mounts, script_dir, config, mount_notifications, runtime_state),
        # The Vikunja server URL is not passed as an environment variable: it
        # is rendered into the credential directory mounted just above, so
        # the address and the token have one source instead of two that can
        # disagree. Only the host mapping is structural, because it is a
        # container-creation flag rather than file content.
        *tracker_host_args(config),
        *k3s_box_args(config, workspace, runtime_state),
    ]
    if config.box.links.enabled:
        link_state = runtime_state or legacy_runtime_state(workspace)
        policy = hashlib.sha256(json.dumps(config.box.links.to_dict(), sort_keys=True).encode()).hexdigest()[:16]
        args += [f"--env={links.SOCKET_ENV}={links.endpoint(link_state.root.name)}",
                 f"--env=AGENT_BOX_LINKS_POLICY={policy}"]
    # A host that relocates Claude Code's config directory must have the
    # container agree: CLAUDE_HOME is already bind-mounted at its host path,
    # but `claude` inside the container would still look in $HOME/.claude
    # without this. Forwarded as the resolved value (not the raw variable) so
    # a tilde or relative setting means the same thing on both sides.
    if os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
        args.append(f"--env=CLAUDE_CONFIG_DIR={CLAUDE_HOME}")
    return args


def base_podman_args(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    config: Config | None = None, mount_notifications: bool = True,
) -> list[str]:
    """Legacy disposable-run argv builder retained for compatibility tests.

    New launches use :func:`ensure_workspace_container` and ``podman exec``.
    Keeping this helper preserves a single source for visibility/hardening and
    avoids changing callers that only inspect the old command construction.
    """
    config = effective_config(runtime_config_base()) if config is None else config
    runtime_state = legacy_runtime_state(workspace)
    prepare_container_lifetime_state(
        config, mount_notifications=mount_notifications, runtime_state=runtime_state,
    )
    args = ["run", "--rm", "--interactive", f"--workdir={workspace.container_cwd}"]
    args += container_lifetime_args(
        workspace, extra_mounts, script_dir, config, mount_notifications,
    )
    args += github_env(config)
    args += provider_env(config)
    return args


def container_argv(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    command: list[str], config: Config | None = None, extra_env: list[str] | None = None,
    mount_notifications: bool = True,
) -> list[str]:
    """Full ``["podman", ...]`` argv for one disposable container run."""
    args = base_podman_args(workspace, extra_mounts, script_dir, config, mount_notifications)
    args += [f"--env={pair}" for pair in (extra_env or [])]
    if sys.stdin.isatty() and sys.stdout.isatty():
        args.append("--tty")
    return ["podman", *args, IMAGE, *command]


# -- workspace container lifecycle -----------------------------------------

def workspace_identity(workspace: Workspace) -> str:
    """Stable opaque identity for a resolved workspace root."""
    return hashlib.sha256(os.fsencode(workspace.host_root.resolve())).hexdigest()[:16]


def workspace_container_name(workspace: Workspace) -> str:
    """A readable, Podman-safe name that cannot collide on a basename alone."""
    readable = re.sub(r"[^a-z0-9_.-]+", "-", workspace.host_root.name.lower()).strip(".-")
    readable = readable or "workspace"
    return f"agent-box-{readable[:36]}-{workspace_identity(workspace)}"


@dataclass(frozen=True)
class WorkspaceContainerSpec:
    name: str
    workspace_id: str
    base_config_hash: str
    extra_mounts_hash: str
    mounts_explicit: bool
    create_argv: list[str]
    creation_config: Config = field(repr=False, compare=False)
    runtime_state: WorkspaceRuntimeState
    policy_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    admitted_id: str = ""
    image: str = ""
    workspace_path: str = ""
    browser_mounts: list[MountSpec] = field(default_factory=list)


def explicit_mounts_hash(mounts):
    return hashlib.sha256(json.dumps([
        (str(mount.path), mount.read_only) for mount in plan_mounts(mounts)
    ], separators=(",", ":")).encode()).hexdigest()[:16]


def workspace_container_spec(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path, config: Config,
    kvm: bool = True, *, image: str = "",
) -> WorkspaceContainerSpec:
    """The immutable box definition; deliberately excludes per-exec secrets/policy."""
    image = image or IMAGE
    runtime_state = workspace_runtime_state(workspace)
    lifetime = container_lifetime_args(
        workspace, extra_mounts, script_dir, config, mount_notifications=False, runtime_state=runtime_state, kvm=kvm,
    )
    base_lifetime = container_lifetime_args(
        workspace, [], script_dir, config, mount_notifications=False, runtime_state=runtime_state, kvm=kvm,
    )
    extra_mounts_hash = explicit_mounts_hash(extra_mounts)
    # Extra binds are the only structural setting that an omitted later
    # --mount may deliberately inherit. Derive the base identity from an
    # independently rendered no-extra box rather than subtracting flags from
    # the flattened argv: an explicit parent mount can subsume the project
    # mount during Podman mount planning.
    base_structural = json.dumps({
        "schema": WORKSPACE_CONTAINER_SCHEMA,
        "workspace": str(workspace.host_root.resolve()),
        "image": image,
        "args": base_lifetime,
    }, separators=(",", ":"), sort_keys=True).encode()
    base_config_hash = hashlib.sha256(base_structural).hexdigest()[:16]
    name = workspace_container_name(workspace)
    policy_id = uuid.uuid4().hex
    labels = [
        f"--label={WORKSPACE_CONTAINER_LABEL}=true",
        f"--label={WORKSPACE_RUNTIME_LABEL}={runtime_bundle.identity(script_dir)}",
        f"--label={WORKSPACE_CONTAINER_WORKSPACE_LABEL}={workspace_identity(workspace)}",
        f"--label={WORKSPACE_PATH_LABEL}={workspace.host_root.resolve()}",
        f"--label={WORKSPACE_CONTAINER_SCHEMA_LABEL}={WORKSPACE_CONTAINER_SCHEMA}",
        f"--label={WORKSPACE_CONTAINER_BASE_CONFIG_LABEL}={base_config_hash}",
        f"--label={WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL}={extra_mounts_hash}",
        f"--label={WORKSPACE_CONTAINER_POLICY_LABEL}={policy_id}",
    ]
    return WorkspaceContainerSpec(
        name=name,
        workspace_id=workspace_identity(workspace),
        base_config_hash=base_config_hash,
        extra_mounts_hash=extra_mounts_hash,
        mounts_explicit=bool(extra_mounts),
        browser_mounts=plan_mounts(extra_mounts),
        create_argv=[
            "podman", "create", "--name", name, "--rm", "--init", *labels, *lifetime,
            image, "bash", str(KEEPER_PATH), str(workspace_lock_path(workspace)),
        ],
        creation_config=config,
        runtime_state=runtime_state,
        policy_id=policy_id,
        image=image,
        workspace_path=str(workspace.host_root.resolve()),
    )


@contextmanager
def workspace_transition_lock(workspace: Workspace):
    """Short advisory lock for create/exec/idle-delete transition windows.

    The keeper and host flock separate open file descriptions of the same
    lock inode.  An accidental unlink does not release either already-held
    flock, although a trusted workspace peer that replaces the pathname can defeat future lock
    acquisition.  Workspace-box processes are trusted peers; preserving the
    simple shared lock is preferable to adding a second lifecycle mechanism.
    """
    with transition_lock_at(workspace_lock_path(workspace)):
        yield


@contextmanager
def transition_lock_at(lock_path: Path, *, shared: bool = False):
    """The transition lock by its file, for a workspace known only by its id."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


WORKSPACE_LOCK_FILE = "lifecycle.lock"


def workspace_lock_path(workspace: Workspace) -> Path:
    return workspace_runtime_state(workspace).root / WORKSPACE_LOCK_FILE


def workspace_box_policy(spec: WorkspaceContainerSpec) -> dict:
    """Persist only shared choices, never an agent's workflow or credential values."""
    config = spec.creation_config
    groups = image_tool_groups()
    tools = config.box.tools.to_dict() if groups is None else {
        name: name in groups for name in config.box.tools.groups
    }
    tools["podman"] = tools["podman"] and "--device=/dev/net/tun" in spec.create_argv
    source, destination, read_only = tracker_mounts(config, spec.runtime_state)[0]
    return {
        "box": {"tools": tools, "k3s": config.box.k3s.to_dict(),
                "network": config.box.network.to_dict(),
                # Whichever session leaves last deletes or keeps the data by this.
                "podman": config.box.podman.to_dict(),
                "audio": any(arg.startswith("--env=PULSE_SERVER=") for arg in spec.create_argv),
                "host_tmp": config.box.host_tmp,
                "links": config.box.links.to_dict()},
        "credentials": {"git": config.credentials.git.to_dict(),
                        "github": {"config": config.credentials.github.config},
                        "registries": config.credentials.registries.to_dict(),
                        "kubernetes": config.credentials.kubernetes.to_dict(),
                        "cloud": config.credentials.cloud.to_dict()},
        "workflow_mount": config.tracker.enabled or config.workflow.enabled,
        "session_mount": [str(source), str(destination), read_only],
        "browser_mounts": [[str(m.path), m.read_only] for m in spec.browser_mounts],
    }


def existing_workspace_spec(workspace: Workspace, options: LaunchOptions, package: Path = PACKAGE_ROOT) -> WorkspaceContainerSpec | None:
    """Read a live box under the keeper lock; recheck its ID when admitting exec."""
    name = workspace_container_name(workspace)
    state = workspace_runtime_state(workspace)
    with workspace_transition_lock(workspace):
        current = inspect_workspace_container(name)
        if current is None:
            return None
        labels = current.get("Config", {}).get("Labels", {}) or {}
        owned = (labels.get(WORKSPACE_CONTAINER_LABEL) == "true"
                 and labels.get(WORKSPACE_CONTAINER_WORKSPACE_LABEL) == workspace_identity(workspace))
        if not owned:
            raise AgentBoxError("workspace container name is occupied by a foreign container; resolve the name collision first")
        current = _wait_for_exiting_keeper(name, current)
        if current is None or current.get("State", {}).get("Status") != "running":
            return None
        if labels.get(WORKSPACE_RUNTIME_LABEL) != runtime_bundle.identity(package):
            raise AgentBoxError("running workspace box uses a different agent-box runtime; end it with agent-box stop and rerun")
        policy_id = labels.get(WORKSPACE_CONTAINER_POLICY_LABEL, "")
        if labels.get(WORKSPACE_CONTAINER_SCHEMA_LABEL) != WORKSPACE_CONTAINER_SCHEMA or not re.fullmatch(r"[0-9a-f]{32}", policy_id):
            raise AgentBoxError("running workspace box has no current creation policy; end it with agent-box stop and rerun")
        try:
            policy = read_json(state.root / f"box-{policy_id}.json")
            shared = apply_box_policy(Config(), policy)
            source, destination, read_only = policy["session_mount"]
            if not isinstance(source, str) or not isinstance(destination, str) or not isinstance(read_only, bool):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise AgentBoxError("cannot read the running workspace box's creation policy; end it with agent-box stop and rerun") from None
        groups = image_tool_groups(current["Image"])
        if groups is not None:
            policy["box"]["tools"] = {name: name in groups for name in shared.box.tools.groups}
            policy["box"]["tools"]["podman"] = "podman" in groups and any(
                device.get("PathOnHost") == "/dev/net/tun"
                for device in current.get("HostConfig", {}).get("Devices", []) or []
            )
            apply_box_policy(shared, policy)
        if not any(item.get("Source") == source and item.get("Destination") == destination
                   for item in current.get("Mounts", [])):
            raise AgentBoxError("running workspace box lacks its session snapshot mount; end it with agent-box stop and rerun")
        if shared.box.k3s.enabled:
            sidecar = k3s.inspect(workspace_identity(workspace))
            if (sidecar or {}).get("State", {}).get("Status") != "running":
                raise AgentBoxError("running workspace box's k3s sidecar is unavailable; end it with agent-box stop and rerun")
        desired = effective_config(runtime_config_base())
        desired_network = requested_network(desired.box.network, options)
        try:
            matches = networking.identity(desired_network) == networking.identity(shared.box.network)
            if shared.box.k3s.enabled and sorted(networking.ports(desired.box.k3s.ports)) != sorted(networking.ports(shared.box.k3s.ports)):
                raise AgentBoxError("requested k3s published ports differ from the running workspace; "
                                    "end it with agent-box stop and rerun")
        except ValueError as exc:
            raise AgentBoxError(f"invalid workspace network policy: {exc}") from None
        if not matches:
            raise AgentBoxError("requested network mode/ports differ from the running workspace "
                                f"(running {shared.box.network.to_dict()}, requested {desired_network.to_dict()}); "
                                "repeat its explicit network options or "
                                "end it with agent-box stop and rerun")
        api_port = int(k3s.api_port(workspace_identity(workspace))) if shared.box.k3s.enabled else None
        problem = networking.inspection_problem(shared.box.network, current, api_port)
        if problem:
            raise AgentBoxError(f"running workspace network: {problem}; end it with agent-box stop and rerun")
        shared_flags = {"registries": shared.credentials.registries.enabled,
                        "k3s": shared.box.k3s.enabled,
                        "kubeconfigs": shared.credentials.kubernetes.enabled,
                        "cloud-profiles": shared.credentials.cloud.enabled,
                        "links": shared.box.links.enabled,
                        "host-tmp": shared.box.host_tmp,
                        "git-config": shared.credentials.git.full_config,
                        "gh-config": shared.credentials.github.config}
        for key, actual in shared_flags.items():
            if key in options.overrides and options.overrides[key] != actual:
                flag = f"--{key}" if options.overrides[key] else f"--no-{key}"
                raise AgentBoxError(f"{flag} conflicts with the running workspace box's shared {key} access; "
                                    "end it with agent-box stop and rerun with that option")
        if not options.kvm and any(device.get("PathOnHost") == "/dev/kvm"
                                   for device in current.get("HostConfig", {}).get("Devices", []) or []):
            raise AgentBoxError("--no-kvm conflicts with the running workspace box's /dev/kvm device; "
                                "end it with agent-box stop and rerun with --no-kvm")
        if options.mounts:
            from .mounts import parse_mount_arg
            requested = plan_mounts([parse_mount_arg(mount) for mount in options.mounts])
            for mount in requested:
                covering = [item for item in current.get("Mounts", [])
                            if mount.path.is_relative_to(Path(item["Destination"]))]
                actual = max(covering, key=lambda item: len(Path(item["Destination"]).parts), default=None)
                if (actual is None or bool(actual.get("RW")) == mount.read_only
                        or Path(actual["Source"]) / mount.path.relative_to(actual["Destination"]) != mount.path):
                    raise AgentBoxError(f"--mount {mount.path} conflicts with the running workspace box's mounts; "
                                        "end it with agent-box stop and rerun with the requested mount")
        spec = WorkspaceContainerSpec(
            name, workspace_identity(workspace), labels.get(WORKSPACE_CONTAINER_BASE_CONFIG_LABEL, ""),
            labels.get(WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL, ""), bool(options.mounts), [], shared, state,
            policy_id, current["Id"], current["Image"],
            browser_mounts=verified_browser_mounts(policy, current),
        )
        return spec


def verified_browser_mounts(policy, container):
    """Saved sharing choices need a matching actual bind before reaching the sibling."""
    selected = []
    for path, read_only in policy.get("browser_mounts", []):
        target = Path(path)
        if not target.is_absolute() or type(read_only) is not bool:
            raise AgentBoxError("invalid saved browser mount; stop and relaunch the workspace")
        selected.append(MountSpec(target, read_only))
    for mount in [*selected, *host_tmp_mounts(Config.from_dict(policy).box.host_tmp)]:
        target, read_only = mount.path, mount.read_only
        covering = [item for item in container.get("Mounts", [])
                    if target.is_relative_to(Path(item["Destination"]))]
        actual = max(covering, key=lambda item: len(Path(item["Destination"]).parts), default=None)
        if (actual is None or bool(actual.get("RW")) == read_only
                or Path(actual["Source"]) / target.relative_to(actual["Destination"]) != target):
            raise AgentBoxError("saved browser mounts differ from the workspace; stop and relaunch it")
    expected = container.get("Config", {}).get("Labels", {}).get(WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL)
    if explicit_mounts_hash(selected) != expected:
        raise AgentBoxError("saved browser mounts differ from the explicit workspace mounts; stop and relaunch it")
    return selected


def workspace_container_exists(name: str) -> bool:
    """Return a definite Podman container-exists answer, or fail closed.

    ``podman container exists`` reserves exit status 1 for a missing
    container and 125 for a storage/runtime failure.  Only the former can
    authorize replacing this workspace's Git/GH bind-source roots.
    """
    command = ["podman", "container", "exists", name]
    try:
        result = subprocess.run(command, text=True, capture_output=True)
    except OSError as exc:
        raise AgentBoxError(
            f"could not determine workspace container state: {' '.join(command)} failed: {exc}"
        ) from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic from podman"
    raise AgentBoxError(
        f"could not determine workspace container state: {' '.join(command)} "
        f"failed with exit {result.returncode}: {detail}"
    )


def inspect_workspace_container(name: str) -> dict | None:
    """Inspect a box, returning ``None`` only after confirmed absence.

    An inspect can race a container's normal removal.  Rechecking explicit
    existence makes that race safe; every other inspect failure is ambiguous
    and therefore raises instead of looking like a missing container.
    """
    if not workspace_container_exists(name):
        return None
    command = ["podman", "container", "inspect", name]
    try:
        result = subprocess.run(command, text=True, capture_output=True)
    except OSError as exc:
        raise AgentBoxError(
            f"could not inspect workspace container: {' '.join(command)} failed: {exc}"
        ) from exc
    if result.returncode:
        # The keeper may have just exited and Podman may have removed the box
        # between the explicit exists and inspect calls.  Re-evaluate only
        # that narrow race; a still-existing box means inspect was ambiguous.
        if not workspace_container_exists(name):
            return None
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic from podman"
        raise AgentBoxError(
            f"could not inspect workspace container: {' '.join(command)} "
            f"failed with exit {result.returncode}: {detail}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AgentBoxError("could not inspect workspace container: invalid JSON from podman") from exc
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    raise AgentBoxError("could not inspect workspace container: unexpected response from podman")


def remove_workspace_container_then_confirm_absent(name: str) -> None:
    """Remove a stale box and establish that its bind mounts are no longer live."""
    workspace_id = name.rsplit("-", 1)[-1]
    if re.fullmatch(r"[0-9a-f]{16}", workspace_id) and (workspace_state_root(workspace_id) / "browser").exists():
        browser.stop(workspace_id, "workspace")
    command = ["podman", "rm", "-f", name]
    try:
        result = subprocess.run(command, text=True, capture_output=True)
    except OSError as exc:
        raise AgentBoxError(f"could not remove workspace container: {' '.join(command)} failed: {exc}") from exc
    current = inspect_workspace_container(name)
    if current is None:
        return
    detail = (
        (result.stderr.strip() or result.stdout.strip() or "no diagnostic from podman")
        if result.returncode else "container still exists after removal"
    )
    raise AgentBoxError(f"could not remove workspace container: {' '.join(command)} failed: {detail}")


def _container_matches(container: dict, spec: WorkspaceContainerSpec) -> bool:
    labels = container.get("Config", {}).get("Labels", {})
    return isinstance(labels, dict) and (
        labels.get(WORKSPACE_CONTAINER_LABEL) == "true"
        and labels.get(WORKSPACE_CONTAINER_WORKSPACE_LABEL) == spec.workspace_id
        and labels.get(WORKSPACE_CONTAINER_SCHEMA_LABEL) == WORKSPACE_CONTAINER_SCHEMA
        and labels.get(WORKSPACE_CONTAINER_BASE_CONFIG_LABEL) == spec.base_config_hash
    )


def _container_belongs_to_workspace(container: dict, spec: WorkspaceContainerSpec) -> bool:
    labels = container.get("Config", {}).get("Labels", {})
    return isinstance(labels, dict) and (
        labels.get(WORKSPACE_CONTAINER_LABEL) == "true"
        and labels.get(WORKSPACE_CONTAINER_WORKSPACE_LABEL) == spec.workspace_id
        and labels.get(WORKSPACE_CONTAINER_SCHEMA_LABEL) == WORKSPACE_CONTAINER_SCHEMA
    )


def _container_is_managed_workspace(container: dict, spec: WorkspaceContainerSpec) -> bool:
    labels = container.get("Config", {}).get("Labels", {})
    return isinstance(labels, dict) and (
        labels.get(WORKSPACE_CONTAINER_LABEL) == "true"
        and labels.get(WORKSPACE_CONTAINER_WORKSPACE_LABEL) == spec.workspace_id
    )


def _container_mounts_match(container: dict, spec: WorkspaceContainerSpec) -> bool:
    labels = container.get("Config", {}).get("Labels", {})
    return isinstance(labels, dict) and labels.get(WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL) == spec.extra_mounts_hash


def _confirm_creation_policy(container: dict, spec: WorkspaceContainerSpec) -> None:
    """Equal mounts cannot authorize refreshing another launch's credential selection."""
    if container.get("Config", {}).get("Labels", {}).get(WORKSPACE_CONTAINER_POLICY_LABEL) != spec.policy_id:
        raise AgentBoxError("a workspace box appeared while this launch was starting; run the command again")


def ensure_workspace_container(spec: WorkspaceContainerSpec) -> None:
    """Create/start a compatible workspace box, recovering stopped stale boxes."""
    current = inspect_workspace_container(spec.name)
    if current is not None:
        current = _wait_for_exiting_keeper(spec.name, current)
    if spec.admitted_id:
        if (current is None or current.get("Id") != spec.admitted_id
                or current.get("State", {}).get("Status") != "running"
                or not _container_belongs_to_workspace(current, spec)):
            raise AgentBoxError("the workspace box ended or changed while this launch was starting; run the command again")
        return
    if current is not None:
        if not _container_belongs_to_workspace(current, spec):
            if (
                _container_is_managed_workspace(current, spec)
                and current.get("State", {}).get("Status") != "running"
            ):
                remove_workspace_container_then_confirm_absent(spec.name)
                current = None
            else:
                raise AgentBoxError("workspace container name is occupied by an incompatible container")
        if current is None:
            pass
        elif not _container_matches(current, spec):
            state = str(current.get("State", {}).get("Status", "unknown"))
            if state == "running":
                raise AgentBoxError(
                    "workspace container is already running with incompatible container-level configuration"
                )
            remove_workspace_container_then_confirm_absent(spec.name)
            current = None
        elif not _container_mounts_match(current, spec):
            state = str(current.get("State", {}).get("Status", "unknown"))
            # Omitting --mount means join an existing box, even if it was
            # originally created with extra paths. Explicit mounts are exact.
            if state == "running" and spec.mounts_explicit:
                raise AgentBoxError("workspace container is already running with different mount configuration")
            if state == "running":
                _confirm_creation_policy(current, spec)
                return
            remove_workspace_container_then_confirm_absent(spec.name)
            current = None
        elif current.get("State", {}).get("Status") == "running":
            _confirm_creation_policy(current, spec)
            return
        else:
            # A stopped container can hold stale state from a crash or an old
            # image. Recreate rather than guessing whether it is safe to resume.
            remove_workspace_container_then_confirm_absent(spec.name)
            current = None
    if current is None:
        # ``inspect_workspace_container`` returns None only after Podman's
        # explicit exists query confirmed absence (including after removal).
        # This is the safety boundary: do not reseed live Git/GH bind roots.
        # The gone box's record is replaced below, so its disposal policy is
        # applied first; a disposable new box starts empty as well.
        previous = last_box_config(spec.runtime_state, spec.creation_config)
        if previous is not None and not (previous.box.podman.persistent_data
                                         and spec.creation_config.box.podman.persistent_data):
            remove_nested_podman_volume(spec.workspace_id)
        prepare_container_lifetime_state(
            spec.creation_config, mount_notifications=False, runtime_state=spec.runtime_state,
        )
        spec.runtime_state.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        spec.runtime_state.root.chmod(0o700)
        for old in spec.runtime_state.root.glob("box-*.json"):
            old.unlink()
        migrations.write(spec.runtime_state.root / f"box-{spec.policy_id}.json",
                         workspace_box_policy(spec), migrations.BOX_POLICY)
        record_workspace_path(spec.workspace_id, spec.workspace_path)
        if spec.creation_config.box.tools.groups["podman"]:
            ensure_nested_podman_volume(spec.workspace_id, spec.workspace_path)
        created = subprocess.run(spec.create_argv, text=True, capture_output=True)
        if created.returncode:
            # Podman's name uniqueness is the final arbiter when two first
            # launches race (including across different agent-box versions).
            winner = inspect_workspace_container(spec.name)
            if winner is None or not _container_matches(winner, spec):
                detail = created.stderr.strip() or "podman could not create workspace container"
                raise AgentBoxError(detail)
            _confirm_creation_policy(winner, spec)
            if winner.get("State", {}).get("Status") == "running":
                return
    started = subprocess.run(["podman", "start", spec.name], text=True, capture_output=True)
    if started.returncode:
        detail = started.stderr.strip() or "podman could not start workspace container"
        raise AgentBoxError(detail)


def exec_argv(name: str, workspace: Workspace, command: list[str], extra_env: list[str]) -> list[str]:
    args = ["podman", "exec", "--interactive", f"--workdir={workspace.container_cwd}"]
    if sys.stdin.isatty() and sys.stdout.isatty():
        args.append("--tty")
    args += [f"--env={pair}" for pair in extra_env]
    return [*args, name, *command]


# -- detachable sessions ---------------------------------------------------
#
# A `podman exec` session cannot be re-attached: when the client dies (a
# closed terminal, a dropped ssh connection) the process inside the box keeps
# running with nobody on the other end of its pty, and Podman offers no way
# back in -- `podman attach` only ever reaches the container's main process,
# which here is the keeper. So agent-box runs interactive agent sessions
# under a tmux server *inside* the box. The exec client then owns nothing but
# a tmux client: losing it detaches the session instead of orphaning it.
#
# A detached session is still a workload for the keeper, so its box stays up
# until the session ends or `agent-box stop` removes it.


def tmux_enabled(agent: str, allowed: bool = True) -> bool:
    """Whether this launch gets a detachable session.

    Only a real terminal is wrapped.  A piped or captured run (``claude -p``,
    CI, a redirected ``agent-box shell -c``) must keep writing straight to the
    caller's stdout, where that output is the entire point, and has no
    terminal to be detached from in the first place. ``--no-tmux`` clears
    ``allowed``.
    """
    if agent not in TMUX_AGENTS or not allowed:
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


@dataclass(frozen=True)
class TmuxSession:
    """One launch's detachable session, and the file it reports its status in.

    Every launch gets its own session: two ``agent-box claude`` commands in
    one workspace remain two independent Claude sessions, exactly as they
    were before tmux existed here.
    """

    name: str
    status_path: Path

    def wrap(self, command: list[str], *, host_root: Path | None = None,
             cwd: Path | None = None, shell: str = "bash") -> list[str]:
        # tmux reports its own exit status, not the wrapped program's, and
        # returns 0 for a detach as well as for a finished session. The inner
        # shell records the real status in the workspace's runtime state --
        # which is bind-mounted at the same path on both sides -- so a
        # launcher that is still alive can exit with it. Its absence is how
        # `finish` tells a detached session from a finished one.
        recorder = 'status=$1; shift; "$@"; printf %s "$?" >"$status"'
        return tmux_argv(
            "new-session", "-s", self.name, *(["-c", str(cwd)] if cwd is not None else []), "--",
            shell, "-c", recorder, "agent-box-session", str(self.status_path), *command,
            host_root=host_root,
        )

    def recorded_status(self) -> int | None:
        try:
            recorded = self.status_path.read_text().strip()
        except OSError:
            return None
        finally:
            self.status_path.unlink(missing_ok=True)
        return int(recorded) if recorded.isdigit() else None

    def finish(self, container: str | None, rc: int, *, host_root: Path | None = None) -> int:
        """Translate the tmux client's exit into the launch's own exit."""
        recorded = self.recorded_status()
        if recorded is not None:
            if recorded:
                # The pane dies with its session and the terminal is restored,
                # so a command that fails before anyone can read it -- an
                # agent-box error raised before the CLI starts included --
                # leaves an exit code and nothing else on screen.
                print(
                    f"agent-box: session '{self.name}' exited with status {recorded}; what it "
                    "printed went with the terminal -- rerun with --no-tmux to see it",
                    file=sys.stderr,
                )
            return recorded
        if rc in {126, 127}:
            warn("tmux could not start on the host" if host_root is not None else
                 "this workspace image has no tmux; run 'agent-box build'")
            return rc
        exists = (tmux_session_exists(container, self.name) if host_root is None else
                  tmux_session_exists(container, self.name, host_root=host_root))
        if exists:
            location = "on the host" if host_root is not None else "in this workspace's box"
            attach = "agent-box --host attach" if host_root is not None else "agent-box attach"
            print(
                f"agent-box: session '{self.name}' is still running {location}; "
                f"return to it with '{attach}'",
                file=sys.stderr,
            )
        return rc


def tmux_session(agent: str, runtime_state: WorkspaceRuntimeState) -> TmuxSession:
    sessions = runtime_state.root / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    # The launcher's PID is unique among the live launches of one workspace,
    # which is all the uniqueness a session name needs.
    name = f"{agent}-{os.getpid()}"
    return TmuxSession(name=name, status_path=sessions / f"{name}.status")


def announce_existing_sessions(container: str) -> None:
    """Say what this workspace's box already holds before adding to it.

    A closed terminal leaves no trace on the host, so without this a second
    launch silently starts a second agent beside the one still running there.
    """
    try:
        if inspect_workspace_container(container) is None:
            return
        existing = tmux_sessions(container)
    except AgentBoxError:
        return
    if existing:
        print(
            f"agent-box: this workspace's box already holds {len(existing)} session(s) "
            f"({', '.join(existing)}); 'agent-box attach' returns to one",
            file=sys.stderr,
        )


def _is_podman_pause(args: str) -> bool:
    """Whether a process is rootless Podman's pause process, ``catatonit -P``.

    One nested ``podman`` call starts it to hold the user namespace, and it
    never exits by itself. It runs nothing, so excluding the exact pause-mode
    argv cannot hide work; ``catatonit`` as an init for a real command can.
    Unlike ``pause.pid``, whose directory depends on the environment of the
    exec that first ran podman, the argv is visible to ``podman top`` too.
    """
    argv = args.split()
    return len(argv) == 2 and argv[0].rsplit("/", 1)[-1] == "catatonit" and argv[1] == "-P"


def parse_top_workloads(output: str) -> list[tuple[int, str]]:
    """Return processes outside the init/keeper process subtree.

    The keeper occasionally needs helper children (for example ``flock`` or a
    delay). Excluding its *tree* is structural and cannot hide an unrelated
    user ``sleep``/daemon merely because it has a familiar command name. The
    one argv-based exclusion is Podman's pause process, which runs nothing.
    """
    processes: dict[int, tuple[int, str]] = {}
    for line in output.splitlines()[1:]:  # first line is Podman's header
        fields = line.strip().split(None, 2)
        if len(fields) < 3:
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        processes[pid] = (parent, fields[2])

    # PID 1 is catatonit, but user daemons can be re-parented directly to it;
    # only descendants of the keeper (PID 2) are internal.
    internal = {2}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _args) in processes.items():
            if parent in internal and pid not in internal:
                internal.add(pid)
                changed = True
    return [(pid, args) for pid, (_parent, args) in processes.items()
            if pid not in internal and pid != 1 and not _is_podman_pause(args)]


def container_has_keeper(name: str) -> bool | None:
    result = subprocess.run(["podman", "top", name, "pid,ppid,args"], text=True, capture_output=True)
    if result.returncode:
        return None
    for line in result.stdout.splitlines()[1:]:
        fields = line.strip().split(None, 2)
        if len(fields) >= 2:
            try:
                if int(fields[0]) == 2 and int(fields[1]) == 1:
                    return True
            except ValueError:
                continue
    return False


def _wait_for_exiting_keeper(name: str, current: dict) -> dict | None:
    """Wait out the brief state-reporting lag after a keeper exits.

    The caller holds the shared transition lock. If the keeper is gone, no
    exec is admitted until Podman reports the old box stopped/removed.
    """
    if current.get("State", {}).get("Status") != "running":
        return current
    keeper = container_has_keeper(name)
    if keeper is True:
        return current
    if keeper is None:
        raise AgentBoxError("could not determine workspace keeper state")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        time.sleep(0.02)
        current = inspect_workspace_container(name)
        if current is None or current.get("State", {}).get("Status") != "running":
            return current
        if container_has_keeper(name) is True:
            return current
    # Never admit an exec to a box whose keeper has already gone away.  Podman
    # normally removes this state immediately; if it does not, failing is safer
    # than allowing a side-effecting provider command to start in a box that is
    # in the middle of disappearing.
    raise AgentBoxError("workspace container is shutting down; retry the command")


def container_has_workloads(name: str) -> bool | None:
    result = subprocess.run(["podman", "top", name, "pid,ppid,args"], text=True, capture_output=True)
    if result.returncode:
        return None
    return bool(parse_top_workloads(result.stdout))


def cleanup_workspace_container_if_idle(name: str) -> None:
    """Remove only a verified idle container; inspection failure is fail-safe."""
    current = inspect_workspace_container(name)
    if current is None:
        return
    if current.get("State", {}).get("Status") != "running":
        remove_workspace_container_then_confirm_absent(name)
        return
    workloads = container_has_workloads(name)
    if workloads is False:
        remove_workspace_container_then_confirm_absent(name)


def launch_in_workspace_container(
    spec: WorkspaceContainerSpec, workspace: Workspace, command: list[str], extra_env: list[str],
    process_env: dict[str, str] | None = None,
) -> int:
    """Start one exec under the transition lock, then clean up after it exits."""
    with workspace_transition_lock(workspace):
        host_k3s.verify(spec.creation_config, spec.runtime_state.root)
        if not spec.admitted_id:
            confirm_k3s(spec)
        ensure_workspace_container(spec)
        host_k3s.record_box(spec.creation_config, spec.runtime_state.root)
        if spec.admitted_id and spec.creation_config.box.links.enabled:
            try:
                links.reattach(spec.runtime_state.root.name, spec.name)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise AgentBoxError(str(exc)) from exc
        elif not spec.admitted_id and spec.creation_config.box.links.enabled:
            try:
                links.ensure(spec.runtime_state.root.name, spec.name, spec.creation_config.box.links,
                             host_network=spec.creation_config.box.network.mode == "host")
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise AgentBoxError(str(exc)) from exc
        # Only now does the box match this launch: a launch refused for
        # mounting differently must not rewrite a running box's logins.
        for problem in [*registries.sync(spec.creation_config, spec.runtime_state.root.name),
                        *kubeconfigs.sync(spec.creation_config, spec.runtime_state.root.name),
                        *cloud.sync(spec.creation_config, spec.runtime_state.root.name)]:
            warn(problem)
        process = subprocess.Popen(exec_argv(spec.name, workspace, command, extra_env), env=process_env)
        # The keeper takes this same advisory lock before it makes its final
        # idle-to-exit decision. Holding it until a real non-keeper process is
        # visible makes admission and shutdown mutually exclusive.
        while process.poll() is None:
            if container_has_workloads(spec.name) is True:
                break
            time.sleep(0.01)
    rc = process.wait()
    try:
        with workspace_transition_lock(workspace):
            cleanup_workspace_container_if_idle(spec.name)
            release_k3s(workspace, spec.creation_config, spec.runtime_state)
            release_nested_podman(workspace, spec.creation_config)
    except (AgentBoxError, OSError, subprocess.SubprocessError) as exc:
        # The agent command's status is more meaningful than a best-effort
        # post-exit cleanup problem. A later launch will recover stale
        # stopped/idle containers automatically.
        warn(f"could not clean up workspace container: {exc}")
    # Never retry an exec command: after Popen starts, its provider command
    # may already have performed side effects even if Podman returns 125.
    return rc


# Claude Code kills background shells, whole process tree included, on a
# 150 ms-in-2 s cgroup PSI spike once the user has been idle for 30 minutes.
# That spike is not memory exhaustion, and it killed managed reviews that must
# run in the background because they outlast the foreground command limit.
# The kernel OOM killer still applies.
PRESSURE_REAP_OPT_OUT = "CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP"


def invocation_env(config: Config, agent: str) -> tuple[list[str], dict[str, str]]:
    """Per-exec env flags and the host environment from which named values flow.

    Secret values are placed in Popen's environment and forwarded by *name*,
    never embedded in the Podman argv or a persistent container label/env.
    Processes inside one shared workspace box are nevertheless one trust
    boundary: same-UID peers can inspect each other's /proc environment on
    ordinary Linux/Podman setups.
    """
    forwarded = [*github_env(config), *provider_env(config), *user_env(config)]
    # github_env may obtain a token from `gh auth token` and add it to the
    # current process environment. Snapshot only after that deliberate
    # discovery so podman exec can inherit the name it forwards.
    env = dict(os.environ)
    if config.host.notifications.usable and agent in {"claude", "codex"}:
        env[notify_module.NOTIFY_TOKEN_ENV] = config.host.notifications.telegram_token
        env[notify_module.NOTIFY_CHAT_ID_ENV] = config.host.notifications.telegram_chat_id
        forwarded += [
            f"--env={notify_module.NOTIFY_TOKEN_ENV}",
            f"--env={notify_module.NOTIFY_CHAT_ID_ENV}",
        ]
    # `--env=NAME` means inherit NAME from the local podman client. Convert
    # the existing helpers' argv spelling into exec_argv's compact form.
    # Every agent gets the reaper opt-out, not only Claude: a tmux server keeps
    # the environment of the launch that started it, so a later Claude session
    # in a box first opened by `agent-box codex` would otherwise lack it.
    # Listed before user flags so an explicit box.env.set still wins.
    return [f"TERM={env.get('TERM', 'xterm-256color')}", f"{PRESSURE_REAP_OPT_OUT}=1",
            *(item[6:] for item in forwarded)], env


DOCTOR_SCRIPT = r'''
set -u
printf "%-20s %s\n" "image:" "${AGENT_BOX_IMAGE:-unknown}"
printf "%-20s %s\n" "architecture:" "$(uname -m)"
printf "%-20s %s\n" "user:" "$(id)"
printf "%-20s %s\n" "workdir:" "$PWD"
printf "%-20s %s\n" "claude:" "$(claude --version)"
printf "%-20s %s\n" "codex:" "$(codex --version)"
printf "%-20s %s\n" "bun:" "$(bun --version)"
printf "%-20s %s\n" "python:" "$(python --version 2>&1)"
printf "%-20s %s\n" "uv:" "$(uv --version)"
printf "%-20s %s\n" "mise:" "$(mise --version)"
printf "%-20s %s\n" "git:" "$(git --version)"
printf "%-20s %s\n" "git user:" "$(git config --global user.name) <$(git config --global user.email)>"
printf "%-20s %s\n" "gh:" "$(gh --version | head -1)"
printf "%-20s %s\n" "gh account:" "$(gh api user --jq .login 2>/dev/null || echo "not authenticated")"
# Only the tool groups this configuration asked for are checked. A group left
# out is a choice, and reporting it as missing would turn every deliberate
# omission into a fault.
has_group() { case ",${AGENT_BOX_TOOLS:-}," in *",$1,"*) return 0;; *) return 1;; esac; }

printf "%-20s %s\n" "tool groups:" "${AGENT_BOX_TOOLS:-(none)}"
python3 ''' + shlex.quote(str(KEEPER_PATH.with_name("podman_doctor.py"))) + r'''
if has_group analysis; then
  printf "%-20s %s\n" "shellcheck:" "$(shellcheck --version | sed -n 's/^version: //p')"
  printf "%-20s %s\n" "jscpd:" "$(jscpd --version 2>/dev/null || echo MISSING)"
fi
if has_group kubernetes; then
  printf "%-20s %s\n" "yq:" "$(yq --version)"
  printf "%-20s %s\n" "kubectl:" "$(kubectl version --client 2>/dev/null | head -1)"
  printf "%-20s %s\n" "helm:" "$(helm version --short)"
fi
if has_group skopeo; then
  printf "%-20s %s\n" "skopeo:" "$(skopeo --version)"
fi
echo
if has_group virtualization; then
  printf "%-20s %s\n" "qemu-x86:" "$(qemu-system-x86_64 --version | head -1)"
  printf "%-20s %s\n" "qemu-arm64:" "$(qemu-system-aarch64 --version | head -1)"
  if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    echo "KVM device:          usable"
    rc=0; timeout 2 qemu-system-x86_64 -accel kvm -machine none -nodefaults -display none >/dev/null 2>&1 || rc=$?
    [[ "${rc}" -eq 124 ]] && echo "amd64 KVM:           OK" || echo "amd64 KVM:           FAILED (rc=${rc})"
  else
    echo "KVM device:          unavailable"
  fi
  rc=0; timeout 2 qemu-system-aarch64 -accel tcg -machine virt -cpu max -S -nodefaults -display none >/dev/null 2>&1 || rc=$?
  [[ "${rc}" -eq 124 ]] && echo "arm64 TCG:           OK" || echo "arm64 TCG:           FAILED (rc=${rc})"
  echo
fi
# Honors CLAUDE_CONFIG_DIR the same way agent_box.paths.CLAUDE_HOME does, so
# doctor inspects the directory Claude actually uses rather than assuming
# $HOME/.claude.
claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
printf "%-20s %s\n" "claude config:" "${claude_home}"
printf "%-20s %s\n" "codex home:" "${CODEX_HOME:-$HOME/.codex}"
if [[ -f "${claude_home}/channels/telegram/.env" ]]; then
  echo "telegram channel:    token configured"
else
  echo "telegram channel:    no token (see 'agent-box config check')"
fi
echo
if [[ -d "$HOME/.agents/skills" ]]; then
  broken=0; shared=0
  while IFS= read -r -d "" link; do
    if [[ ! -e "${link}" ]]; then echo "BROKEN agent skill: ${link} -> $(readlink "${link}")"; broken=1; fi
  done < <(find "$HOME/.agents/skills" -maxdepth 1 -type l -print0)
  # Claude's copies are only expected when the configuration asks for them;
  # an unlinked tree is a deliberate answer, not a fault.
  if [[ "${AGENT_BOX_CLAUDE_SKILL_LINKS:-1}" == "1" ]]; then
    while IFS= read -r -d "" skill; do
      [[ -f "${skill}/SKILL.md" ]] || continue
      name="$(basename -- "${skill}")"; claude_skill="${claude_home}/skills/${name}"
      if [[ ! -f "${claude_skill}/SKILL.md" ]]; then echo "MISSING Claude skill: ${claude_skill}"; broken=1; continue; fi
      agent_real="$(readlink -f -- "${skill}")"; claude_real="$(readlink -f -- "${claude_skill}")"
      if [[ "${agent_real}" != "${claude_real}" ]]; then echo "MISMATCH Claude skill: ${claude_skill} -> ${claude_real} (expected ${agent_real})"; broken=1; continue; fi
      shared=$((shared + 1))
    done < <(find "$HOME/.agents/skills" -mindepth 1 -maxdepth 1 -print0)
    [[ "${broken}" -eq 0 ]] && echo "shared agent skills: OK (${shared})"
  else
    [[ "${broken}" -eq 0 ]] && echo "shared agent skills: not linked into Claude (turned off in 'agent-box config')"
  fi
fi
echo
# The Vikunja integration is off by default, so "not exposed" is a normal
# answer here, not a fault. When it is on, the interesting question is not
# "is the file mounted" but "does a call actually reach the server", because
# The tracker command reads only the rendered, enabled connection subset.
if [[ -n "${AGENT_BOX_RUNTIME_CONFIG:-}" ]]; then
  agent-box tracker status || true
else
  echo "tracker:             not exposed (enable it with 'agent-box config')"
fi
echo
# Checked from inside the box, through the same kubeconfig and loopback
# forward an agent uses, so "OK" means kubectl here really reaches the cluster.
if [[ "${AGENT_BOX_K3S:-0}" == "1" ]]; then
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "k3s cluster:         kubectl MISSING (turn on the kubernetes tool group and run 'agent-box build')"
  elif nodes="$(timeout 15 kubectl get nodes --no-headers 2>&1)"; then
    printf "%-20s %s\n" "k3s cluster:" "OK (${nodes//$'\n'/; })"
  else
    echo "k3s cluster:         FAILED"
    printf "  %s\n" "${nodes}"
  fi
elif [[ "${AGENT_BOX_K3S:-0}" == "nested" ]]; then
  echo "k3s cluster:         none in a nested box (k3s runs beside a box started on the host)"
else
  echo "k3s cluster:         disabled (turn on box.k3s in 'agent-box config')"
fi
'''


CLAUDE_MANAGEMENT_COMMANDS = frozenset({
    "agents", "attach", "auth", "auto-mode", "daemon", "doctor", "gateway", "install",
    "kill", "logs", "mcp", "plugin", "plugins", "project", "remote-control", "rc",
    "respawn", "rm",
    "self-hosted-runner", "setup-token", "stop", "ultrareview", "update", "upgrade",
})

# Claude 2.1.240 accepts a leading ``--model`` for these commands.  It does
# not do so for every command above: ``claude --model opus daemon status`` and
# ``claude --model opus logs ID`` start a session instead.  Keep this explicit
# rather than attempting to clone Claude's complete argument parser.
_CLAUDE_MODEL_PREFIX_MANAGEMENT_COMMANDS = frozenset({
    "agents", "auth", "auto-mode", "doctor", "gateway", "install", "mcp", "plugin",
    "plugins", "project", "remote-control", "rc", "setup-token", "ultrareview", "update",
    "upgrade",
})

_CLAUDE_INFORMATION_FLAGS = frozenset({"--help", "-h", "--version", "-v"})
_CLAUDE_BACKGROUND_FLAGS = frozenset({"--bg", "--background"})


def is_claude_non_session_flag_invocation(rest: list[str]) -> bool:
    """Whether verified flag-only syntax dispatches without a Claude session.

    Only inspect arguments before ``--`` so a prompt such as ``--init-only``
    remains session input.  This intentionally covers only documented forms:
    information flags, ``--init-only``, and ``--bg/--background --exec``.
    """
    try:
        before_separator = rest[:rest.index("--")]
    except ValueError:
        before_separator = rest
    flags = frozenset(before_separator)
    if flags & _CLAUDE_INFORMATION_FLAGS or "--init-only" in flags:
        return True
    return bool(flags & _CLAUDE_BACKGROUND_FLAGS) and any(
        arg == "--exec" or arg.startswith("--exec=") for arg in before_separator
    )


def is_claude_session_invocation(rest: list[str]) -> bool:
    """Whether Claude will dispatch this argv to a session.

    This is deliberately a small dispatch table verified against Claude Code,
    not a reimplementation of its option parser.  ``import`` is excluded from
    the management set because supported builds start an interactive session
    which runs ``/import``.  A feature-disabled build exits before that session
    starts, but its invocation form remains session-oriented.
    """
    if not rest:
        return True
    if is_claude_non_session_flag_invocation(rest):
        return False
    first = rest[0]
    if first == "--":
        return True
    if not first.startswith("-"):
        return first not in CLAUDE_MANAGEMENT_COMMANDS

    # Claude documents this daemon-specific exception for aliases that put a
    # permission flag first.  No other leading option is inferred here.
    permission_flags = {
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
    }
    if first in permission_flags:
        return not (len(rest) > 1 and rest[1] == "daemon")

    if first == "--model":
        command = rest[2] if len(rest) > 2 else ""
        return command not in _CLAUDE_MODEL_PREFIX_MANAGEMENT_COMMANDS
    if first.startswith("--model="):
        command = rest[1] if len(rest) > 1 else ""
        return command not in _CLAUDE_MODEL_PREFIX_MANAGEMENT_COMMANDS
    return True


def claude_session_args(
    rest: list[str], channel: list[str], browser: list[str] | None = None,
) -> list[str]:
    """Add Claude's variadic options after all ordinary Claude arguments.

    Both ``--channels`` and ``--mcp-config`` consume every following
    non-option argument.  By appending them after the session argv, a
    positional prompt cannot become a channel entry or be mistaken for an MCP
    config path -- ``claude --print --mcp-config FILE "hi"`` really does try
    to load ``hi`` as a second config file.  Each option stops at the next
    one because that starts with ``-``, and an explicit ``--`` ends Claude
    option parsing, so both are inserted immediately before that separator
    when present.
    """
    appended = [*channel, *(browser or [])]
    try:
        separator = rest.index("--")
    except ValueError:
        return [*rest, *appended]
    return [*rest[:separator], *appended, *rest[separator:]]


def claude_channel_args(
    agent: str, config: Config, workspace: Workspace, rest: list[str] | None = None,
    session: TmuxSession | None = None,
) -> tuple[list[str], list[str]]:
    """Decide whether this Claude session gets the official Telegram channel.

    Three outcomes, none of which stops Claude from starting:

    * not Claude, a management command, or the channel is switched off -- nothing to do.
    * enabled but the plugin/token setup is incomplete -- warn and run without
      it. ``agent-box config`` refuses to enable the channel before the
      prerequisites exist, so this only happens if the plugin was uninstalled
      or its token removed afterwards.
    * enabled and another live session already owns the channel -- say so and
      run without it. The bot has one conversation; two sessions answering
      every inbound message is worse than the second one being local-only.

    When the channel is taken, the Claude hook entry point suppresses only
    interactive prompt notifications: the channel already relays those to the
    paired chat.  Mechanical Stop/StopFailure lifecycle notifications remain
    enabled so the notification bot still reports completion and errors.
    """
    if agent != "claude":
        return [], []
    # The official plugin is globally loaded by Claude Code and its MCP
    # server can start even without --channels. Its source loads .env only
    # when TELEGRAM_BOT_TOKEN is *undefined*, so an explicit empty value
    # prevents fallback to the shared persisted token and exits before
    # getUpdates. Every non-owner needs this, including when the channel is
    # disabled, or a second normal Claude would steal the bot poller.
    isolated = ["TELEGRAM_BOT_TOKEN="]
    if not is_claude_session_invocation(rest or []):
        return [], isolated
    if not config.agents.claude.telegram_channel:
        return [], isolated
    missing = channel_module.missing_requirements()
    if missing:
        warn("Telegram channel unavailable: " + " and ".join(missing))
        warn("run 'agent-box config check' for setup steps; starting Claude without it")
        return [], isolated
    # Naming the session this launch is about to start is what keeps the claim
    # standing if the terminal goes away: the detached Claude is still the one
    # talking to the bot (see agent_box.channel).
    if not channel_module.acquire_channel(
        str(workspace.host_root),
        container=workspace_container_name(workspace) if session is not None else "",
        session=session.name if session is not None else "",
    ):
        owner = channel_module.channel_owner()
        detail = f" ({channel_module.describe_owner(owner)})" if owner else ""
        print(f"agent-box: Telegram channel already active in another Claude session{detail}", file=sys.stderr)
        return [], isolated
    return channel_module.channel_flag(), [f"{SUPPRESS_NOTIFY_ENV}=1"]


def runtime_main(
    args: list[str], script_dir: Path, options: LaunchOptions | None = None,
    *, api_provider: str = "",
) -> None:
    from .mounts import MountError, parse_mount_arg, plan_mounts

    options = options or LaunchOptions()
    host_podman.require()
    if not (script_dir / "Containerfile").is_file():
        raise AgentBoxError(f"Containerfile not found: {script_dir / 'Containerfile'}")
    if not shutil.which("git"):
        # Not fatal, but the failure it causes is invisible: workspace
        # resolution falls back to the current directory, so the box quietly
        # gets a subdirectory of the project instead of the repository root.
        warn("git not found on the host: the workspace root falls back to the "
             "current directory instead of the repository root")
    workspace = resolved_workspace(options.workspace)
    try:
        extra_mounts = plan_mounts([parse_mount_arg(spec) for spec in options.mounts])
    except MountError as exc:
        raise AgentBoxError(str(exc)) from exc

    ensure_state_dirs()
    ensure_refresh_token()
    options, requested = nested_launch_options(options)
    existing = existing_workspace_spec(workspace, options, script_dir)
    config = launch_config(options, box_policy=existing.creation_config._box_policy if existing else None)
    with workspace_transition_lock(workspace):
        host_k3s.inherit(config, options, workspace_runtime_state(workspace).root)
    nested = list(dict.fromkeys(requested + withhold_from_nested_box(config)))
    errors = config.box.validate()
    if errors:
        raise AgentBoxError(errors[0])
    try:
        plan_mounts([*extra_mounts, *host_tmp_mounts(config.box.host_tmp)])
    except MountError as exc:
        raise AgentBoxError(str(exc)) from exc
    agent, rest = config.agents.default, args
    if args and args[0] in {"claude", "codex", "shell", "doctor"}:
        agent, rest = args[0], args[1:]
    if agent not in {"claude", "codex", "shell", "doctor"}:
        raise AgentBoxError(f"unsupported agent '{agent}'; expected claude or codex")
    if api_provider:
        if agent not in {"claude", "codex"}:
            raise AgentBoxError(f"ext {api_provider} runs claude or codex, not {agent}")
        from .external_launch import selected_model
        selected_model(config, api_provider, agent, rest)

    # Joining a running workspace never consults or replaces its image.
    selected_image = existing.image if existing is not None else ensure_workspace_image(script_dir, config)
    if (config.box.network.mode != "default" or config.box.network.ports or config.box.network.host_ports
            or config.box.k3s.enabled and config.box.k3s.ports):
        for line in networking.summary(config.box.network, config.box.k3s):
            print("agent-box: " + line, file=sys.stderr)
    # Every launch, not only box creation: joining a running box with the
    # switch on and no socket leaves box identity unchanged and would say nothing.
    from .external_config import model_warnings
    for problem in audio_warnings(config) + nested + k3s_warnings(config) + tool_warnings(config) + model_warnings(config):
        warn(problem)


    # CLI dispatch handles package-update reconciliation once, before this
    # launch. This layer only reads the choices and prepares runtime state.

    # Decided before the channel is claimed, because the claim records it.
    runtime_state = workspace_runtime_state(workspace)
    session = tmux_session(agent, runtime_state) if tmux_enabled(agent, options.tmux) else None
    # The session directory is already mounted; browser choice is per invocation.
    browser_dir = browser_session(config, workspace, runtime_state, agent)
    if browser_dir is not None:
        try:
            inherited = existing.browser_mounts if existing is not None else extra_mounts
            browser.prepare(browser_dir, workspace.host_root, workspace.cwd, inherited,
                            container=workspace_container_name(workspace), workspace=workspace_identity(workspace),
                            session=session.name if session else "", host_tmp=config.box.host_tmp)
        except (AgentBoxError, OSError) as exc:
            warn(f"browser unavailable: {exc}; starting without it")
            browser_dir = None
    personal_dir = None
    if (config.host.personal_browser.enabled and agent in {"claude", "codex"}
            and not any(arg in {"--version", "-V", "--help", "-h"} for arg in rest)):
        try:
            desktop, notice = personal_browser.prepare(config.host.personal_browser)
            print(f"agent-box: personal browser: {notice}", file=sys.stderr)
            personal_dir = browser_session_root(runtime_state) / browser_session_id(agent)
            inherited = existing.browser_mounts if existing is not None else extra_mounts
            browser.prepare(personal_dir, workspace.host_root, workspace.cwd, inherited,
                            container=workspace_container_name(workspace), workspace=workspace_identity(workspace),
                            session=session.name if session else "", host_tmp=config.box.host_tmp, desktop=desktop)
        except (AgentBoxError, OSError) as exc:
            warn(f"personal browser unavailable: {exc}")
            personal_dir = None
    channel, extra_env = claude_channel_args(agent, config, workspace, rest, session)
    if getattr(config, "_launch", {}).get("notify") is False:
        extra_env += [f"{notify_module.NOTIFY_OFF_ENV}=1"]
    snapshot = session_runtime(config, runtime_state=runtime_state)
    snapshot_root, snapshot_destination, _ = tracker_mounts(config, runtime_state)[0]
    snapshot_target = snapshot_destination / snapshot.relative_to(snapshot_root)
    if config.tracker.enabled or config.workflow.enabled:
        extra_env += [f"AGENT_BOX_WORKFLOW_STATE={CONTAINER_WORKFLOW_STATE}"]
    extra_env += [f"AGENT_BOX_RUNTIME_CONFIG={snapshot_target}"]


    try:
        if agent == "shell":
            command = ["bash", "--login", *rest]
        elif agent == "doctor":
            print(browser.diagnose() if config.box.browser.active() else
                  "browser: disabled (enable Workspace box > Browser in agent-box config)", flush=True)
            # The script runs inside the box and cannot read the host config,
            # so the one bit it needs is forwarded per-exec.
            extra_env = [
                *extra_env,
                f"{BROWSER_ENABLED_ENV}={'1' if config.box.browser.active() else '0'}",
                # A joined nested box already carries k3s off in its policy.
                f"AGENT_BOX_K3S={'nested' if inside_box() else '1' if config.box.k3s.enabled else '0'}",
                f"{CLAUDE_SKILL_LINKS_ENV}={'1' if config.host.claude_skill_links else '0'}",
                f"{IMAGE_TOOLS_ENV}={','.join(config.box.tools.enabled_groups())}",
                f"AGENT_BOX_IMAGE={existing.image if existing else IMAGE}",
            ]
            command = ["bash", "-c", DOCTOR_SCRIPT]
        elif agent == "claude":
            if is_claude_session_invocation(rest):
                command = [
                    "claude", "--dangerously-skip-permissions",
                    *claude_session_args(rest, channel, [*browser_claude_args(browser_dir),
                        *browser_claude_args(personal_dir, server_name=personal_browser.SERVER)]),
                ]
            else:
                command = ["claude", *rest]
        else:
            command = codex_command(rest, config, browser_dir)
            command[1:1] = [arg for override in browser_codex_overrides(
                personal_dir, server_name=personal_browser.SERVER) for arg in ("-c", override)]
        if api_provider:
            command = external_command(command, agent, api_provider, container=True)
        elif remote_control_invocation(agent, rest):
            command = remote_control_command(command, agent, config, container=True)
        if existing is None:
            prepare_network(config)
            prepare_k3s(config, workspace, runtime_state)
            bundle = runtime_bundle.prepare(script_dir, STATE_ROOT / "runtime/launcher")
            spec = workspace_container_spec(workspace, extra_mounts, bundle, config, options.kvm, image=selected_image)
        else:
            spec = existing
        invocation, process_env = invocation_env(config, agent)
        # Channel/workflow values are invocation policy, not box identity.
        invocation += extra_env
        if session is not None:
            announce_existing_sessions(spec.name)
            command = session.wrap(command)
        rc = launch_in_workspace_container(spec, workspace, command, invocation, process_env)
        if session is not None:
            rc = session.finish(spec.name, rc)
    except BaseException as exc:
        # The lock names this PID and would go stale the moment we exit, but
        # releasing it explicitly means a failed launch never leaves a record
        # that `agent-box config check` would report as an active session.
        channel_module.release_channel()
        # plan_mounts rejects contradictory --mount requests here; that is a
        # user error and must print as one, not as a traceback.
        if isinstance(exc, MountError):
            raise AgentBoxError(str(exc)) from exc
        raise
    channel_module.release_channel()
    raise SystemExit(rc)


def resolved_workspace(override: str = "") -> Workspace:
    try:
        return resolve_workspace(override=override)
    except (NotADirectoryError, ValueError) as exc:
        raise AgentBoxError(str(exc)) from exc


def running_workspace_container(workspace: Workspace) -> str:
    name = workspace_container_name(workspace)
    current = inspect_workspace_container(name)
    if current is None or current.get("State", {}).get("Status") != "running":
        raise AgentBoxError("no running agent-box container for this workspace")
    return name


def choose_session(sessions: list[str], wanted: str | None, *, location: str = "this workspace's box") -> str:
    if not sessions:
        raise AgentBoxError(f"{location} has no detachable sessions")
    if wanted is not None:
        if wanted not in sessions:
            raise AgentBoxError(f"no session '{wanted}' in {location}")
        return wanted
    if len(sessions) == 1:
        return sessions[0]
    listing = "\n".join(f"  {name}" for name in sessions)
    raise AgentBoxError(f"{location} holds several sessions; name one:\n" + listing)


def attach_main(args: list[str], workspace_root: str = "") -> int:
    """``agent-box attach [session]``: take back a session from a dead terminal."""
    if len(args) > 1:
        raise AgentBoxError("usage: agent-box attach [session]")
    host_podman.require()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise AgentBoxError("agent-box attach needs a terminal")
    workspace = resolved_workspace(workspace_root)
    try:
        name = running_workspace_container(workspace)
    except AgentBoxError:
        # No box to attach to: a sidecar bound to the box it outlived stops now.
        with workspace_transition_lock(workspace):
            release_k3s(workspace)
            release_nested_podman(workspace)
        raise
    session = choose_session(tmux_sessions(name), args[0] if args else None)
    # ``-d`` evicts whichever client still holds the session. After a closed
    # terminal that client is a ghost: conmon keeps its pty open, so it never
    # reads EOF and would otherwise keep the session to itself forever.
    command = tmux_argv("attach-session", "-d", "-t", session)
    with workspace_transition_lock(workspace):
        try:
            links.reattach(workspace_runtime_state(workspace).root.name, name)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise AgentBoxError(str(exc)) from exc
        # The box is holding a live tmux server, so unlike a first launch
        # there is no idle window here for the keeper to shut down.
        process = subprocess.Popen(exec_argv(name, workspace, command, []))
    rc = process.wait()
    try:
        with workspace_transition_lock(workspace):
            cleanup_workspace_container_if_idle(name)
            release_k3s(workspace)
            release_nested_podman(workspace)
    except (AgentBoxError, OSError, subprocess.SubprocessError) as exc:
        warn(f"could not clean up workspace container: {exc}")
    return rc


def stop_main(args: list[str], workspace_root: str = "") -> int:
    """``agent-box stop [--cluster-data] [--podman-data]``: end this workspace's box and cluster.

    Ordinary boxes still disappear on their own once no workload remains. A
    detached session *is* a workload, so a box someone walked away from needs
    a way to be shut down deliberately. The k3s sidecar stops with it
    whatever its lifetime; its data stays unless ``persistent_data`` is off or
    ``--cluster-data`` asks for it to go. Nested Podman data follows its own
    ``persistent_data`` the same way, and ``--podman-data`` deletes it anyway.
    """
    flags = {"--cluster-data", "--podman-data"}
    for arg in args:
        if arg not in flags:
            raise AgentBoxError(f"unexpected argument '{arg}'; agent-box stop takes only "
                                "--cluster-data and --podman-data")
    cluster_data, podman_data = "--cluster-data" in args, "--podman-data" in args
    host_podman.require()
    workspace = resolved_workspace(workspace_root)
    name = workspace_container_name(workspace)
    runtime_state = workspace_runtime_state(workspace)
    config = effective_config(runtime_config_base())
    with workspace_transition_lock(workspace):
        host_k3s.refuse_owners(runtime_state.root)
        retained = host_k3s.load(runtime_state.root)
        box_config = last_box_config(runtime_state, config)
        if retained:
            config.box.k3s = Config.from_dict({"box": retained["box"]}).box.k3s
        elif box_config is not None:
            config.box.k3s = box_config.box.k3s
        else:
            # An unreadable creation policy cannot authorize deleting data.
            config.box.k3s = replace(config.box.k3s, persistent_data=True)
        current = inspect_workspace_container(name)
        if current is None:
            print("agent-box: no workspace container for this project", file=sys.stderr)
            remove_browser_sessions(runtime_state)
        else:
            if current.get("State", {}).get("Status") == "running":
                try:
                    sessions = tmux_sessions(name)
                except AgentBoxError:
                    sessions = []
                if sessions:
                    print(f"agent-box: ending {len(sessions)} session(s): {', '.join(sessions)}", file=sys.stderr)
            remove_workspace_container_then_confirm_absent(name)
            shutil.rmtree(runtime_state.root / "sessions", ignore_errors=True)
            # The box is gone, so no browser session of this workspace can still
            # be running; its snapshots and screenshots go with it.
            remove_browser_sessions(runtime_state)
            print(f"agent-box: removed workspace container {name}", file=sys.stderr)
        links.stop(runtime_state.root.name)
        workspace_id = workspace_identity(workspace)
        sidecar = k3s.name(workspace_id)
        if cluster_data:
            k3s.remove(workspace_id, k3s_state_dir(runtime_state), data=True)
            print(f"agent-box: removed the k3s cluster {sidecar} and its data", file=sys.stderr)
        # Only a workspace that ever created a sidecar asks Podman about one.
        elif k3s_state_dir(runtime_state).is_dir() and k3s.stop(workspace_id, config.box.k3s,
                                                               k3s_state_dir(runtime_state)):
            kept = "" if config.box.k3s.persistent_data else " and deleted its data"
            print(f"agent-box: stopped the k3s cluster {sidecar}{kept}", file=sys.stderr)
        disposable = box_config is not None and not box_config.box.podman.persistent_data
        if (podman_data or disposable) and remove_nested_podman_volume(workspace_id):
            print(f"agent-box: deleted the nested Podman data {nested_podman_volume(workspace_id)}", file=sys.stderr)
    return 0


def apply_config(config: Config, *, session=False) -> list[str]:
    """Make the host match the configuration, and say what that took.

    The one place anything agent-box owns *outside its own directories* is
    written: the skills it ships, the links that let Claude read the shared
    tree, and the status line and hooks in Claude's ``settings.json``. Its own
    state -- credential renders under ``~/.local/share/agent-box`` -- is
    refreshed here too, but that part is also safe to redo at launch, because
    it is agent-box's to manage.

    Configuration commands call this explicitly. The CLI also reconciles once
    on the first host launch after a package update, under the package-apply
    lock; unchanged launches and rendered sessions never reapply host settings.

    ``session`` applies only what a rendered session snapshot describes --
    the shipped skills and the configured agent profiles. The host-only
    writers (notification credentials and hooks, status line,
    Claude skill links, runtime render) are skipped: the snapshot does not
    carry those sections, so reconciling them would remove host state.

    Returns the human-facing lines describing what happened, so every caller
    renders the same report instead of each narrating its own.
    """
    report: list[str] = []

    ensure_state_dirs()
    if not session:
        notify_module.sync_credentials(config, path=NOTIFY_CREDENTIALS)
        from . import codex_settings
        report.extend(codex_settings.reconcile_notify(CODEX_HOME / "config.toml", config.host.notifications.enabled))
        sync_tracker_runtime(config)

    wanted = managed_skills(config)
    sync_managed_skills(config)
    from . import agent_profiles
    report.extend(agent_profiles.sync(config, CODEX_HOME, CLAUDE_HOME))
    report.append(
        f"skills installed in {AGENT_SKILLS_DIR}: {', '.join(sorted(wanted)) or 'none'}")

    if not session:
        claude_skill_links(config)
        report.append(
            f"shared skills linked into {CLAUDE_HOME / 'skills'}: "
            f"{'yes' if config.host.claude_skill_links else 'no'}")

        settings = CLAUDE_HOME / "settings.json"
        statusline = claude_settings.statusline_intent(config.agents.claude.statusline.enabled)
        claude_settings.reconcile_claude_settings(
            settings, notifications=config.host.notifications.enabled, statusline=statusline)
        report.append(
            f"reconciled {settings}"
            + ("" if statusline is not None else " (status line left untouched)"))
    return report


def managed_container_names() -> list[str]:
    """Every workspace box agent-box created, across all projects."""
    command = [
        "podman", "ps", "--all", "--filter", f"label={WORKSPACE_CONTAINER_LABEL}=true",
        "--format", "{{.Names}}",
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True)
    except OSError:
        return []
    if result.returncode:
        return []
    return [name for name in result.stdout.split() if name]


def uninstall_main(args: list[str]) -> int:
    """``agent-box uninstall``: undo everything agent-box wrote outside itself.

    A launcher that reconciles the user's Claude settings, installs skills
    into a shared tree and renders credential files owes them a way back. The
    alternative is what agent-box had until now: deleting the program leaves a
    ``statusLine`` pointing at a command that no longer exists, hooks that
    fail on every turn, and skill symlinks dangling in a directory Claude
    still reads.

    Two levels, because they are different decisions. The default removes
    what agent-box put in *other* people's places -- Claude's
    ``settings.json``, ``~/.claude/skills``, the shared agent tree, the
    rendered credential files -- and stops the
    boxes holding those mounts open. ``--purge`` additionally deletes
    agent-box's own configuration and state, which is where the Telegram and
    Vikunja tokens live; that one is irreversible and is confirmed unless
    ``--yes`` says not to.

    Installer-owned entry points and shell startup files are left alone.
    The installer removes the Python package; the container image is also
    left for explicit removal.
    """
    purge = False
    assume_yes = False
    for arg in args:
        if arg == "--purge":
            purge = True
        elif arg == "--yes":
            assume_yes = True
        else:
            raise AgentBoxError(f"unexpected argument '{arg}'; expected --purge and/or --yes")

    with host_k3s.admission_lock(exclusive=True):
        return _uninstall(purge, assume_yes)


def _uninstall(purge: bool, assume_yes: bool) -> int:
    """Hold admission closed from the owner preflight through the final purge."""
    for root in (STATE_ROOT / "runtime/workspaces").glob("*"):
        if root.is_dir():
            host_k3s.refuse_owners(root)
    done: list[str] = []

    from . import upgrades
    upgrades.forget()

    names = managed_container_names() if shutil.which("podman") else []
    for name in names:
        remove_workspace_container_then_confirm_absent(name)
    if names:
        done.append(f"removed {len(names)} workspace box(es)")
    # Containers and networks at both levels; the clusters' data is
    # agent-box state, so its volumes go only after --purge is confirmed.
    clusters: list[str] = []
    if shutil.which("podman"):
        try:
            clusters = k3s.all_workspaces()
            for workspace_id in clusters:
                k3s.remove(workspace_id, None, data=False)
        except AgentBoxError as exc:
            done.append(f"warning: k3s clusters were not removed: {exc}")
        else:
            if clusters:
                done.append(f"removed {len(clusters)} k3s cluster(s)")
    # Nested Podman data is agent-box state like a cluster's, kept until --purge.
    podman_data: list[str] = []
    if shutil.which("podman"):
        try:
            podman_data = nested_podman_workspaces()
        except AgentBoxError as exc:
            done.append(f"warning: nested Podman volumes were not listed: {exc}")

    settings = CLAUDE_HOME / "settings.json"
    if claude_settings.reconcile_claude_settings(
            settings, notifications=False, statusline=False):
        done.append(f"removed the agent-box status line and notification hooks from {settings}")


    from . import codex_settings
    done.extend(codex_settings.reconcile_notify(CODEX_HOME / "config.toml", False))

    disabled = Config()
    disabled.host.claude_skill_links = False
    claude_skill_links(disabled)
    done.append(f"removed agent-box's skill links from {CLAUDE_HOME / 'skills'}")

    sync_managed_skills(disabled)
    from . import agent_profiles
    done.extend(agent_profiles.sync(disabled, CODEX_HOME, CLAUDE_HOME))
    done.append(f"removed the skills agent-box installed in {AGENT_SKILLS_DIR}")

    NOTIFY_CREDENTIALS.unlink(missing_ok=True)
    shutil.rmtree(TRACKER_RUNTIME_DIR, ignore_errors=True)
    shutil.rmtree(POLICY_RUNTIME_DIR, ignore_errors=True)
    shutil.rmtree(registries.REGISTRY_RUNTIME_DIR, ignore_errors=True)
    shutil.rmtree(kubeconfigs.KUBECONFIG_RUNTIME_DIR, ignore_errors=True)
    shutil.rmtree(cloud.CLOUD_RUNTIME_DIR, ignore_errors=True)
    done.append("removed the rendered notification, tracker, registry, kubeconfig and cloud CLI credentials")

    if purge:
        if not assume_yes:
            print(f"This deletes {CONFIG_ROOT} and {STATE_ROOT}, including any stored tokens.")
            try:
                answer = input("Delete agent-box's configuration and state? [y/N]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                answer = ""
            if answer not in {"y", "yes"}:
                print("Keeping configuration and state.")
                purge = False
        if purge:
            try:
                for workspace_id in clusters:
                    k3s.remove(workspace_id, None, data=True)
            except AgentBoxError as exc:
                done.append(f"warning: k3s cluster data was not removed: {exc}")
            else:
                if clusters:
                    done.append(f"deleted the data of {len(clusters)} k3s cluster(s)")
            try:
                for workspace_id in podman_data:
                    remove_nested_podman_volume(workspace_id)
            except AgentBoxError as exc:
                done.append(f"warning: nested Podman data was not removed: {exc}")
            else:
                if podman_data:
                    done.append(f"deleted the nested Podman data of {len(podman_data)} workspace(s)")
            shutil.rmtree(CONFIG_ROOT, ignore_errors=True)
            shutil.rmtree(STATE_ROOT, ignore_errors=True)
            done.append(f"deleted {CONFIG_ROOT} and {STATE_ROOT}")
    for line in done:
        print(f"agent-box: {line}")
    print()
    print("Remaining, deliberately left for you:")
    if not purge:
        print(f"  configuration and state   {CONFIG_ROOT}, {STATE_ROOT}")
        if clusters:
            print("  k3s cluster data          podman volume rm " + " ".join(k3s.name(c) for c in clusters))
        if podman_data:
            print("  nested Podman data        podman volume rm "
                  + " ".join(nested_podman_volume(w) for w in podman_data))
    print(f"  container image           podman rmi {IMAGE}")
    print("  Python package            uv tool uninstall agent-box-tool (or pipx uninstall agent-box-tool)")
    return 0


def host_main(
    agent: str | None, rest: list[str], options: LaunchOptions | None = None,
    *, api_provider: str = "",
) -> int:
    """``agent-box --host [claude|codex]``: run the installed host executable
    directly. The optional browser integrations use Podman.

    Workflow policy is read from the same global configuration as container mode.
    No agent means the default agent, resolved as in container mode.
    """
    options = options or LaunchOptions()
    workspace = resolved_workspace(options.workspace)
    config = launch_config(options)
    from .external_config import model_warnings
    for problem in model_warnings(config):
        warn(problem)
    agent = agent or config.agents.default
    executable = shutil.which(agent)
    if not executable:
        raise AgentBoxError(f"'{agent}' not found on PATH; install it on the host to use --host")
    from .external_launch import builtin_environment, selected_model
    if api_provider:
        selected_model(config, api_provider, agent, rest)
    reservation = None
    if host_interactive_invocation(agent, rest, batch=True):
        reservation = host_k3s.reserve(config, options, workspace)
    try:
        # As in container mode: a launch applies nothing to the user's own files.
        env = builtin_environment(os.environ)
        env[PRESSURE_REAP_OPT_OUT] = "1"
        # ``box.env.set`` states what a session should have; a host session is still a
        # session. ``box.env.forward`` has nothing to do here -- host mode already
        # runs in the very shell those names would have been forwarded from, so
        # the values are present already.
        env.update(config.box.env.set)
        # Owned metadata describes this launch, even when box.env.set names these keys.
        env["AGENT_BOX_RUNTIME_CONFIG"] = str(session_runtime(config, container=False))
        if getattr(config, "_launch", {}).get("notify") is False:
            env[notify_module.NOTIFY_OFF_ENV] = "1"
        # agent-box's own request channel is not part of the session it describes.
        for key in [k for k in env if k.startswith(ENV_PREFIX)]:
            del env[key]

        if reservation:
            env[host_k3s.ENV] = json.dumps([str(reservation[0]), reservation[1]])
            if config.box.k3s.enabled:
                env["KUBECONFIG"] = str(k3s.kubeconfig_path(reservation[0] / "k3s"))

        browser_dir = None
        browser_command = None
        if config.box.browser.active() and not any(arg in {"--version", "-V", "--help", "-h"} for arg in rest):
            browser_dir = HOST_SESSIONS_ROOT / workspace_identity(workspace) / "browser" / uuid.uuid4().hex
            try:
                browser_command = browser.prepare(browser_dir, workspace.host_root, workspace.cwd,
                                                  workspace=workspace_identity(workspace), host_tmp=config.box.host_tmp)
                record_workspace_path(workspace_identity(workspace), str(workspace.host_root.resolve()))
            except (AgentBoxError, OSError) as exc:
                warn(f"browser unavailable: {exc}; starting without it")
                browser_dir = None
        personal_dir = None
        personal_command = None
        if config.host.personal_browser.enabled and not any(arg in {"--version", "-V", "--help", "-h"} for arg in rest):
            try:
                desktop, notice = personal_browser.prepare(config.host.personal_browser)
                print(f"agent-box: personal browser: {notice}", file=sys.stderr)
                personal_dir = HOST_SESSIONS_ROOT / workspace_identity(workspace) / "browser" / uuid.uuid4().hex
                personal_command = browser.prepare(personal_dir, workspace.host_root, workspace.cwd,
                                                   workspace=workspace_identity(workspace),
                                                   host_tmp=config.box.host_tmp, desktop=desktop)
                record_workspace_path(workspace_identity(workspace), str(workspace.host_root.resolve()))
            except (AgentBoxError, OSError) as exc:
                warn(f"personal browser unavailable: {exc}")
                personal_dir = None
        workflow_overrides = codex_workflow_overrides(rest, config) if agent == "codex" else []
        if agent == "codex":
            workflow_overrides += browser_codex_overrides(browser_dir, browser_command)
            workflow_overrides += browser_codex_overrides(personal_dir, personal_command, server_name=personal_browser.SERVER)
        browser_args = browser_claude_args(browser_dir, browser_command) if agent == "claude" and is_claude_session_invocation(rest) else []
        if agent == "claude" and is_claude_session_invocation(rest):
            browser_args += browser_claude_args(personal_dir, personal_command, server_name=personal_browser.SERVER)
        agent_args = claude_session_args(rest, [], browser_args) if agent == "claude" else rest
        command = [executable, *(arg for override in workflow_overrides for arg in ("-c", override)), *agent_args]
        if api_provider:
            command = external_command(command, agent, api_provider, container=False)
        elif remote_control_invocation(agent, rest):
            command = remote_control_command(command, agent, config, container=False)
        if tmux_enabled(agent, options.tmux) and host_interactive_invocation(agent, rest):
            tmux = shutil.which("tmux")
            if tmux is not None:
                return host_tmux_launch(workspace, agent, command, env, tmux)
            warn("tmux not found; starting directly without detach/attach support")
        if reservation:
            command = [sys.executable, str(HOST_SESSION_RUNNER), "--direct", *command]
        return subprocess.call(command, cwd=str(workspace.cwd), env=env)

    finally:
        if reservation:
            host_k3s.release(*reservation, pending=True)


def external_command(command: list[str], agent: str, name: str, *, container: bool) -> list[str]:
    launcher = ["python3", str(CONTAINER_LAUNCHER)] if container else [sys.executable, str(HOST_LAUNCHER)]
    return [*launcher, "_external", name, agent, *command]


def remote_control_command(command: list[str], agent: str, config: Config, *, container: bool) -> list[str]:
    if not getattr(config.agents, agent).remote_control:
        return command
    launcher = ["python3", str(CONTAINER_LAUNCHER)] if container else [sys.executable, str(HOST_LAUNCHER)]
    return [*launcher, "_session", agent, *command]


def remote_control_invocation(agent: str, rest: list[str]) -> bool:
    if agent not in {"claude", "codex"} or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    flags = rest[:rest.index("--")] if "--" in rest else rest
    if agent == "claude" and any(arg in _CLAUDE_BACKGROUND_FLAGS for arg in flags):
        return False
    return host_interactive_invocation(agent, rest)


def host_interactive_invocation(agent: str, rest: list[str], *, batch=False) -> bool:
    """Keep batch and management commands direct even at a real terminal."""
    flags = rest[:rest.index("--")] if "--" in rest else rest
    if agent == "claude":
        command = flags
        if command[:1] == ["--model"]:
            command = command[2:]
        elif command and (command[0].startswith("--model=") or command[0] in {
                "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}):
            command = command[1:]
        if command[:1] == ["import"]:
            return False
        return is_claude_session_invocation(rest) and (batch or not any(
            arg in {"-p", "--print", "--dry-run", "--yes"} or
            arg.startswith(("--print=", "--yes=")) for arg in flags))
    if any(arg in {"--help", "-h", "--version", "-V"} for arg in flags):
        return False
    # Skip option values so a model/profile named 'exec' remains interactive.
    values = {"-c", "--config", "-m", "--model", "-p", "--profile", "-C", "--cd",
              "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image",
              "--add-dir", "--enable", "--disable", "--local-provider", "--remote",
              "--remote-auth-token-env"}
    commands = {"agents", "exec", "e", "review", "login", "logout", "mcp", "plugin",
                "app-server", "remote-control", "completion", "update", "doctor", "sandbox",
                "debug", "apply", "a", "queue", "archive", "delete", "migrate-rollouts",
                "unarchive", "cloud", "exec-server", "features", "help"}
    if batch:
        commands -= {"exec", "e", "review"}
    skip = False
    for arg in flags:
        if skip:
            skip = False
        elif arg in values:
            skip = True
        elif not arg.startswith("-"):
            return arg not in commands
    return True


def host_tmux_launch(workspace: Workspace, agent: str, command: list[str],
                     environment: dict[str, str], executable: str) -> int:
    shell = shutil.which("bash")
    if shell is None:
        raise AgentBoxError("host tmux requires bash on PATH; install it or use --no-tmux")
    shell = str(Path(shell).resolve())
    executable = str(Path(executable).resolve())
    root = HOST_SESSIONS_ROOT / workspace_identity(workspace)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    record_workspace_path(workspace_identity(workspace), str(workspace.host_root.resolve()))
    existing = tmux_sessions(None, host_root=root)
    if existing:
        print(f"agent-box: host sessions already running ({', '.join(existing)}); "
              "return with 'agent-box --host attach'", file=sys.stderr)
    name = f"{agent}-{uuid.uuid4().hex[:12]}"
    session = TmuxSession(name, root / f"{name}.status")
    env_path = root / f"{session.name}.env.json"
    if host_k3s.ENV in environment:
        host_k3s.register_tmux(environment[host_k3s.ENV], session.name, root)
    # POSIX environment bytes may be represented by surrogate escapes.
    atomic_write_text(env_path, json.dumps(environment, ensure_ascii=True), 0o600)
    wrapped = session.wrap([sys.executable, str(HOST_SESSION_RUNNER), str(env_path), *command],
                           host_root=root, cwd=workspace.cwd, shell=shell)
    wrapped[0] = executable
    # The server must not retain credentials or source BASH_ENV from its first
    # launcher. The pane runner supplies the full, current per-launch env. The
    # locale is no credential and cannot be dropped: tmux reads UTF-8 support
    # from the *client's* locale and draws "_" for every other character, and
    # the first client is also the server.
    server_env = {"PATH": os.defpath, "TERM": environment.get("TERM", "xterm-256color")}
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if locale_value := environment.get(key):
            server_env[key] = locale_value
    try:
        rc = subprocess.call(wrapped, cwd=str(workspace.cwd), env=host_tmux_env(server_env))
        return session.finish(None, rc, host_root=root)
    finally:
        # A user can detach before the pane has consumed its environment.
        if not tmux_session_exists(None, session.name, host_root=root):
            env_path.unlink(missing_ok=True)


def stop_host_sessions(root: Path) -> list[str]:
    """End the host tmux server of one workspace's sessions; the sessions it held."""
    names = tmux_sessions(None, host_root=root)
    if (root / "browser").exists():
        browser.stop(root.name, "host")
    if not names:
        return []
    result = subprocess.run(tmux_argv("kill-server", host_root=root), capture_output=True, text=True,
                            env=host_tmux_env())
    if result.returncode:
        raise AgentBoxError(f"could not stop host sessions: {result.stderr.strip()}")
    for name in names:
        if re.fullmatch(r"(?:claude|codex)-[0-9a-f]{12}", name):
            (root / f"{name}.status").unlink(missing_ok=True)
            (root / f"{name}.env.json").unlink(missing_ok=True)
    return names


def host_session_main(args: list[str], *, stop: bool = False, workspace_root: str = "") -> int:
    command = "stop" if stop else "attach"
    if (stop and args) or len(args) > 1:
        raise AgentBoxError(f"usage: agent-box --host {command}" + ("" if stop else " [session]"))
    if not stop and not shutil.which("tmux"):
        raise AgentBoxError("tmux not found on the host")
    if not stop and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise AgentBoxError("agent-box --host attach needs a terminal")
    root = HOST_SESSIONS_ROOT / workspace_identity(resolved_workspace(workspace_root))
    if stop:
        workspace = resolved_workspace(workspace_root)
        owned = host_k3s.stop(workspace_runtime_state(workspace).root)
        names = stop_host_sessions(root) if shutil.which("tmux") else []
        names = list(dict.fromkeys([*names, *owned]))
        if not names:
            print("agent-box: no host sessions for this project", file=sys.stderr)
            return 0
        print(f"agent-box: ended {len(names)} host session(s) for this project", file=sys.stderr)
        return 0
    names = tmux_sessions(None, host_root=root)
    name = choose_session(names, args[0] if args else None, location="this project's host server")
    rc = subprocess.call(tmux_argv("attach-session", "-d", "-t", "=" + name, host_root=root),
                         env=host_tmux_env())
    return TmuxSession(name, root / f"{name}.status").finish(None, rc, host_root=root)
