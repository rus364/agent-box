"""GitHub transport and the GraphQL that reads Projects v2 and their issues.

Projects v2 exist only in GraphQL, and an issue's relations, its project item
and that item's field values come back in one GraphQL read, so issues are read
there too. Every operation carries a name: it is what a test fixture dispatches
on, and what a provider error names when it is reported.
"""
from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .core import TrackerError

DEFAULT_API = "https://api.github.com"

REF = "number repository { nameWithOwner }"
ISSUE = f"""
id number title body state stateReason url updatedAt
repository {{ nameWithOwner }}
labels(first: 100) {{ totalCount nodes {{ name }} }}
assignees(first: 100) {{ totalCount nodes {{ login }} }}
parent {{ {REF} }}
subIssues(first: 100) {{ totalCount nodes {{ {REF} }} }}
blockedBy(first: 100) {{ totalCount nodes {{ {REF} }} }}
relatesTo(first: 100) {{ totalCount nodes {{ {REF} }} }}
duplicateOf {{ {REF} }}
projectItems(first: 50, includeArchived: false) {{
  totalCount
  nodes {{
    id updatedAt project {{ id }}
    fieldValues(first: 50) {{
      totalCount
      nodes {{ ... on ProjectV2ItemFieldSingleSelectValue {{ name optionId field {{ ... on ProjectV2FieldCommon {{ id }} }} }} }}
    }}
  }}
}}
"""
PROJECT = """
id number title url closed
owner { ... on User { login } ... on Organization { login } }
fields(first: 100) {
  totalCount
  nodes { ... on ProjectV2FieldCommon { id name dataType } ... on ProjectV2SingleSelectField { options { id name } } }
}
workflows(first: 50) { nodes { name enabled } }
"""
QUERIES = {
    "Viewer": "query Viewer { viewer { login } }",
    "Project": f"""query Project($owner: String!, $number: Int!) {{
  repositoryOwner(login: $owner) {{ ... on ProjectV2Owner {{ projectV2(number: $number) {{ {PROJECT} }} }} }}
}}""",
    "Projects": """query Projects($owner: String!, $after: String) {
  repositoryOwner(login: $owner) { ... on ProjectV2Owner { projectsV2(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { number title closed owner { ... on User { login } ... on Organization { login } } }
  } } }
}""",
    "Issue": f"""query Issue($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{ issue(number: $number) {{ {ISSUE} }} }}
}}""",
    "Items": f"""query Items($project: ID!, $query: String, $after: String) {{
  node(id: $project) {{ ... on ProjectV2 {{ items(first: 50, after: $after, query: $query) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ type content {{ __typename ... on Issue {{ {ISSUE} }} }} }}
  }} }} }}
}}""",
    "Comments": """query Comments($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) { issue(number: $number) { comments(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id url body createdAt updatedAt author { login } }
  } } }
}""",
}


class APIError(TrackerError):
    """A refused or lost request, categorized for the command that made it.

    A write whose reply was lost may have been applied, so it reports an unknown
    outcome rather than inviting a retry that would duplicate an issue or a comment.
    """

    def __init__(self, status, detail, *, write=False, kind=None, rate_limited=False):
        self.status = status
        self.kind = kind
        transient = rate_limited or status in (0, 408, 500, 502, 503, 504)
        unknown = write and not rate_limited and (status in (0, 408) or status >= 500)
        suffix = ""
        if unknown:
            suffix = ". Write outcome unknown; the server may have applied it. Do not repeat the write; reread task state/comments first"
        elif rate_limited:
            suffix = ". GitHub rate limit reached; retry after it resets"
        elif transient and not write:
            suffix = ". Retry only the failed read; the command may have completed earlier writes"
        prefix = f"GitHub API HTTP {status}" if status else "GitHub transport"
        # GitHub answers an exhausted rate limit with 403, which is not a denial.
        category = ("unknown-outcome" if unknown else "transport" if rate_limited
                    else "not-found" if status == 404 or kind == "NOT_FOUND"
                    else "authentication" if status in (401, 403) or kind in ("FORBIDDEN", "INSUFFICIENT_SCOPES")
                    else "transport" if transient else "provider")
        super().__init__(f"{prefix}: {detail}{suffix}", category, transient and not unknown)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The token belongs to the configured endpoint, never a redirect destination.
        return None


def endpoints(server):
    """The REST base, the GraphQL endpoint and the web host of one GitHub instance.

    github.com serves its API from a separate host; GitHub Enterprise Server serves
    it under /api/v3 of its own host, with GraphQL beside it at /api/graphql.
    """
    base = (server or DEFAULT_API).rstrip("/")
    parts = urlsplit(base)
    if parts.hostname == "api.github.com":
        return base, base + "/graphql", "github.com"
    if not parts.path.endswith("/api/v3"):
        raise TrackerError("A GitHub Enterprise Server URL ends with /api/v3", "configuration")
    return base, base[:-len("/v3")] + "/graphql", parts.hostname


class GitHubClient:
    def __init__(self, url, token, timeout=30, opener=None):
        self.url, self.graphql_url, self.web_host = endpoints(url)
        self.token = token
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open

    def request(self, method, url, body=None, *, write=False):
        headers = {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + self.token,
                   "User-Agent": "agent-box-tracker/1", "X-GitHub-Api-Version": "2022-11-28"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", "replace")
            except (OSError, http.client.HTTPException):
                raw = ""
            finally:
                exc.close()
            try:
                problem = json.loads(raw)
            except json.JSONDecodeError:
                problem = {}
            detail = (problem.get("message") if isinstance(problem, dict) else None) or exc.reason
            limited = exc.code == 429 or exc.code == 403 and (
                exc.headers.get("x-ratelimit-remaining") == "0" or exc.headers.get("retry-after") is not None
                or "rate limit" in str(detail).lower())
            raise APIError(exc.code, self.redact(detail), write=write, rate_limited=limited) from None
        except (OSError, http.client.HTTPException) as exc:
            raise APIError(0, f"request failed: {self.redact(exc)}", write=write) from None
        if not payload and status in (204, 205):
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise APIError(status, "invalid JSON response", write=write) from None

    def redact(self, value):
        return str(value).replace(self.token, "[redacted]") if self.token else str(value)

    def graphql(self, query, variables=None):
        """Run one named operation; GraphQL reports refusals inside a 200 reply.

        A mutation refused that way was not applied, so only a lost reply makes
        its outcome unknown.
        """
        operation = re.match(r"\s*(query|mutation)\s+(\w+)", query)
        write = bool(operation and operation[1] == "mutation")
        reply = self.request("POST", self.graphql_url, {"query": query, "variables": variables or {}}, write=write)
        errors = reply.get("errors") if isinstance(reply, dict) else None
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            kind = first.get("type")
            name = operation[2] if operation else "GraphQL"
            raise APIError(200, f"{name}: {self.redact(first.get('message') or 'request refused')}",
                           kind=kind, rate_limited=kind == "RATE_LIMITED")
        if not isinstance(reply, dict) or not isinstance(reply.get("data"), dict):
            raise APIError(200, "GraphQL reply carries no data", write=write)
        return reply["data"]


def complete(connection, what):
    """Nodes of a first-page connection, refusing a list GitHub cut short.

    A relation list read in part would make a blocker or a child silently vanish.
    """
    nodes = (connection or {}).get("nodes") or []
    if (connection or {}).get("totalCount", len(nodes)) > len(nodes):
        raise TrackerError(f"GitHub returned only part of {what}; it is too long to read completely", "provider")
    return nodes


def ref(node):
    return (node["repository"]["nameWithOwner"], int(node["number"])) if node else None


def issue_record(raw):
    """A GraphQL issue as plain data; everything the adapter reads comes from here."""
    items = []
    for item in complete(raw.get("projectItems"), "an issue's project items"):
        values = {}
        for value in complete(item.get("fieldValues"), "a project item's field values"):
            field = (value or {}).get("field") or {}
            if field.get("id") and value.get("optionId"):
                values[field["id"]] = {"name": value.get("name") or "", "option": value["optionId"]}
        items.append({"id": item["id"], "project": item["project"]["id"],
                      "updated_at": item.get("updatedAt") or "", "values": values})
    return {"id": raw["id"], "repository": raw["repository"]["nameWithOwner"], "number": int(raw["number"]),
            "title": raw.get("title") or "", "body": raw.get("body") or "", "state": raw["state"],
            "state_reason": raw.get("stateReason"), "url": raw.get("url") or "",
            "updated_at": raw.get("updatedAt") or "",
            "labels": [label["name"] for label in complete(raw.get("labels"), "an issue's labels")],
            "assignees": [user["login"] for user in complete(raw.get("assignees"), "an issue's assignees")],
            "parent": ref(raw.get("parent")),
            "children": [ref(node) for node in complete(raw.get("subIssues"), "an issue's sub-issues")],
            "blocked_by": [ref(node) for node in complete(raw.get("blockedBy"), "an issue's blockers")],
            "related": [ref(node) for node in complete(raw.get("relatesTo"), "an issue's related issues")],
            "duplicate_of": ref(raw.get("duplicateOf")), "items": items}


def project_record(raw):
    fields = []
    for node in complete(raw.get("fields"), "a project's fields"):
        if node.get("id"):
            fields.append({"id": node["id"], "name": node.get("name") or "", "type": node.get("dataType"),
                           "options": [{"id": option["id"], "name": option["name"]}
                                       for option in node.get("options") or []]})
    owner = (raw.get("owner") or {}).get("login") or ""
    return {"node": raw["id"], "id": f"{owner}/{raw['number']}", "owner": owner, "number": int(raw["number"]),
            "title": raw.get("title") or "", "url": raw.get("url") or "", "closed": bool(raw.get("closed")),
            "parent": "", "fields": fields,
            "workflows": [{"name": node["name"], "enabled": bool(node.get("enabled"))}
                          for node in (raw.get("workflows") or {}).get("nodes") or []]}


def pages(client, operation, variables, path, what):
    """Every node of a paginated connection found at ``path`` in each reply."""
    after, nodes = None, []
    for _ in range(10000):
        data = client.graphql(QUERIES[operation], {**variables, "after": after})
        connection = data
        for key in path:
            connection = (connection or {}).get(key)
        if connection is None:
            raise APIError(404, f"{what} not found", kind="NOT_FOUND")
        nodes.extend(connection.get("nodes") or [])
        info = connection.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return nodes
        after = info.get("endCursor")
    raise APIError(200, f"pagination of {what} did not converge")


def viewer(client):
    return client.graphql(QUERIES["Viewer"])["viewer"]


def project(client, owner, number):
    data = client.graphql(QUERIES["Project"], {"owner": owner, "number": int(number)})
    raw = ((data.get("repositoryOwner") or {}).get("projectV2"))
    if not raw:
        raise APIError(404, f"project {owner}/{number} not found", kind="NOT_FOUND")
    return project_record(raw)


def projects(client, owner):
    nodes = pages(client, "Projects", {"owner": owner}, ("repositoryOwner", "projectsV2"), f"projects of {owner}")
    return [{"id": f"{(node.get('owner') or {}).get('login') or owner}/{node['number']}",
             "title": node.get("title") or "", "closed": bool(node.get("closed")), "parent": ""} for node in nodes]


def issue(client, repository, number):
    owner, name = repository.split("/", 1)
    data = client.graphql(QUERIES["Issue"], {"owner": owner, "name": name, "number": int(number)})
    raw = (data.get("repository") or {}).get("issue")
    if not raw:
        raise APIError(404, f"issue {repository}#{number} not found", kind="NOT_FOUND")
    return issue_record(raw)


def project_issues(client, project_node, query=""):
    """Issues on a project's board; draft items and pull requests are not issues."""
    nodes = pages(client, "Items", {"project": project_node, "query": query or None}, ("node", "items"), "project items")
    return [issue_record(node["content"]) for node in nodes
            if node.get("type") == "ISSUE" and (node.get("content") or {}).get("__typename") == "Issue"]


def comments(client, repository, number):
    owner, name = repository.split("/", 1)
    nodes = pages(client, "Comments", {"owner": owner, "name": name, "number": int(number)},
                  ("repository", "issue", "comments"), f"comments of {repository}#{number}")
    return [{"id": node["id"], "author": (node.get("author") or {}).get("login"), "created": node.get("createdAt"),
             "updated": node.get("updatedAt"), "url": node.get("url"), "comment": node.get("body") or ""}
            for node in nodes]
