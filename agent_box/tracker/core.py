"""Workflow semantics live here, never in product adapters or skill copies."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

from ..errors import AgentBoxError

NORMALIZED_LABEL = "ab:normalized"

STATES = ("Backlog", "Ready", "Doing", "Review", "Done")


class TrackerError(AgentBoxError):
    def __init__(self, message, category="invalid", retryable=False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


def relation_key(task_id, kind, other):
    """Use one portable orientation for an adoption's intended relation edit."""
    if kind not in {"parent", "child", "blocks", "blocked-by", "related", "duplicate"} or task_id == other:
        raise TrackerError("Invalid relation")
    if kind in {"parent", "blocked-by"}:
        task_id, other = other, task_id
        kind = {"parent": "child", "blocked-by": "blocks"}[kind]
    if kind == "related" and task_id > other:
        task_id, other = other, task_id
    return task_id, kind, other


@dataclass
class Task:
    id: str
    project: str
    title: str
    description: str = ""
    state: str = "Backlog"
    assignees: list[str] = field(default_factory=list)
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    priority: int = 0
    revision: str = ""
    url: str = ""
    number: int | None = None

    @property
    def tier(self):
        values = {label.rsplit(":", 1)[-1] for label in self.labels if label.startswith("ab:tier:")}
        if not values:
            return None
        if len(values) != 1 or not values <= {"1", "2", "3"}:
            raise TrackerError(f"Task {self.id} has conflicting or invalid tier labels")
        return int(next(iter(values)))

    def to_dict(self, full=True):
        data = asdict(self)
        data["tier"] = self.tier
        if not full:
            data.pop("description")
            data.pop("revision")
        return data


class Adapter(Protocol):
    capabilities: frozenset[str]

    def get(self, task_id: str) -> Task: ...
    def resolve(self, reference: str, project: str, recursive: bool = False, *, title: bool = False,
                canonical: bool = False) -> Task: ...
    def search(self, project: str, query: str = "", recursive: bool = False) -> list[Task]: ...
    def update(self, task: Task, fields: dict) -> Task: ...
    def set_state(self, task: Task, state: str) -> Task: ...
    def comment(self, task_id: str, text: str) -> dict: ...
    def identity(self) -> dict: ...
    def projects(self) -> list[dict]: ...
    def project(self, reference: str) -> dict: ...
    def subtree(self, project: str, recursive: bool = False) -> list[str]: ...
    def board(self, project: str) -> dict: ...
    def lifecycle_report(self, projects=None) -> list[dict]: ...
    def initialize_boards(self, projects, apply=False) -> list[dict]: ...
    def comments(self, task_id: str) -> list[dict]: ...
    def relate(self, task_id: str, kind: str, other: str, remove=False) -> Task: ...
    def set_tier(self, task_id: str, tier: int) -> Task: ...
    def set_normalized(self, task_id: str, normalized: bool = True) -> Task: ...
    def rename_project(self, project_id: str, title: str) -> dict: ...
    def clone_project_tree(self, source: str, title: str, progress: dict,
                           checkpoint: Callable[[dict], None], apply=False) -> dict: ...
    def preserve_external_relations(self, relations: list[dict], apply=False) -> list[dict]: ...


def root_task(task: Task, get: Callable[[str], Task]) -> Task:
    seen = {task.id}
    while task.parent:
        if task.parent in seen:
            raise TrackerError("Parent relation cycle", "conflict")
        seen.add(task.parent)
        task = get(task.parent)
    return task


def blocking_reasons(task: Task, get: Callable[[str], Task]) -> list[str]:
    reasons = []
    try:
        own_root = root_task(task, get)
        checked, visiting = set(), set()

        def visit(node):
            if node.id in visiting:
                raise TrackerError("Dependency relation cycle", "conflict")
            if node.id in checked:
                return
            visiting.add(node.id)
            for task_id in node.blockers:
                dependency = get(task_id)
                visit(dependency)
                if dependency.state != "Done":
                    reasons.append(f"Dependency {task_id} is not Done")
                dependency_root = root_task(dependency, get)
                if dependency_root.id != own_root.id and dependency_root.state != "Done":
                    reasons.append(f"Dependency {task_id} awaits parent {dependency_root.id} merge")
            visiting.remove(node.id)
            checked.add(node.id)

        current = task
        while True:
            visit(current)
            if not current.parent:
                break
            current = get(current.parent)
    except (KeyError, TrackerError) as exc:
        reasons.append(f"Dependency cannot be verified: {exc}")
    return list(dict.fromkeys(reasons))


def ready_tasks(tasks: list[Task], get: Callable[[str], Task], user: str | None = None):
    ready = []
    for task in tasks:
        if task.state != "Ready" or (task.assignees and task.assignees != [user]):
            continue
        try:
            parent = root_task(task, get)
            if parent.assignees and parent.assignees != [user]:
                continue
            if not blocking_reasons(task, get):
                ready.append(task)
        except (KeyError, TrackerError):
            continue
    return sorted(ready, key=lambda t: (-t.priority, t.project, t.id))


def checkboxes(body: str):
    result = []
    fence = None
    offset = 0
    for line in body.splitlines(keepends=True):
        stripped = line.lstrip()
        match = re.match(r"(`{3,}|~{3,})", stripped)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        elif fence is None:
            match = re.match(r"^(\s*(?:[-+*]|\d+[.)])\s+\[)([ xX])(\]\s+)(.*?)(\r?\n)?$", line)
            if match:
                result.append({"index": len(result) + 1, "checked": match[2].lower() == "x",
                               "text": match[4], "offset": offset,
                               "line": line})
        offset += len(line)
    return result


def set_checkboxes(body: str, indices, checked: bool) -> str:
    """Rewrite every named item at once, so one decision costs one description write.

    Quoting each item's current text back proved nothing the task's own revision
    does not prove for the whole description, and it forced one write, and one
    race with it, per checked item.
    """
    items = checkboxes(body)
    unknown = sorted({index for index in indices if not 1 <= index <= len(items)})
    if unknown:
        raise TrackerError(f"Checklist has {len(items)} items; no item "
                           + ", ".join(str(index) for index in unknown))
    for index in sorted(set(indices), reverse=True):
        target = items[index - 1]
        offset = target["offset"]
        line = re.sub(r"\[[ xX]\]", "[x]" if checked else "[ ]", target["line"], count=1)
        body = body[:offset] + line + body[offset + len(target["line"]):]
    return body


def claim(adapter: Adapter, task_id: str, resume=False) -> tuple[Task, bool]:
    """Return the confirmed task and whether this claim observed a new Ready start."""
    task = adapter.get(task_id)
    user = str(adapter.identity()["id"])
    if any(assignee != user for assignee in task.assignees):
        raise TrackerError(f"Task {task_id} is assigned to another user", "conflict")
    if task.state in ("Doing", "Review"):
        if not resume or task.assignees != [user]:
            raise TrackerError("Task is already Doing; use --resume after checking existing work", "conflict")
        blockers = blocking_reasons(task, adapter.get)
        if blockers:
            raise TrackerError("; ".join(blockers), "conflict")
        return (adapter.set_state(task, "Doing") if task.state == "Review" else task), False
    if task.state != "Ready":
        raise TrackerError(f"Task must be Ready, currently {task.state}", "conflict")
    blockers = blocking_reasons(task, adapter.get)
    if blockers:
        raise TrackerError("; ".join(blockers), "conflict")
    task = adapter.update(task, {"assignees": [user]})
    task = adapter.set_state(task, "Doing")
    if task.assignees != [user] or task.state != "Doing":
        raise TrackerError("Claim was not confirmed; inspect the task before continuing", "conflict")
    return task, True


def completion_reasons(task, tasks, user, *, states=("Doing", "Review"), checklist=True):
    """Why this task cannot be completed yet; `checklist` separates acceptance from completion.

    An item only the user can close, such as a check that cannot run from a
    container, used to block the round that judges the code as well as the
    completion that needs the check done. Acceptance asks for neither.
    """
    reasons = []
    if task.state not in states:
        reasons.append("Task must be " + " or ".join(states))
    if task.assignees != [user]:
        reasons.append("Task must remain assigned to the current API-key user")
    if any(child.state != "Done" for child in tasks if child.id != task.id):
        reasons.append("All subtasks must remain Done")
    if checklist and any(not item["checked"] for child in tasks for item in checkboxes(child.description)):
        reasons.append("Acceptance or validation checklist remains incomplete")
    return reasons
