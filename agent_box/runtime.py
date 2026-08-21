"""Podman container build/run: the single authoritative place that builds a
``podman run``/``podman build`` command line.

Both interactive local use (``agent-box claude``/``codex``/``shell``) and the
Telegram remote-control daemon's machine-mode sessions go through
:func:`base_podman_args` here, so they share one environment/mount/hardening
model instead of the daemon re-approximating a second one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .atomicio import atomic_write_json, atomic_write_text, read_json
from .errors import AgentBoxError
from .identity import HostIdentity, detect_host_identity
from .mounts import MountSpec
from .paths import (
    BUILD_STATE, CLAUDE_HOME, CODEX_HOME, HOST_HOME, NOTIFY_CONFIG,
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


def migrate_statusline() -> None:
    path = CLAUDE_HOME / "settings.json"
    if not path.is_file():
        return
    try:
        settings = read_json(path)
        status = settings.get("statusLine")
        if isinstance(status, dict) and status.get("command") in {
            "~/.local/bin/claude-usage --statusline",
            "~/.local/bin/agent-box usage claude --statusline",
            "~/.local/bin/agent-box usage all --statusline",
        }:
            status["command"] = "agent-box usage all --statusline"
            atomic_write_json(path, settings)
    except (OSError, ValueError, AttributeError):
        pass


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


def build_mounts(workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path) -> list[str]:
    """Build ``--mount`` flags.

    Everything the agent should see is mounted at the *same absolute path*
    it has on the host (see workspace.py's module docstring) -- there is no
    container-specific home or workspace root to translate to, because the
    container's user/home mirror the host's (agent_box.identity).

    Ensures the host-side directories these mounts bind from actually exist
    first: podman fails a bind mount outright if its source is missing, and
    unlike interactive local runs (whose caller always calls
    ``ensure_state_dirs`` first), Telegram machine-mode sessions have no
    other call site that's guaranteed to run before this.
    """
    from .mounts import MountSpec, plan_mounts

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
    if NOTIFY_CONFIG.is_file():
        pairs.append((NOTIFY_CONFIG, HOST_HOME / ".config/agent-box/telegram.json", True))
    if (HOST_HOME / ".claude.json").is_file():
        pairs.append((HOST_HOME / ".claude.json", HOST_HOME / ".claude.json", False))

    seed_state_dir(STATE_ROOT / "git", HOST_HOME / ".config/git", HOST_HOME / ".gitconfig")
    seed_state_dir(STATE_ROOT / "gh", HOST_HOME / ".config/gh")
    pairs += [
        (STATE_ROOT / "git", HOST_HOME / ".config/git", False),
        (STATE_ROOT / "gh", HOST_HOME / ".config/gh", False),
    ]

    agents = HOST_HOME / ".agents"
    if agents.is_dir():
        pairs.append((agents, agents, True))
        seen: set[tuple[str, str]] = set()
        for link in agents.rglob("*"):
            if not link.is_symlink():
                continue
            try:
                host_target = link.resolve(strict=True)
            except OSError:
                warn(f"broken agent symlink: {link}")
                continue
            if agents in host_target.parents or host_target == agents:
                continue  # already covered by the whole-tree mount above
            key = (str(host_target), str(host_target))
            if key not in seen:
                seen.add(key)
                pairs.append((host_target, host_target, True))

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


def maybe_gh_token() -> None:
    if os.environ.get("AGENT_BOX_GH_TOKEN", "1") == "0" or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or not shutil.which("gh"):
        return
    r = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True)
    token = r.stdout.strip() if r.returncode == 0 else ""
    if token:
        os.environ["GH_TOKEN"] = token
    else:
        warn("no GitHub token on the host; run 'gh auth login' outside the container")


def base_podman_args(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path, *,
    include_kvm: bool = True,
) -> list[str]:
    """The full ``podman run`` argument list, minus the trailing image/command.

    Shared by interactive local runs and the Telegram daemon's machine-mode
    provider subprocesses, so both get the identical mount/env/hardening
    model -- there is exactly one place that decides what the container can
    see.

    Uses ``HOST_HOME`` (this process's real home, as opposed to
    ``agent_box.identity``'s build-time snapshot) since that's what
    ``build_mounts`` actually binds host state from; the two coincide except
    in the rare case a rebuild happens under a different UID than the one
    currently running.
    """
    home = HOST_HOME
    args = [
        "run", "--rm", "--interactive", "--userns=keep-id",
        *(kvm_args() if include_kvm else []),
        "--cap-drop=all", "--security-opt=no-new-privileges", "--hostname=agent-box",
        f"--workdir={workspace.container_cwd}", f"--env=HOME={home}",
        f"--env=PATH={home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"--env=CODEX_HOME={CODEX_HOME}",
        f"--env=GIT_CONFIG_GLOBAL={home}/.config/git/config",
        f"--env=GH_CONFIG_DIR={home}/.config/gh",
        f"--env=XDG_CACHE_HOME={XDG_CACHE_HOME}",
        f"--env=UV_CACHE_DIR={XDG_CACHE_HOME}/uv",
        f"--env=MISE_DATA_DIR={home}/.local/share/mise",
        "--env=DISABLE_AUTOUPDATER=1", f"--env=TERM={os.environ.get('TERM', 'xterm-256color')}",
        *build_mounts(workspace, extra_mounts, script_dir),
    ]
    maybe_gh_token()
    for name in (
        "GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
        "HTTP_PROXY", "HTTPS_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
    ):
        if os.environ.get(name):
            args.append(f"--env={name}")
    return args


def interactive_podman_args(workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path) -> list[str]:
    args = base_podman_args(workspace, extra_mounts, script_dir)
    if sys.stdin.isatty() and sys.stdout.isatty():
        args.append("--tty")
    return args


def machine_podman_argv(
    workspace: Workspace, extra_mounts: list[MountSpec], script_dir: Path, command: list[str],
) -> list[str]:
    """Full ``["podman", ...]`` argv for a piped (non-tty) machine-mode session.

    Used by the Telegram daemon to run Claude/Codex *inside* the same Podman
    environment as normal ``agent-box`` usage, over stdin/stdout, instead of
    on the bare host.
    """
    args = base_podman_args(workspace, extra_mounts, script_dir, include_kvm=False)
    return ["podman", *args, IMAGE, *command]


DOCTOR_SCRIPT = r'''
set -u
printf "%-20s %s\n" "architecture:" "$(uname -m)"
printf "%-20s %s\n" "user:" "$(id)"
printf "%-20s %s\n" "workdir:" "$PWD"
printf "%-20s %s\n" "claude:" "$(claude --version)"
printf "%-20s %s\n" "codex:" "$(codex --version)"
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
if [[ -d "$HOME/.agents/skills" ]]; then
  broken=0; shared=0
  while IFS= read -r -d "" link; do
    if [[ ! -e "${link}" ]]; then echo "BROKEN agent skill: ${link} -> $(readlink "${link}")"; broken=1; fi
  done < <(find "$HOME/.agents/skills" -maxdepth 1 -type l -print0)
  while IFS= read -r -d "" skill; do
    [[ -f "${skill}/SKILL.md" ]] || continue
    name="$(basename -- "${skill}")"; claude_skill="$HOME/.claude/skills/${name}"
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
    migrate_statusline()
    ensure_refresh_token()

    if not image_exists():
        print("agent-box: image not found; building it...", file=sys.stderr)
        rc = build_image(script_dir, False, False)
        if rc:
            raise SystemExit(rc)

    sync_claude_agent_skills()
    try:
        podman = interactive_podman_args(workspace, extra_mounts, script_dir)
    except MountError as exc:
        raise AgentBoxError(str(exc)) from exc

    if args and args[0] == "shell":
        os.execvp("podman", ["podman", *podman, IMAGE, "bash", "--login", *args[1:]])
    if args and args[0] == "doctor":
        os.execvp("podman", ["podman", *podman, IMAGE, "bash", "-c", DOCTOR_SCRIPT])

    agent, rest = DEFAULT_AGENT, args
    if args and args[0] in {"claude", "codex"}:
        agent, rest = args[0], args[1:]
    if agent == "claude":
        cmd = ["podman", *podman, IMAGE, "claude", "--dangerously-skip-permissions", *rest]
    elif agent == "codex":
        notify = ["-c", 'notify=["agent-box","notify","codex"]'] if NOTIFY_CONFIG.is_file() else []
        cmd = ["podman", *podman, IMAGE, "codex", *notify, "--dangerously-bypass-approvals-and-sandbox", *rest]
    else:
        raise AgentBoxError(f"unsupported agent '{agent}'; expected claude or codex")
    os.execvp("podman", cmd)
