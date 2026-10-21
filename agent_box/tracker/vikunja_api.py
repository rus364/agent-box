#!/usr/bin/env python3
"""Vikunja API v2 transport, board operations, and structured import support.

Extracted from agent-box's original native integration; no installed skill owns code.
"""

from __future__ import annotations

import csv
import hashlib
import http.client
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


from ..paths import WORKFLOW_STATE
from .core import TrackerError

API_PREFIX = "/api/v2"
DEFAULT_STATE_DIR = WORKFLOW_STATE / "imports/vikunja"
PLAN_FIELDS = (
    "Title", "Description", "Priority", "Labels", "Project", "Done",
    "Plan ID", "Kind", "Depends On", "Source Tasks", "Order",
)
PLAN_OPTIONAL_FIELDS = (
    "Due Date", "Start Date", "End Date", "Percent Done", "Hex Color",
    "Assignees", "Bucket", "Parent",
)
PLAN_KNOWN_FIELDS = PLAN_FIELDS + PLAN_OPTIONAL_FIELDS
PLAN_REQUIRED_FIELDS = ("Title", "Description", "Project", "Plan ID")
PLAN_REQUIRED_VALUES = ("Title", "Description", "Project", "Plan ID")
UNMANAGED = ""
CLEARED = "-"
HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
NULL_TIMESTAMP = "0001-01-01T00:00:00Z"
# A plan ID names a row only inside its file. Anything else a row references is
# an existing task's tracker ID, so an all-digit plan ID would be ambiguous.
PLAN_ID_RE = re.compile(r"^(?!\d+$)[A-Za-z0-9][A-Za-z0-9._:-]*$")
TRACKER_ID_RE = re.compile(r"^\d+$")
RELATION_KINDS = {
    "subtask", "parenttask", "related", "duplicateof", "duplicates",
    "blocking", "blocked", "precedes", "follows", "copiedfrom", "copiedto",
}


class VikunjaError(TrackerError):
    """Base error with a concise user-facing message."""


class ConfigError(VikunjaError):
    pass


class APIError(VikunjaError):
    def __init__(self, status, detail, code=None, *, method="GET"):
        self.status = status
        self.code = code
        writing = method.upper() not in ("GET", "HEAD", "OPTIONS")
        transient = status in (0, 408, 429, 500, 502, 503, 504)
        unknown = writing and (status in (0, 408) or status >= 500 or 200 <= status < 300)
        suffix = f" (code {code})" if code is not None else ""
        prefix = f"Vikunja API HTTP {status}" if status else "Vikunja transport"
        if unknown:
            suffix += ". Write outcome unknown; the server may have applied it. Do not repeat the write; reread task state/comments first"
        elif transient and not writing:
            suffix += ". Retry only the failed read; the command may have completed earlier writes"
        super().__init__(f"{prefix}: {detail}{suffix}",
                         "unknown-outcome" if unknown else "conflict" if status in (409, 412) else
                         "authentication" if status in (401, 403) else "transport" if transient else "provider",
                         transient and not writing)


class AuthenticationError(APIError):
    pass


class NotFound(VikunjaError):
    pass


class AmbiguousMatch(VikunjaError):
    pass


class PlanValidationError(VikunjaError):
    pass


class ImportConflict(VikunjaError):
    pass


def stable_json(value, pretty=False):
    if pretty:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials belong to the configured endpoint, never a redirect destination.
        return None


class VikunjaClient:
    def __init__(self, url, token, per_page=1000, timeout=30, opener=None):
        self.url = url.rstrip("/")
        self.token = token
        self.per_page = per_page
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open
        self.revisions = {}

    def request(self, method, path, body=None, headers=None):
        if not path.startswith("/"):
            path = "/" + path
        url = self.url + API_PREFIX + path
        request_headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self.token,
            "User-Agent": "agent-box-tracker/1",
        }
        request_headers.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                self.revisions[path.split("?")[0]] = getattr(response, "headers", {}).get("ETag", "")
                payload = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", "replace")
            except (OSError, http.client.HTTPException):
                # The received status still establishes an explicit denial even if its body is lost.
                raw = ""
            finally:
                exc.close()
            try:
                problem = json.loads(raw)
            except json.JSONDecodeError:
                problem = {}
            if not isinstance(problem, dict):
                problem = {}
            detail = problem.get("detail") or problem.get("message") or problem.get("title") or exc.reason
            error_type = AuthenticationError if exc.code in (401, 403) else APIError
            raise error_type(exc.code, str(detail).replace(self.token, "[redacted]"), problem.get("code"),
                             method=method) from None
        except (OSError, http.client.HTTPException) as exc:
            detail = str(exc).replace(self.token, "[redacted]")
            raise APIError(0, f"request failed: {detail}", method=method) from None
        if not payload and (status in (204, 205) or method.upper() == "HEAD"):
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise APIError(status, "invalid JSON response", method=method) from None

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body):
        return self.request("POST", path, body)

    def put(self, path, body):
        return self.request("PUT", path, body)

    def patch(self, path, body, markdown=False):
        headers = {"Content-Type": "application/merge-patch+json"}
        if self.revisions.get(path.split("?")[0]):
            headers["If-Match"] = self.revisions[path.split("?")[0]]
        if markdown:
            headers["X-Vikunja-Format"] = "markdown"
        return self.request("PATCH", path, body, headers)

    def delete(self, path):
        return self.request("DELETE", path)

    def paginate(self, path, params=None):
        params = dict(params or {})
        params["per_page"] = self.per_page
        page = 1
        items = []
        expected_total = None
        while True:
            params["page"] = page
            separator = "&" if "?" in path else "?"
            encoded = urllib.parse.urlencode(params, doseq=True)
            response = self.get(path + separator + encoded)
            if not isinstance(response, dict) or not isinstance(response.get("items"), list):
                raise APIError(200, "paginated response did not contain an items array")
            items.extend(response["items"])
            expected_total = response.get("total", expected_total)
            total_pages = response.get("total_pages")
            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(response["items"]) < self.per_page:
                break
            page += 1
            if page > 10000 or not response["items"]:
                raise APIError(200, "pagination did not converge")
        if expected_total is not None and len(items) < expected_total:
            raise APIError(200, f"pagination truncated: received {len(items)} of {expected_total} items")
        return items


def _normalize_path(value):
    parts = tuple(part.strip() for part in value.split("/") if part.strip())
    if not parts:
        raise ValueError("empty project path")
    return parts


class ProjectIndex:
    def __init__(self, projects):
        self.projects = list(projects)
        self.by_id = {int(p["id"]): p for p in self.projects}

    def path_for(self, project):
        parts = []
        seen = set()
        current = project
        while current:
            current_id = int(current["id"])
            if current_id in seen:
                raise ImportConflict(f"project hierarchy cycle at id {current_id}")
            seen.add(current_id)
            parts.append(current["title"])
            parent_id = int(current.get("parent_project_id") or 0)
            current = self.by_id.get(parent_id)
        return " / ".join(reversed(parts))

    def resolve(self, reference):
        text = str(reference).strip()
        if re.fullmatch(r"-?\d+", text):
            found = self.by_id.get(int(text))
            if not found:
                raise NotFound(f"project id {text} was not found")
            return found
        if "/" in text:
            matches = [p for p in self.projects if self.path_for(p) == " / ".join(_normalize_path(text))]
        else:
            matches = [p for p in self.projects if p.get("title") == text]
        if not matches:
            raise NotFound(f"project {text!r} was not found")
        if len(matches) > 1:
            paths = ", ".join(f"{self.path_for(p)} (id {p['id']})" for p in matches)
            raise AmbiguousMatch(f"project {text!r} is ambiguous: {paths}")
        return matches[0]

    def by_exact_path(self, path):
        canonical = " / ".join(_normalize_path(path))
        matches = [p for p in self.projects if self.path_for(p) == canonical]
        if len(matches) > 1:
            raise AmbiguousMatch(f"project path {canonical!r} matched multiple projects")
        return matches[0] if matches else None


class TaskIndex:
    def __init__(self, tasks):
        self.tasks = list(tasks)
        self.by_id = {int(task["id"]): task for task in self.tasks}

    def resolve(self, reference):
        text = str(reference).strip()
        if text.isdigit():
            found = self.by_id.get(int(text))
            if not found:
                raise NotFound(f"task id {text} was not found")
            return found
        matches = [task for task in self.tasks if task.get("title") == text]
        if not matches:
            raise NotFound(f"task {text!r} was not found")
        if len(matches) > 1:
            ids = ", ".join(str(task["id"]) for task in matches)
            raise AmbiguousMatch(f"task title {text!r} is ambiguous; matching ids: {ids}")
        return matches[0]


def markdown_description(description):
    """Keep the plan's single line breaks visible once Vikunja renders Markdown."""
    lines = (description or "").rstrip().splitlines()
    prepared = []
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        next_nonblank = index + 1 < len(lines) and bool(lines[index + 1].strip())
        if stripped and next_nonblank and not in_fence and not line.endswith("  "):
            line += "  "
        prepared.append(line)
    return "\n".join(prepared).rstrip()


def _cell(row, field):
    return (row.get(field) or "").strip()


def _plan_timestamp(plan_id, field, value):
    """Optional date cell: blank leaves the field unmanaged, "-" clears it."""
    if value == UNMANAGED:
        return None
    if value == CLEARED:
        return NULL_TIMESTAMP
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})?)?", value):
        raise PlanValidationError(
            f"{plan_id}: {field} must be ISO-8601 (2026-09-02 or 2026-09-02T15:04:05Z), "
            f'blank to leave unmanaged, or "-" to clear'
        )
    if len(value) == 10:
        value += "T00:00:00Z"
    return value.replace(" ", "T")


class PlanTask:
    def __init__(self, title, description, priority, labels, project_path, done,
                 plan_id, kind, depends_on, source_tasks, order,
                 fields=None, assignees=None, bucket=UNMANAGED, parent=None):
        self.title = title
        self.description = description
        self.priority = priority
        self.labels = tuple(labels)
        self.project_path = project_path
        self.done = done
        self.plan_id = plan_id
        self.kind = kind
        self.depends_on = tuple(depends_on)
        self.source_tasks = source_tasks
        self.order = order
        self.fields = dict(fields or {})
        self.assignees = None if assignees is None else tuple(assignees)
        self.bucket = bucket
        self.parent = parent

    @classmethod
    def from_row(cls, row, default_order=1):
        plan_id = _cell(row, "Plan ID")
        if not PLAN_ID_RE.fullmatch(plan_id):
            raise PlanValidationError(f"invalid plan ID {plan_id!r}; use letters, digits and ._:- with at least one non-digit")
        priority_text = _cell(row, "Priority") or "0"
        try:
            priority = int(priority_text)
        except ValueError:
            raise PlanValidationError(f"{plan_id}: priority must be an integer") from None
        if priority not in range(0, 6):
            raise PlanValidationError(f"{plan_id}: priority must be between 0 and 5")
        done_text = (_cell(row, "Done") or "false").lower()
        if done_text not in ("true", "false"):
            raise PlanValidationError(f"{plan_id}: Done must be true or false")
        order_text = _cell(row, "Order")
        if order_text:
            try:
                order = int(order_text)
            except ValueError:
                raise PlanValidationError(f"{plan_id}: Order must be an integer") from None
        else:
            order = default_order
        try:
            project_path = " / ".join(_normalize_path(row["Project"]))
        except ValueError:
            raise PlanValidationError(f"{plan_id}: Project must not be empty") from None
        labels = tuple(dict.fromkeys(x.strip() for x in _cell(row, "Labels").split(",") if x.strip()))
        depends = tuple(dict.fromkeys(x.strip() for x in _cell(row, "Depends On").split(",") if x.strip()))

        fields = {}
        for column, key in (("Due Date", "due_date"), ("Start Date", "start_date"),
                            ("End Date", "end_date")):
            stamp = _plan_timestamp(plan_id, column, _cell(row, column))
            if stamp is not None:
                fields[key] = stamp
        percent = _cell(row, "Percent Done")
        if percent:
            try:
                value = float(percent)
            except ValueError:
                raise PlanValidationError(f"{plan_id}: Percent Done must be a number") from None
            if not 0.0 <= value <= 1.0:
                raise PlanValidationError(
                    f"{plan_id}: Percent Done must be between 0 and 1 (0.5 means 50%)"
                )
            fields["percent_done"] = value
        color = _cell(row, "Hex Color")
        if color:
            if color == CLEARED:
                fields["hex_color"] = ""
            elif HEX_COLOR_RE.fullmatch(color):
                fields["hex_color"] = color.lstrip("#").lower()
            else:
                raise PlanValidationError(f"{plan_id}: Hex Color must be six hex digits")

        assignee_cell = _cell(row, "Assignees")
        if assignee_cell == CLEARED:
            assignees = ()
        elif assignee_cell:
            assignees = tuple(dict.fromkeys(x.strip() for x in assignee_cell.split(",") if x.strip()))
        else:
            assignees = None
        parent = _cell(row, "Parent") or None
        if parent == plan_id:
            raise PlanValidationError(f"{plan_id} is its own parent")

        return cls(
            _cell(row, "Title"), row["Description"].rstrip(), priority, labels,
            project_path, done_text == "true", plan_id, _cell(row, "Kind"),
            depends, _cell(row, "Source Tasks"), order,
            fields=fields, assignees=assignees, bucket=_cell(row, "Bucket"), parent=parent,
        )

    def manages_assignees(self):
        return self.assignees is not None

    def rendered_description(self):
        return markdown_description(self.description)

    def as_dict(self):
        value = {
            "depends_on": list(self.depends_on), "description": self.description,
            "done": self.done, "plan_id": self.plan_id, "kind": self.kind,
            "labels": list(self.labels), "order": self.order, "priority": self.priority,
            "project": self.project_path, "source_tasks": self.source_tasks,
            "title": self.title,
        }
        if self.fields:
            value["fields"] = dict(self.fields)
        if self.assignees is not None:
            value["assignees"] = list(self.assignees)
        if self.bucket:
            value["bucket"] = self.bucket
        if self.parent:
            value["parent"] = self.parent
        return value


class Plan:
    def __init__(self, tasks, source, digest):
        self.tasks = tuple(tasks)
        self.source = Path(source)
        self.digest = digest

    def relation_edges(self):
        """Edges as (base task, relation kind, other task).

        `A Depends On B` is posted on B as `blocking` A. `A Parent P` is posted
        on A as `parenttask` P, and Vikunja records the inverse `subtask` on P.
        """
        edges = []
        for task in self.tasks:
            for dependency in task.depends_on:
                edges.append((dependency, "blocking", task.plan_id))
            if task.parent:
                edges.append((task.plan_id, "parenttask", task.parent))
        return tuple(edges)

    def tracker_references(self):
        """References that no row defines; each must be an existing task's tracker ID."""
        known = {task.plan_id for task in self.tasks}
        referenced = set()
        for task in self.tasks:
            referenced.update(task.depends_on)
            if task.parent:
                referenced.add(task.parent)
        return tuple(sorted(referenced - known))

    def as_dict(self):
        return {
            "digest": self.digest,
            "tracker_references": list(self.tracker_references()),
            "projects": sorted({task.project_path for task in self.tasks}),
            "relation_edges": [list(edge) for edge in self.relation_edges()],
            "source": str(self.source),
            "tasks": [task.as_dict() for task in sorted(self.tasks, key=lambda item: item.order)],
        }


def _find_cycle(tasks_by_id):
    visiting = set()
    visited = set()
    stack = []

    def visit(node):
        if node in visiting:
            start = stack.index(node)
            return stack[start:] + [node]
        if node in visited:
            return None
        visiting.add(node)
        stack.append(node)
        task = tasks_by_id[node]
        successors = list(task.depends_on)
        if task.parent:
            successors.append(task.parent)
        for dependency in successors:
            if dependency not in tasks_by_id:
                continue
            cycle = visit(dependency)
            if cycle:
                return cycle
        stack.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in tasks_by_id:
        cycle = visit(node)
        if cycle:
            return cycle
    return None


JSON_PLAN_KEYS = {
    "id": "Plan ID", "title": "Title", "description": "Description",
    "priority": "Priority", "labels": "Labels", "project": "Project",
    "done": "Done", "kind": "Kind", "depends_on": "Depends On",
    "source_tasks": "Source Tasks", "order": "Order", "due_date": "Due Date",
    "start_date": "Start Date", "end_date": "End Date",
    "percent_done": "Percent Done", "hex_color": "Hex Color",
    "assignees": "Assignees", "bucket": "Bucket", "parent": "Parent",
}


def _json_cell(key, value):
    """Render one JSON plan value into the shared row grammar."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if key in ("assignees",) and not value:
            return CLEARED
        for item in value:
            if not isinstance(item, str):
                raise PlanValidationError(f"{key} must be a list of strings")
        return ",".join(item.strip() for item in value)
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return value
    raise PlanValidationError(f"{key} has unsupported type {type(value).__name__}")


def _rows_from_json(document):
    if isinstance(document, dict):
        entries = document.get("tasks")
        unknown_top = sorted(set(document) - {"tasks"})
        if unknown_top:
            raise PlanValidationError(f"unknown top-level keys: {', '.join(unknown_top)}")
    else:
        entries = document
    if not isinstance(entries, list):
        raise PlanValidationError('JSON plan must be a list of tasks or {"tasks": [...]}')
    rows = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise PlanValidationError(f"JSON task {position} must be an object")
        unknown = sorted(set(entry) - set(JSON_PLAN_KEYS))
        if unknown:
            raise PlanValidationError(
                f"JSON task {position} has unknown keys: {', '.join(unknown)}; "
                f"known keys are {', '.join(sorted(JSON_PLAN_KEYS))}"
            )
        row = {column: "" for column in PLAN_KNOWN_FIELDS}
        for key, value in entry.items():
            row[JSON_PLAN_KEYS[key]] = _json_cell(key, value)
        rows.append(row)
    return rows


def parse_plan(path):
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if path.suffix.lower() == ".json":
        try:
            document = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanValidationError(f"{path}: invalid JSON plan: {exc}") from None
        return _finish_plan(_rows_from_json(document), path, digest)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PlanValidationError(f"{path}: CSV must be UTF-8: {exc}") from None
    # Passing splitlines() discards the physical newlines inside quoted CSV
    # fields, causing csv.reader to concatenate every line of a description.
    reader = csv.DictReader(io.StringIO(text, newline=""))
    columns = list(reader.fieldnames or [])
    unknown = [name for name in columns if name not in PLAN_KNOWN_FIELDS]
    if unknown:
        raise PlanValidationError(
            f"unknown CSV columns: {', '.join(unknown)}; "
            f"known columns are {', '.join(PLAN_KNOWN_FIELDS)}"
        )
    duplicates = sorted({name for name in columns if columns.count(name) > 1})
    if duplicates:
        raise PlanValidationError(f"duplicate CSV columns: {', '.join(duplicates)}")
    missing_columns = [name for name in PLAN_REQUIRED_FIELDS if name not in columns]
    if missing_columns:
        raise PlanValidationError(f"CSV is missing required columns: {', '.join(missing_columns)}")
    rows = []
    for number, csv_row in enumerate(reader, start=2):
        if None in csv_row:
            raise PlanValidationError(f"CSV row {number} has extra columns")
        rows.append(csv_row)
    return _finish_plan(rows, path, digest)


def _finish_plan(rows, path, digest):
    tasks = []
    for number, row in enumerate(rows, start=1):
        blank = [field for field in PLAN_REQUIRED_VALUES
                 if not (row.get(field) or "").strip()]
        if blank:
            raise PlanValidationError(f"plan entry {number} has blank fields: {', '.join(blank)}")
        tasks.append(PlanTask.from_row(row, default_order=number))
    if not tasks:
        raise PlanValidationError("plan contains no tasks")
    by_id = {}
    for task in tasks:
        if task.plan_id in by_id:
            raise PlanValidationError(f"duplicate plan ID {task.plan_id}")
        by_id[task.plan_id] = task
    references = Plan(tasks, path, digest).tracker_references()
    invalid = [ref for ref in references if not TRACKER_ID_RE.fullmatch(ref)]
    if invalid:
        raise PlanValidationError(
            "references must name a plan row or an existing task's tracker ID: " + ", ".join(invalid)
        )
    seen_edges = set()
    for task in tasks:
        for dependency in task.depends_on:
            if dependency == task.plan_id:
                raise PlanValidationError(f"{task.plan_id} depends on itself")
            edge = (dependency, task.plan_id)
            if edge in seen_edges:
                raise PlanValidationError(f"duplicate dependency edge {dependency} -> {task.plan_id}")
            seen_edges.add(edge)
    cycle = _find_cycle(by_id)
    if cycle:
        raise PlanValidationError("dependency cycle: " + " -> ".join(cycle))
    tasks.sort(key=lambda item: (item.order, item.plan_id))
    return Plan(tasks, path.resolve(), digest)


class Inventory:
    def __init__(self, projects, tasks, labels):
        self.projects = list(projects)
        self.tasks = list(tasks)
        self.labels = list(labels)


class Action:
    def __init__(self, kind, entity, key, details=None):
        self.kind = kind
        self.entity = entity
        self.key = str(key)
        self.details = details or {}

    def as_dict(self):
        return {"details": self.details, "entity": self.entity, "key": self.key, "kind": self.kind}


def _imported_tasks(inventory, state_task_ids=None):
    """Plan IDs this import already created, as recorded by its own checkpoint.

    The checkpoint is the only link between a plan row and a task: the task body
    stays the user's text, and a plan ID means nothing outside its file.
    """
    tasks_by_id = {int(task["id"]): task for task in inventory.tasks}
    imported = {}
    for plan_id, task_id in (state_task_ids or {}).items():
        task = tasks_by_id.get(int(task_id))
        if task is None:
            raise ImportConflict(
                f"import state maps {plan_id!r} to missing Vikunja task id {task_id}"
            )
        imported[plan_id] = task
    return imported


def _referenced_tasks(plan, inventory):
    tasks_by_id = {str(task["id"]): task for task in inventory.tasks}
    references = plan.tracker_references()
    missing = [ref for ref in references if ref not in tasks_by_id]
    if missing:
        raise ImportConflict(
            "referenced tracker IDs were not found in the import scope: " + ", ".join(missing)
        )
    return {ref: tasks_by_id[ref] for ref in references}


def build_import_actions(plan, inventory, require_root=True, state_task_ids=None):
    actions = []
    project_index = ProjectIndex(inventory.projects)
    desired_paths = sorted({task.project_path for task in plan.tasks}, key=lambda p: (p.count("/"), p))
    roots = sorted({_normalize_path(path)[0] for path in desired_paths})
    if len(roots) != 1:
        raise ImportConflict(f"plan must have exactly one root project, got: {', '.join(roots)}")
    root = roots[0]
    root_matches = [p for p in inventory.projects if p["title"] == root and not int(p.get("parent_project_id") or 0)]
    if require_root and not root_matches:
        raise ImportConflict(f"required existing root project {root!r} was not found")
    if len(root_matches) > 1:
        raise ImportConflict(f"root project {root!r} is ambiguous: {[p['id'] for p in root_matches]}")

    known_paths = {project_index.path_for(p): p for p in inventory.projects}
    for path in desired_paths:
        parts = _normalize_path(path)
        for depth in range(1, len(parts) + 1):
            partial = " / ".join(parts[:depth])
            if partial in known_paths:
                if not any(a.entity == "project" and a.key == partial for a in actions):
                    actions.append(Action("reuse", "project", partial, {"id": known_paths[partial]["id"]}))
            elif depth == 1 and require_root:
                raise ImportConflict(f"required existing root project {partial!r} was not found")
            elif not any(a.entity == "project" and a.key == partial for a in actions):
                actions.append(Action("create", "project", partial, {"parent_path": " / ".join(parts[:depth - 1])}))

    imported = _imported_tasks(inventory, state_task_ids)
    known_tasks = {**_referenced_tasks(plan, inventory), **imported}
    project_tasks = {}
    for task in inventory.tasks:
        project_tasks.setdefault(int(task.get("project_id") or 0), []).append(task)

    for task in sorted(plan.tasks, key=lambda item: item.order):
        existing = imported.get(task.plan_id)
        desired_project = known_paths.get(task.project_path)
        if existing:
            actions.append(Action("reuse", "task", task.plan_id,
                                  {"id": existing["id"], "project_path": task.project_path}))
            continue
        if desired_project:
            collisions = [candidate for candidate in project_tasks.get(int(desired_project["id"]), [])
                          if candidate.get("title") == task.title]
            if collisions:
                raise ImportConflict(
                    f"task title {task.title!r} already exists in {task.project_path}; "
                    f"import only creates tasks and refuses to reuse one by title"
                )
        details = {
            "project_path": task.project_path, "title": task.title,
            "description": task.rendered_description(), "priority": task.priority,
            "done": task.done, "order": task.order,
        }
        details.update(task.fields)
        actions.append(Action("create", "task", task.plan_id, details))

    for task in sorted(plan.tasks, key=lambda item: item.order):
        existing = imported.get(task.plan_id)
        if task.manages_assignees():
            current = sorted(
                (u.get("username") or "") for u in ((existing or {}).get("assignees") or [])
            )
            desired = sorted(task.assignees)
            kind = "reuse" if existing and current == desired else "set"
            actions.append(Action(kind, "assignee", task.plan_id,
                                  {"usernames": desired, "current": current}))
        if task.bucket:
            current = task_bucket_titles(existing or {})
            kind = "reuse" if existing and task.bucket in current else "set"
            actions.append(Action(kind, "bucket", task.plan_id,
                                  {"title": task.bucket, "current": current}))

    labels_by_title = {label["title"]: label for label in inventory.labels}
    for title in sorted({label for task in plan.tasks for label in task.labels}):
        if title in labels_by_title:
            actions.append(Action("reuse", "label", title, {"id": labels_by_title[title]["id"]}))
        else:
            actions.append(Action("create", "label", title))
    relation_keys = existing_relation_keys(inventory.tasks)
    for base, relation_kind, other in plan.relation_edges():
        source = known_tasks.get(base)
        target = known_tasks.get(other)
        kind = "reuse" if source and target and (
            int(source["id"]), relation_kind, int(target["id"])
        ) in relation_keys else "create"
        actions.append(Action(kind, "relation", f"{base}:{relation_kind}:{other}", {
            "from": base, "relation_kind": relation_kind, "to": other,
        }))
    return actions


def attach_label(client, task_id, label_id):
    return client.post(f"/tasks/{int(task_id)}/labels", {"label_id": int(label_id)})


def _items(response):
    """List endpoints answer either a bare array or a paging envelope around one."""
    if isinstance(response, dict):
        items = response.get("items")
        return items if isinstance(items, list) else response
    return response


def _view_label(view):
    return f"{view.get('title') or 'kanban'} (id {view['id']})"


def resolve_kanban_view(client, project_id, reference=None):
    """The kanban view whose buckets are the project's board.

    A project may carry several kanban views, so picking the first one silently
    would move cards on an arbitrary board. Ambiguity is an error here, exactly
    as it is for project and task titles.
    """
    views = [v for v in _items(client.get(f"/projects/{int(project_id)}/views"))
             if v.get("view_kind") == "kanban"]
    if not views:
        raise NotFound(f"project id {project_id} has no kanban view")
    if reference is not None:
        text = str(reference).strip()
        matches = [v for v in views if str(v["id"]) == text or v.get("title") == text]
        if not matches:
            available = ", ".join(_view_label(v) for v in views)
            raise NotFound(f"kanban view {text!r} was not found; available: {available}")
        if len(matches) > 1:
            raise AmbiguousMatch(f"kanban view {text!r} is ambiguous")
        return matches[0]
    if len(views) > 1:
        available = ", ".join(_view_label(v) for v in views)
        raise AmbiguousMatch(
            f"project id {project_id} has several kanban views ({available}); name one explicitly"
        )
    return views[0]


def view_buckets(client, project_id, view_id):
    return _items(client.get(f"/projects/{int(project_id)}/views/{int(view_id)}/buckets"))


def board_columns(client, project_id, view_id):
    """Buckets with their tasks in one request, rather than a task query per column."""
    return _items(client.get(
        f"/projects/{int(project_id)}/views/{int(view_id)}/buckets/tasks?format=markdown"
    ))


def resolve_bucket(buckets, reference, missing=NotFound):
    text = str(reference).strip()
    matches = [b for b in buckets if str(b.get("id")) == text]
    if not matches:
        matches = [b for b in buckets if b.get("title") == text]
    if not matches:
        available = ", ".join(sorted(str(b.get("title")) for b in buckets))
        raise missing(f"bucket {text!r} not found; available: {available}")
    if len(matches) > 1:
        raise AmbiguousMatch(f"bucket {text!r} is ambiguous")
    return matches[0]


def place_in_bucket(client, project_id, view, bucket, task_id):
    """PUT the task into the bucket. Idempotent server-side; callers skip no-op moves."""
    if view.get("bucket_configuration_mode") == "filter":
        raise VikunjaError(
            f"kanban view {_view_label(view)} builds its buckets from filters; "
            f"a card moves there by changing the task fields the filters match, not by hand"
        )
    return client.put(
        f"/projects/{int(project_id)}/views/{int(view['id'])}"
        f"/buckets/{int(bucket['id'])}/tasks",
        {"task_id": int(task_id)},
    )


def create_bucket(client, project_id, view, title, position=None):
    """Add one column to a manually configured board.

    A filter board's columns are a query rather than a place, so a column added
    there would hold whatever the filter says and nothing a workflow moved into
    it; the refusal is the same one `place_in_bucket` already makes.
    """
    if view.get("bucket_configuration_mode") == "filter":
        raise VikunjaError(
            f"kanban view {_view_label(view)} builds its buckets from filters; "
            f"a lifecycle column cannot be added to it"
        )
    body = {"title": title}
    if position is not None:
        body["position"] = position
    return client.post(
        f"/projects/{int(project_id)}/views/{int(view['id'])}/buckets", body
    )


def update_bucket(client, project_id, view, bucket, title, position):
    # API v2 replaces these three fields with PUT; omitting limit would clear it.
    return client.put(
        f"/projects/{int(project_id)}/views/{int(view['id'])}/buckets/{int(bucket['id'])}",
        {"title": title, "position": position, "limit": bucket.get("limit") or 0},
    )


def task_bucket_in_view(task, view_id):
    """The task's bucket in one specific view, or None.

    A task carries one bucket entry per kanban view, so comparing titles across
    all of them would call a move a no-op when the card sits in a same-named
    column of a different board.
    """
    for bucket in (task.get("buckets") or []):
        if int(bucket.get("project_view_id") or 0) == int(view_id):
            return bucket
    return None


def task_bucket_titles(task):
    """The columns a task sits in, one per kanban view.

    `bucket_id` on a task is always 0 on API v2: the placement lives in the
    expanded `buckets` array instead, one entry per kanban view of the project.
    """
    return sorted({str(b.get("title")) for b in (task.get("buckets") or []) if b.get("title")})


def get_task_complete(client, task_id):
    query = urllib.parse.urlencode([
        ("format", "markdown"), ("expand", "subtasks"), ("expand", "buckets"),
        ("expand", "reactions"), ("expand", "comment_count"),
        ("expand", "time_entries_count"), ("expand", "is_unread"),
    ])
    task = client.get(f"/tasks/{int(task_id)}?{query}")
    task["comments"] = client.paginate(
        f"/tasks/{int(task_id)}/comments", {"format": "markdown"}
    )
    return task


BULK_WRITABLE_TASK_FIELDS = {
    "title", "description", "done", "priority", "due_date",
    "start_date", "end_date", "percent_done", "hex_color", "is_favorite",
}


def plan_bulk_updates(specification, tasks):
    if not isinstance(specification, list) or not specification:
        raise ImportConflict("bulk update file must contain a non-empty JSON array")
    index = TaskIndex(tasks)
    actions = []
    seen = set()
    for position, item in enumerate(specification, start=1):
        if not isinstance(item, dict) or set(item) != {"task", "changes"}:
            raise ImportConflict(f"bulk item {position} must contain only task and changes")
        task = index.resolve(item["task"])
        task_id = int(task["id"])
        if task_id in seen:
            raise ImportConflict(f"duplicate bulk target task id {task_id}")
        seen.add(task_id)
        changes = item["changes"]
        if not isinstance(changes, dict) or not changes:
            raise ImportConflict(f"bulk item {position} changes must be a non-empty object")
        forbidden = sorted(set(changes) - BULK_WRITABLE_TASK_FIELDS)
        if forbidden:
            raise ImportConflict(f"bulk fields are not writable: {', '.join(forbidden)}"
                                 + ("; move tasks with agent-box tracker move" if "project_id" in forbidden else ""))
        actions.append(Action("update", "task", task_id, {"changes": changes, "title": task.get("title", "")}))
    return actions


def execute_actions(actions, writer, apply=False):
    actions = list(actions)
    if apply:
        for action in actions:
            writer.execute(action)
    return {
        "applied": len(actions) if apply else 0,
        "dry_run": not apply,
        "planned": len(actions),
    }


class ImportState:
    def __init__(self, path, source_digest):
        self.path = Path(path)
        self.source_digest = source_digest
        if self.path.exists():
            from .. import migrations
            self.data = migrations.read(self.path, migrations.IMPORT_STATE)
            if self.data.get("source_digest") != source_digest:
                raise ImportConflict(
                    f"import source digest changed for {self.path}; use a new state file or restore the source"
                )
        else:
            self.data = {"source_digest": source_digest, "projects": {}, "tasks": {}, "relations": {}}

    def _save(self):
        from .. import migrations
        migrations.write(self.path, self.data, migrations.IMPORT_STATE)

    def record_project(self, path, project_id, disposition):
        self.data["projects"][path] = {"id": int(project_id), "disposition": disposition}
        self._save()

    def record_task(self, plan_id, task_id, disposition):
        self.data["tasks"][plan_id] = {"id": int(task_id), "disposition": disposition}
        self._save()

    def record_relation(self, key, disposition):
        self.data["relations"][key] = {"disposition": disposition}
        self._save()

    def task_id(self, plan_id):
        value = self.data["tasks"].get(plan_id)
        return value["id"] if value else None


def projects_from_client(client):
    return client.paginate("/projects", {"format": "markdown", "is_archived": "true"})


def inventory_from_client(client):
    projects = projects_from_client(client)
    tasks = client.paginate("/tasks", {"format": "markdown", "expand": ["buckets"]})
    labels = client.paginate("/labels")
    return Inventory(projects, tasks, labels)


def import_inventory(client, plan, project=None):
    """An explicit target root scopes the import to its subtree.

The plan's root is a local name for that target, so importing into a renamed or
nested staging root neither renames it nor creates an extra root above it.
"""
    if project is None:
        return inventory_from_client(client)
    projects = projects_from_client(client)
    root = ProjectIndex(projects).resolve(project)
    ids = descendants(projects, root["id"])
    names = {_normalize_path(task.project_path)[0] for task in plan.tasks}
    if len(names) != 1:
        raise ImportConflict("plan must have exactly one root project")
    projects = [dict(p) for p in projects if int(p["id"]) in ids]
    for p in projects:
        if int(p["id"]) == int(root["id"]):
            p.update(title=next(iter(names)), parent_project_id=0)
    tasks = collect_tasks(client, project=str(root["id"]), recursive=True)
    return Inventory(projects, tasks, client.paginate("/labels"))


def collect_tasks(client, query=None, project=None, recursive=False, filter_expr=None):
    """List or search tasks server-side.

    The API matches `q` as a case-insensitive substring of title or description;
    comments are never searched. Project scope and `filter_expr` are combined
    into one filter expression so the server, not this process, does the work.
    """
    clauses = []
    if filter_expr:
        clauses.append(f"({filter_expr})")
    if project:
        if str(project).isdigit() and not recursive:
            ids = [int(project)]
        else:
            projects = projects_from_client(client)
            resolved = ProjectIndex(projects).resolve(project)
            ids = (sorted(descendants(projects, resolved["id"])) if recursive else [int(resolved["id"])])
        clauses.append("project_id in " + ", ".join(str(i) for i in ids))
    params = {"format": "markdown", "expand": ["buckets"]}
    if clauses:
        params["filter"] = " && ".join(clauses)
    if query:
        params["q"] = query
    return sorted(client.paginate("/tasks", params), key=lambda t: int(t["id"]))


ACTION_COUNT_SUFFIX = {"reuse": "reused", "create": "created", "update": "updated",
                       "set": "set", "delete": "deleted"}


def summarize_actions(actions):
    counts = {}
    for action in actions:
        suffix = ACTION_COUNT_SUFFIX.get(action.kind, action.kind)
        counts[f"{action.entity}s_{suffix}"] = counts.get(f"{action.entity}s_{suffix}", 0) + 1
    return {
        "actions": [action.as_dict() for action in actions],
        "counts": dict(sorted(counts.items())),
        "dry_run": True,
    }


def _state_path(plan, state_dir):
    return Path(state_dir) / "imports" / f"{plan.digest}.json"


class Importer:
    def __init__(self, client, plan, state_dir=DEFAULT_STATE_DIR, allow_create_root=False, project=None):
        self.client = client
        self.plan = plan
        self.project = str(ProjectIndex(projects_from_client(client)).resolve(project)["id"]) if project is not None else None
        if self.project is not None:
            if allow_create_root:
                raise ImportConflict("--project selects an existing root; do not combine it with --create-root")
            scope = hashlib.sha256((client.url + "|" + self.project).encode()).hexdigest()
            state_dir = Path(state_dir) / scope
        self.state = ImportState(_state_path(plan, state_dir), plan.digest)
        self.allow_create_root = allow_create_root
        self._kanban_cache = {}

    def _state_task_ids(self):
        return {
            plan_id: value["id"]
            for plan_id, value in self.state.data["tasks"].items()
        }

    def dry_run(self):
        inventory = import_inventory(self.client, self.plan, self.project)
        state_task_ids = self._state_task_ids()
        return summarize_actions(build_import_actions(
            self.plan, inventory, not self.allow_create_root, state_task_ids=state_task_ids,
        ))

    def apply(self):
        inventory = import_inventory(self.client, self.plan, self.project)
        state_task_ids = self._state_task_ids()
        actions = build_import_actions(
            self.plan, inventory, not self.allow_create_root, state_task_ids=state_task_ids,
        )
        projects = ProjectIndex(inventory.projects)
        path_to_id = {projects.path_for(p): int(p["id"]) for p in inventory.projects}
        report = {"projects_created": 0, "projects_reused": 0, "tasks_created": 0,
                  "tasks_reused": 0, "labels_created": 0,
                  "labels_reused": 0, "relations_created": 0, "relations_reused": 0,
                  "assignees_set": 0, "assignees_reused": 0, "buckets_set": 0,
                  "buckets_reused": 0}

        for action in [a for a in actions if a.entity == "project"]:
            if action.kind == "reuse":
                project_id = int(action.details["id"])
                report["projects_reused"] += 1
            else:
                parent_id = path_to_id.get(action.details["parent_path"], 0)
                title = _normalize_path(action.key)[-1]
                created = self.client.post("/projects?format=markdown", {
                    "title": title, "parent_project_id": parent_id,
                })
                project_id = int(created["id"])
                path_to_id[action.key] = project_id
                report["projects_created"] += 1
            self.state.record_project(action.key, project_id, action.kind)

        plan_by_id = {task.plan_id: task for task in self.plan.tasks}
        task_ids = {}
        for action in [a for a in actions if a.entity == "task"]:
            task = plan_by_id[action.key]
            if action.kind == "create":
                body = {
                    "title": task.title, "description": task.rendered_description(),
                    "priority": task.priority, "done": task.done,
                }
                body.update(task.fields)
                created = self.client.post(
                    f"/projects/{path_to_id[task.project_path]}/tasks?format=markdown", body
                )
                task_id = int(created["id"])
                report["tasks_created"] += 1
            else:
                task_id = int(action.details["id"])
                report["tasks_reused"] += 1
            task_ids[action.key] = task_id
            self.state.record_task(action.key, task_id, action.kind)

        labels = {label["title"]: int(label["id"]) for label in inventory.labels}
        for title in sorted({label for task in self.plan.tasks for label in task.labels}):
            if title in labels:
                report["labels_reused"] += 1
            else:
                created = self.client.post("/labels?format=markdown", {"title": title})
                labels[title] = int(created["id"])
                report["labels_created"] += 1
        current = {
            int(t["id"]): {int(label["id"]) for label in (t.get("labels") or [])}
            for t in inventory.tasks
        }
        for task in self.plan.tasks:
            task_id = task_ids[task.plan_id]
            attached = current.get(task_id, set())
            for title in task.labels:
                label_id = labels[title]
                if label_id not in attached:
                    attach_label(self.client, task_id, label_id)

        for action in [a for a in actions if a.entity == "assignee"]:
            if action.kind == "reuse":
                report["assignees_reused"] += 1
                continue
            users = [self._resolve_user(name) for name in action.details["usernames"]]
            self.client.put(f"/tasks/{task_ids[action.key]}/assignees/bulk",
                            {"assignees": [{"id": user_id} for user_id in users]})
            report["assignees_set"] += 1

        for action in [a for a in actions if a.entity == "bucket"]:
            if action.kind == "reuse":
                report["buckets_reused"] += 1
                continue
            task = plan_by_id[action.key]
            self._move_to_bucket(
                task_ids[action.key], path_to_id[task.project_path], action.details["title"]
            )
            report["buckets_set"] += 1

        refreshed = import_inventory(self.client, self.plan, self.project)
        resolved = {**{ref: int(ref) for ref in self.plan.tracker_references()}, **task_ids}
        relations = existing_relation_keys(refreshed.tasks)
        for base, kind, other in self.plan.relation_edges():
            key = f"{base}:{kind}:{other}"
            numeric_key = (resolved[base], kind, resolved[other])
            if numeric_key in relations:
                report["relations_reused"] += 1
                self.state.record_relation(key, "reuse")
                continue
            self.client.post(f"/tasks/{resolved[base]}/relations", {
                "other_task_id": resolved[other], "relation_kind": kind,
            })
            report["relations_created"] += 1
            self.state.record_relation(key, "create")
        return report

    def _resolve_user(self, username):
        found = self.client.paginate("/users", {"q": username})
        exact = [user for user in found if user.get("username") == username]
        if not exact:
            raise ImportConflict(f"assignee {username!r} did not match any Vikunja user")
        if len(exact) > 1:
            raise AmbiguousMatch(f"assignee {username!r} matched multiple users")
        return int(exact[0]["id"])

    def _kanban(self, project_id):
        cached = self._kanban_cache.get(int(project_id))
        if cached is None:
            try:
                view = resolve_kanban_view(self.client, project_id)
            except NotFound as exc:
                raise ImportConflict(f"{exc} for buckets") from None
            cached = (view, view_buckets(self.client, project_id, view["id"]))
            self._kanban_cache[int(project_id)] = cached
        return cached

    def _move_to_bucket(self, task_id, project_id, bucket_title):
        view, buckets = self._kanban(project_id)
        bucket = resolve_bucket(buckets, bucket_title, missing=ImportConflict)
        return place_in_bucket(self.client, project_id, view, bucket, task_id)


def existing_relation_keys(tasks):
    keys = set()
    for task in tasks:
        task_id = int(task["id"])
        related = task.get("related_tasks") or {}
        for kind, others in related.items():
            for other in others or []:
                keys.add((task_id, kind, int(other["id"])))
    return keys


def validation_summary(plan):
    """Local validation result. Tracker references are resolved only on import."""
    return {
        "digest": plan.digest,
        "tracker_references": list(plan.tracker_references()),
        "projects": len({t.project_path for t in plan.tasks}),
        "relations": len(plan.relation_edges()),
        "tasks": len(plan.tasks),
        "valid": True,
    }


def project_tree(projects):
    index = ProjectIndex(projects)
    return [{"id": p["id"], "parent_project_id": p.get("parent_project_id", 0),
             "path": index.path_for(p), "title": p["title"]}
            for p in sorted(projects, key=lambda item: index.path_for(item))]


def descendants(projects, root_id):
    wanted = {int(root_id)}
    changed = True
    while changed:
        changed = False
        for project in projects:
            if int(project.get("parent_project_id") or 0) in wanted and int(project["id"]) not in wanted:
                wanted.add(int(project["id"]))
                changed = True
    return wanted


def project_children(projects, parent_ref, recursive=False):
    index = ProjectIndex(projects)
    parent = index.resolve(parent_ref)
    parent_id = int(parent["id"])
    if recursive:
        child_ids = descendants(projects, parent_id) - {parent_id}
    else:
        child_ids = {int(project["id"]) for project in projects
                     if int(project.get("parent_project_id") or 0) == parent_id}
    children = [project for project in projects if int(project["id"]) in child_ids]
    return [{"id": project["id"],
             "parent_project_id": project.get("parent_project_id", 0),
             "path": index.path_for(project), "title": project["title"]}
            for project in sorted(children, key=index.path_for)]


TASK_RELATION_KINDS = {"parent": "parenttask", "child": "subtask", "blocks": "blocking",
                  "blocked-by": "blocked", "related": "related", "duplicate": "duplicateof"}

RELATION_INVERSE = {
    "subtask": "parenttask", "parenttask": "subtask", "blocking": "blocked",
    "blocked": "blocking", "related": "related", "duplicateof": "duplicates",
    "duplicates": "duplicateof", "precedes": "follows", "follows": "precedes",
    "copiedfrom": "copiedto", "copiedto": "copiedfrom",
}


def relation_edges(tasks):
    """Every relation once, in one orientation.

    Vikunja records both ends of a relation, so a diff over the raw pairs would
    ask to create or delete the same edge twice and fail on the second call.
    """
    edges = set()
    for task in tasks:
        base = str(task["id"])
        for kind, others in (task.get("related_tasks") or {}).items():
            inverse = RELATION_INVERSE.get(kind)
            for other in others or []:
                other_id = str(other["id"])
                if inverse and base > other_id:
                    edges.add((other_id, inverse, base))
                else:
                    edges.add((base, kind, other_id))
    return edges
