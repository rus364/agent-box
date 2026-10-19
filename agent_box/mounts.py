"""Bind-mount planning: parsing ``--mount`` and resolving overlaps.

Kept as pure logic (no subprocess, no filesystem side effects beyond
``Path.resolve()``) so mount planning can be unit-tested deterministically.
Its one dependency is ``paths.expand_user``, so that ``~`` in a ``--mount``
value resolves against the passwd home like every other host path here.

Design: a host path that is deliberately exposed to the agent appears at the
*same* absolute path inside the container (see ``workspace.py``), so a
``MountSpec`` carries a single canonical path rather than a host/container
pair. All paths are resolved (symlinks followed) before planning, both so
overlap detection is meaningful and so a mount can't be widened by a symlink
none of the validation logic saw.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .paths import HOST_TMP_DIR, expand_user


class MountError(ValueError):
    """A user-supplied ``--mount`` value, or a set of mounts, is invalid."""


@dataclass(frozen=True)
class MountSpec:
    path: Path          # resolved (symlink-free), absolute
    read_only: bool
    label: str = ""      # human-readable origin, for error messages only

    @property
    def mode(self) -> str:
        return "ro" if self.read_only else "rw"


def parse_mount_arg(raw: str) -> MountSpec:
    """Parse one ``--mount`` value: ``PATH`` or ``PATH:ro``.

    The path is resolved to its canonical, symlink-free form and must exist
    and be a directory — silently mounting a not-yet-existing path is more
    likely a typo than an intentional request, and podman would fail later
    with a much less clear error.
    """
    if not raw or not raw.strip():
        raise MountError("--mount requires a path")
    read_only = False
    text = raw
    if text.endswith(":ro"):
        read_only = True
        text = text[: -len(":ro")]
    elif text.endswith(":rw"):
        text = text[: -len(":rw")]
    if not text:
        raise MountError(f"--mount: missing path in {raw!r}")
    # expand_user, not Path.expanduser: "~" must mean the passwd home, so a
    # poisoned $HOME cannot silently redirect a --mount somewhere else.
    path = expand_user(text)
    if not path.is_absolute():
        raise MountError(f"--mount path must be absolute: {raw!r}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MountError(f"--mount path does not exist: {text}") from exc
    if not resolved.is_dir():
        raise MountError(f"--mount path is not a directory: {text}")
    return MountSpec(path=resolved, read_only=read_only, label=raw)


def _is_within(inner: Path, outer: Path) -> bool:
    return inner == outer or outer in inner.parents


def host_tmp_mounts(enabled: bool) -> list[MountSpec]:
    """Use the explicit-mount rules without consulting caller-controlled TMPDIR."""
    return [parse_mount_arg(str(HOST_TMP_DIR))] if enabled else []


def plan_mounts(specs: list[MountSpec]) -> list[MountSpec]:
    """Collapse/validate a set of mount requests into a minimal, safe set.

    Rules, applied deterministically (no reliance on podman's own mount
    ordering):

    * Two requests for the identical resolved path must agree on
      read-only/read-write; if they don't, that's an ambiguous request and
      is rejected rather than silently picking one.
    * If one request's path contains another's (a parent/child pair), only
      the broader (parent) mount is kept, but only when they agree on
      access mode. A parent mounted read-write would silently make a child
      that asked for read-only writable (or vice versa, silently restrict
      it), so that combination is rejected instead of guessed at.
    """
    by_path: dict[Path, MountSpec] = {}
    for spec in specs:
        existing = by_path.get(spec.path)
        if existing is None:
            by_path[spec.path] = spec
            continue
        if existing.read_only != spec.read_only:
            raise MountError(
                f"conflicting mount modes for {spec.path}: "
                f"{existing.label or existing.path} is {existing.mode}, "
                f"{spec.label or spec.path} is {spec.mode}"
            )
        # identical duplicate; keep the first

    # A persistent workspace compares its structural mount configuration by
    # value.  Canonical ordering means equivalent `--mount` sets supplied in
    # a different harmless order produce the same container identity.
    ordered = sorted(by_path.values(), key=lambda s: (len(s.path.parts), str(s.path)))
    kept: list[MountSpec] = []
    for spec in ordered:
        covering = next((k for k in kept if _is_within(spec.path, k.path)), None)
        if covering is None:
            kept.append(spec)
            continue
        if covering.read_only != spec.read_only:
            raise MountError(
                f"conflicting mount modes: {covering.label or covering.path} "
                f"({covering.mode}) contains {spec.label or spec.path} ({spec.mode})"
            )
        # covering mount already grants the same access; the narrower one is redundant
    return kept
