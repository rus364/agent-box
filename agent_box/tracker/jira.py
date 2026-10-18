"""Native issue keys, statuses and links implement the portable Jira contract."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import quote, urlsplit

from .core import NORMALIZED_LABEL, STATES, Task, TrackerError
from .jira_api import JiraClient

KEY = re.compile(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*")
PRIORITIES = {"lowest": 1, "low": 1, "medium": 2, "high": 3, "highest": 4, "urgent": 4, "immediate": 5}


class JiraAdapter:
    capabilities = frozenset({"tasks", "projects", "comments", "checklists", "relations", "assignees", "boards"})

    def __init__(self, url, token, binding=None, client=None, username=""):
        self.client = client or JiraClient(url, token, username)
        self.binding = binding
        self.server = (binding.server if binding else self.client.url).rstrip("/")
        self._boards = {}

    def identity(self):
        raw = self.client.request("GET", "/myself")
        user = raw["accountId"] if self.client.cloud else raw["name"]
        return {"id": user, "username": raw.get("name") or raw.get("emailAddress") or user}

    def project(self, reference):
        text = str(reference).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*|[0-9]+", text):
            raise TrackerError("Name a Jira project by its key or numeric project ID")
        raw = self.client.request("GET", "/project/" + quote(text, safe=""))
        return {**raw, "id": raw["key"], "title": raw["name"], "parent": ""}

    def projects(self):
        rows = (self.client.pages("/project/search", "values") if self.client.cloud
                else self.client.request("GET", "/project"))
        return [{"id": raw["key"], "title": raw["name"], "parent": ""} for raw in rows]

    def subtree(self, project, recursive=False):
        if recursive:
            raise TrackerError("Jira projects have no subprojects; drop --recursive", "unsupported")
        return [self.project(project)["id"]]

    def lifecycle(self, project):
        project = self.project(project)["id"]
        if project not in self._boards:
            types = self.client.request("GET", f"/project/{project}/statuses")
            states, problems = {}, []
            for issue_type in types:
                for state in STATES:
                    matches = {str(s["id"]) for s in issue_type["statuses"] if s["name"].casefold() == state.casefold()}
                    if len(matches) != 1:
                        problems.append(f"issue type {issue_type['name']}: expected one status for {state}")
                    elif state in states and states[state] not in matches:
                        problems.append(f"issue types disagree on status for {state}")
                    else:
                        states[state] = next(iter(matches))
            if not types:
                problems.append("project has no issue type workflows")
            self._boards[project] = {"project": project, "states": states, "problems": problems, "types": types}
        return self._boards[project]

    def board(self, project):
        result = self.lifecycle(project)
        if result["problems"]:
            raise TrackerError("Configure Jira workflow statuses: " + "; ".join(result["problems"]), "configuration")
        return result

    def lifecycle_report(self, projects=None):
        if projects is None:
            projects = self.subtree(self.binding.project, self.binding.recursive) if self.binding else []
        return [{k: v for k, v in self.lifecycle(project).items() if k != "types"} for project in projects]

    def initialize_boards(self, projects, apply=False):
        raise TrackerError("Configure Jira workflows in Jira administration; automatic board provisioning is unsupported",
                           "unsupported")

    def task_key(self, task_id):
        if not KEY.fullmatch(str(task_id)):
            raise TrackerError("Use a canonical Jira issue key such as PROJ-123; run tracker resolve for user references")
        return str(task_id)

    def raw(self, task_id):
        return self.client.request("GET", f"/issue/{self.task_key(task_id)}", params={"fields": "*all"})

    def normalize(self, raw):
        fields, key = raw["fields"], raw["key"]
        description = fields.get("description") or ""
        if not isinstance(description, str):
            raise TrackerError("Jira REST v2 returned a non-text description; refusing a lossy conversion", "unsupported")
        project = fields["project"]["key"]
        board = self.lifecycle(project)
        state = next((s for s, ident in board["states"].items() if ident == str(fields["status"]["id"])), "Unknown")
        reason = None
        if board["problems"]:
            state, reason = "Unknown", "project workflow: " + "; ".join(board["problems"])
        elif state == "Unknown":
            reason = f"Status {fields['status']['name']!r} maps to no lifecycle state"
        blockers, related = [], []
        for link in fields.get("issuelinks") or []:
            kind = link["type"]
            if "inwardIssue" in link and kind.get("outward", "").casefold() == "blocks":
                blockers.append(link["inwardIssue"]["key"])
            if kind.get("outward", "").casefold() == "relates to":
                related.append((link.get("inwardIssue") or link.get("outwardIssue"))["key"])
        assignee = fields.get("assignee")
        assignees = [assignee["accountId"] if self.client.cloud else assignee["name"]] if assignee else []
        # Jira comments may change independently of the issue timestamp.
        journal = self.comments(key)
        revision = hashlib.sha256(json.dumps([fields.get("updated"), journal], sort_keys=True).encode()).hexdigest()
        return Task(id=key, project=project, title=fields["summary"], description=description, state=state,
                    assignees=assignees, parent=(fields.get("parent") or {}).get("key"),
                    children=[row["key"] for row in fields.get("subtasks") or []], blockers=blockers, related=related,
                    labels=list(fields.get("labels") or []), priority=PRIORITIES.get(
                        (fields.get("priority") or {}).get("name", "").casefold(), 0), revision=revision,
                    url=f"{self.server}/browse/{key}", number=int(key.rsplit("-", 1)[1]), state_reason=reason)

    def get(self, task_id):
        return self.normalize(self.raw(task_id))

    def resolve(self, reference, project, recursive=False, *, title=False, canonical=False):
        scope = self.subtree(project, recursive)
        text = str(reference).strip()
        key = None
        if not title and text.startswith(("http://", "https://")):
            link, server = urlsplit(text), urlsplit(self.server)
            path = re.fullmatch(re.escape(server.path.rstrip("/")) + r"/browse/([A-Z][A-Z0-9_]*-[1-9][0-9]*)/?", link.path)
            if (link.scheme, link.netloc) != (server.scheme, server.netloc) or not path or link.query or link.fragment:
                raise TrackerError("Use an issue URL from the configured Jira site")
            key = path[1]
        elif not title and KEY.fullmatch(text):
            if canonical:
                return self.get(text)
            key = text
        elif not title and re.fullmatch(r"#?[1-9][0-9]*", text):
            key = scope[0] + "-" + text.lstrip("#")
        if key:
            task = self.get(key)
            if task.project not in scope:
                raise TrackerError("Issue is outside the selected project scope", "conflict")
            return task
        matches = [task for task in self.search(scope[0], text) if task.title == text]
        if len(matches) != 1:
            raise TrackerError("No exact task match" if not matches else
                               "Ambiguous title; use an issue key: " + ", ".join(t.id for t in matches),
                               "not-found" if not matches else "conflict")
        return matches[0]

    def search(self, project, query="", recursive=False, *, open_only=False):
        project = self.subtree(project, recursive)[0]
        rows = self.client.search(f'project = "{project}" ORDER BY key ASC')
        # Enhanced Cloud search can return ADF even under /api/2; hydrate through
        # the issue text endpoint so checklists never depend on a lossy conversion.
        tasks = [self.get(raw["key"]) if self.client.cloud else self.normalize(raw) for raw in rows]
        return [task for task in tasks if (not open_only or task.state != "Done") and
                query.casefold() in (task.title + "\n" + task.description).casefold()]

    def writable(self, task_id):
        task = self.get(task_id)
        if not self.binding or task.project not in self.subtree(self.binding.project, self.binding.recursive):
            raise TrackerError("Writing Jira tasks requires a task inside the repository binding", "conflict")
        return task

    def fresh(self, task):
        current = self.writable(task.id)
        if current != task:
            raise TrackerError("Task changed; reread before updating", "conflict")
        return current

    def priority(self, value):
        if value == 0:
            return None
        name = {1: "low", 2: "medium", 3: "high", 4: "urgent", 5: "immediate"}.get(value)
        if not name:
            raise TrackerError("Priority must be 0 through 5")
        rows = self.client.request("GET", "/priority")
        found = [r for r in rows if r["name"].casefold() == name]
        if not found and value == 4:
            found = [r for r in rows if r["name"].casefold() == "highest"]
        if len(found) != 1:
            raise TrackerError(f"Configure one Jira priority named {name.title()}", "configuration")
        return {"id": str(found[0]["id"])}

    def update(self, task, fields):
        self.fresh(task)
        if fields.keys() - {"title", "description", "priority", "assignees"}:
            raise TrackerError("Unsupported Jira task fields", "unsupported")
        mapped = {}
        for key, value in fields.items():
            if key == "assignees":
                if len(value) > 1:
                    raise TrackerError("Jira supports one assignee", "unsupported")
                mapped["assignee"] = ({"accountId" if self.client.cloud else "name": str(value[0])} if value else None)
            elif key == "priority":
                mapped[key] = self.priority(value)
            else:
                mapped[{"title": "summary", "description": "description"}[key]] = value
        self.client.request("PUT", f"/issue/{task.id}", {"fields": mapped})
        result = self.get(task.id)
        if any(getattr(result, key) != value for key, value in fields.items()):
            raise TrackerError("Jira field update was not confirmed; reread before continuing", "conflict")
        return result

    def set_state(self, task, state, unimplemented=False):
        self.fresh(task)
        if state not in STATES:
            raise TrackerError("Unknown lifecycle state")
        board = self.board(task.project)
        if task.state == state:
            return task
        rows = self.client.request("GET", f"/issue/{task.id}/transitions")["transitions"]
        matches = [r for r in rows if str(r["to"]["id"]) == board["states"][state]]
        if len(matches) != 1:
            raise TrackerError(f"Expected one available Jira transition to {state}; found {len(matches)}", "configuration")
        self.client.request("POST", f"/issue/{task.id}/transitions", {"transition": {"id": matches[0]["id"]}})
        result = self.get(task.id)
        if result.state != state:
            raise TrackerError("Jira transition was not confirmed; reread before continuing", "conflict")
        return result

    def create(self, project, title, description="", parent=None, priority=0):
        project = self.project(project)["id"]
        if not self.binding or project not in self.subtree(self.binding.project, self.binding.recursive):
            raise TrackerError("Create Jira tasks inside the repository binding", "conflict")
        board = self.board(project)
        types = [row for row in board["types"] if bool(row.get("subtask")) == bool(parent)]
        preferred = "sub-task" if parent else "task"
        exact = [row for row in types if row["name"].casefold() == preferred]
        selected = exact or types
        if len(selected) != 1:
            raise TrackerError("Configure one Jira Task/Sub-task issue type", "configuration")
        fields = {"project": {"key": project}, "issuetype": {"id": selected[0]["id"]},
                  "summary": title, "description": description}
        if parent:
            parent_task = self.writable(parent)
            if parent_task.project != project or parent_task.parent:
                raise TrackerError("A native Jira subtask needs a top-level parent in the same project")
            fields["parent"] = {"key": parent_task.id}
        if priority:
            fields["priority"] = self.priority(priority)
        created = self.client.request("POST", "/issue", {"fields": fields})
        if not isinstance(created, dict) or not KEY.fullmatch(str(created.get("key", ""))):
            raise TrackerError("Jira create reply contains no issue key; write outcome unknown. "
                               "Search the project before creating another issue", "unknown-outcome")
        # A failed subsequent transition must leave the created key discoverable.
        try:
            return self.set_state(self.get(created["key"]), "Backlog")
        except TrackerError as exc:
            raise TrackerError(f"Created Jira issue {created['key']}; initialization failed: {exc}. "
                               "Inspect this issue before creating another", exc.category, False) from None

    def comments(self, task_id):
        rows = self.client.pages(f"/issue/{self.task_key(task_id)}/comment", "comments")
        return [{"id": str(row["id"]), "comment": row.get("body") or "", "created": row.get("created"),
                 "updated": row.get("updated"), "author": (row.get("author") or {}).get("displayName")} for row in rows]

    def comment(self, task_id, text):
        self.writable(task_id)
        if not text.strip():
            raise TrackerError("Comment must not be empty")
        result = self.client.request("POST", f"/issue/{task_id}/comment", {"body": text})
        if not isinstance(result, dict) or not result.get("id"):
            raise TrackerError("Jira comment reply contains no ID; write outcome unknown. "
                               "Reread comments before repeating it", "unknown-outcome")
        return result

    def labels(self, task_id, transform):
        task = self.writable(task_id)
        self.fresh(task)
        labels = sorted(transform(set(task.labels)))
        self.client.request("PUT", f"/issue/{task_id}", {"fields": {"labels": labels}})
        result = self.get(task_id)
        if sorted(result.labels) != labels:
            raise TrackerError("Jira labels were not confirmed", "conflict")
        return result

    def set_tier(self, task_id, tier):
        if tier not in (1, 2, 3):
            raise TrackerError("Task tier must be 1, 2, or 3")
        return self.labels(task_id, lambda labels: {s for s in labels if not s.startswith("ab:tier:")} | {f"ab:tier:{tier}"})

    def set_normalized(self, task_id, normalized=True):
        return self.labels(task_id, lambda labels: labels | {NORMALIZED_LABEL} if normalized else labels - {NORMALIZED_LABEL})

    def relate(self, task_id, kind, other, remove=False):
        if kind in {"parent", "child"}:
            raise TrackerError("Create native Jira subtasks with create --parent; reparenting existing issues is unsupported",
                               "unsupported")
        outward = {"blocks": "blocks", "blocked-by": "blocks", "related": "relates to", "duplicate": "duplicates"}.get(kind)
        if not outward or task_id == other:
            raise TrackerError("Invalid relation")
        task, target = self.writable(task_id), self.writable(other)
        if kind in {"blocks", "blocked-by"} and not remove:
            origin, dependency = (other, task_id) if kind == "blocks" else (task_id, other)
            pending, seen = [dependency], set()
            while pending:
                node = pending.pop()
                if node == origin:
                    raise TrackerError("Relation would introduce a dependency cycle", "conflict")
                if node not in seen:
                    seen.add(node)
                    pending.extend(self.get(node).blockers)
        types = self.client.request("GET", "/issueLinkType")["issueLinkTypes"]
        matches = [row for row in types if row.get("outward", "").casefold() == outward]
        if len(matches) != 1:
            raise TrackerError(f"Configure one Jira link type with outward description {outward!r}", "configuration")
        link_type = matches[0]
        source, destination = (other, task_id) if kind == "blocked-by" else (task_id, other)
        raw = self.raw(source)
        existing = [link for link in raw["fields"].get("issuelinks") or []
                    if str(link["type"]["id"]) == str(link_type["id"]) and
                    ((link.get("outwardIssue") or {}).get("key") == destination or
                     kind == "related" and (link.get("inwardIssue") or {}).get("key") == destination)]
        self.fresh(task)
        self.fresh(target)
        if remove:
            for link in existing:
                self.client.request("DELETE", f"/issueLink/{quote(str(link['id']), safe='')}")
        elif not existing:
            self.client.request("POST", "/issueLink", {"type": {"id": link_type["id"]},
                                "inwardIssue": {"key": source}, "outwardIssue": {"key": destination}})
        links = self.raw(source)["fields"].get("issuelinks") or []
        present = any(str(link["type"]["id"]) == str(link_type["id"]) and
                      ((link.get("outwardIssue") or {}).get("key") == destination or
                       kind == "related" and (link.get("inwardIssue") or {}).get("key") == destination)
                      for link in links)
        if present != (not remove):
            raise TrackerError("Jira relation change was not confirmed", "conflict")
        return self.get(task_id)
