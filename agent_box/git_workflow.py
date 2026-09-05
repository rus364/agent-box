"""Git guards shared by task branches, parent pushes, and PR completion."""

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from .errors import AgentBoxError

REMOTE_QUERY_TIMEOUT = 15
_PUSH_CONFIG_OVERRIDES = {"push.followTags": "false", "push.recurseSubmodules": "no"}


def _push_config_args() -> list[str]:
    return [arg for key, value in _PUSH_CONFIG_OVERRIDES.items() for arg in ('-c', f'{key}={value}')]

def _run(args: list[str], cwd: Path | str | None=None, timeout: float=10) -> subprocess.CompletedProcess:
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    return subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout, check=False, env=env)

def current_branch(cwd: Path) -> str | None:
    result = _run(['git', 'symbolic-ref', '--short', '-q', 'HEAD'], cwd=cwd)
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None

def head_sha(cwd: Path) -> str | None:
    result = _run(['git', 'rev-parse', 'HEAD'], cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else None

class RemoteState:

    def __init__(self, default_branch: str | None, default_branch_sha: str | None, branch_sha: str | None, branch_exists: bool):
        self.default_branch = default_branch
        self.default_branch_sha = default_branch_sha
        self.branch_sha = branch_sha
        self.branch_exists = branch_exists

def _effective_push_url(cwd: Path) -> str | None:
    result = _run(['git', 'remote', 'get-url', '--push', '--all', 'origin'], cwd=cwd)
    if result.returncode != 0:
        return None
    urls = [line for line in result.stdout.splitlines() if line.strip()]
    return urls[0] if len(urls) == 1 else None

def _query_origin(cwd: Path, branch: str, remote_url: str) -> RemoteState | None:
    branch_ref = f'refs/heads/{branch}'
    try:
        result = _run(['git', 'ls-remote', '--symref', remote_url, 'HEAD', branch_ref], cwd=cwd, timeout=REMOTE_QUERY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    default_branch: str | None = None
    default_branch_sha: str | None = None
    branch_sha: str | None = None
    branch_exists = False
    for line in result.stdout.splitlines():
        if line.startswith('ref:'):
            rest = line[len('ref:'):].strip()
            ref, _, target = rest.rpartition('\t')
            if target == 'HEAD' and ref.startswith('refs/heads/'):
                default_branch = ref[len('refs/heads/'):]
            continue
        sha, _, ref = line.partition('\t')
        if ref == 'HEAD' and sha:
            default_branch_sha = sha
        if ref == branch_ref and sha:
            branch_sha = sha
            branch_exists = True
    if default_branch is None:
        return None
    return RemoteState(default_branch, default_branch_sha, branch_sha, branch_exists)

def _worktree_clean(cwd: Path) -> bool:
    result = _run(['git', 'status', '--porcelain'], cwd=cwd)
    return result.returncode == 0 and result.stdout.strip() == ''

def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    result = _run(['git', 'merge-base', '--is-ancestor', ancestor, descendant], cwd=cwd)
    return result.returncode == 0

def _merge_base(cwd: Path, a: str, b: str) -> str | None:
    result = _run(['git', 'merge-base', a, b], cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else None


def git(root, *args, timeout=30):
    try:
        result = _run(["git", *map(str, args)], cwd=root, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentBoxError(f"Git operation failed: {type(exc).__name__}") from None
    if result.returncode:
        raise AgentBoxError(f"git {args[0]} failed: {result.stderr.strip()[:1000]}")
    return result.stdout.strip()


def require_clean(root):
    if not _worktree_clean(root):
        raise AgentBoxError("Commit or preserve the current changes before changing workflow branches")


def branch_name(task):
    slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")[:55] or "task"
    ident = re.sub(r"[^a-zA-Z0-9_-]", "-", task.id)
    return f"ab/{ident}-{slug}"


def switch_branch(root, branch, base, *, resume=False):
    git(root, "check-ref-format", "--branch", branch)
    git(root, "check-ref-format", "--branch", base)
    exists = _run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], root).returncode == 0
    if exists:
        if not resume:
            raise AgentBoxError(f"Branch {branch} already exists; inspect it and use --resume")
        git(root, "switch", branch)
    else:
        git(root, "switch", "-c", branch, base)


def github_origin(root):
    url = _effective_push_url(root)
    if not url:
        raise AgentBoxError("origin must have exactly one effective push URL")
    if url.startswith("git@github.com:"):
        path = url.split(":", 1)[1]
    else:
        parsed = urlsplit(url)
        if parsed.hostname != "github.com" or parsed.scheme not in ("https", "ssh") or parsed.password or parsed.scheme == "https" and parsed.username:
            raise AgentBoxError("Workflow GitHub actions require a github.com origin without embedded credentials")
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        raise AgentBoxError("Cannot resolve owner/repository from origin")
    return path


def push_parent(root, branch, base):
    require_clean(root)
    if current_branch(root) != branch or branch in (base, "main", "master"):
        raise AgentBoxError("Only the current task's parent branch may be pushed")
    github_origin(root)
    url = _effective_push_url(root)
    state = _query_origin(root, branch, url)
    if state is None or branch == state.default_branch:
        raise AgentBoxError("Cannot establish a safe non-default push branch")
    head = head_sha(root)
    if state.branch_exists and state.branch_sha != head and not _is_ancestor(root, state.branch_sha, head):
        raise AgentBoxError("Remote branch diverged; refusing automatic repair")
    git(root, *_push_config_args(), "push", "-u", "origin", f"HEAD:refs/heads/{branch}", timeout=120)
    return {"branch": branch, "head": head, "pushed": True}
