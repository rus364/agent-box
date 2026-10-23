"""Small atomic file I/O helpers shared across the package."""
from __future__ import annotations

import json
import fcntl
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _retire_temporary_writes(directory: Path) -> None:
    """A killed writer leaves a file; its kernel lock distinguishes it from a slow live writer."""
    cutoff = time.time() - 86400
    for path in directory.glob(".agent-box.tmp.*"):
        if not re.fullmatch(r"\.agent-box\.tmp\.[a-z0-9_]{8}", path.name):
            continue
        fd = None
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_mtime >= cutoff:
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                path.unlink()
        except OSError:
            # Cleanup is best-effort; a locked or concurrently replaced file belongs to its writer.
            pass
        finally:
            if fd is not None:
                os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Close the creation-to-lock gap without adding a persistent lock file per target.
        fcntl.flock(directory, fcntl.LOCK_EX)
        _retire_temporary_writes(path.parent)
        fd, name = tempfile.mkstemp(prefix=".agent-box.tmp.", dir=path.parent)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            os.unlink(name)
            raise
    finally:
        os.close(directory)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            if mode is not None:
                os.fchmod(f.fileno(), mode)
            os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def atomic_write_json(path: Path, value: Any, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode)
