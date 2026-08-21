"""Project discovery constrained to configured workspace roots.

A Telegram user selects a project from a button list built from this module;
they never type a path. Every path returned here is verified to *resolve*
(symlinks followed) inside one of the configured, host-administered
``workspace_roots`` -- a symlink under a root that points outside it is not
listed, so a remote user can't be handed a button that quietly escapes the
allowed tree.
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


def _resolved_roots(workspace_roots: list[str]) -> list[Path]:
    roots = []
    for raw in workspace_roots:
        root = _expand_root(raw)
        if root is not None and root not in roots:
            roots.append(root)
    return roots


def _contained_in_any(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def list_projects(workspace_roots: list[str]) -> list[ProjectEntry]:
    """List each configured root plus its immediate subdirectories.

    Hidden directories (dotfiles) are skipped, as is any entry (root or
    child) whose resolved, symlink-free path is not actually contained in a
    configured root -- that can only happen via a symlink escaping the
    root, and such an entry must never be offered as a selectable project.
    """
    roots = _resolved_roots(workspace_roots)
    entries: list[ProjectEntry] = []
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        entries.append(ProjectEntry(root, f"{root.name} (root)"))
        try:
            children = sorted(p for p in root.iterdir() if not p.name.startswith("."))
        except OSError:
            continue
        for child in children:
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            if not resolved.is_dir():
                continue
            if not _contained_in_any(resolved, roots):
                continue  # symlink escapes every configured root; do not list it
            seen.add(resolved)
            entries.append(ProjectEntry(resolved, resolved.name))
    return entries


def resolve_allowed_project(path: Path, workspace_roots: list[str]) -> Path | None:
    """Return ``path`` resolved, only if it is inside one configured root."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    roots = _resolved_roots(workspace_roots)
    return resolved if _contained_in_any(resolved, roots) else None
