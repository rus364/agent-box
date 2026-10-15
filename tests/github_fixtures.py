"""A fake GitHub that answers the adapter's named GraphQL operations in GitHub's own shapes.

Dispatching on the operation name keeps the fixture independent of query text,
while replies go through the same parsing as live ones. An operation the fake
does not know is an AssertionError, so a new call is visible.
"""
import copy
import re

from agent_box.repo_binding import Binding
from agent_box.tracker.github import GitHubAdapter

REPO = "octo/app"
STATUS = ("Backlog", "Ready", "In progress", "In review", "Done")


def field(ident, name, options, data_type="SINGLE_SELECT"):
    return {"id": ident, "name": name, "dataType": data_type,
            "options": [{"id": f"{ident}-{index}", "name": option} for index, option in enumerate(options)]}


def project(number=2, owner="octo", status=STATUS, priority=("Immediate", "Urgent", "High", "Medium", "Low"),
            workflows=()):
    fields = [{"id": "F-title", "name": "Title", "dataType": "TITLE"}]
    if status is not None:
        fields.append(field("F-status", "Status", status))
    if priority is not None:
        fields.append(field("F-priority", "Priority", priority))
    return {"id": f"P-{owner}-{number}", "number": number, "title": f"Board {number}", "closed": False,
            "url": f"https://github.com/users/{owner}/projects/{number}", "owner": {"login": owner},
            "fields": {"totalCount": len(fields), "nodes": fields},
            "workflows": {"nodes": [{"name": name, "enabled": enabled} for name, enabled in workflows]}}


def issue(number, repo=REPO, **values):
    """An issue; ``status``/``priority`` name options on project 2, ``on`` lists project numbers."""
    return {"repo": repo, "number": number, "title": values.get("title", f"Issue {number}"),
            "body": values.get("body", ""), "state": values.get("state", "OPEN"),
            "stateReason": values.get("reason"), "updatedAt": values.get("updated", "2026-01-01T00:00:00Z"),
            "labels": values.get("labels", []), "assignees": values.get("assignees", []),
            "parent": values.get("parent"), "children": values.get("children", []),
            "blockedBy": values.get("blocked_by", []), "relatesTo": values.get("related", []),
            "duplicateOf": values.get("duplicate_of"), "on": values.get("on", [2] if "status" in values else []),
            "status": values.get("status"), "priority": values.get("priority"),
            "itemUpdatedAt": values.get("item_updated", "2026-01-01T00:00:00Z")}


class FakeGitHub:
    web_host = "github.com"
    url = "https://api.github.com"

    def __init__(self, issues=(), projects=None, drafts=0, pulls=0, page=50, list_page=None, login="octocat"):
        self.projects = projects or {2: project()}
        self.list_page = list_page
        self.issues = {(raw["repo"], raw["number"]): raw for raw in copy.deepcopy(list(issues))}
        self.drafts, self.pulls, self.page, self.login = drafts, pulls, page, login
        self.operations = []

    def ref(self, value):
        repo, number = (REPO, value) if isinstance(value, int) else value
        return {"number": number, "repository": {"nameWithOwner": repo}}

    def option(self, number, name, value):
        fields = self.projects[number]["fields"]["nodes"]
        found = next((f for f in fields if f["name"] == name), None)
        option = next((o for o in (found or {}).get("options", []) if o["name"] == value), None)
        return {"name": value, "optionId": option["id"], "field": {"id": found["id"]}} if option else {}

    def lists(self, raw):
        """Every list of an issue in full; ``connection`` serves them a page at a time."""
        items = []
        for number in raw["on"]:
            values = [self.option(number, "Status", raw["status"]) if raw["status"] else {},
                      self.option(number, "Priority", raw["priority"]) if raw["priority"] else {}]
            items.append({"id": f"I-{number}-{raw['number']}", "updatedAt": raw["itemUpdatedAt"],
                          "project": {"id": self.projects[number]["id"]},
                          "fieldValues": {"totalCount": len(values), "nodes": values}})
        return {"labels": [{"name": n} for n in raw["labels"]], "assignees": [{"login": n} for n in raw["assignees"]],
                "subIssues": [self.ref(v) for v in raw["children"]], "blockedBy": [self.ref(v) for v in raw["blockedBy"]],
                "relatesTo": [self.ref(v) for v in raw["relatesTo"]], "projectItems": items}

    def connection(self, nodes, after=None):
        start = int(after or 0)
        size = self.list_page or len(nodes) or 1
        return {"totalCount": len(nodes), "pageInfo": {"hasNextPage": start + size < len(nodes),
                                                       "endCursor": str(start + size)},
                "nodes": nodes[start:start + size]}

    def render(self, raw):
        return {"id": f"N-{raw['repo']}-{raw['number']}", "number": raw["number"], "title": raw["title"],
                "body": raw["body"], "state": raw["state"], "stateReason": raw["stateReason"],
                "url": f"https://github.com/{raw['repo']}/issues/{raw['number']}", "updatedAt": raw["updatedAt"],
                "repository": {"nameWithOwner": raw["repo"]},
                "parent": self.ref(raw["parent"]) if raw["parent"] else None,
                "duplicateOf": self.ref(raw["duplicateOf"]) if raw["duplicateOf"] else None,
                **{name: self.connection(nodes) for name, nodes in self.lists(raw).items()}}

    def graphql(self, query, variables=None):
        operation = re.match(r"\s*(?:query|mutation)\s+(\w+)", query)[1]
        variables = variables or {}
        self.operations.append((operation, variables))
        handler = getattr(self, "op_" + operation, None)
        if handler is None and operation.startswith("Issue") and operation != "Issue":
            name = operation[5].lower() + operation[6:]
            raw = next(raw for raw in self.issues.values() if f"N-{raw['repo']}-{raw['number']}" == variables["id"])
            return {"node": {name: self.connection(self.lists(raw)[name], variables["after"])}}
        if handler is None:
            raise AssertionError(operation)
        return handler(**variables)

    def op_Viewer(self):
        return {"viewer": {"login": self.login}}

    def op_Project(self, owner, number):
        found = self.projects.get(number)
        return {"repositoryOwner": {"projectV2": copy.deepcopy(found) if found and
                                    found["owner"]["login"].casefold() == owner.casefold() else None}}

    def op_Projects(self, owner, after):
        nodes = [{"number": p["number"], "title": p["title"], "closed": p["closed"], "owner": p["owner"]}
                 for p in self.projects.values() if p["owner"]["login"].casefold() == owner.casefold()]
        return {"repositoryOwner": {"projectsV2": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}

    def op_Issue(self, owner, name, number):
        raw = self.issues.get((f"{owner}/{name}", number))
        if raw is None:
            from agent_box.tracker.github_api import APIError
            raise APIError(200, f"Issue: Could not resolve to an Issue with the number of {number}.", kind="NOT_FOUND")
        return {"repository": {"issue": self.render(raw)}}

    def op_Items(self, project, query, after):
        number = next(n for n, p in self.projects.items() if p["id"] == project)
        nodes = [{"type": "DRAFT_ISSUE", "content": {"__typename": "DraftIssue"}} for _ in range(self.drafts)]
        nodes += [{"type": "PULL_REQUEST", "content": {"__typename": "PullRequest"}} for _ in range(self.pulls)]
        nodes += [{"type": "ISSUE", "content": {"__typename": "Issue", **self.render(raw)}}
                  for raw in self.issues.values() if number in raw["on"]
                  and not (query == "is:open" and raw["state"] != "OPEN")]
        start = int(after or 0)
        chunk = nodes[start:start + self.page]
        more = start + self.page < len(nodes)
        return {"node": {"items": {"pageInfo": {"hasNextPage": more, "endCursor": str(start + self.page)},
                                   "nodes": chunk}}}

    def op_Comments(self, owner, name, number, after):
        raw = self.issues[(f"{owner}/{name}", number)]
        nodes = [{"id": f"C{index}", "url": "", "body": body, "createdAt": "", "updatedAt": "", "author": {"login": "octocat"}}
                 for index, body in enumerate(raw.get("comments", []))]
        return {"repository": {"issue": {"comments": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}


def adapter(issues=(), *, bound="octo/2", **options):
    client = FakeGitHub(issues, **options)
    binding = Binding("github", "https://api.github.com", bound) if bound else None
    return GitHubAdapter(client.url, "token", binding, client=client, repository=REPO), client
