"""Vikunja translates products and endpoints; the core owns workflow decisions."""
from __future__ import annotations

from .core import Task, TrackerError, STATES, root_task
from . import vikunja_api as api


class VikunjaAdapter:
    capabilities = frozenset({"tasks", "projects", "comments", "checklists", "relations",
                              "assignees", "boards", "optimistic_checks", "import", "bulk"})

    def __init__(self, url, token, binding=None, client=None):
        self.client = client or api.VikunjaClient(url, token)
        self.binding = binding

    def identity(self):
        return self.client.get("/user")

    def projects(self):
        return api.projects_from_client(self.client)

    def project(self, reference):
        if str(reference).isdigit():
            return self.client.get(f"/projects/{int(reference)}")
        return api.ProjectIndex(self.projects()).resolve(str(reference))

    def normalize(self, raw):
        related = raw.get("related_tasks") or {}
        state = "Done" if raw.get("done") else "Unknown"
        if self.binding and str(raw.get("project_id")) == self.binding.project and not raw.get("done"):
            bucket = api.task_bucket_in_view(raw, self.binding.view)
            if bucket:
                state = next((name for name, ident in self.binding.states.items()
                              if str(bucket["id"]) == ident), "Unknown")
        parent = related.get("parenttask") or []
        if len(parent) > 1:
            raise TrackerError(f"Task {raw['id']} has multiple parents", "conflict")
        return Task(str(raw["id"]), str(raw["project_id"]), raw["title"],
                    description=raw.get("description") or "", state=state,
                    assignees=[str(u["id"]) for u in raw.get("assignees") or []],
                    parent=str(parent[0]["id"]) if parent else None,
                    children=[str(t["id"]) for t in related.get("subtask") or []],
                    blockers=[str(t["id"]) for t in related.get("blocked") or []],
                    related=[str(t["id"]) for t in related.get("related") or []],
                    labels=[label["title"] for label in raw.get("labels") or []],
                    priority=raw.get("priority") or 0,
                    revision=self.client.revisions.get(f"/tasks/{raw['id']}", ""),
                    url=f"{self.client.url}/tasks/{raw['id']}")

    def get(self, task_id):
        try:
            task_id = str(int(task_id))
        except ValueError:
            raise TrackerError("Use an exact numeric Vikunja task ID; search first") from None
        return self.normalize(self.client.get(f"/tasks/{task_id}?format=markdown&expand=buckets&expand=subtasks"))

    def search(self, project, query="", recursive=False):
        return [self.normalize(raw) for raw in api.collect_tasks(
            self.client, query=query, project=project, recursive=recursive)]

    def update(self, task, fields):
        current = self.get(task.id)
        if current != task:
            raise TrackerError("Task changed; reread before updating", "conflict")
        fields = dict(fields)
        assignments = fields.pop("assignees", None)
        allowed = {"title", "description", "priority", "done", "due_date", "start_date", "end_date", "percent_done"}
        if fields.keys() - allowed:
            raise TrackerError(f"Unsupported task fields: {sorted(fields.keys() - allowed)}")
        if fields:
            self.client.revisions[f"/tasks/{task.id}"] = task.revision
            self.client.patch(f"/tasks/{task.id}", fields, markdown="description" in fields)
        if assignments is not None:
            # Assignments have their own endpoint; never mistake a task PATCH for a claim.
            current = self.get(task.id)
            if current.assignees != task.assignees:
                raise TrackerError("Assignment changed; reread before updating", "conflict")
            self.client.put(f"/tasks/{task.id}/assignees/bulk",
                            {"assignees": [{"id": int(user)} for user in assignments]})
        return self.get(task.id)

    def set_state(self, task, state):
        if state not in STATES:
            raise TrackerError(f"Unknown lifecycle state: {state}")
        binding = self.binding
        if not binding or task.project != binding.project:
            raise TrackerError("Changing state requires the task project's repository binding")
        view = api.resolve_kanban_view(self.client, task.project, binding.view)
        bucket = api.resolve_bucket(api.view_buckets(self.client, task.project, binding.view), binding.states[state])
        api.place_in_bucket(self.client, task.project, view, bucket, task.id)
        current = self.get(task.id)
        if (state == "Done") != (current.state == "Done"):
            current = self.update(current, {"done": state == "Done"})
        if current.state != state:
            raise TrackerError(f"State transition not confirmed: expected {state}, got {current.state}", "conflict")
        return current

    def create(self, project, title, description="", parent=None, priority=0):
        if parent:
            parent_task = self.get(parent)
            if parent_task.project != str(project):
                raise TrackerError("Parent and child must belong to the same project")
            root_task(parent_task, self.get)
        raw = self.client.post(f"/projects/{int(project)}/tasks?format=markdown",
                               {"title": title, "description": description, "priority": priority})
        task = self.get(str(raw["id"]))
        if parent:
            self.relate(task.id, "parent", parent)
        return self.set_state(self.get(task.id), "Backlog") if self.binding and task.project == self.binding.project else self.get(task.id)

    def comments(self, task_id):
        return self.client.paginate(f"/tasks/{int(task_id)}/comments", {"format": "markdown"})

    def comment(self, task_id, text):
        if not text.strip():
            raise TrackerError("Comment must not be empty")
        return self.client.post(f"/tasks/{int(task_id)}/comments?format=markdown", {"comment": text})

    def relate(self, task_id, kind, other, remove=False):
        mapping = {"parent": "parenttask", "child": "subtask", "blocks": "blocking",
                   "blocked-by": "blocked", "related": "related", "duplicate": "duplicateof"}
        if kind not in mapping or str(task_id) == str(other):
            raise TrackerError("Invalid relation")
        relation = mapping[kind]
        if not remove and kind in ("parent", "child", "blocks", "blocked-by"):
            origin, target = (str(other), str(task_id)) if kind in ("child", "blocks") else (str(task_id), str(other))
            if kind in ("parent", "child"):
                existing = self.get(origin)
                if existing.parent and existing.parent != target:
                    raise TrackerError("Task already has a different parent", "conflict")
                if existing.project != self.get(target).project:
                    raise TrackerError("Parent and child must belong to the same project")
            pending, seen = [target], set()
            while pending:
                node = pending.pop()
                if node == origin:
                    raise TrackerError("Relation would introduce a cycle", "conflict")
                if node in seen:
                    continue
                seen.add(node)
                task = self.get(node)
                pending.extend(([task.parent] if task.parent else []) if kind in ("parent", "child") else task.blockers)
        if remove:
            self.client.delete(f"/tasks/{int(task_id)}/relations/{relation}/{int(other)}")
        else:
            self.client.post(f"/tasks/{int(task_id)}/relations", {"other_task_id": int(other), "relation_kind": relation})
        return self.get(task_id)

    def set_tier(self, task_id, tier):
        if tier not in (1, 2, 3):
            raise TrackerError("Task tier must be 1, 2, or 3")
        raw = self.client.get(f"/tasks/{int(task_id)}")
        title = f"ab:tier:{tier}"
        labels = self.client.paginate("/labels", {"q": title})
        exact = [label for label in labels if label["title"] == title]
        if len(exact) > 1:
            raise TrackerError(f"Ambiguous label: {title}", "conflict")
        for label in raw.get("labels") or []:
            if label["title"].startswith("ab:tier:") and label["title"] != title:
                self.client.delete(f"/tasks/{int(task_id)}/labels/{label['id']}")
        label = exact[0] if exact else self.client.post("/labels", {"title": title})
        if not any(old["id"] == label["id"] for old in raw.get("labels") or []):
            api.attach_label(self.client, task_id, label["id"])
        return self.get(task_id)

    def lifecycle(self, project, view=None):
        selected = api.resolve_kanban_view(self.client, project, view)
        buckets = api.view_buckets(self.client, project, selected["id"])
        proposed = {}
        aliases = {"Backlog": ("backlog", "todo", "to do"), "Ready": ("ready",),
                   "Doing": ("doing", "in progress", "in-progress"), "Review": ("review", "in review"),
                   "Done": ("done", "completed")}
        for state, names in aliases.items():
            matches = [b for b in buckets if b["title"].casefold() in names]
            if len(matches) == 1:
                proposed[state] = str(matches[0]["id"])
        return {"view": selected, "buckets": buckets, "proposed": proposed,
                "missing": [state for state in STATES if state not in proposed]}
