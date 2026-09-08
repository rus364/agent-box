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
        self.branch_symbolic = False

def effective_push_url(cwd: Path) -> str | None:
    """Where a push to origin actually lands, which is not `remote.origin.url`.

    `url.<base>.insteadOf` and `pushInsteadOf` rewrite that value, and every
    decision the workflow derives from origin - the default branch, the live
    state of a task branch, the GitHub repository - has to follow the rewrite the
    push itself would follow. `git remote get-url --push` applies it; reading the
    configuration key does not. Several push URLs mean no single destination, so
    the answer is nothing rather than the first one.
    """
    result = _run(['git', 'remote', 'get-url', '--push', '--all', 'origin'], cwd=cwd)
    if result.returncode != 0:
        return None
    urls = [line for line in result.stdout.splitlines() if line.strip()]
    return urls[0] if len(urls) == 1 else None

def push_destination(root):
    url = effective_push_url(root)
    if not url:
        raise AgentBoxError("origin must have exactly one effective push URL")
    if url.startswith("-") or any(ord(char) < 32 for char in url) or url in git(root, "remote").splitlines():
        raise AgentBoxError("Cannot freeze origin destination: unsafe URL or remote alias")
    # Passing an already rewritten URL to Git applies insteadOf once more.
    # Refuse chained rewrites rather than read one repository and mutate another.
    if git(root, "ls-remote", "--get-url", url) != url:
        raise AgentBoxError("Cannot freeze origin destination: chained URL rewrites")
    rules = _run(["git", "config", "--null", "--get-regexp",
                     r"^url\..*\.pushinsteadof$"], cwd=root)
    if rules.returncode not in (0, 1):
        raise AgentBoxError("Cannot read origin URL rewrite configuration")
    for rule in rules.stdout.split("\0"):
        if not rule:
            continue
        key, prefix = rule.split("\n", 1)
        replacement = key[len("url."):-len(".pushinsteadof")]
        if url.startswith(prefix) and replacement + url[len(prefix):] != url:
            raise AgentBoxError("Cannot freeze origin destination: chained push URL rewrites")
    return url


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
    branch_symbolic = False
    for line in result.stdout.splitlines():
        if line.startswith('ref:'):
            rest = line[len('ref:'):].strip()
            ref, _, target = rest.rpartition('\t')
            if target == branch_ref:
                branch_symbolic = True
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
    state = RemoteState(default_branch, default_branch_sha, branch_sha, branch_exists)
    state.branch_symbolic = branch_symbolic
    return state

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


def branch_prefix(task_id):
    return "ab/" + re.sub(r"[^a-zA-Z0-9_-]", "-", str(task_id)) + "-"


def branch_name(task):
    slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")[:55] or "task"
    return branch_prefix(task.id) + slug


def _branch_refs(root, pattern):
    return sorted(set(git(root, "for-each-ref", "--format=%(refname:short)", pattern).split()))


def task_branch(root, task):
    """The task ID owns a branch namespace, so Git alone can name the branch again.

    Recording the branch elsewhere would make a second source of truth that an
    agent with tracker write access could contradict; a title edit must not
    rename a live branch either, so the existing ref wins over the derived name.
    A fresh clone has the work only under `origin/`, and starting a second branch
    from the base there would silently abandon it, so the remote counts as the
    task's branch too. Anything the task could equally mean is refused.
    """
    prefix = branch_prefix(task.id)
    local = _branch_refs(root, f"refs/heads/{prefix}*")
    if len(local) > 1:
        raise AgentBoxError(f"Several local branches claim task {task.id}: {', '.join(local)}")
    remote = [name.removeprefix("origin/") for name in
              _branch_refs(root, f"refs/remotes/origin/{prefix}*")]
    if len(remote) > 1:
        raise AgentBoxError(f"Several origin branches claim task {task.id}: {', '.join(remote)}")
    if local and remote and local[0] != remote[0]:
        raise AgentBoxError(
            f"Task {task.id} has local branch {local[0]} and origin/{remote[0]}; reconcile them first")
    if local:
        return local[0]
    return remote[0] if remote else None


_DEFAULT_BASE: dict[str, str] = {}


def default_base(root):
    """Origin's current default branch, asked of the remote before the local copy.

    `refs/remotes/origin/HEAD` is only as fresh as the last fetch, so a project
    that renamed its default branch would keep sending branches and pull requests
    at the old name. The local ref remains the offline answer. Memoized per
    process so one command does not repeat the round trip.
    """
    key = str(Path(root).resolve())
    if key not in _DEFAULT_BASE:
        url = effective_push_url(root)
        state = _query_origin(root, "HEAD", url) if url else None
        name = state.default_branch if state else ""
        if not name:
            result = _run(["git", "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], cwd=root)
            name = result.stdout.strip().removeprefix("origin/") if result.returncode == 0 else ""
        if not name:
            raise AgentBoxError("Cannot determine origin's default branch; set refs/remotes/origin/HEAD")
        _DEFAULT_BASE[key] = name
    return _DEFAULT_BASE[key]


def base_ref(root, base):
    """A fresh clone may only have the remote-tracking copy of the base branch."""
    for candidate in (base, f"refs/remotes/origin/{base}"):
        if _run(["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], cwd=root).returncode == 0:
            return candidate
    raise AgentBoxError(f"Base {base} does not resolve to a commit in this repository")


def local_branch_head(root, branch):
    """Resolve a local branch exactly; landing must not substitute a remote ref."""
    result = _run(["git", "rev-parse", "--verify", "--quiet",
                   f"refs/heads/{branch}^{{commit}}"], cwd=root)
    if result.returncode:
        raise AgentBoxError(f"Local branch {branch} is required for local merge")
    return result.stdout.strip()


def base_commit(root, base):
    """Where this branch left the base; a moving base tip must not restate scope."""
    return git(root, "merge-base", base_ref(root, base), "HEAD")


def remote_branch_head(root, branch):
    """Origin's live copy of a branch; `refs/remotes/*` is only as fresh as a fetch.

    Another checkout can push to the same task branch, so completion has to ask
    the remote itself rather than believe what this clone last saw.
    """
    url = effective_push_url(root)
    state = _query_origin(root, branch, url) if url else None
    if state is None:
        raise AgentBoxError(f"Cannot read origin's copy of {branch}; reconcile before completing the task")
    return state.branch_sha if state.branch_exists else None


def switch_branch(root, branch, base, *, resume=False):
    git(root, "check-ref-format", "--branch", branch)
    git(root, "check-ref-format", "--branch", base)
    local = _run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], root).returncode == 0
    remote = _run(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], root).returncode == 0
    if local:
        if not resume:
            raise AgentBoxError(f"Branch {branch} already exists; inspect it and use --resume")
        if remote and not _is_ancestor(root, f"refs/remotes/origin/{branch}", branch):
            raise AgentBoxError(f"Branch {branch} and origin/{branch} have diverged; reconcile them before continuing")
        git(root, "switch", branch)
    elif remote:
        # The work already exists on the remote; forking the base again would
        # start a second branch for the same task and abandon it.
        git(root, "switch", "-c", branch, f"origin/{branch}")
    else:
        git(root, "switch", "-c", branch, base)


def github_origin(root):
    url = effective_push_url(root)
    return github_repository(url)


def github_repository(url):
    """Resolve GitHub identity from the same destination used for transport."""
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
    url = effective_push_url(root)
    state = _query_origin(root, branch, url)
    if state is None or branch == state.default_branch:
        raise AgentBoxError("Cannot establish a safe non-default push branch")
    head = head_sha(root)
    if state.branch_exists and state.branch_sha != head and not _is_ancestor(root, state.branch_sha, head):
        raise AgentBoxError("Remote branch diverged; refusing automatic repair")
    git(root, *_push_config_args(), "push", "-u", "origin", f"HEAD:refs/heads/{branch}", timeout=120)
    return {"branch": branch, "head": head, "pushed": True}


def push_local_base(root, base, accepted_head):
    """Publish only the accepted fast-forward result, with no mutable source ref."""
    require_clean(root)
    git(root, "check-ref-format", f"refs/heads/{base}")
    if current_branch(root) != base or local_branch_head(root, base) != accepted_head:
        raise AgentBoxError("Local base must equal the exact accepted head before publication")
    if _run(["git", "symbolic-ref", "-q", f"refs/heads/{base}"], cwd=root).returncode != 1:
        raise AgentBoxError("Cannot publish a symbolic or unreadable local base ref")
    url = push_destination(root)
    state = _query_origin(root, base, url)
    if (state is None or state.default_branch != base or state.branch_symbolic or not state.branch_sha
            or state.default_branch_sha != state.branch_sha):
        raise AgentBoxError("Cannot establish origin's live default/base and published head")
    if not _is_ancestor(root, state.branch_sha, accepted_head):
        raise AgentBoxError("Remote base is unknown or diverged; refusing automatic repair")
    if local_branch_head(root, base) != accepted_head or push_destination(root) != url:
        raise AgentBoxError("Local base or origin destination changed before publication")
    if state.branch_sha != accepted_head:
        git(root, *_push_config_args(), "push", "--no-force", "--no-mirror",
            "--no-follow-tags", "--recurse-submodules=no", "--", url,
            f"{accepted_head}:refs/heads/{base}", timeout=120)
    published = _query_origin(root, base, url)
    if (published is None or published.default_branch != base or published.branch_symbolic
            or published.branch_sha != accepted_head or published.default_branch_sha != accepted_head):
        raise AgentBoxError("Origin does not confirm the exact published default/base head")
    return {"branch": base, "head": accepted_head, "pushed": True}
