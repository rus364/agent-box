"""Portable repository identity and conservative, marker-scoped setup."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .atomicio import atomic_write_text
from .errors import AgentBoxError

BINDING_FILE = ".agent-box.json"


def server_identity(url):
    if not isinstance(url, str):
        raise AgentBoxError("Tracker URL must be a string")
    try:
        parts = urlsplit(url.strip().rstrip("/"))
    except ValueError:
        raise AgentBoxError("Invalid tracker server URL") from None
    try:
        parts.port
    except ValueError:
        raise AgentBoxError("Tracker URL has an invalid port") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise AgentBoxError("Tracker URL must be an HTTP(S) server URL without credentials, query, or fragment")
    return parts._replace(netloc=parts.netloc.lower()).geturl().rstrip("/")


@dataclass
class Binding:
    """Which tracker project this repository belongs to, and nothing else.

    Board view IDs, lifecycle column IDs and the base branch are facts owned by
    Vikunja and Git; reading them live cannot go stale, while a cached copy can
    silently move a card to a deleted column or a push to a renamed branch.
    `server` stays because a project ID means nothing without the instance it
    belongs to, and Vikunja exposes no stable instance identity of its own.
    """
    provider: str
    server: str
    project: str
    recursive: bool = False

    def validate(self):
        from .config import TRACKER_PROVIDERS
        if any(not isinstance(getattr(self, name), str) for name in ("provider", "server", "project")):
            raise AgentBoxError("Repository provider, server and project must be strings")
        if not isinstance(self.recursive, bool):
            raise AgentBoxError("Repository recursive must be boolean")
        if self.provider not in TRACKER_PROVIDERS:
            raise AgentBoxError(f"Unknown tracker provider: {self.provider}")
        server_identity(self.server)
        if not self.project:
            raise AgentBoxError("Repository binding needs a project ID")

    @classmethod
    def load(cls, root: Path):
        from .project_config import load
        data = load(root / BINDING_FILE).get("tracker", {})
        try:
            provider = data["provider"]
            result = cls(provider, data["connections"][provider]["url"],
                         data["project"], data.get("recursive", False))
            result.validate()
            return result
        except (KeyError, TypeError):
            raise AgentBoxError(f"Incomplete repository binding: {root / BINDING_FILE}; run ab-setup") from None

    @classmethod
    def session(cls, root: Path):
        """The binding this session works in: the file, then --tracker/--project.

        Setup and adoption keep reading :meth:`load`, because they compare and
        write the file itself. Returns None for an unbound repository.
        """
        from .launch_options import session_overrides
        from .project_config import load
        launch = session_overrides()
        data = load(root / BINDING_FILE).get("tracker", {})
        if not launch.keys() & {"tracker", "project"}:
            return cls.load(root) if "project" in data else None
        from .config import effective_config
        provider = data.get("provider") or effective_config().tracker.provider
        server = launch.get("tracker") or data.get("connections", {}).get(provider, {}).get("url")
        project = launch.get("project") or data.get("project")
        if not server or not project:
            raise AgentBoxError("This repository has no tracker binding; a session binds one only with both --tracker and --project")
        result = cls(provider, server, project, data.get("recursive", False))
        result.validate()
        return result


def binding_document(root, binding):
    from .project_config import load
    from .migrations import PROJECT_CONFIG
    data = load(root / BINDING_FILE)
    tracker = data.setdefault("tracker", {})
    tracker.update(provider=binding.provider, project=binding.project, recursive=binding.recursive)
    tracker.setdefault("connections", {}).setdefault(binding.provider, {})["url"] = binding.server
    return {"version": PROJECT_CONFIG.version, **data}


def repository_root(cwd: Path | None = None, *, fallback=False):
    directory = Path.cwd() if cwd is None else Path(cwd)
    env = {**os.environ, "LC_ALL": "C"}
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                                capture_output=True, text=True, timeout=15,
                                env=env)
    except FileNotFoundError:
        if fallback and directory.is_dir():
            return directory
        raise AgentBoxError(f"Cannot discover Git repository in {directory}: Git or directory is unavailable") from None
    except subprocess.TimeoutExpired:
        raise AgentBoxError(f"Git repository discovery timed out in {directory}") from None
    except OSError:
        raise AgentBoxError(f"Cannot discover Git repository in {directory}: Git could not run") from None
    if result.returncode:
        if result.stderr.startswith("fatal: not a git repository (or any"):
            if fallback:
                return directory
            raise AgentBoxError("Run this command inside a Git repository")
        # Git can discover a bare parent yet have no worktree top-level to show.
        try:
            probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd,
                                   capture_output=True, text=True, timeout=15, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise AgentBoxError(f"Git repository discovery failed in {directory} (exit {result.returncode})") from None
        if probe.returncode == 0 and probe.stdout.strip() == "false":
            if fallback:
                return directory
            raise AgentBoxError("Run this command inside a Git worktree")
        raise AgentBoxError(f"Git repository discovery failed in {directory} (exit {result.returncode})")
    return Path(result.stdout.strip()).resolve()


def git_dir(cwd: Path | None = None) -> Path:
    """This checkout's own Git directory, for workspace state that must never be committed."""
    directory = Path.cwd() if cwd is None else Path(cwd)
    try:
        result = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=cwd,
                                capture_output=True, text=True, timeout=15,
                                env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise AgentBoxError(f"Cannot discover the Git directory in {directory}: Git could not run") from None
    if result.returncode:
        raise AgentBoxError("Run this command inside a Git repository")
    return Path(result.stdout.strip())


def block_text(body, name):
    digest = hashlib.sha256(body.encode()).hexdigest()
    return f"<!-- BEGIN AGENT-BOX {name} sha256={digest} -->\n{body}<!-- END AGENT-BOX {name} -->\n"


def update_block(original: str, body: str, name: str, path: Path):
    begin = f"<!-- BEGIN AGENT-BOX {name}"
    end = f"<!-- END AGENT-BOX {name} -->"
    if begin not in original and end not in original:
        return block_text(body, name) + "\n" + original
    pattern = re.compile(re.escape(begin) + r" sha256=([a-f0-9]{64}) -->\n(.*?)" + re.escape(end) + r"\n?", re.S)
    match = pattern.search(original)
    if original.count(begin) != 1 or original.count(end) != 1 or not match:
        raise AgentBoxError(f"Malformed or duplicate managed block in {path}; repair it explicitly")
    if hashlib.sha256(match[2].encode()).hexdigest() != match[1]:
        first = original[:match.start()].count("\n") + 1
        last = original[:match.end()].count("\n") + 1
        raise AgentBoxError(f"User-edited block in {path}:{first}-{last}; preserved")
    return original[:match.start()] + block_text(body, name) + original[match.end():]


AGENT_RULES = """Read the tracked task before work; .agent-box.json binds this repository to its tracker project.
Use agent-box tracker for task state, progress and dependencies; read related history when context is missing.
Use ab-interview, ab-plan, ab-work, ab-sync, ab-review, ab-pr, and ab-land as needed. ab-setup maintains this binding and ab-adopt brings an existing project in line with the workflow; ab-normalize processes unmarked tasks in a bound project.
The coordinator owns branches, commits, lifecycle, dependencies, and GitHub actions. Delegate implementation sequentially and research to scouts, which may run in parallel, when their model lists are configured; with an empty list the coordinator does that work itself.
A worker changes project files and answers; scouts and a fresh reviewer only read. Plan sync reads the repository and updates tracker descriptions and comments through agent-box tracker; it never changes project files, Git or lifecycle. The coordinator verifies answers and findings and commits what it accepts.
Tracker comments are a human journal. Branch, base branch, and pull request come from Git and GitHub; lifecycle comes from the tracker's own fields, never from comment text.
Use the user's language for all coordinator messages to them: progress updates, questions, option explanations and final answers. Literal project labels may stay quoted in the project language. Write tracker text, GitHub text, commit messages, code comments and documentation in the `language` that `agent-box tracker status` and `workflow inspect` report; when it is null, use the task's language in the tracker and English for implementation. Agent-to-agent handoffs and reports stay English; agent-box CLI output, errors and help stay English.
Complete subtasks only after validation and integration into the parent branch; complete parents only after merge into the base branch.
"""


def setup_content(root: Path, binding: Binding):
    binding.validate()
    content = {
        root / BINDING_FILE: json.dumps(binding_document(root, binding), indent=2, ensure_ascii=False) + "\n",
    }
    for name, body, tag in (("AGENTS.md", AGENT_RULES, "workflow"),
                            ("CLAUDE.md", "@AGENTS.md\n", "instructions")):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise AgentBoxError(f"Refusing to replace {path}")
        if name == "CLAUDE.md" and not path.exists():
            continue
        original = path.read_bytes().decode() if path.exists() else ""
        if name == "CLAUDE.md" and "@AGENTS.md" in original and "BEGIN AGENT-BOX instructions" not in original:
            continue
        content[path] = update_block(original, body, tag, path)
    for path in content:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise AgentBoxError(f"Refusing to replace {path}")
    return content


def write_binding(root: Path, binding: Binding):
    """Cutover writes one file, after repository instruction checks have passed."""
    binding.validate()
    path = root / BINDING_FILE
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise AgentBoxError(f"Refusing to replace {path}")
    from .project_config import save
    save(path, binding_document(root, binding))


def write_setup(root: Path, binding: Binding, *, instructions_only=False):
    content = setup_content(root, binding)
    if instructions_only:
        content.pop(root / BINDING_FILE)
    originals = {}
    for path in content:
        originals[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    written = []
    try:
        for path, text in content.items():
            if originals[path] and originals[path][0] == text.encode():
                continue
            if path.name == BINDING_FILE:
                from .project_config import save
                save(path, json.loads(text))
            else:
                atomic_write_text(path, text, originals[path][1] if originals[path] else 0o644)
            written.append(path)
    except OSError:
        for path in reversed(written):
            old = originals[path]
            if old is None:
                path.unlink()
            else:
                atomic_write_text(path, old[0].decode(), old[1])
        raise
    return [str(p) for p in content]
