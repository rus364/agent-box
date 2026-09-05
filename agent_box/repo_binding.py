"""Portable repository identity and conservative, marker-scoped setup."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .atomicio import atomic_write_text
from .errors import AgentBoxError
from .tracker.core import STATES

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
    provider: str
    server: str
    project: str
    view: str = ""
    states: dict[str, str] = field(default_factory=dict)
    recursive: bool = False
    base_branch: str = ""

    def validate(self):
        from .config import TRACKER_PROVIDERS
        if any(not isinstance(getattr(self, name), str) for name in ("provider", "server", "project", "view", "base_branch")):
            raise AgentBoxError("Repository provider, server, project, view and base branch must be strings")
        if not isinstance(self.recursive, bool) or not isinstance(self.states, dict):
            raise AgentBoxError("Repository recursive must be boolean and states must be an object")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.states.items()):
            raise AgentBoxError("Lifecycle state names and IDs must be strings")
        if self.provider not in TRACKER_PROVIDERS:
            raise AgentBoxError(f"Unknown tracker provider: {self.provider}")
        server_identity(self.server)
        if not self.project:
            raise AgentBoxError("Repository binding needs a project ID")
        missing = set(STATES) - self.states.keys()
        if missing or set(self.states) != set(STATES) or len(set(self.states.values())) != len(STATES) or any(not v for v in self.states.values()):
            raise AgentBoxError("Lifecycle needs distinct Backlog, Ready, Doing, Review, and Done states")

    @classmethod
    def load(cls, root: Path):
        path = root / BINDING_FILE
        if path.is_symlink():
            raise AgentBoxError(f"Refusing symlink: {path}")
        try:
            data = json.loads(path.read_text())
            allowed = cls.__dataclass_fields__.keys()
            if set(data) - set(allowed):
                raise ValueError("Unknown repository binding fields")
            result = cls(**data)
            result.validate()
            return result
        except (OSError, ValueError, TypeError) as exc:
            raise AgentBoxError(f"Cannot read {path}: {exc}; run ab-setup") from None


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
Use ab-interview, ab-plan, ab-work, ab-review, ab-pr, and ab-land as needed. ab-setup maintains this binding.
The orchestrator owns branches, lifecycle, dependencies, and GitHub actions. Delegate implementation sequentially; independent read-only scouts may run in parallel.
Workers may comment and check existing items on their assigned task. Review runs in fresh context and returns findings to the orchestrator.
Communicate with the user in their language, write tracker content in the task's language, and use English for agent handoffs and implementation.
Complete subtasks only after validation and integration into the parent branch; complete parents only after merge into the base branch.
"""


def write_setup(root: Path, binding: Binding):
    binding.validate()
    content = {
        root / BINDING_FILE: json.dumps(asdict(binding), indent=2, ensure_ascii=False) + "\n",
    }
    originals = {}
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
