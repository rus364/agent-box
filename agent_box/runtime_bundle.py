"""Immutable package copies keep live bind mounts intact across tool reinstalls."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile


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


def prepare(package: Path, root: Path) -> Path:
    """Publish a complete copy atomically; never replace a live directory inode."""
    files = list(_files(package))
    destination = root / _digest(files)
    if destination.is_dir():
        return destination / "agent_box"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=root) as temporary:
        stage = Path(temporary)
        for relative, content in files:
            target = stage / "agent_box" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o755 if relative == Path("container-launcher") else 0o644)
        try:
            os.rename(stage, destination)
        except OSError:
            # Another launch may have published these same bytes while we copied.
            if not destination.is_dir():
                raise
    return destination / "agent_box"
