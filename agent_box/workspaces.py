"""``agent-box workspaces``: what each project keeps on the host, and removing it.

A workspace is known by a hash of its directory, so its box, k3s cluster,
nested Podman volume and state directories carry the directory as a label or
a ``path`` file. The listing reads those records back from Podman and the
state tree instead of keeping a registry that could drift from them.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import k3s, links, runtime
from .errors import AgentBoxError

WORKSPACE_ID = re.compile(r"[0-9a-f]{16}")
USAGE = "usage: agent-box workspaces [rm ID|PATH... | prune] [--yes]"


@dataclass
class Entry:
    id: str
    path: str = ""          # empty when nothing recorded it
    box: str = ""           # the box container's name, empty when there is none
    box_state: str = ""
    cluster: str = ""       # the sidecar's state, "data" for a volume alone, empty for none
    podman_volume: str = ""
    cluster_volume: str = ""
    state: bool = False

    @property
    def vanished(self) -> bool:
        return bool(self.path) and not Path(self.path).exists()


def _podman_json(*args: str) -> list[dict]:
    try:
        result = subprocess.run(["podman", *args, "--format=json"], text=True, capture_output=True)
    except OSError as exc:
        raise AgentBoxError(f"podman {args[0]} failed: {exc}") from exc
    if result.returncode:
        raise AgentBoxError(f"podman {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return json.loads(result.stdout or "[]") or []


def _has_state(directory: Path) -> bool:
    """A state root holding only its lifecycle lock is empty: removal keeps that lock on purpose."""
    if directory.parent != runtime.workspace_state_root("0" * 16).parent:
        return True
    return any(child.name != runtime.WORKSPACE_LOCK_FILE for child in directory.iterdir())


def collect(podman: bool = True) -> list[Entry]:
    """Every workspace that holds a box, a cluster, a nested Podman volume or a state directory.

    Without Podman only the state directories are read: host sessions need none.
    """
    found: dict[str, Entry] = {}

    def entry(workspace_id: str, path: str = "") -> Entry:
        item = found.setdefault(workspace_id, Entry(workspace_id))
        item.path = item.path or path
        return item

    if podman:
        for container in _podman_json("ps", "--all", f"--filter=label={runtime.WORKSPACE_CONTAINER_LABEL}=true"):
            labels = container.get("Labels") or {}
            workspace_id = labels.get(runtime.WORKSPACE_CONTAINER_WORKSPACE_LABEL, "")
            if WORKSPACE_ID.fullmatch(workspace_id):
                item = entry(workspace_id, labels.get(runtime.WORKSPACE_PATH_LABEL, ""))
                item.box, item.box_state = (container.get("Names") or [""])[0], container.get("State", "")
        for container in _podman_json("ps", "--all", f"--filter=label={k3s.LABEL}"):
            labels = container.get("Labels") or {}
            if WORKSPACE_ID.fullmatch(labels.get(k3s.LABEL, "")):
                entry(labels[k3s.LABEL], labels.get(runtime.WORKSPACE_PATH_LABEL, "")).cluster = container.get("State", "")
        # A network can outlive the rest of a cluster; it is still something to remove.
        for network in _podman_json("network", "ls", f"--filter=label={k3s.LABEL}"):
            labels = network.get("labels") or {}
            if WORKSPACE_ID.fullmatch(labels.get(k3s.LABEL, "")):
                item = entry(labels[k3s.LABEL])
                item.cluster = item.cluster or "network"
        for volume in _podman_json("volume", "ls"):
            labels = volume.get("Labels") or {}
            path = labels.get(runtime.WORKSPACE_PATH_LABEL, "")
            if WORKSPACE_ID.fullmatch(labels.get(runtime.NESTED_PODMAN_LABEL, "")):
                entry(labels[runtime.NESTED_PODMAN_LABEL], path).podman_volume = volume["Mountpoint"]
            elif WORKSPACE_ID.fullmatch(labels.get(k3s.LABEL, "")) and volume.get("Name") == k3s.name(labels[k3s.LABEL]):
                item = entry(labels[k3s.LABEL], path)
                item.cluster_volume = volume["Mountpoint"]
                item.cluster = item.cluster or "data"
    # Each kind of state directory sits in a parent of its own, named by the id.
    for parent in {directory.parent for directory in runtime.workspace_state_dirs("0" * 16)}:
        for child in parent.glob("*") if parent.is_dir() else ():
            if child.is_dir() and WORKSPACE_ID.fullmatch(child.name) and _has_state(child):
                entry(child.name).state = True
    for item in found.values():
        record = runtime.workspace_state_root(item.id) / runtime.WORKSPACE_PATH_FILE
        if not item.path and record.is_file():
            # Exactly the one delimiter: a directory name may end in a newline itself.
            item.path = os.fsdecode(record.read_bytes().removesuffix(b"\n"))
    return sorted(found.values(), key=lambda item: (not item.path, item.path, item.id))


def _sizes(paths: list[str]) -> dict[str, int]:
    """Bytes under each volume mountpoint; the files belong to subordinate IDs, hence ``unshare``."""
    if not paths:
        return {}
    try:
        result = subprocess.run(["podman", "unshare", "du", "-sb", "--", *paths], text=True, capture_output=True)
    except OSError:
        return {}
    sizes = {}
    for line in result.stdout.splitlines():
        size, _, path = line.partition("\t")
        if size.isdigit():
            sizes[path] = int(size)
    return sizes


def _human(size: int | None) -> str:
    if size is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    raise AssertionError("unreachable")


def render(items: list[Entry]) -> str:
    sizes = _sizes([p for item in items for p in (item.podman_volume, item.cluster_volume) if p])
    rows = [("ID", "BOX", "PODMAN", "K3S", "PATH")]
    for item in items:
        podman = _human(sizes.get(item.podman_volume)) if item.podman_volume else "-"
        cluster = item.cluster or "-"
        if item.cluster_volume:
            cluster += f" {_human(sizes.get(item.cluster_volume))}"
        path = (f"{item.path} (missing)" if item.vanished else item.path) or "(unknown)"
        rows.append((item.id, item.box_state or "-", podman, cluster, path))
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(row[:4], widths)) + "  " + row[4]
                     for row in rows)


def remove(item: Entry, podman: bool = True) -> list[str]:
    """Delete everything agent-box keeps for one workspace; what could not be deleted.

    All under the workspace's lifecycle lock, and the box looked up again
    there: a launch while the user was confirming may have created one. The
    lock file itself stays: a launch already waiting holds that inode, and a
    recreated one would let the next launch run beside it.
    """
    root = runtime.workspace_state_root(item.id)
    left = []
    with runtime.transition_lock_at(root / runtime.WORKSPACE_LOCK_FILE):
        if podman:
            for box in _podman_json("ps", "--all",
                                    f"--filter=label={runtime.WORKSPACE_CONTAINER_WORKSPACE_LABEL}={item.id}"):
                runtime.remove_workspace_container_then_confirm_absent((box.get("Names") or [""])[0])
        sessions = runtime.HOST_SESSIONS_ROOT / item.id
        # A live session's directory is usually empty: ask its server, not the files.
        if sessions.is_dir():
            if shutil.which("tmux"):
                runtime.stop_host_sessions(sessions)
            else:
                left.append(f"{sessions} (its host sessions may still run: tmux not found to stop them)")
        links.stop(item.id)
        if podman:
            k3s.remove(item.id, root / "k3s", data=True)
            runtime.remove_nested_podman_volume(item.id)
        for directory in runtime.workspace_state_dirs(item.id):
            if directory == root:
                for child in root.iterdir() if root.is_dir() else ():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    elif child.name != runtime.WORKSPACE_LOCK_FILE:
                        child.unlink(missing_ok=True)
            elif not left or directory != sessions:
                shutil.rmtree(directory, ignore_errors=True)
    return left + [str(directory) for directory in runtime.workspace_state_dirs(item.id)
                   if directory.exists() and _has_state(directory) and not (left and directory == sessions)]


def _resolve(targets: list[str], items: list[Entry]) -> list[Entry]:
    by_id = {item.id: item for item in items}
    chosen = []
    for target in targets:
        workspace_id = target if WORKSPACE_ID.fullmatch(target) else runtime.workspace_identity(
            runtime.Workspace(Path(target).absolute(), Path(target).absolute()))
        if workspace_id not in by_id:
            raise AgentBoxError(f"agent-box keeps nothing for {target}; 'agent-box workspaces' lists what it keeps")
        chosen.append(by_id[workspace_id])
    return chosen


def _confirm(items: list[Entry], assume_yes: bool) -> bool:
    print("This deletes, with no way back, everything agent-box keeps for:")
    print(render(items))
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise AgentBoxError("refusing to delete without a terminal to confirm on; pass --yes")
    try:
        answer = input("Delete? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        answer = ""
    return answer in {"y", "yes"}


def main(args: list[str]) -> int:
    assume_yes = "--yes" in args
    words = [arg for arg in args if arg != "--yes"]
    if any(word.startswith("-") for word in words):
        raise AgentBoxError(USAGE)
    podman = shutil.which("podman") is not None
    if podman:
        runtime.host_podman.require()
    items = collect(podman)
    if not words:
        if assume_yes:
            raise AgentBoxError(USAGE)
        print(render(items) if items else "agent-box keeps nothing for any workspace")
        return 0
    command, targets = words[0], words[1:]
    if command == "rm" and targets:
        chosen = _resolve(targets, items)
    elif command == "prune" and not targets:
        chosen = [item for item in items if item.vanished]
        unknown = sum(1 for item in items if not item.path)
        if unknown:
            print(f"agent-box: {unknown} workspace(s) with no recorded directory are left; "
                  "remove one with 'agent-box workspaces rm ID'", file=sys.stderr)
        if not chosen:
            print("agent-box: no workspace directory has vanished")
            return 0
    else:
        raise AgentBoxError(USAGE)
    if not _confirm(chosen, assume_yes):
        print("Nothing deleted.")
        return 1
    status = 0
    for item in chosen:
        left = remove(item, podman)
        if left:
            status = 1
            print(f"agent-box: workspace {item.id} could not be removed completely; delete by hand: "
                  + ", ".join(left), file=sys.stderr)
        else:
            print(f"agent-box: removed workspace {item.id} ({item.path or 'unknown directory'})")
    return status
