"""Vikunja translates products and endpoints; the core owns workflow decisions."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .core import NORMALIZED_LABEL, Task, TrackerError, STATES, root_task
from . import vikunja_api as api

# Column titles agent-box accepts for each lifecycle state. A per-repository
# override table would be a second mapping language to keep correct; a project
# whose board does not say what it means is refused instead.
LIFECYCLE_ALIASES = {
    "Backlog": ("backlog", "todo", "to do", "to-do"),
    "Ready": ("ready",),
    "Doing": ("doing", "in progress", "in-progress"),
    "Review": ("review", "in review", "in-review"),
    "Done": ("done", "completed"),
}


class VikunjaAdapter:
    capabilities = frozenset({"tasks", "projects", "comments", "checklists", "relations",
                              "assignees", "boards", "optimistic_checks", "import", "bulk",
                              "clone_project_tree", "rename_project"})

    def __init__(self, url, token, binding=None, client=None):
        self.client = client or api.VikunjaClient(url, token)
        self.binding = binding
        # Browser links retain the binding identity when container transport rewrites the host.
        self.server = (binding.server if binding else self.client.url).rstrip("/")
        # Views and buckets are read once per process, never persisted: a cached
        # ID in the repository would outlive the column it names.
        self._boards = {}
        self._scope = None

    def projects_in_scope(self):
        """A recursive binding governs the root project and every project under it.

        Anything outside stays readable but unwritable, so a subtree binding can
        never move a card in an unrelated project that happens to be reachable.
        """
        if self._scope is None:
            if not self.binding:
                self._scope = frozenset()
            elif self.binding.recursive:
                self._scope = frozenset(str(ident) for ident in
                                        api.descendants(self.projects(), int(self.binding.project)))
            else:
                self._scope = frozenset({str(self.binding.project)})
        return self._scope

    def lifecycle(self, project, view=None):
        """The project's own board, its columns, and what they mean.

        Vikunja already knows which bucket means done, so that one is taken from
        the view rather than guessed from a title. The remaining states are
        matched by title; anything ambiguous is reported, never resolved.

        A project with no kanban view, or several, is reported the same way. It
        used to raise, which made every read of every task in that project fail,
        including the audit whose whole job is to describe boards like this one.
        """
        project = str(project)
        key = (project, str(view or ""))
        if key not in self._boards:
            try:
                selected = api.resolve_kanban_view(self.client, project, view)
            except (api.NotFound, api.AmbiguousMatch) as exc:
                self._boards[key] = {"view": None, "buckets": [], "states": {},
                                     "missing": list(STATES), "problems": [str(exc)]}
                return self._boards[key]
            buckets = api.view_buckets(self.client, project, selected["id"])
            titles = {}
            for bucket in buckets:
                titles.setdefault(str(bucket.get("title") or "").casefold(), []).append(str(bucket["id"]))
            known = {str(bucket["id"]) for bucket in buckets}
            states, missing, ambiguous = {}, [], []
            done = selected.get("done_bucket_id")
            if done and str(done) in known:
                states["Done"] = str(done)
            for state in STATES:
                if state in states:
                    continue
                found = [ident for alias in LIFECYCLE_ALIASES[state] for ident in titles.get(alias, [])]
                if len(found) == 1:
                    states[state] = found[0]
                elif found:
                    ambiguous.append(state)
                else:
                    missing.append(state)
            problems = []
            if selected.get("bucket_configuration_mode") == "filter":
                problems.append("board builds its columns from filters instead of manual buckets")
            if missing:
                problems.append("no column for " + ", ".join(missing))
            if ambiguous:
                problems.append("several columns match " + ", ".join(ambiguous))
            if len(set(states.values())) != len(states):
                problems.append("one column is claimed by more than one state")
            self._boards[key] = {"view": selected, "buckets": buckets, "states": states,
                                 "missing": missing, "problems": problems}
        return self._boards[key]

    def board_plan(self, project):
        """What repairing one board would add, without touching it."""
        project = str(project)
        resolved = self.lifecycle(project)
        plan = api.plan_board_init(resolved, STATES)
        return {"project": project, "view": resolved["view"]["id"] if resolved["view"] else None,
                "create": plan["create"], "done_bucket": plan["done_bucket"],
                "blocked": plan["blocked"], "applied": False,
                "states": resolved["states"], "problems": resolved["problems"]}

    def initialize_boards(self, projects, apply=False):
        """Add the lifecycle columns each board lacks; never rename or remove one.

        Setup can only refuse a board it does not understand, which leaves the
        user adding the same two columns by hand to every project in a subtree.
        Creating the missing ones is mechanical; everything that needs a decision
        stays reported and untouched.

        The whole subtree is planned before anything is written, so a project the
        repair cannot describe is reported next to the others instead of aborting
        the run halfway through and leaving the subtree half repaired.
        """
        plans = [self.board_plan(project) for project in projects]
        if not apply:
            return plans
        return [self.apply_board_plan(plan) for plan in plans]

    def apply_board_plan(self, plan):
        """Create one board's planned columns and point the view at its done column."""
        if plan["blocked"] or not (plan["create"] or plan["done_bucket"]):
            return plan
        project = plan["project"]
        resolved = self.lifecycle(project)
        for column in plan["create"]:
            api.create_bucket(self.client, project, resolved["view"], column["title"], column["position"])
        resolved = self.refresh_lifecycle(project)
        if plan["done_bucket"] and "Done" in resolved["states"]:
            api.set_done_bucket(self.client, project, resolved["view"], resolved["states"]["Done"])
            resolved = self.refresh_lifecycle(project)
        return {**plan, "applied": True, "states": resolved["states"],
                "problems": resolved["problems"]}

    def refresh_lifecycle(self, project):
        """Re-read a board after changing it; a cached view would report the old one."""
        self._boards.pop((str(project), ""), None)
        return self.lifecycle(project)

    def board(self, project):
        """Reading tolerates a broken board; moving a card across one does not."""
        resolved = self.lifecycle(project)
        if resolved["problems"]:
            raise TrackerError(f"Project {project} board cannot express the workflow lifecycle: "
                               + "; ".join(resolved["problems"]), "configuration")
        return resolved

    def subtree(self, project, recursive=False):
        """The project and, when asked, everything under it, as string IDs."""
        projects = self.projects()
        root = self.project(project)
        found = api.descendants(projects, root["id"]) if recursive else {int(root["id"])}
        return [str(ident) for ident in sorted(found)]

    def lifecycle_report(self, projects=None):
        """Per-project diagnostics, returned for rendering.

        Defaults to the bound subtree; an explicit list is what setup and board
        repair need, because both run before a binding exists.
        """
        report = []
        for project in sorted(projects or self.projects_in_scope(), key=lambda value: int(value)):
            try:
                resolved = self.lifecycle(project)
                report.append({"project": project,
                               "view": resolved["view"]["id"] if resolved["view"] else None,
                               "states": resolved["states"], "problems": resolved["problems"]})
            except TrackerError as exc:
                report.append({"project": project, "view": None, "states": {}, "problems": [str(exc)]})
        return report

    def identity(self):
        return self.client.get("/user")

    def projects(self):
        return [{**p, "parent": str(p.get("parent_project_id") or "")}
                for p in api.projects_from_client(self.client)]

    def project(self, reference):
        if str(reference).isdigit():
            project = self.client.get(f"/projects/{int(reference)}")
            return {**project, "parent": str(project.get("parent_project_id") or "")}
        return api.ProjectIndex(self.projects()).resolve(str(reference))

    def rename_project(self, project_id, title):
        if not title.strip() or len(title) > 250:
            raise TrackerError("Project title must contain 1-250 characters")
        current = self.project(project_id)
        if current["title"] != title:
            self.client.patch(f"/projects/{int(project_id)}", {"title": title})
        current = self.project(project_id)
        if current["title"] != title:
            raise TrackerError(f"Project {project_id} rename was not confirmed", "conflict")
        return current

    def clone_project_tree(self, source, title, progress, checkpoint, apply=False):
        from .vikunja_clone import clone
        return clone(self, source, title, progress, checkpoint, apply)

    def preserve_external_relations(self, relations, apply=False):
        from .vikunja_relations import preserve
        return preserve(self, relations, apply)

    def normalize(self, raw):
        related = raw.get("related_tasks") or {}
        # Done is Vikunja's own flag; the other states are where the card sits.
        state = "Done" if raw.get("done") else "Unknown"
        project = str(raw.get("project_id"))
        if state != "Done" and project in self.projects_in_scope():
            resolved = self.lifecycle(project)
            bucket = api.task_bucket_in_view(raw, resolved["view"]["id"]) if not resolved["problems"] else None
            if bucket:
                state = next((name for name, ident in resolved["states"].items()
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
                    url=f"{self.server}/tasks/{raw['id']}",
                    number=raw.get("index"))

    def get(self, task_id):
        try:
            task_id = str(int(task_id))
        except ValueError:
            raise TrackerError("Use a canonical numeric Vikunja task ID; run tracker resolve for user references") from None
        return self.normalize(self.client.get(f"/tasks/{task_id}?format=markdown&expand=buckets&expand=subtasks"))

    def resolve(self, reference, project, recursive=False, *, title=False):
        """User references resolve once; internal reads keep their canonical IDs."""
        text = reference.strip()
        if not text:
            raise TrackerError("Pass a visible task number, exact title or task URL")
        scope = set(self.subtree(project, recursive))
        if not title and text.startswith(("http://", "https://")):
            link, server = urlsplit(text), urlsplit(self.server)
            match = re.fullmatch(re.escape(server.path.rstrip("/")) + r"/tasks/([0-9]+)/?", link.path)
            if (link.scheme, link.netloc) != (server.scheme, server.netloc) or not match:
                raise TrackerError("Use a task URL from the configured tracker")
            task = self.get(match[1])
            if task.project not in scope:
                raise TrackerError("Task URL is outside the selected project scope")
            return task
        number = re.fullmatch(r"#?([0-9]+)", text) if not title else None
        if number:
            tasks = []
            for project_id in sorted(scope):
                try:
                    raw = self.client.get(
                        f"/projects/{project_id}/tasks/by-index/{int(number[1])}"
                        "?format=markdown&expand=buckets&expand=subtasks")
                except api.APIError as exc:
                    # Only a missing task is absence; incomplete reads cannot prove uniqueness.
                    if exc.status == 404 and exc.code == 4002:
                        continue
                    raise
                tasks.append(self.normalize(raw))
        else:
            tasks = self.search(project, query=text, recursive=recursive)
        matches = [task for task in tasks if task.project in scope and (
            task.number == int(number[1]) if number else task.title == text)]
        if not matches:
            raise TrackerError("No exact task match; specify a project, exact title or task URL", "not-found")
        if len(matches) > 1:
            projects = {str(p["id"]): p["title"] for p in self.projects()}
            choices = "; ".join(
                f"{projects.get(task.project, task.project)} (project {task.project}): "
                f"#{task.number} {task.title} (ID {task.id}, {task.url})" for task in matches)
            raise TrackerError(f"Ambiguous task reference: {choices}. Use --project or a task URL", "conflict")
        return self.get(matches[0].id)

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
        if "description" in fields:
            api.check_external_marker(task.id, task.description, fields["description"])
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
        if task.project not in self.projects_in_scope():
            raise TrackerError("Changing state requires a task inside the bound project subtree")
        resolved = self.board(task.project)
        bucket = api.resolve_bucket(resolved["buckets"], resolved["states"][state])
        api.place_in_bucket(self.client, task.project, resolved["view"], bucket, task.id)
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
        return self.set_state(self.get(task.id), "Backlog") if task.project in self.projects_in_scope() else self.get(task.id)

    def comments(self, task_id):
        return self.client.paginate(f"/tasks/{int(task_id)}/comments", {"format": "markdown"})

    def comment(self, task_id, text):
        if not text.strip():
            raise TrackerError("Comment must not be empty")
        return self.client.post(f"/tasks/{int(task_id)}/comments?format=markdown", {"comment": text})

    def relate(self, task_id, kind, other, remove=False):
        mapping = api.TASK_RELATION_KINDS
        if kind not in mapping or str(task_id) == str(other):
            raise TrackerError("Invalid relation")
        relation = mapping[kind]
        raw = self.client.get(f"/tasks/{int(task_id)}")
        present = str(other) in {str(t["id"]) for t in (raw.get("related_tasks") or {}).get(relation, [])}
        if present == (not remove):
            return self.get(task_id)
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
        raw = self.client.get(f"/tasks/{int(task_id)}")
        present = str(other) in {str(t["id"]) for t in (raw.get("related_tasks") or {}).get(relation, [])}
        if present != (not remove):
            raise TrackerError("Relation change was not confirmed", "conflict")
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


    def set_normalized(self, task_id, normalized=True):
        """The marker records completed processing, independently of task readiness."""
        raw = self.client.get(f"/tasks/{int(task_id)}")
        attached = [label for label in raw.get("labels") or [] if label["title"] == NORMALIZED_LABEL]
        if normalized and not attached:
            labels = self.client.paginate("/labels", {"q": NORMALIZED_LABEL})
            matches = [label for label in labels if label["title"] == NORMALIZED_LABEL]
            if len(matches) > 1:
                raise TrackerError(f"Ambiguous label: {NORMALIZED_LABEL}", "conflict")
            label = matches[0] if matches else self.client.post("/labels", {"title": NORMALIZED_LABEL})
            api.attach_label(self.client, task_id, label["id"])
        elif not normalized:
            for label in attached:
                self.client.delete(f"/tasks/{int(task_id)}/labels/{label['id']}")
        task = self.get(task_id)
        if (NORMALIZED_LABEL in task.labels) != normalized:
            raise TrackerError("Normalization marker change was not confirmed", "conflict")
        return task
