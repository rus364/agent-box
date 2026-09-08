"""Portable repository identity and conservative, marker-scoped setup."""
from __future__ import annotations

import hashlib
import json
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


def binding_document(root, binding):
    from .project_config import load
    data = load(root / BINDING_FILE)
    tracker = data.setdefault("tracker", {})
    tracker.update(provider=binding.provider, project=binding.project, recursive=binding.recursive)
    tracker.setdefault("connections", {}).setdefault(binding.provider, {})["url"] = binding.server
    return data


def repository_root(cwd: Path | None = None):
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise AgentBoxError("Run this command inside a Git repository")
    return Path(result.stdout.strip()).resolve()


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
Use agent-box tracker for task state, progress, history, and dependencies; search related tasks when context is missing.
Use ab-interview, ab-plan, ab-work, ab-review, ab-pr, and ab-land as needed. ab-setup maintains this binding and ab-adopt brings an existing project in line with the workflow.
The coordinator owns branches, commits, lifecycle, dependencies, and GitHub actions. Delegate implementation sequentially; independent read-only scouts may run in parallel.
A worker changes project files in the working tree and returns a report; the coordinator records what it accepts. Review runs in fresh context and returns findings to the coordinator.
Tracker comments are a human journal. Branch, base branch, and pull request come from Git and GitHub; lifecycle comes from the tracker's own fields, never from comment text.
Communicate with the user in their language and write tracker content in the task's language. Fixed diagnostic comments generated automatically by agent-box may be English. Use English for agent handoffs and implementation.
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
    atomic_write_text(path, json.dumps(binding_document(root, binding), indent=2, ensure_ascii=False) + "\n",
                      path.stat().st_mode & 0o777 if path.exists() else 0o644)


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
