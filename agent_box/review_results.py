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


def result_id(binding, task, round_id):
    return hashlib.sha256(json.dumps([binding, task, round_id]).encode()).hexdigest()


def seal_file(path, seal):
    """Keep the content identity beside the receipt before publishing its budget index."""
    target = path.with_suffix(".seal")
    value = {"owner": OWNER, "id": path.stem, "receipt": {"id": path.stem, **seal}}
    if target.exists() or target.is_symlink():
        previous, _ = read_file(target)
        if previous != value:
            raise AgentBoxError("Review result recovery seal differs; leaving it untouched")
    else:
        atomic_write_text(target, json.dumps(value, sort_keys=True) + "\n", 0o400)


def save(state, root, repository, value):
    """Persist before reconciliation; only verified owned receipts are pruned."""
    target = directory(state, repository)
    if target.resolve().is_relative_to(Path(root).resolve()):
        raise AgentBoxError("Review results must be outside the repository")
    ident = result_id(value["descriptor"]["binding"], value["descriptor"]["task"], value["descriptor"]["delegation"])
    path = target / (ident + ".json")
    if path.exists() or path.is_symlink():
        previous, seal = read_file(path)
        if any(previous.get(key) != item for key, item in value.items()):
            raise AgentBoxError("This round already has a different saved result")
        seal_file(path, seal)
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
            record, candidate_seal = read_file(candidate)
            created = float(record["created_at"])
            companion = candidate.with_suffix(".seal")
            extra = 0
            if companion.exists() or companion.is_symlink():
                sealed, _ = read_file(companion)
                if sealed.get("receipt") != {"id": candidate.stem, **candidate_seal}:
                    continue
                extra = companion.stat().st_size
            owned.append((created, candidate, candidate.stat().st_size + extra))
        except (AgentBoxError, KeyError, TypeError, ValueError):
            continue
    count, total = 1, size + 512
    for created, candidate, length in sorted(owned, reverse=True):
        if now - created > MAX_AGE or count >= MAX_FILES or total + length > MAX_TOTAL_BYTES:
            candidate.unlink()
            candidate.with_suffix(".seal").unlink(missing_ok=True)
        else:
            count += 1
            total += length
    atomic_write_text(path, raw, 0o400)
    _, seal = read_file(path)
    seal_file(path, seal)
    return {"id": ident, **seal}


def find(state, repository, binding, task, round_id=None):
    """Recover a receipt whose first index write failed; never guess a budget or route."""
    target = directory(state, repository)
    paths = ([target / (result_id(binding, task, round_id) + ".json")] if round_id else
             sorted(target.glob("*.json"), key=lambda path: path.lstat().st_mtime, reverse=True))
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            sealed, _ = read_file(path.with_suffix(".seal"))
            receipt = sealed["receipt"]
            value, actual_path = read(state, repository, receipt)
            descriptor = value["descriptor"]
            if (actual_path != path or descriptor["repository"] != repository
                    or descriptor["binding"] != binding or descriptor["task"] != task
                    or (round_id is not None and descriptor["delegation"] != round_id)):
                continue
            return receipt, value, path
        except (AgentBoxError, KeyError, TypeError):
            if round_id:
                raise AgentBoxError("Saved review result or recovery seal is missing or changed") from None
    return None


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
