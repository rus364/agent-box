"""The host's rootless Podman, checked once before any command that needs it.

agent-box needs rootless Podman specifically: ``--userns=keep-id`` is what
keeps workspace files owned by the host user, and rootless Docker has no
per-container equivalent, so a Docker fallback would be a second isolation
model rather than another CLI. A missing or unusable engine therefore gets a
diagnosis and the command that fixes it, never a bare "podman not found".
"""
from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path

from .errors import AgentBoxError

#: Podman 5 is the line agent-box is tested on, and the k3s sidecar's
#: ``--network=pasta:-T,PORT`` loopback forward needs its pasta networking.
#: One floor for the whole tool rather than a second, untested one for boxes
#: without a cluster; distributions that ship 4.x get the upstream guide.
MIN_VERSION = (5, 0)
INSTALL_GUIDE = "https://podman.io/docs/installation"
OS_RELEASE = (Path("/etc/os-release"), Path("/usr/lib/os-release"))
SUBID_FILES = (Path("/etc/subuid"), Path("/etc/subgid"))
#: The first ID of the subordinate ranges ``useradd`` hands out.
SUBID_START = 100000
SUBID_COUNT = 65536

#: Matched against ``ID`` first, then each ``ID_LIKE`` entry, so derivatives
#: (CachyOS, Mint, Rocky) resolve through the family they declare.
INSTALL_COMMANDS = {
    "arch": "sudo pacman -S podman",
    "debian": "sudo apt install podman",
    "ubuntu": "sudo apt install podman",
    "fedora": "sudo dnf install podman",
    "rhel": "sudo dnf install podman",
    "centos": "sudo dnf install podman",
    "suse": "sudo zypper install podman",
    "opensuse": "sudo zypper install podman",
    "alpine": "sudo apk add podman",
    "gentoo": "sudo emerge app-containers/podman",
}


def os_release(paths: tuple[Path, ...] = OS_RELEASE) -> dict[str, str]:
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fields = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                fields[key.strip()] = value.strip().strip("\"'")
        return fields
    return {}


def install_hint(release: dict[str, str]) -> str:
    for name in [release.get("ID", ""), *release.get("ID_LIKE", "").split()]:
        command = INSTALL_COMMANDS.get(name.lower())
        if command:
            return f"Install it: {command}"
    return f"Install it as your distribution describes: {INSTALL_GUIDE}"


def free_subid_start() -> int:
    """A start past every range already handed out, so a new range shares no IDs."""
    start = SUBID_START
    for path in SUBID_FILES:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            fields = line.split(":")
            if len(fields) == 3 and fields[1].isdigit() and fields[2].isdigit():
                start = max(start, int(fields[1]) + int(fields[2]))
    return start


def _run(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["podman", *args], 124, "", "timed out after 30s")
    except OSError as exc:
        return subprocess.CompletedProcess(["podman", *args], 127, "", str(exc))


def problem() -> str | None:
    """Why the host's Podman cannot run agent-box boxes, with the fix; None when it can."""
    if os.geteuid() == 0:
        return "rootless Podman is required: run agent-box as your own user, not as root or under sudo"
    if not shutil.which("podman"):
        lines = ["rootless Podman is required, and podman is not installed.",
                 install_hint(os_release())]
        if shutil.which("docker"):
            lines.append("Docker can stay installed alongside it: install the podman package, "
                         "not podman-docker, which replaces the docker command.")
        return "\n".join(lines)
    result = _run("--version")
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    if result.returncode or not match:
        return f"podman --version failed: {result.stderr.strip() or result.stdout.strip() or f'exit {result.returncode}'}"
    version = (int(match.group(1)), int(match.group(2)))
    if version < MIN_VERSION:
        return (f"Podman {'.'.join(map(str, MIN_VERSION))} or newer is required; found "
                f"{result.stdout.strip()}.\nYour distribution's package may be older; newer "
                f"builds: {INSTALL_GUIDE}")
    result = _run("unshare", "cat", "/proc/self/uid_map", "/proc/self/gid_map")
    if result.returncode:
        return (f"rootless Podman cannot enter its user namespace: "
                f"{result.stderr.strip() or f'exit {result.returncode}'}\n"
                "newuidmap and newgidmap must be installed (package uidmap or shadow); "
                "'podman system migrate' resets a stale namespace.")
    # Each map's first line maps the user to root; a subordinate range adds a
    # second line starting at ID 1. Without both, images whose files belong
    # to several owners cannot be unpacked.
    ranges = sum(1 for line in result.stdout.splitlines() if line.split()[:1] not in ([], ["0"]))
    if ranges < 2:
        user = pwd.getpwuid(os.getuid()).pw_name
        start = free_subid_start()
        span = f"{start}-{start + SUBID_COUNT - 1}"
        return ("rootless Podman has no subordinate UID/GID range for "
                f"{user} in /etc/subuid and /etc/subgid. Add one and restart Podman's namespace:\n"
                f"  sudo usermod --add-subuids {span} --add-subgids {span} {user}\n"
                "  podman system migrate")
    return None


def require() -> None:
    message = problem()
    if message:
        raise AgentBoxError(message)
