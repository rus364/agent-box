"""Immutable package copies keep live bind mounts intact across tool reinstalls."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


def _live_references(proc: Path = Path("/proc")):
    """Fail closed if either rootless containers or this user's processes cannot be inventoried."""
    try:
        listing = subprocess.run(["podman", "ps", "--all", "--quiet"],
                                 text=True, capture_output=True, timeout=5, check=True)
        ids = listing.stdout.split()
        if any(not re.fullmatch(r"[a-f0-9]{12,64}", ident) for ident in ids):
            return None
        references = []
        if ids:
            result = subprocess.run(["podman", "inspect", *ids],
                                    text=True, capture_output=True, timeout=5, check=True)
            references += [mount["Source"] for box in json.loads(result.stdout)
                           for mount in box.get("Mounts", []) if isinstance(mount.get("Source"), str)]
        for process in proc.iterdir():
            if not process.name.isdecimal():
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue
                arguments = (process / "cmdline").read_bytes().decode(errors="surrogateescape").split("\0")
                if "agent_box.browser" in arguments and "serve" in arguments:
                    # Old brokers chdir after importing; /proc cannot identify their bundle.
                    return None
                references += arguments
            except (FileNotFoundError, ProcessLookupError):
                pass
        return references
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


def _retire_bundles(parent: Path, current: Path) -> None:
    cutoff = time.time() - 7 * 86400
    candidates = []
    for root in (parent / "launcher", parent / "browser-launcher"):
        if not root.is_dir() or root.is_symlink():
            continue
        bundles = sorted((path for path in root.iterdir() if not path.is_symlink() and path.is_dir()
                          and re.fullmatch(r"[a-f0-9]{64}", path.name)),
                         key=lambda path: path.stat().st_mtime, reverse=True)
        for path in bundles[2:]:
            if path != current and path.stat().st_mtime < cutoff:
                candidates.append(path)
        # New staging writers hold .bundles.lock. Older unleased staging is deliberately left alone.
        for path in root.glob(".prepare-locked-*"):
            if (re.fullmatch(r"\.prepare-locked-[a-z0-9_]{8}", path.name)
                    and not path.is_symlink() and path.is_dir() and path.stat().st_mtime < cutoff):
                shutil.rmtree(path)
    if candidates:
        references = _live_references()
        if references is not None:
            for path in candidates:
                if not any(str(path) in ref for ref in references):
                    shutil.rmtree(path)


def _files(package: Path):
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(package)
        if "__pycache__" not in relative.parts and path.is_file() and path.suffix not in {".pyc", ".pyo"}:
            yield relative, path.read_bytes()


def _digest(files) -> str:
    digest = hashlib.sha256()
    for relative, content in files:
        digest.update(str(relative).encode() + b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def identity(package: Path) -> str:
    return _digest(_files(package))


def prepare(package: Path, root: Path, *, managed: bool = False) -> Path:
    """Publish a complete copy atomically; never replace a live directory inode."""
    files = list(_files(package))
    destination = root / _digest(files)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root.parent / ".bundles.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if not destination.is_dir():
            with tempfile.TemporaryDirectory(prefix=".prepare-locked-", dir=root) as temporary:
                stage = Path(temporary)
                for relative, content in files:
                    target = stage / "agent_box" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    target.chmod(0o755 if relative == Path("container-launcher") else 0o644)
                try:
                    os.rename(stage, destination)
                except OSError:
                    # Pre-lock runtimes may still publish this same digest.
                    if not destination.is_dir():
                        raise
        # Protect a concurrent launch between preparation and process/container startup.
        os.utime(destination, None)
        if managed:
            try:
                _retire_bundles(root.parent, destination)
            except OSError:
                pass
    finally:
        os.close(fd)
    return destination / "agent_box"
