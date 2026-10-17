"""Forward-only JSON formats; reads convert copies, explicit writes retain originals.

Package versions do not identify data formats. Each format keeps its own steps
so a new release can read every public predecessor without installing it first.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .atomicio import atomic_write_bytes, atomic_write_json
from .errors import AgentBoxError
from .paths import MIGRATION_STATE


@dataclass(frozen=True)
class Schema:
    name: str
    version: int = 1
    steps: dict[int, Callable[[dict], dict]] = field(default_factory=dict)
    key: str = "format_version"

    def source_version(self, data):
        if not isinstance(data, dict):
            raise AgentBoxError(f"Invalid {self.name}: expected a JSON object")
        # Omission is explicitly part of the first public format, including
        # hand-written config fragments; it never means the latest version.
        version = data.get(self.key, 1)
        if type(version) is not int or version < 1:
            raise AgentBoxError(f"Invalid {self.name} schema version")
        if version > self.version:
            raise AgentBoxError(f"{self.name} uses a newer schema ({version}); "
                                "upgrade agent-box before reading or writing it")
        return version

    def prepare(self, data):
        version = self.source_version(data)
        result = copy.deepcopy(data)
        while version < self.version:
            step = self.steps.get(version)
            if step is None:
                raise AgentBoxError(f"Missing {self.name} migration from schema {version}; nothing written")
            try:
                result = step(result)
                if not isinstance(result, dict):
                    raise ValueError()
                json.dumps(result, allow_nan=False)
            except AgentBoxError:
                raise
            except Exception:
                # A converter can handle credentials; its exception must not
                # quote their values to a terminal or an agent transcript.
                raise AgentBoxError(f"Cannot migrate {self.name} from schema {version}; "
                                    "nothing written; inspect the migration and retry") from None
            version += 1
            result[self.key] = version
        result[self.key] = self.version
        return result


GLOBAL_CONFIG = Schema("global configuration", key="version")
PROJECT_CONFIG = Schema("project configuration", key="version")
WORKFLOW_EVIDENCE = Schema("workflow evidence")
REVIEW_BUDGET = Schema("review budget")
PLAN_SYNC = Schema("Plan sync record")
IMPORT_STATE = Schema("tracker import mapping")
MOVE_JOURNAL = Schema("tracker move journal")
ADOPTION_RECEIPT = Schema("tracker adoption receipt")
BOX_POLICY = Schema("workspace creation policy")
SKILL_MARKER = Schema("managed skill marker")
REVIEW_RECEIPT = Schema("review receipt")
CONFIG_PROPOSAL = Schema("global configuration proposal")
APPLIED_PACKAGE = Schema("applied package")


def read(path, schema):
    """Read a current payload; the file envelope is not part of the consumer's API."""
    result = schema.prepare(json.loads(Path(path).read_bytes()))
    result.pop(schema.key)
    return result


def _original(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise AgentBoxError(f"Refusing nonregular migration target: {path}")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@contextmanager
def _lock(path):
    # Locks and private backups stay out of repository worktrees. The inode
    # survives each operation so another writer cannot lock a replacement.
    directory = MIGRATION_STATE / hashlib.sha256(str(path.absolute()).encode()).hexdigest()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / "write.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(fd)


def write(path, data, schema, *, validate=None, mode=0o600, expected=None):
    """A single-document commit; format conversions retain immutable private backups.

    Existing operation locks still guard read/modify/write of tracker journals.
    This lock additionally serializes format persistence and backup creation.
    """
    path = Path(path)
    # Callers write current payloads. Unlike a file read, an omitted envelope
    # here means the caller's current schema, never a second migration of data
    # that was already converted while loading.
    result = schema.prepare({schema.key: schema.version, **data})
    if validate:
        validate(result)
    with _lock(path) as directory:
        original = _original(path)
        if expected is not None and original != expected:
            raise AgentBoxError(f"{path} changed during migration; nothing written; retry")
        if original is not None:
            try:
                previous = json.loads(original)
            except ValueError:
                raise AgentBoxError(f"Cannot read {path}; refusing to overwrite it") from None
            old_version = schema.source_version(previous)
            # Refuse a missing/broken path even when the caller supplies an
            # already parsed current-format object that would hide that loss.
            schema.prepare(previous)
            if old_version < schema.version:
                digest = hashlib.sha256(original).hexdigest()
                backup = directory / f"schema-{old_version}-{digest}.json"
                if backup.exists() or backup.is_symlink():
                    if backup.is_symlink() or backup.read_bytes() != original:
                        raise AgentBoxError(f"Migration backup differs: {backup}; nothing written")
                else:
                    atomic_write_bytes(backup, original, 0o600)
        # Detect an editor that does not participate in our advisory lock.
        if _original(path) != original:
            raise AgentBoxError(f"{path} changed during migration; nothing written; retry")
        atomic_write_json(path, result, mode)


def upgrade(path, schema, *, validate=None, mode=0o600):
    """Explicitly persist a converted document, leaving current files byte-for-byte alone."""
    path = Path(path)
    original = _original(path)
    if original is None:
        return False
    data = json.loads(original)
    if schema.source_version(data) == schema.version:
        return False
    data[schema.key] = schema.source_version(data)
    write(path, data, schema, validate=validate, mode=mode, expected=original)
    return True
