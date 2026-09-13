"""Bounded private receipts; applicability lives in the existing review budget."""
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from .atomicio import atomic_write_text
from .errors import AgentBoxError

MAX_FILES = 20
MAX_AGE = 7 * 24 * 3600
MAX_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
OWNER = "agent-box-review-result"


def directory(state, repository):
    target = state / "review-results" / repository
    for path in (state, state / "review-results", target):
        if path.is_symlink():
            raise AgentBoxError("Review result directory is a symlink; leaving it untouched")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or (path != state and info.st_mode & 0o077)):
            raise AgentBoxError("Review result directory must be private and owned by the current user")
    return target


def read_file(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o400 or info.st_nlink != 1):
                raise ValueError("not an owned read-only regular file")
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("size limit exceeded")
        value = json.loads(raw)
        if value.get("owner") != OWNER or value.get("id") != path.stem:
            raise ValueError("ownership marker differs")
        seal = {"sha256": hashlib.sha256(raw).hexdigest(), "inode": info.st_ino,
                "device": info.st_dev, "ctime_ns": info.st_ctime_ns}
        return value, seal
    except (OSError, ValueError, AttributeError) as exc:
        raise AgentBoxError("Review result is missing, replaced or invalid; no acceptance recorded") from exc


def save(state, root, repository, value):
    """Persist before reconciliation; only verified owned receipts are pruned."""
    target = directory(state, repository)
    if target.resolve().is_relative_to(Path(root).resolve()):
        raise AgentBoxError("Review results must be outside the repository")
    ident = hashlib.sha256(json.dumps([value["descriptor"]["binding"], value["descriptor"]["task"],
                                     value["descriptor"]["delegation"]]).encode()).hexdigest()
    path = target / (ident + ".json")
    if path.exists() or path.is_symlink():
        previous, seal = read_file(path)
        if any(previous.get(key) != item for key, item in value.items()):
            raise AgentBoxError("This round already has a different saved result")
        return {"id": ident, **seal}
    now = time.time()
    raw = json.dumps({**value, "owner": OWNER, "id": ident, "created_at": now},
                     ensure_ascii=False, sort_keys=True) + "\n"
    size = len(raw.encode())
    if size > MAX_BYTES:
        raise AgentBoxError("Review result exceeds the private 8 MiB limit; input retained")
    owned = []
    for candidate in target.glob("*.json"):
        if len(candidate.stem) != 64:
            continue
        try:
            record, _ = read_file(candidate)
            created = float(record["created_at"])
            owned.append((created, candidate, candidate.stat().st_size))
        except (AgentBoxError, KeyError, TypeError, ValueError):
            continue
    count, total = 1, size
    for created, candidate, length in sorted(owned, reverse=True):
        if now - created > MAX_AGE or count >= MAX_FILES or total + length > MAX_TOTAL_BYTES:
            candidate.unlink()
        else:
            count += 1
            total += length
    atomic_write_text(path, raw, 0o400)
    _, seal = read_file(path)
    return {"id": ident, **seal}


def read(state, repository, receipt):
    ident = receipt.get("id", "")
    if len(ident) != 64 or any(c not in "0123456789abcdef" for c in ident):
        raise AgentBoxError("Invalid review result reference")
    path = directory(state, repository) / (ident + ".json")
    record, seal = read_file(path)
    if any(receipt.get(key) != value for key, value in seal.items()):
        raise AgentBoxError("Review result was replaced or modified; no acceptance recorded")
    if time.time() - record["created_at"] > MAX_AGE:
        raise AgentBoxError("Review result expired; no acceptance recorded")
    return record, path
