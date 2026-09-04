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
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import tomllib
import urllib.parse

from . import channel as channel_module
from . import claude_settings
from . import notify as notify_module
from .atomicio import atomic_write_json, atomic_write_text, read_json
from .config import ENV_PREFIX, SUPERPOWERS_SKILLS, Config, load_config
from .errors import AgentBoxError
from .flow_next import AUTO_PUSH_ENV, POLICY_ENV_VARS, flow_next_launch_env
from .identity import HostIdentity, detect_host_identity
from .mounts import MountSpec, plan_mounts
from .notify import SUPPRESS_NOTIFY_ENV
from .paths import (
    AGENT_SKILLS_DIR,
    AGENTS_DIR,
    BUILD_STATE,
    CLAUDE_HOME,
    CODEX_HOME,
    CONFIG_ROOT,
    CONTAINER_NOTIFY_CREDENTIALS,
    CONTAINER_VIKUNJA_CONFIG_DIR,
    CONTAINER_VIKUNJA_STATE_DIR,
    HOST_HOME,
    MANAGED_SKILLS_SOURCE,
    NOTIFY_CREDENTIALS,
    NOTIFY_STATE_ROOT,
    STATE_ROOT,
    TOOLS_REFRESH_FILE,
    VIKUNJA_CREDENTIALS_DIR,
    VIKUNJA_STATE_DIR,
    XDG_CACHE_HOME,
)
from .sessions import tmux_argv, tmux_session_exists, tmux_sessions
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
#: Only these run as a person's live session; ``doctor`` is a diagnostic
#: script whose output belongs on the caller's stdout.
TMUX_AGENTS = frozenset({"claude", "codex", "shell"})
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


def codex_command(
    rest: list[str], config: Config, flow_next_auto_push_active: bool = False,
    browser_session_dir: Path | None = None,
) -> list[str]:
    """Build the Codex command, including all container-only config overrides.

    Codex supports exactly one `notify` program, so it is registered whenever
    *either* outbound Telegram notifications are usable *or* this launch's
    Flow-Next auto-push policy is active (``flow_next_auto_push_active``,
    derived by the caller from ``flow_next_launch_env``'s
    ``AGENT_BOX_FLOW_NEXT_AUTO_PUSH`` signal) -- `agent-box notify codex`
    itself dispatches to both independently (see `agent_box.cli`).

    ``browser_session_dir`` adds the container-native Playwright MCP server
    for this invocation only (see :func:`browser_codex_overrides`). It is
    additive: the ChatGPT desktop MCP filtering below is unaffected, and both
    kinds of override reach Codex through the same ephemeral ``-c`` mechanism
    without ever writing to the user's ``~/.codex/config.toml``.
    """
    command = ["codex"]
    if config.notifications.usable or flow_next_auto_push_active:
        command += ["-c", 'notify=["agent-box","notify","codex"]']
    for override in browser_codex_overrides(browser_session_dir):
        command += ["-c", override]
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


#: Label the image carries, naming the optional tool groups it was built with.
IMAGE_TOOLS_LABEL = "io.agent-box.tools"


def tool_build_args(config: Config) -> list[str]:
    """``--build-arg`` flags selecting the image's optional tool groups.

    Every group is passed explicitly, including the ones that are off, so the
    build never depends on the Containerfile's own defaults agreeing with the
    configuration -- and so turning a group off is as much a decision the
    build layer sees as turning one on.
    """
    args: list[str] = []
    for name, enabled in sorted(config.image.tools.items()):
        args += ["--build-arg", f"TOOLS_{name.upper()}={'1' if enabled else '0'}"]
    args += ["--build-arg", f"AGENT_BOX_TOOLS={','.join(config.image.enabled_groups())}"]
    return args


def build_image(script_dir: Path, refresh: bool, no_cache: bool,
                identity: HostIdentity | None = None,
                config: Config | None = None) -> int:
    ensure_state_dirs()
    ensure_refresh_token()
    identity = identity or detect_host_identity()
    config = load_config() if config is None else config
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
        *tool_build_args(config),
        "--tag", IMAGE, "--file", str(script_dir / "Containerfile"),
    ]
    if no_cache:
        args.append("--no-cache")
    args.append(str(script_dir))
    return subprocess.call(args)


def image_tool_groups() -> list[str] | None:
    """The groups the built image carries, or ``None`` when it cannot be read.

    ``None`` is not "no groups": an image that predates the label, or a
    Podman that cannot be asked, must not be reported as missing everything.
    """
    command = ["podman", "image", "inspect", "--format",
               f"{{{{index .Labels \"{IMAGE_TOOLS_LABEL}\"}}}}", IMAGE]
    try:
        result = subprocess.run(command, text=True, capture_output=True)
    except OSError:
        return None
    if result.returncode:
        return None
    value = result.stdout.strip()
    if value in ("", "<no value>"):
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
    wanted = config.image.enabled_groups()
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
        data = read_json(target / MANAGED_SKILL_MARKER)
    except (OSError, ValueError):
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
    if target.exists() or target.is_symlink():
        recorded = managed_skill_digest(target)
        if recorded is None:
            warn(f"skill '{name}' already exists and is not managed by agent-box; keeping {target}")
            return
        if recorded == digest:
            return
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    atomic_write_json(target / MANAGED_SKILL_MARKER, {"skill": name, "digest": digest})


def remove_managed_skill(name: str) -> None:
    """Remove a skill agent-box installed; never one the user owns."""
    target = AGENT_SKILLS_DIR / name
    if managed_skill_digest(target) is not None:
        shutil.rmtree(target, ignore_errors=True)


def managed_skills(config: Config) -> set[str]:
    """The shipped skills the current configuration asks for."""
    wanted: set[str] = set()
    if config.vikunja.enabled:
        wanted.add("vikunja")
    if config.skills.superpowers:
        wanted.update(SUPERPOWERS_SKILLS)
    return wanted


def sync_managed_skills(config: Config) -> None:
    """Bring the shared tree in line with what the configuration enables."""
    wanted = managed_skills(config)
    for name in sorted(wanted):
        install_managed_skill(name)
    if not MANAGED_SKILLS_SOURCE.is_dir():
        return
    for source in sorted(MANAGED_SKILLS_SOURCE.iterdir()):
        if source.is_dir() and source.name not in wanted and (source / "SKILL.md").is_file():
            remove_managed_skill(source.name)


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

    if not config.skills.claude_links:
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
            except OSError:
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
    if config.vikunja.enabled:
        # Create the import-state source ourselves so Podman does not, which
        # keeps ownership and mode the user's rather than whatever the bind
        # would invent.
        VIKUNJA_STATE_DIR.mkdir(parents=True, exist_ok=True)
        # The credential directory is rendered rather than created empty: an
        # empty one would mount fine and look configured while holding no
        # token, which is exactly the failure this integration used to have.
        sync_vikunja_credentials(config)
        VIKUNJA_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        for problem in vikunja_warnings(config):
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
#: The Vikunja skill's CLI, inside the shared agent tree agent-box installs it
#: into. That tree is already bind-mounted (see :func:`agent_tree_mounts`), so
#: the second, narrow bind below adds no new exposure -- it only gives the
#: script a name on the container PATH, which is how the skill's own
#: documentation tells an agent to invoke it.
VIKUNJA_SKILL_ENTRYPOINT = AGENT_SKILLS_DIR / "vikunja/scripts/vikunja.py"
#: Where the container's copy of that CLI is named.
CONTAINER_VIKUNJA_BIN = HOST_HOME / ".local/bin/vikunja"


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


def container_vikunja_url(raw: str) -> str:
    """Rewrite a Vikunja address that has no name worth preserving.

    A self-hosted Vikunja is almost always configured on the host as
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
    the host keeps its URL and is redirected by :func:`vikunja_host_args`
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


def sync_vikunja_credentials(config: Config, directory: Path | None = None) -> None:
    """Render, or delete, the credential pair the container's skill reads.

    The same shape as ``notify.sync_credentials`` and for the same reason:
    agent-box's own ``config.json`` holds switches and secrets the container
    has no business reading, so what gets mounted is a minimal artifact
    derived from it -- here the two files the Vikunja CLI actually opens,
    ``config.json`` (``url``) and ``token``. An incomplete or disabled
    integration deletes them, so the credential stops being reachable rather
    than merely being ignored.

    The URL written here is the *container's* view of it
    (:func:`container_vikunja_url`), which is why this is a render step
    rather than a copy: the value the user configured stays the host's, and
    the rewrite that makes it reachable from inside a box never leaks back
    into their settings.
    """
    directory = VIKUNJA_CREDENTIALS_DIR if directory is None else directory
    if not config.vikunja.usable:
        shutil.rmtree(directory, ignore_errors=True)
        return
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    atomic_write_json(
        directory / "config.json",
        {"url": container_vikunja_url(config.vikunja.url)},
        stat.S_IRUSR | stat.S_IWUSR,
    )
    atomic_write_text(
        directory / "token", config.vikunja.token + "\n", stat.S_IRUSR | stat.S_IWUSR)


def vikunja_installed() -> bool:
    """Whether the skill's CLI is present in the shared agent tree."""
    try:
        return VIKUNJA_SKILL_ENTRYPOINT.resolve(strict=True).is_file()
    except OSError:
        return False


def vikunja_mounts(config: Config) -> list[tuple[Path, Path, bool]]:
    """What the container gets for Vikunja, or nothing at all.

    Pure and silent by design: this is part of the box's structural identity
    (see :func:`workspace_container_spec`), so it must depend on the config
    switch alone and must not print a warning every time an attach recomputes
    the identity. Diagnostics live in :func:`vikunja_warnings`, which runs
    only where a person is actually waiting on the answer.

    Destinations are the literal ``$HOME`` paths the skill's CLI resolves
    from ``~``, not the host's XDG-derived sources, so a host that relocates
    ``XDG_CONFIG_HOME`` still lands the configuration where the CLI looks.

    The rendered credential directory is read-only: it carries an API token,
    and an agent has no reason to rewrite the user's credentials. The import
    state directory is read-write, because a partially applied
    ``vikunja import`` is only resumable if its journal survives the box.
    """
    if not config.vikunja.enabled:
        return []
    mounts = [
        (VIKUNJA_CREDENTIALS_DIR, CONTAINER_VIKUNJA_CONFIG_DIR, True),
        (VIKUNJA_STATE_DIR, CONTAINER_VIKUNJA_STATE_DIR, False),
    ]
    if vikunja_installed():
        mounts.append((VIKUNJA_SKILL_ENTRYPOINT.resolve(), CONTAINER_VIKUNJA_BIN, True))
    return mounts


def vikunja_host_alias(raw: str) -> str:
    """The Vikunja hostname that must be pointed back at the host, or ``""``.

    Only a DNS name qualifies. Loopback names and address literals are
    already handled by :func:`container_vikunja_url`, and a name that
    resolves anywhere but this machine is reachable from the container as it
    stands.
    """
    url = raw.strip()
    if not url:
        return ""
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return ""
    if not host or host.lower() in LOOPBACK_HOSTNAMES or is_ip_literal(host):
        return ""
    return host if is_host_local(host) else ""


def vikunja_host_args(config: Config) -> list[str]:
    """``--add-host=NAME:host-gateway`` for a host-local Vikunja hostname.

    A self-hosted Vikunja is often addressed by a LAN name such as
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
    if not config.vikunja.enabled:
        return []
    host = vikunja_host_alias(config.vikunja.url)
    return [f"--add-host={host}:{CONTAINER_HOST_GATEWAY}"] if host else []


def vikunja_warnings(config: Config) -> list[str]:
    """Human-facing problems with an enabled Vikunja integration.

    Returned rather than printed so both the launch path and
    ``agent-box config`` can render them, and so neither has to guess which
    of the two halves (skill, credentials) is missing.
    """
    if not config.vikunja.enabled:
        return []
    problems = []
    if not config.vikunja.url:
        problems.append("Vikunja integration is on but no server URL is configured")
    if not config.vikunja.token:
        problems.append("Vikunja integration is on but no API token is configured")
    if not vikunja_installed():
        problems.append(
            f"Vikunja integration is on but its skill is not installed at "
            f"{AGENT_SKILLS_DIR / 'vikunja'}")
    return problems


# -- container-native browser (Playwright MCP + headless Chromium) ----------
#
# Architecture, deliberately the whole of it:
#
#     Claude / Codex  --stdio MCP-->  playwright-mcp  -->  headless Chromium
#
# All three run inside the workspace container that is already there. There is
# no sidecar, no Podman pod, no HTTP MCP endpoint, no host browser and no
# X11/Wayland socket -- and no host browser profile or cookie jar is exposed,
# because none is mounted.
#
# Enablement is *invocation-scoped*, never part of the box's structural
# identity: `playwright-mcp` and Chromium are baked into the image, the
# generated MCP configuration lives under the workspace runtime state that is
# already bind-mounted, and nothing new is added to `container_lifetime_args`.
# So turning `browser.enabled` on or off never destroys an otherwise
# compatible running workspace container.

#: The MCP server name both agents see. Stable so a user can name it in
#: allow/deny lists, and prefixed so it cannot collide with a server the user
#: configured themselves.
BROWSER_MCP_SERVER_NAME = "agent_box_browser"
#: Installed globally by the Containerfile's Playwright MCP layer.
BROWSER_MCP_EXECUTABLE = "playwright-mcp"
#: Playwright MCP's default browser is the *branded Chrome channel*
#: (``/opt/google/chrome/chrome``), which this image does not ship and which
#: would fail at the first navigation. The bundled Chromium that
#: ``playwright install chromium`` downloaded has to be selected explicitly.
BROWSER_MCP_BROWSER = "chromium"
#: How ``agent-box doctor`` learns whether the integration is switched on: the
#: script runs inside the box, which never sees the host config file.
BROWSER_ENABLED_ENV = "AGENT_BOX_BROWSER"
#: Whether this configuration links shared skills into Claude's own skills
#: directory. Forwarded to ``agent-box doctor`` for the same reason as the
#: browser flag: the script runs inside the box and cannot read the host
#: config, and without it a deliberately unlinked tree would be reported as
#: broken.
CLAUDE_SKILL_LINKS_ENV = "AGENT_BOX_CLAUDE_SKILL_LINKS"
#: The optional tool groups this configuration asks the image to carry, so
#: ``agent-box doctor`` checks only what was meant to be there.
IMAGE_TOOLS_ENV = "AGENT_BOX_TOOLS"


def browser_session_id(agent: str) -> str:
    """A name for this launch's browser state.

    Same shape as :func:`tmux_session`'s name and for the same reason: the
    launcher's PID is unique among one workspace's live launches. Derived
    independently of tmux because a piped, non-terminal launch
    (``claude -p ...``) gets no tmux session but still needs its own browser
    profile and output directory -- two concurrent sessions in one box must
    never share a writable Playwright profile.
    """
    return f"{agent}-{os.getpid()}"


def browser_session_root(runtime_state: WorkspaceRuntimeState) -> Path:
    """Where every browser session of one workspace keeps its state."""
    return runtime_state.root / "browser"


def browser_mcp_args(session_dir: Path) -> list[str]:
    """The Playwright MCP argv shared by the Claude and Codex wiring.

    ``--isolated`` is what keeps a session non-persistent: Playwright MCP
    holds the browser profile in memory and never writes it to disk, so
    cookies, localStorage and any credential a page hands out are gone when
    the MCP process exits and cannot leak into an unrelated later session.

    ``--output-dir`` keeps generated artifacts (snapshots, screenshots,
    traces, downloads) out of the mounted project: without it Playwright MCP
    writes them relative to the working directory, which is the user's repo.

    Neither ``--sandbox`` nor ``--no-sandbox`` is passed: Playwright already
    launches its own bundled Chromium with ``--no-sandbox``, and forcing
    Chromium's internal sandbox back on with Playwright MCP's ``--sandbox``
    simply fails inside an agent-box container -- it needs a nested user
    namespace and privileges that ``--cap-drop=all``,
    ``--security-opt=no-new-privileges`` and rootless ``--userns=keep-id``
    deliberately do not grant. agent-box does not weaken any of those flags
    to get it, because Chromium's own sandbox is not the boundary that
    matters here: the container is (see the trust-boundary note in the
    README). Nothing in this argv adds a Linux capability.

    The one thing the environment does need is a writable ``$HOME/.config``
    for Chromium's crash-reports database, which the image provides -- see
    the Containerfile's browser layer. Without it Chromium dies at startup
    with ``chrome_crashpad_handler: --database is required``.
    """
    return [
        BROWSER_MCP_EXECUTABLE,
        "--browser", BROWSER_MCP_BROWSER,
        "--headless",
        "--isolated",
        "--output-dir", str(session_dir / "output"),
    ]


def _toml_string(value: str) -> str:
    r"""A TOML basic string for a ``codex -c`` override.

    ``json.dumps`` is exactly the right escaper here: every escape a JSON
    string can produce (``\"``, ``\\``, ``\n``, ``\uXXXX``) is also a valid
    TOML basic-string escape, and Codex parses the value portion of ``-c`` as
    TOML.
    """
    return json.dumps(value)


def browser_codex_overrides(session_dir: Path | None) -> list[str]:
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
    command, *args = browser_mcp_args(session_dir)
    table = (
        f"command={_toml_string(command)}, "
        f"args=[{', '.join(_toml_string(arg) for arg in args)}]"
    )
    return [f"mcp_servers.{BROWSER_MCP_SERVER_NAME}={{{table}}}"]


def browser_claude_args(session_dir: Path | None) -> list[str]:
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
    command, *args = browser_mcp_args(session_dir)
    config_path = session_dir / "mcp.json"
    atomic_write_json(config_path, {
        "mcpServers": {BROWSER_MCP_SERVER_NAME: {"command": command, "args": args}},
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
    if not config.browser.enabled or agent not in {"claude", "codex"}:
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
    runtime_state = runtime_state or legacy_runtime_state()
    # The project root and any explicit --mount paths can legitimately
    # overlap (a --mount of a parent directory containing the project is
    # the documented way to expose sibling repos); plan_mounts collapses
    # that deterministically instead of issuing two overlapping podman
    # --mount flags for the same tree.
    project_mounts = plan_mounts([MountSpec(workspace.host_root, False, "project root"), *extra_mounts])

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
        (STATE_ROOT / "cargo", HOST_HOME / ".cargo", False),
        (STATE_ROOT / "rustup", HOST_HOME / ".rustup", False),
        # Notification hook delivery state is intentionally host-global so
        # duplicate suppression and rate tracking work across agent sessions.
        (NOTIFY_STATE_ROOT, HOST_HOME / ".local/share/agent-box/notify", False),
        (script_dir / "agent-box", HOST_HOME / ".local/bin/agent-box", True),
    ]
    if mount_workspace_runtime_state:
        # The keeper needs only this workspace's lifecycle lock and snapshots,
        # never another workspace's writable runtime state.
        pairs.append((runtime_state.root, runtime_state.root, False))
    if (script_dir / "agent_box").is_dir():
        pairs.append((script_dir / "agent_box", HOST_HOME / ".local/lib/agent-box/agent_box", True))
    # The container never sees config.json. The legacy disposable argv helper
    # may request a derived two-key credential file; persistent workspace
    # boxes deliberately pass ``mount_notifications=False`` and use per-exec
    # forwarding instead, because a shared bind mount would expose it to every
    # concurrent process in that box.
    if mount_notifications and NOTIFY_CREDENTIALS.is_file():
        pairs.append((NOTIFY_CREDENTIALS, CONTAINER_NOTIFY_CREDENTIALS, True))
    # Claude Code's ~/.claude.json sits *beside* the config directory rather
    # than inside it. When CLAUDE_CONFIG_DIR relocates that directory, the
    # relocated tree is already mounted whole (CLAUDE_HOME above), so only the
    # default home location needs its own mount.
    for claude_json in {HOST_HOME / ".claude.json", CLAUDE_HOME.parent / ".claude.json"}:
        if claude_json.is_file():
            pairs.append((claude_json, claude_json, False))

    # Both directories are always mounted so git and gh work inside the
    # container. Their source contents are prepared separately at box
    # creation, never while merely calculating an attach spec.
    pairs += [
        (runtime_state.git, HOST_HOME / ".config/git", False),
        (runtime_state.gh, HOST_HOME / ".config/gh", False),
    ]

    pairs += agent_tree_mounts()
    pairs += vikunja_mounts(config)

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


def kvm_args(config: Config) -> list[str]:
    """``/dev/kvm`` passthrough, when there is something in the box to use it.

    Tied to the ``virtualization`` tool group: without QEMU in the image the
    device is a bind with no consumer, and handing a container a device it has
    no use for is not a neutral default.
    """
    if not config.image.tools.get("virtualization"):
        return []
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
    """Whether a GitHub credential may reach the container at all.

    Being logged in to ``gh`` on the host is not consent, and neither is
    having ``GH_TOKEN`` exported in the launching shell -- an agent container
    is a different trust boundary from your interactive shell, and a variable
    that happens to be in the environment is not a decision about it. So the
    config switch gates *both* the host-derived token and any already-exported
    one.

    ``AGENT_BOX_GH_TOKEN`` overrides for a single run: ``1`` allows, ``0``
    denies. Anything else is ignored rather than guessed at.
    """
    override = os.environ.get("AGENT_BOX_GH_TOKEN", "").strip()
    if override == "1":
        return True
    if override == "0":
        return False
    return config.github.gh_token


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

    Two spellings, matching the two halves of ``config.env``. A forwarded
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
    assignments = config.env.set
    names = [
        name for name in config.env.forward
        if name not in assignments and os.environ.get(name)
    ]
    return [
        *(f"--env={name}" for name in names),
        *(f"--env={name}={value}" for name, value in sorted(assignments.items())),
    ]


def provider_env(config: Config) -> list[str]:
    names = (
        *(PROVIDER_API_KEY_ENV if config.providers.api_keys else ()),
        *(PROVIDER_TRANSPORT_ENV if config.providers.transport else ()),
    )
    # --env=NAME (not NAME=value) so a secret never lands in the argv, which
    # is world-readable via /proc on most systems.
    return [f"--env={name}" for name in names if os.environ.get(name)]


def container_lifetime_args(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    config: Config | None = None, mount_notifications: bool = False,
    runtime_state: WorkspaceRuntimeState | None = None,
) -> list[str]:
    """Container-level arguments shared by workspace creation and legacy argv tests.

    ``HOST_HOME`` is the passwd-derived home (``paths.passwd_home``), the
    same value ``agent_box.identity`` builds the image's user with and the
    same value ``build_mounts`` binds host state from. ``$HOME`` is never
    consulted, so a poisoned ``$HOME`` cannot move the container's home,
    PATH, or any bind mount.
    """
    config = load_config() if config is None else config
    home = HOST_HOME
    args = [
        "--userns=keep-id", *kvm_args(config),
        "--cap-drop=all", "--security-opt=no-new-privileges", "--hostname=agent-box", "--tz=local",
        f"--env=HOME={home}",
        f"--env=PATH={CONTAINER_RUNTIME_PATH}",
        f"--env=CODEX_HOME={CODEX_HOME}",
        f"--env=GIT_CONFIG_GLOBAL={home}/.config/git/config",
        f"--env=GH_CONFIG_DIR={home}/.config/gh",
        f"--env=XDG_CACHE_HOME={XDG_CACHE_HOME}",
        f"--env=UV_CACHE_DIR={XDG_CACHE_HOME}/uv",
        # The official Telegram channel plugin runs its MCP server under Bun
        # and installs its dependencies on first start; keep that download in
        # the container-only cache so it survives across disposable runs.
        f"--env=BUN_INSTALL_CACHE_DIR={XDG_CACHE_HOME}/bun",
        f"--env=MISE_DATA_DIR={home}/.local/share/mise",
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
        *vikunja_host_args(config),
    ]
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
    """Full ``["podman", ...]`` argv for one disposable container run."""
    args = base_podman_args(workspace, extra_mounts, script_dir, config, mount_notifications)
    args += [f"--env={pair}" for pair in (extra_env or [])]
    if sys.stdin.isatty() and sys.stdout.isatty():
        args.append("--tty")
    return ["podman", *args, IMAGE, *command]


# -- workspace container lifecycle -----------------------------------------

def workspace_identity(workspace: Workspace) -> str:
    """Stable opaque identity for a resolved workspace root."""
    return hashlib.sha256(str(workspace.host_root.resolve()).encode()).hexdigest()[:16]


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


def workspace_container_spec(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path, config: Config,
) -> WorkspaceContainerSpec:
    """The immutable box definition; deliberately excludes per-exec secrets/policy."""
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
    # Extra binds are the only structural setting that an omitted later
    # --mount may deliberately inherit. Derive the base identity from an
    # independently rendered no-extra box rather than subtracting flags from
    # the flattened argv: an explicit parent mount can subsume the project
    # mount during Podman mount planning.
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
    """Short advisory lock for create/exec/idle-delete transition windows.

    The keeper and host flock separate open file descriptions of the same
    lock inode.  An accidental unlink does not release either already-held
    flock, although a trusted workspace peer that replaces the pathname can defeat future lock
    acquisition.  Workspace-box processes are trusted peers; preserving the
    simple shared lock is preferable to adding a second lifecycle mechanism.
    """
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
    """Create/start a compatible workspace box, recovering stopped stale boxes."""
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
            # Omitting --mount means join an existing box, even if it was
            # originally created with extra paths. Explicit mounts are exact.
            if state == "running" and spec.mounts_explicit:
                raise AgentBoxError("workspace container is already running with different mount configuration")
            if state == "running":
                return
            remove_workspace_container_then_confirm_absent(spec.name)
            current = None
        elif current.get("State", {}).get("Status") == "running":
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
        prepare_container_lifetime_state(
            spec.creation_config, mount_notifications=False, runtime_state=spec.runtime_state,
        )
        created = subprocess.run(spec.create_argv, text=True, capture_output=True)
        if created.returncode:
            # Podman's name uniqueness is the final arbiter when two first
            # launches race (including across different agent-box versions).
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


def tmux_enabled(agent: str) -> bool:
    """Whether this launch gets a detachable session.

    Only a real terminal is wrapped.  A piped or captured run (``claude -p``,
    CI, a redirected ``agent-box shell -c``) must keep writing straight to the
    caller's stdout, where that output is the entire point, and has no
    terminal to be detached from in the first place.
    """
    if agent not in TMUX_AGENTS or os.environ.get("AGENT_BOX_TMUX", "1") == "0":
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

    def wrap(self, command: list[str]) -> list[str]:
        # tmux reports its own exit status, not the wrapped program's, and
        # returns 0 for a detach as well as for a finished session. The inner
        # shell records the real status in the workspace's runtime state --
        # which is bind-mounted at the same path on both sides -- so a
        # launcher that is still alive can exit with it. Its absence is how
        # `finish` tells a detached session from a finished one.
        recorder = 'status=$1; shift; "$@"; printf %s "$?" >"$status"'
        return tmux_argv(
            "new-session", "-s", self.name, "--",
            "bash", "-c", recorder, "agent-box-session", str(self.status_path), *command,
        )

    def recorded_status(self) -> int | None:
        try:
            recorded = self.status_path.read_text().strip()
        except OSError:
            return None
        finally:
            self.status_path.unlink(missing_ok=True)
        return int(recorded) if recorded.isdigit() else None

    def finish(self, container: str, rc: int) -> int:
        """Translate the tmux client's exit into the launch's own exit."""
        recorded = self.recorded_status()
        if recorded is not None:
            return recorded
        if rc in {126, 127}:
            warn("this workspace image has no tmux; run 'agent-box build'")
            return rc
        if tmux_session_exists(container, self.name):
            print(
                f"agent-box: session '{self.name}' is still running in this workspace's box; "
                f"return to it with 'agent-box attach'",
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


def parse_top_workloads(output: str) -> list[tuple[int, str]]:
    """Return processes outside the init/keeper process subtree.

    The keeper occasionally needs helper children (for example ``flock`` or a
    delay). Excluding its *tree* is structural and cannot hide an unrelated
    user ``sleep``/daemon merely because it has a familiar command name.
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
        ensure_workspace_container(spec)
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
    except (AgentBoxError, OSError, subprocess.SubprocessError) as exc:
        # The agent command's status is more meaningful than a best-effort
        # post-exit cleanup problem. A later launch will recover stale
        # stopped/idle containers automatically.
        warn(f"could not clean up workspace container: {exc}")
    # Never retry an exec command: after Popen starts, its provider command
    # may already have performed side effects even if Podman returns 125.
    return rc


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
    if config.notifications.usable and agent in {"claude", "codex"}:
        env[notify_module.NOTIFY_TOKEN_ENV] = config.notifications.telegram_token
        env[notify_module.NOTIFY_CHAT_ID_ENV] = config.notifications.telegram_chat_id
        forwarded += [
            f"--env={notify_module.NOTIFY_TOKEN_ENV}",
            f"--env={notify_module.NOTIFY_CHAT_ID_ENV}",
        ]
    # `--env=NAME` means inherit NAME from the local podman client. Convert
    # the existing helpers' argv spelling into exec_argv's compact form.
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
# Only the tool groups this configuration asked for are checked. A group left
# out is a choice, and reporting it as missing would turn every deliberate
# omission into a fault.
has_group() { case ",${AGENT_BOX_TOOLS:-}," in *",$1,"*) return 0;; *) return 1;; esac; }

printf "%-20s %s\n" "tool groups:" "${AGENT_BOX_TOOLS:-(none)}"
if has_group analysis; then
  printf "%-20s %s\n" "shellcheck:" "$(shellcheck --version | sed -n 's/^version: //p')"
  printf "%-20s %s\n" "jscpd:" "$(jscpd --version 2>/dev/null || echo MISSING)"
fi
if has_group kubernetes; then
  printf "%-20s %s\n" "yq:" "$(yq --version)"
  printf "%-20s %s\n" "kubectl:" "$(kubectl version --client 2>/dev/null | head -1)"
  printf "%-20s %s\n" "helm:" "$(helm version --short)"
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
# addressing the host from inside a container (runtime.container_vikunja_url
# for an address, runtime.vikunja_host_args for a name) is the part most
# likely to be wrong. A failure therefore reports what the name resolved to,
# which is the difference between "the server is down" and "that address is
# this container".
if command -v vikunja >/dev/null 2>&1; then
  # The URL is rendered into the mounted credential directory by
  # runtime.sync_vikunja_credentials rather than passed as an environment
  # variable, so that the address and the token cannot disagree. Read it back
  # from the same file the skill's CLI reads.
  vikunja_url="$(sed -n 's/.*"url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      "$HOME/.config/vikunja/config.json" 2>/dev/null)"
  printf "%-20s %s\n" "vikunja url:" "${vikunja_url:-(not configured)}"
  printf "%-20s %s\n" "vikunja token:" "$([[ -r "$HOME/.config/vikunja/token" ]] && echo present || echo MISSING)"
  if status="$(vikunja status 2>&1)"; then
    printf "%-20s %s\n" "vikunja:" "OK ($(printf "%s" "${status}" | tr -d " \n" | sed -n 's/.*"user":"\([^"]*\)".*/\1/p'))"
  else
    printf "%-20s %s\n" "vikunja:" "FAILED"
    printf "  %s\n" "${status}"
    host="$(printf "%s" "${vikunja_url}" | sed -n 's|^[a-zA-Z0-9+.-]*://||; s|^[^@]*@||; s|[/?#].*$||; s|:[0-9]*$||; p')"
    if [[ -n "${host}" ]] && command -v getent >/dev/null 2>&1; then
      printf "  %s\n" "${host} resolves to: $(getent hosts "${host}" | awk '{print $1}' | paste -sd, - || echo "(nothing)")"
      printf "  %s\n" "this container is: $(hostname -I 2>/dev/null | tr ' ' ',' | sed 's/,$//')"
    fi
  fi
else
  echo "vikunja:             not exposed (enable it with 'agent-box config')"
fi
echo
# The browser integration is off by default, so "disabled" is a normal answer
# here rather than a fault. When it is on, the question worth answering is not
# "is playwright-mcp on PATH" but "can a headless Chromium actually start",
# which is what the smoke test proves -- bounded by its own 20s timeout and a
# hard one here, and entirely offline (it renders a data: URL).
if has_group browser; then
  printf "%-20s %s\n" "playwright-mcp:" "$(playwright-mcp --version 2>/dev/null || echo MISSING)"
fi
if [[ "${AGENT_BOX_BROWSER:-0}" == "1" ]]; then
  if ! has_group browser; then
    echo "browser chromium:    MISSING (the browser tool group is off; turn it on in"
    echo "                     'agent-box config' and run 'agent-box build')"
  elif ! command -v agent-box-browser-smoke >/dev/null 2>&1; then
    echo "browser chromium:    MISSING (image built without it; run 'agent-box build')"
  elif smoke="$(timeout 30 agent-box-browser-smoke 2>&1)"; then
    printf "%-20s %s\n" "browser chromium:" "OK (${smoke})"
  else
    echo "browser chromium:    FAILED"
    printf "  %s\n" "${smoke}"
  fi
  echo "browser integration: enabled"
else
  echo "browser integration: disabled (enable it with 'agent-box config')"
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
    if not config.claude.telegram_channel:
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
    args: list[str], script_dir: Path, extra_mount_specs: list[str] | None = None,
    env_specs: list[tuple[str, str | None]] | None = None,
) -> None:
    from .mounts import MountError, parse_mount_arg, plan_mounts

    if not shutil.which("podman"):
        raise AgentBoxError("podman not found")
    if not (script_dir / "Containerfile").is_file():
        raise AgentBoxError(f"Containerfile not found: {script_dir / 'Containerfile'}")
    if not shutil.which("git"):
        # Not fatal, but the failure it causes is invisible: workspace
        # resolution falls back to the current directory, so the box quietly
        # gets a subdirectory of the project instead of the repository root.
        warn("git not found on the host: the workspace root falls back to the "
             "current directory instead of the repository root")
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
    # Per-invocation environment beats the stored lists, and is resolved once
    # here so container and host mode cannot disagree about the answer.
    config.env = config.env.overlay(env_specs)

    if not image_exists():
        print("agent-box: image not found; building it...", file=sys.stderr)
        rc = build_image(script_dir, False, False, config=config)
        if rc:
            raise SystemExit(rc)
    else:
        # Reported, never acted on: a rebuild is slow, and choosing when to
        # spend that time is the user's call, not a launch's.
        missing, extra = image_tool_drift(config)
        if missing:
            warn(f"image lacks configured tool group(s): {', '.join(missing)}"
                 f" -- run 'agent-box build'")
        if extra:
            warn(f"image still carries tool group(s) you turned off: {', '.join(extra)}"
                 f" -- run 'agent-box build' to drop them")


    agent, rest = DEFAULT_AGENT, args
    if args and args[0] in {"claude", "codex", "shell", "doctor"}:
        agent, rest = args[0], args[1:]
    if agent not in {"claude", "codex", "shell", "doctor"}:
        raise AgentBoxError(f"unsupported agent '{agent}'; expected claude or codex")

    # Nothing here reconciles Claude's settings, installs a skill or writes a
    # symlink: those belong to 'agent-box config' and 'agent-box config apply'
    # (see apply_config). A launch reads the configuration; it does not apply it.

    # Decided before the channel is claimed, because the claim records it.
    runtime_state = workspace_runtime_state(workspace)
    session = tmux_session(agent, runtime_state) if tmux_enabled(agent) else None
    # Invocation-scoped, and deliberately not part of the box's structural
    # identity: everything it needs is already in the image and already
    # mounted, so flipping browser.enabled never recreates a compatible box.
    browser_dir = browser_session(config, workspace, runtime_state, agent)
    channel, extra_env = claude_channel_args(agent, config, workspace, rest, session)
    extra_env = [
        *extra_env,
        *flow_next_launch_env(
            workspace.host_root, agent, config, runtime_path=CONTAINER_RUNTIME_PATH),
    ]

    try:
        if agent == "shell":
            command = ["bash", "--login", *rest]
        elif agent == "doctor":
            # The script runs inside the box and cannot read the host config,
            # so the one bit it needs is forwarded per-exec.
            extra_env = [
                *extra_env,
                f"{BROWSER_ENABLED_ENV}={'1' if config.browser.enabled else '0'}",
                f"{CLAUDE_SKILL_LINKS_ENV}={'1' if config.skills.claude_links else '0'}",
                f"{IMAGE_TOOLS_ENV}={','.join(config.image.enabled_groups())}",
            ]
            command = ["bash", "-c", DOCTOR_SCRIPT]
        elif agent == "claude":
            if is_claude_session_invocation(rest):
                command = [
                    "claude", "--dangerously-skip-permissions",
                    *claude_session_args(rest, channel, browser_claude_args(browser_dir)),
                ]
            else:
                command = ["claude", *rest]
        else:
            flow_next_auto_push_active = any(
                pair.startswith(f"{AUTO_PUSH_ENV}=") for pair in extra_env
            )
            command = codex_command(rest, config, flow_next_auto_push_active, browser_dir)
        spec = workspace_container_spec(workspace, extra_mounts, script_dir, config)
        invocation, process_env = invocation_env(config, agent)
        # Channel/Flow-Next values are invocation policy, not box identity.
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


def resolved_workspace() -> Workspace:
    try:
        return resolve_workspace()
    except (NotADirectoryError, ValueError) as exc:
        raise AgentBoxError(str(exc)) from exc


def running_workspace_container(workspace: Workspace) -> str:
    name = workspace_container_name(workspace)
    current = inspect_workspace_container(name)
    if current is None or current.get("State", {}).get("Status") != "running":
        raise AgentBoxError("no running agent-box container for this workspace")
    return name


def choose_session(sessions: list[str], wanted: str | None) -> str:
    if not sessions:
        raise AgentBoxError("this workspace's box has no detachable sessions")
    if wanted is not None:
        if wanted not in sessions:
            raise AgentBoxError(f"no session '{wanted}' in this workspace's box")
        return wanted
    if len(sessions) == 1:
        return sessions[0]
    listing = "\n".join(f"  {name}" for name in sessions)
    raise AgentBoxError("this workspace's box holds several sessions; name one:\n" + listing)


def attach_main(args: list[str]) -> int:
    """``agent-box attach [session]``: take back a session from a dead terminal."""
    if len(args) > 1:
        raise AgentBoxError("usage: agent-box attach [session]")
    if not shutil.which("podman"):
        raise AgentBoxError("podman not found")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise AgentBoxError("agent-box attach needs a terminal")
    workspace = resolved_workspace()
    name = running_workspace_container(workspace)
    session = choose_session(tmux_sessions(name), args[0] if args else None)
    # ``-d`` evicts whichever client still holds the session. After a closed
    # terminal that client is a ghost: conmon keeps its pty open, so it never
    # reads EOF and would otherwise keep the session to itself forever.
    command = tmux_argv("attach-session", "-d", "-t", session)
    with workspace_transition_lock(workspace):
        # The box is holding a live tmux server, so unlike a first launch
        # there is no idle window here for the keeper to shut down.
        process = subprocess.Popen(exec_argv(name, workspace, command, []))
    rc = process.wait()
    try:
        with workspace_transition_lock(workspace):
            cleanup_workspace_container_if_idle(name)
    except (AgentBoxError, OSError, subprocess.SubprocessError) as exc:
        warn(f"could not clean up workspace container: {exc}")
    return rc


def stop_main(args: list[str]) -> int:
    """``agent-box stop``: end this workspace's box, detached sessions included.

    Ordinary boxes still disappear on their own once no workload remains. A
    detached session *is* a workload, so a box someone walked away from needs
    a way to be shut down deliberately.
    """
    if args:
        raise AgentBoxError("agent-box stop takes no arguments")
    if not shutil.which("podman"):
        raise AgentBoxError("podman not found")
    workspace = resolved_workspace()
    name = workspace_container_name(workspace)
    with workspace_transition_lock(workspace):
        current = inspect_workspace_container(name)
        if current is None:
            print("agent-box: no workspace container for this project", file=sys.stderr)
            return 0
        if current.get("State", {}).get("Status") == "running":
            try:
                sessions = tmux_sessions(name)
            except AgentBoxError:
                sessions = []
            if sessions:
                print(f"agent-box: ending {len(sessions)} session(s): {', '.join(sessions)}", file=sys.stderr)
        remove_workspace_container_then_confirm_absent(name)
        runtime_state = workspace_runtime_state(workspace)
        shutil.rmtree(runtime_state.root / "sessions", ignore_errors=True)
        # The box is gone, so no browser session of this workspace can still
        # be running; its snapshots and screenshots go with it.
        remove_browser_sessions(runtime_state)
    print(f"agent-box: removed workspace container {name}", file=sys.stderr)
    return 0


def apply_config(config: Config) -> list[str]:
    """Make the host match the configuration, and say what that took.

    The one place anything agent-box owns *outside its own directories* is
    written: the skills it ships, the links that let Claude read the shared
    tree, and the status line and hooks in Claude's ``settings.json``. Its own
    state -- credential renders under ``~/.local/share/agent-box`` -- is
    refreshed here too, but that part is also safe to redo at launch, because
    it is agent-box's to manage.

    Launching an agent deliberately calls none of this. A command that starts
    Claude should be safe to run without wondering what it will rewrite in
    your home directory, and "when did that skill appear?" should have one
    answer: when you asked for it. The cost is that a newer agent-box does not
    refresh its shipped skills until this runs again, which is why
    ``agent-box upgrade`` ends by calling it and ``agent-box config apply``
    exists for a hand-edited file.

    Returns the human-facing lines describing what happened, so every caller
    renders the same report instead of each narrating its own.
    """
    from . import flow_push
    report: list[str] = []

    ensure_state_dirs()
    notify_module.sync_credentials(config)
    sync_vikunja_credentials(config)

    wanted = managed_skills(config)
    sync_managed_skills(config)
    report.append(
        f"skills installed in {AGENT_SKILLS_DIR}: {', '.join(sorted(wanted)) or 'none'}")

    claude_skill_links(config)
    report.append(
        f"shared skills linked into {CLAUDE_HOME / 'skills'}: "
        f"{'yes' if config.skills.claude_links else 'no'}")

    settings = CLAUDE_HOME / "settings.json"
    statusline = claude_settings.statusline_intent(config.claude.statusline)
    claude_settings.reconcile_claude_settings(
        settings, notifications=config.notifications.enabled, statusline=statusline)
    if config.flow_next.auto_push:
        flow_push.install_flow_push_hooks(settings)
    else:
        flow_push.remove_flow_push_hooks(settings)
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
    rendered credential files, the symlink it put on PATH -- and stops the
    boxes holding those mounts open. ``--purge`` additionally deletes
    agent-box's own configuration and state, which is where the Telegram and
    Vikunja tokens live; that one is irreversible and is confirmed unless
    ``--yes`` says not to.

    The PATH line in a shell startup file is the exception on both levels: it
    is removed only under ``--purge``, and even then only when it still
    matches byte for byte what agent-box wrote. An edited block is somebody's
    own line by now, and a stray ``export PATH`` costs nothing to leave --
    deleting the wrong two lines of a login shell's startup file costs a lot.
    Everything not removed is reported with its location.

    Neither level removes the container image or this source checkout: both
    are large, both are trivially re-derived, and neither is something a
    program should delete without being asked for it by name.
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

    done: list[str] = []

    names = managed_container_names() if shutil.which("podman") else []
    for name in names:
        remove_workspace_container_then_confirm_absent(name)
    if names:
        done.append(f"removed {len(names)} workspace box(es)")

    settings = CLAUDE_HOME / "settings.json"
    from . import flow_push
    if claude_settings.reconcile_claude_settings(
            settings, notifications=False, statusline=False):
        done.append(f"removed the agent-box status line and notification hooks from {settings}")
    if flow_push.remove_flow_push_hooks(settings):
        done.append(f"removed the Flow-Next auto-push hooks from {settings}")

    disabled = Config()
    disabled.skills.claude_links = False
    claude_skill_links(disabled)
    done.append(f"removed agent-box's skill links from {CLAUDE_HOME / 'skills'}")

    sync_managed_skills(disabled)
    done.append(f"removed the skills agent-box installed in {AGENT_SKILLS_DIR}")

    NOTIFY_CREDENTIALS.unlink(missing_ok=True)
    shutil.rmtree(VIKUNJA_CREDENTIALS_DIR, ignore_errors=True)
    done.append("removed the rendered notification and Vikunja credentials")

    from . import pathsetup
    for link in pathsetup.remove_symlinks():
        done.append(f"removed the symlink {link}")
    rc = pathsetup.rc_file()
    rc_note: str | None = None
    if rc is not None:
        state = pathsetup.rc_state(rc)
        if state.kind != "absent":
            rc_note = (f"{rc} lines {state.span[0]}-{state.span[1]}"
                       if state.span else str(rc))

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
            shutil.rmtree(CONFIG_ROOT, ignore_errors=True)
            shutil.rmtree(STATE_ROOT, ignore_errors=True)
            done.append(f"deleted {CONFIG_ROOT} and {STATE_ROOT}")
            if rc is not None and rc_note:
                removed, note = pathsetup.remove_rc_block(rc)
                if removed:
                    done.append(note)
                    rc_note = None
                elif note:
                    done.append(note)
                    rc_note = None

    for line in done:
        print(f"agent-box: {line}")
    print()
    print("Remaining, deliberately left for you:")
    if not purge:
        print(f"  configuration and state   {CONFIG_ROOT}, {STATE_ROOT}")
    if rc_note:
        print(f"  shell PATH line           {rc_note}")
    print(f"  container image           podman rmi {IMAGE}")
    print(f"  the agent-box checkout    rm -rf {pathsetup.checkout_root()}")
    return 0


def host_main(
    agent: str, rest: list[str],
    env_specs: list[tuple[str, str | None]] | None = None,
) -> int:
    """``agent-box --host claude|codex``: run the installed host executable
    directly, no Podman.

    Shares :func:`flow_next.flow_next_launch_env` with the containerized path
    (``runtime_main`` above) so the two cannot drift on Flow-Next detection or
    the Flow-Next launch policy -- it is the same function call,
    just applied to the host environment instead of ``--env`` flags on a
    ``podman exec``. Everything else about host mode is intentionally plain:
    the pass-through args are forwarded unmodified, and the host's normal
    environment (git/gh credentials included) flows through unchanged except
    for the invocation-scoped Flow-Next overrides.
    """
    executable = shutil.which(agent)
    if not executable:
        raise AgentBoxError(f"'{agent}' not found on PATH; install it on the host to use --host")
    try:
        workspace = resolve_workspace()
    except (NotADirectoryError, ValueError) as exc:
        raise AgentBoxError(str(exc)) from exc

    config = load_config()
    config.env = config.env.overlay(env_specs)
    # As in container mode: a launch applies nothing to the user's own files.
    env = dict(os.environ)
    # A non-Flow-Next launch must not inherit stale Flow-Next policy values
    # the parent shell happens to have exported -- clear every agent-box-owned
    # policy variable, then let flow_next_launch_env
    # re-derive the decision (including an explicit FLOW_REVIEW_BACKEND the
    # shell set for this one launch, which it reads from the real process
    # environment), so an empty result from the resolver really means
    # "no policy", not "whatever was already there".
    for name in POLICY_ENV_VARS:
        env.pop(name, None)
    for pair in flow_next_launch_env(
        workspace.host_root, agent, config, runtime_path=env.get("PATH", "")):
        name, _, value = pair.partition("=")
        env[name] = value
    # ``env.set`` states what a session should have; a host session is still a
    # session. ``env.forward`` has nothing to do here -- host mode already
    # runs in the very shell those names would have been forwarded from, so
    # the values are present already.
    env.update(config.env.set)
    # agent-box's own request channel is not part of the session it describes.
    for key in [k for k in env if k.startswith(ENV_PREFIX)]:
        del env[key]

    return subprocess.call([executable, *rest], cwd=str(workspace.container_cwd), env=env)
