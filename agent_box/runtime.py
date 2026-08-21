"""Podman container build/run: the single authoritative place that builds a
``podman run``/``podman build`` command line.

Every local command -- ``agent-box claude``/``codex``/``shell``/``doctor`` --
goes through :func:`base_podman_args`, so there is exactly one definition of
what a container can see. What that is depends on ``agent-box config``: host
credentials are exposed only where the user turned the corresponding switch
on (see :mod:`agent_box.config`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .atomicio import atomic_write_text
from .channel import channel_args
from .config import Config, load_config
from .errors import AgentBoxError
from .identity import HostIdentity, detect_host_identity
from .mounts import MountSpec
from .notify import SUPPRESS_NOTIFY_ENV
from .paths import (
    BUILD_STATE, CLAUDE_HOME, CODEX_HOME, CONFIG_FILE, HOST_HOME,
    NOTIFY_STATE_ROOT, STATE_ROOT, TOOLS_REFRESH_FILE, XDG_CACHE_HOME,
)
from .workspace import Workspace, resolve_workspace

IMAGE = os.environ.get("AGENT_BOX_IMAGE", "localhost/agent-box:latest")
DEFAULT_AGENT = os.environ.get("AGENT_BOX_AGENT", "claude")


def warn(message: str) -> None:
    print(f"agent-box: warning: {message}", file=sys.stderr)


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
    config: Config | None = None,
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
    # Read-only: the notification hooks run inside the container and need the
    # bot token to send, but nothing in there may rewrite the user's config.
    # The destination is the *default* XDG location rather than the host's
    # own CONFIG_FILE path, because agent-box inside the container gets no
    # AGENT_BOX_CONFIG_DIR/XDG_CONFIG_HOME and so looks exactly there. A host
    # that relocates its config directory would otherwise mount the file at a
    # path the container never reads.
    if CONFIG_FILE.is_file():
        pairs.append((CONFIG_FILE, HOST_HOME / ".config/agent-box/config.json", True))
    # Claude Code's ~/.claude.json sits *beside* the config directory rather
    # than inside it. When CLAUDE_CONFIG_DIR relocates that directory, the
    # relocated tree is already mounted whole (CLAUDE_HOME above), so only the
    # default home location needs its own mount.
    for claude_json in {HOST_HOME / ".claude.json", CLAUDE_HOME.parent / ".claude.json"}:
        if claude_json.is_file():
            pairs.append((claude_json, claude_json, False))

    # Both directories are always mounted so git and gh work inside the
    # container, but they are only *seeded* from the host when the user opted
    # in. Seeding with no sources leaves an empty 0700 directory: git falls
    # back to its built-in defaults, gh runs unauthenticated. Copies, not bind
    # mounts of the originals, so a container can never write back to the
    # host's real git/gh configuration.
    seed_state_dir(
        STATE_ROOT / "git",
        *((HOST_HOME / ".config/git", HOST_HOME / ".gitconfig") if config.git.config else ()),
    )
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


def maybe_gh_token(config: Config) -> None:
    """Forward a host GitHub token, but only when explicitly configured.

    Being logged in to ``gh`` on the host is NOT consent to hand a live
    GitHub credential to every container -- an earlier agent-box treated it
    as though it were. The token now travels only when
    ``github.gh_token`` is on in ``agent-box config``.

    ``AGENT_BOX_GH_TOKEN`` still overrides for a single command: ``0``
    withholds the token from a configured-on host, ``1`` forwards it from a
    configured-off one.
    """
    override = os.environ.get("AGENT_BOX_GH_TOKEN", "").strip()
    enabled = config.github.gh_token
    if override == "0":
        enabled = False
    elif override == "1":
        enabled = True
    if not enabled or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return
    if not shutil.which("gh"):
        warn("github.gh_token is enabled but 'gh' is not installed on the host")
        return
    r = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True)
    token = r.stdout.strip() if r.returncode == 0 else ""
    if token:
        os.environ["GH_TOKEN"] = token
    else:
        warn("github.gh_token is enabled but the host has no token; run 'gh auth login'")


#: Provider credential/endpoint variables forwarded from the invoking shell
#: when ``providers.env`` is on. Only ones already exported for this command
#: are passed; agent-box never reads them from a file.
PROVIDER_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "HTTP_PROXY", "HTTPS_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
)


def base_podman_args(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    config: Config | None = None,
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
        *build_mounts(workspace, extra_mounts, script_dir, config),
    ]
    # A host that relocates Claude Code's config directory must have the
    # container agree: CLAUDE_HOME is already bind-mounted at its host path,
    # but `claude` inside the container would still look in $HOME/.claude
    # without this. Forwarded as the resolved value (not the raw variable) so
    # a tilde or relative setting means the same thing on both sides.
    if os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
        args.append(f"--env=CLAUDE_CONFIG_DIR={CLAUDE_HOME}")
    maybe_gh_token(config)
    forwarded = ("GH_TOKEN", "GITHUB_TOKEN", *(PROVIDER_ENV if config.providers.env else ()))
    for name in forwarded:
        if os.environ.get(name):
            args.append(f"--env={name}")
    return args


def container_argv(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path,
    command: list[str], config: Config | None = None, extra_env: list[str] | None = None,
) -> list[str]:
    """Full ``["podman", ...]`` argv for one disposable container run."""
    args = base_podman_args(workspace, extra_mounts, script_dir, config)
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

    if agent == "claude" and config.claude.telegram_channel:
        # Resolved before the container starts so a missing plugin or token is
        # an actionable error here, not a session that looks fine and never
        # reaches Telegram.
        channel = channel_args()
        # The official channel relays this session's replies, prompts and
        # questions to the paired chat itself. Outbound notification hooks
        # would post the same interaction a second time, to a chat the user
        # picked for a different purpose, so they stand down for this run.
        extra_env = [f"{SUPPRESS_NOTIFY_ENV}=1"]
    else:
        channel, extra_env = [], []

    try:
        podman = container_argv(workspace, extra_mounts, script_dir, [], config, extra_env)
    except MountError as exc:
        raise AgentBoxError(str(exc)) from exc

    if agent == "shell":
        os.execvp("podman", [*podman, "bash", "--login", *rest])
    if agent == "doctor":
        os.execvp("podman", [*podman, "bash", "-c", DOCTOR_SCRIPT])
    if agent == "claude":
        os.execvp("podman", [*podman, "claude", "--dangerously-skip-permissions", *channel, *rest])
    notify = ["-c", 'notify=["agent-box","notify","codex"]'] if config.notifications.usable else []
    os.execvp("podman", [*podman, "codex", *notify, "--dangerously-bypass-approvals-and-sandbox", *rest])
