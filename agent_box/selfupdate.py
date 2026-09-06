"""``agent-box upgrade``: move this checkout to the newest release.

Installing agent-box is a ``git clone``, so upgrading it is a git operation
rather than a download. That keeps a fork, a mirror and an air-gapped clone
working exactly like the canonical repository, needs no release artefacts, no
GitHub API and no network code of our own -- and leaves the user with a
checkout they can still read, diff and revert.

Releases are tags. When the repository has none yet the same command
fast-forwards the tracking branch instead, so ``upgrade`` does the sensible
thing before the first tag exists and starts following releases the moment one
does, with nothing to reconfigure in between.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .errors import AgentBoxError
from .pathsetup import checkout_root

#: ``v1.2.3`` or ``1.2.3``. Anything else -- a date tag, a codename, a
#: release candidate -- is deliberately not a release: picking "newest" out of
#: a set with no agreed ordering is how an upgrade command surprises people.
RELEASE_TAG = re.compile(r"^v?\d+\.\d+\.\d+$")


def _git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise AgentBoxError("git not found; agent-box needs it to upgrade") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentBoxError(f"git {' '.join(args)} failed: {exc}") from exc


def _out(args: list[str], cwd: Path, timeout: int = 120) -> str:
    result = _git(args, cwd, timeout)
    if result.returncode:
        raise AgentBoxError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def resolve_checkout() -> Path:
    """The git checkout this running agent-box came from.

    Resolved from the module's own location, never from the current
    directory: the command is normally run from whatever project the user is
    in, and on a machine with more than one clone the difference between "the
    installed one" and "the one I happen to be standing in" is the difference
    between upgrading and clobbering.
    """
    root = checkout_root()
    result = _git(["rev-parse", "--show-toplevel"], root)
    if result.returncode:
        raise AgentBoxError(
            f"{root} is not a git checkout, so there is nothing to upgrade; "
            f"update agent-box the way you installed it")
    top = Path(result.stdout.strip()).resolve()
    if top != root.resolve():
        raise AgentBoxError(
            f"agent-box runs from {root}, which is inside the git repository "
            f"{top} rather than being one; upgrade it by hand")
    return root


def dirty_files(checkout: Path) -> list[str]:
    """Paths git reports as changed, with the two status columns removed.

    Read from the unstripped output on purpose: a plain modification is
    reported as ``" M path"``, and stripping the whole blob would eat the
    leading space of the first line and then take a character off its name.
    """
    result = _git(["status", "--porcelain"], checkout)
    if result.returncode:
        raise AgentBoxError(f"git status failed: {result.stderr.strip()}")
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


def release_tags(checkout: Path) -> list[str]:
    """Release tags, newest first, by git's own version ordering."""
    listed = _out(["tag", "--list", "--sort=-v:refname"], checkout).splitlines()
    return [tag for tag in (line.strip() for line in listed) if RELEASE_TAG.match(tag)]


def current_branch(checkout: Path) -> str:
    return _git(["symbolic-ref", "--quiet", "--short", "HEAD"], checkout).stdout.strip()


def _same_commit(checkout: Path, a: str, b: str) -> bool:
    try:
        return _out(["rev-parse", a], checkout) == _out(["rev-parse", b], checkout)
    except AgentBoxError:
        return False


def _unpushed_commits(checkout: Path, branch: str) -> list[str]:
    """Local commits no remote branch has, i.e. somebody's unfinished work."""
    upstream = _git(["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], checkout)
    if upstream.returncode:
        return []
    listed = _git(["log", "--oneline", f"{upstream.stdout.strip()}..HEAD"], checkout)
    return [line for line in listed.stdout.splitlines() if line]


def upgrade_main(args: list[str]) -> int:
    """Fetch, then move to the newest release -- or say why it will not.

    Every refusal here is a case where continuing would destroy work that is
    not agent-box's: uncommitted edits, local commits, a checkout wired up in
    a way this command did not create. The alternative to refusing is a stash
    or a reset, and neither belongs in a command someone runs to get a newer
    version of a launcher.
    """
    check_only = False
    for arg in args:
        if arg == "--check":
            check_only = True
        else:
            raise AgentBoxError(f"unexpected argument '{arg}'; expected --check")

    checkout = resolve_checkout()
    print(f"agent-box: checkout {checkout}")

    dirty = dirty_files(checkout)
    if dirty:
        listed = "\n".join(f"  {name}" for name in dirty[:10])
        more = f"\n  ... and {len(dirty) - 10} more" if len(dirty) > 10 else ""
        raise AgentBoxError(
            f"the checkout has uncommitted changes, so it will not be moved:\n{listed}{more}")

    branch = current_branch(checkout)
    if branch:
        unpushed = _unpushed_commits(checkout, branch)
        if unpushed:
            raise AgentBoxError(
                f"branch '{branch}' has {len(unpushed)} commit(s) no remote has; "
                f"this checkout is being worked on, not just installed")

    if not _out(["remote"], checkout):
        raise AgentBoxError("the checkout has no git remote to upgrade from")

    if check_only:
        print("agent-box: fetching (no changes will be made)")
    fetch = _git(["fetch", "--tags", "--prune", "--quiet"], checkout, timeout=300)
    if fetch.returncode:
        raise AgentBoxError(f"git fetch failed: {fetch.stderr.strip()}")

    tags = release_tags(checkout)
    if tags:
        target, kind = tags[0], f"release {tags[0]}"
    elif branch:
        target, kind = f"origin/{branch}", f"origin/{branch} (no release tags yet)"
    else:
        raise AgentBoxError(
            "the repository has no release tags and this checkout is not on a "
            "branch, so there is nothing to move to")

    if _same_commit(checkout, "HEAD", target):
        print(f"agent-box: already on {kind}")
        return 0
    if check_only:
        print(f"agent-box: an upgrade to {kind} is available; run 'agent-box upgrade'")
        return 0

    if tags:
        # Detached at the tag: a release checkout has no branch to be behind
        # on, and leaving the user on a branch silently rewound to an older
        # commit would be worse than an honest detached HEAD.
        result = _git(["checkout", "--quiet", "--detach", target], checkout)
    else:
        result = _git(["merge", "--ff-only", "--quiet", target], checkout)
    if result.returncode:
        raise AgentBoxError(
            f"could not move the checkout to {kind}: "
            f"{result.stderr.strip() or result.stdout.strip()}")
    print(f"agent-box: updated to {kind}")

    # The skills agent-box ships are maintained in the checkout that just moved,
    # so the installed copies are now the old ones. This is the case
    # `apply_config` exists for; an upgrade that left them stale would make
    # "agent-box config apply" a thing users had to know to run.
    from . import runtime
    from .config import load_config
    for line in runtime.apply_config(load_config()):
        print(f"agent-box: {line}")

    try:
        missing, extra = runtime.image_tool_drift(load_config())
    except Exception:
        missing, extra = [], []
    if missing or extra:
        print("agent-box: the workspace image no longer matches the configured "
              "tool groups; run 'agent-box build' when convenient")
    return 0
