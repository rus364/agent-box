"""GitHub Projects translates a project board and its issues; the core owns workflow decisions."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .core import STATES, Task, TrackerError
from . import github_api as api

#: Priority options, highest first, as the board lists them: the board sorts and
#: groups by option order, so this order is part of what the names mean.
PRIORITIES = ("Immediate", "Urgent", "High", "Medium", "Low")
PRIORITY_VALUES = {name.casefold(): 5 - index for index, name in enumerate(PRIORITIES)}
CLOSE_REASONS = {"NOT_PLANNED": "closed as not planned", "DUPLICATE": "closed as a duplicate"}


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
