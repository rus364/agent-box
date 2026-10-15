"""GitHub Projects translates a project board and its issues; the core owns workflow decisions."""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from .core import NORMALIZED_LABEL, STATES, Task, TrackerError
from . import github_api as api

#: Priority options, highest first, as the board lists them: the board sorts and
#: groups by option order, so this order is part of what the names mean.
PRIORITIES = ("Immediate", "Urgent", "High", "Medium", "Low")
PRIORITY_VALUES = {name.casefold(): 5 - index for index, name in enumerate(PRIORITIES)}
CLOSE_REASONS = {"NOT_PLANNED": "closed as not planned", "DUPLICATE": "closed as a duplicate"}
RELATIONS = ("parent", "child", "blocks", "blocked-by", "related", "duplicate")
#: Pauses before rereading a write; GitHub applies project workflows a few seconds late.
CONFIRM_DELAYS = (0, 1, 2, 4)


def repository_from_url(url, web_host):
    """owner/name of a Git remote on this GitHub instance, or None."""
    if not url:
        return None
    match = None if "://" in url else re.fullmatch(r"git@([^:/]+):(.+)", url)
    if match:
        host, path = match[1], match[2]
    else:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "ssh") or parts.password:
            return None
        host, path = parts.hostname, parts.path.lstrip("/")
    path = path.removesuffix("/").removesuffix(".git")
    if (host or "").casefold() != web_host.casefold() or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        return None
    return path


class GitHubAdapter:
    """A binding names one project; its tasks are the issues of this repository on it.

    The repository is read from origin rather than stored: the binding file lives
    in that repository, so a second copy could only disagree with it. Issues of
    other repositories on the same board stay readable, as blockers for example,
    and are never written from here.
    """
    capabilities = frozenset({"tasks", "projects", "comments", "checklists", "relations", "assignees", "boards"})

    def __init__(self, url, token, binding=None, client=None, repository=None):
        if binding and binding.recursive:
            raise TrackerError("GitHub projects have no subprojects; bind without recursive", "configuration")
        self.client = client or api.GitHubClient(url, token)
        self.binding = binding
        self._repository = repository
        # Fields and options are read once per process, never persisted: an option
        # ID kept in the repository would outlive the option it names.
        self._projects = {}
        self.sleep = time.sleep

    def repository(self):
        if self._repository is None:
            from ..git_workflow import effective_push_url
            from ..repo_binding import repository_root
            try:
                url = effective_push_url(repository_root())
            except Exception:
                url = None
            found = repository_from_url(url, self.client.web_host)
            if not found:
                raise TrackerError(f"A GitHub tracker binding needs an origin on {self.client.web_host}; "
                                   "its issues are this repository's issues", "configuration")
            self._repository = found
        return self._repository

    def project_key(self, reference):
        """owner/number from a key or a project URL."""
        text = str(reference).strip()
        if text.startswith(("http://", "https://")):
            parts = urlsplit(text)
            match = re.match(r"/(?:users|orgs)/([^/]+)/projects/([0-9]+)(?:/|$)", parts.path)
            if (parts.hostname or "").casefold() != self.client.web_host.casefold() or not match:
                raise TrackerError(f"Use a project URL from {self.client.web_host}")
            return f"{match[1]}/{match[2]}"
        match = re.fullmatch(r"([A-Za-z0-9-]+)/([0-9]+)", text)
        if not match:
            raise TrackerError("Name a GitHub project as owner/number or by its URL")
        return f"{match[1]}/{match[2]}"

    def project(self, reference):
        key = self.project_key(reference)
        if key.casefold() not in self._projects:
            owner, number = key.split("/")
            self._projects[key.casefold()] = api.project(self.client, owner, number)
        return self._projects[key.casefold()]

    def projects(self):
        owners = [api.viewer(self.client)["login"]]
        if self.binding:
            owners.append(self.project_key(self.binding.project).split("/")[0])
        found = {}
        for owner in dict.fromkeys(owner.casefold() for owner in owners):
            for project in api.projects(self.client, owner):
                found.setdefault(project["id"].casefold(), project)
        return list(found.values())

    def subtree(self, project, recursive=False):
        if recursive:
            raise TrackerError("GitHub projects have no subprojects; drop --recursive")
        return [self.project(project)["id"]]

    def bound_project(self):
        if not self.binding:
            raise TrackerError("This operation needs a repository binding; run ab-setup")
        return self.project(self.binding.project)

    def single_select(self, project, name):
        """The project's single-select field with this name, ignoring case, or a problem."""
        found = [field for field in project["fields"] if field["name"].casefold() == name.casefold()]
        if len(found) > 1:
            return None, f"several fields are named {name}"
        if not found or found[0]["type"] != "SINGLE_SELECT":
            return None, f"no single-select {name} field"
        return found[0], None

    def lifecycle(self, project):
        """The project's Status field and which option means which state.

        Options are matched by name, ignoring case. GitHub's Board template says
        Todo where the workflow says Backlog and Ready; a synonym table would be
        one more mapping to keep right, so board init renames the option instead.
        """
        resolved = self.project(project)
        field, problem = self.single_select(resolved, "Status")
        if not field:
            return {"project": resolved["id"], "field": None, "options": [], "states": {},
                    "missing": list(STATES), "problems": [problem]}
        names = {}
        for option in field["options"]:
            names.setdefault(option["name"].casefold(), []).append(option["id"])
        states, missing, ambiguous = {}, [], []
        for state in STATES:
            found = names.get(state.casefold(), [])
            if len(found) == 1:
                states[state] = found[0]
            elif found:
                ambiguous.append(state)
            else:
                missing.append(state)
        problems = []
        if missing:
            problems.append("no Status option for " + ", ".join(missing))
        if ambiguous:
            problems.append("several Status options match " + ", ".join(ambiguous))
        return {"project": resolved["id"], "field": field["id"], "options": field["options"], "states": states,
                "missing": missing, "problems": problems}

    def board(self, project):
        """Reading tolerates a broken board; moving a card across one does not."""
        resolved = self.lifecycle(project)
        if resolved["problems"]:
            raise TrackerError(f"Project {resolved['project']} board cannot express the workflow lifecycle: "
                               + "; ".join(resolved["problems"]), "configuration")
        return resolved

    def lifecycle_report(self, projects=None):
        report = []
        for project in projects or [self.bound_project()["id"]]:
            try:
                resolved = self.lifecycle(project)
                report.append({"project": resolved["project"], "field": resolved["field"],
                               "states": resolved["states"], "problems": resolved["problems"]})
            except TrackerError as exc:
                report.append({"project": str(project), "field": None, "states": {}, "problems": [str(exc)]})
        return report

    def identity(self):
        login = api.viewer(self.client)["login"]
        return {"id": login, "username": login}

    def task_id(self, reference):
        """The canonical ID of an issue: its number here, owner/name#number elsewhere."""
        repository, number = reference
        return str(number) if repository.casefold() == self.repository().casefold() else f"{repository}#{number}"

    def issue_ref(self, task_id):
        text = str(task_id).strip()
        if re.fullmatch(r"[0-9]+", text):
            return self.repository(), int(text)
        match = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)", text)
        if not match:
            raise TrackerError("Use a canonical GitHub task ID: an issue number of this repository or owner/name#number;"
                               " run tracker resolve for user references")
        return match[1], int(match[2])

    def normalize(self, raw, board=None):
        """``board`` is the project a search reads; otherwise the bound one gives the column."""
        own = raw["repository"].casefold() == self.repository().casefold()
        bound = board or (self.bound_project() if self.binding else None)
        item = next((item for item in raw["items"] if bound and item["project"] == bound["node"]), None)
        state, reason = self.issue_state(raw, item, bound)
        priority = 0
        if item:
            field, _ = self.single_select(bound, "Priority")
            value = item["values"].get(field["id"]) if field else None
            priority = PRIORITY_VALUES.get((value or {}).get("name", "").casefold(), 0)
        return Task(self.task_id((raw["repository"], raw["number"])),
                    bound["id"] if own and item else "", raw["title"], description=raw["body"],
                    state=state, assignees=list(raw["assignees"]),
                    parent=self.task_id(raw["parent"]) if raw["parent"] else None,
                    children=[self.task_id(child) for child in raw["children"]],
                    blockers=[self.task_id(blocker) for blocker in raw["blocked_by"]],
                    related=[self.task_id(other) for other in raw["related"]],
                    labels=list(raw["labels"]), priority=priority,
                    revision=f"{raw['updated_at']}|{item['updated_at'] if item else ''}",
                    url=raw["url"], number=raw["number"], state_reason=reason)

    def issue_state(self, raw, item, bound):
        """Closed means Done; an open issue's state is its Status option.

        The issue's own state wins because GitHub keeps the two apart: reopening
        leaves the card in Done, and a closed issue may sit anywhere once the
        board's Item closed workflow is off. An open issue in Done is reported
        rather than read either way.
        """
        if raw["state"] == "CLOSED":
            if raw["state_reason"] == "DUPLICATE" and raw["duplicate_of"]:
                return "Done", f"closed as a duplicate of {self.task_id(raw['duplicate_of'])}"
            return "Done", CLOSE_REASONS.get(raw["state_reason"])
        if not item:
            return "Unknown", ("issue is not on the project" if bound
                               else "no project is bound, so the issue has no lifecycle column")
        resolved = self.lifecycle(bound["id"])
        if resolved["problems"]:
            return "Unknown", "project board: " + "; ".join(resolved["problems"])
        value = item["values"].get(resolved["field"])
        if not value:
            return "Unknown", "issue has no Status on the project"
        state = next((name for name, option in resolved["states"].items() if option == value["option"]), None)
        if state == "Done":
            return "Unknown", "issue is open but its Status is Done; close it or move it"
        if state:
            return state, None
        return "Unknown", f"Status {value['name']!r} maps to no lifecycle state"

    def get(self, task_id):
        repository, number = self.issue_ref(task_id)
        return self.normalize(api.issue(self.client, repository, number))

    def resolve(self, reference, project, recursive=False, *, title=False, canonical=False):
        """Visible numbers are canonical IDs here, so a bare number has one reading.

        It names an issue of this repository; only the board check differs between
        a canonical ID, readable anywhere, and a visible number on the board.
        """
        text = reference.strip()
        if not text:
            raise TrackerError("Pass a visible task number, exact title or task URL")
        scope = set(self.subtree(project, recursive))
        if not title and text.startswith(("http://", "https://")):
            parts = urlsplit(text)
            match = re.fullmatch(r"/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([0-9]+)/?", parts.path)
            if (parts.hostname or "").casefold() != self.client.web_host.casefold() or not match:
                raise TrackerError(f"Use an issue URL from {self.client.web_host}")
            task = self.found(match[1], int(match[2]))
            if task.project not in scope:
                raise TrackerError("Task URL is outside the selected project scope")
            return task
        if not title and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+", text):
            # Another repository's issue is read by its canonical ID, never written.
            return self.found(*self.issue_ref(text))
        number = re.fullmatch(r"#?([0-9]+)", text) if not title else None
        if number:
            task = self.found(self.repository(), int(number[1]))
            # A bare number given as a canonical ID reads like any other ID, on
            # the board or not; #N and resolve still name a card on the board.
            if task.project not in scope and not (canonical and not text.startswith("#")):
                raise TrackerError(f"Issue #{number[1]} is not on project {', '.join(sorted(scope))}", "not-found")
            return task
        matches = [task for task in self.search(project, text) if task.title == text]
        if not matches:
            raise TrackerError("No exact task match; specify an exact title, issue number or URL", "not-found")
        if len(matches) > 1:
            raise TrackerError("Ambiguous task reference: " + "; ".join(
                f"#{task.number} {task.title} ({task.url})" for task in matches) + ". Use the issue number", "conflict")
        return matches[0]

    def found(self, repository, number):
        try:
            return self.normalize(api.issue(self.client, repository, number))
        except api.APIError as exc:
            if exc.category == "not-found":
                raise TrackerError(f"No issue {repository}#{number}", "not-found") from None
            raise

    def search(self, project, query="", recursive=False, *, open_only=False):
        """This repository's issues on the board; a substring of title or body narrows them.

        GitHub's own filter only matches words, so the substring is applied here;
        the open filter runs on the server, where it saves reading every closed issue.
        """
        resolved = self.project(self.subtree(project, recursive)[0])
        needle = query.casefold()
        raws = api.project_issues(self.client, resolved["node"], "is:open" if open_only else "")
        tasks = [self.normalize(raw, resolved) for raw in raws
                 if raw["repository"].casefold() == self.repository().casefold()
                 and (needle in raw["title"].casefold() or needle in raw["body"].casefold())]
        return [task for task in tasks if not open_only or task.state != "Done"]

    def comments(self, task_id):
        repository, number = self.issue_ref(task_id)
        return api.comments(self.client, repository, number)

    # Writes. Only this repository's issues on the bound board are written, and
    # every write is confirmed by reading it back.

    def writable(self, task_id):
        """The issue, its card on the bound board and the board, or a refusal before any write."""
        repository, number = self.issue_ref(task_id)
        raw = api.issue(self.client, repository, number)
        bound = self.bound_project()
        item = next((item for item in raw["items"] if item["project"] == bound["node"]), None)
        if raw["repository"].casefold() != self.repository().casefold() or not item:
            raise TrackerError(f"Task {task_id} is outside the repository binding: only this repository's issues "
                               f"on project {bound['id']} are written", "conflict")
        return raw, item, bound

    def column(self, raw, bound):
        """The Status option name of the issue's card on the bound board, or None."""
        item = next((item for item in raw["items"] if item["project"] == bound["node"]), None)
        field, _ = self.single_select(bound, "Status")
        value = (item or {}).get("values", {}).get(field["id"]) if field else None
        return value["name"] if value else None

    def confirm(self, task_id, check, message):
        """Reread until the write shows; a board workflow may land a few seconds after it.

        ``check`` sees the task and its card's Status: a closed issue reads as Done
        wherever its card sits, so a Done transition has to look at the card too.
        """
        bound = self.bound_project()
        for delay in CONFIRM_DELAYS:
            self.sleep(delay)
            raw = api.issue(self.client, *self.issue_ref(task_id))
            task = self.normalize(raw)
            if check(task, self.column(raw, bound)):
                return task
        raise TrackerError(f"{message}; inspect task {task_id} in the tracker", "conflict")

    def set_option(self, bound, item, name, option):
        field, problem = self.single_select(bound, name)
        if not field:
            raise TrackerError(f"Project {bound['id']}: {problem}; run tracker provider github board init", "configuration")
        if option is None:
            api.mutate(self.client, "ClearField", projectId=bound["node"], itemId=item["id"], fieldId=field["id"])
            return
        found = [choice for choice in field["options"] if choice["name"].casefold() == option.casefold()]
        if len(found) != 1:
            raise TrackerError(f"Project {bound['id']} has no single {name} option {option}; "
                               "run tracker provider github board init", "configuration")
        api.mutate(self.client, "SetField", projectId=bound["node"], itemId=item["id"], fieldId=field["id"],
                   value={"singleSelectOptionId": found[0]["id"]})

    def priority_option(self, bound, priority):
        """The option name for a priority, checked before the first write."""
        if not priority:
            return None
        name = PRIORITIES[5 - int(priority)]
        field, problem = self.single_select(bound, "Priority")
        if not field or not any(choice["name"].casefold() == name.casefold() for choice in field["options"]):
            raise TrackerError(f"Project {bound['id']} has no Priority option {name}; "
                               "run tracker provider github board init", "configuration")
        return name

    def set_state(self, task, state, unimplemented=False):
        """Done is a closed issue and the Done column; agent-box writes both.

        The card moves first. A failure between the two writes then leaves an
        open issue in Done, which reads as Unknown and is finished by any rerun;
        the other order would leave a closed issue in its old column reading as
        Done, which nothing would revisit. The board's Auto-close workflow may
        close the issue as completed in between; closing it again still sets
        the reason given here, as GitHub lets a closed issue's reason change.
        Leaving Done reopens the issue, because reopening leaves the card where it was.
        """
        if state not in STATES:
            raise TrackerError(f"Unknown lifecycle state: {state}")
        raw, item, bound = self.writable(task.id)
        status = self.option_name(bound, state)
        reason = None
        if state != "Done" and raw["state"] == "CLOSED":
            api.mutate(self.client, "ReopenIssue", issueId=raw["id"])
        self.set_option(bound, item, "Status", status)
        # Closing without implementation is also a correction of an issue the board
        # already closed as completed, for instance when our own close was lost.
        if state == "Done" and (raw["state"] == "OPEN" or unimplemented and raw["state_reason"] != "NOT_PLANNED"):
            reason = "NOT_PLANNED" if unimplemented else "COMPLETED"
            api.mutate(self.client, "CloseIssue", issueId=raw["id"], stateReason=reason)
        expected = CLOSE_REASONS.get(reason)
        return self.confirm(task.id, lambda current, column: current.state == state and column == status
                            and (reason is None or current.state_reason == expected),
                            f"State transition not confirmed: expected {state}")

    def update(self, task, fields):
        current = self.get(task.id)
        if current != task:
            raise TrackerError("Task changed; reread before updating", "conflict")
        fields = dict(fields)
        unsupported = fields.keys() - {"title", "description", "priority", "assignees"}
        if unsupported:
            raise TrackerError(f"Unsupported task fields: {sorted(unsupported)}")
        raw, item, bound = self.writable(task.id)
        option = self.priority_option(bound, fields["priority"]) if "priority" in fields else None
        text = {name: fields[key] for key, name in (("title", "title"), ("description", "body")) if key in fields}
        if text:
            api.mutate(self.client, "UpdateIssue", id=raw["id"], **text)
        if "priority" in fields:
            self.set_option(bound, item, "Priority", option)
        if "assignees" in fields:
            api.mutate(self.client, "ReplaceAssignees", assignableId=raw["id"], actorLogins=list(fields["assignees"]))
        expected = {"title": "title", "description": "description", "priority": "priority", "assignees": "assignees"}
        return self.confirm(task.id, lambda found, _: all(getattr(found, expected[key]) == value
                                                       for key, value in fields.items()),
                            "Task update not confirmed")

    def create(self, project, title, description="", parent=None, priority=0):
        """A new issue here, on the bound board in Backlog; everything is checked before it exists."""
        bound = self.bound_project()
        if self.project(project)["id"] != bound["id"]:
            raise TrackerError("Create the task on the bound project")
        self.board(bound["id"])
        option = self.priority_option(bound, priority)
        if parent:
            self.writable(parent)
        repository = api.repository(self.client, self.repository())
        created = api.mutate(self.client, "CreateIssue", repositoryId=repository["id"], title=title,
                             body=description)["createIssue"]["issue"]
        item = {"id": api.mutate(self.client, "AddItem", projectId=bound["node"],
                                 contentId=created["id"])["addProjectV2ItemById"]["item"]["id"]}
        self.set_option(bound, item, "Status", self.option_name(bound, "Backlog"))
        if option:
            self.set_option(bound, item, "Priority", option)
        task_id = str(created["number"])
        if parent:
            self.relate(task_id, "parent", parent)
        backlog = self.option_name(bound, "Backlog")
        return self.confirm(task_id, lambda task, column: task.state == "Backlog" and column == backlog
                            and task.priority == priority,
                            "New task's Backlog column was not confirmed")

    def option_name(self, bound, state):
        resolved = self.board(bound["id"])
        return next(option["name"] for option in resolved["options"] if option["id"] == resolved["states"][state])

    def comment(self, task_id, text):
        if not text.strip():
            raise TrackerError("Comment must not be empty")
        raw, _, _ = self.writable(task_id)
        node = api.mutate(self.client, "AddComment", subjectId=raw["id"], body=text)["addComment"]["commentEdge"]["node"]
        return {"id": node["id"], "url": node.get("url"), "comment": node.get("body", text)}

    def related_to(self, task_id, kind, other):
        task = self.get(task_id)
        if kind == "parent":
            return task.parent == other
        if kind == "child":
            return other in task.children
        if kind == "blocks":
            return task_id in self.get(other).blockers
        if kind == "blocked-by":
            return other in task.blockers
        if kind == "related":
            return other in task.related
        raw = api.issue(self.client, *self.issue_ref(task_id))
        return bool(raw["duplicate_of"]) and self.task_id(raw["duplicate_of"]) == other

    def relate(self, task_id, kind, other, remove=False):
        """Every relation is GitHub's own: sub-issues, dependencies, relates-to and duplicates.

        A duplicate is a closing reason on GitHub, so marking one closes the task
        as Done, and only an open issue can be marked. Removing the mark leaves
        the issue closed, as GitHub does; reopening is a state change.
        """
        task_id, other = str(task_id), str(other)
        if kind not in RELATIONS or task_id == other:
            raise TrackerError("Invalid relation")
        raw, item, bound = self.writable(task_id)
        other_raw, _, _ = self.writable(other)
        # A duplicate closes the issue, so its Done column is resolved before any
        # write, and a rerun after a broken board finishes moving the card.
        done = self.option_name(bound, "Done") if kind == "duplicate" and not remove else None
        if self.related_to(task_id, kind, other) == (not remove):
            if done and self.column(raw, bound) != done:
                self.set_option(bound, item, "Status", done)
                return self.confirm(task_id, lambda _, column: column == done, "Duplicate's Done column was not confirmed")
            return self.get(task_id)
        if not remove and kind in ("parent", "child", "blocks", "blocked-by"):
            origin, target = (other, task_id) if kind in ("child", "blocks") else (task_id, other)
            if kind in ("parent", "child"):
                existing = self.get(origin)
                if existing.parent and existing.parent != target:
                    raise TrackerError("Task already has a different parent", "conflict")
            pending, seen = [target], set()
            while pending:
                node = pending.pop()
                if node == origin:
                    raise TrackerError("Relation would introduce a cycle", "conflict")
                if node in seen:
                    continue
                seen.add(node)
                found = self.get(node)
                pending.extend(([found.parent] if found.parent else []) if kind in ("parent", "child") else found.blockers)
        if kind == "duplicate" and not remove and raw["state"] == "CLOSED":
            raise TrackerError("GitHub marks only an open issue as a duplicate; reopen it first", "conflict")
        mine, theirs = raw["id"], other_raw["id"]
        verb = "Remove" if remove else "Add"
        if kind in ("parent", "child"):
            parent, child = (theirs, mine) if kind == "parent" else (mine, theirs)
            api.mutate(self.client, verb + "SubIssue", issueId=parent, subIssueId=child)
        elif kind in ("blocks", "blocked-by"):
            blocked, blocking = (theirs, mine) if kind == "blocks" else (mine, theirs)
            api.mutate(self.client, verb + "BlockedBy", issueId=blocked, blockingIssueId=blocking)
        elif kind == "related":
            api.mutate(self.client, verb + "RelatesTo", issueId=mine, relatedIssueId=theirs)
        elif remove:
            api.mutate(self.client, "UnmarkDuplicate", duplicateId=mine, canonicalId=theirs)
        else:
            api.mutate(self.client, "CloseIssue", issueId=mine, stateReason="DUPLICATE", duplicateIssueId=theirs)
            self.set_option(bound, item, "Status", done)
        self.confirm(task_id, lambda _, column: self.related_to(task_id, kind, other) == (not remove)
                     and (not done or column == done), "Relation change was not confirmed")
        return self.get(task_id)

    def label(self, name, create=True):
        found = api.repository(self.client, self.repository(), name)
        if found["label"] or not create:
            return (found["label"] or {}).get("id")
        return api.mutate(self.client, "CreateLabel", repositoryId=found["id"], name=name,
                          color="ededed")["createLabel"]["label"]["id"]

    def set_labels(self, task_id, add, remove):
        """Attach and detach this repository's labels by name; other labels stay."""
        raw, _, _ = self.writable(task_id)
        detach = [self.label(name, False) for name in raw["labels"] if name in remove]
        if any(detach):
            api.mutate(self.client, "RemoveLabels", labelableId=raw["id"], labelIds=[i for i in detach if i])
        if add and add not in raw["labels"]:
            api.mutate(self.client, "AddLabels", labelableId=raw["id"], labelIds=[self.label(add)])
        return self.confirm(task_id, lambda task, _: (not add or add in task.labels)
                            and not set(remove) & set(task.labels), "Label change was not confirmed")

    def set_tier(self, task_id, tier):
        if tier not in (1, 2, 3):
            raise TrackerError("Task tier must be 1, 2, or 3")
        title = f"ab:tier:{tier}"
        current = self.get(task_id)
        return self.set_labels(task_id, title, [name for name in current.labels
                                                if name.startswith("ab:tier:") and name != title])

    def set_normalized(self, task_id, normalized=True):
        """The marker records completed processing, independently of task readiness."""
        return self.set_labels(task_id, NORMALIZED_LABEL if normalized else None,
                               [] if normalized else [NORMALIZED_LABEL])
