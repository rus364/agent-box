"""Global configuration changes proposed where config.json is not written, applied on the host with consent."""
from __future__ import annotations

import copy
from pathlib import Path

from .atomicio import atomic_write_json, read_json
from .config import SECRET_MODE, Config
from .errors import AgentBoxError
from .repo_binding import git_dir


def proposal_path(cwd=None) -> Path:
    """In the checkout's own Git directory.

    That directory is inside the workspace mount, so a box and its host see the
    same file without a new structural mount; each checkout has its own; and
    nothing there can be committed. A runtime-state directory was rejected:
    the box mounts those read-only or only with tracker/workflow enabled.
    """
    return git_dir(cwd) / "agent-box" / "config-proposal.json"


def load(path: Path) -> dict:
    if not path.is_file():
        return {"set": {}, "reset": []}
    try:
        data = read_json(path)
    except (OSError, ValueError):
        data = None
    if not (isinstance(data, dict) and set(data) == {"set", "reset"} and isinstance(data["set"], dict)
            and isinstance(data["reset"], list)
            and all(isinstance(item, str) and item for item in [*data["set"], *data["reset"]])):
        raise AgentBoxError(f"Unreadable global proposal {path}; remove it with 'agent-box config proposal discard'")
    return data


def merged(proposal: dict, assignments, resets) -> dict:
    """Later changes replace earlier ones for the same setting or a setting overlapping it."""
    new = [tuple(path) for path, _ in assignments] + [tuple(path) for path in resets]

    def kept(dotted):
        path = tuple(dotted.split("."))
        return not any(path[:len(other)] == other or other[:len(path)] == path for other in new)

    result = {"set": {dotted: value for dotted, value in proposal["set"].items() if kept(dotted)},
              "reset": [dotted for dotted in proposal["reset"] if kept(dotted)]}
    result["set"].update({".".join(path): copy.deepcopy(value) for path, value in assignments})
    result["reset"] += [".".join(path) for path in resets]
    return result


def applied(data: dict, proposal: dict) -> dict:
    """``data`` in ``Config.to_dict`` form with resets restored to built-in values and assignments set.

    The caller has checked every path against ``data``; this only rewrites a copy.
    """
    result, defaults = copy.deepcopy(data), Config().to_dict()

    def value_at(tree, parts):
        for part in parts:
            tree = tree[part]
        return tree

    changes = [(dotted, value_at(defaults, dotted.split("."))) for dotted in proposal["reset"]]
    for dotted, value in [*changes, *proposal["set"].items()]:
        *parents, name = dotted.split(".")
        value_at(result, parents)[name] = copy.deepcopy(value)
    return result


def save(path: Path, proposal: dict) -> None:
    # Credentials are refused before saving, but the file stays as private as config.json.
    try:
        atomic_write_json(path, proposal, SECRET_MODE)
    except OSError as exc:
        # A sandboxed agent may be unable to write the Git directory; explain instead of a traceback.
        raise AgentBoxError(f"Cannot write the global proposal {path}: {exc.strerror or exc}") from None


def discard(path: Path) -> None:
    path.unlink(missing_ok=True)
