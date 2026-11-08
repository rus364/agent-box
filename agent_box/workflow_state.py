"""Local execution locks and launch allocation, never authoritative task state."""
import fcntl
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from .atomicio import atomic_write_json, read_json
from .errors import AgentBoxError
from .migrations import Schema
from .paths import WORKFLOW_STATE


BALANCE_FORMAT = Schema("workflow balance counters")


def weighted_turn(weights, credits):
    """Smooth weighted round-robin over whole weights; input order breaks ties."""
    for vendor, weight in weights.items():
        credits[vendor] = credits.get(vendor, 0) + weight
    winner = max(weights, key=credits.__getitem__)
    credits[winner] -= sum(weights.values())
    return winner


def _balance_keys(data):
    """Validate every key before a write, including allocations unused by this launch."""
    data = BALANCE_FORMAT.prepare(data)
    keys = data.get("keys")
    if set(data) != {"format_version", "keys"} or not isinstance(keys, dict):
        raise ValueError("expected a keys object")
    for key, entry in keys.items():
        role, _, tier = key.partition(":")
        if (role not in ("worker", "reviewer", "sync", "scout")
                or tier not in (("none",) if role == "scout" else ("none", "1", "2", "3"))
                or not isinstance(entry, dict) or set(entry) != {"weights", "credits"}):
            raise ValueError("invalid role/tier entry")
        weights, credits = entry.get("weights"), entry.get("credits")
        if (not isinstance(weights, dict) or len(weights) < 2 or not isinstance(credits, dict)
                or weights.keys() != credits.keys()
                or any(not name or type(weight) is not int or weight <= 0 for name, weight in weights.items())
                or any(type(credit) is not int for credit in credits.values())):
            raise ValueError("invalid weights or credits")
    return keys


def balance_vendor(role, tier, weights):
    """Consume one global turn for this role/tier, resetting only changed eligible weights."""
    path = WORKFLOW_STATE / "balance.json"
    with lock(None, "balance", blocking=True):
        try:
            if path.is_symlink():
                raise ValueError("refusing a symlink")
            try:
                data = read_json(path)
            except FileNotFoundError:
                data = {"format_version": BALANCE_FORMAT.version, "keys": {}}
            keys = _balance_keys(data)
        except (OSError, ValueError, AgentBoxError) as exc:
            raise AgentBoxError(f"Cannot read workflow balance counters {path}: {exc}; nothing written") from None
        key = f"{role}:{tier if tier is not None else 'none'}"
        previous = keys.get(key, {})
        credits = previous.get("credits", {}) if previous.get("weights") == weights else {}
        winner = weighted_turn(weights, credits)
        keys[key] = {"weights": dict(weights), "credits": credits}
        try:
            atomic_write_json(path, {"format_version": BALANCE_FORMAT.version, "keys": keys}, mode=0o600)
        except (OSError, ValueError) as exc:
            raise AgentBoxError(f"Cannot write workflow balance counters {path}: {exc}") from None
        return winner


@contextmanager
def lock(scope, purpose, *, blocking=False):
    """A None scope shares one lock even when host and box mount state at different paths."""
    key = "global" if scope is None else hashlib.sha256(str(Path(scope).resolve()).encode()).hexdigest()[:24]
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
