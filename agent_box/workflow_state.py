"""Local execution locks, never authoritative task state."""
import fcntl
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from .errors import AgentBoxError
from .paths import WORKFLOW_STATE


@contextmanager
def lock(scope, purpose, *, blocking=False):
    key = hashlib.sha256(str(Path(scope).resolve()).encode()).hexdigest()[:24]
    directory = WORKFLOW_STATE / "locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{key}-{purpose}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise AgentBoxError(f"Another local {purpose} operation owns {scope}") from None
        yield
    finally:
        os.close(fd)


@contextmanager
def scout_slot(scope, limit):
    """Independent read-only scouting can overlap without sharing a worker slot."""
    for index in range(limit):
        candidate = lock(scope, f"scout-{index}")
        try:
            candidate.__enter__()
        except AgentBoxError:
            continue
        try:
            yield
        finally:
            candidate.__exit__(None, None, None)
        return
    raise AgentBoxError(f"All {limit} local scout slots are busy")
