"""Podman image/workspace lifecycle: the single authoritative place that
builds image, persistent workspace-container, and ``podman exec`` commands.

Every local command -- ``agent-box claude``/``codex``/``shell``/``doctor`` --
goes through :func:`container_lifetime_args`, so there is exactly one
definition of what a container can see. What that is depends on
``agent-box config``: host credentials are exposed only where the user turned
the corresponding switch on (see :mod:`agent_box.config`).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import tomllib

from . import channel as channel_module
from . import notify as notify_module
from .atomicio import atomic_write_text
from .config import Config, load_config
from .errors import AgentBoxError
from .flow_next import AUTO_PUSH_ENV, POLICY_ENV_VARS, flow_next_launch_env
from .identity import HostIdentity, detect_host_identity
from .mounts import MountSpec, plan_mounts
from .notify import SUPPRESS_NOTIFY_ENV
from .paths import (
    BUILD_STATE,
    CLAUDE_HOME,
    CODEX_HOME,
    CONTAINER_NOTIFY_CREDENTIALS,
    HOST_HOME,
    NOTIFY_CREDENTIALS,
    NOTIFY_STATE_ROOT,
    STATE_ROOT,
    TOOLS_REFRESH_FILE,
    XDG_CACHE_HOME,
)
from .workspace import Workspace, resolve_workspace

IMAGE = os.environ.get("AGENT_BOX_IMAGE", "localhost/agent-box:latest")
DEFAULT_AGENT = os.environ.get("AGENT_BOX_AGENT", "claude")
CHATGPT_DESKTOP_RESOURCES = "/usr/lib/chatgpt/resources/"

WORKSPACE_CONTAINER_SCHEMA = "2"
WORKSPACE_CONTAINER_LABEL = "io.agent-box.managed"
WORKSPACE_CONTAINER_WORKSPACE_LABEL = "io.agent-box.workspace"
WORKSPACE_CONTAINER_BASE_CONFIG_LABEL = "io.agent-box.base-config-hash"
WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL = "io.agent-box.extra-mounts-hash"
WORKSPACE_CONTAINER_SCHEMA_LABEL = "io.agent-box.schema"
KEEPER_PATH = HOST_HOME / ".local/lib/agent-box/agent_box/keeper.sh"
CONTAINER_RUNTIME_PATH = (
    f"{HOST_HOME}/.local/share/mise/shims:{HOST_HOME}/.cargo/bin:{HOST_HOME}/.local/bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


def warn(message: str) -> None:
    print(f"agent-box: warning: {message}", file=sys.stderr)


def codex_runtime_overrides() -> list[str]:
    """Disable MCPs that only the host ChatGPT desktop application provides.

    ``CODEX_HOME`` remains the normal bind-mounted home.  These ephemeral
    command-line settings merely stop Codex in the container from trying to
    execute binaries that are not mounted there.
    """
    config_path = CODEX_HOME / "config.toml"
    if not config_path.is_file():
        return []
    try:
        data = tomllib.loads(config_path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        warn(f"could not parse Codex config at {config_path}; not filtering MCP servers")
        return []
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return []

    overrides = []
    for name in sorted(servers):
        server = servers[name]
        command = server.get("command") if isinstance(server, dict) else None
        if not isinstance(name, str) or not isinstance(command, str):
            continue
        if not command.startswith(CHATGPT_DESKTOP_RESOURCES):
            continue
        key = name if name.replace("_", "").replace("-", "").isalnum() else json.dumps(name)
        overrides.append(f"mcp_servers.{key}.enabled=false")
        warn(f"ignoring Codex MCP '{name}' inside container: ChatGPT desktop resource")
    return overrides


def codex_command(rest: list[str], config: Config, flow_next_auto_push_active: bool = False) -> list[str]:
    """Build the Codex command, including all container-only config overrides.

    Codex supports exactly one `notify` program, so it is registered whenever
    *either* outbound Telegram notifications are usable *or* this launch's
    Flow-Next auto-push policy is active (``flow_next_auto_push_active``,
    derived by the caller from ``flow_next_launch_env``'s
    ``AGENT_BOX_FLOW_NEXT_AUTO_PUSH`` signal) -- `agent-box notify codex`
    itself dispatches to both independently (see `agent_box.cli`).
    """
    command = ["codex"]
    if config.notifications.usable or flow_next_auto_push_active:
        command += ["-c", 'notify=["agent-box","notify","codex"]']
    for override in codex_runtime_overrides():
        command += ["-c", override]
    return [*command, "--dangerously-bypass-approvals-and-sandbox", *rest]


def ensure_state_dirs() -> None:
    for path in (
        BUILD_STATE, STATE_ROOT / "cache", STATE_ROOT / "uv", STATE_ROOT / "mise",
        STATE_ROOT / "cargo", STATE_ROOT / "rustup", NOTIFY_STATE_ROOT,
        STATE_ROOT / "runtime/workspaces", CODEX_HOME, CLAUDE_HOME,
        XDG_CACHE_HOME / "claude-code", XDG_CACHE_HOME / "claude-cli-nodejs",
    ):
        path.mkdir(parents=True, exist_ok=True)


def ensure_refresh_token() -> None:
    if not TOOLS_REFRESH_FILE.is_file() or TOOLS_REFRESH_FILE.stat().st_size == 0:
        atomic_write_text(TOOLS_REFRESH_FILE, "initial\n")


def build_image(script_dir: Path, refresh: bool, no_cache: bool, identity: HostIdentity | None = None) -> int:
    ensure_state_dirs()
    ensure_refresh_token()
    identity = identity or detect_host_identity()
    if refresh:
        now = datetime.now(timezone.utc)
        token = now.strftime("%Y%m%dT%H%M%S.") + f"{time.time_ns() % 1_000_000_000:09d}Z\n"
        atomic_write_text(TOOLS_REFRESH_FILE, token)
    args = [
        "podman", "build", "--format=oci", "--pull=always", "--layers",
        "--build-arg", f"USER_NAME={identity.name}",
        "--build-arg", f"USER_UID={identity.uid}",
        "--build-arg", f"USER_GID={identity.gid}",
        "--build-arg", f"USER_HOME={identity.home}",
        "--build-arg", f"TOOLS_REFRESH={TOOLS_REFRESH_FILE.read_text().strip()}",
        "--tag", IMAGE, "--file", str(script_dir / "Containerfile"),
    ]
    if no_cache:
        args.append("--no-cache")
    args.append(str(script_dir))
    return subprocess.call(args)


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


def sync_claude_agent_skills() -> None:
    source = HOST_HOME / ".agents/skills"
    target_root = CLAUDE_HOME / "skills"
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
            except OSError:
                pass
            warn(f"Claude skill '{skill.name}' already points elsewhere; keeping {target}")
        elif target.exists():
            warn(f"Claude skill '{skill.name}' already exists; keeping {target}")
        else:
            target.symlink_to(skill)
    for link in target_root.iterdir():
        if link.is_symlink() and os.readlink(link).startswith(str(source) + "/") and not link.exists():
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
#: container's image always has `gh` on PATH (see README).
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
    (``github_enabled``, which also honors ``AGENT_BOX_GH_TOKEN``), a
    ``credential.https://github.com.helper`` entry is added so a plain
    ``git push`` over HTTPS actually has a usable credential path -- without
    this, forwarding ``GH_TOKEN`` into the container environment alone does
    nothing, because plain git never consults it on its own.
    """
    if config.git.full_config:
        seed_state_dir(target, HOST_HOME / ".config/git", HOST_HOME / ".gitconfig")
    else:
        seed_state_dir(target)
        if config.git.identity:
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


@dataclass(frozen=True)
class WorkspaceRuntimeState:
    """Mutable bind sources owned by one resolved workspace box."""

    root: Path
    git: Path
    gh: Path


def workspace_runtime_state(workspace: Workspace) -> WorkspaceRuntimeState:
    root = STATE_ROOT / "runtime/workspaces" / workspace_identity(workspace)
    return WorkspaceRuntimeState(root=root, git=root / "git", gh=root / "gh")


def legacy_runtime_state() -> WorkspaceRuntimeState:
    """State used only by the retained disposable argv compatibility path."""
    return WorkspaceRuntimeState(
        root=STATE_ROOT,
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
        *((HOST_HOME / ".config/gh",) if config.github.gh_config else ()),
    )
    if mount_notifications:
        notify_module.sync_credentials(config, NOTIFY_CREDENTIALS)


def agent_tree_mounts() -> list[tuple[Path, Path, bool]]:
    """Read-only mounts for ``~/.agents`` and everything it symlinks out to.

    Scope, decided deliberately: the **whole** ``~/.agents`` tree is walked
    recursively, not just ``~/.agents/skills``.

    The reason is that the tree itself is bind-mounted read-only at its own
    path, so *every* symlink in it -- skills, shared agent definitions,
    commands, prompt fragments, whatever a user keeps there -- would dangle
    inside the container unless its target is mounted too. Narrowing the walk
    to ``skills/`` would not make the container see less of the host in any
    meaningful sense; it would just make the non-skill half of an already
    exposed directory silently broken. ``sync_claude_agent_skills`` only cares
    about ``skills/`` because that is the only part Claude Code consumes by
    convention -- that is a different question from what has to resolve.

    Everything is mounted read-only, including external targets: agent-box
    exposes shared agent material to the container, it does not let the
    container edit the user's shared library. Symlinks pointing back inside
    ``~/.agents`` are skipped (the whole-tree mount already covers them), and a
    broken symlink is warned about and skipped rather than failing the run.
    """
    agents = HOST_HOME / ".agents"
    if not agents.is_dir():
        return []
    mounts: list[tuple[Path, Path, bool]] = [(agents, agents, True)]
    seen: set[Path] = set()
    for link in sorted(agents.rglob("*")):
        if not link.is_symlink():
            continue
        try:
            host_target = link.resolve(strict=True)
        except OSError:
            warn(f"broken agent symlink: {link}")
            continue
        if agents in host_target.parents or host_target == agents:
            continue
        if host_target in seen:
            continue
        seen.add(host_target)
        mounts.append((host_target, host_target, True))
    return mounts


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
    runtime_state = runtime_state or legacy_runtime_state()
    project_mounts = plan_mounts([MountSpec(workspace.host_root, False, "project root"), *extra_mounts])

    pairs: list[tuple[Path, Path, bool]] = [
        (CODEX_HOME, CODEX_HOME, False),
        (CLAUDE_HOME, CLAUDE_HOME, False),
        (STATE_ROOT / "cache", XDG_CACHE_HOME, False),
        (XDG_CACHE_HOME / "claude-code", XDG_CACHE_HOME / "claude-code", False),
        (XDG_CACHE_HOME / "claude-cli-nodejs", XDG_CACHE_HOME / "claude-cli-nodejs", False),
        # Shared container-only tool state. Keep Rust's in-container paths
        # canonical while isolating them from the host's real ~/.cargo and
        # ~/.rustup. Multiple projects/toolchains safely share these stores.
        (STATE_ROOT / "uv", HOST_HOME / ".local/share/uv", False),
        (STATE_ROOT / "mise", HOST_HOME / ".local/share/mise", False),
        (STATE_ROOT / "cargo", HOST_HOME / ".cargo", False),
        (STATE_ROOT / "rustup", HOST_HOME / ".rustup", False),
        (NOTIFY_STATE_ROOT, HOST_HOME / ".local/share/agent-box/notify", False),
        (script_dir / "agent-box", HOST_HOME / ".local/bin/agent-box", True),
    ]
    if mount_workspace_runtime_state:
        pairs.append((runtime_state.root, runtime_state.root, False))
    if (script_dir / "agent_box").is_dir():
        pairs.append((script_dir / "agent_box", HOST_HOME / ".local/lib/agent-box/agent_box", True))
    if mount_notifications and NOTIFY_CREDENTIALS.is_file():
        pairs.append((NOTIFY_CREDENTIALS, CONTAINER_NOTIFY_CREDENTIALS, True))
    for claude_json in {HOST_HOME / ".claude.json", CLAUDE_HOME.parent / ".claude.json"}:
        if claude_json.is_file():
            pairs.append((claude_json, claude_json, False))

    pairs += [
        (runtime_state.git, HOST_HOME / ".config/git", False),
        (runtime_state.gh, HOST_HOME / ".config/gh", False),
    ]

    pairs += agent_tree_mounts()

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


def kvm_args() -> list[str]:
    if os.environ.get("AGENT_BOX_KVM", "1") == "0" or not Path("/dev/kvm").exists():
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


def github_enabled(config: Config) -> bool:
    override = os.environ.get("AGENT_BOX_GH_TOKEN", "").strip()
    if override == "1":
        return True
    if override == "0":
        return False
    return config.github.gh_token


def github_env(config: Config) -> list[str]:
    if not github_enabled(config):
        return []
    existing = [n for n in ("GH_TOKEN", "GITHUB_TOKEN") if os.environ.get(n)]
    if existing:
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


PROVIDER_API_KEY_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
PROVIDER_TRANSPORT_ENV = (
    "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "HTTP_PROXY", "HTTPS_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
)


def provider_env(config: Config) -> list[str]:
    names = (
        *(PROVIDER_API_KEY_ENV if config.providers.api_keys else ()),
        *(PROVIDER_TRANSPORT_ENV if config.providers.transport else ()),
    )
    return [f"--env={name}" for name in names if os.environ.get(name)]


def container_lifetime_args(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    config: Config | None = None, mount_notifications: bool = False,
    runtime_state: WorkspaceRuntimeState | None = None,
) -> list[str]:
    config = load_config() if config is None else config
    home = HOST_HOME
    args = [
        "--userns=keep-id", *kvm_args(),
        "--cap-drop=all", "--security-opt=no-new-privileges", "--hostname=agent-box", "--tz=local",
        f"--env=HOME={home}",
        f"--env=PATH={CONTAINER_RUNTIME_PATH}",
        f"--env=CODEX_HOME={CODEX_HOME}",
        f"--env=GIT_CONFIG_GLOBAL={home}/.config/git/config",
        f"--env=GH_CONFIG_DIR={home}/.config/gh",
        f"--env=XDG_CACHE_HOME={XDG_CACHE_HOME}",
        f"--env=UV_CACHE_DIR={XDG_CACHE_HOME}/uv",
        f"--env=BUN_INSTALL_CACHE_DIR={XDG_CACHE_HOME}/bun",
        f"--env=MISE_DATA_DIR={home}/.local/share/mise",
        "--env=DISABLE_AUTOUPDATER=1",
        *build_mounts(workspace, extra_mounts, script_dir, config, mount_notifications, runtime_state),
    ]
    if os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
        args.append(f"--env=CLAUDE_CONFIG_DIR={CLAUDE_HOME}")
    return args


def base_podman_args(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    config: Config | None = None, mount_notifications: bool = True,
) -> list[str]:
    config = load_config() if config is None else config
    runtime_state = legacy_runtime_state()
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
    args = base_podman_args(workspace, extra_mounts, script_dir, config, mount_notifications)
    args += [f"--env={pair}" for pair in (extra_env or [])]
    if sys.stdin.isatty() and sys.stdout.isatty():
        args.append("--tty")
    return ["podman", *args, IMAGE, *command]


def workspace_identity(workspace: Workspace) -> str:
    return hashlib.sha256(str(workspace.host_root.resolve()).encode()).hexdigest()[:16]


def workspace_container_name(workspace: Workspace) -> str:
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


def workspace_container_spec(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path, config: Config,
) -> WorkspaceContainerSpec:
    runtime_state = workspace_runtime_state(workspace)
    lifetime = container_lifetime_args(
        workspace, extra_mounts, script_dir, config, mount_notifications=False, runtime_state=runtime_state,
    )
    base_lifetime = container_lifetime_args(
        workspace, [], script_dir, config, mount_notifications=False, runtime_state=runtime_state,
    )
    extra_mounts_hash = hashlib.sha256(json.dumps([
        (str(mount.path), mount.read_only)
        for mount in plan_mounts(extra_mounts)
    ], separators=(",", ":")).encode()).hexdigest()[:16]
    base_structural = json.dumps({
        "schema": WORKSPACE_CONTAINER_SCHEMA,
        "workspace": str(workspace.host_root.resolve()),
        "image": IMAGE,
        "args": base_lifetime,
    }, separators=(",", ":"), sort_keys=True).encode()
    base_config_hash = hashlib.sha256(base_structural).hexdigest()[:16]
    name = workspace_container_name(workspace)
    labels = [
        f"--label={WORKSPACE_CONTAINER_LABEL}=true",
        f"--label={WORKSPACE_CONTAINER_WORKSPACE_LABEL}={workspace_identity(workspace)}",
        f"--label={WORKSPACE_CONTAINER_SCHEMA_LABEL}={WORKSPACE_CONTAINER_SCHEMA}",
        f"--label={WORKSPACE_CONTAINER_BASE_CONFIG_LABEL}={base_config_hash}",
        f"--label={WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL}={extra_mounts_hash}",
    ]
    return WorkspaceContainerSpec(
        name=name,
        workspace_id=workspace_identity(workspace),
        base_config_hash=base_config_hash,
        extra_mounts_hash=extra_mounts_hash,
        mounts_explicit=bool(extra_mounts),
        create_argv=[
            "podman", "create", "--name", name, "--rm", "--init", *labels, *lifetime,
            IMAGE, "bash", str(KEEPER_PATH), str(workspace_lock_path(workspace)),
        ],
        creation_config=config,
        runtime_state=runtime_state,
    )


@contextmanager
def workspace_transition_lock(workspace: Workspace):
    lock_path = workspace_lock_path(workspace)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def workspace_lock_path(workspace: Workspace) -> Path:
    return workspace_runtime_state(workspace).root / "lifecycle.lock"


def workspace_container_exists(name: str) -> bool:
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


def ensure_workspace_container(spec: WorkspaceContainerSpec) -> None:
    current = inspect_workspace_container(spec.name)
    if current is not None:
        current = _wait_for_exiting_keeper(spec.name, current)
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
            if state == "running" and spec.mounts_explicit:
                raise AgentBoxError("workspace container is already running with different mount configuration")
            if state == "running":
                return
            remove_workspace_container_then_confirm_absent(spec.name)
            current = None
        elif current.get("State", {}).get("Status") == "running":
            return
        else:
            remove_workspace_container_then_confirm_absent(spec.name)
            current = None
    if current is None:
        prepare_container_lifetime_state(
            spec.creation_config, mount_notifications=False, runtime_state=spec.runtime_state,
        )
        created = subprocess.run(spec.create_argv, text=True, capture_output=True)
        if created.returncode:
            winner = inspect_workspace_container(spec.name)
            if winner is None or not _container_matches(winner, spec):
                detail = created.stderr.strip() or "podman could not create workspace container"
                raise AgentBoxError(detail)
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


def parse_top_workloads(output: str) -> list[tuple[int, str]]:
    processes: dict[int, tuple[int, str]] = {}
    for line in output.splitlines()[1:]:
        fields = line.strip().split(None, 2)
        if len(fields) < 3:
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        processes[pid] = (parent, fields[2])

    internal = {2}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _args) in processes.items():
            if parent in internal and pid not in internal:
                internal.add(pid)
                changed = True
    return [(pid, args) for pid, (_parent, args) in processes.items() if pid not in internal and pid != 1]


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
    raise AgentBoxError("workspace container is shutting down; retry the command")


def container_has_workloads(name: str) -> bool | None:
    result = subprocess.run(["podman", "top", name, "pid,ppid,args"], text=True, capture_output=True)
    if result.returncode:
        return None
    return bool(parse_top_workloads(result.stdout))


def cleanup_workspace_container_if_idle(spec: WorkspaceContainerSpec) -> None:
    current = inspect_workspace_container(spec.name)
    if current is None:
        return
    if current.get("State", {}).get("Status") != "running":
        remove_workspace_container_then_confirm_absent(spec.name)
        return
    workloads = container_has_workloads(spec.name)
    if workloads is False:
        remove_workspace_container_then_confirm_absent(spec.name)


def launch_in_workspace_container(
    spec: WorkspaceContainerSpec, workspace: Workspace, command: list[str], extra_env: list[str],
    process_env: dict[str, str] | None = None,
) -> int:
    with workspace_transition_lock(workspace):
        ensure_workspace_container(spec)
        process = subprocess.Popen(exec_argv(spec.name, workspace, command, extra_env), env=process_env)
        while process.poll() is None:
            if container_has_workloads(spec.name) is True:
                break
            time.sleep(0.01)
    rc = process.wait()
    try:
        with workspace_transition_lock(workspace):
            cleanup_workspace_container_if_idle(spec)
    except (AgentBoxError, OSError, subprocess.SubprocessError) as exc:
        warn(f"could not clean up workspace container: {exc}")
    return rc


def invocation_env(config: Config, agent: str) -> tuple[list[str], dict[str, str]]:
    forwarded = [*github_env(config), *provider_env(config)]
    env = dict(os.environ)
    if config.notifications.usable and agent in {"claude", "codex"}:
        env[notify_module.NOTIFY_TOKEN_ENV] = config.notifications.telegram_token
        env[notify_module.NOTIFY_CHAT_ID_ENV] = config.notifications.telegram_chat_id
        forwarded += [
            f"--env={notify_module.NOTIFY_TOKEN_ENV}",
            f"--env={notify_module.NOTIFY_CHAT_ID_ENV}",
        ]
    return [f"TERM={env.get('TERM', 'xterm-256color')}", *(item[6:] for item in forwarded)], env


DOCTOR_SCRIPT = r'''
set -u
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
printf "%-20s %s\n" "yq:" "$(yq --version)"
printf "%-20s %s\n" "kubectl:" "$(kubectl version --client 2>/dev/null | head -1)"
printf "%-20s %s\n" "helm:" "$(helm version --short)"
printf "%-20s %s\n" "qemu-x86:" "$(qemu-system-x86_64 --version | head -1)"
printf "%-20s %s\n" "qemu-arm64:" "$(qemu-system-aarch64 --version | head -1)"
echo
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
  while IFS= read -r -d "" skill; do
    [[ -f "${skill}/SKILL.md" ]] || continue
    name="$(basename -- "${skill}")"; claude_skill="${claude_home}/skills/${name}"
    if [[ ! -f "${claude_skill}/SKILL.md" ]]; then echo "MISSING Claude skill: ${claude_skill}"; broken=1; continue; fi
    agent_real="$(readlink -f -- "${skill}")"; claude_real="$(readlink -f -- "${claude_skill}")"
    if [[ "${agent_real}" != "${claude_real}" ]]; then echo "MISMATCH Claude skill: ${claude_skill} -> ${claude_real} (expected ${agent_real})"; broken=1; continue; fi
    shared=$((shared + 1))
  done < <(find "$HOME/.agents/skills" -mindepth 1 -maxdepth 1 -print0)
  [[ "${broken}" -eq 0 ]] && echo "shared agent skills: OK (${shared})"
fi
'''


CLAUDE_MANAGEMENT_COMMANDS = frozenset({
    "agents", "attach", "auth", "auto-mode", "daemon", "doctor", "gateway", "install",
    "kill", "logs", "mcp", "plugin", "plugins", "project", "remote-control", "rc",
    "respawn", "rm",
    "self-hosted-runner", "setup-token", "stop", "ultrareview", "update", "upgrade",
})

_CLAUDE_MODEL_PREFIX_MANAGEMENT_COMMANDS = frozenset({
    "agents", "auth", "auto-mode", "doctor", "gateway", "install", "mcp", "plugin",
    "plugins", "project", "remote-control", "rc", "setup-token", "ultrareview", "update",
    "upgrade",
})

_CLAUDE_INFORMATION_FLAGS = frozenset({"--help", "-h", "--version", "-v"})
_CLAUDE_BACKGROUND_FLAGS = frozenset({"--bg", "--background"})


def is_claude_non_session_flag_invocation(rest: list[str]) -> bool:
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
    if not rest:
        return True
    if is_claude_non_session_flag_invocation(rest):
        return False
    first = rest[0]
    if first == "--":
        return True
    if not first.startswith("-"):
        return first not in CLAUDE_MANAGEMENT_COMMANDS

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


def claude_session_args(rest: list[str], channel: list[str]) -> list[str]:
    try:
        separator = rest.index("--")
    except ValueError:
        return [*rest, *channel]
    return [*rest[:separator], *channel, *rest[separator:]]


def claude_channel_args(
    agent: str, config: Config, workspace: Workspace, rest: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    if agent != "claude":
        return [], []
    isolated = ["TELEGRAM_BOT_TOKEN="]
    if not is_claude_session_invocation(rest or []):
        return [], isolated
    if not config.claude.telegram_channel:
        return [], isolated
    missing = channel_module.missing_requirements()
    if missing:
        warn("Telegram channel unavailable: " + " and ".join(missing))
        warn("run 'agent-box config check' for setup steps; starting Claude without it")
        return [], isolated
    if not channel_module.acquire_channel(str(workspace.host_root)):
        owner = channel_module.channel_owner()
        detail = f" ({channel_module.describe_owner(owner)})" if owner else ""
        print(f"agent-box: Telegram channel already active in another Claude session{detail}", file=sys.stderr)
        return [], isolated
    return channel_module.channel_flag(), [f"{SUPPRESS_NOTIFY_ENV}=1"]


def runtime_main(args: list[str], script_dir: Path, extra_mount_specs: list[str] | None = None) -> None:
    from .mounts import MountError, parse_mount_arg, plan_mounts

    if not shutil.which("podman"):
        raise AgentBoxError("podman not found")
    if not (script_dir / "Containerfile").is_file():
        raise AgentBoxError(f"Containerfile not found: {script_dir / 'Containerfile'}")
    try:
        workspace = resolve_workspace()
    except (NotADirectoryError, ValueError) as exc:
        raise AgentBoxError(str(exc)) from exc

    try:
        extra_mounts = plan_mounts([parse_mount_arg(spec) for spec in (extra_mount_specs or [])])
    except MountError as exc:
        raise AgentBoxError(str(exc)) from exc

    ensure_state_dirs()
    ensure_refresh_token()
    config = load_config()

    if not image_exists():
        print("agent-box: image not found; building it...", file=sys.stderr)
        rc = build_image(script_dir, False, False)
        if rc:
            raise SystemExit(rc)

    sync_claude_agent_skills()

    agent, rest = DEFAULT_AGENT, args
    if args and args[0] in {"claude", "codex", "shell", "doctor"}:
        agent, rest = args[0], args[1:]
    if agent not in {"claude", "codex", "shell", "doctor"}:
        raise AgentBoxError(f"unsupported agent '{agent}'; expected claude or codex")

    if agent == "claude" and is_claude_session_invocation(rest):
        notify_module.reconcile_claude_notify_hooks(
            CLAUDE_HOME / "settings.json", config.notifications.enabled)

    channel, extra_env = claude_channel_args(agent, config, workspace, rest)
    extra_env = [
        *extra_env,
        *flow_next_launch_env(
            workspace.host_root, agent, config, runtime_path=CONTAINER_RUNTIME_PATH),
    ]

    try:
        if agent == "shell":
            command = ["bash", "--login", *rest]
        elif agent == "doctor":
            command = ["bash", "-c", DOCTOR_SCRIPT]
        elif agent == "claude":
            if is_claude_session_invocation(rest):
                command = ["claude", "--dangerously-skip-permissions", *claude_session_args(rest, channel)]
            else:
                command = ["claude", *rest]
        else:
            flow_next_auto_push_active = any(
                pair.startswith(f"{AUTO_PUSH_ENV}=") for pair in extra_env
            )
            command = codex_command(rest, config, flow_next_auto_push_active)
        spec = workspace_container_spec(workspace, extra_mounts, script_dir, config)
        invocation, process_env = invocation_env(config, agent)
        invocation += extra_env
        rc = launch_in_workspace_container(spec, workspace, command, invocation, process_env)
    except BaseException as exc:
        channel_module.release_channel()
        if isinstance(exc, MountError):
            raise AgentBoxError(str(exc)) from exc
        raise
    channel_module.release_channel()
    raise SystemExit(rc)


def host_main(agent: str, rest: list[str]) -> int:
    executable = shutil.which(agent)
    if not executable:
        raise AgentBoxError(f"'{agent}' not found on PATH; install it on the host to use --host")
    try:
        workspace = resolve_workspace()
    except (NotADirectoryError, ValueError) as exc:
        raise AgentBoxError(str(exc)) from exc

    config = load_config()
    if agent == "claude" and is_claude_session_invocation(rest):
        notify_module.reconcile_claude_notify_hooks(
            CLAUDE_HOME / "settings.json", config.notifications.enabled)
    env = dict(os.environ)
    for name in POLICY_ENV_VARS:
        env.pop(name, None)
    for pair in flow_next_launch_env(
        workspace.host_root, agent, config, runtime_path=env.get("PATH", "")):
        name, _, value = pair.partition("=")
        env[name] = value

    return subprocess.call([executable, *rest], cwd=str(workspace.container_cwd), env=env)
