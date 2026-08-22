"""Podman container build/run: the single authoritative place that builds a
``podman run``/``podman build`` command line.

Every local command -- ``agent-box claude``/``codex``/``shell``/``doctor`` --
goes through :func:`base_podman_args`, so there is exactly one definition of
what a container can see. What that is depends on ``agent-box config``: host
credentials are exposed only where the user turned the corresponding switch
on (see :mod:`agent_box.config`).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import tomllib

from . import channel as channel_module
from . import notify as notify_module
from .atomicio import atomic_write_text
from .config import Config, load_config
from .errors import AgentBoxError
from .flow_next import POLICY_ENV_VARS, flow_next_launch_env
from .identity import HostIdentity, detect_host_identity
from .mounts import MountSpec
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


def codex_command(rest: list[str], config: Config) -> list[str]:
    """Build the Codex command, including all container-only config overrides."""
    command = ["codex"]
    if config.notifications.usable:
        command += ["-c", 'notify=["agent-box","notify","codex"]']
    for override in codex_runtime_overrides():
        command += ["-c", override]
    return [*command, "--dangerously-bypass-approvals-and-sandbox", *rest]


def ensure_state_dirs() -> None:
    for path in (
        BUILD_STATE, STATE_ROOT / "cache", STATE_ROOT / "uv", STATE_ROOT / "mise", NOTIFY_STATE_ROOT,
        STATE_ROOT / "git", STATE_ROOT / "gh", CODEX_HOME, CLAUDE_HOME,
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
    return subprocess.run(
        ["podman", "image", "exists", IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


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
            continue  # already covered by the whole-tree mount above
        if host_target in seen:
            continue
        seen.add(host_target)
        mounts.append((host_target, host_target, True))
    return mounts


def build_mounts(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    config: Config | None = None, mount_notifications: bool = True,
) -> list[str]:
    """Build ``--mount`` flags.

    Everything the agent should see is mounted at the *same absolute path*
    it has on the host (see workspace.py's module docstring) -- there is no
    container-specific home or workspace root to translate to, because the
    container's user/home mirror the host's (agent_box.identity).

    Ensures the host-side directories these mounts bind from actually exist
    first, because podman fails a bind mount outright if its source is
    missing.
    """
    from .mounts import MountSpec, plan_mounts

    config = load_config() if config is None else config
    ensure_state_dirs()
    # The project root and any explicit --mount paths can legitimately
    # overlap (a --mount of a parent directory containing the project is
    # the documented way to expose sibling repos); plan_mounts collapses
    # that deterministically instead of issuing two overlapping podman
    # --mount flags for the same tree.
    project_mounts = plan_mounts([MountSpec(workspace.host_root, False, "project root"), *extra_mounts])

    pairs: list[tuple[Path, Path, bool]] = [
        (CODEX_HOME, CODEX_HOME, False),
        (CLAUDE_HOME, CLAUDE_HOME, False),
        # An isolated, container-only cache directory -- deliberately not
        # the host's real ~/.cache -- mounted at the same path the
        # container's XDG_CACHE_HOME env var points at (see
        # base_podman_args) so the two stay consistent.
        (STATE_ROOT / "cache", XDG_CACHE_HOME, False),
        (XDG_CACHE_HOME / "claude-code", XDG_CACHE_HOME / "claude-code", False),
        (XDG_CACHE_HOME / "claude-cli-nodejs", XDG_CACHE_HOME / "claude-cli-nodejs", False),
        (STATE_ROOT / "uv", HOST_HOME / ".local/share/uv", False),
        (STATE_ROOT / "mise", HOST_HOME / ".local/share/mise", False),
        (NOTIFY_STATE_ROOT, HOST_HOME / ".local/share/agent-box/notify", False),
        (script_dir / "agent-box", HOST_HOME / ".local/bin/agent-box", True),
    ]
    if (script_dir / "agent_box").is_dir():
        pairs.append((script_dir / "agent_box", HOST_HOME / ".local/lib/agent-box/agent_box", True))
    # The container never sees config.json. It would carry the bot token into
    # every container even with notifications off, plus host-integration
    # settings the container has no business reading. Instead the host derives
    # a two-key credential file (notify.sync_credentials) and mounts it
    # read-only, and only while notifications are actually usable -- so a
    # disabled feature exposes no token at all rather than an ignored one.
    if mount_notifications:
        notify_module.sync_credentials(config, NOTIFY_CREDENTIALS)
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
    # container, but what goes into them depends on the config. Copies, not
    # bind mounts of the originals, so a container can never write back to the
    # host's real git/gh configuration.
    seed_git_config(STATE_ROOT / "git", config)
    seed_state_dir(
        STATE_ROOT / "gh",
        *((HOST_HOME / ".config/gh",) if config.github.gh_config else ()),
    )
    pairs += [
        (STATE_ROOT / "git", HOST_HOME / ".config/git", False),
        (STATE_ROOT / "gh", HOST_HOME / ".config/gh", False),
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


def provider_env(config: Config) -> list[str]:
    names = (
        *(PROVIDER_API_KEY_ENV if config.providers.api_keys else ()),
        *(PROVIDER_TRANSPORT_ENV if config.providers.transport else ()),
    )
    # --env=NAME (not NAME=value) so a secret never lands in the argv, which
    # is world-readable via /proc on most systems.
    return [f"--env={name}" for name in names if os.environ.get(name)]


def base_podman_args(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    config: Config | None = None, mount_notifications: bool = True,
) -> list[str]:
    """The full ``podman run`` argument list, minus the trailing image/command.

    ``HOST_HOME`` is the passwd-derived home (``paths.passwd_home``), the
    same value ``agent_box.identity`` builds the image's user with and the
    same value ``build_mounts`` binds host state from. ``$HOME`` is never
    consulted, so a poisoned ``$HOME`` cannot move the container's home,
    PATH, or any bind mount.
    """
    config = load_config() if config is None else config
    home = HOST_HOME
    args = [
        "run", "--rm", "--interactive", "--userns=keep-id", *kvm_args(),
        "--cap-drop=all", "--security-opt=no-new-privileges", "--hostname=agent-box",
        f"--workdir={workspace.container_cwd}", f"--env=HOME={home}",
        f"--env=PATH={home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
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
        "--env=DISABLE_AUTOUPDATER=1", f"--env=TERM={os.environ.get('TERM', 'xterm-256color')}",
        *build_mounts(workspace, extra_mounts, script_dir, config, mount_notifications),
    ]
    # A host that relocates Claude Code's config directory must have the
    # container agree: CLAUDE_HOME is already bind-mounted at its host path,
    # but `claude` inside the container would still look in $HOME/.claude
    # without this. Forwarded as the resolved value (not the raw variable) so
    # a tilde or relative setting means the same thing on both sides.
    if os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
        args.append(f"--env=CLAUDE_CONFIG_DIR={CLAUDE_HOME}")
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


def claude_channel_args(
    agent: str, config: Config, workspace: Workspace,
) -> tuple[list[str], list[str]]:
    """Decide whether this Claude session gets the official Telegram channel.

    Three outcomes, none of which stops Claude from starting:

    * not Claude, or the channel is switched off -- nothing to do.
    * enabled but the plugin/token setup is incomplete -- warn and run without
      it. ``agent-box config`` refuses to enable the channel before the
      prerequisites exist, so this only happens if the plugin was uninstalled
      or its token removed afterwards.
    * enabled and another live session already owns the channel -- say so and
      run without it. The bot has one conversation; two sessions answering
      every inbound message is worse than the second one being local-only.

    When the channel *is* taken, outbound notification hooks stand down for
    this container: the channel already relays this session's replies and
    prompts to the paired chat, and the hooks would post the same interaction
    again to the notification chat, which is a different audience.
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

    channel, extra_env = claude_channel_args(agent, config, workspace)
    extra_env = [*extra_env, *flow_next_launch_env(workspace.host_root, agent, config)]

    try:
        mount_notifications = bool(config.notifications.usable and (
            agent == "codex" or (agent == "claude" and not channel)
        ))
        podman = container_argv(
            workspace, extra_mounts, script_dir, [], config, extra_env, mount_notifications,
        )
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

    if agent == "shell":
        os.execvp("podman", [*podman, "bash", "--login", *rest])
    if agent == "doctor":
        os.execvp("podman", [*podman, "bash", "-c", DOCTOR_SCRIPT])
    if agent == "claude":
        os.execvp("podman", [*podman, "claude", "--dangerously-skip-permissions", *channel, *rest])
    os.execvp("podman", [*podman, *codex_command(rest, config)])


def host_main(agent: str, rest: list[str]) -> int:
    """``agent-box --host claude|codex``: run the installed host executable
    directly, no Podman.

    Shares :func:`flow_next.flow_next_launch_env` with the containerized path
    (``runtime_main`` above) so the two cannot drift on Flow-Next detection or
    the FLOW_REVIEW_BACKEND/auto-push policy -- it is the same function call,
    just applied to the host environment instead of ``--env`` flags on a
    ``podman run``. Everything else about host mode is intentionally plain:
    the pass-through args are forwarded unmodified, and the host's normal
    environment (git/gh credentials included) flows through unchanged except
    for this one deliberate override.
    """
    executable = shutil.which(agent)
    if not executable:
        raise AgentBoxError(f"'{agent}' not found on PATH; install it on the host to use --host")
    try:
        workspace = resolve_workspace()
    except (NotADirectoryError, ValueError) as exc:
        raise AgentBoxError(str(exc)) from exc

    config = load_config()
    env = dict(os.environ)
    # A non-Flow-Next launch must not inherit a stale FLOW_REVIEW_BACKEND/
    # auto-push value the parent shell happens to have exported -- clear
    # every agent-box-owned policy variable before applying this launch's
    # actual decision, so an empty result from the resolver really means
    # "no policy", not "whatever was already there".
    for name in POLICY_ENV_VARS:
        env.pop(name, None)
    for pair in flow_next_launch_env(workspace.host_root, agent, config):
        name, _, value = pair.partition("=")
        env[name] = value

    return subprocess.call([executable, *rest], cwd=str(workspace.container_cwd), env=env)
