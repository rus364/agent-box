"""Project discovery constrained to configured workspace roots.

A Telegram user selects a project from a button list built from this module;
they never type a path. Every path returned here is verified to resolve
inside one of the configured, host-administered ``workspace_roots`` so a
remote user cannot cause an arbitrary host directory to be used.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProjectEntry:
    path: Path
    label: str


def _expand_root(root: str) -> Path | None:
    try:
        resolved = Path(root).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_dir() else None


def list_projects(workspace_roots: list[str]) -> list[ProjectEntry]:
    """List immediate subdirectories of each configured root, plus the root itself.

    Hidden directories (dotfiles) are skipped. Results are sorted for stable
    button ordering.
    """
    entries: list[ProjectEntry] = []
    seen: set[Path] = set()
    for raw_root in workspace_roots:
        root = _expand_root(raw_root)
        if root is None or root in seen:
            continue
        seen.add(root)
        entries.append(ProjectEntry(root, f"{root.name} (root)"))
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            continue
        for child in children:
            resolved = child.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            entries.append(ProjectEntry(resolved, resolved.name))
    return entries


def resolve_allowed_project(path: Path, workspace_roots: list[str]) -> Path | None:
    """Return ``path`` resolved, only if it is inside one configured root."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    for raw_root in workspace_roots:
        root = _expand_root(raw_root)
        if root is None:
            continue
        if resolved == root or root in resolved.parents:
            return resolved
    return None
