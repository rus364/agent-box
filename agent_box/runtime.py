"""Podman container build/run: mounts, environment, KVM, GitHub token passthrough."""
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
from .paths import (
    BUILD_STATE, CLAUDE_HOME, CODEX_HOME, CONTAINER_HOME, CONTAINER_USER,
    HOST_HOME, NOTIFY_CONFIG, NOTIFY_STATE_ROOT, STATE_ROOT, TOOLS_REFRESH_FILE,
    XDG_CACHE_HOME,
)
from .workspace import Workspace, resolve_workspace

IMAGE = os.environ.get("AGENT_BOX_IMAGE", "localhost/agent-box:latest")
DEFAULT_AGENT = os.environ.get("AGENT_BOX_AGENT", "claude")
HOST_UID = os.getuid()
HOST_GID = os.getgid()


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


def build_image(script_dir: Path, refresh: bool, no_cache: bool) -> int:
    ensure_state_dirs()
    ensure_refresh_token()
    if refresh:
        now = datetime.now(timezone.utc)
        token = now.strftime("%Y%m%dT%H%M%S.") + f"{time.time_ns() % 1_000_000_000:09d}Z\n"
        atomic_write_text(TOOLS_REFRESH_FILE, token)
    args = [
        "podman", "build", "--format=oci", "--pull=always", "--layers",
        "--build-arg", f"USER_NAME={CONTAINER_USER}",
        "--build-arg", f"USER_UID={HOST_UID}",
        "--build-arg", f"USER_GID={HOST_GID}",
        "--build-arg", f"TOOLS_REFRESH={TOOLS_REFRESH_FILE.read_text().strip()}",
        "--tag", IMAGE, "--file", str(script_dir / "Containerfile"),
    ]
    if no_cache:
        args.append("--no-cache")
    args.append(str(script_dir))
    return subprocess.call(args)


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


def build_mounts(workspace: Workspace, script_dir: Path) -> list[str]:
    c = CONTAINER_HOME
    pairs: list[tuple[Path, Path, bool]] = [
        (workspace.host_root, workspace.container_root, False),
        (CODEX_HOME, c / ".codex", False),
        (CLAUDE_HOME, c / ".claude", False),
        (STATE_ROOT / "cache", c / ".cache", False),
        (XDG_CACHE_HOME / "claude-code", c / ".cache/claude-code", False),
        (XDG_CACHE_HOME / "claude-cli-nodejs", c / ".cache/claude-cli-nodejs", False),
        (STATE_ROOT / "uv", c / ".local/share/uv", False),
        (STATE_ROOT / "mise", c / ".local/share/mise", False),
        (NOTIFY_STATE_ROOT, c / ".local/share/agent-box/notify", False),
        (script_dir / "agent-box", c / ".local/bin/agent-box", True),
    ]
    if (script_dir / "agent_box").is_dir():
        pairs.append((script_dir / "agent_box", c / ".local/lib/agent-box/agent_box", True))
    if NOTIFY_CONFIG.is_file():
        pairs.append((NOTIFY_CONFIG, c / ".config/agent-box/telegram.json", True))
    if (HOST_HOME / ".claude.json").is_file():
        pairs.append((HOST_HOME / ".claude.json", c / ".claude.json", False))

    seed_state_dir(STATE_ROOT / "git", HOST_HOME / ".config/git", HOST_HOME / ".gitconfig")
    seed_state_dir(STATE_ROOT / "gh", HOST_HOME / ".config/gh")
    pairs += [(STATE_ROOT / "git", c / ".config/git", False), (STATE_ROOT / "gh", c / ".config/gh", False)]

    # ~/.agents is mounted at its literal host path (not remapped under the
    # container home) because host-authored symlinks under it -- including
    # the ones agent-box itself writes into ~/.claude/skills -- are absolute
    # host paths. The container's home directory name is neutral/portable,
    # but these symlink targets are not, so they only resolve if the host
    # path itself exists inside the container too.
    agents = HOST_HOME / ".agents"
    if agents.is_dir():
        pairs.append((agents, agents, True))
        if agents != c / ".agents":
            # Also mount at $HOME/.agents so Codex (which looks relative to
            # its own $HOME inside the container) discovers skills too.
            pairs.append((agents, c / ".agents", True))
        seen: set[tuple[str, str]] = set()
        for link in agents.rglob("*"):
            if not link.is_symlink():
                continue
            try:
                host_target = link.resolve(strict=True)
            except OSError:
                warn(f"broken agent symlink: {link}")
                continue
            raw = os.readlink(link)
            container_target = Path(raw) if os.path.isabs(raw) else Path(os.path.normpath(str(link.parent / raw)))
            try:
                container_target.relative_to(agents)
                continue
            except ValueError:
                pass
            key = (str(host_target), str(container_target))
            if key not in seen:
                seen.add(key)
                pairs.append((host_target, container_target, True))

    out = []
    for src, dst, ro in pairs:
        spec = f"type=bind,src={src},dst={dst}" + (",ro=true" if ro else "")
        out += ["--mount", spec]
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


def run_args(workspace: Workspace, script_dir: Path) -> list[str]:
    c = CONTAINER_HOME
    args = [
        "run", "--rm", "--interactive", "--userns=keep-id", *kvm_args(),
        "--cap-drop=all", "--security-opt=no-new-privileges", "--hostname=agent-box",
        f"--workdir={workspace.container_cwd}", f"--env=HOME={c}",
        f"--env=PATH={c}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"--env=CODEX_HOME={c}/.codex",
        f"--env=GIT_CONFIG_GLOBAL={c}/.config/git/config",
        f"--env=GH_CONFIG_DIR={c}/.config/gh",
        f"--env=XDG_CACHE_HOME={c}/.cache",
        f"--env=UV_CACHE_DIR={c}/.cache/uv",
        f"--env=MISE_DATA_DIR={c}/.local/share/mise",
        "--env=DISABLE_AUTOUPDATER=1", f"--env=TERM={os.environ.get('TERM', 'xterm-256color')}",
        *build_mounts(workspace, script_dir),
    ]
    maybe_gh_token()
    for name in (
        "GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
        "HTTP_PROXY", "HTTPS_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
    ):
        if os.environ.get(name):
            args.append(f"--env={name}")
    if sys.stdin.isatty() and sys.stdout.isatty():
        args.append("--tty")
    return args


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


def runtime_main(args: list[str], script_dir: Path) -> None:
    if not shutil.which("podman"):
        raise AgentBoxError("podman not found")
    if not (script_dir / "Containerfile").is_file():
        raise AgentBoxError(f"Containerfile not found: {script_dir / 'Containerfile'}")
    try:
        workspace = resolve_workspace()
    except (NotADirectoryError, ValueError) as exc:
        raise AgentBoxError(str(exc)) from exc

    ensure_state_dirs()
    migrate_statusline()
    ensure_refresh_token()

    if subprocess.run(["podman", "image", "exists", IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        print("agent-box: image not found; building it...", file=sys.stderr)
        rc = build_image(script_dir, False, False)
        if rc:
            raise SystemExit(rc)

    sync_claude_agent_skills()
    podman = run_args(workspace, script_dir)

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
